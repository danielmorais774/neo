use futures_util::StreamExt;
use log::{error, info, warn};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tauri::{AppHandle, Emitter};
use tokio::sync::{mpsc, RwLock};
use tokio_tungstenite::{connect_async, tungstenite::Message};

/// Notification received from neo-core via WebSocket
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NeoNotification {
    pub id: Option<String>,
    pub user_id: Option<String>,
    pub mission_id: Option<String>,
    pub kind: Option<String>,
    pub title: Option<String>,
    pub body: Option<String>,
    pub payload: Option<serde_json::Value>,
    pub created_at_unix: Option<i64>,
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
                                        // Emit to frontend via Tauri event
                                        if let Err(e) = app_handle.emit("notification", &notification) {
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