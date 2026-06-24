//! Native macOS notch island host (`NeoNotchHost`).
//!
//! The host is a separate Swift process that draws the Dynamic-Island-style
//! pill/peek/expanded surface. This module owns its lifecycle and the
//! bidirectional JSON-line IPC bridge over the child's stdin/stdout, mirroring
//! the pattern used by `notifications` for `NeoNotificationHost`.
//!
//! Rust -> notch (stdin):
//!   {"type":"config","voice":{...}}            voice ws config for STT/TTS
//!   {"type":"notification","notification":{…}} a real notification to list
//!   {"type":"agent_complete","text":…,"voice":…} final agent answer (+TTS voice)
//!   {"type":"agent_error","message":…}         agent turn failed
//!
//! notch -> Rust (stdout):
//!   {"type":"ready"}                            host booted; reply with config
//!   {"type":"prompt","text":…}                  run the full agent for this text
//!   {"type":"notification_action","notification_id":…,"action_type":…,"payload":…}
//!   {"type":"notification_dismiss","notification_id":…}

#[cfg(target_os = "macos")]
use std::sync::Mutex;

#[cfg(target_os = "macos")]
pub struct NotchHostState {
    pub child: Mutex<Option<std::process::Child>>,
}

#[cfg(target_os = "macos")]
impl Default for NotchHostState {
    fn default() -> Self {
        Self {
            child: Mutex::new(None),
        }
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use crate::commands;
    use crate::notifications::types::NeoNotification;
    use log::{info, warn};
    use std::io::{BufRead, BufReader, Write};
    use std::process::{ChildStdin, ChildStdout, Command, Stdio};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex, OnceLock};
    use tauri::{AppHandle, Emitter, Manager};

    use super::NotchHostState;

    struct NotchBridge {
        stdin: Arc<Mutex<ChildStdin>>,
        /// True while a notch-initiated agent turn is in flight, so we only
        /// forward `agent-event`s to the notch for prompts it actually sent
        /// (main-window turns must not leak into the notch).
        turn_active: AtomicBool,
    }

    static BRIDGE: OnceLock<NotchBridge> = OnceLock::new();

    /// Write a single JSON line to the notch host's stdin.
    pub fn send_to_notch(value: &serde_json::Value) {
        let Some(bridge) = BRIDGE.get() else {
            return;
        };
        let Ok(line) = serde_json::to_string(value) else {
            return;
        };
        if let Ok(mut stdin) = bridge.stdin.lock() {
            if stdin.write_all(line.as_bytes()).is_err()
                || stdin.write_all(b"\n").is_err()
                || stdin.flush().is_err()
            {
                warn!("[NeoNotch] pipe write failed");
            }
        }
    }

    /// Push a real notification into the notch's expanded list.
    pub fn forward_notification(notification: &NeoNotification) {
        if BRIDGE.get().is_none() {
            return;
        }
        send_to_notch(&serde_json::json!({
            "type": "notification",
            "notification": notification,
        }));
    }

    /// Forward terminal `agent-event`s to the notch, but only while a
    /// notch-initiated turn is active.
    pub fn forward_agent_event(event_type: &str, data: &serde_json::Value) {
        let Some(bridge) = BRIDGE.get() else {
            return;
        };
        if !bridge.turn_active.load(Ordering::Relaxed) {
            return;
        }
        match event_type {
            "complete" => {
                let text = data.get("message").and_then(|v| v.as_str()).unwrap_or_default();
                let voice = data.get("voice").cloned().unwrap_or(serde_json::Value::Null);
                send_to_notch(&serde_json::json!({
                    "type": "agent_complete",
                    "text": text,
                    "voice": voice,
                }));
                bridge.turn_active.store(false, Ordering::Relaxed);
            }
            "error" => {
                let message = data
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("Falha ao processar.");
                send_to_notch(&serde_json::json!({
                    "type": "agent_error",
                    "message": message,
                }));
                bridge.turn_active.store(false, Ordering::Relaxed);
            }
            _ => {}
        }
    }

    /// Tell the native host to enter hands-free Live Mode (shows the chip and
    /// starts native mic capture). Triggered by the wake word.
    pub fn live_start() {
        send_to_notch(&serde_json::json!({ "type": "live_start" }));
    }

    fn send_config() {
        let cfg = commands::get_voice_config();
        send_to_notch(&serde_json::json!({
            "type": "config",
            "voice": {
                "wsBaseUrl": cfg.ws_base_url,
                "apiKey": cfg.api_key,
                "voice": cfg.voice,
                "backend": cfg.backend,
                "language": cfg.language,
            }
        }));
    }

