mod commands;
mod ext_bridge;
mod grpc_gen;
mod notch_host;
mod notifications;
mod services;
mod websocket;

use crate::services::system_info::collect_system_info;
use crate::websocket::DeviceWsManager;
use log::{info, warn};
use std::sync::{Arc, Mutex};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Emitter, Listener, Manager, RunEvent,
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

/// Diretório onde o device guarda sua identidade (`device-id`) e, agora, seu
/// token de pareamento (`device-token`).
fn device_config_dir() -> std::path::PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("neo-desktop")
}

/// Token de pareamento deste device, se já pareado.
///
/// É a credencial que o desktop nunca teve: sem ela o `user_id` era só o que o
/// cliente declarava (`NEO_USER_ID`, com fallback hardcoded), e qualquer um que
/// alcançasse a :8000 podia se dizer dono de qualquer aparelho. O canal de
/// controle — o único que consegue suspender ou desligar a máquina — exige este
/// token sem exceção. Ver docs/device-control-plan.md §2.
pub fn device_token() -> Option<String> {
    let path = device_config_dir().join("device-token");
    let token = std::fs::read_to_string(path).ok()?;
    let token = token.trim().to_string();
    if token.is_empty() {
        None
    } else {
        Some(token)
    }
}

/// Injeta a credencial do aparelho em uma chamada HTTP ao neo-core.
///
/// **Por que um helper e não um header por chamada.** O token já existia (e já
/// viajava no gRPC e nos WS), mas NENHUMA das chamadas HTTP o mandava: histórico,
/// sessões, caderno, tarefas, upload, download e quick-help iam nuas. Com o
/// portão de autenticação do core (`neo-core/docs/api-auth-plan.md`) elas passam
/// a levar 401 em `enforce` — e um helper garante que a próxima chamada nasça com
/// credencial, em vez de nascer nua e ser descoberta em produção.
///
/// Sem token (aparelho não pareado) a chamada sai igual: quem decide se isso
/// passa é o modo do core, não o cliente.
pub fn with_device_token(req: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
    match device_token() {
        Some(token) => req.header("X-Device-Token", token),
        None => req,
    }
}

/// Credencial da ponte da extensão de música (`ws://127.0.0.1:17872`).
///
/// **Por que existe.** A ponte nasceu sem autenticação nenhuma: `accept_async`
/// aceitava qualquer handshake, sem token e sem olhar `Origin`. Isso não era só
/// "alguém mexe na música":
///
/// * a ponte guarda UMA conexão (`set_connection`), então quem conecta depois
///   **desloca a extensão de verdade** em vez de coexistir;
/// * o que entra como `type:"tabs"` vira `metadata["media_context"]["browser_tabs"]`
///   e chega ao prompt do turno — `title`/`url` são string livre, ou seja, um
///   canal de injeção por fora do modelo de ameaça do `release-request-plan`;
/// * `type:"media_event"` faz ESTE processo chamar `/api/v1/eventos/midia`
///   **com o token do aparelho**. Um gatilho local aberto empresta a credencial:
///   o atacante não precisa tê-la, ele pede para quem tem.
///
/// O token é gerado na primeira execução e guardado em disco com 0600, do mesmo
/// jeito que o `device-token`. Nada o expira: ele identifica a extensão instalada,
/// e trocá-lo é apagar o arquivo (aí a extensão precisa colar o novo).
pub fn ext_bridge_token() -> Result<String, String> {
    let dir = device_config_dir();
    let path = dir.join("ext-token");

    if let Ok(existente) = std::fs::read_to_string(&path) {
        let existente = existente.trim().to_string();
        if !existente.is_empty() {
            return Ok(existente);
        }
    }

    std::fs::create_dir_all(&dir).map_err(|e| format!("não consegui criar {:?}: {}", dir, e))?;
    // Dois UUID v4 = 64 hex de CSPRNG (o `uuid` usa `getrandom`). Evita somar
    // uma dependência de `rand` só para isto.
    let token = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    std::fs::write(&path, &token).map_err(|e| format!("não consegui gravar o token: {}", e))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(token)
}

