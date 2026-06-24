pub mod types;

use crate::commands;
use log::{error, info, warn};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Mutex, OnceLock};
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition};

use self::types::NeoNotification;

const NOTIFICATION_WINDOW_LABEL: &str = "notification";
const NOTIFICATION_MARGIN: i32 = 16;

struct MacNotificationHost {
    child: Child,
    stdin: Arc<Mutex<ChildStdin>>,
}

pub struct NotificationRouter {
    app: AppHandle,
    #[cfg(target_os = "macos")]
    mac_host: Mutex<Option<MacNotificationHost>>,
}

static ROUTER: OnceLock<NotificationRouter> = OnceLock::new();

pub fn init(app: &AppHandle) {
    let router = NotificationRouter {
        app: app.clone(),
        #[cfg(target_os = "macos")]
        mac_host: Mutex::new(None),
    };
    let _ = ROUTER.set(router);

    #[cfg(target_os = "macos")]
    {
        if let Some(r) = ROUTER.get() {
            r.ensure_macos_host();
        }
    }
}

pub fn show_notification(notification: &NeoNotification) {
    let Some(router) = ROUTER.get() else {
        warn!("[Notifications] Router not initialized");
        return;
    };
    router.show(notification);
}

#[cfg(target_os = "macos")]
pub fn shutdown() {
    if let Some(router) = ROUTER.get() {
        if let Ok(mut guard) = router.mac_host.lock() {
            if let Some(mut host) = guard.take() {
                let _ = host.child.kill();
            }
        }
    }
}

#[cfg(not(target_os = "macos"))]
pub fn shutdown() {}

impl NotificationRouter {
    fn show(&self, notification: &NeoNotification) {
        // Mirror every real notification into the native notch island's
        // expanded list (independent of the toast surface below).
        #[cfg(target_os = "macos")]
        crate::notch_host::forward_notification(notification);

        #[cfg(target_os = "macos")]
        {
            if self.show_via_macos_host(notification) {
                return;
            }
            warn!("[Notifications] Falling back to webview notification");
        }

        self.show_via_webview(notification);
    }

    fn show_via_webview(&self, notification: &NeoNotification) {
        let Some(window) = self.app.get_webview_window(NOTIFICATION_WINDOW_LABEL) else {
            warn!("[Notifications] Notification window not found");
            return;
        };

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

            if let Ok(window_size) = window.outer_size() {
                let position = notification
                    .config
                    .as_ref()
                    .map(|cfg| cfg.position.as_str())
                    .unwrap_or("bottom_right");
                let margin = (NOTIFICATION_MARGIN as f64 * scale) as i32;
                let (x, y) = match position {
                    "top_left" => (work_area_pos.x + margin, work_area_pos.y + margin),
                    "top_right" => (
                        work_area_pos.x + work_area_size.width as i32 - window_size.width as i32 - margin,
                        work_area_pos.y + margin,
                    ),
                    "bottom_left" => (
                        work_area_pos.x + margin,
                        work_area_pos.y + work_area_size.height as i32 - window_size.height as i32 - margin,
                    ),
                    "center" => (
                        work_area_pos.x + (work_area_size.width as i32 - window_size.width as i32) / 2,
                        work_area_pos.y + (work_area_size.height as i32 - window_size.height as i32) / 2,
                    ),
                    _ => (
                        work_area_pos.x + work_area_size.width as i32 - window_size.width as i32 - margin,
                        work_area_pos.y + work_area_size.height as i32 - window_size.height as i32 - margin,
                    ),
                };
                let _ = window.set_position(PhysicalPosition::new(x, y));
            }
        }

