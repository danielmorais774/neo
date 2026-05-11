// AI Agent service - communicates with Neo Core via gRPC bidirectional streaming

use crate::grpc_gen::neo_desktop::neo_desktop_service_client::NeoDesktopServiceClient;
use crate::grpc_gen::neo_desktop::{
    core_message, desktop_message, CoreMessage, DesktopMessage, UserInput,
};
use log::{error, info, warn};
use std::collections::HashMap;
use tauri::{AppHandle, Emitter};
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::Channel;
use uuid::Uuid;

#[derive(Debug, Clone, serde::Serialize)]
pub struct AgentEvent {
    #[serde(rename = "type")]
    pub event_type: String,
    pub data: serde_json::Value,
    pub timestamp: String,
}

pub struct GrpcClient {
    device_id: String,
    user_id: String,
    neo_core_url: String,
    session_id: String,
}

impl GrpcClient {
    pub fn new(device_id: &str, user_id: &str, neo_core_url: &str) -> Self {
        Self {
            device_id: device_id.to_string(),
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
        metadata.insert("device_id".to_string(), self.device_id.clone());
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

        loop {
            let next = tokio::time::timeout_at(task_deadline, stream.message()).await;

            match next {
                Ok(Ok(Some(core_msg))) => {
                    // Emit a real-time event for this CoreMessage.
                    if let Some(evt) = self.core_message_to_event(&core_msg) {
                        if let Err(e) = app_handle.emit("agent-event", &evt) {
                            warn!("[GrpcClient] failed to emit agent-event: {}", e);
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
                    "type": "processing",
                    "content": t.message,
                    "iteration": t.iteration,
                }),
                timestamp,
            },
            core_message::Content::ToolRequest(t) => AgentEvent {
                event_type: "tool_request".to_string(),
                data: serde_json::json!({
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
                            "message": r.message,
                            "isFinal": true,
                            "totalIterations": r.total_iterations,
                        }),
                        timestamp,
                    }
                } else {
                    AgentEvent {
                        event_type: "thinking".to_string(),
                        data: serde_json::json!({
                            "type": "response",
                            "content": r.message,
                        }),
                        timestamp,
                    }
                }
            }
            core_message::Content::Error(e) => AgentEvent {
                event_type: "error".to_string(),
                data: serde_json::json!({
                    "message": if !e.error.is_empty() { e.error.clone() } else { e.details.clone() },
                    "error": e.error,
                    "details": e.details,
                }),
                timestamp,
            },
            core_message::Content::Iteration(i) => AgentEvent {
                event_type: "iteration".to_string(),
                data: serde_json::json!({
                    "current": i.current,
                    "max": i.max,
                }),
                timestamp,
            },
            core_message::Content::PlanUpdate(p) => AgentEvent {
                event_type: "plan_update".to_string(),
                data: serde_json::json!({
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