/// Erro de rede SEM a URL — porque a URL carrega o token.
///
/// O `Display` de um `reqwest::Error` inclui a URL da requisição, e várias
/// chamadas ao core levam a credencial na QUERY (`?token=…`, que é como as rotas
/// de device a recebem). Repassar esse texto para a UI põe a credencial na tela —
/// aconteceu em 2026-09-27: o erro de "não alcancei o core" foi renderizado na
/// aba Dispositivos com o token inteiro dentro, e foi parar num screenshot.
///
/// `without_url()` tira a URL e preserva a causa ("error sending request",
/// "operation timed out"), que é o que interessa para diagnosticar.
pub fn erro_sem_url(e: reqwest::Error) -> String {
    e.without_url().to_string()
}

/// Apaga o token local — usado quando ESTE device é removido da conta.
///
/// Sem isto o aparelho ficaria com um token que o core já não reconhece, e o
/// canal de controle entraria num ciclo de 4401 a cada reconexão.
pub fn clear_device_token() -> Result<(), String> {
    let path = device_config_dir().join("device-token");
    match std::fs::remove_file(&path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("não consegui apagar o token: {}", e)),
    }
}

fn store_device_token(token: &str) -> Result<(), String> {
    let dir = device_config_dir();
    std::fs::create_dir_all(&dir).map_err(|e| format!("não consegui criar {:?}: {}", dir, e))?;
    let path = dir.join("device-token");
    std::fs::write(&path, token).map_err(|e| format!("não consegui gravar o token: {}", e))?;

    // 0600: o token vale tanto quanto uma senha da máquina.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

/// O `device_id` que este hardware já tem na conta, se houver.
///
/// Precisa do token de pareamento: o `user_id` agora sai DELE no core, não de um
/// query param (antes bastava declarar o UUID de alguém para descobrir os
/// aparelhos dessa pessoa). Sem token — aparelho nunca pareado — não há registro
/// a reusar de qualquer forma, e o chamador gera um id novo.
fn resolve_existing_device_id(hardware_id: &str) -> Option<String> {
    if hardware_id.is_empty() {
        return None;
    }
    let token = device_token()?;

    tauri::async_runtime::block_on(async move {
        let client = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(5))
            .build()
            .ok()?;

        let response = client
            .get(format!("{}/api/v1/devices/resolve", neo_core_http_url()))
            .query(&[("hardware_id", hardware_id), ("token", token.as_str())])
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

/// Onde o id da sessão de chat fica entre execuções do app.
///
/// O `device_id` sempre foi persistido aqui ao lado; o `session_id` não era, e
/// cada boot do app entrava no neo-core como uma sessão nova SEM elo com a
/// anterior — o core só grava `created_after_expired_session_id` quando é ELE
/// quem fecha a sessão. Resultado medido no corpus: 290 das 536 sessões nasceram
/// assim, sem histórico e sem nada para o `resolve_continuity_context` puxar.
/// Um follow-up depois de reabrir o app ("tenta de novo", "converte o preço da
/// ponte que eu tinha pedido") caía numa conversa em branco.
fn session_id_path() -> std::path::PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("neo-desktop")
        .join("session-id")
}

/// Grava o id da sessão ativa. Chamado pelo `reset_agent`, que é por onde passam
/// TODAS as trocas de sessão: "Novo chat", o seletor de sessões antigas e a
/// adoção do id novo quando o próprio core rotaciona (evento `session_changed`).
/// Sem isto, persistir no boot só congelaria a primeira sessão para sempre.
pub fn persist_session_id(session_id: &str) {
    let path = session_id_path();
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Err(e) = std::fs::write(&path, session_id) {
        warn!("[Neo] não consegui persistir o session_id: {}", e);
    }
}

/// Retoma a sessão do último uso do app, ou cria uma se não houver.
///
/// Falha de leitura nunca é fatal: no pior caso volta ao comportamento antigo
/// (sessão nova), que é degradação, não quebra.
fn get_or_create_session_id() -> String {
    if let Ok(id) = std::fs::read_to_string(session_id_path()) {
        let id = id.trim().to_string();
        if uuid::Uuid::parse_str(&id).is_ok() {
            info!("[Neo] Retomando sessão anterior: {}", id);
            return id;
        }
    }
    let new_id = uuid::Uuid::new_v4().to_string();
    persist_session_id(&new_id);
    new_id
}

fn get_or_create_device_id(hardware_id: &str) -> String {
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

    if let Some(existing_device_id) = resolve_existing_device_id(hardware_id) {
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
    // `NEO_USER_ID` já não é a fonte da verdade: com o aparelho pareado, o core
    // resolve o dono pelo TOKEN e IGNORA o que o cliente declarar
    // (neo-core/docs/api-auth-plan.md §1.4). O UUID que estava hardcoded aqui saiu:
    // ele fazia qualquer instalação se dizer dona da conta de produção, e quando o
    // token existe o valor não é usado para nada. Fica a variável, vazia por
    // padrão, porque o campo do protobuf continua existindo.
    let user_id = std::env::var("NEO_USER_ID").unwrap_or_default();
    let device_id = get_or_create_device_id(&hardware_id);
    let session_id = get_or_create_session_id();

    info!("[Neo] Device ID: {}", device_id);
    if !hardware_id.is_empty() {
        info!("[Neo] Hardware ID detected");
    }

    let mut builder = tauri::Builder::default()
        .plugin(
            // O padrão do plugin é UM arquivo de 40 KB: a falha do job 40852ae7
            // (02:31) já tinha sido descartada às 13:10 do mesmo dia.
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .max_file_size(5 * 1024 * 1024)
                .rotation_strategy(tauri_plugin_log::RotationStrategy::KeepSome(5))
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
            commands::system_idle_seconds,
            commands::pick_attachment_files,
            commands::upload_attachment,
            commands::download_generated_file,
            commands::load_chat_image,
            commands::reveal_local_file,
            commands::stop_execution,
            commands::respond_to_command,
            commands::respond_file_change,
            commands::undo_file_change,
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
            commands::audio_route,
            commands::list_audio_inputs,
            commands::get_audio_input,
            commands::set_audio_input,
            commands::headless_stats,
            commands::headless_get_limits,
            commands::headless_set_limits,
            commands::headless_shutdown,
            commands::wake_start,
            commands::wake_stop,
            commands::wake_status,
            commands::wake_phrases,
            commands::wake_add_sample,
            commands::wake_audit_samples,
            commands::wake_delete_phrase,
            commands::wake_enroll,
            commands::device_pairing_status,
            commands::rename_device,
            commands::remove_device,
            commands::pair_device,
            commands::wake_tts_state,
            commands::notch_live_ui,
            commands::wake_set_test_mode,
            commands::get_conversation_history,
            commands::clear_conversation_history,
            commands::save_conversation_history,
            commands::list_sessions,
            commands::get_model_catalog,
            commands::mission_command,
            commands::ext_bridge_token,
            commands::get_session_id,
            commands::list_working_notes,
            commands::read_working_note,
            commands::open_working_memory_window,
            commands::open_jobs_window,
            commands::open_claude_terminal,
            commands::list_jobs,
            commands::get_job,
            commands::get_job_logs,
            commands::cancel_job,
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
            // O handle vai para a ponte porque o pareamento da extensão pede um
            // DIÁLOGO NATIVO (ver `confirmar_pareamento`): consentimento em página
            // web seria alcançável por qualquer aba.
            app.state::<AppState>()
                .ext_bridge
                .set_app(app.handle().clone());
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

                // Wake word ("Ei Neo") routes Live Mode by WHERE the user is
                // looking:
                //  - main window FOCUSED (frontmost/key) -> open Live Mode INSIDE
                //    the app (webview overlay), where the user is actually looking.
                //    Rust emits `wake-live-inapp`; ChatPage opens the overlay.
                //  - anything else (minimized, hidden, OR visible but behind
                //    another window / not focused) -> the native notch chip.
                // Focus (not mere visibility) is the gate: a window covered by
                // another app is "on screen" but the user isn't watching it.
                let wake_handle = app.handle().clone();
                app.listen("wake-detected", move |_event| {
                    let focused = wake_handle
                        .get_webview_window("main")
                        .map(|w| {
                            w.is_focused().unwrap_or(false)
                                && w.is_visible().unwrap_or(false)
                                && !w.is_minimized().unwrap_or(false)
                        })
                        .unwrap_or(false);
                    if focused {
                        let _ = wake_handle.emit("wake-live-inapp", ());
                    } else {
                        crate::notch_host::live_start();
                    }
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
            // Nenhuma tarefa em segundo plano sobrevive ao app: sem o Neo aberto,
            // ninguém acompanha nem para (services/local_jobs.rs).
            if let RunEvent::Exit = event {
                services::local_jobs::kill_all();
            }
            #[cfg(target_os = "macos")]
            if let RunEvent::Exit = event {
                notifications::shutdown();
                let notch_state = _app.state::<NotchHostState>();
                stop_notch_host(&notch_state);
            }
        });
}
