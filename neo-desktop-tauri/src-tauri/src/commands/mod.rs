use crate::grpc_gen::neo_desktop::{desktop_message, CommandApproval, DesktopMessage};
use crate::services::ai_agent::{GrpcClient, VoiceInput};
use crate::AppState;
use log::{error, info, warn};
use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager, State, Window};

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Message {
    #[serde(flatten)]
    pub data: serde_json::Value,
}

#[derive(Debug, Deserialize)]
struct ConversationHistoryResponse {
    messages: Vec<Message>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct AgentEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub data: serde_json::Value,
    pub timestamp: String,
}

// Store pending permission requests
lazy_static::lazy_static! {
    static ref PENDING_PERMISSIONS: Mutex<std::collections::HashMap<String, tokio::sync::oneshot::Sender<bool>>> =
        Mutex::new(std::collections::HashMap::new());
}

// ============ AGENT EXECUTION ============

#[tauri::command]
#[allow(clippy::too_many_arguments)]
pub async fn execute_with_loop(
    app: AppHandle,
    _state: State<'_, AppState>,
    message: String,
    model: Option<String>,
    mode: Option<String>,
    provider: Option<String>,
    attachments: Option<Vec<String>>,
    // Origem da ENTRADA (≠ `mode`, que é o estilo da SAÍDA): "voice" quando o
    // texto veio do STT do neo-voice. Ver docs/voice-input-normalization-plan.md.
    input_source: Option<String>,
    asr_raw: Option<String>,
    asr_engine: Option<String>,
    // Quantos caracteres desta mensagem vieram do CLIPBOARD. Acima do limiar o
    // neo-core marca o turno como contaminado (fonte `colagem`) e revelar passa a
    // pedir o card de liberação — colado era a única entrada de conteúdo de
    // terceiro que o portão não enxergava. Ver
    // neo-core/docs/release-request-plan.md §21.
    pasted_chars: Option<u32>,
) -> Result<(), String> {
    info!(
        "[Neo] execute_with_loop: {} (model={:?}, mode={:?}, anexos={}, origem={:?})",
        message,
        model,
        mode,
        attachments.as_ref().map_or(0, |a| a.len()),
        input_source
    );
    let voice = VoiceInput::from_parts(input_source, asr_raw, asr_engine);
    run_agent_message(app, message, model, mode, provider, attachments, voice, pasted_chars);
    Ok(())
}

/// Runs an agent turn against neo-core and streams `agent-event`s back to the
/// app. Factored out of `execute_with_loop` so non-command callers (e.g. the
/// native notch host's prompt field) can trigger the same full-agent pipeline.
///
/// Safe to call from any thread: it pulls everything it needs out of `AppState`
/// synchronously and then spawns the work on Tauri's async runtime.
pub fn run_agent_message(
    app: AppHandle,
    message: String,
    model: Option<String>,
    mode: Option<String>,
    provider: Option<String>,
    attachments: Option<Vec<String>>,
    voice: Option<VoiceInput>,
    pasted_chars: Option<u32>,
) {
    let state = app.state::<AppState>();
    let device_id = state.device_id.clone();
    let hardware_id = state.hardware_id.clone();
    let user_id = state.user_id.clone();
    let session_id = state.session_id.lock().unwrap().clone();
    let neo_core_url =
        std::env::var("NEO_CORE_URL").unwrap_or_else(|_| "127.0.0.1:50051".to_string());

    let app_handle = app.clone();
    let cancel_notify = state.cancel_notify.clone();
    let stream_tx_slot = state.stream_tx.clone();
    let ext_bridge = state.ext_bridge.clone();
    drop(state);

    tauri::async_runtime::spawn(async move {
        let mut client = GrpcClient::new(&device_id, &hardware_id, &user_id, &neo_core_url);
        client.set_session_id(&session_id);

        let result = client
            .execute_task(
                app_handle.clone(),
                message,
                model.as_deref(),
                provider.as_deref(),
                mode.as_deref(),
                attachments,
                voice,
                pasted_chars,
                cancel_notify,
                stream_tx_slot,
                ext_bridge,
            )
            .await;

        match result {
            Ok(()) => {
                info!("[Neo] run_agent_message completed");
            }
            Err(e) => {
                error!("[Neo] run_agent_message error: {}", e);
                let error_event = AgentEvent {
                    event_type: "error".to_string(),
                    data: serde_json::json!({ "message": e }),
                    timestamp: chrono::Utc::now().to_rfc3339(),
                };
                let _ = app_handle.emit("agent-event", &error_event);
            }
        }
    });
}

#[tauri::command]
pub fn stop_execution(state: State<'_, AppState>) -> Result<(), String> {
    info!("[Neo] stop_execution requested — cancelling in-flight run");
    // O Stop só derrubava o stream: o comando (ou o opencode) seguia rodando na
    // máquina, e um card de aprovação ficava esperando 15 min por nada.
    let killed = crate::services::shell::kill_running();
    if killed > 0 {
        info!("[Neo] stop_execution: {} processo(s) encerrado(s)", killed);
    }
    crate::services::file_ops::cancel_pending();
    crate::services::command_policy::cancel_pending();
    // Wake the execute_task read loop, which drops the gRPC stream and thereby
    // cancels the server-side RPC (grpc.aio cancels the handler on client abort).
    state.cancel_notify.notify_waiters();
    Ok(())
}

/// Approve or reject a pending command (the agent's `command_request`). Pushes a
/// CommandApproval into the in-flight stream so neo-core can continue: on approve it
/// replies with an `execute_command` tool request that this client runs inline; on
/// reject it closes the turn. Without this the run hangs until the stream's hard
/// ceiling (the idle timeout is paused while the core waits on an approval).
#[tauri::command]
pub async fn respond_to_command(
    state: State<'_, AppState>,
    command_id: String,
    approved: bool,
) -> Result<(), String> {
    info!(
        "[Neo] respond_to_command: id={} approved={}",
        command_id, approved
    );

    // Card desenhado pelo próprio cliente (política de comando num passo de plano):
    // o neo-core está esperando um ToolResult, não um CommandApproval — mandar um
    // approval pra ele seria responder a um pedido que ninguém fez.
    if command_id.starts_with(crate::services::command_policy::LOCAL_PREFIX) {
        return crate::services::command_policy::respond(&command_id, approved);
    }

    let sender = {
        state
            .stream_tx
            .lock()
            .map_err(|_| "stream sender lock poisoned".to_string())?
            .clone()
    };
    let Some(sender) = sender else {
        return Err("nenhuma execução ativa para aprovar".to_string());
    };

    let session_id = state.session_id.lock().unwrap().clone();
    let user_id = state.user_id.clone();

    let msg = DesktopMessage {
        session_id,
        user_id,
        content: Some(desktop_message::Content::CommandApproval(CommandApproval {
            command_id,
            approved,
        })),
    };

    sender
        .send(msg)
        .await
        .map_err(|e| format!("falha ao enviar aprovação do comando: {}", e))
}

/// Aprovar/Rejeitar uma alteração (ou leitura) de arquivo que caiu fora das pastas
/// liberadas — o card do chat chama isto (services/file_ops.rs).
#[tauri::command]
pub fn respond_file_change(change_id: String, approved: bool) -> Result<(), String> {
    info!("[Neo] respond_file_change: id={} approved={}", change_id, approved);
    crate::services::file_ops::respond(&change_id, approved)
}

/// Desfazer uma alteração de arquivo (botão do card). Recusa se o arquivo mudou
/// depois — nunca desfaz por cima de edição do usuário.
#[tauri::command]
pub async fn undo_file_change(app: AppHandle, change_id: String) -> Result<(), String> {
    let meta = tokio::task::spawn_blocking(move || {
        crate::services::file_ops::undo_change(&change_id)
    })
    .await
    .map_err(|e| format!("falha ao desfazer: {}", e))??;
    crate::services::file_ops::emit_undone(&app, &meta);
    Ok(())
}

#[tauri::command]
pub fn reset_agent(state: State<'_, AppState>, new_session_id: String) -> Result<String, String> {
    info!("[Neo] reset_agent: new session {}", new_session_id);
    let mut session = state.session_id.lock().unwrap();
    *session = new_session_id.clone();
    // Único ponto por onde passa toda troca de sessão ("Novo chat", seletor de
    // sessões antigas e o `session_changed` vindo do core) — é aqui que o disco
    // acompanha, senão o próximo boot retomaria uma sessão já abandonada.
    crate::persist_session_id(&new_session_id);
    Ok(new_session_id)
}

// ============ COMMAND EXECUTION ============

#[derive(Debug, Serialize, Deserialize)]
pub struct CommandResult {
    pub success: bool,
    pub output: Option<String>,
    pub error: Option<String>,
}

#[tauri::command]
pub async fn execute_command(command: String) -> Result<CommandResult, String> {
    info!("[Neo] execute_command: {}", command);

    // Mesmo shell por plataforma do executor pós-aprovação (services::shell).
    let output = crate::services::shell::run(&command)
        .map_err(|e| format!("Failed to execute: {}", e))?;
    let (success, text, error) = crate::services::shell::split_output(output);

    if success {
        Ok(CommandResult {
            success: true,
            output: Some(text),
            error: None,
        })
    } else {
        Ok(CommandResult {
            success: false,
            output: None,
            error: Some(error),
        })
    }
}

// ============ HISTORY ============

fn get_history_path() -> std::path::PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("neo-desktop")
        .join("conversation-history.json")
}

fn neo_core_http_url() -> String {
    std::env::var("NEO_CORE_HTTP_URL")
        .or_else(|_| std::env::var("NEO_CORE_API_URL"))
        .ok()
        .or_else(|| {
            std::env::var("NEO_CORE_URL")
                .ok()
                .filter(|v| v.starts_with("http://") || v.starts_with("https://"))
        })
        .unwrap_or_else(|| "http://127.0.0.1:8000".to_string())
        .trim_end_matches('/')
        .to_string()
}

// ============ ANEXOS (File Store) ============
//
// Anexar um arquivo ao chat: picker nativo → upload multipart pro File Store do
// neo-core → o id volta pro front, que o manda em `metadata["attachments"]` na
// mensagem. O CONTEÚDO nunca passa pelo chat; o Neo lê por fatias com as tools
// file_* (neo-core/docs/file-store-plan.md §5).

/// Espelha o limite do servidor (NEO_FILES_MAX_FILE_MB) — o core recusa com 413
/// de qualquer jeito; isto só evita ler um arquivo gigante do disco à toa.
const MAX_ATTACHMENT_BYTES: u64 = 500 * 1024 * 1024;

/// Abre o picker nativo. Lista vazia = o usuário cancelou.
#[tauri::command]
pub async fn pick_attachment_files(app: AppHandle) -> Result<Vec<String>, String> {
    use tauri_plugin_dialog::DialogExt;

    // blocking_pick_files trava se rodar na thread principal (ela é quem serve o
    // diálogo) — daí o spawn_blocking.
    tauri::async_runtime::spawn_blocking(move || {
        let picked = app.dialog().file().blocking_pick_files();
        Ok(picked
            .unwrap_or_default()
            .into_iter()
            .filter_map(|p| p.into_path().ok())
            .map(|p| p.to_string_lossy().to_string())
            .collect())
    })
    .await
    .map_err(|e| format!("Falha ao abrir o seletor de arquivos: {}", e))?
}

/// Sobe um arquivo local pro File Store e devolve o registro do catálogo
/// (`{id, name, size_bytes, mime, expires_at, ...}`). O `id` é o que vai no
/// metadata da mensagem.
#[tauri::command]
pub async fn upload_attachment(
    state: State<'_, AppState>,
    path: String,
) -> Result<serde_json::Value, String> {
    let session_id = state.session_id.lock().unwrap().clone();
    let user_id = state.user_id.clone();
    upload_path_to_store(&path, &session_id, &user_id).await
}

