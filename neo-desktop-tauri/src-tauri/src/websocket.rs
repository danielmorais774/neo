use futures_util::{SinkExt, StreamExt};
use log::{error, info, warn};
use std::sync::Arc;
use tauri::{AppHandle, Emitter, Manager};
use tokio::sync::{mpsc, RwLock};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use crate::AppState;
use crate::notifications;
use crate::notifications::types::NeoNotification;

fn should_show_notification(app_handle: &AppHandle) -> bool {
    let Some(main_window) = app_handle.get_webview_window("main") else {
        return true;
    };

    let is_visible = main_window.is_visible().unwrap_or(true);
    let is_minimized = main_window.is_minimized().unwrap_or(false);
    let is_focused = main_window.is_focused().unwrap_or(false);

    let active_tab = app_handle
        .state::<AppState>()
        .notification_context
        .lock()
        .map(|ctx| ctx.active_tab.clone())
        .unwrap_or_else(|_| "chat".to_string());

    !(is_visible && !is_minimized && is_focused && active_tab == "chat")
}

/// WebSocket manager for device online/offline signaling, notifications and control
/// A URL sem a credencial, para log.
///
/// Os canais de device recebem o token na QUERY, e estas URLs eram logadas
/// inteiras em nível INFO — ou seja, a credencial ia para o terminal, para o
/// `app.log` em disco e para qualquer screenshot do log. Queimou em 2026-09-27.
///
/// Corta em `token=` e mantém o resto: o que se quer ver no log é o host, o
/// caminho e o `device_id`, nunca o segredo.
fn sem_token(url: &str) -> String {
    match url.find("token=") {
        Some(i) => format!("{}token=<oculto>", &url[..i]),
        None => url.to_string(),
    }
}

pub struct DeviceWsManager {
    device_id: String,
    api_url: String,
    app_handle: AppHandle,
    online_task: Arc<RwLock<Option<tokio::task::JoinHandle<()>>>>,
    notif_task: Arc<RwLock<Option<tokio::task::JoinHandle<()>>>>,
    control_task: Arc<RwLock<Option<tokio::task::JoinHandle<()>>>>,
    shutdown_tx: Arc<RwLock<Option<mpsc::Sender<()>>>>,
}

impl DeviceWsManager {
    pub fn new(device_id: String, app_handle: AppHandle) -> Self {
        let api_url = std::env::var("NEO_CORE_URL")
            .unwrap_or_else(|_| "http://127.0.0.1:8000".to_string());
        
        Self {
            device_id,
            api_url,
            app_handle,
            online_task: Arc::new(RwLock::new(None)),
            notif_task: Arc::new(RwLock::new(None)),
            control_task: Arc::new(RwLock::new(None)),
            shutdown_tx: Arc::new(RwLock::new(None)),
        }
    }

