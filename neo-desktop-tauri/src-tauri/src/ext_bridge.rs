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

/// An open music tab reported by the extension. Serialized into `media_context`.
///
/// Só os campos que ALGUÉM aqui precisa entender ficam nomeados; todo o resto do
/// que a extensão reporta (estado do player, duração, som na guia, música ou
/// vídeo, tipo de página…) viaja em `extra` e chega ao neo-core intacto. Esta
/// ponte não lê nenhum deles: ela recebe JSON e reemite JSON.
///
/// O `flatten` é deliberado. Com campos tipados, todo sinal novo aprendido pela
/// extensão exigiria mexer no Rust — e mexer no Rust aqui custa reiniciar o
/// `tauri dev` do usuário (não há build de release neste projeto). Assim a
/// extensão evolui sozinha: o que ela mandar, o cérebro recebe.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct MusicTab {
    pub site: String, // "youtube" | "spotify"
    #[serde(rename = "tabId", default)]
    pub tab_id: i64,
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub title: String,
    /// "a aba está tocando". A extensão refina com o estado real do player
    /// (`paused`) e usa o som da guia só como piso — vídeo tocando no mudo não
    /// é audível e mesmo assim está tocando.
    #[serde(default)]
    pub playing: bool,
    #[serde(flatten, default)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

struct CmdResult {
    success: bool,
    error: String,
}

/// Uma vigia armada: "esta aba está tocando ESTA faixa por conta de uma fila".
///
/// Chega junto com a atuação de música (`observar` nos argumentos), quando — e só
/// quando — o neo-core tem uma inscrição armada para este aparelho. Sem watch a
/// ponte não fala com o neo-core fora do turno: um "toca um AC/DC" continua sendo
/// exatamente o que era. Ver docs/playlists-skill-plan.md §7.4.
#[derive(Clone, Debug)]
pub struct Watch {
    pub subscription_id: String,
    pub device_id: String,
    pub tab_id: Option<i64>,
    pub video_id: String,
    /// Código único DESTE despacho (a linha da fila que está tocando). Volta em
    /// todo evento para o neo-core marcar o status na linha certa, mesmo que a
    /// fila tenha sido mexida no meio do caminho.
    pub codigo: String,
    /// A extensão já mandou evento explícito para esta vigia? Depois do primeiro,
    /// paramos de inferir pelo snapshot: quem manda no fim da faixa é ela, que vê
    /// o `<video>` de verdade. Evita os dois caminhos disputando o mesmo evento.
    pub ext_driven: bool,
    /// Já vimos a NOSSA faixa nessa aba? Enquanto não, um vídeo diferente ali
    /// quer dizer "a navegação ainda não chegou" — e não "o usuário trocou".
    /// Sem isto a vigia se auto-sabota: ela nasce no mesmo instante em que a aba
    /// ainda mostra a música anterior, reporta "faixa inesperada" e a fila pausa
    /// sozinha no primeiro segundo (visto em produção em 2026-09-08).
    pub seen: bool,
    /// Último evento já reportado ("<evento>:<faixa>"). Impede reenviar o mesmo
    /// aviso a cada snapshot (a extensão manda um a cada ~24s e a cada evento de
    /// aba) — sem isso a fila pularia várias faixas de uma vez.
    pub sent: Option<String>,
}

/// Faltando isto (em segundos) para o fim, já pedimos a próxima. O evento certo
/// é "está acabando", não "acabou": dá tempo de trocar antes de o autoplay do
/// YouTube assumir a fila, e não deixa buraco de silêncio entre faixas.
const ENDING_WINDOW_SECS: f64 = 6.0;