/// Sobe um caminho local pro File Store (`POST /api/v1/files`) e devolve o
/// registro do catálogo. Corpo compartilhado por `upload_attachment` (clipe) e
/// pelo intercept de `request_user_file` em ai_agent.rs (o cérebro puxando um
/// arquivo do usuário) — o único que sabe resolver a URL do core é o Rust.
pub async fn upload_path_to_store(
    path: &str,
    session_id: &str,
    user_id: &str,
) -> Result<serde_json::Value, String> {
    let meta = tokio::fs::metadata(path)
        .await
        .map_err(|e| format!("Não consegui ler o arquivo: {}", e))?;
    if !meta.is_file() {
        return Err("Só dá para anexar arquivo (não pasta).".to_string());
    }
    if meta.len() > MAX_ATTACHMENT_BYTES {
        return Err(format!(
            "Arquivo muito grande ({:.1} MB). O limite é {} MB.",
            meta.len() as f64 / (1024.0 * 1024.0),
            MAX_ATTACHMENT_BYTES / (1024 * 1024)
        ));
    }

    let file_name = std::path::Path::new(path)
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "arquivo".to_string());
    let mime = mime_guess::from_path(path)
        .first_or_octet_stream()
        .to_string();
    // Streaming do disco: com até 500 MB, ler tudo pra RAM antes de enviar pesa.
    let file = tokio::fs::File::open(path)
        .await
        .map_err(|e| format!("Não consegui ler o arquivo: {}", e))?;

    info!(
        "[Neo] upload_path_to_store: {} ({} bytes, {})",
        file_name,
        meta.len(),
        mime
    );

    let part = reqwest::multipart::Part::stream_with_length(reqwest::Body::from(file), meta.len())
        .file_name(file_name.clone())
        .mime_str(&mime)
        .map_err(|e| format!("Tipo de arquivo inválido: {}", e))?;
    let form = reqwest::multipart::Form::new()
        .part("file", part)
        .text("retention", "temp")
        .text("origin", "upload-desktop")
        .text("session_id", session_id.to_string())
        .text("user_id", user_id.to_string());

    // Timeout proporcional ao tamanho: 120s fixos estouravam antes de um arquivo
    // grande terminar de subir pelo túnel. +1s por MB (500 MB → ~10 min).
    let timeout_secs = 120 + meta.len() / (1024 * 1024);
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(timeout_secs))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    let resp = crate::with_device_token(
        client.post(format!("{}/api/v1/files", neo_core_http_url())),
    )
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("Upload falhou: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        // O core devolve {"detail": {"error": ..., "message": ...}} nos casos
        // tratados (arquivo grande demais, quota); mostra a mensagem dele.
        let friendly = serde_json::from_str::<serde_json::Value>(&body)
            .ok()
            .and_then(|v| {
                v.get("detail")
                    .and_then(|d| d.get("message"))
                    .and_then(|m| m.as_str())
                    .map(|s| s.to_string())
            })
            .unwrap_or_else(|| format!("{} {}", status, body));
        return Err(friendly);
    }

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Resposta inválida do neo-core: {}", e))
}

/// Busca os bytes de um arquivo já catalogado (file_save) no neo-core.
/// Compartilhado por `download_generated_file` (clique manual, com diálogo)
/// e `save_generated_file_to_desktop` (disparado pelo próprio agente, sem
/// diálogo) — a URL certa (neo_core_http_url) só o Rust sabe resolver; é por
/// isso que nenhum comando de shell escrito pelo modelo alcança esse arquivo
/// (sessão ef4f723c: o modelo chutou 3 hosts/portas errados tentando `curl`).
async fn fetch_generated_file_bytes(file_id: &str) -> Result<Vec<u8>, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(120))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    let resp = crate::with_device_token(client.get(format!(
        "{}/api/v1/files/{}/download",
        neo_core_http_url(),
        file_id
    )))
        .send()
        .await
        .map_err(|e| format!("Download falhou: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        return Err(format!("Download falhou: {}", status));
    }
    resp.bytes()
        .await
        .map(|b| b.to_vec())
        .map_err(|e| format!("Falha ao ler o arquivo: {}", e))
}

/// Só o nome-base do caminho — nunca escreve fora de onde mandamos, mesmo que
/// `file_name` venha com `../` (defesa em profundidade: o valor vem do
/// backend, mas essa função escreve sem diálogo/confirmação do usuário).
fn sanitize_file_name(name: &str) -> String {
    std::path::Path::new(name)
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .filter(|n| !n.is_empty())
        .unwrap_or_else(|| "arquivo".to_string())
}

/// Baixa um arquivo que o Neo gerou/catalogou (ex: um .zip via a tool
/// file_save) do catálogo do neo-core e salva localmente via diálogo nativo
/// "Salvar como". Espelha o download de um clique do neo-panel (`GET
/// /api/v1/files/{id}/download`) — o webview do Tauri não tem a mesma UX de
/// download automático de um browser real, então quem baixa é o Rust.
#[tauri::command]
pub async fn download_generated_file(
    app: AppHandle,
    file_id: String,
    file_name: String,
) -> Result<String, String> {
    let bytes = fetch_generated_file_bytes(&file_id).await?;

    use tauri_plugin_dialog::DialogExt;
    // blocking_save_file trava se rodar na thread principal — spawn_blocking,
    // mesmo padrão de pick_attachment_files.
    let picked = tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .set_file_name(&file_name)
            .blocking_save_file()
    })
    .await
    .map_err(|e| format!("Falha ao abrir o diálogo de salvar: {}", e))?;

    let Some(picked_path) = picked else {
        // Usuário cancelou o diálogo — não é um erro real, o front trata esse
        // texto como cancelamento silencioso.
        return Err("cancelled".to_string());
    };
    let path = picked_path
        .into_path()
        .map_err(|e| format!("Caminho inválido: {}", e))?;
    tokio::fs::write(&path, &bytes)
        .await
        .map_err(|e| format!("Falha ao salvar o arquivo: {}", e))?;

    Ok(path.to_string_lossy().to_string())
}

/// Resolve `destination` pro diretório real. Atalhos conhecidos (desktop,
/// downloads, documents, home) ou um caminho literal (absoluto ou com `~/`)
/// quando o usuário citou uma pasta específica — ver `save_generated_file` no
/// catálogo de tools (neo-core/modules/desktop/tools.py).
fn resolve_destination_dir(destination: &str) -> Result<std::path::PathBuf, String> {
    let trimmed = destination.trim();
    match trimmed.to_lowercase().as_str() {
        "" | "desktop" | "área de trabalho" | "area de trabalho" => dirs::desktop_dir()
            .ok_or_else(|| "Não consegui localizar a Área de Trabalho/Desktop.".to_string()),
        "downloads" | "download" => dirs::download_dir()
            .ok_or_else(|| "Não consegui localizar a pasta Downloads.".to_string()),
        "documents" | "documentos" => dirs::document_dir()
            .ok_or_else(|| "Não consegui localizar a pasta Documentos.".to_string()),
        "home" | "casa" => {
            dirs::home_dir().ok_or_else(|| "Não consegui localizar a pasta pessoal (home).".to_string())
        }
        _ => {
            // Caminho literal — expande "~/" pro home; absoluto vai direto.
            if let Some(rest) = trimmed.strip_prefix("~/") {
                dirs::home_dir()
                    .map(|h| h.join(rest))
                    .ok_or_else(|| "Não consegui resolver \"~\" (home).".to_string())
            } else {
                Ok(std::path::PathBuf::from(trimmed))
            }
        }
    }
}

/// Baixa um arquivo do catálogo e escreve DIRETO no destino pedido, sem
/// diálogo — chamado pelo próprio agente (tool_name="save_generated_file",
/// despachado em ai_agent.rs) quando o pedido é "bota esse arquivo no meu
/// [Desktop/Downloads/pasta X]". Diferente de `download_generated_file`
/// (clique manual do usuário + "Salvar como"), esta roda sem interação:
/// precisa de destino determinístico, não um diálogo que ninguém vai
/// responder.
/// Formata um `SystemTime` como "YYYY-MM-DD HH:MM" no fuso local (pro cérebro
/// citar a data do arquivo existente ao perguntar sobre sobrescrita).
fn system_time_iso(t: Option<std::time::SystemTime>) -> Option<String> {
    t.map(|st| {
        let dt: chrono::DateTime<chrono::Local> = st.into();
        dt.format("%Y-%m-%d %H:%M").to_string()
    })
}

/// `destination` pode ser a PASTA ou o CAMINHO COMPLETO do arquivo. Tratado
/// sempre como pasta, "~/Desktop/plano/analise.md" fazia `create_dir_all` bater
/// no arquivo existente e morrer com "File exists (os error 17)" (sessão
/// 30b1e484). É caminho de arquivo quando já existe um arquivo ali, ou quando o
/// último segmento tem a mesma extensão do `file_name` e não é uma pasta — uma
/// pasta "site.com" não vira arquivo por ter ponto no nome.
async fn split_file_destination(
    target: std::path::PathBuf,
    file_name: &str,
) -> (std::path::PathBuf, String) {
    let meta = tokio::fs::metadata(&target).await.ok();
    let is_file = meta.as_ref().map(|m| m.is_file()).unwrap_or(false);
    let is_dir = meta.as_ref().map(|m| m.is_dir()).unwrap_or(false);
    let ext_of = |p: &std::path::Path| p.extension().map(|e| e.to_string_lossy().to_lowercase());
    let same_ext = !is_dir
        && ext_of(&target).is_some()
        && ext_of(&target) == ext_of(std::path::Path::new(file_name));
    if is_file || same_ext {
        if let (Some(parent), Some(name)) = (target.parent(), target.file_name()) {
            return (parent.to_path_buf(), name.to_string_lossy().to_string());
        }
    }
    (target, file_name.to_string())
}

/// Baixa um arquivo do catálogo e escreve no destino pedido, sem diálogo.
/// **Nunca sobrescreve às cegas:** se já existir um arquivo no destino e
/// `overwrite=false`, NÃO baixa nem grava — devolve `status="conflict"` com os
/// dados do arquivo existente (nome, tamanho, datas) pro cérebro decidir ou
/// perguntar. Com `overwrite=true` (ou destino livre) grava e devolve
/// `status="saved"`. Chamado pelo agente (tool_name="save_generated_file",
/// despachado em ai_agent.rs).
pub async fn save_generated_file_to_path(
    file_id: &str,
    file_name: &str,
    destination: &str,
    overwrite: bool,
) -> Result<serde_json::Value, String> {
    let target = resolve_destination_dir(destination)?;
    let (dir, final_name) = split_file_destination(target, file_name).await;
    tokio::fs::create_dir_all(&dir)
        .await
        .map_err(|e| format!("Falha ao acessar a pasta de destino ({}): {}", dir.display(), e))?;

    let path = dir.join(sanitize_file_name(&final_name));

    // Colisão: checa ANTES de baixar/gravar (não paga o download à toa).
    if !overwrite {
        if let Ok(meta) = tokio::fs::metadata(&path).await {
            if meta.is_file() {
                let existing = serde_json::json!({
                    "name": path
                        .file_name()
                        .map(|n| n.to_string_lossy().to_string()),
                    "size_bytes": meta.len(),
                    "created_at": system_time_iso(meta.created().ok()),
                    "modified_at": system_time_iso(meta.modified().ok()),
                });
                return Ok(serde_json::json!({
                    "status": "conflict",
                    "path": path.to_string_lossy().to_string(),
                    "existing": existing,
                }));
            }
        }
    }

    let bytes = fetch_generated_file_bytes(file_id).await?;
    tokio::fs::write(&path, &bytes)
        .await
        .map_err(|e| format!("Falha ao salvar o arquivo: {}", e))?;

    Ok(serde_json::json!({
        "status": "saved",
        "path": path.to_string_lossy().to_string(),
    }))
}


// ============ IMAGENS INLINE NO CHAT ============
//
// O núcleo cognitivo pede a exibição de uma imagem escrevendo `[[IMAGE:origem]]`
// na resposta (ver _INLINE_IMAGE_INSTRUCTION no neo-core); o front extrai o
// marcador (src/lib/inlineImages.ts) e chama `load_chat_image` para cada um. Os
// BYTES nunca passam pelo prompt nem pelo LLM: saem do disco do usuário, do
// File Store ou de uma URL http(s) direto pro webview, como data URL — que é o
// que a CSP do app já permite em `img-src` ('self' data:), sem abrir o
// protocolo de assets.
//
// A origem remota (foto de anúncio numa busca de produto) é baixada AQUI, pelo
// Rust: o webview nunca faz GET em terceiro, então a CSP segue fechada. O
// `source` vem do modelo, então URL é entrada não confiável — ver
// `is_private_image_host`.
//
// Nasceu do turno em que o pedido "carrega a imagem aqui no chat pra eu ver"
// virou `base64 -i foto.jpeg`: o blob entrou no contexto e o usuário não viu
// imagem nenhuma.

/// Teto do que vale embutir como data URL numa bolha de chat. Foto de celular
/// (~5 MB) passa folgado; acima disso a string base64 (~1,33x) pesa mais que o
/// valor de mostrar, e o erro explica o motivo em vez de travar a UI.
const MAX_INLINE_IMAGE_BYTES: u64 = 24 * 1024 * 1024;

/// Extensões que o webview realmente desenha. HEIC/HEIF (padrão do iPhone) fica
/// FORA de propósito: o WebKit do Tauri não decodifica, e sem esta checagem o
/// usuário veria um quadro quebrado sem explicação.
const RENDERABLE_IMAGE_EXTS: &[&str] = &[
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "ico", "svg", "avif",
];