    /// Start both WebSocket connections: online status and notifications
    pub async fn connect(&self) {
        info!("[DeviceWS] Starting connections for device: {}", self.device_id);
        
        // Create shutdown channel
        let (shutdown_tx, _shutdown_rx) = mpsc::channel::<()>(1);
        *self.shutdown_tx.write().await = Some(shutdown_tx);

        // Clone handles for tasks
        let device_id = self.device_id.clone();
        let api_url = self.api_url.clone();
        let app_handle = self.app_handle.clone();

        // Token de pareamento nos canais antigos (F5). Enquanto
        // `neo_device_auth_required` estiver OFF no core, mandar o token é
        // inofensivo; quando for ligada, é o que mantém estes canais de pé.
        let token_qs = crate::device_token()
            .map(|t| format!("&token={}", urlencoding::encode(&t)))
            .unwrap_or_default();

        // Online status WebSocket
        let online_url = format!(
            "{}/api/v1/ws/devices/online?device_id={}{}",
            api_url.replace("http", "ws"),
            urlencoding::encode(&device_id),
            token_qs
        );
        info!("[DeviceWS] Online URL: {}", sem_token(&online_url));

        let online_task = tokio::spawn(async move {
            Self::connect_online_ws(&online_url).await;
        });
        *self.online_task.write().await = Some(online_task);

        // Notifications WebSocket
        let notif_url = format!(
            "{}/api/v1/ws/devices/notifications/{}{}",
            api_url.replace("http", "ws"),
            urlencoding::encode(&device_id),
            // O `&token=` do helper vira `?token=` aqui: esta URL não tem
            // query string própria.
            token_qs.replacen('&', "?", 1)
        );
        info!("[DeviceWS] Notifications URL: {}", sem_token(&notif_url));

        let notif_task = tokio::spawn(async move {
            Self::connect_notifications_ws(&notif_url, app_handle).await;
        });
        *self.notif_task.write().await = Some(notif_task);

        // Canal de CONTROLE (volume, mídia, energia). Só sobe se este device
        // estiver pareado — sem token o core recusa a conexão, e ficar tentando
        // seria um loop de 4401 a cada 5s.
        match crate::device_token() {
            Some(token) => {
                let control_url = format!(
                    "{}/api/v1/ws/devices/control/{}?token={}",
                    self.api_url.replace("http", "ws"),
                    urlencoding::encode(&self.device_id),
                    urlencoding::encode(&token)
                );
                let control_app = self.app_handle.clone();
                let control_task = tokio::spawn(async move {
                    Self::connect_control_ws(&control_url, control_app).await;
                });
                *self.control_task.write().await = Some(control_task);
                info!("[DeviceWS] Control channel initiated");
            }
            None => {
                warn!(
                    "[DeviceWS] Sem token de pareamento — canal de controle NÃO subiu. \
                     Gere um código no painel e pareie este dispositivo."
                );
            }
        }

        info!("[DeviceWS] WebSocket connections initiated");
    }

    /// Connect to online status WebSocket
    async fn connect_online_ws(url: &str) {
        loop {
            match connect_async(url).await {
                Ok((ws_stream, _)) => {
                    info!("[DeviceWS] Online connection established");
                    let (_, mut read) = ws_stream.split();

                    // Keep connection alive, reconnect on close
                    while let Some(msg) = read.next().await {
                        match msg {
                            Ok(Message::Close(_)) => {
                                warn!("[DeviceWS] Online connection closed by server");
                                break;
                            }
                            Ok(_) => {}
                            Err(e) => {
                                warn!("[DeviceWS] Online read error: {}", e);
                                break;
                            }
                        }
                    }
                }
                Err(e) => {
                    error!("[DeviceWS] Failed to connect online WS: {}", e);
                }
            }

            // Reconnect after delay with exponential backoff
            tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
        }
    }

    /// Connect to notifications WebSocket
    async fn connect_notifications_ws(url: &str, app_handle: AppHandle) {
        loop {
            match connect_async(url).await {
                Ok((ws_stream, _)) => {
                    info!("[DeviceWS] Notifications connection established");
                    let (_write, mut read) = ws_stream.split();

                    while let Some(msg) = read.next().await {
                        match msg {
                            Ok(Message::Text(text)) => {
                                match serde_json::from_str::<NeoNotification>(&text) {
                                    Ok(notification) => {
                                        info!("[DeviceWS] Notification received: {:?}", notification.title);
                                        if let Err(e) = app_handle.emit_to("main", "notification", &notification) {
                                            error!("[DeviceWS] Failed to emit notification to main window: {}", e);
                                        }
                                        if should_show_notification(&app_handle) {
                                            notifications::show_notification(&notification);
                                        } else {
                                            info!("[DeviceWS] Notification suppressed by desktop context");
                                        }
                                    }
                                    Err(e) => {
                                        warn!("[DeviceWS] Failed to parse notification: {} - text: {}", e, text);
                                    }
                                }
                            }
                            Ok(Message::Close(_)) => {
                                warn!("[DeviceWS] Notifications connection closed by server");
                                break;
                            }
                            Err(e) => {
                                warn!("[DeviceWS] Notifications read error: {}", e);
                                break;
                            }
                            _ => {}
                        }
                    }
                }
                Err(e) => {
                    error!("[DeviceWS] Failed to connect notifications WS: {}", e);
                }
            }

            // Reconnect after delay
            tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
        }
    }

