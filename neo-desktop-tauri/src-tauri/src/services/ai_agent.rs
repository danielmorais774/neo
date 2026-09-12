// AI Agent service - communicates with Neo Core via gRPC bidirectional streaming

use crate::grpc_gen::neo_desktop::neo_desktop_service_client::NeoDesktopServiceClient;
use crate::ext_bridge::ExtBridge;
use crate::grpc_gen::neo_desktop::{
    core_message, desktop_message, CoreMessage, DesktopMessage, ToolResult, UserInput,
    VoiceSettings,
};
use log::{error, info, warn};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter};
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::Channel;
use uuid::Uuid;

/// Prefix neo-core puts on a thinking message that reports a self-continuation
/// step starting (`CYCLE_PROGRESS_MARKER` in modules/desktop/ai_service.py). Both
/// sides must agree on it; it is stripped before the text reaches the chat.
const CYCLE_PROGRESS_MARKER: &str = "[[CYCLE]]";

/// Shared handle to the active bidi stream's input sender. Lets UI-driven actions
/// (command approval/rejection via `respond_to_command`) inject a message into the
/// in-flight task from outside `execute_task`. Set when a task starts, cleared on
/// any exit path via `SlotGuard`.
pub type StreamTxSlot = Arc<Mutex<Option<mpsc::Sender<DesktopMessage>>>>;

/// Clears the shared stream-sender slot when the task ends (any return/panic).
struct SlotGuard(StreamTxSlot);

impl Drop for SlotGuard {
    fn drop(&mut self) {
        if let Ok(mut slot) = self.0.lock() {
            *slot = None;
        }
    }
}

/// Turno cuja ENTRADA veio de áudio transcrito (STT do neo-voice), não do teclado.
///
/// É independente de `mode="live"`, que descreve a SAÍDA (resposta falada, sem
/// markdown): existe voz que responde em texto e texto que responde falando. O
/// neo-core usa `input_source=voice` para tolerar erro de transcrição — nome torto,
/// palavra colada, trecho em outro idioma. Ver
/// neo-core/docs/voice-input-normalization-plan.md §3.
#[derive(Debug, Clone)]
pub struct VoiceInput {
    /// hoje sempre "voice"; string p/ abrir espaço a outras origens sem quebrar o wire
    pub source: String,
    /// transcrição ANTES da correção do nome no neo-voice (auditoria/eval)
    pub raw: Option<String>,
    /// parakeet-gpu / parakeet-cpu / openrouter
    pub engine: Option<String>,
}

impl VoiceInput {
    /// `None` quando o cliente não marcou origem (texto digitado) — assim a
    /// tolerância a erro de ASR nunca vale para o que o usuário escreveu.
    pub fn from_parts(
        source: Option<String>,
        raw: Option<String>,
        engine: Option<String>,
    ) -> Option<Self> {
        let source = source.filter(|s| !s.trim().is_empty())?;
        Some(Self {
            source,
            raw: raw.filter(|s| !s.trim().is_empty()),
            engine: engine.filter(|s| !s.trim().is_empty()),
        })
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct AgentEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub data: serde_json::Value,
    pub timestamp: String,
}

pub struct GrpcClient {
    device_id: String,
    hardware_id: String,
    user_id: String,
    neo_core_url: String,
    session_id: String,
}

impl GrpcClient {
    pub fn new(device_id: &str, hardware_id: &str, user_id: &str, neo_core_url: &str) -> Self {
        Self {
            device_id: device_id.to_string(),
            hardware_id: hardware_id.to_string(),
            user_id: user_id.to_string(),
            neo_core_url: neo_core_url.to_string(),
            session_id: Uuid::new_v4().to_string(),
        }
    }

    pub fn set_session_id(&mut self, session_id: &str) {
        self.session_id = session_id.to_string();
    }


    /// Conecta, abre o stream bidi e enfileira a mensagem inicial.
    ///
    /// Uma tentativa completa e independente: o canal é reconstruído do zero
    /// porque um canal aberto contra um servidor que reiniciou fica inutilizável.
    /// Falhar aqui é seguro de repetir — o servidor ainda não consumiu nada.
    async fn open_stream(
        endpoint: &str,
        first: &DesktopMessage,
        stream_tx_slot: &StreamTxSlot,
    ) -> Result<
        (
            tonic::Response<tonic::Streaming<CoreMessage>>,
            mpsc::Sender<DesktopMessage>,
        ),
        String,
    > {
        let channel = Channel::from_shared(endpoint.to_string())
            .map_err(|e| format!("Failed to create channel: {}", e))?
            .connect()
            .await
            .map_err(|e| format!("Failed to connect: {}", e))?;
        let mut client = NeoDesktopServiceClient::new(channel);

        // `tx` fica vivo no escopo do chamador até ser explicitamente dropado no
        // fim do loop — é isso que fecha o nosso lado do stream.
        let (tx, rx) = mpsc::channel::<DesktopMessage>(16);

        // Expose this task's sender so `respond_to_command` (UI approval) can push a
        // CommandApproval into the live stream. The guard clears it on any exit path.
        if let Ok(mut slot) = stream_tx_slot.lock() {
            *slot = Some(tx.clone());
        }

        // Push the initial message into the channel BEFORE calling
        // execute_task. Tonic awaits response headers from the server, and
        // grpc.aio in the Python server only fully spins up the stream after
        // it has at least one inbound frame — so we need the message in the
        // buffer before the call. Channel is buffered (16), so this won't
        // block.
        tx.send(first.clone())
            .await
            .map_err(|e| format!("Failed to enqueue initial message: {}", e))?;
        info!("[GrpcClient] initial DesktopMessage enqueued");

        // Token de pareamento na METADATA da chamada (F5), nunca no corpo da
        // mensagem: o `metadata` do user_input alimenta o contexto do prompt
        // (`_build_current_device_context` no core), e credencial não pode
        // chegar perto disso.
        let mut request = tonic::Request::new(ReceiverStream::new(rx));
        if let Some(token) = crate::device_token() {
            match token.parse::<tonic::metadata::MetadataValue<tonic::metadata::Ascii>>() {
                Ok(value) => {
                    request.metadata_mut().insert("x-device-token", value);
                }
                Err(e) => warn!("[GrpcClient] token de device inválido para metadata: {}", e),
            }
        }

        let response = client
            .execute_task(request)
            .await
            .map_err(|e| format!("gRPC call failed: {}", e))?;
        Ok((response, tx))
    }