/// camelCase de propósito: as chaves são lidas direto pelo webview
/// (`ChatImage` em src/lib/tauriApi.ts).
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ChatImage {
    /// `data:<mime>;base64,…` — pronto pro `src` de um `<img>`.
    pub data_url: String,
    pub name: String,
    pub mime: String,
    pub size_bytes: u64,
    /// Caminho absoluto real (só para origem local) — usado no "revelar no Finder"
    /// e no tooltip. `None` quando a imagem veio do catálogo.
    pub path: Option<String>,
}

/// Remove os marcadores `[[IMAGE:…]]` de um texto que vai ser FALADO (notch/TTS)
/// ou mostrado onde não há renderização. Sem isto o TTS lê o caminho inteiro em
/// voz alta. O front tem o equivalente em src/lib/inlineImages.ts (que também
/// devolve as imagens); aqui só interessa o texto limpo.
pub fn strip_image_markers(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = find_image_marker(rest) {
        out.push_str(&rest[..start]);
        let after = &rest[start..];
        match after.find("]]") {
            Some(end) => rest = &after[end + 2..],
            None => {
                // Marcador truncado (resposta cortada): descarta o resto, que é
                // caminho de arquivo — nunca fala pela metade.
                rest = "";
            }
        }
    }
    out.push_str(rest);
    // Junta o que sobrou dos dois lados sem deixar linha vazia solta no meio.
    out.lines()
        .map(|l| l.trim_end())
        .filter(|l| !l.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string()
}

/// Índice do próximo `[[IMAGE:` (case-insensitive, tolerando espaços como
/// `[[ image :`), que é o mesmo formato aceito pelo parser do front.
///
/// Varre bytes ASCII do texto ORIGINAL de propósito: `to_lowercase()` pode mudar
/// o comprimento em bytes de alguns caracteres Unicode, e o índice devolvido
/// serve para fatiar o original. `[` nunca aparece dentro de um char multibyte,
/// então o corte cai sempre num limite válido.
fn find_image_marker(haystack: &str) -> Option<usize> {
    let bytes = haystack.as_bytes();
    let skip_ws = |mut idx: usize| {
        while idx < bytes.len() && bytes[idx].is_ascii_whitespace() {
            idx += 1;
        }
        idx
    };
    for i in 0..bytes.len().saturating_sub(2) {
        if bytes[i] != b'[' || bytes[i + 1] != b'[' {
            continue;
        }
        let word = skip_ws(i + 2);
        if bytes.len() < word + 5 || !bytes[word..word + 5].eq_ignore_ascii_case(b"image") {
            continue;
        }
        let colon = skip_ws(word + 5);
        if colon < bytes.len() && bytes[colon] == b':' {
            return Some(i);
        }
    }
    None
}

/// Base64 padrão (RFC 4648) sem quebra de linha. São ~20 linhas em vez de uma
/// dependência nova só para isto.
fn base64_encode(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(ALPHABET[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

/// Expande `~/` e resolve o caminho pedido pelo cérebro. Não confina a leitura a
/// um diretório: o usuário pede imagens de qualquer lugar do disco DELE e é o
/// mesmo alcance que `run_command` já tem neste app — o valor aqui é validar que
/// o caminho existe e é imagem, para o modelo não conseguir "mostrar" um
/// caminho inventado (falha visível, em vez de quadro quebrado silencioso).
fn resolve_local_image_path(raw: &str) -> Result<std::path::PathBuf, String> {
    let trimmed = raw.trim().trim_matches('"').trim_matches('\'');
    if trimmed.is_empty() {
        return Err("Caminho vazio.".to_string());
    }
    let expanded = if trimmed == "~" {
        dirs::home_dir().ok_or_else(|| "Não consegui resolver \"~\".".to_string())?
    } else if let Some(rest) = trimmed.strip_prefix("~/") {
        dirs::home_dir()
            .ok_or_else(|| "Não consegui resolver \"~\".".to_string())?
            .join(rest)
    } else {
        std::path::PathBuf::from(trimmed)
    };

    let canonical = std::fs::canonicalize(&expanded)
        .map_err(|_| format!("Não achei o arquivo: {}", expanded.display()))?;
    let meta = std::fs::metadata(&canonical)
        .map_err(|e| format!("Não consegui ler o arquivo: {}", e))?;
    if !meta.is_file() {
        return Err(format!("{} não é um arquivo.", canonical.display()));
    }
    Ok(canonical)
}

/// Host de rede privada/local — recusado antes de qualquer GET remoto.
///
/// O `source` do marcador é escrito pelo MODELO, então tratar como entrada não
/// confiável: sem isto, um `[[IMAGE:http://192.168.4.x/…]]` faria o app do
/// usuário varrer a LAN dele. Mesma política do guard de SSRF do `web_fetch` no
/// neo-core ("internet sim, LAN não").
fn is_private_image_host(host: &str) -> bool {
    let h = host.trim().trim_matches(|c| c == '[' || c == ']').to_lowercase();
    if h == "localhost" || h.ends_with(".local") || h.ends_with(".internal") {
        return true;
    }
    if let Ok(ip) = h.parse::<std::net::IpAddr>() {
        return !ip_is_global(&ip);
    }
    false
}

/// `IpAddr::is_global` ainda é unstable no stable — a checagem equivalente para
/// o que importa aqui (loopback, privado, link-local, multicast, reservado).
fn ip_is_global(ip: &std::net::IpAddr) -> bool {
    match ip {
        std::net::IpAddr::V4(v4) => {
            !(v4.is_private()
                || v4.is_loopback()
                || v4.is_link_local()
                || v4.is_broadcast()
                || v4.is_documentation()
                || v4.is_unspecified()
                || v4.octets()[0] == 0
                || (v4.octets()[0] & 0xf0) == 0xf0)
        }
        std::net::IpAddr::V6(v6) => {
            !(v6.is_loopback()
                || v6.is_unspecified()
                // fc00::/7 (unique local) e fe80::/10 (link local)
                || (v6.segments()[0] & 0xfe00) == 0xfc00
                || (v6.segments()[0] & 0xffc0) == 0xfe80)
        }
    }
}

/// Baixa uma imagem de uma URL http(s) e devolve os bytes.
///
/// Quem faz o GET é o RUST, não o webview — assim a foto de um anúncio aparece
/// na bolha sem afrouxar a CSP (`img-src 'self' data:` continua valendo) e sem
/// o webview conversar com terceiro nenhum.
async fn fetch_remote_image_bytes(url: &str) -> Result<Vec<u8>, String> {
    let parsed = reqwest::Url::parse(url).map_err(|_| format!("URL inválida: {}", url))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(format!("Só http(s) no marcador de imagem (veio {}).", parsed.scheme()));
    }
    match parsed.host_str() {
        Some(host) if !is_private_image_host(host) => {}
        Some(host) => return Err(format!("Host de rede local recusado: {}", host)),
        None => return Err("URL sem host.".to_string()),
    }

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(20))
        // Loja costuma responder 403 pra user-agent de robô; aqui é o app do
        // usuário buscando a imagem que ele pediu pra ver.
        .user_agent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
        .build()
        .map_err(|e| format!("Falha ao criar cliente HTTP: {}", e))?;

    let resp = client
        .get(parsed.clone())
        .send()
        .await
        .map_err(|e| format!("Não consegui baixar a imagem: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("A imagem respondeu {}.", resp.status()));
    }
    // Corta cedo pelo Content-Length quando o servidor informa, para não puxar
    // 100 MB só pra descobrir que não cabe.
    if let Some(len) = resp.content_length() {
        if len > MAX_INLINE_IMAGE_BYTES {
            return Err(format!(
                "Imagem muito grande para exibir ({:.1} MB; limite {} MB).",
                len as f64 / (1024.0 * 1024.0),
                MAX_INLINE_IMAGE_BYTES / (1024 * 1024)
            ));
        }
    }
    let bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("Falha ao ler a imagem: {}", e))?
        .to_vec();
    if bytes.len() as u64 > MAX_INLINE_IMAGE_BYTES {
        return Err(format!(
            "Imagem muito grande para exibir ({:.1} MB; limite {} MB).",
            bytes.len() as f64 / (1024.0 * 1024.0),
            MAX_INLINE_IMAGE_BYTES / (1024 * 1024)
        ));
    }
    Ok(bytes)
}

/// Confere se a extensão é de imagem desenhável pelo webview.
fn ensure_renderable_image(path: &std::path::Path) -> Result<(), String> {
    let ext = path
        .extension()
        .map(|e| e.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    if RENDERABLE_IMAGE_EXTS.contains(&ext.as_str()) {
        return Ok(());
    }
    if matches!(ext.as_str(), "heic" | "heif") {
        return Err(format!(
            "{} está em HEIC, que o chat não desenha. Converta antes (ex.: `sips -s format jpeg`).",
            path.file_name().unwrap_or_default().to_string_lossy()
        ));
    }
    Err(format!(
        "{} não parece ser imagem exibível (extensão .{}).",
        path.file_name().unwrap_or_default().to_string_lossy(),
        if ext.is_empty() { "?".into() } else { ext }
    ))
}

/// Carrega uma imagem para exibir na bolha do chat.
///
/// `source` vem do marcador `[[IMAGE:…]]`: um caminho do disco do usuário (com
/// `~/` ou absoluto) ou `file:<file_id>` para um arquivo do catálogo do neo-core
/// (ex.: um gráfico que o próprio Neo gerou no sandbox).
#[tauri::command]
pub async fn load_chat_image(source: String) -> Result<ChatImage, String> {
    if let Some(file_id) = source.trim().strip_prefix("file:") {
        let file_id = file_id.trim();
        if file_id.is_empty() {
            return Err("file_id vazio no marcador de imagem.".to_string());
        }
        let bytes = fetch_generated_file_bytes(file_id).await?;
        if bytes.len() as u64 > MAX_INLINE_IMAGE_BYTES {
            return Err(format!(
                "Imagem muito grande para exibir ({:.1} MB).",
                bytes.len() as f64 / (1024.0 * 1024.0)
            ));
        }
        // O catálogo não devolve o nome no download; o mime sai do conteúdo.
        let mime = infer_image_mime(&bytes)
            .ok_or_else(|| "O arquivo do catálogo não é uma imagem reconhecida.".to_string())?;
        return Ok(ChatImage {
            data_url: format!("data:{};base64,{}", mime, base64_encode(&bytes)),
            // `chars().take` (não fatia de bytes): um id fora do ASCII não pode
            // derrubar o comando no meio de um caractere.
            name: format!(
                "{}.{}",
                file_id.chars().take(8).collect::<String>(),
                mime_ext(mime)
            ),
            mime: mime.to_string(),
            size_bytes: bytes.len() as u64,
            path: None,
        });
    }

    let trimmed = source.trim();
    if trimmed.starts_with("http://") || trimmed.starts_with("https://") {
        let bytes = fetch_remote_image_bytes(trimmed).await?;
        // Mime pelos BYTES, nunca pela extensão da URL: link de CDN de loja
        // costuma terminar em `...jpg_720x720q80.jpg_.webp` ou em nada.
        let mime = infer_image_mime(&bytes)
            .ok_or_else(|| "O que veio dessa URL não é uma imagem reconhecida.".to_string())?;
        let name = reqwest::Url::parse(trimmed)
            .ok()
            .and_then(|u| {
                u.path_segments()
                    .and_then(|segs| segs.last().map(|s| s.to_string()))
            })
            .filter(|n| !n.is_empty())
            .unwrap_or_else(|| format!("imagem.{}", mime_ext(mime)));
        info!(
            "[Neo] load_chat_image (remoto): {} ({} bytes, {})",
            trimmed,
            bytes.len(),
            mime
        );
        return Ok(ChatImage {
            data_url: format!("data:{};base64,{}", mime, base64_encode(&bytes)),
            name,
            mime: mime.to_string(),
            size_bytes: bytes.len() as u64,
            // Não existe arquivo no disco — o lightbox esconde o "Abrir no
            // sistema" quando `path` é None.
            path: None,
        });
    }

    let path = resolve_local_image_path(&source)?;
    ensure_renderable_image(&path)?;

    let meta = tokio::fs::metadata(&path)
        .await
        .map_err(|e| format!("Não consegui ler o arquivo: {}", e))?;
    if meta.len() > MAX_INLINE_IMAGE_BYTES {
        return Err(format!(
            "Imagem muito grande para exibir ({:.1} MB; limite {} MB).",
            meta.len() as f64 / (1024.0 * 1024.0),
            MAX_INLINE_IMAGE_BYTES / (1024 * 1024)
        ));
    }

    let bytes = tokio::fs::read(&path)
        .await
        .map_err(|e| format!("Não consegui ler o arquivo: {}", e))?;
    let mime = infer_image_mime(&bytes)
        .map(|m| m.to_string())
        .unwrap_or_else(|| {
            mime_guess::from_path(&path)
                .first_or_octet_stream()
                .to_string()
        });
    info!(
        "[Neo] load_chat_image: {} ({} bytes, {})",
        path.display(),
        bytes.len(),
        mime
    );

    Ok(ChatImage {
        data_url: format!("data:{};base64,{}", mime, base64_encode(&bytes)),
        name: path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_else(|| "imagem".to_string()),
        mime,
        size_bytes: meta.len(),
        path: Some(path.to_string_lossy().to_string()),
    })
}

/// Mime pelos bytes iniciais (magic number) — mais confiável que a extensão, e é
/// a única pista quando o arquivo vem do catálogo (download sem nome).
fn infer_image_mime(bytes: &[u8]) -> Option<&'static str> {
    if bytes.starts_with(&[0xFF, 0xD8, 0xFF]) {
        return Some("image/jpeg");
    }
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        return Some("image/png");
    }
    if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") {
        return Some("image/gif");
    }
    if bytes.len() > 12 && &bytes[0..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
        return Some("image/webp");
    }
    if bytes.starts_with(b"BM") {
        return Some("image/bmp");
    }
    // SVG é texto: aceita quando começa com <svg ou um XML que o contenha logo.
    let head = String::from_utf8_lossy(&bytes[..bytes.len().min(256)]).to_lowercase();
    if head.contains("<svg") {
        return Some("image/svg+xml");
    }
    None
}

