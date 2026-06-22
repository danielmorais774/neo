// Local WebSocket bridge between the desktop app and the slim browser extension
// (neo-music-ext). The extension connects to ws://127.0.0.1:17872 and:
//   • reports a snapshot of open music tabs (YouTube/Spotify) → fed to neo-core as
//     `metadata["media_context"]["browser_tabs"]` so the music skill can reuse a tab;
//   • executes `media_command` play requests (focus tab + navigate to the videoId)
//     and replies with a correlated result.
//
// Single extension connection at a time (last wins). Loopback-only bind — no LAN
// exposure. See neo-music-feature (Etapa 3).

use futures_util::{SinkExt, StreamExt};
use log::{error, info, warn};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::Message;

/// Loopback port the browser extension connects to.
pub const EXT_BRIDGE_PORT: u16 = 17872;

/// An open music tab reported by the extension. Serialized into `media_context`;
/// `find_music_tab` in neo-core only keys off `site`, the rest is informational.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MusicTab {
    pub site: String, // "youtube" | "spotify"
    #[serde(rename = "tabId", default)]
    pub tab_id: i64,
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub playing: bool,
}

struct CmdResult {
    success: bool,
    error: String,
}

#[derive(Default)]
struct Inner {
    /// Latest tab snapshot from the connected extension.
    tabs: Vec<MusicTab>,
    /// Writer for the current extension socket (None when nothing is connected).
    conn: Option<mpsc::Sender<Message>>,
    /// Monotonic id of the current connection, so a stale socket's teardown can't
    /// clobber a newer connection's writer.
    conn_id: u64,
    /// In-flight `media_command`s awaiting a `result` from the extension, by id.
    pending: HashMap<String, oneshot::Sender<CmdResult>>,
}

/// Shared, cloneable handle to the extension bridge. Stored in `AppState` and used
/// by the gRPC task to read the tab snapshot and dispatch `media_command`s.
#[derive(Clone)]
pub struct ExtBridge {
    inner: Arc<Mutex<Inner>>,
}

impl Default for ExtBridge {
    fn default() -> Self {
        Self::new()
    }
}

impl ExtBridge {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner::default())),
        }
    }

    /// Current open music tabs (empty if no extension is connected).
    pub fn snapshot(&self) -> Vec<MusicTab> {
        self.inner
            .lock()
            .map(|g| g.tabs.clone())
            .unwrap_or_default()
    }

    /// Send a play command to the extension and await its result (~8s timeout).
    /// Returns Err with a human-readable reason when nothing is connected, the
    /// send fails, or the extension reports failure / times out.
    pub async fn send_command(
        &self,
        action: &str,
        target: &str,
        video_id: &str,
    ) -> Result<(), String> {
        let id = uuid::Uuid::new_v4().to_string();
        let (resp_tx, resp_rx) = oneshot::channel::<CmdResult>();

        let sender = {
            let mut g = self
                .inner
                .lock()
                .map_err(|_| "ext_bridge lock".to_string())?;
            let Some(sender) = g.conn.clone() else {
                return Err("extensão de navegador não conectada".to_string());
            };
            g.pending.insert(id.clone(), resp_tx);
            sender
        };

        let payload = serde_json::json!({
            "type": "command",
            "id": id,
            "action": action,
            "target": target,
            "videoId": video_id,
        })
        .to_string();

        if sender.send(Message::Text(payload.into())).await.is_err() {
            if let Ok(mut g) = self.inner.lock() {
                g.pending.remove(&id);
            }
            return Err("falha ao enviar comando para a extensão".to_string());
        }

        match tokio::time::timeout(Duration::from_secs(8), resp_rx).await {
            Ok(Ok(r)) if r.success => Ok(()),
            Ok(Ok(r)) => Err(if r.error.is_empty() {
                "a extensão não conseguiu tocar".to_string()
            } else {
                r.error
            }),
            Ok(Err(_)) => Err("a conexão com a extensão caiu".to_string()),
            Err(_) => {
                if let Ok(mut g) = self.inner.lock() {
                    g.pending.remove(&id);
                }
                Err("a extensão não respondeu a tempo".to_string())
            }
        }
    }

    fn set_connection(&self, sender: mpsc::Sender<Message>) -> u64 {
        let mut g = match self.inner.lock() {
            Ok(g) => g,
            Err(_) => return 0,
        };
        g.conn_id += 1;
        g.conn = Some(sender);
        g.tabs.clear();
        g.conn_id
    }

    /// Tear down `conn`/`tabs` only if `my_id` is still the current connection.
    fn clear_connection(&self, my_id: u64) {
        if let Ok(mut g) = self.inner.lock() {
            if g.conn_id == my_id {
                g.conn = None;
                g.tabs.clear();
            }
        }
    }

    fn on_message(&self, txt: &str) {
        let parsed: serde_json::Value = match serde_json::from_str(txt) {
            Ok(v) => v,
            Err(_) => return,
        };
        match parsed.get("type").and_then(|v| v.as_str()) {
            Some("tabs") => {
                let tabs: Vec<MusicTab> = parsed
                    .get("tabs")
                    .and_then(|t| serde_json::from_value(t.clone()).ok())
                    .unwrap_or_default();
                if let Ok(mut g) = self.inner.lock() {
                    g.tabs = tabs;
                }
            }
            Some("result") => {
                let id = parsed
                    .get("id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let success = parsed.get("success").and_then(|v| v.as_bool()).unwrap_or(false);
                let error = parsed
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                if let Ok(mut g) = self.inner.lock() {
                    if let Some(tx) = g.pending.remove(&id) {
                        let _ = tx.send(CmdResult { success, error });
                    }
                }
            }
            _ => {}
        }
    }
}