    /// Run a task end-to-end: opens a bidi stream, sends the user input,
    /// emits each CoreMessage to the frontend in real time, and ends as
    /// soon as we receive a final response or an error.
    #[allow(clippy::too_many_arguments)]
    pub async fn execute_task(
        &self,
        app_handle: AppHandle,
        message: String,
        model: Option<&str>,
        provider: Option<&str>,
        mode: Option<&str>,
        attachments: Option<Vec<String>>,
        voice: Option<VoiceInput>,
        cancel: std::sync::Arc<tokio::sync::Notify>,
        stream_tx_slot: StreamTxSlot,
        ext_bridge: ExtBridge,
    ) -> Result<(), String> {
        info!(
            "[GrpcClient] execute_task: {} (session={})",
            message, self.session_id
        );

        let task_start = std::time::Instant::now();

        let neo_core_url = self
            .neo_core_url
            .replace("http://", "")
            .replace("https://", "");
        info!("[GrpcClient] Connecting to gRPC server: {}", neo_core_url);
        let endpoint = format!("http://{}", neo_core_url);

        let homedir = dirs::home_dir()
            .unwrap_or_default()
            .to_string_lossy()
            .to_string();
        let platform = std::env::consts::OS;
        let arch = std::env::consts::ARCH;

        let mut metadata = HashMap::new();
        metadata.insert("timestamp".to_string(), chrono::Utc::now().to_rfc3339());
        metadata.insert("client".to_string(), "neo-desktop-tauri".to_string());
        if let Some(m) = model {
            metadata.insert("model".to_string(), m.to_string());
        }
        if let Some(p) = provider {
            metadata.insert("provider".to_string(), p.to_string());
        }
        // Origem do turno; "live" faz o neo-core responder em texto falado/compacto.
        if let Some(md) = mode {
            metadata.insert("mode".to_string(), md.to_string());
        }
        // Origem da ENTRADA. `mode` fala da saída; isto fala de como o texto foi
        // produzido: `input_source=voice` liga a tolerância a erro de ASR no core,
        // e `asr_raw` guarda o que o Parakeet realmente escreveu (auditoria/eval).
        if let Some(v) = &voice {
            metadata.insert("input_source".to_string(), v.source.clone());
            if let Some(raw) = &v.raw {
                metadata.insert("asr_raw".to_string(), raw.clone());
            }
            if let Some(engine) = &v.engine {
                metadata.insert("asr_engine".to_string(), engine.clone());
            }
        }
        // Ids do File Store anexados a ESTA mensagem. O metadata do gRPC é
        // map<string,string>, então a lista vai serializada (mesmo padrão do
        // media_context); o core parseia e monta o manifesto do turno.
        // Ver neo-core/docs/file-store-plan.md §5.
        if let Some(ids) = attachments.filter(|v| !v.is_empty()) {
            metadata.insert(
                "attachments".to_string(),
                serde_json::to_string(&ids).unwrap_or_else(|_| "[]".to_string()),
            );
        }
        metadata.insert("device_id".to_string(), self.device_id.clone());
        if !self.hardware_id.is_empty() {
            metadata.insert("hardware_id".to_string(), self.hardware_id.clone());
        }
        metadata.insert("os_platform".to_string(), platform.to_string());
        metadata.insert("os_arch".to_string(), arch.to_string());
        metadata.insert(
            "os_release".to_string(),
            crate::services::system_info::os_release(),
        );
        metadata.insert("home_dir".to_string(), homedir.clone());
        // Pastas do usuário resolvidas pelo SO, não montadas com `{home}/Nome`: no
        // Linux o XDG as localiza (pt-BR = ~/Documentos, ~/Área de trabalho) e no
        // Windows o separador é `\` — o hardcode gerava caminho inexistente lá e
        // `C:\Users\x/Downloads` aqui. Acertava só no macOS, por coincidência.
        metadata.insert("downloads_dir".to_string(), user_dir(dirs::download_dir(), &homedir, "Downloads"));
        metadata.insert("documents_dir".to_string(), user_dir(dirs::document_dir(), &homedir, "Documents"));
        metadata.insert("desktop_dir".to_string(), user_dir(dirs::desktop_dir(), &homedir, "Desktop"));

        // Snapshot of open music tabs from the browser extension (Etapa 3). Lets the
        // music skill reuse an existing YouTube/Spotify tab via media_context.browser_tabs
        // instead of opening a new one. Always sent (cheap; empty when nothing connected).
        let media_context =
            serde_json::json!({ "browser_tabs": ext_bridge.snapshot() }).to_string();
        metadata.insert("media_context".to_string(), media_context);

        let user_input = UserInput { message, metadata };

        let desktop_msg = DesktopMessage {
            session_id: self.session_id.clone(),
            user_id: self.user_id.clone(),
            content: Some(desktop_message::Content::UserInput(user_input)),
        };

        // O guard limpa o slot do sender em qualquer saída — inclusive entre as
        // tentativas de abertura abaixo, que descartam o `tx` anterior.
        let _slot_guard = SlotGuard(stream_tx_slot.clone());

        // Abrir o stream é RETENTÁVEL: enquanto o servidor não aceitou o
        // DesktopMessage inicial nada foi processado, então repetir não duplica
        // turno nem mensagem no banco. Isso cobre a janela em que o neo-core está
        // de pé mas reiniciando (hot-reload por edição de arquivo, deploy, restart
        // do container): antes, qualquer mensagem enviada nesses ~2-5s morria como
        // `status: Unknown, message: "transport error"` e o turno era perdido, com
        // o usuário tendo que reiniciar o container na mão.
        const OPEN_ATTEMPTS: u32 = 4;
        let (response, tx) = {
            let mut attempt: u32 = 1;
            loop {
                match Self::open_stream(&endpoint, &desktop_msg, &stream_tx_slot).await {
                    Ok(v) => break v,
                    Err(e) if attempt < OPEN_ATTEMPTS => {
                        // 400ms, 1.2s, 3.6s — cobre o boot do uvicorn sem travar a UI.
                        let backoff = 400u64 * 3u64.pow(attempt - 1);
                        warn!(
                            "[GrpcClient] abertura do stream falhou ({}/{}): {} — nova tentativa em {}ms",
                            attempt, OPEN_ATTEMPTS, e, backoff
                        );
                        tokio::time::sleep(tokio::time::Duration::from_millis(backoff)).await;
                        attempt += 1;
                    }
                    Err(e) => return Err(e),
                }
            }
        };
        info!("[GrpcClient] bidi stream opened with neo-core");

        let mut stream = response.into_inner();

        // Read CoreMessages and emit each one to the frontend as it arrives.
        // Whole-stream timeout: gives plenty of room for long agent runs but
        // protects us from a stuck server.
        let total_timeout = tokio::time::Duration::from_secs(300);
        let task_deadline = tokio::time::Instant::now() + total_timeout;

        let mut got_final = false;
        let mut last_response_message: Option<String> = None;
        let mut last_total_iterations: i32 = 0;
        // Pattern A: active TTS voice carried with the final reply (engine/voice/language/label).
        let mut last_voice_json: Option<serde_json::Value> = None;
        // Voice imitation: ordered speech segments ({text, voice}) for multi-voice replies.
        let mut last_segments_json: Option<serde_json::Value> = None;
        // Arquivos gerados/catalogados na resposta final (ex: um .zip via file_save) —
        // {id, name, sizeBytes}, mesma forma que o neo-panel usa (AttachmentRef).
        let mut last_files_json: Option<serde_json::Value> = None;
        let mut active_session_id = self.session_id.clone();

        loop {
            let next = tokio::select! {
                biased;
                // User pressed Stop: drop the stream (cancels the server RPC) and
                // tell the UI the run was interrupted.
                _ = cancel.notified() => {
                    info!("[GrpcClient] execution cancelled by user");
                    let stopped_evt = AgentEvent {
                        event_type: "complete".to_string(),
                        data: serde_json::json!({
                            "success": true,
                            "stopped": true,
                            "sessionId": active_session_id.clone(),
                        }),
                        timestamp: chrono::Utc::now().to_rfc3339(),
                    };
                    let _ = app_handle.emit("agent-event", &stopped_evt);
                    return Ok(()); // dropping `stream`/`tx` here cancels the RPC
                }
                res = tokio::time::timeout_at(task_deadline, stream.message()) => res,
            };

            match next {
                Ok(Ok(Some(core_msg))) => {
                    if !core_msg.session_id.is_empty() && core_msg.session_id != active_session_id {
                        let previous_session_id = active_session_id.clone();
                        active_session_id = core_msg.session_id.clone();
                        let evt = AgentEvent {
                            event_type: "session_changed".to_string(),
                            data: serde_json::json!({
                                "sessionId": active_session_id.clone(),
                                "previousSessionId": previous_session_id,
                                "reason": "core_assigned_session",
                            }),
                            timestamp: chrono::Utc::now().to_rfc3339(),
                        };
                        let _ = app_handle.emit("agent-event", &evt);
                    }

                    // Emit a real-time event for this CoreMessage.
                    if let Some(evt) = self.core_message_to_event(&core_msg) {
                        if let Err(e) = app_handle.emit("agent-event", &evt) {
                            warn!("[GrpcClient] failed to emit agent-event: {}", e);
                        }
                    }

                    // Music actuation (neo-music-feature): the generic client-tool
                    // round-trip isn't wired in this client, so we execute the media
                    // tools (open_url / launch_uri / media_command) inline here and
                    // send the ToolResult back so neo-core can confirm the playback.
                    if let Some(core_message::Content::ToolRequest(t)) = &core_msg.content {
                        // `observar` (docs/playlists-skill-plan.md §7.5): o neo-core só
                        // manda este campo quando existe inscrição ARMADA para este
                        // aparelho. É ele que liga a vigia da fila — e a ausência dele
                        // (o caso normal, "toca um AC/DC") desliga qualquer vigia velha.
                        if is_media_tool(&t.tool_name) || t.tool_name == "media_command" {
                            apply_watch_directive(&ext_bridge, &self.device_id, &t.arguments);
                        }
                        if t.tool_name == "media_command" {
                            // Browser-tab control via the extension bridge (Etapa 3):
                            // round-trips to the extension (focus tab + play videoId).
                            // Run off the read loop so streaming events keep flowing.
                            let bridge = ext_bridge.clone();
                            let tx2 = tx.clone();
                            let arguments = t.arguments.clone();
                            let tool_call_id = t.tool_call_id.clone();
                            let tool_name = t.tool_name.clone();
                            let sid = active_session_id.clone();
                            let uid = self.user_id.clone();
                            tokio::spawn(async move {
                                let (success, result_text, error_text) =
                                    run_media_command(&bridge, &arguments).await;
                                let tr = DesktopMessage {
                                    session_id: sid,
                                    user_id: uid,
                                    content: Some(desktop_message::Content::ToolResult(
                                        ToolResult {
                                            tool_call_id,
                                            tool_name,
                                            success,
                                            result: result_text,
                                            error: error_text,
                                        },
                                    )),
                                };
                                if let Err(e) = tx2.send(tr).await {
                                    warn!("[GrpcClient] failed to send media_command result: {}", e);
                                }
                            });
                        } else if is_media_tool(&t.tool_name) {
                            let (success, result_text, error_text) =
                                run_media_tool(&t.tool_name, &t.arguments);
                            let tr = DesktopMessage {
                                session_id: active_session_id.clone(),
                                user_id: self.user_id.clone(),
                                content: Some(desktop_message::Content::ToolResult(ToolResult {
                                    tool_call_id: t.tool_call_id.clone(),
                                    tool_name: t.tool_name.clone(),
                                    success,
                                    result: result_text,
                                    error: error_text,
                                })),
                            };
                            if let Err(e) = tx.send(tr).await {
                                warn!("[GrpcClient] failed to send media tool result: {}", e);
                            }
                        } else if t.tool_name == "execute_command" || t.tool_name == "run_command" {
                            // Shell execution requested by neo-core. Two origins:
                            //   `execute_command` — o comando já passou pelo
                            //     CommandRequest → aprovação do usuário.
                            //   `run_command`     — passo de um plano multi-step
                            //     (PlanManager). Não tinha braço aqui: o core mandava
                            //     o ToolRequest, nada executava, nenhum ToolResult
                            //     voltava e o turno ficava "Executando" pra sempre
                            //     (sessão 18772e25 — "me mostra qualquer outra imagem
                            //     da mesma pasta" parou no passo 1/2).
                            // Execute OFF the read loop (in a spawned task) so streaming
                            // events keep flowing, then send the ToolResult back. Without
                            // this the stream would hang waiting for a result that never
                            // comes.
                            let tx2 = tx.clone();
                            let arguments = t.arguments.clone();
                            let tool_call_id = t.tool_call_id.clone();
                            let tool_name = t.tool_name.clone();
                            let needs_approval = t.requires_approval;
                            let sid = active_session_id.clone();
                            let uid = self.user_id.clone();
                            tokio::spawn(async move {
                                // Não existe UI de aprovação para um ToolRequest — só
                                // para CommandRequest. O core roteia passo destrutivo
                                // por lá; se mesmo assim chegar um aqui, recuse com
                                // resultado em vez de executar sem permissão (ou de
                                // travar esperando um "aprovar" que ninguém pode dar).
                                let (success, result_text, error_text) = if needs_approval {
                                    (
                                        false,
                                        String::new(),
                                        "comando exige aprovação e chegou como tool_request \
                                         (sem UI de aprovação) — não executado"
                                            .to_string(),
                                    )
                                } else {
                                    tokio::task::spawn_blocking(move || {
                                        run_command_blocking(&arguments)
                                    })
                                    .await
                                    .unwrap_or((
                                        false,
                                        String::new(),
                                        "falha ao executar o comando".to_string(),
                                    ))
                                };
                                let tr = DesktopMessage {
                                    session_id: sid,
                                    user_id: uid,
                                    content: Some(desktop_message::Content::ToolResult(
                                        ToolResult {
                                            tool_call_id,
                                            tool_name,
                                            success,
                                            result: result_text,
                                            error: error_text,
                                        },
                                    )),
                                };
                                if let Err(e) = tx2.send(tr).await {
                                    warn!("[GrpcClient] failed to send command result: {}", e);
                                }
                            });
                        } else if t.tool_name == "save_generated_file" {
                            // "Bota esse arquivo no meu Desktop/Downloads/pasta X": o Neo
                            // já tem o file_id (chamou file_save no sandbox) — só o cliente
                            // sabe a URL certa do catálogo (neo_core_http_url), por isso
                            // essa tool existe em vez de deixar o modelo escrever um comando
                            // (sessão ef4f723c: 3 tentativas de curl com host/porta
                            // inventados, nenhuma funcionou). Off the read loop, mesmo
                            // padrão de execute_command.
                            let tx2 = tx.clone();
                            let arguments = t.arguments.clone();
                            let tool_call_id = t.tool_call_id.clone();
                            let tool_name = t.tool_name.clone();
                            let sid = active_session_id.clone();
                            let uid = self.user_id.clone();
                            tokio::spawn(async move {
                                let args: serde_json::Value =
                                    serde_json::from_str(&arguments).unwrap_or(serde_json::Value::Null);
                                let file_id = args.get("file_id").and_then(|v| v.as_str()).unwrap_or("");
                                let file_name = args
                                    .get("file_name")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("arquivo");
                                let destination = args
                                    .get("destination")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("desktop");
                                // overwrite só quando o cérebro manda explicitamente true
                                // (depois de um status="conflict"). Padrão false ⇒ nunca
                                // sobrescreve na primeira tentativa.
                                let overwrite = args
                                    .get("overwrite")
                                    .and_then(|v| v.as_bool())
                                    .unwrap_or(false);
                                let (success, result_text, error_text) = if file_id.is_empty() {
                                    (false, String::new(), "file_id ausente".to_string())
                                } else {
                                    match crate::commands::save_generated_file_to_path(
                                        file_id, file_name, destination, overwrite,
                                    )
                                    .await
                                    {
                                        // Result vai como JSON string ({status,path,existing}) —
                                        // o core (process_tool_result) trata saved vs conflict.
                                        Ok(v) => (true, v.to_string(), String::new()),
                                        Err(e) => (false, String::new(), e),
                                    }
                                };
                                let tr = DesktopMessage {
                                    session_id: sid,
                                    user_id: uid,
                                    content: Some(desktop_message::Content::ToolResult(
                                        ToolResult {
                                            tool_call_id,
                                            tool_name,
                                            success,
                                            result: result_text,
                                            error: error_text,
                                        },
                                    )),
                                };
                                if let Err(e) = tx2.send(tr).await {
                                    warn!(
                                        "[GrpcClient] failed to send save_generated_file result: {}",
                                        e
                                    );
                                }
                            });
                        } else if t.tool_name == "write_file" {
                            // Escrever arquivo SEM passar pelo shell. Pelo
                            // `run_command` o conteúdo vira heredoc e aspas,
                            // crase, `$` ou um "EOF" dentro do texto corrompem o
                            // arquivo calado. Aqui o conteúdo é argumento, não
                            // script. Off the read loop, mesmo padrão dos irmãos.
                            let tx2 = tx.clone();
                            let arguments = t.arguments.clone();
                            let tool_call_id = t.tool_call_id.clone();
                            let tool_name = t.tool_name.clone();
                            let sid = active_session_id.clone();
                            let uid = self.user_id.clone();
                            tokio::spawn(async move {
                                let args: serde_json::Value =
                                    serde_json::from_str(&arguments).unwrap_or(serde_json::Value::Null);
                                let path = args.get("path").and_then(|v| v.as_str()).unwrap_or("");
                                let content =
                                    args.get("content").and_then(|v| v.as_str()).unwrap_or("");
                                let mode =
                                    args.get("mode").and_then(|v| v.as_str()).unwrap_or("write");
                                // Só sobrescreve quando o cérebro manda true de
                                // propósito, depois de ver um status="conflict".
                                let overwrite = args
                                    .get("overwrite")
                                    .and_then(|v| v.as_bool())
                                    .unwrap_or(false);
                                let (success, result_text, error_text) = if path.is_empty() {
                                    (false, String::new(), "path ausente".to_string())
                                } else {
                                    match crate::commands::write_text_file(
                                        path, content, mode, overwrite,
                                    )
                                    .await
                                    {
                                        // JSON string ({status,path,bytes_written}) — o core
                                        // trata saved vs conflict como no save_generated_file.
                                        Ok(v) => (true, v.to_string(), String::new()),
                                        Err(e) => (false, String::new(), e),
                                    }
                                };
                                let tr = DesktopMessage {
                                    session_id: sid,
                                    user_id: uid,
                                    content: Some(desktop_message::Content::ToolResult(
                                        ToolResult {
                                            tool_call_id,
                                            tool_name,
                                            success,
                                            result: result_text,
                                            error: error_text,
                                        },
                                    )),
                                };
                                if let Err(e) = tx2.send(tr).await {
                                    warn!("[GrpcClient] failed to send write_file result: {}", e);
                                }
                            });
                        } else if t.tool_name == "request_user_file" {
                            // O cérebro está PUXANDO um arquivo do disco do usuário pro
                            // File Store (plano "arquivos sob iniciativa do cérebro"):
                            // mode="pick" abre o seletor nativo; mode="path" sobe o
                            // caminho citado. Reusa pick_attachment_files + upload_path_to_store.
                            // Off the read loop, mesmo padrão de save_generated_file.
                            let tx2 = tx.clone();
                            let arguments = t.arguments.clone();
                            let tool_call_id = t.tool_call_id.clone();
                            let tool_name = t.tool_name.clone();
                            let sid = active_session_id.clone();
                            let uid = self.user_id.clone();
                            let app = app_handle.clone();
                            tokio::spawn(async move {
                                let (success, result_text, error_text) =
                                    run_request_user_file(&app, &arguments, &sid, &uid).await;
                                let tr = DesktopMessage {
                                    session_id: sid,
                                    user_id: uid,
                                    content: Some(desktop_message::Content::ToolResult(
                                        ToolResult {
                                            tool_call_id,
                                            tool_name,
                                            success,
                                            result: result_text,
                                            error: error_text,
                                        },
                                    )),
                                };
                                if let Err(e) = tx2.send(tr).await {
                                    warn!(
                                        "[GrpcClient] failed to send request_user_file result: {}",
                                        e
                                    );
                                }
                            });
                        } else {
                            // Rede de segurança: um ToolRequest sem braço acima NUNCA
                            // pode ser ignorado em silêncio — o core fica esperando um
                            // ToolResult que não vem e o turno trava em "Executando"
                            // até a conexão cair. Devolve a falha para o core decidir
                            // (adaptar o plano, ou fechar o turno com uma mensagem).
                            warn!(
                                "[GrpcClient] tool_request sem handler no cliente: {}",
                                t.tool_name
                            );
                            let tr = DesktopMessage {
                                session_id: active_session_id.clone(),
                                user_id: self.user_id.clone(),
                                content: Some(desktop_message::Content::ToolResult(ToolResult {
                                    tool_call_id: t.tool_call_id.clone(),
                                    tool_name: t.tool_name.clone(),
                                    success: false,
                                    result: String::new(),
                                    error: format!(
                                        "este cliente não sabe executar a ferramenta '{}'",
                                        t.tool_name
                                    ),
                                })),
                            };
                            if let Err(e) = tx.send(tr).await {
                                warn!("[GrpcClient] failed to send unknown-tool result: {}", e);
                            }
                        }
                    }

                    // Detect terminal conditions.
                    if let Some(content) = &core_msg.content {
                        match content {
                            core_message::Content::Response(r) => {
                                if r.is_final {
                                    got_final = true;
                                    last_response_message = Some(r.message.clone());
                                    last_total_iterations = r.total_iterations;
                                    let voice_json = |v: &VoiceSettings| {
                                        serde_json::json!({
                                            "engine": v.engine,
                                            "voice": v.voice,
                                            "language": v.language,
                                            "label": v.label,
                                        })
                                    };
                                    last_voice_json = r.voice.as_ref().map(&voice_json);
                                    if !r.segments.is_empty() {
                                        last_segments_json = Some(serde_json::Value::Array(
                                            r.segments
                                                .iter()
                                                .map(|s| {
                                                    serde_json::json!({
                                                        "text": s.text,
                                                        "voice": s.voice.as_ref().map(&voice_json),
                                                    })
                                                })
                                                .collect(),
                                        ));
                                    }
                                    if !r.files.is_empty() {
                                        last_files_json = Some(serde_json::Value::Array(
                                            r.files
                                                .iter()
                                                .map(|f| {
                                                    serde_json::json!({
                                                        "id": f.id,
                                                        "name": f.name,
                                                        "sizeBytes": f.size_bytes,
                                                    })
                                                })
                                                .collect(),
                                        ));
                                    }
                                    break;
                                }
                            }
                            core_message::Content::Error(_) => {
                                break;
                            }
                            _ => {}
                        }
                    }
                }
                Ok(Ok(None)) => {
                    // Server closed the stream without an explicit terminator.
                    info!("[GrpcClient] stream ended by server");
                    break;
                }
                Ok(Err(e)) => {
                    return Err(format!("Stream error: {}", e));
                }
                Err(_) => {
                    error!("[GrpcClient] stream timeout after 300s");
                    let evt = AgentEvent {
                        event_type: "error".to_string(),
                        data: serde_json::json!({
                            "message": "Neo Core não respondeu em tempo hábil (timeout 300s)"
                        }),
                        timestamp: chrono::Utc::now().to_rfc3339(),
                    };
                    let _ = app_handle.emit("agent-event", &evt);
                    // Drop tx to close client side before bailing.
                    drop(tx);
                    return Err("timeout".to_string());
                }
            }
        }

        // Closing our send half tells the server we're done with this task.
        drop(tx);

        // Emit a `complete` event so the frontend renders the final result.
        // Mirrors what the Electron delegator does when it sees is_final.
        if got_final {
            let duration_ms = task_start.elapsed().as_millis() as u64;
            let complete_evt = AgentEvent {
                event_type: "complete".to_string(),
                data: serde_json::json!({
                    "success": true,
                    "message": last_response_message.unwrap_or_default(),
                    "iterations": last_total_iterations,
                    "durationMs": duration_ms,
                    "sessionId": active_session_id.clone(),
                    "voice": last_voice_json,
                    "segments": last_segments_json,
                    "files": last_files_json,
                }),
                timestamp: chrono::Utc::now().to_rfc3339(),
            };
            let _ = app_handle.emit("agent-event", &complete_evt);
        }

        Ok(())
    }