/// Extensão sugerida para um mime de imagem (nome amigável do arquivo do catálogo).
fn mime_ext(mime: &str) -> &'static str {
    match mime {
        "image/png" => "png",
        "image/gif" => "gif",
        "image/webp" => "webp",
        "image/bmp" => "bmp",
        "image/svg+xml" => "svg",
        _ => "jpg",
    }
}

/// Abre a imagem no app padrão do sistema (o "ver em tamanho real de verdade").
#[tauri::command]
pub async fn reveal_local_file(path: String) -> Result<(), String> {
    let resolved = resolve_local_image_path(&path)?;
    let result = if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(&resolved).spawn()
    } else if cfg!(target_os = "windows") {
        std::process::Command::new("cmd")
            .args(["/C", "start", ""])
            .arg(&resolved)
            .spawn()
    } else {
        std::process::Command::new("xdg-open").arg(&resolved).spawn()
    };
    result
        .map(|_| ())
        .map_err(|e| format!("Não consegui abrir o arquivo: {}", e))
}

/// O token da ponte da extensão de música, para colar no popup dela.
///
/// Mostrar a credencial na tela é aceitável aqui e não é no `device-token`: esta
/// existe justamente para ser copiada para outro programa (a extensão), enquanto
/// aquela é do processo. Gera na primeira chamada se ainda não existir.
#[tauri::command]
pub fn ext_bridge_token() -> Result<String, String> {
    crate::ext_bridge_token()
}

/// Aprovar / editar / cancelar um card de missão a partir do CHAT.
///
/// Existe porque o `ChatPage.tsx` fazia isto com `fetch` puro do webview, sem
/// header nenhum — e as rotas de missão estão atrás de credencial no core desde
/// que o painel ganhou JWT, ou seja, **o botão do card não funcionava** e o erro
/// morria num `console.error`. Aqui a chamada leva o token do aparelho, e a falha
/// volta para a UI como `Err` em vez de sumir.
#[tauri::command]
pub async fn mission_command(
    mission_id: String,
    command: String,
    edited_text: Option<String>,
) -> Result<serde_json::Value, String> {
    let base_url = neo_core_http_url();
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(20))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    let mut payload = serde_json::json!({ "command": command });
    if let Some(texto) = edited_text.filter(|t| !t.trim().is_empty()) {
        payload["edited_text"] = serde_json::Value::String(texto);
    }

    let resp = crate::with_device_token(
        client.post(format!("{}/api/v1/missions/{}/command", base_url, mission_id)),
    )
    .json(&payload)
    .send()
    .await
    .map_err(|e| format!("Falha ao contatar o neo-core: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("neo-core respondeu {}: {}", status, body));
    }
    let data: serde_json::Value = resp.json().await.unwrap_or(serde_json::Value::Null);

    // O ack é melhor-esforço: a decisão já foi registrada, e falhar aqui só
    // deixaria a notificação marcada como não vista.
    let ack = crate::with_device_token(client.post(format!(
        "{}/api/v1/missions/{}/notifications/ack",
        base_url, mission_id
    )))
    .send()
    .await;
    if let Err(e) = ack {
        warn!("[Neo] ack da notificação da missão {} falhou: {}", mission_id, e);
    }

    Ok(data)
}


async fn ack_notification_http(notification_id: &str) -> Result<(), String> {
    let base_url = neo_core_http_url();
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    let resp = crate::with_device_token(client.post(format!(
        "{}/api/v1/missions/notifications/{}/ack",
        base_url, notification_id
    )))
        .send()
        .await
        .map_err(|e| format!("Notification ack request failed: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Notification ack failed: {} {}", status, body));
    }

    Ok(())
}

#[tauri::command]
pub fn get_history() -> Result<Vec<Message>, String> {
    let path = get_history_path();
    if !path.exists() {
        return Ok(vec![]);
    }
    let content =
        std::fs::read_to_string(&path).map_err(|e| format!("Failed to read history: {}", e))?;
    let messages: Vec<Message> =
        serde_json::from_str(&content).map_err(|e| format!("Failed to parse history: {}", e))?;
    Ok(messages)
}

#[tauri::command]
pub fn clear_history() -> Result<(), String> {
    let path = get_history_path();
    if path.exists() {
        std::fs::remove_file(&path).map_err(|e| format!("Failed to clear history: {}", e))?;
    }
    Ok(())
}

#[tauri::command]
pub async fn get_conversation_history(state: State<'_, AppState>) -> Result<Vec<Message>, String> {
    let session_id = state.session_id.lock().unwrap().clone();
    let base_url = neo_core_http_url();
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    let url = format!("{}/api/v1/desktop/session/{}/history", base_url, session_id);
    match crate::with_device_token(client.get(url)).send().await {
        Ok(resp) if resp.status().is_success() => match resp.json::<ConversationHistoryResponse>().await {
            Ok(history) => return Ok(history.messages),
            Err(e) => warn!("[Neo] Failed to parse backend history, using local history: {}", e),
        },
        Ok(resp) => warn!(
            "[Neo] Backend history unavailable (status={}), using local history",
            resp.status()
        ),
        Err(e) => warn!("[Neo] Backend history request failed, using local history: {}", e),
    }

    get_history()
}

#[tauri::command]
pub fn clear_conversation_history() -> Result<(), String> {
    clear_history()
}

#[tauri::command]
pub fn save_conversation_history(messages: Vec<Message>) -> Result<(), String> {
    let path = get_history_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create history dir: {}", e))?;
    }
    let payload = serde_json::to_string_pretty(&messages)
        .map_err(|e| format!("Failed to serialize history: {}", e))?;
    std::fs::write(&path, payload).map_err(|e| format!("Failed to write history: {}", e))?;
    Ok(())
}

// ============ SESSION MANAGEMENT ============
//
// As sessões NÃO moram aqui: quem as cria e persiste é o neo-core (tabela
// `conversation_session`). O desktop só gera o UUID no "Novo chat" e o manda em
// cada turno — o core registra a sessão e as mensagens sozinho. Por isso listar
// é um proxy pro core, e não um índice local: um arquivo local só enxergaria as
// sessões desta máquina desde que o recurso existe, e ficaria vazio no 1º uso.

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct SessionInfo {
    pub session_id: String,
    /// Título derivado pelo core (1ª mensagem do usuário); None se não houver.
    pub title: Option<String>,
    pub message_count: i64,
    /// ISO-8601 SEM timezone (o core grava `datetime.utcnow()`), ou seja: UTC.
    /// O front precisa tratar como UTC ao humanizar — ver sessionsService.ts.
    pub last_activity_at: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ListSessionsResponse {
    sessions: Vec<SessionInfo>,
}

/// Sessões anteriores, mais recentes primeiro. Erro de rede vira Err para a UI
/// poder dizer "não deu pra carregar" em vez de mostrar uma lista vazia (que o
/// usuário leria como "não tenho conversas").
#[tauri::command]
pub async fn list_sessions(limit: Option<u32>) -> Result<Vec<SessionInfo>, String> {
    let base_url = neo_core_http_url();
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    let url = format!(
        "{}/api/v1/desktop/sessions?service=neo-desktop-tauri&limit={}",
        base_url,
        limit.unwrap_or(50)
    );

    let resp = crate::with_device_token(client.get(url))
        .send()
        .await
        .map_err(|e| format!("Falha ao contatar o neo-core: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("neo-core respondeu {}", resp.status()));
    }

    let parsed = resp
        .json::<ListSessionsResponse>()
        .await
        .map_err(|e| format!("Resposta inválida do neo-core: {}", e))?;

    Ok(parsed.sessions)
}

/// Catálogo do seletor de provider/modelo do chat (`GET /desktop/model-catalog`).
///
/// O JSON passa direto: quem conhece o formato é o front (lib/models.ts), que
/// também guarda a última lista em cache para o seletor abrir sem esperar a
/// rede. Erro vira Err — o front segue com o cache.
#[tauri::command]
pub async fn get_model_catalog() -> Result<serde_json::Value, String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    let url = format!("{}/api/v1/desktop/model-catalog", neo_core_http_url());
    let resp = crate::with_device_token(client.get(url))
        .send()
        .await
        .map_err(|e| format!("Falha ao contatar o neo-core: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("neo-core respondeu {}", resp.status()));
    }

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Resposta inválida do neo-core: {}", e))
}

/// Sessão ativa agora — a UI usa para destacar a linha correspondente na lista.
#[tauri::command]
pub fn get_session_id(state: State<'_, AppState>) -> Result<String, String> {
    Ok(state.session_id.lock().unwrap().clone())
}

// ============ NEO CORE STATUS ============

#[derive(Debug, Serialize)]
pub struct NeoCoreStatus {
    pub connected: bool,
    pub status: String,
    pub service: String,
    pub checked_at: String,
}

#[tauri::command]
pub async fn get_neo_core_status() -> Result<NeoCoreStatus, String> {
    let url = neo_core_http_url();

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    match client.get(format!("{}/health", url)).send().await {
        Ok(resp) if resp.status().is_success() => Ok(NeoCoreStatus {
            connected: true,
            status: "connected".to_string(),
            service: "neo-core".to_string(),
            checked_at: chrono::Utc::now().to_rfc3339(),
        }),
        _ => Ok(NeoCoreStatus {
            connected: false,
            status: "disconnected".to_string(),
            service: "neo-core".to_string(),
            checked_at: chrono::Utc::now().to_rfc3339(),
        }),
    }
}

// ============ WINDOW MODE ============

#[tauri::command]
pub fn get_window_mode() -> &'static str {
    "tooltip"
}

// ============ VOICE (LIVE MODE) CONFIG ============

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct VoiceConfig {
    /// WebSocket base URL of the neo-voice service (no trailing slash),
    /// e.g. ws://127.0.0.1:5002. The frontend appends /ws/panel and /ws/tts.
    pub ws_base_url: String,
    /// Optional API key forwarded as ?api_key= on the STT websocket.
    pub api_key: Option<String>,
    /// Default TTS voice name; None lets the engine pick its default.
    pub voice: Option<String>,
    /// TTS backend: qwen | coqui | dia.
    pub backend: String,
    /// Optional TTS language override. None deixa cada engine usar seu default
    /// (Qwen="Portuguese", Coqui="pt"), evitando acoplar idioma ao backend.
    pub language: Option<String>,
    /// Segundos de OCIOSIDADE (escutando, sem ninguem falar) ate o Modo Live se
    /// desligar sozinho na JANELA principal. So corre no estado `listening`: e
    /// pausado enquanto o Neo pensa ou fala, e rearmado a cada parcial do STT.
    /// Mais folgado que o do notch porque aqui voce esta olhando a tela, lendo a
    /// resposta anterior — fechar em 15s seria agressivo. 0 desliga o timeout.
    pub live_idle_timeout_s: f64,
    /// Idem para o chip nativo do notch, que e hands-free e fica sobre todas as
    /// janelas: sem ninguem olhando, o mic aberto a toa incomoda mais, entao o
    /// default e bem mais curto. Consumido pelo LiveSession do NeoNotchHost.swift.
    pub notch_idle_timeout_s: f64,
    /// Microfone escolhido pelo usuario, por NOME (o mesmo string no CoreAudio,
    /// no AVCaptureDevice e no enumerateDevices). `None` = padrao do sistema.
    /// Toda captura TENTA este device; se ele sumiu, cai no padrao.
    pub input_device_name: Option<String>,
}

/// Le um timeout em segundos do env, caindo no default quando ausente/invalido.
/// Negativo vira 0 (= sem timeout), p/ um typo nao virar "fecha imediatamente".
fn idle_timeout_env(key: &str, default: f64) -> f64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.trim().parse::<f64>().ok())
        .map(|v| v.max(0.0))
        .unwrap_or(default)
}