    /// Canal de controle: recebe comandos do core, executa e devolve o resultado.
    ///
    /// Envelope (docs/device-control-plan.md §3):
    ///   core → device   {"id":"c-7f3a","action":"volume_set","args":{"level":30}}
    ///   device → core   {"id":"c-7f3a","ok":true,"result":{"level":30,"muted":false}}
    ///
    /// O `result` carrega o estado REAL medido depois de agir — é o que permite
    /// ao Neo reportar o volume que ficou, e não o que foi pedido.
    async fn connect_control_ws(url: &str, app_handle: AppHandle) {
        loop {
            match connect_async(url).await {
                Ok((ws_stream, _)) => {
                    info!("[DeviceWS] Control connection established");
                    let (mut write, mut read) = ws_stream.split();

                    // Cada comando roda na SUA tarefa, e as respostas saem por uma
                    // escritora só. Em fila, um `navigate` (até 20 s esperando a
                    // página) numa aba segurava o snapshot de qualquer outra — e o
                    // `paralelo` do neo-core (docs/browser-multi-aba-plan.md §5.3)
                    // virava fila. O core casa a resposta pelo `id`, não pela ordem.
                    let (saida, mut fila) = mpsc::unbounded_channel::<String>();
                    let escritora = tokio::spawn(async move {
                        while let Some(reply) = fila.recv().await {
                            if let Err(e) = write.send(Message::Text(reply.into())).await {
                                warn!("[DeviceWS] Control send failed: {}", e);
                                break;
                            }
                        }
                    });

                    while let Some(msg) = read.next().await {
                        match msg {
                            Ok(Message::Text(text)) => {
                                let app = app_handle.clone();
                                let saida = saida.clone();
                                let texto = text.to_string();
                                tokio::spawn(async move {
                                    if let Some(reply) = Self::handle_control_command(&app, &texto).await {
                                        let _ = saida.send(reply);
                                    }
                                });
                            }
                            Ok(Message::Close(frame)) => {
                                // 4401 = "não pareado". Reconectar em loop só
                                // geraria um 4401 a cada 5s para sempre — o
                                // conserto é parear, não insistir.
                                if frame.as_ref().map(|f| u16::from(f.code)) == Some(4401) {
                                    error!(
                                        "[DeviceWS] Controle recusado: dispositivo não pareado. \
                                         Gere um código no painel e pareie."
                                    );
                                    return;
                                }
                                warn!("[DeviceWS] Control connection closed by server");
                                break;
                            }
                            Err(e) => {
                                warn!("[DeviceWS] Control read error: {}", e);
                                break;
                            }
                            _ => {}
                        }
                    }
                    drop(saida);
                    escritora.abort();
                }
                Err(e) => {
                    error!("[DeviceWS] Failed to connect control WS: {}", e);
                }
            }

            tokio::time::sleep(tokio::time::Duration::from_secs(5)).await;
        }
    }

    /// Executa um comando e monta a resposta. `None` = não responder (ações de
    /// energia: a máquina cai, e o core confirma pela desconexão).
    async fn handle_control_command(app_handle: &AppHandle, text: &str) -> Option<String> {
        use crate::services::device_control;

        let command: serde_json::Value = match serde_json::from_str(text) {
            Ok(v) => v,
            Err(e) => {
                warn!("[DeviceWS] Control command inválido: {}", e);
                return None;
            }
        };

        let id = command.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let action = command.get("action").and_then(|v| v.as_str()).unwrap_or("").to_string();
        let args = command.get("args").cloned().unwrap_or(serde_json::Value::Null);

        info!("[DeviceWS] Control command: {} (id={})", action, id);

        // Energia pede confirmação NESTA máquina — quem está na frente dela é
        // quem decide. Recusar mantém o canal aberto, e o core lê "continua
        // conectado" como cancelamento (§4).
        if matches!(action.as_str(), "suspend" | "shutdown" | "restart") {
            if !Self::confirm_power_action(app_handle, &action).await {
                info!("[DeviceWS] Ação de energia '{}' recusada pelo usuário", action);
                return None;
            }
            let act = action.clone();
            let _ = tauri::async_runtime::spawn_blocking(move || device_control::power(&act)).await;
            return None;
        }

        // Navegador local: assíncrono (socket do open-browser-use + CDP) e fora
        // do spawn_blocking. Ver services/browser_local.rs e
        // neo-core/docs/browser-local-plan.md §3.2.
        if action.starts_with("browser.") {
            let reply = match crate::services::browser_local::execute(&action, &args).await {
                Ok(result) => serde_json::json!({ "id": id, "ok": true, "result": result }),
                Err(e) => {
                    warn!("[DeviceWS] {} falhou: {}", action, e);
                    serde_json::json!({ "id": id, "ok": false, "error": e })
                }
            };
            return Some(reply.to_string());
        }

        let act = action.clone();
        let outcome = tauri::async_runtime::spawn_blocking(move || {
            device_control::execute(&act, &args)
        })
        .await;

        let reply = match outcome {
            Ok(Ok(result)) => serde_json::json!({ "id": id, "ok": true, "result": result }),
            Ok(Err(e)) => serde_json::json!({ "id": id, "ok": false, "error": e }),
            Err(e) => serde_json::json!({
                "id": id,
                "ok": false,
                "error": format!("falha ao executar: {}", e)
            }),
        };
        Some(reply.to_string())
    }

