use crate::AppState;
use crate::services::ai_agent::GrpcClient;
use tauri::{State, Emitter, AppHandle, Window};
use serde::{Deserialize, Serialize};
use log::{info, error};
use std::sync::Mutex;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Message {
    pub role: String,
    pub content: String,
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
    _mode: Option<String>,
    provider: Option<String>,
) -> Result<(), String> {
    info!("[Neo] execute_with_loop: {} (model={:?})", message, model);

    let device_id = state.device_id.clone();
    let user_id = state.user_id.clone();
    let session_id = state.session_id.lock().unwrap().clone();
    let neo_core_url = std::env::var("NEO_CORE_URL")
        .unwrap_or_else(|_| "localhost:50051".to_string());

    let app_handle = app.clone();

    tokio::spawn(async move {
        let mut client = GrpcClient::new(&device_id, &user_id, &neo_core_url);
        client.set_session_id(&session_id);

        let result = client
            .execute_task(app_handle.clone(), message, model.as_deref(), provider.as_deref())
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
pub fn stop_execution(_state: State<'_, AppState>) -> Result<(), String> {
    info!("[Neo] stop_execution requested");
    // In a full implementation, we'd set a stop flag on the agent
    Ok(())
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
            output: Some(if stdout.is_empty() { stderr.clone() } else { stdout }),
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

#[tauri::command]
pub fn get_history() -> Result<Vec<Message>, String> {
    let path = get_history_path();
    if !path.exists() {
        return Ok(vec![]);
    }
    let content = std::fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read history: {}", e))?;
    let messages: Vec<Message> = serde_json::from_str(&content)
        .map_err(|e| format!("Failed to parse history: {}", e))?;
    Ok(messages)
}

#[tauri::command]
pub fn clear_history() -> Result<(), String> {
    let path = get_history_path();
    if path.exists() {
        std::fs::remove_file(&path)
            .map_err(|e| format!("Failed to clear history: {}", e))?;
    }
    Ok(())
}

#[tauri::command]
pub fn get_conversation_history() -> Result<Vec<Message>, String> {
    get_history()
}

#[tauri::command]
pub fn clear_conversation_history() -> Result<(), String> {
    clear_history()
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
    let url = std::env::var("NEO_CORE_URL")
        .unwrap_or_else(|_| "http://localhost:8000".to_string());

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

// ============ QUICK HELP ============

#[tauri::command]
pub async fn quick_help_ask_stream(
    window: Window,
    question: String,
) -> Result<(), String> {
    info!("[Neo] quick_help_ask_stream: {}", question);

    let url = std::env::var("NEO_CORE_URL")
        .unwrap_or_else(|_| "http://localhost:8000".to_string());

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
        window.emit("quick-help-error", format!("Server error: {}", error_text))
            .map_err(|e| e.to_string())?;
        return Ok(());
    }

    let text = resp.text().await.map_err(|e| format!("Failed to read response: {}", e))?;

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
    Ok(())
}

#[tauri::command]
pub async fn list_connected_devices() -> Result<Vec<serde_json::Value>, String> {
    info!("[Neo] list_connected_devices");
    Ok(vec![])
}