/// Returns neo-voice connection settings for the Live (voice) mode, read from
/// env with sensible defaults. O neo-voice roda no neo-host (LXC do Proxmox) na
/// LAN em 192.168.4.210:5002, alcancavel direto pelo desktop (sem Core Tunnel).
/// Sobrescreva com NEO_VOICE_WS_URL (ex.: ws://127.0.0.1:5002 p/ um tunnel local).
#[tauri::command]
pub fn get_voice_config() -> VoiceConfig {
    let ws_base_url = std::env::var("NEO_VOICE_WS_URL")
        .ok()
        .filter(|v| !v.is_empty())
        .map(|v| v.trim_end_matches('/').to_string())
        .unwrap_or_else(|| "ws://192.168.4.210:5002".to_string());
    let api_key = std::env::var("NEO_VOICE_APIKEY")
        .ok()
        .filter(|v| !v.is_empty());
    let voice = std::env::var("NEO_VOICE_TTS_VOICE")
        .ok()
        .filter(|v| !v.is_empty());
    // Coqui (XTTS-v2) é o motor mais rápido (~0.9s p/ o 1º som, rtf ~0.14),
    // ideal para conversa ao vivo. Sobrescreva com NEO_VOICE_TTS_BACKEND.
    let backend = std::env::var("NEO_VOICE_TTS_BACKEND")
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "coqui".to_string());
    let language = std::env::var("NEO_VOICE_TTS_LANGUAGE")
        .ok()
        .filter(|v| !v.is_empty());
    VoiceConfig {
        ws_base_url,
        api_key,
        voice,
        backend,
        language,
        live_idle_timeout_s: idle_timeout_env("NEO_LIVE_IDLE_TIMEOUT_S", 60.0),
        notch_idle_timeout_s: idle_timeout_env("NEO_NOTCH_IDLE_TIMEOUT_S", 15.0),
        input_device_name: crate::services::audio_route::preferred_input(),
    }
}

// ============ WAKE WORD (local, sempre ligado) ============
//
// Liga/desliga o sidecar de wake word (Estagio 1 do Modo Live). O sidecar roda
// LOCAL no Mac e offline (openWakeWord); ao ouvir o gatilho, o Rust emite o
// evento `wake-detected` e o frontend acende o Modo Live. Ver services/wake.rs.

use crate::services::wake::{WakePhrase, WakeStatus};

#[tauri::command]
pub fn wake_start(app: AppHandle, state: State<'_, AppState>) -> WakeStatus {
    state.wake.start(app);
    state.wake.status()
}

#[tauri::command]
pub fn wake_stop(state: State<'_, AppState>) -> WakeStatus {
    state.wake.stop();
    state.wake.status()
}

#[tauri::command]
pub fn wake_status(state: State<'_, AppState>) -> WakeStatus {
    state.wake.status()
}

/// Politica de captura para a rota de audio atual (ver `services::audio_route`):
/// diz ao webview se deve pedir `echoCancellation` e qual mic preferir. Resolvido
/// na hora da chamada -- o usuario conecta/desconecta o fone a qualquer momento.
#[tauri::command]
pub fn audio_route() -> crate::services::audio_route::AudioRoute {
    crate::services::audio_route::resolve()
}

/// Microfones disponiveis agora, p/ o seletor do painel de audio.
#[tauri::command]
pub fn list_audio_inputs() -> Vec<crate::services::audio_route::AudioInputDevice> {
    crate::services::audio_route::list_inputs()
}

/// Microfone escolhido pelo usuario (por nome); `None` = padrao do sistema.
#[tauri::command]
pub fn get_audio_input() -> Option<String> {
    crate::services::audio_route::preferred_input()
}

/// Grava a escolha e reenvia o config ao notch, p/ a proxima captura ja usar o
/// device novo sem precisar reiniciar o app.
#[tauri::command]
pub fn set_audio_input(name: Option<String>) -> Result<(), String> {
    crate::services::audio_route::set_preferred_input(name)?;
    crate::notch_host::resend_config();
    Ok(())
}

/// Lista as frases-gatilho cadastradas (com contagem de amostras e status de ref).
#[tauri::command]
pub fn wake_phrases(state: State<'_, AppState>) -> Vec<WakePhrase> {
    state.wake.list_phrases()
}

/// Salva uma amostra gravada (PCM mono 16kHz int16) de uma frase.
/// Nivel das amostras ja gravadas de uma frase — revela as que estao mudas e
/// contaminando a referencia (a trava do `wake_add_sample` so protege o futuro).
#[tauri::command]
pub fn wake_audit_samples(
    state: State<'_, AppState>,
    slug: String,
) -> Vec<crate::services::wake::SampleAudit> {
    state.wake.audit_samples(&slug)
}

/// `role`: `"wake"` (acende o Live) ou `"stop"` (interrompe a fala/o turno).
#[tauri::command]
pub fn wake_add_sample(
    state: State<'_, AppState>,
    label: String,
    samples: Vec<i16>,
    role: Option<String>,
) -> Result<WakePhrase, String> {
    state
        .wake
        .add_sample(label, samples, role.unwrap_or_else(|| "wake".to_string()))
}

/// Apaga uma frase inteira (amostras + referencia).
#[tauri::command]
pub fn wake_delete_phrase(state: State<'_, AppState>, slug: String) -> Result<(), String> {
    state.wake.delete_phrase(&slug)
}

/// Gera a referencia da frase (few-shot). Se o detector estiver rodando,
/// reinicia para recarregar as refs.
#[tauri::command]
pub async fn wake_enroll(
    app: AppHandle,
    state: State<'_, AppState>,
    slug: String,
) -> Result<WakePhrase, String> {
    // WakeManager e Clone; nao seguramos o State<'_> atraves do await.
    let wake = state.wake.clone();
    let phrase = wake.enroll(&slug).await?;
    if wake.status().running {
        wake.stop();
        wake.start(app);
    }
    Ok(phrase)
}

/// O webview reporta quando o TTS dele comeca/para de falar, alimentando o
/// gate anti-eco do wake word (a voz do proprio Neo nao pode acordar o Live).
#[tauri::command]
pub fn wake_tts_state(speaking: bool) {
    crate::services::wake::set_tts_speaking("webview", speaking);
}

/// O webview avisa quando o Modo Live abre/fecha (ou muda de estado) DENTRO do
/// app, para a ilha da notch acender as mesmas asas -- microfone de um lado do
/// recorte, stop do outro -- que a sessao nativa do chip acende sozinha.
#[tauri::command]
pub fn notch_live_ui(active: bool, state: Option<String>, level: Option<f64>) {
    crate::notch_host::live_ui(active, state, level);
}

/// Liga/desliga o modo teste do wake (tela "Teste de Wake"): enquanto ligado, o
/// disparo NAO aciona o Live -- só reporta na tela (evento `wake-test-fire`).
#[tauri::command]
pub fn wake_set_test_mode(on: bool) {
    crate::services::wake::set_wake_test_mode(on);
}

// ============ VOICE ENGINES (PC gamer) ============
//
// Sobe/baixa os engines de STT/TTS que rodam na GPU do PC gamer (RTX 5070 Ti)
// pelos scripts `engine_ctl*.ps1`, executados via SSH. O status é checado direto
// por HTTP em /health de cada porta — reflete a alcançabilidade real pela LAN
// (inclui o portproxy da WSL). Assim o usuário liga os motores sem abrir o SSH
// na mão. Caminhos/portas conferidos no PC gamer (memórias neo-voice /
// pcgamer-engine-scripts / neo-voice-fish-s2-feasibility).

const PC_GAMER_HOST: &str = "192.168.4.185";
const PC_GAMER_USER: &str = "dmdan";

struct EngineDef {
    key: &'static str,
    label: &'static str,
    /// "Windows" (nativo) ou "WSL2".
    kind: &'static str,
    port: u16,
    /// id do backend no neo-voice (None para o STT).
    backend: Option<&'static str>,
    /// Caminho do engine_ctl*.ps1 no PC gamer.
    script: &'static str,
}

const VOICE_ENGINES: &[EngineDef] = &[
    EngineDef {
        key: "stt",
        label: "STT · Parakeet TDT",
        kind: "Windows",
        port: 5005,
        backend: None,
        script: r"C:\Users\dmdan\neo-stt-engine\engine_ctl.ps1",
    },
    EngineDef {
        key: "qwen",
        label: "TTS · Qwen3-TTS",
        kind: "WSL2",
        port: 5006,
        backend: Some("qwen"),
        script: r"C:\Users\dmdan\engine_ctl_wsl.ps1",
    },
    EngineDef {
        key: "coqui",
        label: "TTS · Coqui XTTS-v2",
        kind: "WSL2",
        port: 5007,
        backend: Some("coqui"),
        script: r"C:\Users\dmdan\engine_ctl_coqui_wsl.ps1",
    },
    EngineDef {
        key: "cosyvoice",
        label: "TTS · CosyVoice3",
        kind: "WSL2",
        port: 5009,
        backend: Some("cosyvoice"),
        script: r"C:\Users\dmdan\engine_ctl_cosyvoice_wsl.ps1",
    },
    EngineDef {
        key: "f5",
        label: "TTS · F5-TTS (PT-BR)",
        kind: "WSL2",
        port: 5010,
        backend: Some("f5"),
        script: r"C:\Users\dmdan\engine_ctl_f5_wsl.ps1",
    },
    EngineDef {
        key: "chatterbox",
        label: "TTS · Chatterbox",
        kind: "WSL2",
        port: 5011,
        backend: Some("chatterbox"),
        script: r"C:\Users\dmdan\engine_ctl_chatterbox_wsl.ps1",
    },
];

fn find_engine(key: &str) -> Option<&'static EngineDef> {
    VOICE_ENGINES.iter().find(|e| e.key == key)
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct VoiceEngine {
    pub key: String,
    pub label: String,
    pub kind: String,
    pub port: u16,
    pub backend: Option<String>,
}

/// Lista os engines de voz controláveis no PC gamer (metadados para a UI).
#[tauri::command]
pub fn list_voice_engines() -> Vec<VoiceEngine> {
    VOICE_ENGINES
        .iter()
        .map(|e| VoiceEngine {
            key: e.key.to_string(),
            label: e.label.to_string(),
            kind: e.kind.to_string(),
            port: e.port,
            backend: e.backend.map(|b| b.to_string()),
        })
        .collect()
}

/// `true` se o engine responde em http://<pc-gamer>:<porta>/health. Falhas de
/// rede (motor parado, portproxy ausente) viram `false`, não erro — a UI só
/// quer saber se está online.
#[tauri::command]
pub async fn voice_engine_health(key: String) -> Result<bool, String> {
    let def = find_engine(&key).ok_or_else(|| format!("Engine desconhecido: {}", key))?;
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()
        .map_err(|e| format!("Falha ao criar HTTP client: {}", e))?;
    let url = format!("http://{}:{}/health", PC_GAMER_HOST, def.port);
    match client.get(&url).send().await {
        Ok(resp) => Ok(resp.status().is_success()),
        Err(_) => Ok(false),
    }
}

/// Executa `engine_ctl*.ps1 <action>` no PC gamer via SSH. `action` ∈
/// {start, stop, restart, status}. O script recria portproxy/firewall e sobe o
/// processo destacado via WMI (sobrevive ao fim do SSH). Retorna a saída
/// combinada do script (stdout+stderr).
#[tauri::command]
pub async fn voice_engine_action(key: String, action: String) -> Result<String, String> {
    let action = match action.as_str() {
        "start" | "stop" | "restart" | "status" => action,
        other => return Err(format!("Ação inválida: {}", other)),
    };
    let def = find_engine(&key).ok_or_else(|| format!("Engine desconhecido: {}", key))?;

    let key_path = dirs::home_dir()
        .ok_or_else(|| "Não foi possível resolver o HOME para a chave SSH".to_string())?
        .join(".ssh")
        .join("pc_gamer");

    let remote = format!(
        "powershell -NoProfile -ExecutionPolicy Bypass -File {} {}",
        def.script, action
    );

    info!("[Neo] voice_engine_action: {} {} ({})", key, action, def.script);

    let fut = tokio::process::Command::new("ssh")
        .arg("-i")
        .arg(&key_path)
        .arg("-o")
        .arg("ConnectTimeout=15")
        .arg("-o")
        .arg("StrictHostKeyChecking=accept-new")
        .arg("-o")
        .arg("BatchMode=yes")
        .arg(format!("{}@{}", PC_GAMER_USER, PC_GAMER_HOST))
        .arg(&remote)
        .output();

    // `start` retorna rápido (cria o processo via WMI e volta). Folga p/ o
    // `status`, que sobe um `wsl tail` e pode levar alguns segundos.
    let output = tokio::time::timeout(std::time::Duration::from_secs(75), fut)
        .await
        .map_err(|_| format!("Tempo esgotado ao executar '{}' em {}", action, key))?
        .map_err(|e| {
            format!(
                "Falha ao iniciar o ssh: {} — confira o cliente SSH e a chave ~/.ssh/pc_gamer",
                e
            )
        })?;

    // WSL emite erros em UTF-16 LE → \x00 entre os caracteres; limpa p/ exibir.
    let clean = |b: &[u8]| String::from_utf8_lossy(b).replace('\u{0}', "");
    let stdout = clean(&output.stdout);
    let stderr = clean(&output.stderr);
    let mut parts: Vec<&str> = Vec::new();
    let so = stdout.trim();
    let se = stderr.trim();
    if !so.is_empty() {
        parts.push(so);
    }
    if !se.is_empty() {
        parts.push(se);
    }
    let combined = parts.join("\n");

    if output.status.success() {
        Ok(if combined.is_empty() {
            "(sem saída)".to_string()
        } else {
            combined
        })
    } else {
        Err(if combined.is_empty() {
            format!("ssh saiu com erro ({})", output.status)
        } else {
            combined
        })
    }
}