    fn spawn_reader(app: AppHandle, stdout: ChildStdout) {
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines().map_while(Result::ok) {
                if line.trim().is_empty() {
                    continue;
                }
                let Ok(event) = serde_json::from_str::<serde_json::Value>(&line) else {
                    continue;
                };
                let etype = event.get("type").and_then(|v| v.as_str()).unwrap_or_default();
                match etype {
                    "ready" => {
                        info!("[NeoNotch] host ready");
                        send_config();
                    }
                    "prompt" => {
                        let text = event
                            .get("text")
                            .and_then(|v| v.as_str())
                            .unwrap_or_default()
                            .trim()
                            .to_string();
                        if text.is_empty() {
                            continue;
                        }
                        if let Some(bridge) = BRIDGE.get() {
                            bridge.turn_active.store(true, Ordering::Relaxed);
                        }
                        // Full agent, "live" mode -> neo-core replies with compact
                        // spoken text (no markdown), ideal for the notch + TTS.
                        commands::run_agent_message(
                            app.clone(),
                            text,
                            None,
                            Some("live".to_string()),
                            None,
                        );
                    }
                    "notification_action" => {
                        let id = event
                            .get("notification_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or_default()
                            .to_string();
                        let action_type = event
                            .get("action_type")
                            .and_then(|v| v.as_str())
                            .unwrap_or_default()
                            .to_string();
                        let payload = event.get("payload").cloned();
                        let app2 = app.clone();
                        tauri::async_runtime::spawn(async move {
                            if let Err(err) = commands::process_notification_action(
                                app2, id, action_type, payload,
                            )
                            .await
                            {
                                warn!("[NeoNotch] notification action failed: {}", err);
                            }
                        });
                    }
                    "notification_dismiss" => {
                        let id = event
                            .get("notification_id")
                            .and_then(|v| v.as_str())
                            .unwrap_or_default()
                            .to_string();
                        if !id.is_empty() {
                            tauri::async_runtime::spawn(async move {
                                let _ = commands::ack_notification(id).await;
                            });
                        }
                    }
                    "live_close" => {
                        // The chip's close dot was tapped. The host already
                        // stopped the native session; notify the webview too so
                        // any manual Live session there also winds down.
                        let _ = app.emit("live-close-requested", ());
                    }
                    "live_abort" => {
                        // The chip's abort button was tapped — cancel the
                        // in-flight agent turn (mirrors stop_execution).
                        app.state::<crate::AppState>()
                            .cancel_notify
                            .notify_waiters();
                    }
                    _ => {}
                }
            }
            info!("[NeoNotch] host stdout closed");
        });
    }

    pub fn start_notch_host(app: AppHandle, state: &NotchHostState) {
        let mut guard = match state.child.lock() {
            Ok(guard) => guard,
            Err(_) => return,
        };
        if guard.is_some() {
            return;
        }

        let current_exe = match std::env::current_exe() {
            Ok(path) => path,
            Err(err) => {
                warn!("[NeoNotch] Failed to resolve current executable: {}", err);
                return;
            }
        };
        let Some(exe_dir) = current_exe.parent() else {
            warn!("[NeoNotch] Failed to resolve executable directory");
            return;
        };
        let host_path = exe_dir.join("NeoNotchHost");
        if !host_path.exists() {
            warn!("[NeoNotch] Native notch host not found at {}", host_path.display());
            return;
        }

        stop_existing_notch_hosts(&host_path);

        let mut child = match Command::new(&host_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // Inherit so the host's diagnostic logging (mic/voice) shows up in
            // the `tauri dev` console.
            .stderr(Stdio::inherit())
            .spawn()
        {
            Ok(child) => child,
            Err(err) => {
                warn!("[NeoNotch] Failed to start native notch host: {}", err);
                return;
            }
        };

        let Some(stdin) = child.stdin.take() else {
            warn!("[NeoNotch] host stdin unavailable");
            let _ = child.kill();
            return;
        };
        if let Some(stdout) = child.stdout.take() {
            spawn_reader(app, stdout);
        }

        let _ = BRIDGE.set(NotchBridge {
            stdin: Arc::new(Mutex::new(stdin)),
            turn_active: AtomicBool::new(false),
        });

        info!("[NeoNotch] Native notch host started");
        *guard = Some(child);
    }

    fn stop_existing_notch_hosts(host_path: &std::path::Path) {
        let pattern = host_path.to_string_lossy().to_string();
        if pattern.is_empty() {
            return;
        }
        let _ = Command::new("pkill")
            .arg("-f")
            .arg(&pattern)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }

    pub fn stop_notch_host(state: &NotchHostState) {
        let mut guard = match state.child.lock() {
            Ok(guard) => guard,
            Err(_) => return,
        };
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
        }
    }
}

#[cfg(target_os = "macos")]
pub use imp::{
    forward_agent_event, forward_notification, live_start, start_notch_host, stop_notch_host,
};
