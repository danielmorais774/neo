mod commands;
mod ext_bridge;
mod grpc_gen;
mod notch_host;
mod notifications;
mod services;
mod websocket;

use crate::services::system_info::collect_system_info;
use crate::websocket::DeviceWsManager;
use log::info;
use std::sync::{Arc, Mutex};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Listener, Manager, RunEvent,
};

#[cfg(target_os = "macos")]
use crate::notch_host::{start_notch_host, stop_notch_host, NotchHostState};

pub struct AppState {
    pub device_id: String,
    pub hardware_id: String,
    pub user_id: String,
    pub session_id: Mutex<String>,
    pub ws_manager: Mutex<Option<Arc<DeviceWsManager>>>,
    pub notification_context: Mutex<NotificationContext>,
    /// Signalled by `stop_execution` to cancel the in-flight agent run.
    pub cancel_notify: Arc<tokio::sync::Notify>,
    /// Sender into the active agent stream, so `respond_to_command` can inject a
    /// command approval/rejection from the UI. Set/cleared by `execute_task`.
    pub stream_tx: crate::services::ai_agent::StreamTxSlot,
    /// Bridge to the browser extension (music tabs + play commands, Etapa 3).
    pub ext_bridge: crate::ext_bridge::ExtBridge,
    /// Supervisor do sidecar de wake word (Estagio 1 do Modo Live).
    pub wake: crate::services::wake::WakeManager,
}

#[derive(Debug, Clone)]
pub struct NotificationContext {
    pub active_tab: String,
}

#[derive(serde::Deserialize)]
struct ResolveDeviceResponse {
    device_id: Option<String>,
    found: bool,
}

fn neo_core_http_url() -> String {
    std::env::var("NEO_CORE_HTTP_URL")
        .or_else(|_| std::env::var("NEO_CORE_API_URL"))
        .ok()
        .or_else(|| {
            std::env::var("NEO_CORE_URL")
                .ok()
                .filter(|value| value.starts_with("http://") || value.starts_with("https://"))
        })
        .unwrap_or_else(|| "http://127.0.0.1:8000".to_string())
        .trim_end_matches('/')
        .to_string()
}

fn resolve_existing_device_id(
    user_id: &str,
    hardware_id: &str,
) -> Option<String> {
    if hardware_id.is_empty() {
        return None;
    }

    tauri::async_runtime::block_on(async move {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .ok()?;

        let response = client
            .get(format!("{}/api/v1/devices/resolve", neo_core_http_url()))
            .query(&[("user_id", user_id), ("hardware_id", hardware_id)])
            .send()
            .await
            .ok()?;

        if !response.status().is_success() {
            return None;
        }

        let payload = response.json::<ResolveDeviceResponse>().await.ok()?;
        if payload.found {
            payload.device_id.filter(|device_id| !device_id.trim().is_empty())
        } else {
            None
        }
    })
}