// ============ QUICK HELP ============

#[tauri::command]
pub async fn quick_help_ask_stream(window: Window, question: String) -> Result<(), String> {
    info!("[Neo] quick_help_ask_stream: {}", question);

    let url = neo_core_http_url();

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    let resp = crate::with_device_token(
        client.post(format!("{}/api/v1/quick-help/ask", url)),
    )
        .json(&serde_json::json!({ "question": question }))
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    if !resp.status().is_success() {
        let error_text = resp.text().await.unwrap_or_default();
        window
            .emit("quick-help-error", format!("Server error: {}", error_text))
            .map_err(|e| e.to_string())?;
        return Ok(());
    }

    let text = resp
        .text()
        .await
        .map_err(|e| format!("Failed to read response: {}", e))?;

    // Send chunks
    for chunk in text.split(|c| c == '\n' || c == ' ') {
        if !chunk.is_empty() {
            let _ = window.emit("quick-help-chunk", chunk);
            tokio::time::sleep(tokio::time::Duration::from_millis(20)).await;
        }
    }

    let _ = window.emit("quick-help-done", ());
    Ok(())
}

// ============ NOTIFICATIONS ============

#[cfg(target_os = "macos")]
#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGEventSourceSecondsSinceLastEventType(state_id: u32, event_type: u32) -> f64;
}

/// Segundos desde o último input do usuário (teclado/mouse/trackpad).
///
/// É o sinal de presença que faz a notificação temporária esperar quem saiu da
/// frente do computador, em vez de sumir no vazio (o mesmo comportamento do
/// GNOME). O host nativo do macOS tem a sua própria leitura em Swift; este
/// comando existe para a janela de fallback em webview usar a MESMA regra —
/// duas superfícies com prazos diferentes seria um bug esperando a vez.
///
/// Fora do macOS devolve 0 (= "sempre presente"): o prazo passa a ser um
/// cronômetro comum, que é o que essas plataformas já faziam.
#[tauri::command]
pub fn system_idle_seconds() -> f64 {
    #[cfg(target_os = "macos")]
    unsafe {
        // 1 = kCGEventSourceStateCombinedSessionState, ~0 = kCGAnyInputEventType.
        CGEventSourceSecondsSinceLastEventType(1, u32::MAX)
    }
    #[cfg(not(target_os = "macos"))]
    0.0
}

#[tauri::command]
pub async fn ack_notification(notification_id: String) -> Result<(), String> {
    info!("[Neo] ack_notification: {}", notification_id);
    ack_notification_http(&notification_id).await
}

#[tauri::command]
pub async fn handle_notification_action(
    app: AppHandle,
    notification_id: String,
    action_type: String,
    action_payload: Option<serde_json::Value>,
) -> Result<(), String> {
    process_notification_action(app, notification_id, action_type, action_payload).await
}

pub async fn process_notification_action(
    app: AppHandle,
    notification_id: String,
    action_type: String,
    action_payload: Option<serde_json::Value>,
) -> Result<(), String> {
    info!(
        "[Neo] handle_notification_action: notification={}, action={}",
        notification_id, action_type
    );

    // Solicitação de Liberação (docs/release-request-plan.md §11). A decisão vai
    // autenticada pelo TOKEN DESTE APARELHO: é ele que diz ao core QUEM aprovou.
    // Sem token não há decisão — e isso é o ponto, não um contratempo: aparelho
    // não pareado não libera dado pessoal.
    if matches!(action_type.as_str(), "liberar" | "recusar") {
        let payload = action_payload.clone().unwrap_or_default();
        let release_id = payload
            .get("release_id")
            .or_else(|| payload.get("releaseId"))
            .and_then(|v| v.as_str())
            .ok_or_else(|| "notification action missing release_id".to_string())?;
        let token = crate::device_token()
            .ok_or("Este aparelho não está pareado — pareie antes de liberar dados.")?;

        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(20))
            .build()
            .map_err(|e| format!("Failed to create client: {}", e))?;

        let resp = client
            .post(format!(
                "{}/api/v1/liberacoes/{}/decidir",
                neo_core_http_url(),
                release_id
            ))
            .query(&[("token", token.as_str())])
            .json(&serde_json::json!({
                "aprovar": action_type == "liberar",
                "origem": "desktop",
            }))
            .send()
            .await
            .map_err(|e| format!("Falha ao registrar a decisão: {}", crate::erro_sem_url(e)))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(format!("Decisão recusada pelo core: {} {}", status, body));
        }
        info!(
            "[Neo] liberação {} decidida: {}",
            release_id, action_type
        );
    }

    if matches!(action_type.as_str(), "approve" | "cancel" | "retry") {
        let payload = action_payload.clone().unwrap_or_default();
        let mission_id = payload
            .get("mission_id")
            .or_else(|| payload.get("missionId"))
            .and_then(|v| v.as_str())
            .ok_or_else(|| "notification action missing mission_id".to_string())?;

        let base_url = neo_core_http_url();

        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(20))
            .build()
            .map_err(|e| format!("Failed to create client: {}", e))?;

        let resp = crate::with_device_token(
            client.post(format!("{}/api/v1/missions/{}/command", base_url, mission_id)),
        )
            .json(&serde_json::json!({ "command": action_type }))
            .send()
            .await
            .map_err(|e| format!("Mission command request failed: {}", e))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(format!("Mission command failed: {} {}", status, body));
        }
    }

    if let Err(e) = ack_notification_http(&notification_id).await {
        warn!("[Neo] Failed to ack notification {}: {}", notification_id, e);
    }

    let action_event = serde_json::json!({
        "notification_id": notification_id,
        "action_type": action_type,
        "action_payload": action_payload,
    });

    if let Err(e) = app.emit("notification-action", action_event) {
        error!("[Neo] Failed to emit notification-action event: {}", e);
    }

    Ok(())
}

/// Lista os dispositivos da conta — a aba "Dispositivos".
///
/// Era um stub que devolvia lista vazia, então a aba nunca mostrou nada. Agora
/// consulta `GET /devices/mine`, autenticado pelo TOKEN deste device (a rota
/// `GET /devices` está atrás do JWT do painel, que o desktop não tem).
/// Sem pareamento não há token — e aí a aba mostra o cartão de pareamento.
#[tauri::command]
pub async fn list_connected_devices() -> Result<Vec<serde_json::Value>, String> {
    let Some(token) = crate::device_token() else {
        return Ok(vec![]);
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("Falha ao criar cliente: {}", e))?;

    let response = client
        .get(format!("{}/api/v1/devices/mine", neo_core_http_url()))
        .query(&[("token", token.as_str())])
        .send()
        .await
        .map_err(|e| format!("Falha ao listar dispositivos: {}", crate::erro_sem_url(e)))?;

    if !response.status().is_success() {
        return Err(format!("Falha ao listar dispositivos: {}", response.status()));
    }

    let payload: serde_json::Value = response
        .json()
        .await
        .map_err(|e| format!("Resposta inválida: {}", e))?;

    Ok(payload
        .get("devices")
        .and_then(|d| d.as_array())
        .cloned()
        .unwrap_or_default())
}

/// Renomeia um dispositivo da conta ("PC da sala").
///
/// É o nome pelo qual o cérebro endereça o aparelho em `device_control` — sem
/// ele, sobra o modelo do hardware, que ninguém fala em voz alta.
#[tauri::command]
pub async fn rename_device(
    target_device_id: String,
    display_name: Option<String>,
) -> Result<(), String> {
    let token = crate::device_token().ok_or("Dispositivo não pareado")?;

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("Falha ao criar cliente: {}", e))?;

    let response = client
        .patch(format!(
            "{}/api/v1/devices/mine/{}",
            neo_core_http_url(),
            target_device_id
        ))
        .query(&[("token", token.as_str())])
        .json(&serde_json::json!({ "display_name": display_name }))
        .send()
        .await
        .map_err(|e| format!("Falha ao renomear: {}", crate::erro_sem_url(e)))?;

    if !response.status().is_success() {
        return Err(format!("Falha ao renomear: {}", response.status()));
    }
    Ok(())
}

#[tauri::command]
pub async fn resize_notification_window(
    app: AppHandle,
    height: f64,
    width: f64,
    position: Option<String>,
) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("notification") {
        let min_height = 72.0;
        let max_height = 720.0;
        let min_width = 296.0;
        let max_width = 420.0;
        let clamped_height = height.min(max_height).max(min_height);
        let clamped_width = width.min(max_width).max(min_width);
        window
            .set_size(tauri::Size::Logical(tauri::LogicalSize {
                width: clamped_width,
                height: clamped_height,
            }))
            .map_err(|e| e.to_string())?;

        let monitor = window
            .primary_monitor()
            .ok()
            .flatten()
            .or_else(|| window.current_monitor().ok().flatten());

        if let Some(monitor) = monitor {
            let work_area = monitor.work_area();
            let work_area_pos = work_area.position;
            let work_area_size = work_area.size;
            let scale = monitor.scale_factor();
            let margin = (16.0 * scale) as i32;
            let requested_position = position.as_deref().unwrap_or("bottom_right");

            if let Ok(window_size) = window.outer_size() {
                let (x, y) = match requested_position {
                    "top_left" => (work_area_pos.x + margin, work_area_pos.y + margin),
                    "top_right" => (
                        work_area_pos.x + work_area_size.width as i32
                            - window_size.width as i32
                            - margin,
                        work_area_pos.y + margin,
                    ),
                    "bottom_left" => (
                        work_area_pos.x + margin,
                        work_area_pos.y + work_area_size.height as i32
                            - window_size.height as i32
                            - margin,
                    ),
                    "center" => (
                        work_area_pos.x
                            + (work_area_size.width as i32 - window_size.width as i32) / 2,
                        work_area_pos.y
                            + (work_area_size.height as i32 - window_size.height as i32) / 2,
                    ),
                    _ => (
                        work_area_pos.x + work_area_size.width as i32
                            - window_size.width as i32
                            - margin,
                        work_area_pos.y + work_area_size.height as i32
                            - window_size.height as i32
                            - margin,
                    ),
                };

                window
                    .set_position(tauri::PhysicalPosition::new(x, y))
                    .map_err(|e| e.to_string())?;
            }
        }
    }
    Ok(())
}

#[tauri::command]
pub async fn set_notification_window_interactive(
    app: AppHandle,
    interactive: bool,
) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("notification") {
        window
            .set_focusable(false)
            .map_err(|e| e.to_string())?;
        window
            .set_ignore_cursor_events(!interactive)
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub async fn set_notification_window_focusable(
    app: AppHandle,
    focusable: bool,
) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("notification") {
        window.set_focusable(focusable).map_err(|e| e.to_string())?;
        if focusable {
            let _ = window.set_focus();
        }
    }
    Ok(())
}

#[tauri::command]
pub async fn is_cursor_over_notification_window(app: AppHandle) -> Result<bool, String> {
    let Some(window) = app.get_webview_window("notification") else {
        return Ok(false);
    };

    if !window.is_visible().map_err(|e| e.to_string())? {
        return Ok(false);
    }

    let cursor = app.cursor_position().map_err(|e| e.to_string())?;
    let position = window.outer_position().map_err(|e| e.to_string())?;
    let size = window.outer_size().map_err(|e| e.to_string())?;
    let left = position.x as f64;
    let top = position.y as f64;
    let right = left + size.width as f64;
    let bottom = top + size.height as f64;

    Ok(cursor.x >= left && cursor.x <= right && cursor.y >= top && cursor.y <= bottom)
}