#[derive(Default)]
struct Inner {
    /// Latest tab snapshot from the connected extension.
    tabs: Vec<MusicTab>,
    /// Vigia armada pelo neo-core (None = nada a observar, o caso normal).
    watch: Option<Watch>,
    /// Já existe um POST de evento em voo? (evita dois avanços simultâneos)
    reporting: bool,
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
    /// `tab_id` is the tab the core picked when it decided to reuse (from the
    /// snapshot this bridge reported). It is a pin, not an order: the extension
    /// falls back to its own lookup when that tab is gone. `None` = no pin.
    pub async fn send_command(
        &self,
        action: &str,
        target: &str,
        video_id: &str,
        tab_id: Option<i64>,
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
            "tabId": tab_id,
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

    // ------------------------------------------------------------------
    // Vigia de fila (docs/playlists-skill-plan.md §7.5)
    // ------------------------------------------------------------------

    /// Arma a vigia. Chamado quando a atuação de música traz `observar`.
    pub fn arm_watch(
        &self,
        subscription_id: &str,
        device_id: &str,
        tab_id: Option<i64>,
        video_id: &str,
        codigo: &str,
    ) {
        if let Ok(mut g) = self.inner.lock() {
            info!(
                "[ExtBridge] watch armado: aba {:?} faixa {} (inscrição {})",
                tab_id, video_id, subscription_id
            );
            let ext_driven = g.watch.as_ref().map(|w| w.ext_driven).unwrap_or(false);
            g.watch = Some(Watch {
                subscription_id: subscription_id.to_string(),
                device_id: device_id.to_string(),
                tab_id,
                video_id: video_id.to_string(),
                codigo: codigo.to_string(),
                ext_driven,
                seen: false,
                sent: None,
            });
        }
        // A extensão passa a reportar essa aba de segundos em segundos (em vez do
        // alarme de ~24s): é o que faz a troca acontecer ANTES do fim da faixa,
        // sem buraco de silêncio e sem disputa com o autoplay.
        self.push_message(serde_json::json!({
            "type": "watch",
            "tabId": tab_id,
            "videoId": video_id,
            "codigo": codigo,
        }));
    }

    /// Desarma. Chamado quando o neo-core responde `desarmar` ou quando o usuário
    /// manda tocar outra coisa (a inscrição vira problema do servidor, não daqui).
    pub fn disarm_watch(&self) {
        if let Ok(mut g) = self.inner.lock() {
            if g.watch.is_some() {
                info!("[ExtBridge] watch desarmado");
            }
            g.watch = None;
            g.reporting = false;
        }
        self.push_message(serde_json::json!({"type": "unwatch"}));
    }

    /// Manda uma mensagem à extensão sem esperar resposta (o `send_command`
    /// espera um `result` correlacionado; aqui é aviso, não comando).
    fn push_message(&self, value: serde_json::Value) {
        let sender = match self.inner.lock() {
            Ok(g) => g.conn.clone(),
            Err(_) => None,
        };
        if let Some(tx) = sender {
            tokio::spawn(async move {
                let _ = tx.send(Message::Text(value.to_string().into())).await;
            });
        }
    }

    pub fn watch_armed(&self) -> bool {
        self.inner.lock().map(|g| g.watch.is_some()).unwrap_or(false)
    }

    /// Decide se o snapshot atual merece um evento. Marca `reporting` e o `sent`
    /// ANTES de devolver — quem chama já pode disparar o POST sem correr o risco
    /// de mandar dois pelo mesmo motivo.
    fn evaluate_watch(&self) -> Option<(Watch, String, String)> {
        let mut g = self.inner.lock().ok()?;
        if g.reporting {
            return None;
        }
        let watch = g.watch.clone()?;
        if watch.ext_driven {
            // A extensão está reportando os eventos do player. Inferir pelo
            // snapshot aqui só produziria evento duplicado (e um deles atrasado).
            return None;
        }
        // Tudo que precisamos da aba vira valor PRÓPRIO aqui: logo abaixo
        // mexemos em `g.watch`, e um empréstimo de `g.tabs` vivo impediria.
        let (current_video, ended, duration, position, live) = {
            let tab = g.tabs.iter().find(|t| match watch.tab_id {
                Some(id) => t.tab_id == id,
                None => t
                    .extra
                    .get("videoId")
                    .and_then(|v| v.as_str())
                    .map(|v| v == watch.video_id)
                    .unwrap_or(false),
            })?;
            let num = |key: &str| tab.extra.get(key).and_then(|v| v.as_f64());
            let flag = |key: &str| tab.extra.get(key).and_then(|v| v.as_bool());
            (
                tab.extra
                    .get("videoId")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                flag("ended").unwrap_or(false),
                num("duration").unwrap_or(0.0),
                num("currentTime").unwrap_or(0.0),
                flag("live").unwrap_or(false),
            )
        };

        if !current_video.is_empty() && current_video == watch.video_id {
            if let Some(w) = g.watch.as_mut() {
                w.seen = true;
            }
        }
        let seen = g.watch.as_ref().map(|w| w.seen).unwrap_or(false);
        if !seen {
            // A navegação ainda não chegou nesta aba. Calar aqui é o certo:
            // reportar agora seria inventar um fim que não houve.
            return None;
        }

        // A aba trocou de vídeo DEPOIS de a nossa ter aparecido (o usuário
        // navegou, ou o autoplay assumiu). Reportamos a faixa que ESTÁ lá: o
        // neo-core vê que não é a esperada e PAUSA a inscrição em vez de brigar.
        let (event, reported) = if !current_video.is_empty() && current_video != watch.video_id {
            ("faixa_terminou".to_string(), current_video)
        } else if ended {
            ("faixa_terminou".to_string(), watch.video_id.clone())
        } else {
            if live || duration <= 0.0 || duration - position > ENDING_WINDOW_SECS {
                return None;
            }
            ("faixa_terminando".to_string(), watch.video_id.clone())
        };

        let stamp = format!("{}:{}", event, reported);
        if watch.sent.as_deref() == Some(stamp.as_str()) {
            return None;
        }
        if let Some(w) = g.watch.as_mut() {
            w.sent = Some(stamp);
        }
        g.reporting = true;
        Some((watch, event, reported))
    }

    fn end_reporting(&self) {
        if let Ok(mut g) = self.inner.lock() {
            g.reporting = false;
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
                // Sem extensão não há o que observar. A inscrição continua viva
                // no servidor (e expira pelo TTL) — aqui só paramos de vigiar.
                g.watch = None;
                g.reporting = false;
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
                // Só faz alguma coisa se houver watch armado — o filtro na BORDA
                // (§7.4): sem fila rodando, nenhum evento de mídia vira requisição.
                if let Some((watch, event, video_id)) = self.evaluate_watch() {
                    let bridge = self.clone();
                    tokio::spawn(async move {
                        let codigo = watch.codigo.clone();
                        report_media_event(bridge, watch, event, video_id, codigo).await;
                    });
                }
            }
            // A extensão viu o player: começou, está acabando, acabou. É o
            // caminho BOM (ela enxerga o <video>); o snapshot vira só reserva.
            Some("media_event") => {
                let event = parsed
                    .get("event")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let video_id = parsed
                    .get("videoId")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let codigo = parsed
                    .get("codigo")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let watch = match self.inner.lock() {
                    Ok(mut g) => {
                        if let Some(w) = g.watch.as_mut() {
                            w.ext_driven = true;
                        }
                        g.watch.clone()
                    }
                    Err(_) => None,
                };
                // Sem vigia armada o evento é descartado AQUI: é a primeira das
                // três camadas que protegem o pedido avulso de música.
                if let Some(watch) = watch {
                    if !event.is_empty() {
                        let bridge = self.clone();
                        let reported = if video_id.is_empty() {
                            watch.video_id.clone()
                        } else {
                            video_id
                        };
                        let codigo = if codigo.is_empty() {
                            watch.codigo.clone()
                        } else {
                            codigo
                        };
                        tokio::spawn(async move {
                            report_media_event(bridge, watch, event, reported, codigo).await;
                        });
                    }
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

/// URL HTTP do neo-core (mesma resolução usada pelos comandos do app).
fn neo_core_http_url() -> String {
    std::env::var("NEO_CORE_HTTP_URL")
        .or_else(|_| std::env::var("NEO_CORE_API_URL"))
        .ok()
        .or_else(|| {
            std::env::var("NEO_CORE_URL")
                .ok()
                .filter(|v| v.starts_with("http://") || v.starts_with("https://"))
        })
        .unwrap_or_else(|| "http://127.0.0.1:8000".to_string())
        .trim_end_matches('/')
        .to_string()
}

/// Conta ao neo-core que a faixa observada está acabando — e **executa a resposta**.
///
/// A resposta do POST É o comando (docs/playlists-skill-plan.md §7.5): um round
/// trip, sem canal de push novo e sem mudança no `.proto`. Três respostas
/// possíveis: `tocar` (manda a próxima para a extensão e re-arma), `desarmar`
/// (fila acabou / inscrição encerrada) e `nada` (o servidor decidiu ignorar).
async fn report_media_event(
    bridge: ExtBridge,
    watch: Watch,
    event: String,
    video_id: String,
    codigo: String,
) {
    let url = format!("{}/api/v1/eventos/midia", neo_core_http_url());
    let body = serde_json::json!({
        "device_id": watch.device_id,
        "canal": "musica",
        "evento": event,
        "site": "youtube",
        "tab_id": watch.tab_id,
        "video_id": video_id,
        "codigo": codigo,
        "inscricao_id": watch.subscription_id,
    });

    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            warn!("[ExtBridge] cliente HTTP indisponível: {}", e);
            bridge.end_reporting();
            return;
        }
    };
    let response = match client.post(&url).json(&body).send().await {
        Ok(r) => r,
        Err(e) => {
            warn!("[ExtBridge] evento de mídia falhou: {}", e);
            bridge.end_reporting();
            return;
        }
    };
    let payload: serde_json::Value = response.json().await.unwrap_or(serde_json::Value::Null);
    let acao = payload.get("acao").and_then(|v| v.as_str()).unwrap_or("nada");
    info!("[ExtBridge] evento {} → {}", event, acao);

    match acao {
        "tocar" => {
            let next_video = payload
                .get("video_id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let tab_id = payload
                .get("tab_id")
                .and_then(|v| v.as_i64())
                .or(watch.tab_id);
            if next_video.is_empty() {
                bridge.end_reporting();
                return;
            }
            let next_codigo = payload
                .get("codigo")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            // Re-arma ANTES de tocar: o snapshot seguinte chega em milissegundos
            // e não pode encontrar a vigia apontando para a faixa velha.
            bridge.arm_watch(
                &watch.subscription_id,
                &watch.device_id,
                tab_id,
                &next_video,
                &next_codigo,
            );
            if let Err(e) = bridge.send_command("play", "youtube", &next_video, tab_id).await {
                warn!("[ExtBridge] não consegui tocar a próxima da fila: {}", e);
            }
            bridge.end_reporting();
        }
        "abrir_url" | "abrir_uri" => {
            let target = payload
                .get("url")
                .and_then(|v| v.as_str())
                .or_else(|| payload.get("uri").and_then(|v| v.as_str()))
                .unwrap_or("");
            if !target.is_empty() {
                let opened = if cfg!(target_os = "macos") {
                    std::process::Command::new("open").arg(target).spawn()
                } else if cfg!(target_os = "windows") {
                    std::process::Command::new("cmd")
                        .args(["/C", "start", "", target])
                        .spawn()
                } else {
                    std::process::Command::new("xdg-open").arg(target).spawn()
                };
                if let Err(e) = opened {
                    warn!("[ExtBridge] não consegui abrir {}: {}", target, e);
                }
            }
            // Fora do YouTube a ponte não enxerga o fim da faixa: a fila para aqui.
            bridge.disarm_watch();
        }
        "desarmar" => bridge.disarm_watch(),
        _ => bridge.end_reporting(),
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

#[cfg(test)]
mod tests {
    use super::*;

    // O relay tem que ser TRANSPARENTE: o que a extensão aprender a reportar
    // precisa chegar ao neo-core sem passar por aqui. Antes os campos eram
    // tipados um a um e tudo que não estivesse na struct era descartado em
    // silêncio — o sintoma seria a nota do cérebro sem duração, sem play/pause
    // e sem "música ou vídeo", sem erro nenhum apontando para cá.
    #[test]
    fn campos_novos_da_extensao_atravessam_a_ponte() {
        let json = r#"{
            "site": "youtube",
            "tabId": 42,
            "url": "https://www.youtube.com/watch?v=abc",
            "title": "Clipe",
            "playing": true,
            "audible": true,
            "muted": false,
            "kind": "watch",
            "hasMedia": true,
            "paused": false,
            "currentTime": 72.4,
            "duration": 245.0,
            "contentType": "music",
            "track": "No Matter What",
            "artist": "Papa Roach",
            "adShowing": false
        }"#;
        let tab: MusicTab = serde_json::from_str(json).expect("parse");
        assert_eq!(tab.site, "youtube");
        assert_eq!(tab.tab_id, 42);
        assert!(tab.playing);

        let de_volta = serde_json::to_value(&tab).expect("serialize");
        // Nomes preservados como a extensão escreveu (camelCase), porque é
        // assim que o neo-core lê.
        assert_eq!(de_volta["contentType"], "music");
        assert_eq!(de_volta["hasMedia"], true);
        assert_eq!(de_volta["paused"], false);
        assert_eq!(de_volta["duration"], 245.0);
        assert_eq!(de_volta["track"], "No Matter What");
        assert_eq!(de_volta["adShowing"], false);
        assert_eq!(de_volta["tabId"], 42);
    }

    // Extensão velha (só os cinco campos originais) não pode quebrar a ponte —
    // o usuário atualiza o app e a extensão em momentos diferentes.
    #[test]
    fn snapshot_no_formato_antigo_continua_valendo() {
        let json = r#"{"site":"spotify","tabId":7,"url":"u","title":"T","playing":false}"#;
        let tab: MusicTab = serde_json::from_str(json).expect("parse");
        assert_eq!(tab.site, "spotify");
        assert!(tab.extra.is_empty());
        let de_volta = serde_json::to_value(&tab).expect("serialize");
        assert_eq!(de_volta["tabId"], 7);
    }
}
