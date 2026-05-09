mod commands;
mod grpc_gen;
mod services;

use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};
use std::sync::Mutex;
use log::info;

pub struct AppState {
    pub device_id: String,
    pub user_id: String,
    pub session_id: Mutex<String>,
}

fn get_or_create_device_id() -> String {
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

    let new_id = uuid::Uuid::new_v4().to_string();
    let _ = std::fs::create_dir_all(&config_dir);
    let _ = std::fs::write(&id_path, &new_id);
    new_id
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let device_id = get_or_create_device_id();
    let user_id = std::env::var("NEO_USER_ID")
        .unwrap_or_else(|_| "5a998544-c3f3-423f-bebb-1cc42fca6063".to_string());
    let session_id = uuid::Uuid::new_v4().to_string();

    info!("[Neo] Device ID: {}", device_id);

    tauri::Builder::default()
        .plugin(tauri_plugin_log::Builder::default()
            .level(log::LevelFilter::Info)
            .build())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_fs::init())
        .manage(AppState {
            device_id,
            user_id,
            session_id: Mutex::new(session_id),
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
            commands::quick_help_ask_stream,
            commands::ack_notification,
            commands::list_connected_devices,
        ])
        .setup(|app| {
            info!("[Neo] Desktop initialized!");

            // Show the main window on startup
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }

            // Get icon path
            let icon_path = app.path().resource_dir()
                .unwrap_or_default()
                .join("icons")
                .join("icon.png");

            // Create system tray with menu
            let open_item = MenuItem::with_id(app, "open", "💬 Abrir Chat", true, None::<&str>)?;
            let agent_item = MenuItem::with_id(app, "agent", "🤖 Abrir Agente", true, None::<&str>)?;
            let help_item = MenuItem::with_id(app, "help", "🔍 Ajuda Rápida", true, None::<&str>)?;
            let separator = MenuItem::with_id(app, "sep", "─────────", false, None::<&str>)?;
            let quit_item = MenuItem::with_id(app, "quit", "❌ Sair", true, None::<&str>)?;

            let menu = Menu::with_items(app, &[&open_item, &agent_item, &help_item, &separator, &quit_item])?;

            let _tray = TrayIconBuilder::new()
                .icon(tauri::image::Image::from_path(&icon_path).unwrap_or_else(|_| {
                    tauri::image::Image::from_bytes(include_bytes!("../icons/32x32.png")).unwrap()
                }))
                .tooltip("Neo Assistant")
                .menu(&menu)
                .on_menu_event(|app, event| {
                    match event.id.as_ref() {
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
                    }
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
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}