#[tauri::command]
pub async fn hide_notification_window(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("notification") {
        let _ = window.set_focusable(false);
        let _ = window.set_ignore_cursor_events(true);
        window.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub fn set_notification_context(
    state: State<'_, AppState>,
    active_tab: String,
) -> Result<(), String> {
    if let Ok(mut ctx) = state.notification_context.lock() {
        ctx.active_tab = active_tab;
    }
    Ok(())
}

// ============ MEMÓRIA DE TRABALHO (visor read-only) ============
//
// O caderno mora no neo-core (tabela `working_note`), igual às sessões: aqui é
// só proxy. Nada nesta seção escreve — `pinned`/`status` mudam o que entra no
// contexto do Neo, e isso não pode ser efeito colateral de abrir uma tela.

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct WorkingNoteSummary {
    pub slug: String,
    pub title: String,
    pub summary: String,
    /// `active` ocupa o índice do prompt; `done` é arquivada.
    pub status: String,
    /// `model` = o Neo chamou `wm_write`; `auto` = backstop determinístico.
    pub origin: String,
    pub pinned: bool,
    pub entry_count: i64,
    pub links: Vec<String>,
    /// ISO-8601 SEM timezone (o core grava `datetime.utcnow()`), ou seja: UTC.
    /// O front humaniza tratando como UTC — mesma pegadinha do SessionInfo.
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
    pub expires_at: Option<String>,
    pub session_id: Option<String>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct WorkingNoteDetail {
    #[serde(flatten)]
    pub note: WorkingNoteSummary,
    /// Markdown pronto para render, INTEIRO (sem a truncagem do `wm_read`).
    pub body: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct WorkingNotesPage {
    pub notes: Vec<WorkingNoteSummary>,
    pub count: i64,
    /// Agregado `origin/status` → quantidade. É o número que mostra o backstop
    /// escrevendo no lugar do modelo sem ninguém ter de contar na mão.
    #[serde(default)]
    pub counts: std::collections::HashMap<String, i64>,
}

/// Label da janela do caderno. Precisa estar em `capabilities/default.json`:
/// sem isso a janela abre, mas todo `invoke` dela é NEGADO pelo ACL — a tela
/// subiria em branco, sem erro visível no console do Rust.
const WORKING_MEMORY_WINDOW: &str = "working-memory";

/// Abre (ou traz para a frente) a janela do caderno.
///
/// Janela criada SOB DEMANDA em vez de declarada em `tauri.conf.json` como a de
/// notificação: aquela precisa existir antes do evento chegar, esta só existe
/// quando alguém quer ler. Declarada, seria um webview carregado no boot e
/// mantido em memória para uma tela que se abre de vez em quando.
///
/// Chamar de novo com a janela aberta não cria uma segunda: mostra, restaura e
/// foca a que já existe. Duas cópias do mesmo caderno lado a lado só produziriam
/// a dúvida de qual das duas está desatualizada.
#[tauri::command]
pub async fn open_working_memory_window(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window(WORKING_MEMORY_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        window
            .set_focus()
            .map_err(|e| format!("Não consegui focar a janela do caderno: {}", e))?;
        return Ok(());
    }

    // `?mode=working-memory` no index.html: mesmo mecanismo do `quick-help` que
    // já existe no App.tsx. Evita uma entrada nova de HTML no vite.config e
    // mantém o ThemeProvider do main.tsx valendo aqui também — a janela nasce no
    // mesmo tema da principal (o tema mora no localStorage, mesma origem).
    tauri::WebviewWindowBuilder::new(
        &app,
        WORKING_MEMORY_WINDOW,
        tauri::WebviewUrl::App("index.html?mode=working-memory".into()),
    )
    .title("Caderno do Neo")
    .inner_size(1020.0, 720.0)
    .min_inner_size(680.0, 420.0)
    .resizable(true)
    // Com decoração nativa, ao contrário da janela principal: é uma janela de
    // documento, e o usuário ganha mover/redimensionar/fullscreen/Cmd-W de graça
    // em vez de uma reimplementação da barra de título só para esta tela.
    .decorations(true)
    .transparent(false)
    .build()
    .map_err(|e| format!("Não consegui abrir a janela do caderno: {}", e))?;

    info!("[Neo] janela do caderno de trabalho aberta");
    Ok(())
}

fn working_memory_client() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))
}

/// Todas as notas do caderno. Erro de rede vira `Err` para a UI poder dizer
/// "não deu pra carregar" em vez de mostrar lista vazia — que o usuário leria
/// como "o Neo não anota nada", exatamente a dúvida que a tela existe para tirar.
#[tauri::command]
pub async fn list_working_notes(limit: Option<u32>) -> Result<WorkingNotesPage, String> {
    let url = format!(
        "{}/api/v1/desktop/working-memory/notes?limit={}",
        neo_core_http_url(),
        limit.unwrap_or(200)
    );

    let resp = crate::with_device_token(working_memory_client()?.get(url))
        .send()
        .await
        .map_err(|e| format!("Falha ao contatar o neo-core: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("neo-core respondeu {}", resp.status()));
    }

    resp.json::<WorkingNotesPage>()
        .await
        .map_err(|e| format!("Resposta inválida do neo-core: {}", e))
}

/// O markdown inteiro de uma nota. 404 vira mensagem própria: nota expirada
/// pelo GC entre a listagem e o clique é o caso normal, não uma falha.
#[tauri::command]
pub async fn read_working_note(slug: String) -> Result<WorkingNoteDetail, String> {
    let url = format!(
        "{}/api/v1/desktop/working-memory/notes/{}",
        neo_core_http_url(),
        urlencoding::encode(&slug)
    );

    let resp = crate::with_device_token(working_memory_client()?.get(url))
        .send()
        .await
        .map_err(|e| format!("Falha ao contatar o neo-core: {}", e))?;

    if resp.status() == reqwest::StatusCode::NOT_FOUND {
        return Err(format!(
            "A nota '{}' não existe mais no caderno (pode ter expirado).",
            slug
        ));
    }
    if !resp.status().is_success() {
        return Err(format!("neo-core respondeu {}", resp.status()));
    }

    resp.json::<WorkingNoteDetail>()
        .await
        .map_err(|e| format!("Resposta inválida do neo-core: {}", e))
}

// ── Tarefas em background (jobs do neo-core) ────────────────────────────────

/// Label da janela de tarefas. Mesma exigência do caderno: tem que estar em
/// `capabilities/default.json`, senão todo `invoke` dela é negado pelo ACL.
const JOBS_WINDOW: &str = "jobs";

/// Abre (ou traz para a frente) a janela de tarefas em background.
///
/// Mesmo desenho da janela do caderno: criada sob demanda, decoração nativa, uma
/// só por vez. Um comando longo agora vira job sozinho e roda longe da conversa;
/// sem um lugar para olhar, "está trabalhando" e "travou" são indistinguíveis.
#[tauri::command]
pub async fn open_jobs_window(app: AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window(JOBS_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        window
            .set_focus()
            .map_err(|e| format!("Não consegui focar a janela de tarefas: {}", e))?;
        return Ok(());
    }

    tauri::WebviewWindowBuilder::new(
        &app,
        JOBS_WINDOW,
        tauri::WebviewUrl::App("index.html?mode=jobs".into()),
    )
    .title("Tarefas do Neo")
    .inner_size(1080.0, 720.0)
    .min_inner_size(720.0, 440.0)
    .resizable(true)
    .decorations(true)
    .transparent(false)
    .build()
    .map_err(|e| format!("Não consegui abrir a janela de tarefas: {}", e))?;

    info!("[Neo] janela de tarefas aberta");
    Ok(())
}

/// Proxy CRU para `/api/v1/desktop/jobs*`. A forma do JSON é contrato entre o
/// neo-core e o TypeScript (`JobSummary` em tauriApi.ts): tipar aqui seria um
/// terceiro lugar para divergir sem erro de compilação — só com tela vazia.
async fn jobs_request(
    method: reqwest::Method,
    path_and_query: String,
) -> Result<serde_json::Value, String> {
    let url = format!(
        "{}/api/v1/desktop/jobs{}",
        neo_core_http_url(),
        path_and_query
    );
    let resp = crate::with_device_token(working_memory_client()?.request(method, url))
        .send()
        .await
        .map_err(|e| format!("Falha ao contatar o neo-core: {}", e))?;

    if resp.status() == reqwest::StatusCode::NOT_FOUND {
        return Err("Essa tarefa não existe mais.".to_string());
    }
    if !resp.status().is_success() {
        return Err(format!("neo-core respondeu {}", resp.status()));
    }

    resp.json::<serde_json::Value>()
        .await
        .map_err(|e| format!("Resposta inválida do neo-core: {}", e))
}

/// Tarefas do dono do caderno, ativas primeiro. `status`: `all` ou `active`.
#[tauri::command]
pub async fn list_jobs(
    status: Option<String>,
    limit: Option<u32>,
) -> Result<serde_json::Value, String> {
    jobs_request(
        reqwest::Method::GET,
        format!(
            "?status={}&limit={}",
            urlencoding::encode(status.as_deref().unwrap_or("all")),
            limit.unwrap_or(100)
        ),
    )
    .await
}

#[tauri::command]
pub async fn get_job(job_id: String) -> Result<serde_json::Value, String> {
    jobs_request(
        reqwest::Method::GET,
        format!("/{}", urlencoding::encode(&job_id)),
    )
    .await
}

/// O FIM do log da tarefa (onde quer que ele more no core).
#[tauri::command]
pub async fn get_job_logs(
    job_id: String,
    tail: Option<u32>,
) -> Result<serde_json::Value, String> {
    jobs_request(
        reqwest::Method::GET,
        format!(
            "/{}/logs?tail={}",
            urlencoding::encode(&job_id),
            tail.unwrap_or(16000)
        ),
    )
    .await
}

/// Cancela a tarefa — num comando do sandbox, é o que mata o processo lá.
#[tauri::command]
pub async fn cancel_job(job_id: String) -> Result<serde_json::Value, String> {
    jobs_request(
        reqwest::Method::POST,
        format!("/{}/cancel", urlencoding::encode(&job_id)),
    )
    .await
}


#[cfg(test)]
mod inline_image_tests {
    use super::*;

    #[test]
    fn strips_marker_and_keeps_the_sentence() {
        let out = strip_image_markers("Aqui está:\n[[IMAGE:/Users/d/foto 1.jpeg]]");
        assert_eq!(out, "Aqui está:");
    }

    #[test]
    fn strips_multiple_and_mid_sentence_markers() {
        let out = strip_image_markers("a [[IMAGE:/x.png]] b [[image : file:abc]] c");
        assert_eq!(out, "a  b  c");
    }

    #[test]
    fn truncated_marker_never_leaks_the_path() {
        // Resposta cortada no meio do marcador: o caminho não pode ser falado.
        let out = strip_image_markers("Olha só: [[IMAGE:/Users/d/Downloads/WhatsApp Ima");
        assert_eq!(out, "Olha só:");
    }

    #[test]
    fn text_without_marker_is_untouched() {
        let text = "Resposta normal com [colchete] e [[VOICE:mario]]oi[[/VOICE]]";
        assert_eq!(strip_image_markers(text), text);
    }

    #[test]
    fn accented_text_before_marker_keeps_byte_boundaries() {
        // Guard do índice em bytes: acentos antes do marcador não podem cortar
        // no meio de um char (panic) nem comer letra.
        let out = strip_image_markers("Ação, café e coração [[IMAGE:/tmp/a.png]]");
        assert_eq!(out, "Ação, café e coração");
    }

    #[test]
    fn detects_renderable_extensions_and_explains_heic() {
        assert!(ensure_renderable_image(std::path::Path::new("/tmp/a.JPG")).is_ok());
        assert!(ensure_renderable_image(std::path::Path::new("/tmp/a.png")).is_ok());
        let heic = ensure_renderable_image(std::path::Path::new("/tmp/IMG_1.HEIC")).unwrap_err();
        assert!(heic.contains("HEIC"), "{heic}");
        assert!(ensure_renderable_image(std::path::Path::new("/tmp/a.pdf")).is_err());
    }

    #[test]
    fn base64_matches_reference_vectors() {
        assert_eq!(base64_encode(b""), "");
        assert_eq!(base64_encode(b"f"), "Zg==");
        assert_eq!(base64_encode(b"fo"), "Zm8=");
        assert_eq!(base64_encode(b"foo"), "Zm9v");
        assert_eq!(base64_encode(b"foobar"), "Zm9vYmFy");
        assert_eq!(base64_encode(&[0xFF, 0xD8, 0xFF]), "/9j/");
    }

    #[test]
    fn infers_mime_from_magic_bytes() {
        assert_eq!(infer_image_mime(&[0xFF, 0xD8, 0xFF, 0xE0]), Some("image/jpeg"));
        assert_eq!(infer_image_mime(b"\x89PNG\r\n\x1a\n___"), Some("image/png"));
        assert_eq!(infer_image_mime(b"<svg xmlns=\"...\">"), Some("image/svg+xml"));
        assert_eq!(infer_image_mime(b"nao sou imagem"), None);
    }

    #[test]
    fn invented_path_fails_loudly() {
        // O modelo "mostrando" um caminho que não existe tem que falhar com
        // mensagem, não devolver quadro quebrado.
        let err = resolve_local_image_path("/caminho/que/nao/existe-neo-test.png").unwrap_err();
        assert!(err.contains("Não achei"), "{err}");
    }
}

#[cfg(test)]
mod voice_config_tests {
    use super::*;

    /// As chaves precisam bater EXATAMENTE com o que o webview (VoiceConfig do
    /// tauriApi.ts) e o Swift (`v["idleTimeoutS"]`) leem. Um typo aqui nao quebra
    /// nada visivelmente: os dois lados caem no default e o timeout configurado e
    /// ignorado em silencio — por isso o teste.
    #[test]
    fn serializes_idle_timeouts_as_camel_case() {
        let json = serde_json::to_value(get_voice_config()).unwrap();
        assert!(json.get("liveIdleTimeoutS").is_some(), "faltou liveIdleTimeoutS: {json}");
        assert!(json.get("notchIdleTimeoutS").is_some(), "faltou notchIdleTimeoutS: {json}");
    }

    #[test]
    fn idle_timeout_env_falls_back_and_clamps() {
        // ausente -> default
        assert_eq!(idle_timeout_env("NEO_TEST_IDLE_ABSENT_XYZ", 60.0), 60.0);
        // valor valido -> usado
        std::env::set_var("NEO_TEST_IDLE_OK", "12.5");
        assert_eq!(idle_timeout_env("NEO_TEST_IDLE_OK", 60.0), 12.5);
        // lixo -> default (nao vira 0 = "fecha na hora")
        std::env::set_var("NEO_TEST_IDLE_JUNK", "abc");
        assert_eq!(idle_timeout_env("NEO_TEST_IDLE_JUNK", 60.0), 60.0);
        // negativo -> 0 (sem timeout), nunca um prazo negativo
        std::env::set_var("NEO_TEST_IDLE_NEG", "-5");
        assert_eq!(idle_timeout_env("NEO_TEST_IDLE_NEG", 60.0), 0.0);
    }

    #[test]
    fn defaults_are_60_window_and_15_notch() {
        std::env::remove_var("NEO_LIVE_IDLE_TIMEOUT_S");
        std::env::remove_var("NEO_NOTCH_IDLE_TIMEOUT_S");
        let cfg = get_voice_config();
        assert_eq!(cfg.live_idle_timeout_s, 60.0);
        assert_eq!(cfg.notch_idle_timeout_s, 15.0);
    }
}

// ============ PAREAMENTO DE DEVICE ============

#[derive(Serialize)]
pub struct PairingStatus {
    pub paired: bool,
    pub device_id: String,
}

/// Este device já tem token de pareamento?
///
/// A UI usa isto para mostrar o campo do código quando ainda não pareou — sem
/// pareamento o canal de controle não sobe, e "abaixa o volume do PC da sala"
/// não tem como chegar aqui.
#[tauri::command]
pub fn device_pairing_status(state: State<'_, AppState>) -> PairingStatus {
    PairingStatus {
        paired: crate::device_token().is_some(),
        device_id: state.device_id.clone(),
    }
}

#[derive(Deserialize)]
struct PairResponse {
    device_token: String,
    display_name: Option<String>,
}

/// Troca o código de 6 dígitos (gerado no painel) pelo token deste device.
///
/// O token volta UMA vez e é gravado em disco; o core só guarda o hash. Depois
/// disto o canal de controle sobe na próxima conexão.
#[tauri::command]
pub async fn pair_device(
    app: AppHandle,
    state: State<'_, AppState>,
    code: String,
    display_name: Option<String>,
) -> Result<String, String> {
    let device_id = state.device_id.clone();
    let hardware_id = state.hardware_id.clone();
    let capabilities = crate::services::device_control::capabilities();
    // Sem nome dado pelo usuário, o hostname é o melhor padrão: reconhecível de
    // cara, e renomeável depois para "PC da sala".
    let display_name = display_name
        .filter(|n| !n.trim().is_empty())
        .or_else(crate::services::system_info::hostname);

    let body = serde_json::json!({
        "code": code.trim(),
        "device_id": device_id,
        "hardware_id": hardware_id,
        "device_type": "neo-desktop",
        "model": crate::services::system_info::hardware_model(),
        "os_platform": std::env::consts::OS,
        "os_version": crate::services::system_info::os_release(),
        "client_version": env!("CARGO_PKG_VERSION"),
        "display_name": display_name,
        "capabilities": capabilities,
    });

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("Falha ao criar cliente: {}", e))?;

    let response = client
        .post(format!("{}/api/v1/devices/pair", crate::neo_core_http_url()))
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Falha ao parear: {}", e))?;

    if !response.status().is_success() {
        let status = response.status();
        let text = response.text().await.unwrap_or_default();
        return Err(if status.as_u16() == 403 {
            "Código inválido ou expirado — gere outro no painel.".to_string()
        } else {
            format!("Pareamento falhou: {} {}", status, text)
        });
    }

    let payload: PairResponse = response
        .json()
        .await
        .map_err(|e| format!("Resposta de pareamento inválida: {}", e))?;

    crate::store_device_token(&payload.device_token)?;
    info!("[Neo] device pareado: {}", device_id);

    // Reconecta para o canal de controle subir agora, sem esperar um restart.
    let ws_manager = {
        let guard = state.ws_manager.lock().map_err(|e| e.to_string())?;
        guard.clone()
    };
    if let Some(manager) = ws_manager {
        manager.disconnect().await;
        manager.connect().await;
    }
    let _ = app.emit("device-paired", &payload.display_name);

    Ok(payload.display_name.unwrap_or(device_id))
}

/// Remove um dispositivo da conta.
///
/// Apagar o registro invalida o token junto (o core só guardava o hash) e
/// derruba o canal de controle aberto. Remover ESTE aparelho equivale a
/// desparear: o token local vira lixo, então ele também é apagado do disco e a
/// aba volta a mostrar o cartão de pareamento.
#[tauri::command]
pub async fn remove_device(
    state: State<'_, AppState>,
    target_device_id: String,
) -> Result<bool, String> {
    let token = crate::device_token().ok_or("Dispositivo não pareado")?;
    let is_self = target_device_id == state.device_id;

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("Falha ao criar cliente: {}", e))?;

    let response = client
        .delete(format!(
            "{}/api/v1/devices/mine/{}",
            neo_core_http_url(),
            target_device_id
        ))
        .query(&[("token", token.as_str())])
        .send()
        .await
        .map_err(|e| format!("Falha ao remover: {}", crate::erro_sem_url(e)))?;

    if !response.status().is_success() {
        return Err(format!("Falha ao remover: {}", response.status()));
    }

    if is_self {
        crate::clear_device_token()?;
        info!("[Neo] este dispositivo foi removido — token local apagado");
    }
    Ok(is_self)
}

