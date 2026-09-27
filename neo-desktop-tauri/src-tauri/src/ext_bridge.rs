// Local WebSocket bridge between the desktop app and the slim browser extension
// (neo-music-ext). The extension connects to ws://127.0.0.1:17872 and:
//   • reports a snapshot of open music tabs (YouTube/Spotify) → fed to neo-core as
//     `metadata["media_context"]["browser_tabs"]` so the music skill can reuse a tab;
//   • executes `media_command` play requests (focus tab + navigate to the videoId)
//     and replies with a correlated result.
//
// Single extension connection at a time (last wins). Loopback-only bind — no LAN
// exposure. See neo-music-feature (Etapa 3).
//
// **Credencial (2026-09-26).** O handshake exige `?token=` (o arquivo
// `ext-token`, ver `crate::ext_bridge_token`) e recusa `Origin` de página web.
// Antes disto a ponte aceitava qualquer conexão: loopback não é fronteira de
// confiança, porque (a) qualquer processo da máquina alcança, e (b) uma PÁGINA
// abre WebSocket para 127.0.0.1 — o navegador manda `Origin`, e quem não olha não
// se defende. Como só uma conexão vive por vez, uma conexão hostil DESLOCAVA a
// extensão real. Ver neo-core/docs/api-auth-plan.md §11.