    /// Translate a CoreMessage protobuf into a frontend-shaped AgentEvent.
    /// Returns None for unknown content variants.
    fn core_message_to_event(&self, msg: &CoreMessage) -> Option<AgentEvent> {
        let timestamp = chrono::Utc::now().to_rfc3339();
        let content = msg.content.as_ref()?;

        let evt = match content {
            core_message::Content::Thinking(t) => {
                // A self-continuation step announces itself with CYCLE_PROGRESS_MARKER
                // (neo-core ai_service.py). It is real execution, not the model
                // musing, so the bubble reads "Executando" instead of "Pensando".
                // Marker-based rather than matching on the prose, which changes.
                let is_cycle = t.message.starts_with(CYCLE_PROGRESS_MARKER);
                let body = t
                    .message
                    .strip_prefix(CYCLE_PROGRESS_MARKER)
                    .unwrap_or(&t.message);
                AgentEvent {
                    event_type: "thinking".to_string(),
                    data: serde_json::json!({
                        "sessionId": msg.session_id,
                        "type": if is_cycle { "executing" } else { "processing" },
                        "content": body,
                        "iteration": t.iteration,
                        "cycle": is_cycle,
                    }),
                    timestamp,
                }
            }
            core_message::Content::ToolRequest(t) => AgentEvent {
                event_type: "tool_request".to_string(),
                data: serde_json::json!({
                    "sessionId": msg.session_id,
                    "id": t.tool_call_id,
                    "name": t.tool_name,
                    "arguments": serde_json::from_str::<serde_json::Value>(&t.arguments)
                        .unwrap_or(serde_json::Value::Null),
                    "requiresApproval": t.requires_approval,
                }),
                timestamp,
            },
            core_message::Content::CommandRequest(c) => AgentEvent {
                event_type: "command_request".to_string(),
                data: serde_json::json!({
                    "sessionId": msg.session_id,
                    "id": c.command_id,
                    "cmd": c.command,
                    "command": c.command,
                    "explanation": c.explanation,
                    "isBackground": c.is_background,
                    "status": "pending",
                }),
                timestamp,
            },
            core_message::Content::Response(r) => {
                // Arquivos gerados/catalogados neste turno (ex: um .zip via
                // file_save) — mesma forma {id, name, sizeBytes} que o neo-panel
                // já usa (AttachmentRef). Baixável via GET /api/v1/files/{id}/download
                // no neo-core, agnóstico de cliente.
                let files: Vec<serde_json::Value> = r
                    .files
                    .iter()
                    .map(|f| {
                        serde_json::json!({
                            "id": f.id,
                            "name": f.name,
                            "sizeBytes": f.size_bytes,
                        })
                    })
                    .collect();
                // Intermediate responses are surfaced as thinking so the user
                // sees streaming progress; the final one is converted into a
                // `complete` event by the caller after the loop ends.
                if r.is_final {
                    AgentEvent {
                        event_type: "response".to_string(),
                        data: serde_json::json!({
                            "sessionId": msg.session_id,
                            "message": r.message,
                            "isFinal": true,
                            "totalIterations": r.total_iterations,
                            "files": files,
                        }),
                        timestamp,
                    }
                } else {
                    // Non-final AgentResponse = a persistent user-facing reply that
                    // precedes a client actuation (e.g. the witty answer before a music
                    // play). Render it as its own chat message, not a transient thinking
                    // bubble; the real final ("🎵 …") arrives afterwards.
                    AgentEvent {
                        event_type: "assistant_message".to_string(),
                        data: serde_json::json!({
                            "sessionId": msg.session_id,
                            "content": r.message,
                            "files": files,
                        }),
                        timestamp,
                    }
                }
            }
            core_message::Content::Error(e) => AgentEvent {
                event_type: "error".to_string(),
                data: serde_json::json!({
                    "sessionId": msg.session_id,
                    "message": if !e.error.is_empty() { e.error.clone() } else { e.details.clone() },
                    "error": e.error,
                    "details": e.details,
                }),
                timestamp,
            },
            core_message::Content::Iteration(i) => AgentEvent {
                event_type: "iteration".to_string(),
                data: serde_json::json!({
                    "sessionId": msg.session_id,
                    "current": i.current,
                    "max": i.max,
                }),
                timestamp,
            },
            core_message::Content::PlanUpdate(p) => AgentEvent {
                event_type: "plan_update".to_string(),
                data: serde_json::json!({
                    "sessionId": msg.session_id,
                    "planId": p.plan_id,
                    "goal": p.goal,
                    "currentStep": p.current_step,
                    "status": p.status,
                    "parentPlanId": p.parent_plan_id,
                    "steps": p.steps.iter().map(|s| serde_json::json!({
                        "id": s.id,
                        "index": s.index,
                        "description": s.description,
                        "toolName": s.tool_name,
                        "status": s.status,
                        "resultSummary": s.result_summary,
                        "error": s.error,
                    })).collect::<Vec<_>>(),
                }),
                timestamp,
            },
        };

        Some(evt)
    }
}

// ── Media actuation helpers (neo-music-feature, Etapa 2) ────────────────────

/// Tools opened directly via the OS handler (default browser/app). `media_command`
/// is NOT here — it goes through the extension bridge (`run_media_command`).
fn is_media_tool(name: &str) -> bool {
    matches!(name, "open_url" | "launch_uri")
}

/// `request_user_file`: o cérebro pediu um arquivo do disco do usuário. mode="pick"
/// abre o seletor nativo (o usuário escolhe); mode="path" sobe o caminho citado.
/// Sobe pro File Store e devolve, no `result`, o JSON `{file_id,name,size_bytes,mime}`
/// que o core cataloga (sticky) e usa pra retomar o turno. Cancelar o seletor =
/// success:false com mensagem tratada pelo core. Ver plano "arquivos sob iniciativa
/// do cérebro" + o intercept correspondente em process_tool_result.
async fn run_request_user_file(
    app: &AppHandle,
    arguments: &str,
    session_id: &str,
    user_id: &str,
) -> (bool, String, String) {
    let args: serde_json::Value =
        serde_json::from_str(arguments).unwrap_or(serde_json::Value::Null);
    let mode = args.get("mode").and_then(|v| v.as_str()).unwrap_or("pick");

    let path: Option<String> = if mode == "path" {
        args.get("path")
            .and_then(|v| v.as_str())
            .filter(|s| !s.trim().is_empty())
            .map(|s| s.to_string())
    } else {
        match crate::commands::pick_attachment_files(app.clone()).await {
            Ok(paths) => paths.into_iter().next(),
            Err(e) => return (false, String::new(), format!("Falha ao abrir o seletor: {}", e)),
        }
    };

    let Some(path) = path else {
        // Picker cancelado, ou mode="path" sem caminho — o core responde amigável.
        return (false, String::new(), "usuário não escolheu arquivo".to_string());
    };

    match crate::commands::upload_path_to_store(&path, session_id, user_id).await {
        // O registro do catálogo (`{id,name,size_bytes,mime,...}`) vai cru no
        // result; o core (_resume_after_user_file) já lê `file_id` OU `id`.
        Ok(v) => (true, v.to_string(), String::new()),
        Err(e) => (false, String::new(), e),
    }
}

/// Dispatch a `media_command` (browser-tab play) to the extension bridge and map
/// the outcome to ``(success, result, error)`` for the ToolResult.
async fn run_media_command(bridge: &ExtBridge, arguments: &str) -> (bool, String, String) {
    let args: serde_json::Value =
        serde_json::from_str(arguments).unwrap_or(serde_json::Value::Null);
    let action = args.get("action").and_then(|v| v.as_str()).unwrap_or("play");
    let target = args.get("target").and_then(|v| v.as_str()).unwrap_or("youtube");
    let video_id = args.get("videoId").and_then(|v| v.as_str()).unwrap_or("");
    // A aba que o neo-core escolheu ao decidir o reuso. Sem ela a extensão
    // re-resolvia sozinha, pela primeira aba do YouTube que achasse — que nem
    // sempre é a aba sobre a qual a decisão foi tomada.
    let tab_id = args.get("tabId").and_then(|v| v.as_i64());
    // `novaAba`: o core não achou aba sem dono (o `pick_reuse_tab` se recusa a
    // tomar vídeo do usuário) e converteu o `open_url` em `media_command` para a
    // fila. Sem este sinal a extensão re-escolhia sozinha e pegava a primeira
    // aba do YouTube que achasse — justamente a que o core tinha recusado.
    let nova_aba = args
        .get("novaAba")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    match bridge
        .send_command(action, target, video_id, tab_id, nova_aba)
        .await
    {
        Ok(()) => (true, "comando enviado à extensão".to_string(), String::new()),
        Err(e) => {
            // Plano B da fila: o neo-core converte `open_url` em `media_command`
            // para que a EXTENSÃO dirija a aba (é ela que trava o autoplay). Se
            // a ponte estiver fora, ainda assim tocamos — só sem a fila andar
            // sozinha, o que é melhor do que silêncio.
            let url = args.get("url").and_then(|v| v.as_str()).unwrap_or("");
            if !url.is_empty() {
                warn!("[Media] ponte fora ({}) — abrindo {} pelo sistema", e, url);
                return match open_target(url) {
                    Ok(()) => (
                        true,
                        format!("abriu {} (sem a extensão: a fila não vai andar sozinha)", url),
                        String::new(),
                    ),
                    Err(e2) => (false, String::new(), e2),
                };
            }
            (false, String::new(), e)
        }
    }
}

/// Caminho de uma pasta do usuário, preferindo o que o SO resolve.
///
/// `dirs` consulta o XDG no Linux (pt-BR devolve `~/Documentos`, `~/Área de trabalho`)
/// e as Known Folders no Windows; o fallback usa `Path::join`, que aplica o separador
/// nativo (`\` no Windows) em vez do `/` que o `format!` antigo cravava.
fn user_dir(resolved: Option<std::path::PathBuf>, home: &str, fallback: &str) -> String {
    resolved
        .unwrap_or_else(|| std::path::Path::new(home).join(fallback))
        .to_string_lossy()
        .to_string()
}

/// Run a shell command from an `execute_command` tool request (already approved by
/// the user via the CommandRequest flow) or from a `run_command` plan step. Returns
/// ``(success, result, error)`` for the ToolResult sent back to neo-core. Blocking —
/// call inside `spawn_blocking`.
fn run_command_blocking(arguments: &str) -> (bool, String, String) {
    let args: serde_json::Value =
        serde_json::from_str(arguments).unwrap_or(serde_json::Value::Null);
    let command = args.get("command").and_then(|v| v.as_str()).unwrap_or("");
    if command.is_empty() {
        return (false, String::new(), "comando ausente".to_string());
    }
    // Os passos de plano carregam `working_dir`; o fluxo aprovado não manda nenhum.
    let working_dir = args.get("working_dir").and_then(|v| v.as_str());

    // Shell da plataforma (ver services::shell): era `/bin/bash` fixo, que no Windows
    // não existe e derrubava o comando antes de rodar.
    match crate::services::shell::run_in(command, working_dir) {
        Ok(output) => crate::services::shell::split_output(output),
        Err(e) => (false, String::new(), e),
    }
}

/// Open a URL or app URI with the OS default handler (non-blocking).
fn open_target(target: &str) -> Result<(), String> {
    let result = if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(target).spawn()
    } else if cfg!(target_os = "windows") {
        std::process::Command::new("cmd")
            .args(["/C", "start", "", target])
            .spawn()
    } else {
        std::process::Command::new("xdg-open").arg(target).spawn()
    };
    result.map(|_| ()).map_err(|e| e.to_string())
}

