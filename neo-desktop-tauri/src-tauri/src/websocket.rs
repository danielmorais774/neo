use futures_util::StreamExt;
use log::{error, info, warn};
use serde::{Deserialize, Deserializer, Serialize};
use std::sync::Arc;
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition};
use tokio::sync::{mpsc, RwLock};
use tokio_tungstenite::{connect_async, tungstenite::Message};

const NOTIFICATION_WINDOW_LABEL: &str = "notification";
const NOTIFICATION_MARGIN: i32 = 16;

// Default notification config
fn default_duration_ms() -> u64 { 6000 }
fn default_position() -> String { "bottom_right".to_string() }
fn default_play_sound() -> bool { true }

/// Custom deserializer to accept both "body" (legacy) and "description" (new)
/// from the Python/neo-core side during migration.
fn deserialize_body_or_desc<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum BodyOrDesc {
        New { description: Option<String> },
        Legacy { body: Option<String> },
        Plain(Option<String>),
    }

    let intermediate = BodyOrDesc::deserialize(deserializer)?;
    match intermediate {
        BodyOrDesc::New { description } => Ok(description),
        BodyOrDesc::Legacy { body } => Ok(body),
        BodyOrDesc::Plain(s) => Ok(s),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationAction {
    pub action_type: String,
    pub label: String,
    #[serde(default)]
    pub payload: Option<serde_json::Value>,
    #[serde(default)]
    pub button_style: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationField {
    pub label: String,
    pub value: String,
    #[serde(default)]
    pub field_type: Option<String>,
    #[serde(default)]
    pub options: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationConfig {
    #[serde(default = "default_duration_ms")]
    pub duration_ms: u64,
    #[serde(default = "default_position")]
    pub position: String,
    #[serde(default)]
    pub persistent: bool,
    #[serde(default = "default_play_sound")]
    pub play_sound: bool,
}

/// Notification received from neo-core via WebSocket
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NeoNotification {
    pub id: Option<String>,
    pub user_id: Option<String>,
    pub mission_id: Option<String>,
    pub kind: Option<String>,
    pub title: Option<String>,
    #[serde(default, deserialize_with = "deserialize_body_or_desc")]
    pub description: Option<String>,
    #[serde(default)]
    pub fields: Vec<NotificationField>,
    #[serde(default)]
    pub actions: Vec<NotificationAction>,
    pub payload: Option<serde_json::Value>,
    pub created_at_unix: Option<i64>,
    #[serde(default)]
    pub config: Option<NotificationConfig>,
}

/// Reposition the notification window based on config.position and show it.
/// Auto-hide scheduling is handled by the frontend based on config.duration_ms / config.persistent.
fn show_notification_window(app_handle: &AppHandle, notification: &NeoNotification) {
    let Some(window) = app_handle.get_webview_window(NOTIFICATION_WINDOW_LABEL) else {
        warn!("[DeviceWS] Notification window not found");
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
            let (x, y) = match notification.config.as_ref().and_then(|c| Some(c.position.as_str())).unwrap_or("bottom_right") {
                "top_left" => (
                    work_area_pos.x + (NOTIFICATION_MARGIN as f64 * scale) as i32,
                    work_area_pos.y + (NOTIFICATION_MARGIN as f64 * scale) as i32,
                ),
                "top_right" => (
                    work_area_pos.x + work_area_size.width as i32 - window_size.width as i32 - (NOTIFICATION_MARGIN as f64 * scale) as i32,
                    work_area_pos.y + (NOTIFICATION_MARGIN as f64 * scale) as i32,
                ),
                "bottom_left" => (
                    work_area_pos.x + (NOTIFICATION_MARGIN as f64 * scale) as i32,
                    work_area_pos.y + work_area_size.height as i32 - window_size.height as i32 - (NOTIFICATION_MARGIN as f64 * scale) as i32,
                ),
                "center" => (
                    work_area_pos.x + (work_area_size.width as i32 - window_size.width as i32) / 2,
                    work_area_pos.y + (work_area_size.height as i32 - window_size.height as i32) / 2,
                ),
                _ => ( // "bottom_right" and fallback
                    work_area_pos.x + work_area_size.width as i32 - window_size.width as i32 - (NOTIFICATION_MARGIN as f64 * scale) as i32,
                    work_area_pos.y + work_area_size.height as i32 - window_size.height as i32 - (NOTIFICATION_MARGIN as f64 * scale) as i32,
                ),
            };
            let _ = window.set_position(PhysicalPosition::new(x, y));
        }
    }

    let _ = window.show();
    let _ = window.set_always_on_top(true);
}

/// WebSocket manager for device online/offline signaling and notifications
pub struct DeviceWsManager {
    device_id: String,
    api_url: String,
    app_handle: AppHandle,
    online_task: Arc<RwLock<Option<tokio::task::JoinHandle<()>>>>,
    notif_task: Arc<RwLock<Option<tokio::task::JoinHandle<()>>>>,
    shutdown_tx: Arc<RwLock<Option<mpsc::Sender<()>>>>,
}

impl DeviceWsManager {
    pub fn new(device_id: String, app_handle: AppHandle) -> Self {
        let api_url = std::env::var("NEO_CORE_URL")
            .unwrap_or_else(|_| "http://localhost:8000".to_string());
        
        Self {
            device_id,
            api_url,
            app_handle,
            online_task: Arc::new(RwLock::new(None)),
            notif_task: Arc::new(RwLock::new(None)),
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

        // Online status WebSocket
        let online_url = format!(
            "{}/api/v1/ws/devices/online?device_id={}",
            api_url.replace("http", "ws"),
            urlencoding::encode(&device_id)
        );
        info!("[DeviceWS] Online URL: {}", online_url);

        let online_task = tokio::spawn(async move {
            Self::connect_online_ws(&online_url).await;
        });
        *self.online_task.write().await = Some(online_task);

        // Notifications WebSocket
        let notif_url = format!(
            "{}/api/v1/ws/devices/notifications/{}",
            api_url.replace("http", "ws"),
            urlencoding::encode(&device_id)
        );
        info!("[DeviceWS] Notifications URL: {}", notif_url);

        let notif_task = tokio::spawn(async move {
            Self::connect_notifications_ws(&notif_url, app_handle).await;
        });
        *self.notif_task.write().await = Some(notif_task);

        info!("[DeviceWS] Both WebSocket connections initiated");
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
                                        // Show the dedicated notification window and forward
                                        // the payload only to it (so it's visually outside
                                        // the main chat window).
                                        show_notification_window(&app_handle, &notification);
                                        if let Err(e) = app_handle.emit_to(
                                            NOTIFICATION_WINDOW_LABEL,
                                            "notification",
                                            &notification,
                                        ) {
                                            error!("[DeviceWS] Failed to emit notification: {}", e);
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

        info!("[DeviceWS] All connections stopped");
    }
}