/// Loopback WebSocket server accept loop. Spawn via `tauri::async_runtime::spawn`
/// (the app's tokio runtime) — NOT a bare `tokio::spawn`, which would panic when
/// called from Tauri's `setup` (no runtime entered there). The inner per-connection
/// `tokio::spawn`s are valid because they run inside this already-spawned task.
/// If the port is taken it logs and returns (the bridge stays disconnected).
pub async fn run(bridge: ExtBridge) {
    let addr = format!("127.0.0.1:{}", EXT_BRIDGE_PORT);
    let listener = match TcpListener::bind(&addr).await {
        Ok(l) => {
            info!("[ExtBridge] listening on ws://{}", addr);
            l
        }
        Err(e) => {
            error!("[ExtBridge] failed to bind {}: {}", addr, e);
            return;
        }
    };
    loop {
        match listener.accept().await {
            Ok((stream, _peer)) => {
                let bridge = bridge.clone();
                tokio::spawn(async move {
                    if let Err(e) = handle_conn(stream, bridge).await {
                        warn!("[ExtBridge] connection ended: {}", e);
                    }
                });
            }
            Err(e) => warn!("[ExtBridge] accept error: {}", e),
        }
    }
}

async fn handle_conn(stream: TcpStream, bridge: ExtBridge) -> Result<(), String> {
    let ws = tokio_tungstenite::accept_async(stream)
        .await
        .map_err(|e| e.to_string())?;
    info!("[ExtBridge] extension connected");
    let (mut write, mut read) = ws.split();

    // Writer task: forward queued messages to the socket.
    let (out_tx, mut out_rx) = mpsc::channel::<Message>(32);
    let my_id = bridge.set_connection(out_tx);
    let writer = tokio::spawn(async move {
        while let Some(msg) = out_rx.recv().await {
            if write.send(msg).await.is_err() {
                break;
            }
        }
    });

    // Read loop: tab snapshots + command results.
    while let Some(msg) = read.next().await {
        match msg {
            Ok(Message::Text(txt)) => bridge.on_message(txt.as_str()),
            Ok(Message::Close(_)) => break,
            Ok(_) => {}
            Err(e) => {
                warn!("[ExtBridge] read error: {}", e);
                break;
            }
        }
    }

    bridge.clear_connection(my_id);
    writer.abort();
    info!("[ExtBridge] extension disconnected");
    Ok(())
}