/// Liga/desliga a vigia da fila a partir do `observar` da atuação de música.
///
/// Sem `observar` a vigia é DESLIGADA de propósito: se o usuário pediu outra
/// música no meio da fila, o neo-core já pausou a inscrição do lado dele, e
/// continuar observando aqui só produziria evento que ninguém quer.
fn apply_watch_directive(bridge: &ExtBridge, device_id: &str, arguments: &str) {
    let args: serde_json::Value =
        serde_json::from_str(arguments).unwrap_or(serde_json::Value::Null);
    let observar = match args.get("observar") {
        Some(v) if v.is_object() => v,
        _ => {
            bridge.disarm_watch();
            return;
        }
    };
    let subscription_id = observar
        .get("inscricao_id")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let video_id = observar
        .get("video_id")
        .and_then(|v| v.as_str())
        .or_else(|| args.get("videoId").and_then(|v| v.as_str()))
        .unwrap_or("");
    if subscription_id.is_empty() || video_id.is_empty() {
        bridge.disarm_watch();
        return;
    }
    let tab_id = observar
        .get("tab_id")
        .and_then(|v| v.as_i64())
        .or_else(|| args.get("tabId").and_then(|v| v.as_i64()));
    let codigo = observar
        .get("codigo")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    bridge.arm_watch(subscription_id, device_id, tab_id, video_id, codigo);
}

/// Execute a media tool request; returns ``(success, result, error)`` for the
/// ToolResult sent back to neo-core.
fn run_media_tool(tool_name: &str, arguments: &str) -> (bool, String, String) {
    let args: serde_json::Value =
        serde_json::from_str(arguments).unwrap_or(serde_json::Value::Null);

    let open_with = |key: &str| -> (bool, String, String) {
        let target = args.get(key).and_then(|v| v.as_str()).unwrap_or("");
        if target.is_empty() {
            return (false, String::new(), format!("{} ausente", key));
        }
        match open_target(target) {
            Ok(()) => (true, format!("abriu {}", target), String::new()),
            Err(e) => (false, String::new(), e),
        }
    };

    match tool_name {
        "open_url" => open_with("url"),
        "launch_uri" => open_with("uri"),
        other => (false, String::new(), format!("tool de mídia desconhecida: {}", other)),
    }
}