        let _ = window.set_focusable(false);
        let _ = window.set_ignore_cursor_events(false);
        let _ = window.show();
        let _ = window.set_always_on_top(true);
        if let Err(e) = self
            .app
            .emit_to(NOTIFICATION_WINDOW_LABEL, "notification", notification)
        {
            error!("[Notifications] Failed to emit to notification webview: {}", e);
        }
    }

    #[cfg(target_os = "macos")]
    fn show_via_macos_host(&self, notification: &NeoNotification) -> bool {
        self.ensure_macos_host();
        let Ok(mut guard) = self.mac_host.lock() else {
            return false;
        };
        let Some(host) = guard.as_mut() else {
            return false;
        };

        let msg = serde_json::json!({
            "type": "show",
            "notification": notification,
        });
        let Ok(line) = serde_json::to_string(&msg) else {
            return false;
        };

        let Ok(mut stdin) = host.stdin.lock() else {
            return false;
        };
        if stdin.write_all(line.as_bytes()).is_err() || stdin.write_all(b"\n").is_err() {
            warn!("[Notifications] macOS host pipe write failed");
            return false;
        }
        true
    }

    #[cfg(not(target_os = "macos"))]
    fn show_via_macos_host(&self, _notification: &NeoNotification) -> bool {
        false
    }

    #[cfg(target_os = "macos")]
    fn ensure_macos_host(&self) {
        let Ok(mut guard) = self.mac_host.lock() else {
            return;
        };
        if guard.is_some() {
            return;
        }

        let current_exe = match std::env::current_exe() {
            Ok(path) => path,
            Err(err) => {
                warn!("[Notifications] current_exe error: {}", err);
                return;
            }
        };
        let Some(exe_dir) = current_exe.parent() else {
            warn!("[Notifications] executable dir not found");
            return;
        };
        let host_path = exe_dir.join("NeoNotificationHost");
        if !host_path.exists() {
            warn!(
                "[Notifications] Native notification host not found: {}",
                host_path.display()
            );
            return;
        }

        let mut child = match Command::new(&host_path)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
        {
            Ok(child) => child,
            Err(err) => {
                warn!("[Notifications] Failed to start macOS host: {}", err);
                return;
            }
        };

        let Some(stdin) = child.stdin.take() else {
            warn!("[Notifications] macOS host stdin unavailable");
            return;
        };
        let stdout = child.stdout.take();
        let app = self.app.clone();
        if let Some(stdout) = stdout {
            std::thread::spawn(move || {
                let reader = BufReader::new(stdout);
                for line in reader.lines().map_while(Result::ok) {
                    if line.trim().is_empty() {
                        continue;
                    }
                    let Ok(event) = serde_json::from_str::<serde_json::Value>(&line) else {
                        continue;
                    };
                    let event_type = event.get("type").and_then(|v| v.as_str()).unwrap_or_default();
                    match event_type {
                        "action" => {
                            let notification_id = event
                                .get("notification_id")
                                .and_then(|v| v.as_str())
                                .unwrap_or_default()
                                .to_string();
                            let action_type = event
                                .get("action_type")
                                .and_then(|v| v.as_str())
                                .unwrap_or_default()
                                .to_string();
                            let action_payload = event.get("payload").cloned();
                            let app_handle = app.clone();
                            tauri::async_runtime::spawn(async move {
                                if let Err(err) = commands::process_notification_action(
                                    app_handle,
                                    notification_id,
                                    action_type,
                                    action_payload,
                                )
                                .await
                                {
                                    error!("[Notifications] Failed to process native action: {}", err);
                                }
                            });
                        }
                        "dismissed" => {
                            let notification_id = event
                                .get("notification_id")
                                .and_then(|v| v.as_str())
                                .unwrap_or_default()
                                .to_string();
                            if !notification_id.is_empty() {
                                tauri::async_runtime::spawn(async move {
                                    let _ = commands::ack_notification(notification_id).await;
                                });
                            }
                        }
                        "ready" => info!("[Notifications] macOS notification host ready"),
                        _ => {}
                    }
                }
            });
        }

        *guard = Some(MacNotificationHost {
            child,
            stdin: Arc::new(Mutex::new(stdin)),
        });
    }
}