#[cfg(test)]
mod working_memory_tests {
    use super::*;

    /// Resposta REAL de produção (`GET /api/v1/desktop/working-memory/notes/…`),
    /// com o corpo encurtado. O que se testa aqui é o contrato entre o Python e
    /// estas structs: `#[serde(flatten)]` no `WorkingNoteDetail` significa que um
    /// campo renomeado de um lado só apareceria como "Resposta inválida do
    /// neo-core" na tela, e não em tempo de compilação.
    // `r##"…"##` e não `r#"…"#`: o corpo da nota começa com `"# Mix musical`, e a
    // sequência `"#` fecharia o raw string ali no meio do JSON.
    const DETAIL_JSON: &str = r##"{
        "slug": "playlist-mix-youtube",
        "title": "Mix musical do Daniel (YouTube)",
        "summary": "08/09 12:46",
        "status": "active",
        "origin": "model",
        "pinned": false,
        "entry_count": 2,
        "links": [],
        "created_at": "2026-09-05T01:28:53.104476",
        "updated_at": "2026-09-08T12:46:26.464158",
        "expires_at": "2026-10-08T12:46:26.464158",
        "session_id": "8bf5ceab-5f92-4383-8f54-be7edae6069c",
        "body": "# Mix musical do Daniel (YouTube)\n> caderno de trabalho"
    }"##;

    #[test]
    fn detail_deserializa_a_resposta_do_core() {
        let detail: WorkingNoteDetail = serde_json::from_str(DETAIL_JSON).unwrap();
        assert_eq!(detail.note.slug, "playlist-mix-youtube");
        assert_eq!(detail.note.origin, "model");
        assert_eq!(detail.note.entry_count, 2);
        assert!(!detail.note.pinned);
        assert!(detail.body.starts_with("# Mix musical"));
    }

    #[test]
    fn detail_volta_para_o_front_achatado() {
        // O TypeScript declara `WorkingNoteDetail extends WorkingNoteSummary`,
        // ou seja, espera `slug` e `body` no MESMO nível. Se o flatten sumisse,
        // o front receberia `{ note: {...}, body }` e todo campo viraria
        // undefined — sem erro nenhum, só uma tela vazia.
        let detail: WorkingNoteDetail = serde_json::from_str(DETAIL_JSON).unwrap();
        let out = serde_json::to_value(&detail).unwrap();
        assert_eq!(out["slug"], "playlist-mix-youtube");
        assert!(out.get("note").is_none(), "o flatten sumiu: {out}");
    }

    #[test]
    fn lista_aceita_pagina_sem_counts() {
        // `counts` é `#[serde(default)]`: um core mais antigo (ou a resposta de
        // caderno vazio) não pode derrubar a tela inteira.
        let page: WorkingNotesPage =
            serde_json::from_str(r#"{"notes": [], "count": 0}"#).unwrap();
        assert!(page.notes.is_empty());
        assert!(page.counts.is_empty());
    }

    #[test]
    fn lista_le_o_agregado_de_origem() {
        let page: WorkingNotesPage = serde_json::from_str(
            r#"{"notes": [], "count": 0, "counts": {"auto/active": 36, "model/active": 3}}"#,
        )
        .unwrap();
        assert_eq!(page.counts.get("auto/active"), Some(&36));
        assert_eq!(page.counts.get("model/active"), Some(&3));
    }
}

// ─── Navegador headless local ───────────────────────────────────────────────
//
// O headless roda na máquina DO USUÁRIO: mesmo IP residencial do navegador
// visível dele (que é o que destrava sites que recusam o datacenter da VPS),
// sem tomar a tela. Como é a máquina dele que paga a conta de RAM, os tetos são
// configuráveis — ver `services::headless`.

#[tauri::command]
pub async fn headless_stats() -> serde_json::Value {
    crate::services::headless::stats().await
}

#[tauri::command]
pub fn headless_get_limits() -> serde_json::Value {
    let l = crate::services::headless::limits();
    serde_json::json!({
        "max_tabs": l.max_tabs,
        "max_ram_mb": l.max_ram_mb,
        "idle_ttl_s": l.idle_ttl_s,
        "profile": match l.profile {
            crate::services::headless::Profile::Dedicated => "dedicado",
            crate::services::headless::Profile::UserProfile => "logado",
        },
        "browser_path": l.browser_path,
        "available": crate::services::headless::available(),
    })
}

/// Aplica os tetos vindos da tela de Config.
///
/// `max_tabs`/`max_ram_mb` em 0 = "sem teto meu". Não é "sem teto nenhum": o SO
/// continua sendo a rede de segurança, e o gerente ainda fecha aba ociosa pelo
/// TTL. Num laptop, teto nenhum de verdade é o Chromium comendo a máquina.
#[tauri::command]
pub fn headless_set_limits(
    max_tabs: Option<usize>,
    max_ram_mb: Option<u64>,
    idle_ttl_s: Option<u64>,
    profile: Option<String>,
    browser_path: Option<String>,
) -> serde_json::Value {
    let atual = crate::services::headless::limits();
    let proximo = crate::services::headless::Limits {
        max_tabs: max_tabs.unwrap_or(atual.max_tabs),
        max_ram_mb: max_ram_mb.unwrap_or(atual.max_ram_mb),
        // Um TTL de 0 desligaria a única trava que age sem o usuário pedir;
        // o piso de 30s existe para "desligar por engano" não virar vazamento.
        idle_ttl_s: idle_ttl_s.map(|v| if v == 0 { 0 } else { v.max(30) })
            .unwrap_or(atual.idle_ttl_s),
        profile: match profile.as_deref() {
            Some("logado") => crate::services::headless::Profile::UserProfile,
            Some("dedicado") => crate::services::headless::Profile::Dedicated,
            _ => atual.profile,
        },
        browser_path: browser_path.filter(|s| !s.trim().is_empty()).or(atual.browser_path),
    };
    crate::services::headless::set_limits(proximo);
    headless_get_limits()
}

#[tauri::command]
pub async fn headless_shutdown() -> serde_json::Value {
    crate::services::headless::shutdown().await;
    serde_json::json!({ "ok": true })
}