use futures_util::{SinkExt, StreamExt};
use log::{error, info, warn};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tokio_tungstenite::tungstenite::http::StatusCode;
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
    /// Aba em que a extensão REALMENTE executou (ela pode ter aberto uma nova).
    /// É a única fonte desse id: aqui ninguém mais sabe onde a música caiu.
    tab_id: Option<i64>,
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
    /// Só para o DIÁLOGO NATIVO de pareamento (`pair_request`). A ponte não usa
    /// o app para nada além disso — e o diálogo é justamente o que não pode ser
    /// uma página web: um endpoint local que emite credencial é alcançável por
    /// qualquer aba, então a aprovação tem que acontecer FORA do navegador.
    app: Arc<Mutex<Option<tauri::AppHandle>>>,
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
            app: Arc::new(Mutex::new(None)),
        }
    }

    /// Guarda o handle do app (chamado uma vez, no `setup`).
    pub fn set_app(&self, app: tauri::AppHandle) {
        if let Ok(mut g) = self.app.lock() {
            *g = Some(app);
        }
    }

    fn app_handle(&self) -> Option<tauri::AppHandle> {
        self.app.lock().ok().and_then(|g| g.clone())
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
    /// `nova_aba` = o core decidiu NÃO reusar nada (não havia aba sem dono):
    /// a extensão abre a sua em vez de tomar a primeira que achar.
    pub async fn send_command(
        &self,
        action: &str,
        target: &str,
        video_id: &str,
        tab_id: Option<i64>,
        nova_aba: bool,
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
            "novaAba": nova_aba,
        })
        .to_string();

        if sender.send(Message::Text(payload.into())).await.is_err() {
            if let Ok(mut g) = self.inner.lock() {
                g.pending.remove(&id);
            }
            return Err("falha ao enviar comando para a extensão".to_string());
        }

        match tokio::time::timeout(Duration::from_secs(8), resp_rx).await {
            Ok(Ok(r)) if r.success => {
                // A extensão diz em que aba caiu. Guardar isso é o que mantém a
                // fila na MESMA guia: a vigia passa a olhar essa aba e o id
                // viaja no próximo evento até a inscrição no neo-core.
                if let Some(id) = r.tab_id {
                    self.note_watch_tab(id, video_id);
                }
                Ok(())
            }
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

    /// A aba em que a fila está tocando DE VERDADE, dita por quem a viu.
    ///
    /// Três informantes, todos a jusante da decisão do servidor: o `result` do
    /// play (a extensão pode ter aberto aba nova), o `media_event` do vigia (ele
    /// mora na página) e o snapshot de abas. Sem isto o alvo da vigia continuava
    /// sendo o que o neo-core adivinhou ANTES de a fila começar, o evento saía
    /// com `tab_id: null` e a inscrição nunca aprendia a aba — então a faixa
    /// seguinte era despachada para a guia velha (medido em 12/09, 15:39:58:
    /// `watch armado: aba None` e, no avanço, `aba Some(1976602762)`).
    fn note_watch_tab(&self, tab_id: i64, for_video: &str) {
        if tab_id <= 0 {
            return;
        }
        if let Ok(mut g) = self.inner.lock() {
            let Some(w) = g.watch.as_mut() else { return };
            // Evento de outra faixa quando já sabemos a aba: não mexe (é o
            // "trocaram o vídeo na minha aba", que o servidor trata pausando).
            if w.tab_id.is_some() && !for_video.is_empty() && w.video_id != for_video {
                return;
            }
            if w.tab_id != Some(tab_id) {
                info!(
                    "[ExtBridge] aba da fila: {} (era {:?}, faixa {})",
                    tab_id, w.tab_id, w.video_id
                );
                w.tab_id = Some(tab_id);
            }
        }
    }

    /// Vigia sem aba (o play caiu numa guia que o servidor não escolheu): o
    /// snapshot diz qual delas está com a nossa faixa. Terceiro informante, atrás
    /// do `result` do play e do `media_event` — e o único que funciona quando o
    /// worker da extensão foi reciclado no meio da faixa.
    fn learn_watch_tab_from_snapshot(&self) {
        let Ok(mut g) = self.inner.lock() else { return };
        let faixa = match g.watch.as_ref() {
            Some(w) if w.tab_id.is_none() && !w.video_id.is_empty() => w.video_id.clone(),
            _ => return,
        };
        let achada = g
            .tabs
            .iter()
            .find(|t| t.extra.get("videoId").and_then(|v| v.as_str()) == Some(faixa.as_str()))
            .map(|t| t.tab_id)
            .filter(|id| *id > 0);
        if let (Some(id), Some(w)) = (achada, g.watch.as_mut()) {
            info!("[ExtBridge] aba da fila: {} (pelo snapshot, faixa {})", id, faixa);
            w.tab_id = Some(id);
        }
    }

    /// Aba da vigia armada (None quando não há vigia). Usado nos testes e no log.
    pub fn watch_tab_id(&self) -> Option<i64> {
        self.inner.lock().ok().and_then(|g| g.watch.as_ref().and_then(|w| w.tab_id))
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

    /// Diálogo nativo → código de pareamento → devolve para a extensão.
    async fn atender_pair_request(&self, navegador: String) {
        let Some(app) = self.app_handle() else {
            warn!("[ExtBridge] pair_request sem app handle — ignorando");
            self.push_message(serde_json::json!({
                "type": "pair_denied",
                "reason": "o Neo Desktop não está pronto",
            }));
            return;
        };

        if !confirmar_pareamento(&app, &navegador).await {
            info!("[ExtBridge] pareamento de {} recusado por você", navegador);
            self.push_message(serde_json::json!({
                "type": "pair_denied",
                "reason": "você não autorizou",
            }));
            return;
        }

        let Some(token) = crate::device_token() else {
            self.push_message(serde_json::json!({
                "type": "pair_denied",
                "reason": "este Mac não está pareado com o Neo — pareie o desktop primeiro",
            }));
            return;
        };

        let url = format!("{}/api/v1/devices/extension-code", neo_core_http_url());
        let client = match reqwest::Client::builder()
            .timeout(Duration::from_secs(20))
            .build()
        {
            Ok(c) => c,
            Err(e) => {
                self.push_message(serde_json::json!({
                    "type": "pair_denied",
                    "reason": format!("cliente HTTP indisponível: {}", e),
                }));
                return;
            }
        };

        let resposta = client
            .post(&url)
            .query(&[("token", token.as_str())])
            .send()
            .await;
        match resposta {
            Ok(r) if r.status().is_success() => {
                let corpo: serde_json::Value = r.json().await.unwrap_or(serde_json::Value::Null);
                info!("[ExtBridge] código de pareamento emitido para {}", navegador);
                self.push_message(serde_json::json!({
                    "type": "pair_code",
                    "code": corpo.get("code").and_then(|v| v.as_str()).unwrap_or(""),
                    "expires_in_s": corpo.get("expires_in_s").and_then(|v| v.as_i64()).unwrap_or(0),
                    "core_url": neo_core_http_url(),
                    "sugestao_nome": navegador,
                }));
            }
            Ok(r) => {
                let status = r.status();
                warn!("[ExtBridge] core recusou o código: {}", status);
                self.push_message(serde_json::json!({
                    "type": "pair_denied",
                    "reason": format!("o neo-core respondeu {}", status),
                }));
            }
            Err(e) => {
                warn!("[ExtBridge] não alcancei o core: {}", e);
                self.push_message(serde_json::json!({
                    "type": "pair_denied",
                    "reason": format!("não alcancei o neo-core: {}", e),
                }));
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
                self.learn_watch_tab_from_snapshot();
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
            // Pareamento de UM CLIQUE (neo-core/docs/browser-surface-plan.md §4).
            //
            // A extensão pede; o DESKTOP decide, num diálogo nativo; e o que ele
            // emite é um código de pareamento estampado com a própria máquina —
            // `POST /devices/extension-code`, autenticado pelo token do aparelho.
            // A extensão não pode se declarar "do macbook": se pudesse, o vínculo
            // não valeria nada. Quem sabe qual máquina é, e pode provar, é este
            // processo.
            //
            // Sem listener HTTP novo: a ponte já existe e já é autenticada, então
            // o fluxo de consentimento não acrescenta superfície de ataque — que
            // era a objeção ao redirect estilo OAuth.
            Some("pair_request") => {
                let navegador = parsed
                    .get("browser")
                    .and_then(|v| v.as_str())
                    .unwrap_or("navegador")
                    .to_string();
                let bridge = self.clone();
                tokio::spawn(async move {
                    bridge.atender_pair_request(navegador).await;
                });
            }
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
                // O vigia mora na PÁGINA: o `tabId` que ele manda é a aba onde a
                // música está tocando de verdade. Aprender aqui é o que faz o
                // POST seguinte levar a aba certa ao neo-core.
                if let Some(id) = parsed.get("tabId").and_then(|v| v.as_i64()) {
                    self.note_watch_tab(id, &video_id);
                }
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
                let tab_id = parsed.get("tabId").and_then(|v| v.as_i64());
                if let Ok(mut g) = self.inner.lock() {
                    if let Some(tx) = g.pending.remove(&id) {
                        let _ = tx.send(CmdResult { success, error, tab_id });
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
    // Credencial do aparelho: esta rota dispara AÇÃO (trocar a faixa), e passou a
    // exigir credencial no core (neo-core/docs/api-auth-plan.md).
    let response = match crate::with_device_token(client.post(&url))
        .json(&body)
        .send()
        .await
    {
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
            // `nova_aba: false` — no avanço a fila NUNCA abre guia: ou é a aba
            // dela (a extensão sabe qual, mesmo sem pin), ou nada.
            if let Err(e) = bridge
                .send_command("play", "youtube", &next_video, tab_id, false)
                .await
            {
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
    // Resolvido UMA vez, no start: se falhar, a ponte não sobe. Subir sem token
    // seria subir sem credencial — exatamente o que esta mudança fecha.
    let token = match crate::ext_bridge_token() {
        Ok(t) => Arc::new(t),
        Err(e) => {
            error!("[ExtBridge] sem credencial, ponte NÃO vai subir: {}", e);
            return;
        }
    };

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
                let token = token.clone();
                tokio::spawn(async move {
                    if let Err(e) = handle_conn(stream, bridge, token).await {
                        warn!("[ExtBridge] connection ended: {}", e);
                    }
                });
            }
            Err(e) => warn!("[ExtBridge] accept error: {}", e),
        }
    }
}

/// Comparação em tempo constante. Sem isto, o tempo de resposta vaza o prefixo
/// certo do token — e adivinhar 64 hex byte a byte é viável, enquanto adivinhar
/// o token inteiro de uma vez não é.
fn tokens_iguais(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for i in 0..a.len() {
        diff |= a[i] ^ b[i];
    }
    diff == 0
}

/// `Origin` aceitável: só extensão de navegador.
///
/// Uma página (`https://…`) que abra WebSocket para cá manda o `Origin` dela. Um
/// service worker de extensão manda `chrome-extension://…` (ou nada, em alguns
/// navegadores) — então **ausência de `Origin` passa** e `http(s)://` **nunca**
/// passa. É o inverso do que a intuição sugere, e é o que torna a regra útil sem
/// depender da versão do navegador.
fn origin_aceitavel(origin: Option<&str>) -> bool {
    match origin {
        None => true,
        Some(o) => {
            let o = o.trim().to_ascii_lowercase();
            o.starts_with("chrome-extension://")
                || o.starts_with("moz-extension://")
                || o.starts_with("safari-web-extension://")
                || o == "null"
        }
    }
}

/// Extrai `token` da query da URI do handshake.
fn token_da_uri(uri: &str) -> Option<String> {
    let query = uri.split_once('?')?.1;
    for par in query.split('&') {
        if let Some((chave, valor)) = par.split_once('=') {
            if chave == "token" {
                return Some(valor.to_string());
            }
        }
    }
    None
}

/// Recusa ANTES de completar o handshake.
///
/// Tem que ser antes: aceitar e fechar depois vira 101 seguido de close, que o
/// cliente lê como queda de rede e responde com reconexão infinita — a mesma
/// lição dos canais WS do core (`neo_device_auth_required`). Devolvendo
/// `ErrorResponse`, o tungstenite responde HTTP 401/403 e o handshake nunca sobe.
fn validar_handshake(req: &Request, resp: Response, esperado: &str) -> Result<Response, ErrorResponse> {
    let origin = req
        .headers()
        .get("origin")
        .and_then(|v| v.to_str().ok());

    if !origin_aceitavel(origin) {
        warn!("[ExtBridge] handshake recusado: Origin {:?} não é extensão", origin);
        return Err(ErrorResponse::new(Some(
            "esta ponte aceita apenas extensão de navegador".to_string(),
        )));
    }

    let apresentado = token_da_uri(&req.uri().to_string());
    match apresentado {
        Some(tok) if tokens_iguais(&tok, esperado) => Ok(resp),
        Some(_) => {
            // Nunca logar o token apresentado: log vira arquivo, e arquivo vaza.
            warn!("[ExtBridge] handshake recusado: token inválido");
            let mut erro = ErrorResponse::new(Some("token inválido".to_string()));
            *erro.status_mut() = StatusCode::UNAUTHORIZED;
            Err(erro)
        }
        None => {
            warn!("[ExtBridge] handshake recusado: sem token (extensão desatualizada?)");
            let mut erro = ErrorResponse::new(Some(
                "falta ?token= — copie o token na aba Dispositivos do Neo".to_string(),
            ));
            *erro.status_mut() = StatusCode::UNAUTHORIZED;
            Err(erro)
        }
    }
}

async fn handle_conn(
    stream: TcpStream,
    bridge: ExtBridge,
    token_esperado: Arc<String>,
) -> Result<(), String> {
    let ws = tokio_tungstenite::accept_hdr_async(stream, |req: &Request, resp: Response| {
        validar_handshake(req, resp, token_esperado.as_str())
    })
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

    // A FILA NÃO PODE TROCAR DE GUIA. O alvo que o neo-core manda ao armar a
    // vigia é um chute feito antes de a fila começar (às vezes `None`, como no
    // log de 12/09 15:39:38). Quem sabe a aba de verdade é a extensão, e ela diz
    // — no `result` do play, no `media_event` do vigia e no snapshot. Se a ponte
    // joga isso fora, o POST sai com `tab_id: null`, a inscrição nunca aprende e
    // a faixa seguinte é despachada para a guia velha.
    #[test]
    fn aba_da_fila_aprendida_pelo_snapshot() {
        let bridge = ExtBridge::new();
        bridge.arm_watch("sub-1", "mac-daniel", None, "AAAAAAAAAAA", "ab12");
        assert_eq!(bridge.watch_tab_id(), None, "o core não sabia a aba");

        // `duration: 0` de propósito: assim o snapshot não vira evento (e o
        // teste não precisa de runtime tokio) — só o aprendizado da aba importa.
        bridge.on_message(
            r#"{"type":"tabs","tabs":[
                {"site":"youtube","tabId":9,"url":"u","title":"outra","playing":false,
                 "videoId":"ZZZZZZZZZZZ","duration":0},
                {"site":"youtube","tabId":77,"url":"u","title":"a da fila","playing":true,
                 "videoId":"AAAAAAAAAAA","duration":0}
            ]}"#,
        );
        assert_eq!(bridge.watch_tab_id(), Some(77), "aprendeu a aba da faixa");
    }

    #[test]
    fn result_do_play_carrega_a_aba_usada() {
        let bridge = ExtBridge::new();
        let (tx, rx) = oneshot::channel::<CmdResult>();
        bridge
            .inner
            .lock()
            .unwrap()
            .pending
            .insert("cmd-1".to_string(), tx);
        bridge.on_message(r#"{"type":"result","id":"cmd-1","success":true,"tabId":123}"#);
        let r = rx.blocking_recv().expect("resultado");
        assert!(r.success);
        assert_eq!(r.tab_id, Some(123), "a aba em que o play caiu volta no result");
    }

    // Vídeo trocado na aba (o usuário assumiu o player) NÃO é aba nova: o
    // servidor trata pausando a fila, e mexer no alvo aqui só confundiria.
    #[test]
    fn evento_de_outra_faixa_nao_move_a_aba_conhecida() {
        let bridge = ExtBridge::new();
        bridge.arm_watch("sub-1", "mac-daniel", Some(77), "AAAAAAAAAAA", "ab12");
        bridge.note_watch_tab(88, "OUTROVIDEO1");
        assert_eq!(bridge.watch_tab_id(), Some(77));
        // Já a mesma faixa reportada de outra aba é a verdade nova (a fila abriu
        // a aba dela): aí seguimos o player.
        bridge.note_watch_tab(88, "AAAAAAAAAAA");
        assert_eq!(bridge.watch_tab_id(), Some(88));
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

#[cfg(test)]
mod handshake_auth_tests {
    use super::*;

    // ── Origin ───────────────────────────────────────────────────────────────
    //
    // A regra é contraintuitiva de propósito: AUSÊNCIA de Origin passa, e
    // `http(s)://` nunca passa. Um service worker de extensão pode não mandar
    // Origin; uma página SEMPRE manda o dela. Inverter isso deixaria a ponte
    // dependente da versão do navegador.

    #[test]
    fn extensao_passa() {
        assert!(origin_aceitavel(Some("chrome-extension://abcdefghijklmnop")));
        assert!(origin_aceitavel(Some("moz-extension://abc")));
        assert!(origin_aceitavel(Some("safari-web-extension://abc")));
    }

    #[test]
    fn sem_origin_passa() {
        assert!(origin_aceitavel(None));
        assert!(origin_aceitavel(Some("null")));
    }

    #[test]
    fn pagina_web_nao_passa() {
        // O vetor que motivou tudo: uma página abre WebSocket para 127.0.0.1.
        assert!(!origin_aceitavel(Some("https://evil.example")));
        assert!(!origin_aceitavel(Some("http://localhost:3000")));
        assert!(!origin_aceitavel(Some("https://www.youtube.com")));
    }

    #[test]
    fn origin_nao_escapa_por_caixa_alta() {
        assert!(!origin_aceitavel(Some("HTTPS://EVIL.EXAMPLE")));
        assert!(origin_aceitavel(Some("CHROME-EXTENSION://abc")));
    }

    // ── token na URI ─────────────────────────────────────────────────────────

    #[test]
    fn le_o_token_da_query() {
        assert_eq!(token_da_uri("/?token=abc123").as_deref(), Some("abc123"));
        assert_eq!(token_da_uri("/ws?x=1&token=abc").as_deref(), Some("abc"));
        assert_eq!(token_da_uri("/?token=abc&y=2").as_deref(), Some("abc"));
    }

    #[test]
    fn sem_query_nao_inventa_token() {
        assert!(token_da_uri("/").is_none());
        assert!(token_da_uri("/?outro=abc").is_none());
        // Não casar por prefixo: `mytoken` não é `token`.
        assert!(token_da_uri("/?mytoken=abc").is_none());
    }

    // ── comparação ───────────────────────────────────────────────────────────

    #[test]
    fn compara_tokens() {
        assert!(tokens_iguais("abc", "abc"));
        assert!(!tokens_iguais("abc", "abd"));
        assert!(!tokens_iguais("abc", "ab"));
        assert!(!tokens_iguais("", "x"));
        assert!(tokens_iguais("", ""));
    }

    #[test]
    fn prefixo_certo_nao_basta() {
        // O modo de falha que a comparação em tempo constante existe para não
        // vazar: adivinhar byte a byte.
        let real = "a".repeat(64);
        assert!(!tokens_iguais(&"a".repeat(63), &real));
        assert!(!tokens_iguais(&format!("{}b", "a".repeat(63)), &real));
        assert!(tokens_iguais(&real, &real));
    }
}

/// Diálogo nativo de autorização do pareamento da extensão.
///
/// Nativo, e não uma página: o consentimento é a única coisa que separa
/// "a extensão pediu" de "qualquer aba pediu". Molde igual ao
/// `confirm_power_action` do `websocket.rs` — `blocking_show` numa thread
/// separada, porque na principal ele trava.
async fn confirmar_pareamento(app: &tauri::AppHandle, navegador: &str) -> bool {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

    let corpo = format!(
        "Autorizar o {} a agir como navegador do Neo nesta máquina?\n\nEle vai poder          contar ao Neo quais abas estão abertas e receber comandos de navegação.",
        navegador
    );
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .message(corpo)
            .title("Parear navegador com o Neo")
            .buttons(MessageDialogButtons::OkCancelCustom(
                "Autorizar".to_string(),
                "Não autorizar".to_string(),
            ))
            .blocking_show()
    })
    .await
    .unwrap_or(false)
}
