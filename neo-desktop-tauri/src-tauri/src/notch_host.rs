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
//!   {"type":"prompt","text":…,"source":"voice","raw_text":…,"asr_engine":…}
//!                                               run the full agent for this text
//!                                               (`source` só vem quando o texto
//!                                                saiu do microfone/STT)
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
    use crate::services::ai_agent::VoiceInput;
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
                let raw = data.get("message").and_then(|v| v.as_str()).unwrap_or_default();
                // O marcador `[[IMAGE:…]]` é para a bolha do chat desenhar a imagem;
                // aqui a resposta é FALADA — sem isto o notch leria o caminho inteiro
                // em voz alta ("colchete colchete image dois pontos barra Users…").
                let text = commands::strip_image_markers(raw);
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
    ///
    /// A politica de captura (AEC ligado? qual mic?) NAO vai no payload: o host
    /// nativo resolve a rota de audio ele mesmo, no instante em que abre o mic
    /// (`AudioRoute` no NeoNotchHost.swift). Fone entra e sai a qualquer momento,
    /// e uma rota mandada daqui chegaria velha para a captura do proprio host --
    /// que tambem e disparada pela UI dele, sem passar pelo Rust.
    pub fn live_start() {
        send_to_notch(&serde_json::json!({ "type": "live_start" }));
    }

    /// Tell the native host to interrupt the current Live turn (stop the TTS
    /// playback / cancel the in-flight agent turn) and go back to listening.
    /// Triggered by the "stop" word ("pare").
    pub fn live_interrupt() {
        send_to_notch(&serde_json::json!({ "type": "live_interrupt" }));
    }

    /// Reenvia o config ao notch (ex.: o usuario trocou o microfone no painel).
    /// No-op se o host nao estiver rodando.
    pub fn resend_config() {
        send_config();
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
                // o chip usa o timeout do NOTCH, nao o da janela principal
                "idleTimeoutS": cfg.notch_idle_timeout_s,
                "inputDeviceName": cfg.input_device_name,
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
                        // `source` says the INPUT came from the mic: the notch has
                        // a text field too, and only the mic turns get the ASR
                        // tolerance (voice-input-normalization-plan.md §3).
                        let voice = event
                            .get("source")
                            .and_then(|v| v.as_str())
                            .filter(|s| !s.is_empty())
                            .map(|s| VoiceInput::from_parts(
                                Some(s.to_string()),
                                event.get("raw_text").and_then(|v| v.as_str()).map(str::to_string),
                                event.get("asr_engine").and_then(|v| v.as_str()).map(str::to_string),
                            ))
                            .flatten();
                        commands::run_agent_message(
                            app.clone(),
                            text,
                            None,
                            Some("live".to_string()),
                            None,
                            None,
                            voice,
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
                    "tts_state" => {
                        // Native TTS started/stopped speaking (live chip or
                        // notch island). Feeds the wake-word anti-echo gate so
                        // Neo's own voice can't trigger "Ei Neo".
                        let source = event
                            .get("source")
                            .and_then(|v| v.as_str())
                            .unwrap_or("notch");
                        let speaking = event
                            .get("speaking")
                            .and_then(|v| v.as_bool())
                            .unwrap_or(false);
                        crate::services::wake::set_tts_speaking(source, speaking);
                    }
                    "notch_diag" | "notch_tts_diag" => {
                        // Diagnostico da captura (STT) ou do playback (TTS) do
                        // Modo Live nativo. Logado no Neo Desktop.log para depurar
                        // "capta mas nao transcreve" / "nao fala a resposta".
                        info!("[NeoNotch][diag] {}", event);
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
    forward_agent_event, forward_notification, live_interrupt, live_start, resend_config,
    start_notch_host, stop_notch_host,
};

/// Fora do macOS nao ha notch nativo: trocar o microfone no painel nao precisa
/// avisar ninguem.
#[cfg(not(target_os = "macos"))]
pub fn resend_config() {}
