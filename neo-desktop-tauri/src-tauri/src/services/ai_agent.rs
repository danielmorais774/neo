// AI Agent service - communicates with Neo Core via gRPC bidirectional streaming

use crate::grpc_gen::neo_desktop::neo_desktop_service_client::NeoDesktopServiceClient;
use crate::ext_bridge::ExtBridge;
use crate::grpc_gen::neo_desktop::{
    core_message, desktop_message, CoreMessage, DesktopMessage, ToolResult, UserInput,
};
use log::{error, info, warn};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter};
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::Channel;
use uuid::Uuid;

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


    /// Run a task end-to-end: opens a bidi stream, sends the user input,
    /// emits each CoreMessage to the frontend in real time, and ends as
    /// soon as we receive a final response or an error.
    pub async fn execute_task(
        &self,
        app_handle: AppHandle,
        message: String,
        model: Option<&str>,
        provider: Option<&str>,
        mode: Option<&str>,
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
        let channel = Channel::from_shared(format!("http://{}", neo_core_url))
            .map_err(|e| format!("Failed to create channel: {}", e))?
            .connect()
            .await
            .map_err(|e| format!("Failed to connect: {}", e))?;

        let mut client = NeoDesktopServiceClient::new(channel);

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
        metadata.insert("device_id".to_string(), self.device_id.clone());
        if !self.hardware_id.is_empty() {
            metadata.insert("hardware_id".to_string(), self.hardware_id.clone());
        }
        metadata.insert("os_platform".to_string(), platform.to_string());
        metadata.insert("os_arch".to_string(), arch.to_string());
        metadata.insert("home_dir".to_string(), homedir.clone());
        metadata.insert(
            "downloads_dir".to_string(),
            format!("{}/Downloads", homedir),
        );
        metadata.insert(
            "documents_dir".to_string(),
            format!("{}/Documents", homedir),
        );
        metadata.insert("desktop_dir".to_string(), format!("{}/Desktop", homedir));

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

        // Build the bidi request stream. We hold `tx` in this scope so it
        // stays alive until we explicitly drop it after the loop ends —
        // that's what cleanly closes our side of the stream.
        let (tx, rx) = mpsc::channel::<DesktopMessage>(16);

        // Expose this task's sender so `respond_to_command` (UI approval) can push a
        // CommandApproval into the live stream. The guard clears it on any exit path.
        if let Ok(mut slot) = stream_tx_slot.lock() {
            *slot = Some(tx.clone());
        }
        let _slot_guard = SlotGuard(stream_tx_slot.clone());

        // Push the initial message into the channel BEFORE calling
        // execute_task. Tonic awaits response headers from the server, and
        // grpc.aio in the Python server only fully spins up the stream after
        // it has at least one inbound frame — so we need the message in the
        // buffer before the call. Channel is buffered (16), so this won't
        // block.
        tx.send(desktop_msg)
            .await
            .map_err(|e| format!("Failed to enqueue initial message: {}", e))?;
        info!("[GrpcClient] initial DesktopMessage enqueued");

        let request_stream = ReceiverStream::new(rx);
        let response = client
            .execute_task(request_stream)
            .await
            .map_err(|e| format!("gRPC call failed: {}", e))?;
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
                        } else if t.tool_name == "execute_command" {
                            // Post-approval shell execution: the agent's command went
                            // through CommandRequest → user approval → neo-core now asks
                            // us to actually run it. Execute OFF the read loop (in a
                            // spawned task) so streaming events keep flowing, then send
                            // the ToolResult back. Without this the stream would hang
                            // waiting for a result that never comes.
                            let tx2 = tx.clone();
                            let arguments = t.arguments.clone();
                            let tool_call_id = t.tool_call_id.clone();
                            let tool_name = t.tool_name.clone();
                            let sid = active_session_id.clone();
                            let uid = self.user_id.clone();
                            tokio::spawn(async move {
                                let (success, result_text, error_text) =
                                    tokio::task::spawn_blocking(move || {
                                        run_command_blocking(&arguments)
                                    })
                                    .await
                                    .unwrap_or((
                                        false,
                                        String::new(),
                                        "falha ao executar o comando".to_string(),
                                    ));
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
                                    last_voice_json = r.voice.as_ref().map(|v| {
                                        serde_json::json!({
                                            "engine": v.engine,
                                            "voice": v.voice,
                                            "language": v.language,
                                            "label": v.label,
                                        })
                                    });
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
            core_message::Content::Thinking(t) => AgentEvent {
                event_type: "thinking".to_string(),
                data: serde_json::json!({
                    "sessionId": msg.session_id,
                    "type": "processing",
                    "content": t.message,
                    "iteration": t.iteration,
                }),
                timestamp,
            },
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

/// Dispatch a `media_command` (browser-tab play) to the extension bridge and map
/// the outcome to ``(success, result, error)`` for the ToolResult.
async fn run_media_command(bridge: &ExtBridge, arguments: &str) -> (bool, String, String) {
    let args: serde_json::Value =
        serde_json::from_str(arguments).unwrap_or(serde_json::Value::Null);
    let action = args.get("action").and_then(|v| v.as_str()).unwrap_or("play");
    let target = args.get("target").and_then(|v| v.as_str()).unwrap_or("youtube");
    let video_id = args.get("videoId").and_then(|v| v.as_str()).unwrap_or("");
    match bridge.send_command(action, target, video_id).await {
        Ok(()) => (true, "comando enviado à extensão".to_string(), String::new()),
        Err(e) => (false, String::new(), e),
    }
}

/// Run a shell command from an `execute_command` tool request (already approved by
/// the user via the CommandRequest flow). Returns ``(success, result, error)`` for
/// the ToolResult sent back to neo-core. Blocking — call inside `spawn_blocking`.
fn run_command_blocking(arguments: &str) -> (bool, String, String) {
    let args: serde_json::Value =
        serde_json::from_str(arguments).unwrap_or(serde_json::Value::Null);
    let command = args.get("command").and_then(|v| v.as_str()).unwrap_or("");
    if command.is_empty() {
        return (false, String::new(), "comando ausente".to_string());
    }

    match std::process::Command::new("/bin/bash")
        .arg("-c")
        .arg(command)
        .output()
    {
        Ok(output) => {
            let stdout = String::from_utf8_lossy(&output.stdout).to_string();
            let stderr = String::from_utf8_lossy(&output.stderr).to_string();
            if output.status.success() {
                (true, if stdout.is_empty() { stderr } else { stdout }, String::new())
            } else {
                (false, String::new(), if stderr.is_empty() { stdout } else { stderr })
            }
        }
        Err(e) => (false, String::new(), e.to_string()),
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
