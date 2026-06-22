use crate::grpc_gen::neo_desktop::{desktop_message, CommandApproval, DesktopMessage};
use crate::services::ai_agent::GrpcClient;
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
pub async fn execute_with_loop(
    app: AppHandle,
    state: State<'_, AppState>,
    message: String,
    model: Option<String>,
    mode: Option<String>,
    provider: Option<String>,
) -> Result<(), String> {
    info!("[Neo] execute_with_loop: {} (model={:?}, mode={:?})", message, model, mode);

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

    tokio::spawn(async move {
        let mut client = GrpcClient::new(&device_id, &hardware_id, &user_id, &neo_core_url);
        client.set_session_id(&session_id);

        let result = client
            .execute_task(
                app_handle.clone(),
                message,
                model.as_deref(),
                provider.as_deref(),
                mode.as_deref(),
                cancel_notify,
                stream_tx_slot,
                ext_bridge,
            )
            .await;

        match result {
            Ok(()) => {
                info!("[Neo] execute_with_loop completed");
            }
            Err(e) => {
                error!("[Neo] execute_with_loop error: {}", e);
                let error_event = AgentEvent {
                    event_type: "error".to_string(),
                    data: serde_json::json!({ "message": e }),
                    timestamp: chrono::Utc::now().to_rfc3339(),
                };
                let _ = app_handle.emit("agent-event", &error_event);
            }
        }
    });

    Ok(())
}

#[tauri::command]
pub fn stop_execution(state: State<'_, AppState>) -> Result<(), String> {
    info!("[Neo] stop_execution requested — cancelling in-flight run");
    // Wake the execute_task read loop, which drops the gRPC stream and thereby
    // cancels the server-side RPC (grpc.aio cancels the handler on client abort).
    state.cancel_notify.notify_waiters();
    Ok(())
}

/// Approve or reject a pending command (the agent's `command_request`). Pushes a
/// CommandApproval into the in-flight stream so neo-core can continue: on approve it
/// replies with an `execute_command` tool request that this client runs inline; on
/// reject it closes the turn. Without this the run hangs until the 300s timeout.
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

#[tauri::command]
pub fn reset_agent(state: State<'_, AppState>, new_session_id: String) -> Result<String, String> {
    info!("[Neo] reset_agent: new session {}", new_session_id);
    let mut session = state.session_id.lock().unwrap();
    *session = new_session_id.clone();
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

    use std::process::Command;

    let output = Command::new("/bin/bash")
        .arg("-c")
        .arg(&command)
        .output()
        .map_err(|e| format!("Failed to execute: {}", e))?;

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();

    if output.status.success() {
        Ok(CommandResult {
            success: true,
            output: Some(if stdout.is_empty() {
                stderr.clone()
            } else {
                stdout
            }),
            error: None,
        })
    } else {
        Ok(CommandResult {
            success: false,
            output: None,
            error: Some(if stderr.is_empty() { stdout } else { stderr }),
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

async fn ack_notification_http(notification_id: &str) -> Result<(), String> {
    let base_url = neo_core_http_url();
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
        .map_err(|e| format!("Failed to create client: {}", e))?;

    let resp = client
        .post(format!(
            "{}/api/v1/missions/notifications/{}/ack",
            base_url, notification_id
        ))
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
    match client.get(url).send().await {
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
}

/// Returns neo-voice connection settings for the Live (voice) mode, read from
/// env with sensible defaults (neo-voice runs on the same Mac at :5002).
#[tauri::command]
pub fn get_voice_config() -> VoiceConfig {
    let ws_base_url = std::env::var("NEO_VOICE_WS_URL")
        .ok()
        .filter(|v| !v.is_empty())
        .map(|v| v.trim_end_matches('/').to_string())
        .unwrap_or_else(|| "ws://127.0.0.1:5002".to_string());
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

/// Lista as frases-gatilho cadastradas (com contagem de amostras e status de ref).
#[tauri::command]
pub fn wake_phrases(state: State<'_, AppState>) -> Vec<WakePhrase> {
    state.wake.list_phrases()
}

/// Salva uma amostra gravada (PCM mono 16kHz int16) de uma frase.
#[tauri::command]
pub fn wake_add_sample(
    state: State<'_, AppState>,
    label: String,
    samples: Vec<i16>,
) -> Result<WakePhrase, String> {
    state.wake.add_sample(label, samples)
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

    let resp = client
        .post(format!("{}/api/v1/quick-help/ask", url))
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

        let resp = client
            .post(format!("{}/api/v1/missions/{}/command", base_url, mission_id))
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

#[tauri::command]
pub async fn list_connected_devices() -> Result<Vec<serde_json::Value>, String> {
    info!("[Neo] list_connected_devices");
    Ok(vec![])
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