fn get_or_create_device_id(user_id: &str, hardware_id: &str) -> String {
    let config_dir = dirs::config_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("neo-desktop");
    let id_path = config_dir.join("device-id");

    if let Ok(id) = std::fs::read_to_string(&id_path) {
        let id = id.trim().to_string();
        if !id.is_empty() {
            return id;
        }
    }

    let _ = std::fs::create_dir_all(&config_dir);

    if let Some(existing_device_id) = resolve_existing_device_id(user_id, hardware_id) {
        info!("[Neo] Reusing existing device_id from hardware match");
        let _ = std::fs::write(&id_path, &existing_device_id);
        return existing_device_id;
    }

    let new_id = uuid::Uuid::new_v4().to_string();
    let _ = std::fs::write(&id_path, &new_id);
    new_id
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let system_info = collect_system_info();
    let hardware_id = system_info.hardware_id.clone();
    let user_id = std::env::var("NEO_USER_ID")
        .unwrap_or_else(|_| "5a998544-c3f3-423f-bebb-1cc42fca6063".to_string());
    let device_id = get_or_create_device_id(&user_id, &hardware_id);
    let session_id = uuid::Uuid::new_v4().to_string();

    info!("[Neo] Device ID: {}", device_id);
    if !hardware_id.is_empty() {
        info!("[Neo] Hardware ID detected");
    }

    let mut builder = tauri::Builder::default()
        .plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .build(),
        )
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_fs::init());

    #[cfg(target_os = "macos")]
    {
        builder = builder.manage(NotchHostState::default());
    }

    builder
        .manage(AppState {
            device_id,
            hardware_id,
            user_id,
            session_id: Mutex::new(session_id),
            ws_manager: Mutex::new(None),
            notification_context: Mutex::new(NotificationContext {
                active_tab: "chat".to_string(),
            }),
            cancel_notify: Arc::new(tokio::sync::Notify::new()),
            stream_tx: Arc::new(Mutex::new(None)),
            ext_bridge: crate::ext_bridge::ExtBridge::new(),
            wake: crate::services::wake::WakeManager::new(),
        })
        .invoke_handler(tauri::generate_handler![
            commands::execute_with_loop,
            commands::stop_execution,
            commands::respond_to_command,
            commands::reset_agent,
            commands::execute_command,
            commands::get_history,
            commands::clear_history,
            commands::get_neo_core_status,
            commands::get_window_mode,
            commands::get_voice_config,
            commands::list_voice_engines,
            commands::voice_engine_health,
            commands::voice_engine_action,
            commands::wake_start,
            commands::wake_stop,
            commands::wake_status,
            commands::wake_phrases,
            commands::wake_add_sample,
            commands::wake_delete_phrase,
            commands::wake_enroll,
            commands::get_conversation_history,
            commands::clear_conversation_history,
            commands::save_conversation_history,
            commands::quick_help_ask_stream,
            commands::ack_notification,
            commands::handle_notification_action,
            commands::list_connected_devices,
            commands::resize_notification_window,
            commands::set_notification_window_interactive,
            commands::set_notification_window_focusable,
            commands::is_cursor_over_notification_window,
            commands::hide_notification_window,
            commands::set_notification_context,
        ])
        .setup(|app| {
            info!("[Neo] Desktop initialized!");
            notifications::init(&app.handle().clone());

            // Start the local WebSocket bridge for the browser extension
            // (music tabs + play commands, Etapa 3). Must use the Tauri async
            // runtime — a bare tokio::spawn here panics (no runtime in setup).
            tauri::async_runtime::spawn(ext_bridge::run(
                app.state::<AppState>().ext_bridge.clone(),
            ));

            // Wake word (Estagio 1 do Modo Live): sobe o sidecar local se o
            // ambiente pedir (NEO_WAKE_ENABLED=1). A UI tambem pode ligar/desligar
            // depois via wake_start/wake_stop. Ver services/wake.rs.
            if crate::services::wake::WakeManager::autostart_requested() {
                app.state::<AppState>().wake.start(app.handle().clone());
            }

            // Show the main window on startup
            if let Some(window) = app.get_webview_window("main") {
                // Force a transparent webview background. Without this, macOS
                // WKWebView defaults to white, which flashes through the rounded
                // corners while the window is being dragged (same fix as the
                // notification window below). This keeps the glass/transparent
                // look intact — it only stops the opaque white repaint flicker.
                let _ = window.set_background_color(Some(tauri::webview::Color(0, 0, 0, 0)));
                let _ = window.show();
                let _ = window.set_focus();
            }

            // Force the notification webview to render with a transparent
            // background. Without this, macOS WKWebView defaults to white,
            // which leaks through the rounded corners of the notification body.
            if let Some(notif_window) = app.get_webview_window("notification") {
                let _ = notif_window.set_background_color(Some(tauri::webview::Color(0, 0, 0, 0)));
                let _ = notif_window.set_focusable(false);
                let _ = notif_window.set_ignore_cursor_events(true);
            }

            // Get icon path
            let icon_path = app
                .path()
                .resource_dir()
                .unwrap_or_default()
                .join("icons")
                .join("icon.png");

            // Create system tray with menu
            let open_item = MenuItem::with_id(app, "open", "💬 Abrir Chat", true, None::<&str>)?;
            let agent_item =
                MenuItem::with_id(app, "agent", "🤖 Abrir Agente", true, None::<&str>)?;
            let help_item = MenuItem::with_id(app, "help", "🔍 Ajuda Rápida", true, None::<&str>)?;
            let separator = MenuItem::with_id(app, "sep", "─────────", false, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "❌ Sair", true, None::<&str>)?;

            let menu = Menu::with_items(
                app,
                &[&open_item, &agent_item, &help_item, &separator, &quit_item],
            )?;

            let _tray = TrayIconBuilder::new()
                .icon(
                    tauri::image::Image::from_path(&icon_path).unwrap_or_else(|_| {
                        tauri::image::Image::from_bytes(include_bytes!("../icons/32x32.png"))
                            .unwrap()
                    }),
                )
                .tooltip("Neo Assistant")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "agent" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "help" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::Click { .. } = event {
                        if let Some(window) = tray.app_handle().get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            info!("[Neo] Tray created successfully");

            // Start WebSocket connections for device online status and notifications
            let app_handle = app.handle().clone();
            let state = app.state::<AppState>();
            let device_id = state.device_id.clone();

            // Create and start WebSocket manager
            let ws_manager = Arc::new(DeviceWsManager::new(device_id, app_handle.clone()));

            // Store in state for later cleanup
            if let Ok(mut guard) = state.ws_manager.lock() {
                *guard = Some(ws_manager.clone());
            }

            // Connect WebSockets
            let ws_manager_clone = ws_manager.clone();
            tauri::async_runtime::spawn(async move {
                ws_manager_clone.connect().await;
            });

            #[cfg(target_os = "macos")]
            {
                let app_handle = app.handle().clone();
                let notch_state = app.state::<NotchHostState>();
                start_notch_host(app_handle, &notch_state);

                // Forward terminal agent-events to the native notch host so its
                // prompt field can render the answer (and speak it). The notch
                // module ignores events outside of a notch-initiated turn.
                app.listen("agent-event", |event| {
                    if let Ok(value) = serde_json::from_str::<serde_json::Value>(event.payload()) {
                        let event_type = value
                            .get("type")
                            .and_then(|v| v.as_str())
                            .unwrap_or_default();
                        if let Some(data) = value.get("data") {
                            crate::notch_host::forward_agent_event(event_type, data);
                        }
                    }
                });

                // Wake word ("Ei Neo") triggers native, hands-free Live Mode in
                // the notch host. Native capture keeps working even when the main
                // window is minimized (the webview would be throttled).
                app.listen("wake-detected", |_event| {
                    crate::notch_host::live_start();
                });

                // The "stop" word ("pare") interrupts the in-flight Live turn:
                // stop the TTS / cancel the agent and return to listening.
                app.listen("wake-interrupt", |_event| {
                    crate::notch_host::live_interrupt();
                });
            }

            info!("[Neo] WebSocket connections initiated");
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app, event| {
            #[cfg(target_os = "macos")]
            if let RunEvent::Exit = event {
                notifications::shutdown();
                let notch_state = _app.state::<NotchHostState>();
                stop_notch_host(&notch_state);
            }
        });
}