    /// Diálogo nativo de confirmação para desligar/suspender/reiniciar.
    async fn confirm_power_action(app_handle: &AppHandle, action: &str) -> bool {
        use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

        let (title, body) = match action {
            "suspend" => ("Suspender este computador?", "O Neo pediu para suspender esta máquina."),
            "restart" => ("Reiniciar este computador?", "O Neo pediu para reiniciar esta máquina."),
            _ => ("Desligar este computador?", "O Neo pediu para desligar esta máquina."),
        };

        let app = app_handle.clone();
        // blocking_show trava se rodar na thread principal — mesmo padrão do
        // seletor de arquivos em commands/mod.rs.
        tauri::async_runtime::spawn_blocking(move || {
            app.dialog()
                .message(body)
                .title(title)
                .buttons(MessageDialogButtons::OkCancelCustom(
                    "Confirmar".to_string(),
                    "Cancelar".to_string(),
                ))
                .blocking_show()
        })
        .await
        .unwrap_or(false)
    }

    /// Stop all WebSocket connections
    pub async fn disconnect(&self) {
        info!("[DeviceWS] Disconnecting all connections");

        if let Some(tx) = self.shutdown_tx.write().await.take() {
            let _ = tx.send(()).await;
        }

        if let Some(task) = self.online_task.write().await.take() {
            task.abort();
        }

        if let Some(task) = self.notif_task.write().await.take() {
            task.abort();
        }

        if let Some(task) = self.control_task.write().await.take() {
            task.abort();
        }

        info!("[DeviceWS] All connections stopped");
    }
}

#[cfg(test)]
mod log_sem_credencial_tests {
    use super::sem_token;

    /// Os canais de device recebem o token na QUERY, e estas URLs iam para o log
    /// em nível INFO — terminal, `app.log` em disco, e qualquer screenshot.
    /// Queimou em 2026-09-27, junto com o vazamento pela mensagem de erro.
    #[test]
    fn a_url_logada_nao_carrega_o_token() {
        let com = "ws://127.0.0.1:8000/api/v1/ws/devices/online?device_id=abc&token=SEGREDO";
        let limpo = sem_token(com);
        assert!(!limpo.contains("SEGREDO"));
        // O que serve para diagnosticar CONTINUA no log.
        assert!(limpo.contains("device_id=abc"));
        assert!(limpo.contains("/ws/devices/online"));
    }

    #[test]
    fn token_como_primeiro_parametro_tambem_e_cortado() {
        let com = "ws://127.0.0.1:8000/api/v1/ws/devices/notifications/abc?token=SEGREDO";
        assert!(!sem_token(com).contains("SEGREDO"));
    }

    #[test]
    fn url_sem_token_passa_intacta() {
        let sem = "ws://127.0.0.1:8000/api/v1/ws/devices/online?device_id=abc";
        assert_eq!(sem_token(sem), sem);
    }
}
