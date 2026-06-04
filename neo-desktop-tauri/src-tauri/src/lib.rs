mod commands;
mod grpc_gen;
mod notch_host;
mod services;
mod websocket;

use crate::services::system_info::collect_system_info;
use crate::websocket::DeviceWsManager;
use log::info;
use std::sync::{Arc, Mutex};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager, RunEvent,
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
        })
        .invoke_handler(tauri::generate_handler![
            commands::execute_with_loop,
            commands::stop_execution,
            commands::reset_agent,
            commands::execute_command,
            commands::get_history,
            commands::clear_history,
            commands::get_neo_core_status,
            commands::get_window_mode,
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

            // Show the main window on startup
            if let Some(window) = app.get_webview_window("main") {
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
                let notch_state = app.state::<NotchHostState>();
                start_notch_host(&notch_state);
            }

            info!("[Neo] WebSocket connections initiated");
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app, event| {
            #[cfg(target_os = "macos")]
            if let RunEvent::Exit = event {
                let notch_state = _app.state::<NotchHostState>();
                stop_notch_host(&notch_state);
            }
        });
}
