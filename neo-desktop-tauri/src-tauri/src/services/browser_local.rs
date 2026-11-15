//! Braço do Neo no navegador LOCAL — executa as ações `browser.*` que chegam
//! pelo canal `/ws/devices/control` do neo-core.
//!
//! Quem decide o próximo passo é o neo-core (o cognitivo, na VPS). Este módulo
//! não tem inteligência nenhuma: recebe uma ação do vocabulário fechado,
//! traduz para o backend do open-browser-use e devolve o resultado REAL. Se
//! falhar, reporta a falha — nunca tenta outra coisa por conta própria.
//!
//! Caminho até a aba:
//!
//! ```text
//! neo-core ──▶ websocket.rs ──▶ AQUI ──unix socket JSON-RPC──▶ obu (host nativo)
//!                                                                  │ native messaging
//!                                                                  ▼
//!                                                    extensão neo-browser-local
//!                                                                  │ chrome.debugger (CDP)
//!                                                                  ▼
//!                                                            a aba que o usuário vê
//! ```
//!
//! O socket é descoberto em `/tmp/open-browser-use/active.json`, escrito pelo
//! host quando o navegador o inicia. Sem navegador aberto (ou sem a extensão),
//! o arquivo não existe ou o connect falha — e o core recebe um erro explícito
//! em vez de silêncio.
//!
//! Ver: neo-core/docs/browser-local-plan.md §3.2

use log::{info, warn};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::sync::Mutex;

const SOCKET_RECORD: &str = "/tmp/open-browser-use/active.json";
/// Onde o host nativo cria o socket. O `active.json` mora aqui dentro, e os
/// `*.sock` ao lado dele são o plano B — ver `socket_path`.
const SOCKET_DIR: &str = "/tmp/open-browser-use";
const SESSION_ID: &str = "neo-browser-local";
/// Teto de uma chamada ao backend. O passo inteiro tem timeout do lado do core;
/// este aqui só impede que uma chamada pendurada segure a conexão para sempre.
const CALL_TIMEOUT: Duration = Duration::from_secs(30);

/// PARE apertado na faixa da página. A extensão manda a notificação `neo.stop`
/// pelo socket; guardamos aqui para o próximo `snapshot` responder na hora.
/// (O caminho CONFIÁVEL continua sendo o atributo `data-neo-stop` no HTML — se
/// a notificação se perder, o snapshot vê o atributo mesmo assim.)
static STOP_REQUESTED: AtomicBool = AtomicBool::new(false);

/// `Page.windowOpen` que uma aba emitiu e que ainda ninguém consumiu, por aba.
///
/// É o gatilho barato de "este clique abriu outra aba" (medido no F0 de
/// `neo-core/docs/browser-multi-aba-plan.md` §12: chega 9–31 ms depois do
/// clique, com a URL). Sem ele, nenhum clique paga espera; com ele, vale
/// esperar a aba nascer. Antes o `Conn::request` jogava o evento fora junto
/// com todas as outras notificações que não eram o PARE.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct JanelaPedida {
    pub url: String,
    pub gesto: bool,
}

/// O que o Daniel escreveu no PAINEL da aba (notificação `neo.msg` da extensão),
/// esperando o core ler. O `browser.snapshot` e o `browser.inbox` esvaziam a
/// fila (neo-core/docs/browser-conversa-plan.md §4.3).
///
/// Vem pelo canal da extensão e NUNCA pelo DOM, ao contrário do PARE: um
/// atributo na página pode ser escrito pela própria página, e a mensagem tem a
/// autoridade do Daniel. O pior caso de um PARE forjado é parar; de uma
/// mensagem forjada, obedecer.
static MENSAGENS: OnceLock<std::sync::Mutex<Vec<Value>>> = OnceLock::new();
/// Teto da fila e de cada mensagem. O core lê a cada passo: acumular mais que
/// isto é a tarefa parada, e aí o que importa é o mais recente.
const MENSAGENS_MAX: usize = 20;
const MENSAGEM_CHARS: usize = 2000;

fn mensagens() -> &'static std::sync::Mutex<Vec<Value>> {
    MENSAGENS.get_or_init(|| std::sync::Mutex::new(Vec::new()))
}

/// Guarda uma `neo.msg`: `{tabId, texto, opcao?}`. Texto vazio é ignorado.
pub(crate) fn guardar_mensagem(params: &Value) {
    let texto = params.get("texto").and_then(|v| v.as_str()).unwrap_or("").trim();
    if texto.is_empty() {
        return;
    }
    let texto: String = texto.chars().take(MENSAGEM_CHARS).collect();
    let mut item = json!({ "texto": texto });
    if let Some(tab) = params.get("tabId").and_then(|v| v.as_i64()) {
        item["tab"] = json!(tab);
    }
    // `opcao` só chega quando a extensão conferiu que o botão é da pergunta que o
    // Neo fez (canal privilegiado) — ver `NEO_MSG` no background.js.
    if let Some(opcao) = params.get("opcao").and_then(|v| v.as_u64()) {
        item["opcao"] = json!(opcao);
    }
    if let Ok(mut fila) = mensagens().lock() {
        if fila.len() >= MENSAGENS_MAX {
            fila.remove(0);
        }
        fila.push(item);
    }
}

/// Esvazia a fila: o core leu.
pub(crate) fn tomar_mensagens() -> Vec<Value> {
    mensagens()
        .lock()
        .map(|mut fila| std::mem::take(&mut *fila))
        .unwrap_or_default()
}

/// Por que o navegador soltou o debugger de cada aba (`neo.detach`):
/// `target_closed` é a aba fechada; `canceled_by_user`, a faixa amarela
/// cancelada. Sem isto, "Debugger unattached" não dizia qual dos dois (sessão
/// 2bd028fb: três jobs morreram assim logo depois de digitar um e-mail).
static DESLIGADAS: OnceLock<std::sync::Mutex<HashMap<i64, String>>> = OnceLock::new();

fn desligadas() -> &'static std::sync::Mutex<HashMap<i64, String>> {
    DESLIGADAS.get_or_init(|| std::sync::Mutex::new(HashMap::new()))
}

pub(crate) fn anotar_desligamento(tab: i64, motivo: &str) {
    if let Ok(mut mapa) = desligadas().lock() {
        if mapa.len() > 64 {
            mapa.clear();
        }
        mapa.insert(tab, motivo.to_string());
    }
}

/// O erro do navegador, com o motivo do desligamento quando ele é conhecido.
pub(crate) fn com_motivo_do_desligamento(tab: i64, erro: String) -> String {
    if !erro.contains("Debugger unattached") {
        return erro;
    }
    match desligadas().lock().ok().and_then(|m| m.get(&tab).cloned()) {
        Some(motivo) if motivo == "target_closed" => {
            format!("{erro} (a aba foi fechada)")
        }
        Some(motivo) if motivo == "canceled_by_user" => {
            format!("{erro} (alguém cancelou a depuração na faixa do navegador)")
        }
        Some(motivo) if !motivo.is_empty() => format!("{erro} (motivo: {motivo})"),
        _ => erro,
    }
}

/// Abas com pergunta aberta no painel (`neoPergunta`). A próxima faixa que não
/// for pergunta fecha a caixa de resposta de lá.
static PERGUNTAS: OnceLock<std::sync::Mutex<std::collections::HashSet<i64>>> = OnceLock::new();

fn perguntas() -> &'static std::sync::Mutex<std::collections::HashSet<i64>> {
    PERGUNTAS.get_or_init(|| std::sync::Mutex::new(std::collections::HashSet::new()))
}

static JANELAS: OnceLock<std::sync::Mutex<HashMap<i64, Vec<JanelaPedida>>>> = OnceLock::new();

fn janelas() -> &'static std::sync::Mutex<HashMap<i64, Vec<JanelaPedida>>> {
    JANELAS.get_or_init(|| std::sync::Mutex::new(HashMap::new()))
}

/// Guarda um `Page.windowOpen` da aba `tab` (id do backend: negativo = headless).
pub(crate) fn anotar_janela(tab: i64, url: &str, gesto: bool) {
    if let Ok(mut mapa) = janelas().lock() {
        let fila = mapa.entry(tab).or_default();
        fila.push(JanelaPedida { url: url.to_string(), gesto });
        // Uma página que abre janela em laço não pode crescer isto sem fim.
        if fila.len() > 8 {
            fila.remove(0);
        }
    }
}

fn janelas_de(tab: i64) -> Vec<JanelaPedida> {
    janelas().lock().map(|m| m.get(&tab).cloned().unwrap_or_default()).unwrap_or_default()
}

fn esquecer_janelas(tab: i64) {
    if let Ok(mut mapa) = janelas().lock() {
        mapa.remove(&tab);
    }
}

static CONN: OnceLock<Mutex<Option<Conn>>> = OnceLock::new();

fn conn_slot() -> &'static Mutex<Option<Conn>> {
    CONN.get_or_init(|| Mutex::new(None))
}

struct Conn {
    stream: UnixStream,
    next_id: u64,
    turn_id: String,
}

/// Onde está o socket do host nativo — pelo registro, e, se ele sumiu, pelo
/// próprio diretório.
///
/// O `active.json` era a ÚNICA fonte, e isso quebrava o navegador local sozinho
/// a cada poucos dias. O limpador diário de `/tmp` do macOS apaga arquivo comum
/// sem acesso há 3 dias e **não** apaga o socket: o host segue vivo, o socket
/// segue atendendo, e some só o bilhete que diz onde ele está. Medido em
/// 2026-10-05 nesta máquina — host de pé desde 28/09 respondendo `ping`→`pong` e
/// `getInfo` com a extensão conectada, `active.json` inexistente, e o Neo
/// dizendo ao usuário "a extensão do Neo não está ativa". Dois jobs morreram
/// assim (37a29567, 45fce2e8), e a mensagem ainda mandava arrumar o que não
/// estava quebrado.
///
/// O plano B não substitui o registro: ele é consultado primeiro, porque é o
/// único que diz QUAL socket é o atual quando há mais de um. A varredura só
/// entra quando não há registro nenhum, e aí o socket mais recente é a melhor
/// resposta disponível — quem confirma se ele atende é o `ping` logo em seguida.
fn socket_path() -> Result<PathBuf, String> {
    match std::fs::read_to_string(SOCKET_RECORD) {
        Ok(raw) => {
            let record: Value = serde_json::from_str(&raw)
                .map_err(|e| format!("registro do socket do open-browser-use ilegível: {}", e))?;
            let path = record
                .get("socketPath")
                .and_then(|v| v.as_str())
                .unwrap_or_default();
            if path.is_empty() {
                return Err("registro do socket do open-browser-use sem socketPath".to_string());
            }
            Ok(PathBuf::from(path))
        }
        Err(_) => socket_por_varredura(),
    }
}

/// O `.sock` mais recente em `/tmp/open-browser-use`, quando o registro sumiu.
fn socket_por_varredura() -> Result<PathBuf, String> {
    socket_por_varredura_em(std::path::Path::new(SOCKET_DIR))
}

fn socket_por_varredura_em(dir_base: &std::path::Path) -> Result<PathBuf, String> {
    let ausente = || {
        "o navegador local não está com a extensão do Neo ativa (host do open-browser-use \
         não está no ar)"
            .to_string()
    };
    let dir = std::fs::read_dir(dir_base).map_err(|_| ausente())?;
    let mut melhor: Option<(std::time::SystemTime, PathBuf)> = None;
    for entrada in dir.flatten() {
        let caminho = entrada.path();
        if caminho.extension().and_then(|e| e.to_str()) != Some("sock") {
            continue;
        }
        // É socket mesmo? Um arquivo comum chamado `.sock` não serve, e
        // descobrir isso no `connect` custaria o erro errado.
        let meta = match entrada.metadata() {
            Ok(m) => m,
            Err(_) => continue,
        };
        #[cfg(unix)]
        {
            use std::os::unix::fs::FileTypeExt;
            if !meta.file_type().is_socket() {
                continue;
            }
        }
        let quando = meta.modified().unwrap_or(std::time::UNIX_EPOCH);
        if melhor.as_ref().map_or(true, |(t, _)| quando > *t) {
            melhor = Some((quando, caminho));
        }
    }
    match melhor {
        Some((_, caminho)) => {
            info!(
                "[browser_local] active.json sumiu (limpeza do /tmp); usando o socket {}",
                caminho.display()
            );
            Ok(caminho)
        }
        None => Err(ausente()),
    }
}

impl Conn {
    async fn open() -> Result<Self, String> {
        let path = socket_path()?;
        let stream = UnixStream::connect(&path).await.map_err(|e| {
            format!(
                "não consegui falar com o navegador local ({}): {}",
                path.display(),
                e
            )
        })?;
        Ok(Conn {
            stream,
            next_id: 1,
            turn_id: format!("turn-{}", uuid::Uuid::new_v4()),
        })
    }

    async fn write_frame(&mut self, value: &Value) -> Result<(), String> {
        let payload = serde_json::to_vec(value).map_err(|e| e.to_string())?;
        // Mesmo enquadramento do SDK: u32 no endianness NATIVO + JSON.
        let len = payload.len() as u32;
        self.stream
            .write_all(&len.to_ne_bytes())
            .await
            .map_err(|e| format!("escrita no socket falhou: {}", e))?;
        self.stream
            .write_all(&payload)
            .await
            .map_err(|e| format!("escrita no socket falhou: {}", e))?;
        Ok(())
    }

    async fn read_frame(&mut self) -> Result<Value, String> {
        let mut header = [0u8; 4];
        self.stream
            .read_exact(&mut header)
            .await
            .map_err(|e| format!("socket do navegador fechou: {}", e))?;
        let len = u32::from_ne_bytes(header) as usize;
        // Um snapshot grande passa fácil de 100KB; 8MB é teto de sanidade.
        if len > 8 * 1024 * 1024 {
            return Err(format!("frame absurdo do navegador: {} bytes", len));
        }
        let mut payload = vec![0u8; len];
        self.stream
            .read_exact(&mut payload)
            .await
            .map_err(|e| format!("socket do navegador fechou: {}", e))?;
        serde_json::from_slice(&payload).map_err(|e| format!("resposta ilegível: {}", e))
    }

    /// Uma chamada JSON-RPC. Notificações que chegarem no meio são consumidas
    /// (e `neo.stop` vira o flag do PARE) até a resposta com o nosso id.
    async fn request(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id += 1;

        let mut merged = json!({ "session_id": SESSION_ID, "turn_id": self.turn_id });
        if let (Some(dst), Some(src)) = (merged.as_object_mut(), params.as_object()) {
            for (k, v) in src {
                dst.insert(k.clone(), v.clone());
            }
        }
        let request = json!({ "jsonrpc": "2.0", "id": id, "method": method, "params": merged });
        self.write_frame(&request).await?;

        let deadline = Instant::now() + CALL_TIMEOUT;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(format!("o navegador não respondeu a '{}' a tempo", method));
            }
            let frame = match tokio::time::timeout(remaining, self.read_frame()).await {
                Ok(result) => result?,
                Err(_) => return Err(format!("o navegador não respondeu a '{}' a tempo", method)),
            };

            // Notificação (sem id): o PARE, e o `Page.windowOpen` de uma aba
            // (é o que diz que um clique abriu outra aba).
            if frame.get("id").is_none() {
                match frame.get("method").and_then(|v| v.as_str()) {
                    Some("neo.stop") => {
                        info!("[BrowserLocal] PARE recebido da faixa da página");
                        STOP_REQUESTED.store(true, Ordering::SeqCst);
                    }
                    // O Daniel escreveu no painel da aba. Não loga o texto: é dele.
                    Some("neo.msg") => {
                        let p = frame.get("params").cloned().unwrap_or(Value::Null);
                        let tamanho = p
                            .get("texto")
                            .and_then(|v| v.as_str())
                            .map(|t| t.chars().count())
                            .unwrap_or(0);
                        info!("[BrowserLocal] mensagem do painel da aba ({} caracteres)", tamanho);
                        guardar_mensagem(&p);
                    }
                    Some("neo.detach") => {
                        let p = frame.get("params").cloned().unwrap_or(Value::Null);
                        if let Some(tab) = p.get("tabId").and_then(|v| v.as_i64()) {
                            let motivo = p.get("reason").and_then(|v| v.as_str()).unwrap_or("");
                            warn!("[BrowserLocal] o navegador soltou a aba {}: {}", tab, motivo);
                            anotar_desligamento(tab, motivo);
                        }
                    }
                    Some("onCDPEvent") => {
                        let p = frame.get("params").cloned().unwrap_or(Value::Null);
                        if p.get("method").and_then(|v| v.as_str()) == Some("Page.windowOpen") {
                            if let Some(tab) = p.pointer("/source/tabId").and_then(|v| v.as_i64()) {
                                let ev = p.get("params").cloned().unwrap_or(Value::Null);
                                anotar_janela(
                                    tab,
                                    ev.get("url").and_then(|v| v.as_str()).unwrap_or(""),
                                    ev.get("userGesture").and_then(|v| v.as_bool()).unwrap_or(false),
                                );
                            }
                        }
                    }
                    _ => {}
                }
                continue;
            }
            if frame.get("id").and_then(|v| v.as_u64()) != Some(id) {
                continue; // resposta de outra chamada (não deveria acontecer)
            }
            if let Some(error) = frame.get("error") {
                let message = error
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("erro no navegador");
                return Err(message.to_string());
            }
            return Ok(frame.get("result").cloned().unwrap_or(Value::Null));
        }
    }
}

/// Chamada com reconexão: se o socket morreu (navegador fechou e reabriu),
/// tenta UMA vez de novo com conexão nova antes de desistir.
async fn call(method: &str, params: Value) -> Result<Value, String> {
    let slot = conn_slot();
    let mut guard = slot.lock().await;

    for attempt in 0..2 {
        if guard.is_none() {
            *guard = Some(Conn::open().await?);
        }
        let conn = guard.as_mut().expect("conexão recém-aberta");
        match conn.request(method, params.clone()).await {
            Ok(value) => return Ok(value),
            Err(e) => {
                // Erro de transporte derruba a conexão; erro de aplicação não.
                let transport = e.contains("socket") || e.contains("fechou");
                if transport {
                    *guard = None;
                    if attempt == 0 {
                        warn!("[BrowserLocal] reconectando ao navegador: {}", e);
                        continue;
                    }
                }
                return Err(e);
            }
        }
    }
    Err("não consegui falar com o navegador local".to_string())
}

// ─── CDP ────────────────────────────────────────────────────────────────────

/// Dois backends falam o MESMO CDP; o que muda é só o caminho até ele.
///
/// - `tab_id > 0` → aba real do navegador do usuário (extensão + chrome.debugger)
/// - `tab_id < 0` → aba do navegador headless local (WebSocket CDP direto)
///
/// O sinal carrega o backend de propósito: `tab_id` já atravessa as 20 ações e
/// o canal de controle do core. Passar o modo por fora significaria mudar todas
/// as assinaturas e o contrato do `/ws/devices/control` — e o único jeito de
/// errar seria justamente esquecer de propagá-lo em uma delas. Assim é
/// impossível ter um id sem backend.
///
/// Ids do headless nascem positivos lá dentro; quem nega é o `claim` aqui.
fn is_headless(tab_id: i64) -> bool {
    tab_id < 0
}

async fn cdp(tab_id: i64, method: &str, params: Value) -> Result<Value, String> {
    if is_headless(tab_id) {
        return super::headless::cdp(-tab_id, method, params).await;
    }
    call(
        "executeCdp",
        json!({
            "target": { "tabId": tab_id },
            "method": method,
            "commandParams": params,
        }),
    )
    .await
    .map_err(|erro| com_motivo_do_desligamento(tab_id, erro))
}

/// `Runtime.evaluate` com `returnByValue`, devolvendo já o valor JS.
async fn eval(tab_id: i64, expression: &str) -> Result<Value, String> {
    let result = cdp(
        tab_id,
        "Runtime.evaluate",
        json!({ "expression": expression, "returnByValue": true, "awaitPromise": true }),
    )
    .await?;

    if let Some(details) = result.get("exceptionDetails") {
        let text = details
            .get("exception")
            .and_then(|e| e.get("description"))
            .and_then(|v| v.as_str())
            .or_else(|| details.get("text").and_then(|v| v.as_str()))
            .unwrap_or("erro ao avaliar na página");
        return Err(text.to_string());
    }
    Ok(result
        .get("result")
        .and_then(|r| r.get("value"))
        .cloned()
        .unwrap_or(Value::Null))
}

/// Resultado de um `eval` que devolve `{ok, ...}` — traduz `ok:false` em Err,
/// para o core nunca ler "cliquei" sem ter clicado.
fn unwrap_ok(value: Value) -> Result<Value, String> {
    if value.get("ok").and_then(|v| v.as_bool()) == Some(true) {
        return Ok(value);
    }
    let error = value
        .get("error")
        .and_then(|v| v.as_str())
        .unwrap_or("a página recusou a ação");
    Err(error.to_string())
}

// ─── JS injetado ────────────────────────────────────────────────────────────

/// Preâmbulo com os helpers: fica em `window.__neo` e é reinstalado a cada
/// snapshot (uma navegação limpa o mundo principal — e é justamente por isso
/// que um `ref` de antes da navegação vira "stale" em vez de clicar em outra
/// coisa).
const JS_HELPERS: &str = r#"
(() => {
  const NEO = (window.__neo = window.__neo || {});
  NEO.refs = NEO.refs || [];
  NEO.visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') < 0.05) return false;
    return r.bottom > -200 && r.top < innerHeight + 2000;
  };
  // Nome do elemento. A ordem importa e foi calibrada contra páginas reais
  // (github.com): com a versão ingênua (aria-label || innerText || title),
  // 37% dos elementos vinham SEM rótulo — botão de ícone e link que embrulha
  // SVG. Um elemento sem nome é ruído: o modelo não tem como escolher "clica
  // no [4]" se [4] não tem nome, e clica errado.
  NEO.label = (el) => {
    const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
    const fromIds = (ids) =>
      clean(
        ids
          .split(/\s+/)
          .map((id) => {
            const node = document.getElementById(id);
            return node ? node.innerText || node.textContent || '' : '';
          })
          .join(' ')
      );
    let out = clean(el.getAttribute('aria-label'));
    if (!out && el.getAttribute('aria-labelledby')) out = fromIds(el.getAttribute('aria-labelledby'));
    if (!out) out = clean(el.getAttribute('placeholder'));
    if (!out && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) out = clean(el.value);
    if (!out) out = clean(el.innerText);
    // Texto de leitor de tela (.sr-only) some do innerText e sobrevive no
    // textContent — mas o textContent também traz o CONTEÚDO de <style> e
    // <script>, que o innerText corretamente ignora por não ser renderizado.
    // Sem esta guarda, um contêiner com um <style> dentro vinha rotulado
    // `.A8SBwf{--client-adapter-margin-base:1` (medido no google.com).
    if (!out && !el.querySelector('style,script,noscript')) out = clean(el.textContent);
    if (!out) {
      const inner = el.querySelector('[aria-label], svg title, img[alt]');
      if (inner) {
        out = clean(
          inner.getAttribute('aria-label') || inner.textContent || inner.getAttribute('alt')
        );
      }
    }
    if (!out) out = clean(el.getAttribute('title') || el.getAttribute('name') || el.getAttribute('alt'));
    // Último recurso para link: o caminho da URL diz mais que nada.
    if (!out && el.tagName === 'A') {
      try {
        const url = new URL(el.href);
        out = clean((url.pathname + url.search).replace(/^\//, '')) || url.hostname;
      } catch (_) {
        /* href relativo/inválido */
      }
    }
    return out.slice(0, 120);
  };
  // Nome de um ITEM de lista. O innerText inteiro de uma linha do Drive é
  // "Print\nem Meu Drive\nMais ações\n…" — o que identifica é só a primeira
  // linha. É este nome que vira o rótulo da linha e o "dono" dos botões dela.
  NEO.shortLabel = (el) => {
    const raw = String(el.getAttribute('aria-label') || el.innerText || el.textContent || '');
    const first = raw.split('\n').map((s) => s.trim()).filter(Boolean)[0] || '';
    return first.replace(/\s+/g, ' ').slice(0, 60);
  };
  // A ESTRUTURA de um rádio de grade (vai em `item.grade`), para o core montar linhas × colunas em
  // qualquer site (core/browser_local/grade.py). Antes o core só lia o Google
  // Forms em português: dependia de o `radiogroup` entrar no inventário por
  // acaso (o Google põe `jsaction` nele) e do rótulo "X, resposta para Y". Uma
  // matriz em <table> de rádios comuns (SurveyMonkey, Moodle) dava zero
  // perguntas, e o Google em inglês embaralhava as colunas.
  //   grupo    — quem é a "linha": o radiogroup ARIA ou o `name` do rádio HTML
  //   linha    — o nome dela: o do radiogroup, ou o cabeçalho da linha na tabela
  //   coluna   — o cabeçalho da coluna, quando o rádio está numa tabela
  //   pergunta — o enunciado: legend, caption ou o título do item da lista
  NEO.grade = (el) => {
    const clean = (v) => String(v || '').replace(/\s+/g, ' ').trim();
    const ctl = el.tagName === 'LABEL' && el.control ? el.control : el;
    const nativo = ctl.tagName === 'INPUT' && String(ctl.type || '').toLowerCase() === 'radio';
    const aria = ctl.getAttribute('role') === 'radio';
    if (!nativo && !aria) return null;
    const nomeAria = (n) => {
      let out = clean(n.getAttribute('aria-label'));
      const ids = n.getAttribute('aria-labelledby');
      if (!out && ids) {
        out = clean(ids.split(/\s+/).map((id) => {
          const x = document.getElementById(id);
          return x ? x.innerText || x.textContent || '' : '';
        }).join(' '));
      }
      return out.slice(0, 200);
    };
    const out = {};
    const grupo = ctl.closest('[role=radiogroup]');
    if (grupo) {
      NEO._grupos = NEO._grupos || new WeakMap();
      if (!NEO._grupos.has(grupo)) NEO._grupos.set(grupo, (NEO._nGrupos = (NEO._nGrupos || 0) + 1));
      out.grupo = 'g' + NEO._grupos.get(grupo);
      // Sem nome ARIA, o texto do grupo serve só se for UMA linha ("Clareza"):
      // o de uma escolha simples traz as opções, uma por linha.
      const texto = String(grupo.innerText || '').trim();
      const nome = nomeAria(grupo) || (texto && !texto.includes('\n') ? clean(texto).slice(0, 200) : '');
      if (nome) out.linha = nome;
    } else if (nativo && ctl.name) {
      out.grupo = 'n:' + ctl.name;
    } else return null;
    const celula = ctl.closest('td,th,[role=gridcell],[role=cell]');
    const tr = celula && celula.closest('tr,[role=row]');
    const tabela = tr && tr.closest('table,[role=grid],[role=table]');
    if (celula && tr && tabela) {
      const cells = Array.from(tr.children);
      const i = cells.indexOf(celula);
      const topo = tabela.querySelector('thead tr') || tabela.querySelector('tr,[role=row]');
      if (topo && topo !== tr && i >= 0 && topo.children[i]) {
        const cab = clean(topo.children[i].innerText).slice(0, 80);
        if (cab) out.coluna = cab;
      }
      if (!out.linha) {
        const rotulo = cells.find((c) => c !== celula && !c.querySelector('input,[role=radio]') && clean(c.innerText));
        if (rotulo) out.linha = clean(rotulo.innerText).slice(0, 200);
      }
      const cap = tabela.querySelector('caption');
      if (cap && clean(cap.innerText)) out.pergunta = clean(cap.innerText).slice(0, 300);
      else if (nomeAria(tabela)) out.pergunta = nomeAria(tabela);
    }
    if (!out.pergunta) {
      const legenda = ctl.closest('fieldset') && ctl.closest('fieldset').querySelector('legend');
      if (legenda && clean(legenda.innerText)) out.pergunta = clean(legenda.innerText).slice(0, 300);
    }
    if (!out.pergunta) {
      // Google Forms (e outros): a pergunta é um item de lista com um título.
      const item = ctl.closest('[role=listitem]');
      const titulo = item && item.querySelector('[role=heading]');
      if (titulo && clean(titulo.innerText)) out.pergunta = clean(titulo.innerText).slice(0, 300);
    }
    return out;
  };
  NEO.el = (ref) => {
    const el = NEO.refs[ref];
    if (!el || !el.isConnected) return null;
    return el;
  };
  // MOLDURA do site, em UM lugar. Era uma string literal dentro do
  // `js_snapshot` e o `js_extract` não tinha acesso a ela — então o extract
  // devolvia 1500 caracteres de menu sem nenhum sinal de que era menu, e
  // leitura certa chegava ao modelo idêntica a leitura errada. Repetir o
  // seletor nos dois lugares criaria duas verdades, que é exatamente como esta
  // base já mentiu com autoridade uma vez.
  //
  // Só landmark DECLARADO, de propósito (ver a seção "moldura do site" do
  // inventário): errar para menos aqui é barato, errar para mais esconde a
  // resposta.
  //
  // `header` e `footer` só são banner e rodapé DO SITE fora de um bloco de
  // conteúdo: dentro de article, aside, main, nav ou section, o HTML os define
  // como cabeçalho e rodapé daquele bloco (é a regra que o leitor de tela usa).
  // O seletor pegava qualquer um, e o enunciado de um questionário, que mora no
  // `<header>` do cartão, sumia da leitura: nos jobs 2611e76d e 7b73f755
  // (12axes) o modelo leu "01" e cinco opções, sem a pergunta e sem o
  // "Question 1 of 36", e reiniciou o quiz em loop. O título e o autor de toda
  // notícia em `<article><header>` iam junto.
  NEO.BLOCO = 'article,aside,main,nav,section,' +
    '[role=article],[role=complementary],[role=main],[role=navigation],[role=region]';
  NEO.CHROME = 'nav,[role=navigation],[role=banner],[role=contentinfo],' +
    'header:not(:is(' + NEO.BLOCO + ') header),' +
    'footer:not(:is(' + NEO.BLOCO + ') footer)';
  // Conjunto AMPLO, só para DESCONTAR texto na eleição do conteúdo principal.
  // Não carimba elemento nenhum e não esconde texto de ninguém: muda apenas
  // QUAL bloco é adicionado na frente. Por isso pode ser mais largo que o
  // `NEO.CHROME`, onde errar para mais é caro.
  NEO.CHROME_DESCONTO = NEO.CHROME + ',aside,[role=complementary],[role=search]';
  // Profundidade no DOM — o desempate da eleição do principal.
  NEO.fundura = (el) => {
    let n = 0;
    for (let p = el; p; p = p.parentElement) n++;
    return n;
  };
  // ── O PAINEL DO NEO NÃO É A PÁGINA ─────────────────────────────────────
  // `elementFromPoint` devolve o host do overlay da extensão quando o ponto cai
  // nele, e esse host é um `div` fixo de shadow fechado, sem nome. Todo elemento
  // embaixo do painel saía `⟪COBERTO por "div"⟫`: 0 ocorrências nos jobs de antes
  // de a faixa virar painel lateral, 540 no job 45e85695. E a regra do prompt
  // para COBERTO é "dispense o intruso primeiro" — o próprio Neo fabricava o
  // obstáculo que depois gastava passos tentando tirar da frente.
  NEO.NOSSOS = '#neo-browser-local-overlay,#neo-marks,#neo-map-overlay';
  // `elementFromPoint` de verdade: pula o que é nosso e atravessa shadow DOM
  // aberto, senão o ponto para no host do web component e nada lá dentro existe.
  NEO.topo = (x, y, doc) => {
    const d = doc || document;
    let el = null;
    try { el = d.elementFromPoint(x, y); } catch (_) { return null; }
    if (el && el.matches(NEO.NOSSOS)) {
      el = null;
      try {
        for (const c of d.elementsFromPoint(x, y)) {
          if (!c.matches(NEO.NOSSOS)) { el = c; break; }
        }
      } catch (_) { return null; }
    }
    for (let n = 0; el && el.shadowRoot && n < 8; n++) {
      let dentro = null;
      try { dentro = el.shadowRoot.elementFromPoint(x, y); } catch (_) { break; }
      if (!dentro || dentro === el) break;
      el = dentro;
    }
    return el;
  };
  // `contains` e `closest` param na fronteira do shadow; estes dois atravessam.
  NEO.contem = (a, b) => {
    if (a.contains(b)) return true;
    for (let n = b; n; ) {
      if (n === a) return true;
      const pai = n.parentNode;
      n = pai && pai.host ? pai.host : pai;
    }
    return false;
  };
  NEO.subir = (el, sel) => {
    for (let n = el; n; ) {
      const achou = n.closest(sel);
      if (achou) return achou;
      const raiz = n.getRootNode();
      n = raiz && raiz.host ? raiz.host : null;
    }
    return null;
  };
  // As raízes onde procurar. `querySelectorAll` não entra em shadow DOM, e web
  // component é como Salesforce, Reddit e todo design system corporativo
  // desenham a tela: sem isto, esses sites simplesmente não têm elementos.
  NEO.raizes = (doc) => {
    const out = [doc || document];
    for (let i = 0; i < out.length && out.length < 80; i++) {
      for (const el of out[i].querySelectorAll('*')) {
        if (el.shadowRoot) out.push(el.shadowRoot);
      }
    }
    return out;
  };
  NEO.todos = (sel, raizes) => {
    const rs = raizes || NEO.raizes();
    if (rs.length === 1) return document.querySelectorAll(sel);
    const out = [];
    for (const r of rs) out.push(...r.querySelectorAll(sel));
    return out;
  };
  // O endereço de uma imagem. `data:` é a miniatura embutida do carregamento
  // preguiçoso, e o endereço de verdade fica em `data-src` ou no `srcset`.
  NEO.srcDe = (img) => {
    const direto = String(img.currentSrc || img.src || '');
    if (direto && !direto.startsWith('data:')) return direto;
    const lazy = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '';
    if (lazy) return lazy;
    const conjunto = img.getAttribute('data-srcset') || img.getAttribute('srcset') || '';
    return conjunto ? conjunto.split(',')[0].trim().split(/\s+/)[0] : '';
  };
  NEO.normaliza = (v) => String(v || '')
    .normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/\s+/g, ' ').trim().toLowerCase();

  // ── LEITURA: o texto que um humano enxerga ─────────────────────────────
  // O snapshot lia `body.innerText`, que sai em ordem de DOCUMENTO: todo site põe
  // a navegação primeiro, então o orçamento acabava antes do conteúdo. Os
  // remendos (`principal`, teto do corpo) só mudavam QUAL pedaço do DOM ia na
  // frente — e bastava a eleição errar para o modelo ficar cego. No job 45e85695
  // ela elegeu o carrinho em 19 snapshots de página de produto: em 15 o preço do
  // produto, que estava na tela, não chegou ao prompt, e nos outros 4 só apareceu
  // enterrado numa linha de variação.
  //
  // Aqui a pergunta é outra: o que está NA JANELA agora? Independe de landmark,
  // de framework e de eleição, e rolar passa a trazer texto novo, que é o que
  // qualquer um espera. Medido na mesma página: 1.385 caracteres em 11 ms, com
  // preço, parcelas, frete, estoque, vendedor e nota.
  //
  // Ordem de documento dentro da janela, e não ordenação por coordenada: o DOM
  // respeita coluna, e ordenar por `top` intercalaria as colunas linha a linha.
  NEO.SEM_TEXTO = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|OPTION|OPTGROUP|DATALIST|TEXTAREA|TITLE)$/i;
  // Tapado = atrás de uma camada FIXA e OPACA. Errar para menos é o lado seguro:
  // o erro oposto some com texto visível. Card com `<a>` transparente esticado
  // por cima (o `stretched-link`, comuníssimo em lista de produto) pegaria o
  // ponto do hit-test e levaria o texto inteiro junto se bastasse "não me acertou".
  NEO.tapado = (el, r, gasto) => {
    const x = (Math.max(r.left, 0) + Math.min(r.right, innerWidth)) / 2;
    const y = (Math.max(r.top, 0) + Math.min(r.bottom, innerHeight)) / 2;
    const topo = NEO.topo(x, y);
    if (!topo || topo === el || NEO.contem(el, topo) || NEO.contem(topo, el)) return false;
    let q = topo, opaco = false;
    for (let n = 0; q && q !== document.body && q.nodeType === 1 && n < 8; n++) {
      gasto();
      const s = getComputedStyle(q);
      const cor = s.backgroundColor.match(/[\d.]+/g) || [];
      const alfa = cor.length >= 4 ? parseFloat(cor[3]) : cor.length === 3 ? 1 : 0;
      if (alfa >= 0.9 || s.backgroundImage !== 'none') opaco = true;
      if (s.position === 'fixed') return opaco && !NEO.contem(q, el);
      q = q.parentElement;
    }
    return false;
  };
  // Devolve `tela` (na janela agora) e `resto` (existe, mas fora dela). À ESQUERDA
  // da página não conta como "resto": x negativo não se alcança rolando, é onde
  // moram os skip-links e o painel de atalhos de teclado da Amazon.
  //
  // `{ recorte: true }` respeita o texto que o CSS esconde sem tirar do DOM:
  // caixa de altura fixa com `overflow: hidden`, `line-clamp`, reticências,
  // caixa com rolagem. Sem isto a caixa estar na janela bastava para TODO o
  // texto dela contar como tela. No job 21c563c1 (Portal) a lista lateral
  // mostrava duas linhas de cada uma das 10 questões e despejava os 10
  // enunciados inteiros: 11 mil caracteres, o teto estourava em todo passo, e
  // o que vinha depois no DOM (a questão atual, o aviso de finalizar) caía
  // fora. O escondido vai para `resto`, não some. Só o snapshot pede isto: o
  // `extract` e a busca leem o texto inteiro, como sempre leram.
  NEO.ler = (excluir, opcoes) => {
    const vw = innerWidth, vh = innerHeight;
    const recortar = !!(opcoes && opcoes.recorte);
    const decisao = new Map();
    let orcamento = 400;
    const gasto = () => { orcamento--; };
    const decidir = (el) => {
      let d = decisao.get(el);
      if (d !== undefined) return d;
      d = 0;
      if (!NEO.SEM_TEXTO.test(el.tagName) && !(excluir && excluir(el))) {
        let pode = true;
        try {
          if (el.checkVisibility) {
            pode = el.checkVisibility({
              opacityProperty: true, visibilityProperty: true,
              checkOpacity: true, checkVisibilityCSS: true,
            });
          }
        } catch (_) { pode = true; }
        if (pode) {
          const r = el.getBoundingClientRect();
          if (r.width > 0 && r.height > 0 && r.right > 0) {
            const naJanela = r.bottom > 0 && r.top < vh && r.left < vw;
            d = naJanela && !(orcamento > 0 && NEO.tapado(el, r, gasto)) ? 1 : 2;
          }
        }
      }
      decisao.set(el, d);
      return d;
    };
    const linhas = { 1: [], 2: [] };
    const anterior = { 1: null, 2: null };
    const juntar1 = (d, r, t) => {
      const ant = anterior[d];
      const lista = linhas[d];
      const naMesma = ant && lista.length &&
        Math.abs(r.top - ant.top) < Math.max(r.height, ant.height) * 0.7 && r.left >= ant.left;
      if (naMesma) lista[lista.length - 1] += ' ' + t;
      else lista.push(t);
      anterior[d] = r;
    };
    // A caixa que RECORTA este elemento, até 4 níveis acima: `overflow` que não
    // é `visible` e conteúdo maior que ela. O corpo da página não conta: a
    // rolagem dele é a janela, e a janela já é a pergunta de `decidir`.
    // Cada eixo por si: a caixa que só corta na horizontal (a tabela larga, a
    // linha com reticências) não diz nada sobre o que fica abaixo dela. `rola`
    // separa o escondido que se alcança rolando a caixa (vai para o resto) do
    // que nunca aparece (`hidden`, `clip`, line-clamp: sai do snapshot, e o
    // `extract` e a busca continuam lendo). Mandar o segundo para o resto
    // empurrava para fora do teto dele o que está abaixo da dobra de verdade.
    const corta = new Map();
    const rolavel = (v) => v === 'auto' || v === 'scroll';
    const cortaAqui = (p) => {
      let c = corta.get(p);
      if (c !== undefined) return c;
      const s = getComputedStyle(p);
      const y = s.overflowY !== 'visible' && p.scrollHeight > p.clientHeight + 2;
      const x = s.overflowX !== 'visible' && p.scrollWidth > p.clientWidth + 2;
      c = y || x ? { y, x, rola: (y && rolavel(s.overflowY)) || (x && rolavel(s.overflowX)) } : null;
      corta.set(p, c);
      return c;
    };
    const recorteDe = (el) => {
      for (let p = el, n = 0; p && p.nodeType === 1 && p !== document.body &&
           p !== document.documentElement && n < 4; p = p.parentElement, n++) {
        const c = cortaAqui(p);
        if (c) return { caixa: p, ...c };
      }
      return null;
    };
    // Quanto do nó cabe na parte visível da caixa: as linhas dele (os retângulos
    // do Range) contra a caixa, só no eixo em que ela corta, pela LARGURA
    // visível de cada linha. A janela não entra: "está na janela" é a pergunta de
    // `decidir`, e misturar as duas faria o mesmo parágrafo valer diferente
    // dentro e fora de uma caixa. `[fração, lado]`: lado 1 = o visível é o
    // começo do texto (altura fixa, line-clamp, reticências), -1 = é o fim
    // (caixa rolada para baixo). `null` = não deu para medir.
    const parteVisivel = (no, rec) => {
      const caixa = rec.caixa;
      const cr = caixa.getBoundingClientRect();
      const topo = rec.y ? cr.top + caixa.clientTop : -Infinity;
      const fundo = rec.y ? cr.top + caixa.clientTop + caixa.clientHeight : Infinity;
      const esq = rec.x ? cr.left + caixa.clientLeft : -Infinity;
      const dir = rec.x ? cr.left + caixa.clientLeft + caixa.clientWidth : Infinity;
      let rects;
      try {
        const rg = document.createRange();
        rg.selectNodeContents(no);
        rects = [...rg.getClientRects()].filter((q) => q.width >= 1 && q.height >= 1);
      } catch (_) { return null; }
      // Uma linha pode vir em mais de um retângulo: com reticências o Range dá a
      // linha inteira E o pedaço que aparece. Fica o mais largo de cada linha.
      const porLinha = new Map();
      for (const q of rects) {
        const chave = Math.round(q.top);
        const atual = porLinha.get(chave);
        if (!atual || q.width > atual.width) porLinha.set(chave, q);
      }
      let total = 0, visto = 0, primeira = -1;
      [...porLinha.values()].sort((a, b) => a.top - b.top).forEach((q, i) => {
        total += q.width;
        const naFaixa = q.top < fundo - 1 && q.bottom > topo + 1;
        const w = naFaixa ? Math.max(0, Math.min(q.right, dir) - Math.max(q.left, esq)) : 0;
        visto += w;
        if (w > 0 && primeira < 0) primeira = i;
      });
      if (!total) return null;
      return [visto / total, primeira <= 0 ? 1 : -1];
    };
    const colher = (no) => {
      const t = no.nodeValue.replace(/\s+/g, ' ').trim();
      if (!t) return;
      const el = no.parentElement || (no.parentNode && no.parentNode.host);
      if (!el) return;
      const d = decidir(el);
      if (!d) return;
      const r = el.getBoundingClientRect();
      if (d === 1 && recortar) {
        const rec = recorteDe(el);
        const medida = rec && parteVisivel(no, rec);
        if (medida && medida[0] < 0.98) {
          const [fracao, lado] = medida;
          const alvo = Math.max(1, Math.round(t.length * fracao));
          const guardar = (x) => { if (rec.rola && x) juntar1(2, r, x); };
          if (fracao <= 0) { guardar(t); return; }
          // Texto curto parcialmente à vista é lido inteiro: o recorte serve para
          // o enunciado de mil caracteres, não para "Entrega GRÁTIS". E uma lasca
          // de texto longo ("D …" de "Duração: 3 minutos", o texto de leitor de
          // tela numa caixa de 1px) não é leitura: sai inteira da tela.
          if (t.length < 40) { juntar1(1, r, t); return; }
          if (alvo < 8) { guardar(t); return; }
          // Corta na palavra, para o modelo não ler meia palavra como dado.
          let k = lado > 0 ? t.lastIndexOf(' ', alvo) : t.indexOf(' ', t.length - alvo);
          if (k < 0 || (lado > 0 && k < alvo * 0.6)) k = lado > 0 ? alvo : t.length - alvo;
          const visto = lado > 0 ? t.slice(0, k) : t.slice(k);
          const escondido = lado > 0 ? t.slice(k) : t.slice(0, k);
          juntar1(1, r, lado > 0 ? visto.trim() + ' …' : '… ' + visto.trim());
          guardar(escondido.trim());
          return;
        }
      }
      juntar1(d, r, t);
    };
    const varrer = (raiz) => {
      const w = document.createTreeWalker(raiz, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
      let n;
      while ((n = w.nextNode())) {
        if (n.nodeType === 3) colher(n);
        else if (n.shadowRoot) varrer(n.shadowRoot);
      }
    };
    if (document.body) varrer(document.body);
    const juntar = (l) => l.filter((x, i) => x !== l[i - 1]).join('\n');
    return { tela: juntar(linhas[1]), resto: juntar(linhas[2]) };
  };
  NEO.fire = (el, type, extra) =>
    el.dispatchEvent(new Event(type, { bubbles: true, cancelable: true, ...(extra || {}) }));

  // ── ONDE O CLIQUE CAI ──────────────────────────────────────────────────
  // Evento SOBE na árvore, não desce. O inventário escolhe um elemento visível e
  // o clique era despachado nele, mas o controle de verdade pode ser um FILHO: o
  // botão da Amazon é um `<input>` com `opacity:0.01` esticado sobre um desenho, e
  // o clique no `span` pai nunca chegava nele. Foram 16 tentativas com `ok:true` e
  // o carrinho vazio no fim (job 8bc25dd1), enquanto o `Excluir` do mesmo job, um
  // input visível, funcionava. Só `<label>` encaminha para o controle.
  //
  // A pergunta certa não é "qual elemento?", é "quem recebe este PIXEL?". É o que o
  // navegador faz para um humano, e por isso vale em qualquer site, sem saber nada
  // da Amazon.
  NEO.CONTROLE = 'a[href],area[href],button,input:not([type=hidden]),select,textarea,summary,label,' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=switch],[role=tab],' +
    '[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option],[role=combobox]';
  NEO.dono = (el) => NEO.subir(el, NEO.CONTROLE);
  // Controle transparente sem nome próprio: o nome dele é o que se lê no desenho
  // embaixo. Sem isto ele entra na lista como um elemento mudo, ou com o `name`
  // técnico do formulário, e o modelo não tem como saber que é aquele botão.
  NEO.nomeDe = (el) => {
    const proprio = NEO.label(el);
    if (el.tagName !== 'INPUT' || !el.parentElement) return proprio;
    if (proprio && proprio !== el.getAttribute('name')) return proprio;
    if (parseFloat(getComputedStyle(el).opacity || '1') >= 0.05) return proprio;
    return NEO.shortLabel(el.parentElement) || proprio;
  };
  // O controle por trás de um rosto. É o que torna verdade o `[seleciona ...]`
  // que o inventário escreve: `fill` e `select` recebiam o rosto e desistiam.
  NEO.controleDe = (el) => {
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return el;
    return (NEO.controles && NEO.controles.get(el)) || (el.tagName === 'LABEL' ? el.control : null) || null;
  };
  // `a` tapa ao menos 80% de `b`.
  NEO.cobre = (a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
    const h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
    return w > 0 && h > 0 && w * h >= rb.width * rb.height * 0.8;
  };
  NEO.vizinhos = (a, b) => {
    let p = b;
    for (let n = 0; p && n < 3; n++) {
      p = p.parentElement;
      if (p && p.contains(a)) return true;
    }
    return false;
  };
  // Controle transparente que está na frente: invisível para o filtro de
  // visibilidade, mas é ele que recebe o clique de quem olha o desenho embaixo.
  // Fora da janela não existe teste de acerto, e aí vale a geometria.
  NEO.naFrente = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 12 || r.height < 12 || r.right <= 0 || r.bottom <= -200) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x >= 0 && y >= 0 && x < innerWidth && y < innerHeight) return NEO.topo(x, y) === el;
    return s.position === 'absolute' && !!el.parentElement && NEO.cobre(el, el.parentElement);
  };
  // De onde o documento do elemento começa. Num iframe de mesma origem o elemento
  // mede relativo ao quadro dele, e o mouse real anda na janela de cima.
  NEO.origem = (el) => {
    const doc = el.ownerDocument;
    let dx = 0, dy = 0;
    try {
      for (let w = doc.defaultView; w && w !== w.parent; w = w.parent) {
        const f = w.frameElement;
        if (!f) break;
        const b = f.getBoundingClientRect();
        dx += b.left + f.clientLeft;
        dy += b.top + f.clientTop;
      }
    } catch (_) { /* quadro de outra origem: fica o que deu para somar */ }
    return { doc, dx, dy };
  };
  NEO.mirar = (el) => {
    const { doc, dx, dy } = NEO.origem(el);
    const vista = doc.defaultView || window;
    const r = el.getBoundingClientRect();
    const cy = r.top + r.height / 2;
    const meio = [r.left + r.width / 2, cy];
    const pontos = [
      meio,
      [r.left + Math.min(r.width * 0.25, 40), cy],
      [r.right - Math.min(r.width * 0.25, 40), cy],
    ];
    // Link quebrado em duas linhas: o centro da caixa cai no vazio entre elas.
    const partes = el.getClientRects();
    if (partes.length > 1) pontos.push([partes[0].left + partes[0].width / 2, partes[0].top + partes[0].height / 2]);
    const controle = el.matches(NEO.CONTROLE);
    const nome = NEO.normaliza(NEO.label(el));
    const ponto = (x, y) => ({ x, y, gx: x + dx, gy: y + dy });
    let reserva = null, tampa = null;
    for (const [x, y] of pontos) {
      if (x < 0 || y < 0 || x >= vista.innerWidth || y >= vista.innerHeight) continue;
      const t = NEO.topo(x, y, doc);
      if (!t) continue;
      if (t === el) return { ...ponto(x, y), alvo: el, pixel: true };
      if (NEO.contem(el, t)) {
        const d = NEO.dono(t);
        const outro = d && d !== el && NEO.contem(el, d) && !(el.tagName === 'LABEL' && el.control === d);
        // Conteúdo do próprio alvo: o evento nasce no filho e sobe até ele.
        if (!outro) return { ...ponto(x, y), alvo: controle ? el : t, pixel: true };
        // `el` era o rosto e o controle está dentro dele: o caso da Amazon.
        if (!controle) return { ...ponto(x, y), alvo: d, pixel: true };
        // Controle dentro de controle (o "termos" no meio do label do aceite):
        // este ponto clicaria em outra coisa. Procura um ponto que seja do alvo.
        continue;
      }
      // O pixel vai para um ancestral: `el` é pintura com `pointer-events:none`.
      if (NEO.contem(t, el)) { reserva = reserva || ponto(x, y); continue; }
      // Outro elemento na frente. Só é o alvo se for o mesmo controle sob outra
      // tag: mesmo nome, ou esticado sobre o rosto logo ao lado dele.
      const d = NEO.dono(t);
      if (d && (NEO.normaliza(NEO.label(d)) === nome || (NEO.cobre(d, el) && NEO.vizinhos(d, el)))) {
        return { ...ponto(x, y), alvo: d, pixel: true };
      }
      tampa = tampa || t;
    }
    return { ...(reserva || ponto(meio[0], meio[1])), alvo: el, pixel: false, tampa };
  };
  NEO.descrever = (el) => {
    const tipo = el.getAttribute('type');
    return el.tagName.toLowerCase() + (tipo ? '(' + tipo + ')' : '') + (el.id ? '#' + el.id : '');
  };
  NEO.soltar = (c) => {
    const janela = c.alvo.ownerDocument.defaultView || window;
    for (const t of ['pointerdown', 'mousedown', 'click']) janela.removeEventListener(t, c.ouvir, true);
    if (NEO.clique === c) NEO.clique = null;
  };
  // Primeira metade do clique: rola, descobre quem recebe o pixel e arma a SONDA,
  // que é o que distingue "mandei" de "chegou". O disparo fica para depois porque
  // o caminho principal é o mouse de verdade, que sai pelo CDP, fora da página.
  //   tipo     esquerdo | duplo | direito
  //   trava    regex das ações que não desfazem, conferida no alvo REAL: o rótulo
  //            que o core julgou é o do rosto, e o que recebe o clique pode ser
  //            outro. E em `click_text` ele só conhecia o texto que o modelo
  //            escreveu: "agora" passava por "Comprar agora".
  //   liberado rótulo que o usuário já autorizou
  //   proprio  confere também o `el` (o core nunca viu o rótulo dele)
  NEO.preparar = (el, o) => {
    const opcoes = o || {};
    if (NEO.clique) NEO.soltar(NEO.clique);
    const ctl = NEO.controleDe(el);
    if (el.disabled || (ctl && ctl.disabled)) return { ok: false, error: 'elemento desabilitado' };
    el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    let m = NEO.mirar(el);
    // Barra fixa tapando a borda: `nearest` para colado nela. Centralizar é um
    // pulo na tela, então só quando o ponto falhou.
    if (!m.pixel) {
      el.scrollIntoView({ block: 'center', inline: 'nearest' });
      const denovo = NEO.mirar(el);
      if (denovo.pixel) m = denovo;
    }
    const tipo = opcoes.tipo || 'esquerdo';
    const ehControle = m.alvo !== el && m.alvo.matches(NEO.CONTROLE);
    // O rótulo é lido ANTES de clicar: num INTERRUPTOR ele vira no próprio clique,
    // e o histórico registrava o estado NOVO como se fosse o que foi clicado —
    // "Reproduzir" no clique que acabara de PAUSAR (job 717a8fbb).
    const rotulo = (ehControle && NEO.nomeDe(m.alvo)) || NEO.nomeDe(el);
    if (opcoes.trava) {
      const re = new RegExp(opcoes.trava, 'i');
      const liberado = String(opcoes.liberado || '').slice(0, 120);
      const nomes = [ehControle ? NEO.nomeDe(m.alvo) : '', opcoes.proprio ? NEO.nomeDe(el) : ''];
      for (const nome of nomes) {
        if (nome && re.test(nome) && nome.trim().toLowerCase().slice(0, 120) !== liberado) {
          return { ok: false, error: 'SENSIVEL:' + nome.slice(0, 120) };
        }
      }
    }
    const c = {
      el, alvo: m.alvo, x: m.x, y: m.y, tipo, rotulo, recebeu: null,
      desc: NEO.descrever(m.alvo), achados: opcoes.achados,
    };
    c.ouvir = (e) => { if (!c.recebeu) c.recebeu = (e.composedPath && e.composedPath()[0]) || e.target; };
    const janela = m.alvo.ownerDocument.defaultView || window;
    for (const t of ['pointerdown', 'mousedown', 'click']) janela.addEventListener(t, c.ouvir, true);
    NEO.clique = c;
    // O mouse real obedece o navegador, e o navegador não sabe que o painel é
    // nosso: por baixo dele o clique acertaria o painel.
    let bruto = null;
    try { bruto = document.elementFromPoint(m.gx, m.gy); } catch (_) { /* fora da janela */ }
    // Direito não vai pelo mouse real: abriria o menu NATIVO do sistema por cima
    // da página. O que interessa é o menu do site, e esse responde ao evento.
    const real = m.pixel && tipo !== 'direito' && document.visibilityState === 'visible' &&
      !(bruto && bruto.matches(NEO.NOSSOS));
    // Pulso visual no ponto do clique: a aba mexendo sozinha sem nenhum sinal é
    // desconcertante para quem está olhando. Quem desenha é o shadow DOM da
    // extensão (nenhum nó novo na página) — daí ser um postMessage e não um
    // appendChild aqui.
    postMessage({ __neo_ping: { x: m.gx, y: m.gy, tipo: tipo === 'direito' ? 'direito' : 'esquerdo' } }, '*');
    return { ok: true, real, x: m.gx, y: m.gy, url: location.href, label: rotulo, alvo: c.desc, achados: c.achados };
  };
  // A reserva: evento fabricado no alvo resolvido. Fica para o que o mouse real
  // não alcança (aba em segundo plano, ponto debaixo do painel do Neo, botão
  // direito). `.click()` e não só eventos: é o que aciona o `<a>`, o submit e o
  // label. Sequência completa porque há site que só escuta pointerdown/mouseup.
  NEO.despachar = (c) => {
    const el = c.alvo;
    const vista = el.ownerDocument.defaultView || window;
    const base = { bubbles: true, cancelable: true, composed: true, view: vista, clientX: c.x, clientY: c.y };
    const opts = c.tipo === 'direito' ? { ...base, button: 2, buttons: 2 } : { ...base, button: 0 };
    const Ponteiro = vista.PointerEvent || PointerEvent, Mouse = vista.MouseEvent || MouseEvent;
    const sequencia = (detail) => {
      el.dispatchEvent(new Ponteiro('pointerdown', opts));
      el.dispatchEvent(new Mouse('mousedown', { ...opts, detail }));
      if (el.focus) el.focus();
      el.dispatchEvent(new Ponteiro('pointerup', opts));
      el.dispatchEvent(new Mouse('mouseup', { ...opts, detail }));
    };
    if (c.tipo === 'direito') {
      sequencia(1);
      el.dispatchEvent(new Mouse('contextmenu', opts));
    } else if (c.tipo === 'duplo') {
      for (let i = 1; i <= 2; i++) {
        sequencia(i);
        el.dispatchEvent(new Mouse('click', { ...opts, detail: i }));
      }
      el.dispatchEvent(new Mouse('dblclick', { ...opts, detail: 2 }));
    } else {
      sequencia(1);
      // SVG não tem `.click()`, e o pixel pode ter caído no `path` de um ícone.
      if (el.click) el.click(); else el.dispatchEvent(new Mouse('click', { ...opts, detail: 1 }));
    }
  };
  // O estado de seleção do controle clicado: true/false, ou undefined se não é
  // controle marcável.
  NEO.estadoMarcado = (c) => {
    const ctl = NEO.controleDe(c.el) || NEO.controleDe(c.alvo) ||
      (c.el.querySelector ? c.el.querySelector('input[type=radio],input[type=checkbox]') : null);
    const tipo = ctl && ctl.getAttribute('type');
    if (tipo === 'radio' || tipo === 'checkbox') return !!ctl.checked;
    const aria = c.el.getAttribute('aria-checked') || c.el.getAttribute('aria-selected');
    if (aria === 'true' || aria === 'false') return aria === 'true';
    return undefined;
  };
  // O fim do clique, esperando a página ASSENTAR o estado. O Google Forms vira o
  // `aria-checked` da grade pelo JS dele, um instante depois do clique: lido na
  // hora, saía "NÃO ficou selecionado" para célula que tinha ficado (sessão
  // 74408e70, ~15 vezes num job), e o agente gastava passo re-clicando e
  // pedindo `look`. Só relê quando o primeiro veredito é "não".
  NEO.fimDoCliqueAssentado = async (c, via) => {
    const out = NEO.fimDoClique(c, via);
    if (out.checked !== false || out.saiu) return out;
    for (const ms of [120, 300]) {
      await new Promise((ok) => setTimeout(ok, ms));
      if (!c.el.isConnected) { out.saiu = true; delete out.checked; return out; }
      if (NEO.estadoMarcado(c) === true) { out.checked = true; out.assentou_ms = ms; return out; }
    }
    return out;
  };
  NEO.fimDoClique = (c, via) => {
    const recebeu = c.recebeu;
    NEO.soltar(c);
    const out = { ok: true, url: location.href, label: c.rotulo, via, alvo: c.desc };
    if (c.achados !== undefined) out.achados = c.achados;
    // O mouse real obedece o pixel, e a página pode ter mudado entre mirar e
    // clicar. Não dá para desfazer, mas dá para não mentir sobre onde caiu.
    if (via === 'pixel' && recebeu && !NEO.contem(c.alvo, recebeu) && !NEO.contem(recebeu, c.alvo) &&
        !NEO.contem(c.el, recebeu)) {
      out.desvio = NEO.descrever(recebeu);
    }
    // O clique virou SELEÇÃO? É a única evidência de efeito que existe para
    // "marque a alternativa": marcar um rádio não muda URL, nem título, nem
    // texto — o freio de tela repetida é cego para isso por construção. O estado
    // sai do CONTROLE, inclusive do input escondido atrás do label.
    //
    // Controle que SAIU da página não tem estado para ler. O clique pode
    // desmontá-lo: no job f9370b43 (12axes) a 36ª resposta trocou o cartão pela
    // etapa seguinte antes de o `aria-checked` virar `true`, o nó morto dizia
    // `false`, e "não ficou selecionado" fez o agente ler a etapa nova como o
    // quiz reiniciando. Sumir no clique é prova de que a página reagiu.
    if (!c.el.isConnected) {
      out.saiu = true;
    } else {
      const estado = NEO.estadoMarcado(c);
      if (estado !== undefined) out.checked = estado;
    }
    // O número, quando existe: o modelo volta a poder usar `click`/`extract` nele.
    if (NEO.refs) {
      const i = NEO.refs.indexOf(c.el);
      if (i >= 0) out.ref = i;
    }
    return out;
  };

  // ── VARREDURA DA DOM INTEIRA ───────────────────────────────────────────
  // O inventário é o que está na tela, com teto de 200. Isto é a pergunta oposta:
  // dado um NOME, o que existe no documento inteiro que responde por ele? Entra em
  // shadow DOM e em iframe de mesma origem, e não filtra por visibilidade: o que
  // está escondido volta marcado, porque "existe, mas está fechado" é resposta.
  //
  // Procura pelo NOME ACESSÍVEL e não só pelo texto. No job 8bc25dd1 o modelo chegou
  // a tentar `extract {"busca": "add-to-cart"}`: o controle de verdade não tem nó de
  // texto, o nome dele está em `value`, `title` e `aria-labelledby`, e o `click_text`
  // só andava por nós de texto. Texto casado sem controle em volta sobe até o dono,
  // que pode ser o `<input>` que se declarou rotulado por ele, ou o que está por cima.
  NEO.ATIVAVEL = NEO.CONTROLE + ',[onclick],[ng-click],[data-ng-click],[jsaction],[data-action],[tabindex="0"]';
  NEO.candidatos = (texto) => {
    const procurado = NEO.normaliza(texto);
    const raizes = NEO.raizes();
    let fechados = 0;
    for (let i = 0; i < raizes.length && raizes.length < 120; i++) {
      for (const f of raizes[i].querySelectorAll('iframe,frame')) {
        let d = null;
        try { d = f.contentDocument; } catch (_) { d = null; }
        if (d && d.body) raizes.push(...NEO.raizes(d));
        else fechados++;
      }
    }
    const grauDe = (nome) => {
      const n = NEO.normaliza(nome);
      if (!n) return 0;
      if (n === procurado) return 4;
      if (n.startsWith(procurado)) return 3;
      const i = n.indexOf(procurado);
      if (i < 0) return 0;
      return i === 0 || n[i - 1] === ' ' ? 2 : 1;
    };
    // Quem se declarou rotulado por qual id, para o caminho de volta: do texto ao
    // controle que aponta para ele.
    const rotuladosPor = new Map();
    for (const r of raizes) {
      for (const el of r.querySelectorAll('[aria-labelledby]')) {
        for (const id of el.getAttribute('aria-labelledby').split(/\s+/)) {
          if (id && !rotuladosPor.has(id)) rotuladosPor.set(id, el);
        }
      }
    }
    const sobreOTexto = (el) => {
      let p = el;
      for (let n = 0; p && n < 3; n++) {
        p = p.parentElement;
        if (!p) break;
        const perto = p.querySelectorAll('input:not([type=hidden]),button,a[href]');
        if (perto.length > 6) break;
        for (const c of perto) {
          if (c !== el && !c.contains(el) && NEO.cobre(c, el)) return c;
        }
      }
      return null;
    };
    const donoDoNome = (el) => {
      const d = NEO.dono(el);
      if (d) return d;
      for (let p = el, n = 0; p && n < 4; p = p.parentElement, n++) {
        if (p.id && rotuladosPor.has(p.id)) return rotuladosPor.get(p.id);
      }
      const sobre = sobreOTexto(el);
      if (sobre) return sobre;
      const ativavel = NEO.subir(el, NEO.ATIVAVEL);
      if (ativavel) return ativavel;
      for (let p = el, n = 0; p && n < 6; p = p.parentElement, n++) {
        if (getComputedStyle(p).cursor === 'pointer') return p;
      }
      return el;
    };
    const achados = new Map();
    let parou = false;
    const anotar = (bruto, nome, fonte) => {
      const grau = grauDe(nome);
      if (!grau) return;
      const el = donoDoNome(bruto);
      const antes = achados.get(el);
      if (!antes && achados.size >= 150) { parou = true; return; }
      const fraca = (f) => f === 'title' || f === 'alt';
      if (!antes || grau > antes.grau || (grau === antes.grau && fraca(antes.fonte) && !fraca(fonte))) {
        achados.set(el, { el, grau, fonte, nome: String(nome).replace(/\s+/g, ' ').trim() });
      }
    };
    for (const r of raizes) {
      for (const el of r.querySelectorAll('[aria-label],[aria-labelledby],[title],[alt],[placeholder],input')) {
        if (el.closest(NEO.NOSSOS)) continue;
        for (const a of ['aria-label', 'title', 'alt', 'placeholder']) {
          const v = el.getAttribute(a);
          if (v) anotar(el, v, a);
        }
        const ids = el.getAttribute('aria-labelledby');
        if (ids) {
          const raiz = el.getRootNode();
          const nome = ids.split(/\s+/).map((id) => {
            const n = (raiz.getElementById && raiz.getElementById(id)) || el.ownerDocument.getElementById(id);
            return n ? n.textContent : '';
          }).join(' ');
          anotar(el, nome, 'aria-labelledby');
        }
        if (el.tagName === 'INPUT' && /^(button|submit|reset)$/i.test(el.type)) anotar(el, el.value, 'value');
      }
      // Frase que atravessa nós: em `Li e aceito os <a>termos</a>` nenhum nó de
      // texto sozinho contém "aceito os termos". O texto inteiro do controle contém.
      for (const el of r.querySelectorAll(NEO.CONTROLE)) {
        if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) || el.closest(NEO.NOSSOS)) continue;
        anotar(el, el.textContent, 'texto');
      }
      const base = r.body || r;
      const walker = (r.ownerDocument || r).createTreeWalker(base, NodeFilter.SHOW_TEXT);
      let no;
      while ((no = walker.nextNode())) {
        if (!no.nodeValue || no.nodeValue.length < 2) continue;
        const pai = no.parentElement;
        if (!pai || NEO.SEM_TEXTO.test(pai.tagName) || pai.closest(NEO.NOSSOS)) continue;
        anotar(pai, no.nodeValue, 'texto');
      }
    }
    const lista = [];
    for (const a of achados.values()) {
      const el = a.el;
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      const doc = el.ownerDocument;
      const vista = doc.defaultView || window;
      const estado = {};
      const semArea = r.width < 2 || r.height < 2;
      if (semArea || s.display === 'none' || s.visibility === 'hidden') estado.escondido = true;
      else if (el.checkVisibility && !el.checkVisibility({ checkVisibilityCSS: true })) estado.escondido = true;
      // À esquerda da página não se chega rolando: é onde moram os skip-links.
      if (!estado.escondido && r.right <= 0) estado.escondido = true;
      const transparente = !estado.escondido && el.checkVisibility &&
        !el.checkVisibility({ checkOpacity: true });
      const naJanela = !estado.escondido && r.top < vista.innerHeight && r.bottom > 0 &&
        r.left < vista.innerWidth && r.right > 0;
      if (naJanela) {
        const t = NEO.topo(r.left + r.width / 2, r.top + r.height / 2, doc);
        if (t && (t === el || NEO.contem(el, t) || NEO.contem(t, el))) estado.recebe = true;
        else if (t) {
          const d = NEO.dono(t);
          if (d && NEO.cobre(d, el) && NEO.vizinhos(d, el)) estado.recebe = true;
          else estado.coberto = NEO.shortLabel(t) || t.tagName.toLowerCase();
        }
      } else if (!estado.escondido) estado.fora = true;
      // Transparente que não recebe o pixel está escondido para qualquer um.
      if (transparente && !estado.recebe && !(estado.fora && NEO.naFrente(el))) {
        estado.escondido = true;
        delete estado.fora;
        delete estado.coberto;
      }
      if (el.disabled) estado.desabilitado = true;
      const nativo = el.matches('a[href],area[href],button,input,select,textarea,summary,label');
      const tipoAlvo = nativo ? 30 : el.matches(NEO.CONTROLE) ? 25 : el.matches(NEO.ATIVAVEL) ? 15 :
        s.cursor === 'pointer' ? 10 : 0;
      const nota = a.grau * 10 + (a.fonte === 'title' || a.fonte === 'alt' ? 0 : 5) + tipoAlvo +
        (estado.recebe ? 20 : estado.fora ? 10 : 0) -
        (estado.escondido ? 60 : 0) - (estado.desabilitado ? 40 : 0) - (estado.coberto ? 25 : 0) -
        (NEO.subir(el, NEO.CHROME) ? 8 : 0);
      lista.push({ ...a, ...estado, nota, area: r.width * r.height });
    }
    // Casar no meio de uma palavra ("editar" em "hereditárias") só serve quando
    // não há nada melhor.
    const melhor = lista.reduce((m, c) => Math.max(m, c.grau), 0);
    const boas = melhor > 1 ? lista.filter((c) => c.grau > 1) : lista;
    // Ordenação estável: empate fica na ordem do documento depois do menor.
    boas.sort((a, b) => b.nota - a.nota || a.area - b.area);
    boas.forEach((c, i) => { c.n = i + 1; });
    return { lista: boas, fechados, parou };
  };
  return true;
})()
"#;

fn js_snapshot(max_elements: usize, text_chars: usize) -> String {
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  // Vocabulário CANÔNICO. É a primeira passada, não a única: allowlist de tag e
  // papel só enxerga site bem-comportado, e foi exatamente isso que cegou o
  // driver no questionário do portal (job 9968e68a) — as 5 alternativas eram
  // controle de formulário ESCONDIDO por CSS dentro de um contêiner sem papel
  // nenhum, então o snapshot devolveu 56 elementos e NENHUM deles era uma
  // alternativa. O modelo leu a questão, escolheu certo e clicou no menu
  // lateral 5 vezes. As passadas 2 e 3 abaixo existem por causa disso.
  const SEL = 'a[href],button,input:not([type=hidden]),select,textarea,label,summary,' +
    '[role=button],' +
    '[role=link],[role=tab],[role=checkbox],[role=radio],[role=menuitem],[role=combobox],' +
    '[role=menuitemcheckbox],[role=menuitemradio],[role=switch],[role=searchbox],' +
    '[role=textbox],[role=slider],[role=spinbutton],' +
    // Linha de lista/tabela/árvore. É AQUI que mora o nome do arquivo, da pasta
    // ou da mensagem: sem estes papéis o Google Drive vira 70 botões "Mais
    // ações" indistinguíveis e nenhum elemento clicável carrega o nome do item
    // (foi exatamente o que travou os dois jobs de 2026-08-28).
    '[role=row],[role=gridcell],[role=option],[role=treeitem],' +
    // `tabindex="0"` (e não `[tabindex]`) porque -1 é foco programático, que
    // sites usam em contêiner que ninguém clica. `ng-click`/`jsaction` são os
    // dois handlers declarativos comuns o bastante para valer um seletor
    // próprio; o resto dos frameworks é pego pela evidência de `cursor`.
    '[tabindex="0"],[ng-click],[data-ng-click],[jsaction],[data-action],' +
    '[onclick],[contenteditable=""],[contenteditable="true"]';
  const CONTAINER = '[role=row],[role=gridcell],[role=option],[role=treeitem],tr,li';
  // Menu/diálogo aberto: quando existe, é o ÚNICO lugar onde a próxima ação faz
  // sentido. Sem esta marca os 8 itens do menu do Drive ficavam perdidos no fim
  // de uma lista com 50 botões "Mais ações" — e o modelo reabria o menu em loop
  // em vez de clicar em "Baixar", que estava ali (job 706ff8a8).
  const OPEN = '[role=menu],[role=dialog],[role=alertdialog],[aria-modal="true"]';

  // Estilo calculado é a chamada CARA desta função (já estourou o timeout do
  // passo uma vez, varrendo `div[class]`). Memoiza e cobra de um orçamento: a
  // varredura geométrica lá embaixo sobe pelos MESMOS ancestrais centenas de
  // vezes, então o cache é o que torna ela viável.
  const estilos = new Map();
  let orcamento = 6000;
  const cs = (el) => {{
    let s = estilos.get(el);
    if (s === undefined) {{
      s = orcamento-- > 0 ? getComputedStyle(el) : null;
      estilos.set(el, s);
    }}
    return s;
  }};
  const caixas = new Map(); // el -> DOMRect (calculado uma vez só)
  const box = (el) => {{
    let r = caixas.get(el);
    if (r === undefined) {{ r = el.getBoundingClientRect(); caixas.set(el, r); }}
    return r;
  }};
  const visivel = (el) => {{
    if (!el || el.nodeType !== 1) return false;
    const r = box(el);
    if (r.width < 2 || r.height < 2) return false;
    const s = cs(el);
    if (!s) return false;
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') < 0.05) return false;
    return r.bottom > -200 && r.top < innerHeight + 2000;
  }};

  // ── O QUE UM PAINEL ABERTO É, DE VERDADE ───────────────────────────────
  // `role=dialog` diz o que o site DECLAROU, não o que está acontecendo na tela.
  // A geometria decide, e por isso vale em qualquer site:
  //   fora     nem está na janela (o carrinho da Amazon em `left: 1819` numa
  //            janela de 1600px): não existe para esta tarefa;
  //   pagina   `role=dialog` em fluxo: é parte da página, não abriu nada (a coluna
  //            de filtros da busca da Amazon, 262×7957px: no job aa4c89f8 ela
  //            saiu como ">>> O PAINEL «Filtros» ABRIU E É CONTEÚDO", com 138
  //            itens repetidos no topo, 9.408 caracteres de um prompt de 29.851);
  //   fluxo    não é fixo, mas flutua: menu suspenso, popover;
  //   lateral  faixa presa na borda, sem fundo escurecendo a página;
  //   bloqueia sobrepõe e tem fundo cobrindo a página, ou `aria-modal`, ou cobre
  //            o centro da janela: é modal, e enquanto está aberto é o conteúdo;
  //   solto    sobrepõe sem bloquear nada: popover, banner de rodapé.
  //
  // `lateral` é o carrinho da Amazon numa janela larga: 150px de 1900, sem X, que
  // não fecha com nada. O modal era "o conteúdo" por ser `role=dialog` fixo, e a
  // partir de ~1830px de janela ele tomava o lugar do conteúdo principal em TODA
  // página de produto. Sobrou para o modelo o carrinho, sem os dados do produto, e
  // fechar o painel passou a ser a decisão racional: 196s, 5 Escapes, 3 notas (job
  // 45e85695). O job headless do mesmo dia, em 1280px, nunca viu o carrinho e
  // coletou 12 itens em 100s.
  // O que torna um modal "o conteúdo" é ele SOBREPOR o resto, não o papel ARIA.
  // Dois sinais, nesta ordem: `aria-modal="true"`, que é a marcação que a spec
  // reserva para isso; ou posicionamento `fixed` — inclusive num ancestral
  // próximo, que é o desenho comum (fundo `fixed` com a caixa `absolute`
  // dentro). Um elemento oito vezes mais alto que a janela é conteúdo de
  // página, não sobreposição.
  const ehSobreposicao = (el) => {{
    if (el.getAttribute('aria-modal') === 'true') return true;
    let p = el, n = 0;
    while (p && n++ < 5) {{
      if (getComputedStyle(p).position === 'fixed') return true;
      p = p.parentElement;
    }}
    return false;
  }};
  // Fundo cobrindo a página: é o que separa a gaveta MODAL da faixa presa na borda,
  // e o modal alinhado ao topo (o desenho mais comum) de um popover. O que está
  // atrás de um fundo não responde a clique.
  //
  // A comparação de texto é o que distingue o fundo da CASCA do app: um `fixed`
  // de tela inteira que contém a página toda (SPA com layout fixo) tem muito mais
  // texto que o painel. Sem isso, toda barra lateral desses sites virava modal.
  const fundoCobre = (p) => {{
    let q = NEO.topo(innerWidth / 2, innerHeight / 2);
    for (let n = 0; q && q !== document.body && n < 6; n++) {{
      const r = box(q);
      if (r.width * r.height >= innerWidth * innerHeight * 0.8 &&
          getComputedStyle(q).position === 'fixed') {{
        return String(q.innerText || '').length <= String(p.innerText || '').length + 200;
      }}
      q = q.parentElement;
    }}
    return false;
  }};
  const NAO_E_PAINEL = ['fora', 'lateral', 'pagina'];
  const quadros = new Map();
  const quadro = (p) => {{
    let q = quadros.get(p);
    if (q) return q;
    const r = box(p);
    const w = innerWidth, h = innerHeight;
    if (!(r.top < h && r.bottom > 0 && r.left < w && r.right > 0)) q = 'fora';
    else if (!ehSobreposicao(p)) {{
      const pos = getComputedStyle(p).position;
      const emFluxo = pos === 'static' || pos === 'relative' || pos === 'sticky';
      q = emFluxo && !p.matches('[role=menu]') ? 'pagina' : 'fluxo';
    }} else {{
      const fundo = fundoCobre(p);
      const borda = r.height >= h * 0.4 && r.width <= w * 0.4 && (r.right >= w - 24 || r.left <= 24);
      const centro = r.left <= w / 2 && r.right >= w / 2 && r.top <= h / 2 && r.bottom >= h / 2;
      if (borda && !fundo) q = 'lateral';
      else if (fundo || centro || p.getAttribute('aria-modal') === 'true') q = 'bloqueia';
      else q = 'solto';
    }}
    quadros.set(p, q);
    return q;
  }};

  // ── EVIDÊNCIA DE INTERATIVIDADE ────────────────────────────────────────
  // Troca a pergunta "esta TAG é interativa?" (que nunca fecha — sempre existe
  // o próximo framework) por "este elemento dá SINAL de que reage?". Cada prova
  // é independente de biblioteca. `cursor:pointer` é a mais universal delas:
  // todo site que torna algo clicável muda o cursor, e é ela que pega a
  // `div ng-click` / `@click` / `wire:click` / `hx-get` sem papel e sem onclick.
  const RE_HANDLER = /^(on\w+|(data-)?ng-\w*click|v-on:|@|x-on:|wire:|hx-|jsaction|data-action|data-click)/i;
  const evidencia = (el) => {{
    if (!el || el.nodeType !== 1) return null;
    const tag = el.tagName;
    if (tag === 'BUTTON' || tag === 'SELECT' || tag === 'TEXTAREA' || tag === 'SUMMARY') return 'tag';
    if (tag === 'A' && el.hasAttribute('href')) return 'tag';
    if (tag === 'INPUT' && el.getAttribute('type') !== 'hidden') return 'tag';
    if (tag === 'LABEL' && (el.htmlFor || el.querySelector('input,select,textarea'))) return 'label';
    // Papel de CONTROLE conta como evidência própria. `row`/`gridcell`/
    // `treeitem` de propósito NÃO entram: no Drive a `row` é a lista inteira e
    // tratá-la como controle é o loop de 23 passos do job a0eab88c.
    const papel = el.getAttribute('role');
    if (papel && /^(button|link|checkbox|radio|menuitem|menuitemcheckbox|menuitemradio|option|tab|switch|searchbox|textbox|combobox|slider|spinbutton)$/.test(papel)) return 'role';
    if (el.isContentEditable) return 'editavel';
    if (el.getAttribute('tabindex') === '0') return 'tabindex';
    for (const a of el.attributes) {{
      if (RE_HANDLER.test(a.name)) return 'handler';
    }}
    const s = cs(el);
    if (s && s.cursor === 'pointer') return 'cursor';
    return null;
  }};

  // ── 1. candidatos ──────────────────────────────────────────────────────
  // Três passadas. A primeira pega o que o site DECLARA como interativo; a
  // segunda resgata o controle de formulário escondido por CSS; a terceira
  // varre a tela por PIXEL. Depois delas, a promoção do contêiner do item.
  //
  // `coleta` é maior que o teto de propósito. O limite cortava em ordem de
  // DOCUMENTO: a Amazon gasta os 200 lugares com cabeçalho, filtros e carrosséis,
  // e o que estava na tela ficava de fora. Agora junta-se o triplo e o teto
  // escolhe por RELEVÂNCIA (ver "o teto", mais abaixo).
  const coleta = {max} * 3;
  const raizes = NEO.raizes();
  const cands = [];
  const vistos = new Set();
  const origens = new Map(); // el -> como ele foi achado (entra no diário)
  const olhar = (el, origem, forcar) => {{
    if (!el || vistos.has(el)) return;
    if (el.closest('#neo-browser-local-overlay') || el.closest('#neo-marks')) return;
    // O inspetor visual da extensão desenha num overlay próprio. Ele já é
    // invisível ao mapeamento por não ter evidência e por ser pointer-events:none,
    // mas depender disso seria depender de um acidente.
    if (el.closest('#neo-map-overlay')) return;
    if (!forcar && !visivel(el)) return;
    vistos.add(el);
    origens.set(el, origem || 'declarado');
    cands.push(el);
  }};
  // O que está NA JANELA primeiro, depois o resto: uma coluna de 700 filtros antes
  // do conteúdo no DOM esgotaria até a coleta. A numeração continua a do documento.
  const declarados = [...NEO.todos(SEL, raizes)];
  for (const el of declarados) {{
    if (cands.length >= coleta) break;
    const r = box(el);
    if (r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth) olhar(el, 'declarado');
  }}
  for (const el of declarados) {{
    if (cands.length >= coleta) break;
    olhar(el, 'declarado');
  }}
  {{
    const posicao = new Map(declarados.map((el, i) => [el, i]));
    cands.sort((a, b) => posicao.get(a) - posicao.get(b));
  }}

  // ── 2. controle ESCONDIDO → procurador visível ─────────────────────────
  // O rádio/checkbox custom: `<input type=radio>` com `opacity:0` (ou 1px, ou
  // fora da tela) e um `<label>`/`<span>` desenhado por cima. O filtro de
  // visibilidade descartava o input — e com ele a única maneira de responder
  // uma prova, marcar um "li e aceito", escolher um endereço de entrega.
  //
  // Um controle de formulário é interativo POR DEFINIÇÃO: só a pintura dele
  // sumiu. O alvo clicável passa a ser o procurador (o label, que o navegador
  // encaminha para o controle) e o ESTADO continua saindo do input real.
  const procuradores = new Map(); // procurador -> input de verdade
  NEO.controles = new WeakMap();
  let examinados = 0;
  for (const inp of NEO.todos('input:not([type=hidden]),select,textarea', raizes)) {{
    if (examinados++ > 200 || cands.length >= coleta) break;
    if (vistos.has(inp)) continue;
    if (visivel(inp)) continue;
    // O `<label>` é o rosto de UM controle por definição — não precisa de limite.
    let alvo = (inp.labels && inp.labels[0]) || inp.closest('label') || null;
    // Transparente e na frente: ele É o botão, e o que se vê é o desenho embaixo.
    // Oferecer o pai aqui é o que fez o job 8bc25dd1 clicar 16 vezes no vazio.
    if (!alvo && NEO.naFrente(inp)) {{
      box(inp);
      olhar(inp, 'transparente', true);
      continue;
    }}
    if (!alvo || !visivel(alvo)) {{
      // Sem label, sobe procurando a caixa que ENVOLVE só este controle (o
      // `<div class="opcao">` que embrulha o input invisível e o texto). As
      // duas guardas abaixo vieram do google.com, onde a versão sem limite
      // ofereceu o `<body>` inteiro como "rosto" de um textarea de telemetria
      // e um contêiner do formulário como rosto do botão de busca. Clicar
      // nessas caixas não faz nada — é o erro que a regra da ⟪CAIXA⟫ já
      // combate, entrando pela porta dos fundos.
      const teto = innerWidth * innerHeight * 0.15;
      const PROIBIDO = /^(BODY|HTML|FORM|MAIN|ARTICLE|SECTION|NAV|HEADER|FOOTER)$/;
      let p = inp.parentElement, n = 0;
      alvo = null;
      while (p && n++ < 5) {{
        if (PROIBIDO.test(p.tagName)) break;
        const r = box(p);
        if (r.width * r.height > teto) break;
        // Mais de um controle dentro = é contêiner de vários, não o rosto
        // deste. O próprio input conta como um.
        if (visivel(p) && r.height >= 8 &&
            p.querySelectorAll('input:not([type=hidden]),select,textarea').length <= 1) {{
          alvo = p;
          break;
        }}
        p = p.parentElement;
      }}
    }}
    if (!alvo || !visivel(alvo)) continue;
    if (!procuradores.has(alvo)) {{
      procuradores.set(alvo, inp);
      NEO.controles.set(alvo, inp);
    }}
    olhar(alvo, 'procurador');
  }}

  // ── 3. varredura geométrica (o "inspetor") ─────────────────────────────
  // Em vez de descer por seletor, SOBE a partir dos pixels: amostra uma grade
  // sobre o viewport, e de cada ponto sobe até o primeiro ancestral com
  // evidência. Acha qualquer coisa que ocupe área e reaja — sem depender de
  // tag, papel ou framework. É a rede que pega o que ninguém previu.
  //
  // Barato porque os pontos são desduplicados ANTES de subir: mil amostras
  // caem sobre algumas dezenas de elementos distintos, e o cache de estilo faz
  // o resto. `elementFromPoint` não calcula estilo nenhum.
  const alvos = new Set();
  const PASSO = 40;
  for (let y = 20; y < innerHeight; y += PASSO) {{
    for (let x = 20; x < innerWidth; x += PASSO) {{
      const no = NEO.topo(x, y);
      if (no) alvos.add(no);
    }}
  }}
  const negativos = new Set();
  const provas = new Map(); // el -> por que ele entrou (a poda abaixo usa isto)
  for (const no of alvos) {{
    if (cands.length >= coleta || orcamento <= 0) break;
    let p = no, n = 0;
    while (p && p !== document.body && p !== document.documentElement && n++ < 10) {{
      if (vistos.has(p)) break;           // já inventariado por outra passada
      if (!negativos.has(p)) {{
        const prova = evidencia(p);
        if (prova && visivel(p)) {{
          let escolhido = p;
          if (prova === 'cursor') {{
            // `cursor` HERDA: numa linha clicável, todo `span` de dentro parece
            // clicável também. Emitir os filhos transformaria o alvo de verdade
            // numa ⟪CAIXA com 2 itens⟫ — que é justamente o que o driver
            // RECUSA clicar. O alvo é o mais EXTERNO da faixa contígua com
            // cursor:pointer, que é o que o usuário percebe como "o botão".
            let q = p.parentElement, m = 0;
            while (q && q !== document.body && q !== document.documentElement && m++ < 8) {{
              const sq = cs(q);
              if (!sq || sq.cursor !== 'pointer' || !visivel(q)) break;
              escolhido = q;
              q = q.parentElement;
            }}
          }}
          if (!vistos.has(escolhido)) {{ provas.set(escolhido, prova); olhar(escolhido, 'grade'); }}
          break;
        }}
        negativos.add(p);
      }}
      p = p.parentElement;
    }}
  }}

  // Terceira passada: as REGIÕES ROLÁVEIS que embrulham o que já achamos.
  //
  // O snapshot só enumerava o que é interativo, então um painel de texto puro
  // simplesmente não existia para o modelo. No job 9025d9f8 o painel de
  // detalhes do Drive estava ABERTO na tela, com o tamanho do arquivo logo
  // abaixo da dobra, e os únicos refs por perto eram os botões das abas
  // ("Detalhes", "Atividades"). O modelo passou 21 passos mandando `scroll` no
  // botão da aba — que não rola, porque não é o painel.
  //
  // COMO achar o painel importa. A primeira tentativa subiu a partir dos
  // candidatos e não achou nada (job f27970d6): no Drive o scroller do painel é
  // IRMÃO das abas e o conteúdo dele é texto puro — nenhum candidato mora lá
  // dentro, então a subida nunca passa por ele. Detecção que exige um elemento
  // interativo dentro do painel falha justamente no painel só-de-leitura, que é
  // o caso que interessa.
  //
  // Sondagem geométrica resolve: uma grade de pontos sobre o viewport, e de
  // cada ponto sobe a pilha. Acha qualquer coisa VISÍVEL (que é o critério que
  // queremos) sem depender do conteúdo, e custa ~25 × profundidade chamadas de
  // getComputedStyle — não os milhares de varrer `div[class]`, que já estourou
  // o timeout deste passo uma vez.
  const rolavel = (el) => {{
    if (!el || el === document.body || el === document.documentElement) return false;
    const s = cs(el);
    if (!s || !/(auto|scroll|overlay)/.test(s.overflowY)) return false;
    return el.scrollHeight > el.clientHeight + 8;
  }};
  const rolaveis = new Set();
  const jaOlhado = new Set(); // ancestral compartilhado não é reavaliado
  const subirAte = (node) => {{
    let p = node;
    while (p && p !== document.body && p !== document.documentElement) {{
      if (rolaveis.has(p)) return;
      if (!jaOlhado.has(p)) {{
        jaOlhado.add(p);
        if (rolavel(p)) {{ rolaveis.add(p); return; }}
      }}
      p = p.parentElement;
    }}
  }};
  // Os nós já colhidos pela grade acima servem de partida — a sondagem
  // dedicada de 5x5 pontos que existia aqui virou um subconjunto dela.
  for (const no of alvos) {{
    if (rolaveis.size >= 6) break;
    subirAte(no);
  }}
  // E ainda pelos candidatos: pega o painel que ficou fora da grade de pontos
  // (coluna estreita, lista que não cai em nenhuma amostra).
  for (const el of cands.slice()) {{
    if (rolaveis.size >= 6) break;
    subirAte(el.parentElement);
  }}
  for (const el of rolaveis) {{
    if (cands.length >= coleta) break;
    olhar(el, 'painel');
  }}

  for (const el of cands.slice()) {{
    if (cands.length >= coleta) break;
    const dono = el.closest(CONTAINER);
    // Wrapper puro (o `li` que embrulha um único link e repete o nome dele) não
    // entra: dobraria a lista sem acrescentar alvo. O contêiner só vale quando
    // tem nome PRÓPRIO — que é justamente o caso do item de lista, cujo nome é
    // o do arquivo e não o do botão que ele contém.
    if (dono && dono !== el && !vistos.has(dono)) {{
      const nome = NEO.shortLabel(dono);
      if (nome && nome !== NEO.label(el)) olhar(dono, 'contêiner');
    }}
  }}

  // ── 1b. poda: moldura com `cursor:pointer` NÃO é botão ─────────────────
  // O preço de aceitar `cursor` como evidência é que sites o aplicam em caixas
  // inteiras (o card do produto todo, a linha da tabela inteira). Duas regras
  // baratas resolvem: quem ocupa quase a tela não é controle, e quem EMBRULHA
  // outro candidato é moldura — o alvo é o de dentro. Só vale para quem entrou
  // pela grade E só por cursor: nada que o site declarou é podado aqui.
  {{
    const remover = new Set();
    const teto = innerWidth * innerHeight * 0.45;
    for (const el of cands) {{
      if (provas.get(el) !== 'cursor') continue;
      const r = box(el);
      if (r.width * r.height > teto) {{ remover.add(el); continue; }}
      for (const outro of cands) {{
        if (outro !== el && !remover.has(outro) && el.contains(outro)) {{ remover.add(el); break; }}
      }}
    }}
    if (remover.size) {{
      for (const el of remover) {{ vistos.delete(el); origens.delete(el); }}
      for (let i = cands.length - 1; i >= 0; i--) {{
        if (remover.has(cands[i])) cands.splice(i, 1);
      }}
    }}
  }}

  // ── 2. moldura do site ─────────────────────────────────────────────────
  // Só landmark de navegação DECLARADO. A versão anterior marcava como moldura
  // "tudo que não está na maior região semântica" e isso mentiu feio no job
  // 9025d9f8: a maior região era a grade de arquivos, então o painel de
  // detalhes ABERTO — que continha a resposta — foi carimbado como moldura e o
  // modelo aprendeu a ignorá-lo. Errar para menos aqui é barato (nada marcado);
  // errar para mais esconde a resposta.
  //
  // Mora no `JS_HELPERS` porque o `js_extract` também precisa dele para dizer
  // ao modelo quando a leitura saiu da moldura. Aqui é só o apelido local.
  const CHROME = NEO.CHROME;

  // ── o teto: o que fica quando não cabe tudo ────────────────────────────
  // Por RELEVÂNCIA, não por ordem de documento, e a ordem de `cands` (que é a
  // numeração dos refs) continua a do documento. Moldura na tela tem cota: o
  // cabeçalho precisa continuar alcançável ("Meus pedidos", a busca), mas não
  // pode tirar o lugar do que está na tela. Página que não bate no teto não
  // passa por nada disto.
  let omitidos = 0;
  if (cands.length > {max}) {{
    const COTA_MOLDURA = 40;
    const altura = innerHeight, largura = innerWidth;
    const notas = new Map();
    for (const el of cands) {{
      const r = box(el);
      const naJanela = r.bottom > 0 && r.top < altura && r.right > 0 && r.left < largura;
      const moldura = !!NEO.subir(el, CHROME);
      const painel = el.closest(OPEN);
      const destaque = painel && naJanela && !NAO_E_PAINEL.includes(quadro(painel));
      let nota;
      if (rolaveis.has(el) || destaque) nota = 0;
      else if (moldura && el.matches('input,textarea,select,[role=searchbox],[role=combobox]')) nota = 1;
      else if (naJanela && !moldura) nota = 1;
      else if (naJanela) nota = 2;
      else if (!moldura && r.right > 0 && r.left < largura) nota = 3;
      else nota = 4;
      const longe = r.top > altura ? r.top - altura : r.bottom < 0 ? -r.bottom : 0;
      notas.set(el, nota + Math.min(longe / 1000000, 0.9));
    }}
    const ranking = cands.slice().sort((a, b) => notas.get(a) - notas.get(b));
    const ficam = new Set();
    let molduras = 0;
    for (const el of ranking) {{
      if (ficam.size >= {max}) break;
      if (Math.floor(notas.get(el)) === 2 && molduras++ >= COTA_MOLDURA) continue;
      ficam.add(el);
    }}
    for (const el of ranking) {{
      if (ficam.size >= {max}) break;
      ficam.add(el);
    }}
    omitidos = cands.length - ficam.size;
    const dentro = cands.filter((el) => ficam.has(el));
    for (const el of cands) {{
      if (!ficam.has(el)) {{ vistos.delete(el); origens.delete(el); }}
    }}
    cands.length = 0;
    cands.push(...dentro);
  }}

  // ── 3. quem embrulha quem ──────────────────────────────────────────────
  // Para cada candidato, o candidato-ancestral mais próximo. Sobe pelo DOM
  // (O(n · profundidade)); comparar todos contra todos seria O(n²) e a tela do
  // Drive tem 200 candidatos.
  // A ordem de `cands` É a numeração dos refs (o laço de emissão anda por ela),
  // então dá para dizer ao core QUEM está dentro de quem — e não deduzir por
  // substring do rótulo, que erra: numa caixa cujo nome é a concatenação dos
  // filhos, "Mais ações" é substring do nome da caixa e "palworld Pasta" não é.
  const ordem = new Map();
  cands.forEach((el, i) => ordem.set(el, i));
  const filhos = new Map();
  const pai = new Map();
  for (const el of cands) {{
    let p = el.parentElement;
    while (p && !vistos.has(p)) p = p.parentElement;
    if (p) {{
      pai.set(el, p);
      filhos.set(p, (filhos.get(p) || 0) + 1);
    }}
  }}

  NEO.refs = [];
  const elements = [];
  for (const el of cands) {{
    const ref = NEO.refs.push(el) - 1;
    // Painel rolável usa nome CURTO pelo mesmo motivo que a linha de lista: o
    // `label` de um painel é o innerText inteiro dele, e a linha do elemento
    // viraria um parágrafo. O conteúdo tem lugar próprio (`item.texto`).
    const row = el.matches(CONTAINER) || rolaveis.has(el);
    const item = {{ ref, tag: el.tagName.toLowerCase(), text: row ? NEO.shortLabel(el) : NEO.nomeDe(el) }};
    const type = el.getAttribute('type') || el.getAttribute('role');
    if (type) item.type = type;

    // Geometria. O modelo não vê a tela quando a visão está desligada (chave sem
    // crédito, print recusado) e sem medida não distingue um ícone de 24px de
    // uma caixa de 1174px — foi exatamente o que ele confundiu. Coordenadas em
    // px de CSS, relativas ao viewport.
    const r = caixas.get(el);
    if (r) {{
      item.box = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
      // `NEO.visible` aceita uma folga generosa acima/abaixo para o modelo saber
      // que existe; mas clicar no que não está na tela é outra história.
      if (r.bottom < 0 || r.top > innerHeight) item.fora = true;
      // TESTE DE ACERTO: "este pixel reage a MIM?". `js_click` despacha o evento
      // direto no nó, então ele funciona MESMO com um banner de cookie por cima
      // — e devolve ok:true enquanto o site, que escuta no overlay, ignora. É o
      // mesmo `ok:true ≠ a página reagiu` do Drive entrando por outra porta.
      //
      // CONSERVADOR de propósito, em três camadas. Um "coberto" falso é pior
      // que nenhum: manda o modelo caçar um botão de fechar que não existe.
      //   1. ponto fora da tela não é testado (link de pulo mora em x=-9968;
      //      espremer o ponto para dentro testaria outro elemento qualquer);
      //   2. três pontos — acertar em UM já basta, porque sobreposição parcial
      //      continua clicável;
      //   3. só conta quem está na frente de verdade: uma CAMADA (fixed/sticky/
      //      absolute com z-index, ou dentro de um modal). Medido na Amazon: sem
      //      esta regra, 20 dos 200 elementos vinham "cobertos" por vizinhos de
      //      layout estáticos — ruído puro.
      if (!item.fora && r.width >= 2 && r.height >= 2) {{
        const pontos = [
          [r.left + r.width / 2, r.top + r.height / 2],
          [r.left + Math.min(r.width * 0.25, 40), r.top + r.height / 2],
          [r.right - Math.min(r.width * 0.25, 40), r.top + r.height / 2],
        ];
        let acertou = false, tampa = null;
        for (const [x, y] of pontos) {{
          if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
          const topo = NEO.topo(x, y);
          if (!topo) continue;
          if (topo === el || NEO.contem(el, topo) || NEO.contem(topo, el)) {{ acertou = true; break; }}
          if (!tampa) tampa = topo;
        }}
        if (!acertou && tampa) {{
          let camada = null, q = tampa, m = 0;
          while (q && q !== document.body && m++ < 8) {{
            const sq = cs(q);
            if (!sq) break;
            const flutua = sq.position === 'fixed' || sq.position === 'sticky' ||
              (sq.position === 'absolute' && sq.zIndex !== 'auto' && Number(sq.zIndex) > 0);
            if (flutua || q.matches(OPEN)) {{ camada = q; break; }}
            q = q.parentElement;
          }}
          // Barra lateral não conta. O clique é despachado direto no nó, então ele
          // chega, e dizer "coberto" reabriria a caça ao X de um painel que não fecha.
          const barra = camada && camada.closest(OPEN);
          if (camada && !(barra && quadro(barra) === 'lateral')) {{
            item.coberto = NEO.shortLabel(camada) || camada.tagName.toLowerCase();
            // QUEM cobre muda o conselho. Um banner de cookie na frente = feche
            // e siga; um PAINEL ABERTO na frente = o que interessa provavelmente
            // está DENTRO dele. Sem esta distinção as duas situações chegavam ao
            // modelo com a mesma frase ("dispense o que está na frente"), e no
            // job 6785cf70 isso virou 11 tentativas de fechar o modal que era a
            // própria resposta.
            if (camada.matches(OPEN)) item.coberto_painel = true;
          }}
        }}
      }}
    }}

    // CAIXA: embrulha 2+ elementos e portanto não é um controle. Clicar numa
    // dessas é o loop que queimou dois jobs — o evento é despachado, o site
    // escuta no item de dentro, nada acontece e o driver responde "ok".
    // Evidência PRÓPRIA de clique isenta da regra da caixa: um `<label>` de
    // alternativa embrulha a bolinha e o texto, e chamá-lo de "caixa" faria o
    // driver recusar exatamente o clique que precisa acontecer. `cursor`
    // sozinho não isenta — é fraco demais, e a `row` do Drive continua caixa.
    const prova = provas.get(el) || evidencia(el);
    const forte = prova === 'tag' || prova === 'label' || prova === 'role' ||
      prova === 'handler' || prova === 'editavel' || prova === 'tabindex';
    const n = filhos.get(el) || 0;
    if (n >= 2 && !forte) item.grupo = n;
    // De quem este elemento é filho. É o que permite ao core recusar o clique na
    // caixa E dizer, com os números certos, quais são os alvos lá dentro.
    const acima = pai.get(el);
    if (acima !== undefined && ordem.has(acima)) item.dentro = ordem.get(acima);

    // Fora da área da tarefa: menu lateral, cabeçalho, rodapé.
    if (NEO.subir(el, CHROME)) item.chrome = true;

    // Região que ROLA. Duas coisas de uma vez: o modelo descobre que existe um
    // painel (e qual ref rolar), e ganha o TEXTO dele — que inclui o que está
    // abaixo da dobra, porque innerText não para na borda visível. No caso que
    // motivou isto, isso responde "qual o tamanho do arquivo" sem rolar nada.
    if (rolaveis.has(el)) {{
      item.rola = [Math.round(el.scrollTop), Math.round(el.scrollHeight), Math.round(el.clientHeight)];
      const dentro = String(el.innerText || el.textContent || '').replace(/\n{{3,}}/g, '\n\n').trim();
      if (dentro) item.texto = dentro.slice(0, 1200);
    }}

    // O PAINEL ABERTO em si — o texto inteiro dele, pelo mesmo motivo do painel
    // que rola. Antes só `rolaveis` ganhava `item.texto`, e um modal que CABE na
    // tela (não rola, logo não é `rolavel`) chegava ao prompt como um rótulo
    // cortado em ~120 chars. Foi assim que a timeline de rastreio da Amazon —
    // que ERA a resposta da tarefa — não apareceu em nenhum dos 25 prompts do
    // job 6785cf70: o modelo abriu e fechou o mesmo modal 7 vezes procurando
    // uma informação que estava dentro dele e que ele nunca teve como ler.
    if (el.matches(OPEN) && !NAO_E_PAINEL.includes(quadro(el))) {{
      item.raiz = true;
      if (!item.texto) {{
        const dentro = String(el.innerText || el.textContent || '').replace(/\n{{3,}}/g, '\n\n').trim();
        if (dentro) item.texto = dentro.slice(0, 2000);
      }}
    }}

    // Não basta saber que está num contêiner aberto: importa QUAL. "Fechar"
    // dentro de um menu de escolha é uma coisa; dentro do painel de progresso
    // do download é cancelar o trabalho (job 0611add0, 10 passos perdidos).
    const painel = el.closest(OPEN);
    if (painel) {{
      const q = quadro(painel);
      if (q === 'lateral') {{
        item.lateral = NEO.shortLabel(painel) || 'barra lateral';
      }} else if (!NAO_E_PAINEL.includes(q)) {{
        item.menu = true;
        const nome = NEO.shortLabel(painel);
        if (nome) item.panel = nome;
      }}
    }}
    if (!row) {{
      // Botão sem nome próprio herda o nome da linha: "Mais ações" em "Print".
      const owner = el.closest(CONTAINER);
      if (owner && owner !== el) {{
        const name = NEO.shortLabel(owner);
        if (name && name !== item.text && !item.text.includes(name)) item.owner = name;
      }}
    }}
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') item.disabled = true;
    // ESTADO sai do controle, nunca do rótulo — e quando o elemento é um
    // procurador (o label visível de um rádio escondido), o controle é o input
    // lá atrás. Sem isto, "marcar a alternativa" não tem como ser verificado:
    // selecionar um rádio não muda URL, nem título, nem texto da página.
    const controle = procuradores.get(el) ||
      (el.tagName === 'LABEL' ? (el.control || null) : null) ||
      (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' ? el : null);
    if (controle) {{
      if (controle !== el) {{
        item.controla = (controle.getAttribute('type') || controle.tagName.toLowerCase()) +
          (controle.name ? '[' + controle.name + ']' : '');
        if (controle.disabled) item.disabled = true;
      }}
      const tipo = controle.getAttribute('type');
      if (tipo === 'checkbox' || tipo === 'radio') item.checked = !!controle.checked;
      else if (controle.tagName === 'SELECT') {{
        item.options = Array.from(controle.options).slice(0, 30).map((o) => o.text.trim().slice(0, 60));
        item.value = controle.value;
      }} else item.value = String(controle.value || '').slice(0, 80);
    }}
    // Estado ARIA: o mesmo conceito para quem implementou o controle na mão.
    for (const a of ['aria-checked', 'aria-selected', 'aria-pressed', 'aria-expanded']) {{
      const v = el.getAttribute(a);
      if (v === 'true' || v === 'false') {{
        if (item.checked === undefined && (a === 'aria-checked' || a === 'aria-selected')) {{
          item.checked = v === 'true';
        }} else if (a === 'aria-expanded') item.aberto = v === 'true';
      }}
    }}
    // COMO este elemento foi achado. Só sai quando NÃO foi o site que declarou:
    // é o que deixa medir, no diário, quanto a varredura por pixel está
    // acrescentando em cada site.
    const origem = origens.get(el);
    if (origem && origem !== 'declarado') item.via = origem;
    // O endereço INTEIRO: o `abrir_aba` por número abre exatamente isto. Cortado em
    // 300, o link do AliExpress (~600 chars de rastreio) chegava pela metade, e
    // na aba D do job 9a3223a4 o corte caiu num `%xx`: a página veio vazia. O
    // prompt já mostra o link encurtado no core, então isto não o engorda.
    if (el.tagName === 'A') {{
      const href = String(el.href || '');
      item.href = href.slice(0, 2000);
      if (href.length > 2000) item.href_cortado = true;
    }}
    if (el.tagName === 'IMG') item.src = NEO.srcDe(el).slice(0, 300);
    // Aninhado: `grupo` solto já quer dizer "caixa com N itens" (o driver não
    // deixa clicar nela), e um rádio de grade não pode virar caixa.
    const grade = NEO.grade(el);
    if (grade) item.grade = grade;
    elements.push(item);
  }}
  // Texto da PÁGINA INTEIRA, sempre. Tentei recortar isto para "a área da
  // tarefa" e o recorte jogou fora a resposta (job 9025d9f8: o texto virou as
  // 4 linhas da grade de arquivos e o painel de detalhes sumiu). O corte fino
  // agora mora nos painéis acima, que ADICIONAM texto em vez de subtrair.
  //
  // `|| textContent` pelo mesmo motivo de NEO.label: texto de leitor de tela e
  // nó fora do fluxo somem do innerText. Em Chrome o innerText sempre existe, então
  // isto é só a rede de segurança (e o que torna o coletor exercitável fora dele).
  const corpo = (document.body ? document.body.innerText || document.body.textContent : '') || '';
  // Normalizado UMA vez: o corte e o flag de "foi cortado" têm de medir a
  // MESMA string, senão o flag mente na fronteira.
  const corpoLimpo = corpo.replace(/\n{{3,}}/g, '\n\n').trim();

  // ── conteúdo PRINCIPAL, à frente da moldura ────────────────────────────
  // `body.innerText` sai em ordem de DOCUMENTO, e todo site põe a navegação
  // primeiro. No portal (job 9968e68a) os 3000 caracteres de orçamento viraram
  // os 20 links do menu lateral + cabeçalho + metadados da prova + os 10
  // enunciados do índice lateral — e o bloco "Alternativas", que ERA a tarefa,
  // ficou depois do corte. O modelo não podia clicar (não tinha ref) nem ler.
  //
  // Recortar a moldura fora seria repetir o erro do job 9025d9f8, em que o
  // recorte "só a área da tarefa" jogou a resposta no lixo. Então aqui nada é
  // subtraído: o conteúdo principal é ADICIONADO na frente, com orçamento
  // próprio, no mesmo espírito da seção dos painéis que rolam.
  let principal = null;
  const considerar = (el) => {{
    if (!el || !visivel(el) || el.closest(CHROME)) return;
    // Estar DENTRO de uma barra lateral / caixa de busca também desqualifica —
    // não basta recusar o `<aside>`, porque a `<ul>` lá dentro não é um aside e
    // passava, ganhando a eleição por volume de texto contra o artigo ao lado.
    //
    // Isto usa o conjunto AMPLO, e não contradiz a regra de que o amplo só
    // serve para descontar: recusar a ELEIÇÃO não esconde texto nenhum — o
    // corpo e a seção de painéis continuam carregando o que estiver aí, e sem
    // nenhum eleito o corpo volta a receber o orçamento inteiro. Carimbar
    // `item.chrome`, não: aquilo muda como o modelo TRATA o elemento, e foi
    // assim que o painel de detalhes do Drive virou invisível no job 9025d9f8.
    // Mecanismos diferentes, custos de erro diferentes.
    if (el.closest(NEO.CHROME_DESCONTO)) return;
    const r = box(el);
    if (r.width * r.height < innerWidth * innerHeight * 0.08) return;
    const t = String(el.innerText || '').replace(/\n{{3,}}/g, '\n\n').trim();
    if (t.length < 150) return;
    // "O MAIOR TEXTO GANHA" elegia sempre o wrapper mais EXTERNO que não É
    // moldura — e um `<div>` que EMBRULHA o cabeçalho não está dentro dele,
    // então `closest(CHROME)` o aprova. O critério `t.length` então converge
    // para a página inteira, sempre.
    //
    // Na página de pedidos da Amazon (job 29c739d4) o vencedor foi o wrapper
    // da página: os 2200 caracteres de orçamento viraram os skip-links e a
    // lista de departamentos, e os 5 pedidos — que começavam 659 caracteres
    // depois do corte — não apareceram em NENHUM dos 15 prompts. O modelo
    // rolou 6 vezes atrás de um texto que não muda com rolagem (o dump sai em
    // ordem de documento) e morreu no freio de giro com a resposta na tela.
    //
    // A pontuação passa a ser o texto LÍQUIDO, fora moldura. `textContent` e
    // não `innerText` de propósito: innerText força layout e esta varredura
    // não pode virar O(página inteira). Como é DESCONTO, superestimar penaliza
    // o wrapper — que é o lado seguro do erro.
    let moldura = 0;
    for (const c of el.querySelectorAll(NEO.CHROME_DESCONTO)) moldura += (c.textContent || '').length;
    const liquido = Math.max(0, t.length - moldura);
    if (liquido < 150) return;
    // Quase só moldura não é conteúdo, é wrapper. Teto folgado (60%) porque
    // rejeitar demais devolve `principal = null`, e aí o corpo volta a ser
    // despejado em ordem de documento — o defeito que esta seção existe para
    // evitar.
    if (moldura > t.length * 0.6) return;
    const fundo = NEO.fundura(el);
    if (!principal) {{ principal = {{ el, texto: t, liquido, fundo }}; return; }}
    // EMPATE É O NORMAL, não a exceção: o container da lista e o wrapper que o
    // embrulha contêm quase o mesmo texto líquido (na Amazon, 10.800 contra
    // 10.865). Dentro de 10% ganha o MAIS PROFUNDO — o bloco específico, não
    // quem o embrulha. É este desempate que faz o trabalho; o desconto só
    // torna as duas pontuações comparáveis.
    const folga = Math.max(principal.liquido, liquido) * 0.1;
    if (liquido > principal.liquido + folga) {{ principal = {{ el, texto: t, liquido, fundo }}; return; }}
    if (Math.abs(liquido - principal.liquido) <= folga && fundo > principal.fundo) {{
      principal = {{ el, texto: t, liquido, fundo }};
    }}
  }};
  for (const el of document.querySelectorAll('main,[role=main],[role=tabpanel],article')) {{
    considerar(el);
  }}
  if (!principal) {{
    // Sem landmark declarado: sobe a partir das empresas que não são moldura.
    // Conjunto pequeno e limitado — `innerText` força layout e não pode virar
    // varredura de página inteira.
    const olhados = new Set();
    for (const el of cands) {{
      if (olhados.size > 120) break;
      if (el.closest(CHROME)) continue;
      let pp = el.parentElement;
      // SEM TETO DE PROFUNDIDADE. O teto era 6 níveis, e ele existia porque,
      // quando a pontuação era `t.length`, subir mais SEMPRE ganhava: a raiz
      // tem todo o texto. Com o desconto de moldura isso virou ao contrário —
      // a raiz pontua ZERO e se autoexclui (medido na busca da Amazon:
      // `#a-page` tem 21.426 caracteres de `innerText` e 48.893 de moldura por
      // `textContent`, porque o textContent inclui os menus escondidos; o
      // líquido dá 0).
      //
      // E o teto não era neutro: ele favorecia sistematicamente região RASA.
      // Numa grade de resultados o container fica ~10 níveis acima dos links
      // dos produtos, fora do alcance — enquanto o container de uma barra de
      // filtros, que é rasa, entrava. Foi assim que a BARRA DE FILTROS ganhou
      // a eleição na busca de pendrive (job 5ab98119): 1.305 caracteres de
      // marcas e faixas de preço eleitos como conteúdo, com os 18.711 do
      // `#search` nunca considerados.
      //
      // O custo continua limitado pelo `olhados`, que é o limite de verdade:
      // ancestral compartilhado entra uma vez só, e o teto de 120 agora é
      // checado também aqui dentro — senão um candidato fundo sozinho poderia
      // estourar o orçamento de `innerText`, que força layout.
      while (pp && pp !== document.body) {{
        if (olhados.size > 120) break;
        if (!olhados.has(pp)) {{ olhados.add(pp); considerar(pp); }}
        pp = pp.parentElement;
      }}
    }}
  }}
  if (!principal) {{
    // ÚLTIMO RECURSO: o laço de cima sobe a partir dos CANDIDATOS, que são
    // elementos interativos. Uma página cujo conteúdo não tem link nem botão
    // dentro — um artigo puro, uma fatura, um texto legal — nunca era
    // considerada, e caía no mesmo desfecho do wrapper de moldura: nenhum
    // `principal`, corpo despejado em ordem de documento, cabeçalho primeiro.
    //
    // Só roda quando nada foi eleito, e com o MESMO teto de 120: chegar aqui já
    // significa que a página é simples (nenhuma região interativa de conteúdo),
    // e o teto é o que impede o `innerText` de virar varredura de página
    // inteira — o custo que esta seção não pode ter.
    let vistos = 0;
    for (const el of document.querySelectorAll('main,article,section,div,ul,ol,table,dl')) {{
      if (vistos++ > 120) break;
      considerar(el);
    }}
  }}
  // MODAL ABERTO GANHA o lugar de conteúdo principal — enquanto está aberto, ele
  // É o conteúdo. Não passa pelo `considerar` de propósito: aquele exige 8% do
  // viewport e 150 caracteres, e escolhe o MAIOR texto — três critérios que um
  // modal legítimo perde para o `<main>` atrás dele. Na Amazon o vencedor foi um
  // `<nav>` de skip-links ("Ir para / Conteúdo principal / Atalhos do teclado"),
  // enquanto o modal com a resposta não entrou em lugar nenhum.
  //
  // Só `bloqueia` (ver `quadro`). Foram três reprises do mesmo erro com o mesmo
  // painel, cada uma remendada com mais um filtro: fora da tela (`left: 1819`),
  // em fluxo (a barra de filtros), e agora na borda de uma janela larga. O que
  // faltava era a pergunta certa: ele cobre o que o usuário está olhando?
  const modal = [...NEO.todos('[role=dialog],[role=alertdialog],[aria-modal="true"]', raizes)]
    .filter((el) => visivel(el) && quadro(el) === 'bloqueia')
    .sort((a, b) => {{ const ra = box(a), rb = box(b); return rb.width * rb.height - ra.width * ra.height; }})[0];
  if (modal) {{
    const t = String(modal.innerText || modal.textContent || '').replace(/\n{{3,}}/g, '\n\n').trim();
    // `liquido`/`fundo` vão aqui só para o formato não divergir do que
    // `considerar` produz — esta atribuição é a última e ninguém pontua depois
    // dela, mas um registro com forma diferente é como um campo vira
    // `undefined` seis meses depois.
    if (t.length >= 40) principal = {{ el: modal, texto: t, liquido: t.length, fundo: NEO.fundura(modal) }};
  }}
  // PUBLICADO para o `extract` sem `ref` (ver `js_extract`). Não é cache: é a
  // MESMA escolha que acabou de ir no prompt, guardada em vez de reimplementada.
  // Reimplementar criaria uma segunda verdade, e é assim que a extensão passou a
  // mentir com autoridade uma vez (`neo-map.generated.js`) — aqui seria pior,
  // porque o modelo leria uma região e veria outra descrita.
  NEO.principal = principal ? principal.el : null;
  // Publicado pelo mesmo motivo: `extract` lê com o mesmo critério do snapshot.
  NEO.modal = modal && principal && principal.el === modal ? modal : null;
  // ── a LEITURA da página (ver `NEO.ler`) ────────────────────────────────
  // Barra lateral tem seção própria e moldura é moldura. O resto do que está na
  // janela entra, inclusive modal e popover: o driver deduplica por linha.
  NEO.foraDaLeitura = (el) => {{
    const p = el.closest(OPEN);
    if (p && quadro(p) === 'lateral') return true;
    return !!NEO.subir(el, CHROME);
  }};

  // ── AVISOS POR CIMA DA PÁGINA ──────────────────────────────────────────
  // Toast, snackbar, a faixa "lembre-se de finalizar": a página fala com quem
  // está usando por uma camada por cima, e essa camada costuma ser o ÚLTIMO
  // filho do <body>, ou seja, o último texto da leitura e o primeiro a cair no
  // teto. No job 21c563c1 (Portal) "Você respondeu todas as questões, lembre-se
  // de Finalizar o questionário" ficou na tela por oito passos sem chegar ao
  // modelo, que repetiu a questão 10 até o PARE. Por isso o aviso sai do texto
  // corrido e ganha seção própria.
  //
  // Dois sinais, sem biblioteca: região viva declarada (`role=alert|status`,
  // `aria-live`) e camada `fixed` presa ao <body> que FALA em vez de navegar
  // (pouco texto, no máximo três controles). A barra do topo do site também é
  // `fixed`, mas é feita de links e fica de fora; menu e diálogo já têm a seção
  // de painel.
  const AVISO_VIVO = '[role=alert],[role=status],[aria-live=polite],[aria-live=assertive]';
  const CONTROLES = 'a[href],button,input,select,textarea,[role=button],[role=link],[role=menuitem]';
  const candAviso = new Set(NEO.todos(AVISO_VIVO, raizes));
  const fila = document.body ? [...document.body.children].map((e) => [e, 0]) : [];
  for (let i = 0; i < fila.length && i < 400; i++) {{
    const [e, n] = fila[i];
    if (e.matches(NEO.NOSSOS)) continue;
    const s = cs(e);
    if (s && s.position === 'fixed') {{ candAviso.add(e); continue; }}
    if (n < 2) for (const c of e.children) fila.push([c, n + 1]);
  }}
  // Novo = texto que este elemento não mostrava no snapshot anterior. A chave é
  // o par elemento+texto porque a biblioteca reaproveita o recipiente para o
  // aviso seguinte. Na primeira leitura de uma página nada é "novo".
  const avisosAntes = NEO.avisosVistos;
  NEO.avisosVistos = new WeakMap();
  const avisos = [];
  for (const el of candAviso) {{
    if (el.matches(NEO.NOSSOS) || el.closest(OPEN)) continue;
    if (!visivel(el)) continue;
    const r = box(el);
    if (!(r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth)) continue;
    if (r.width * r.height > innerWidth * innerHeight * 0.5) continue;
    if (r.height >= innerHeight * 0.4 && r.width <= innerWidth * 0.4) continue;
    const texto = String(el.innerText || '').replace(/\s*\n\s*/g, ' · ').replace(/\s+/g, ' ').trim();
    if (texto.length < 3 || texto.length > 500) continue;
    if (el.querySelectorAll(CONTROLES).length > 3) continue;
    if (avisos.some((a) => NEO.contem(a.el, el))) continue;
    for (let i = avisos.length - 1; i >= 0; i--) if (NEO.contem(el, avisos[i].el)) avisos.splice(i, 1);
    avisos.push({{ el, texto, nova: !!avisosAntes && avisosAntes.get(el) !== texto }});
  }}
  for (const a of avisos) NEO.avisosVistos.set(a.el, a.texto);
  const foraDaTela = (el) => avisos.some((a) => NEO.contem(a.el, el)) || NEO.foraDaLeitura(el);
  const leitura = NEO.ler(foraDaTela, {{ recorte: true }});
  const TETO_TELA = {chars} + 2000;
  const barras = [];
  for (const [p, q] of quadros) {{
    if (q !== 'lateral') continue;
    barras.push({{
      rotulo: NEO.shortLabel(p),
      texto: String(p.innerText || '').replace(/\s*\n\s*/g, ' · ').trim().slice(0, 300),
    }});
  }}
  // IMAGENS. O texto da página não traz endereço de imagem, e nenhuma ferramenta
  // devolvia: "a URL da foto principal" era impossível por construção. O cérebro
  // pede isso em toda coleta de produto, porque só mostra no chat a imagem que
  // algum resultado de tool trouxe. No job aa4c89f8 o modelo gastou 32 de 39
  // decisões procurando um dado que nenhuma leitura podia devolver.
  const imagens = [];
  for (const img of NEO.todos('img', raizes)) {{
    const r = box(img);
    if (r.width < 100 || r.height < 100) continue;
    if (!(r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth)) continue;
    if (!visivel(img)) continue;
    const url = NEO.srcDe(img);
    if (!url || url.startsWith('data:')) continue;
    imagens.push({{
      url: url.slice(0, 300),
      alt: String(img.alt || '').trim().slice(0, 70),
      w: Math.round(r.width),
      h: Math.round(r.height),
    }});
  }}
  imagens.sort((a, b) => b.w * b.h - a.w * a.h);
  // IFRAME. Nem `innerText` nem `querySelectorAll` entram nele, então checkout,
  // formulário embutido e player de outro domínio eram tela branca sem aviso:
  // o modelo só podia concluir que a informação não existia. O mesmo domínio dá
  // para ler daqui; o de outro domínio, não, mas o endereço dele dá para abrir.
  const iframes = [];
  for (const f of NEO.todos('iframe', raizes)) {{
    if (iframes.length >= 4) break;
    const r = f.getBoundingClientRect();
    if (r.width * r.height < innerWidth * innerHeight * 0.1) continue;
    if (!(r.top < innerHeight && r.bottom > 0 && r.left < innerWidth && r.right > 0)) continue;
    let texto = '';
    let legivel = false;
    try {{
      const b = f.contentDocument && f.contentDocument.body;
      if (b) {{ texto = String(b.innerText || '').trim().slice(0, 2000); legivel = true; }}
    }} catch (_) {{ legivel = false; }}
    iframes.push({{ url: String(f.src || '').slice(0, 300), titulo: f.title || '', legivel, texto }});
  }}
  // ── estado REAL da mídia ───────────────────────────────────────────────
  // O rótulo do botão de play/pause nomeia a AÇÃO, não o estado: o YouTube
  // escreve "Pausa" JUSTAMENTE quando o vídeo está tocando. O modelo leu isso ao
  // contrário e, como o botão é um INTERRUPTOR, cada leitura errada inverteu o
  // que estava valendo — play, pause, play, pause por 25 passos, com o usuário
  // vendo o vídeo piscar (job 717a8fbb). Estado não se deduz de rótulo: sai do
  // próprio elemento.
  const tempo = (s) => {{
    if (!isFinite(s) || s < 0) return '?';
    const mm = Math.floor(s / 60), ss = Math.floor(s % 60);
    return mm + ':' + String(ss).padStart(2, '0');
  }};
  let midia = null;
  const players = [...document.querySelectorAll('video,audio')].filter(
    (el) => el.currentSrc || el.src || el.duration > 0
  );
  // A maior área ganha: o player principal, não o preview do vídeo sugerido.
  players.sort((a, b) => {{
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return rb.width * rb.height - ra.width * ra.height;
  }});
  if (players[0]) {{
    const el = players[0];
    midia = {{
      tipo: el.tagName.toLowerCase(),
      tocando: !el.paused && !el.ended,
      mudo: !!el.muted || el.volume === 0,
      pos: tempo(el.currentTime),
      dur: tempo(el.duration),
    }};
  }}
  return {{
    url: location.href,
    title: document.title || '',
    ready: document.readyState,
    stop: document.documentElement.getAttribute('data-neo-stop') === '1',
    scroll: {{ y: Math.round(scrollY), height: Math.round(document.body ? document.body.scrollHeight : 0) }},
    // ORÇAMENTO. Eram dois literais (2200 para o principal, 1500 para o corpo)
    // e as DUAS janelas começavam no caractere 0 da MESMA string: 3700
    // caracteres de prompt entregando ~2400 únicos — e no job 29c739d4, ZERO
    // úteis, porque o prefixo comum era a moldura inteira da Amazon. Pior: o
    // `text_chars` que o core manda era ignorado sempre que havia principal,
    // então não existia alavanca nenhuma.
    //
    // Agora os dois saem de `text_chars` e o corpo vai INTEIRO até o
    // orçamento. Pré-cortá-lo em 1500 era justamente o que impedia a subtração
    // no driver de funcionar: o que sobrava era subconjunto do `main`, o
    // resultado vinha vazio, caía abaixo do piso de segurança e a moldura
    // voltava DUPLICADA. A subtração continua morando num lugar só — o driver,
    // que já a fazia e já tem o histórico dela.
    text: corpoLimpo.slice(0, {chars}),
    // Houve corte? O driver precisa saber para não afirmar ao modelo que o
    // snapshot traz a página inteira — foi essa afirmação falsa que realimentou
    // o giro do job 29c739d4, cinco vezes.
    text_cut: corpoLimpo.length > {chars},
    main: principal
      ? {{
          label: NEO.shortLabel(principal.el),
          text: principal.texto.slice(0, {chars}),
          cut: principal.texto.length > {chars},
        }}
      : null,
    // O que está NA JANELA agora e o que existe fora dela. `tela_total` é o que
    // permite ao driver dizer de onde o `extract` com `de` continua a leitura.
    tela: leitura.tela.slice(0, TETO_TELA),
    tela_cut: leitura.tela.length > TETO_TELA,
    tela_total: leitura.tela.length,
    resto: leitura.resto.slice(0, {chars}),
    resto_total: leitura.resto.length,
    avisos: avisos.slice(0, 4).map((a) => ({{ texto: a.texto, nova: a.nova }})),
    barras,
    iframes,
    // Carimbo do que este device sabe fazer. O core sobe sozinho e o device só
    // depois de reiniciar o `tauri dev`: sem isto o core oferece ação que aqui
    // ainda não existe.
    capacidades: ['find', 'clique_real'],
    imagens: imagens.slice(0, 8),
    truncated: omitidos > 0 || elements.length >= {max},
    omitidos,
    midia,
    elements,
  }};
}})()"#,
        helpers = JS_HELPERS,
        max = max_elements,
        chars = text_chars
    )
}

/// Ações que mudam alguma coisa. Passam pelo portão do PARE.
const ATUA: [&str; 8] = [
    "browser.navigate", "browser.click", "browser.click_text", "browser.dblclick",
    "browser.right_click", "browser.fill", "browser.select", "browser.press",
];

/// O PARE chegou enquanto o modelo decidia? O snapshot só lê no começo da rodada,
/// e uma decisão leva segundos: no job 8bc25dd1 ele foi apertado às 19:49:54 e um
/// clique ainda saiu às 19:50:11. Lê o atributo porque é a via confiável, e porque
/// o `eval` é também o que consome a notificação `neo.stop`.
async fn parado(id: i64) -> bool {
    let atributo = eval(id, "document.documentElement.getAttribute('data-neo-stop') === '1'").await;
    matches!(atributo, Ok(Value::Bool(true))) || stop_requested()
}

/// O que o core manda conferir no alvo REAL do clique — ver `NEO.preparar`.
fn trava_de(args: &Value) -> String {
    json!({
        "trava": args.get("trava").and_then(|v| v.as_str()).unwrap_or(""),
        "liberado": args.get("liberado").and_then(|v| v.as_str()).unwrap_or(""),
    })
    .to_string()
}

/// Primeira metade de um clique por `ref`: mira, arma a sonda e devolve o pixel.
/// `tipo` é esquerdo, duplo ou direito.
fn js_mirar(reference: i64, tipo: &str, trava: &str) -> String {
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const el = NEO.el({reference});
  if (!el) return {{ ok: false, error: 'stale' }};
  return NEO.preparar(el, {{ ...{trava}, tipo: '{tipo}' }});
}})()"#,
        helpers = JS_HELPERS,
        reference = reference,
        tipo = tipo,
        trava = trava
    )
}

/// Clique por NOME — a saída para o que se lê e não se consegue endereçar.
///
/// Existe porque todo inventário de elementos é uma aposta, e aposta erra: no
/// job 9968e68a as 5 alternativas do questionário não tinham `ref` nenhum, o
/// modelo de visão LEU as cinco corretamente e, obrigado a citar um número que
/// não existia, inventou — clicou no menu lateral cinco vezes. Com esta ação a
/// pergunta deixa de ser "qual o número?" e passa a ser "qual o texto?", que é
/// verificável e que o modelo sempre tem.
///
/// Quem acha é a varredura da DOM (`NEO.candidatos`): nome acessível, não só nó
/// de texto. `n` escolhe outro candidato da lista que o `find` mostrou.
fn js_click_text(needle: &str, n: i64, trava: &str) -> String {
    let encoded = serde_json::to_string(needle).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const procurado = {needle};
  if (NEO.normaliza(procurado).length < 3) return {{ ok: false, error: 'texto curto demais para achar com segurança (mínimo 3 letras)' }};
  const {{ lista }} = NEO.candidatos(procurado);
  if (!lista.length) {{
    return {{ ok: false, error: 'não achei esse texto na página. Confira a grafia, ou role até a parte onde ele está' }};
  }}
  const clicaveis = lista.filter((c) => !c.escondido && !c.desabilitado);
  const escolhido = {n} > 0 ? lista[{n} - 1] : clicaveis[0];
  if (!escolhido) {{
    return {{
      ok: false,
      error: {n} > 0
        ? 'não existe o candidato ' + {n} + ': a lista tem ' + lista.length
        : 'isso existe na página mas está ESCONDIDO ou desabilitado agora — abra o menu, aba ou seção que contém, ou faça o que libera o controle',
    }};
  }}
  if (escolhido.escondido) return {{ ok: false, error: 'esse candidato está ESCONDIDO agora — abra o que o contém primeiro' }};
  if (escolhido.coberto) {{
    // Coberto = o usuário não conseguiria clicar aqui. Despachar o evento assim
    // mesmo devolveria "ok" com a página inerte, que é a mentira que este
    // desenho inteiro combate.
    return {{
      ok: false,
      error: 'esse texto está COBERTO por "' + escolhido.coberto +
        '" — feche ou dispense o que está na frente antes de clicar',
    }};
  }}
  return NEO.preparar(escolhido.el, {{ ...{trava}, tipo: 'esquerdo', proprio: true, achados: clicaveis.length }});
}})()"#,
        helpers = JS_HELPERS,
        needle = encoded,
        n = n,
        trava = trava
    )
}

/// A varredura da DOM inteira, para o modelo ler. `NEO.candidatos` é a mesma que
/// o `click_text` usa: o número que sai daqui é o `n` que ele aceita.
fn js_find(texto: &str, limite: usize) -> String {
    let encoded = serde_json::to_string(texto).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const procurado = {texto};
  if (NEO.normaliza(procurado).length < 3) return {{ ok: false, error: 'termo curto demais (mínimo 3 letras)' }};
  const {{ lista, fechados, parou }} = NEO.candidatos(procurado);
  return {{
    ok: true,
    total: lista.length,
    frames_fechados: fechados,
    cortado: parou,
    lista: lista.slice(0, {limite}).map((c) => {{
      const ref = NEO.refs ? NEO.refs.indexOf(c.el) : -1;
      const tipo = c.el.getAttribute('type') || c.el.getAttribute('role');
      // O que distingue este de um homônimo: o primeiro contêiner com outro nome.
      let em;
      for (let p = c.el.parentElement, i = 0; p && p !== document.body && i < 10; p = p.parentElement, i++) {{
        const nome = NEO.shortLabel(p);
        if (nome && NEO.normaliza(nome) !== NEO.normaliza(c.nome)) {{ em = nome.slice(0, 50); break; }}
      }}
      return {{
        n: c.n, tag: c.el.tagName.toLowerCase(), tipo: tipo || undefined, ref: ref >= 0 ? ref : undefined,
        nome: c.nome.slice(0, 90), fonte: c.fonte,
        recebe: c.recebe, fora: c.fora, escondido: c.escondido, coberto: c.coberto,
        desabilitado: c.desabilitado, em,
      }};
    }}),
  }};
}})()"#,
        helpers = JS_HELPERS,
        texto = encoded,
        limite = limite
    )
}

/// O evento chegou na página? Não recebe os helpers de propósito: depois de uma
/// navegação `window.__neo` não existe mais, e é assim que se sabe que o clique
/// foi tão bem-sucedido que levou a página embora.
const JS_CLIQUE_CONFIRMA: &str = r#"(async () => {
  const NEO = window.__neo;
  const c = NEO && NEO.clique;
  if (!c) return { ok: true, entregue: null };
  if (!c.recebeu) return { ok: true, entregue: false };
  return { ...(await NEO.fimDoCliqueAssentado(c, 'pixel')), entregue: true };
})()"#;

const JS_CLIQUE_DESPACHA: &str = r#"(async () => {
  const NEO = window.__neo;
  const c = NEO && NEO.clique;
  if (!c) return { ok: false, error: 'stale' };
  NEO.despachar(c);
  return await NEO.fimDoCliqueAssentado(c, 'js');
})()"#;

/// O mouse de verdade, pelo CDP. O navegador faz o teste de acerto, o site recebe
/// `isTrusted` e os eventos de hover vêm junto. É o mesmo motivo do `press_key`:
/// há formulário que só reage ao que é nativo.
async fn mouse_real(id: i64, x: f64, y: f64, vezes: u32) -> Result<(), String> {
    let evento = |tipo: &str, botao: &str, botoes: u32, contagem: u32| {
        json!({
            "type": tipo, "x": x, "y": y, "button": botao,
            "buttons": botoes, "clickCount": contagem,
        })
    };
    cdp(id, "Input.dispatchMouseEvent", evento("mouseMoved", "none", 0, 0)).await?;
    for n in 1..=vezes {
        cdp(id, "Input.dispatchMouseEvent", evento("mousePressed", "left", 1, n)).await?;
        cdp(id, "Input.dispatchMouseEvent", evento("mouseReleased", "left", 0, n)).await?;
    }
    Ok(())
}

/// Os dois tempos de um clique: mira na página, dispara por fora dela e confere
/// com a sonda. Evento fabricado só entra quando a sonda prova que nada chegou:
/// sem essa prova, a reserva viraria clique dobrado, e clique dobrado num
/// "Adicionar ao carrinho" é item em dobro.
async fn clicar(id: i64, mira: String, vezes: u32) -> Result<Value, String> {
    let alvo = unwrap_ok(eval(id, &mira).await?)?;
    if alvo.get("real").and_then(|v| v.as_bool()) == Some(true) {
        let x = alvo.get("x").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let y = alvo.get("y").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let enviado = mouse_real(id, x, y, vezes).await;
        if let Err(e) = &enviado {
            warn!("[BrowserLocal] mouse real falhou, vou conferir a sonda: {}", e);
        }
        match eval(id, JS_CLIQUE_CONFIRMA).await {
            Ok(r) => match r.get("entregue").and_then(|v| v.as_bool()) {
                Some(true) => return Ok(r),
                Some(false) => {}
                // Sem `window.__neo`: a página trocou, logo o clique chegou.
                None => return Ok(json!({
                    "ok": true,
                    "url": alvo.get("url").cloned().unwrap_or(Value::Null),
                    "label": alvo.get("label").cloned().unwrap_or(Value::Null),
                    "alvo": alvo.get("alvo").cloned().unwrap_or(Value::Null),
                    "achados": alvo.get("achados").cloned().unwrap_or(Value::Null),
                    "via": "pixel",
                })),
            },
            // Contexto destruído no meio da navegação: mesma leitura de cima.
            Err(_) if enviado.is_ok() => return Ok(json!({
                "ok": true,
                "url": alvo.get("url").cloned().unwrap_or(Value::Null),
                "label": alvo.get("label").cloned().unwrap_or(Value::Null),
                "alvo": alvo.get("alvo").cloned().unwrap_or(Value::Null),
                "achados": alvo.get("achados").cloned().unwrap_or(Value::Null),
                "via": "pixel",
            })),
            Err(e) => return Err(e),
        }
    }
    unwrap_ok(eval(id, JS_CLIQUE_DESPACHA).await?)
}

// ─── abas que a página abre ─────────────────────────────────────────────────
//
// Job 3512cdd2 (2026-11-02): o anúncio do AliExpress abriu numa aba nova, o
// navegador foi para ela, e o resultado do clique dizia só "ok" — o agente
// seguiu na busca achando que o clique não pegou. Ver
// `neo-core/docs/browser-multi-aba-plan.md` §4.

/// Espera curta depois da ação: o `Page.windowOpen` chega em 9–31 ms (F0).
const ESPERA_JANELA: Duration = Duration::from_millis(80);
/// Teto para a aba nascer depois de um `windowOpen` (medido: 40–90 ms).
const TETO_ABA_NOVA: Duration = Duration::from_millis(1000);

/// As filhas de `id` que nasceram desde a última pergunta, já conectadas.
///
/// `None` = o backend não sabe dizer (extensão anterior à 0.2.2, sem o método).
/// É diferente de "nenhuma": com `None` não se espera nem se afirma nada — dizer
/// "a janela não abriu" quando ela abriu e só não foi relatada seria mentir.
async fn tomar_abas_novas(id: i64) -> Option<Vec<Value>> {
    let cruas: Vec<(i64, String, String)> = if is_headless(id) {
        super::headless::take_new_tabs(-id)
            .await
            .ok()?
            .into_iter()
            .map(|(n, url, title)| (-n, url, title))
            .collect()
    } else {
        match call("neoTakeNewTabs", json!({ "openerTabId": id })).await {
            Ok(Value::Array(lista)) => lista
                .iter()
                .filter_map(|t| {
                    Some((
                        t.get("id")?.as_i64()?,
                        t.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                        t.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    ))
                })
                .collect(),
            Ok(_) => Vec::new(),
            Err(e) => {
                log::debug!("[BrowserLocal] extensão sem neoTakeNewTabs: {}", e);
                return None;
            }
        }
    };
    let mut novas = Vec::new();
    for (tab, url, title) in cruas {
        if !is_headless(tab) {
            if let Err(e) = call("attach", json!({ "tabId": tab })).await {
                warn!("[BrowserLocal] aba nova {} não aceitou o debugger: {}", tab, e);
                continue;
            }
        }
        let _ = cdp(tab, "Page.enable", json!({})).await;
        info!("[BrowserLocal] a aba {} abriu a aba {} ({})", id, tab, url);
        novas.push(json!({ "tab_ref": tab, "url": url, "title": title, "aberta_por": id }));
    }
    Some(novas)
}

/// Anexa ao resultado de uma ação as abas que ela abriu.
///
/// `abas_novas`: as filhas, já prontas para receber comando. `janela_sem_aba`:
/// a página pediu janela e nenhuma nasceu — com `gesto: false` é o bloqueador de
/// popup do navegador. Sem `windowOpen`, volta na hora: nenhum clique espera.
async fn com_abas_novas(id: i64, mut resultado: Value) -> Value {
    tokio::time::sleep(ESPERA_JANELA).await;
    let prazo = Instant::now() + TETO_ABA_NOVA;
    let extra = loop {
        // Também é a chamada que bombeia as notificações do socket.
        let Some(novas) = tomar_abas_novas(id).await else {
            break Value::Null;
        };
        if !novas.is_empty() {
            break json!({ "abas_novas": novas });
        }
        let pedidas = janelas_de(id);
        let Some(primeira) = pedidas.first().cloned() else {
            break Value::Null;
        };
        if Instant::now() >= prazo {
            break json!({ "janela_sem_aba": { "url": primeira.url, "gesto": primeira.gesto } });
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    };
    esquecer_janelas(id);
    if let (Some(obj), Some(mais)) = (resultado.as_object_mut(), extra.as_object()) {
        for (k, v) in mais {
            obj.insert(k.clone(), v.clone());
        }
    }
    resultado
}

/// Todas as abas da sessão do agente, com origem e quem abriu.
async fn abas_da_sessao(id: i64) -> Result<Value, String> {
    if is_headless(id) {
        let abas: Vec<Value> = super::headless::live_tabs()
            .await?
            .into_iter()
            .map(|(n, url, title, opener)| {
                json!({
                    "tab_ref": -n,
                    "url": url,
                    "title": title,
                    "aberta_por": opener.map(|o| -o),
                })
            })
            .collect();
        return Ok(json!({ "tabs": abas }));
    }
    let lista = call("neoSessionTabs", json!({})).await?;
    let abas: Vec<Value> = lista
        .as_array()
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .map(|t| {
            json!({
                "tab_ref": t.get("id").cloned().unwrap_or(Value::Null),
                "url": t.get("url").cloned().unwrap_or(Value::Null),
                "title": t.get("title").cloned().unwrap_or(Value::Null),
                "origem": t.get("origin").cloned().unwrap_or(Value::Null),
                "aberta_por": t.get("openerTabId").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();
    Ok(json!({ "tabs": abas }))
}

// ─── downloads ──────────────────────────────────────────────────────────────

/// O que chegou na PASTA de downloads nos últimos `seconds` (sensor de disco).
///
/// A barra de download é do NAVEGADOR, não da página: nenhum `snapshot` ou
/// `wait_for` jamais vai enxergá-la. Sem esta ação o modelo não tem como saber
/// se o arquivo baixou — em 2026-08-29 ele clicou "Baixar" três vezes e gastou
/// 20 dos 25 passos esperando um "Download completo" que nunca apareceria no
/// DOM.
///
/// Hoje esta função é o SEGUNDO sensor: quem responde primeiro é o próprio
/// navegador (`downloads_merged`). O disco continua aqui porque é o único que
/// enxerga arquivo que não veio por download (movido na mão, baixado por outro
/// navegador) e porque é o fallback quando a extensão está fora do ar.
fn list_downloads(seconds: u64) -> Result<Value, String> {
    let dir = dirs::download_dir().ok_or_else(|| "não achei a pasta de downloads".to_string())?;
    let entries = std::fs::read_dir(&dir).map_err(|e| format!("não consegui ler {:?}: {}", dir, e))?;
    let now = std::time::SystemTime::now();

    let mut files: Vec<(u64, Value)> = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with('.') {
            continue;
        }
        let Ok(meta) = entry.metadata() else { continue };
        if meta.is_dir() {
            continue;
        }
        let Ok(modified) = meta.modified() else { continue };
        let Ok(age) = now.duration_since(modified) else { continue };
        let age = age.as_secs();
        if age > seconds {
            continue;
        }
        // Extensão temporária = o navegador ainda está escrevendo. Chamar isso
        // de "baixado" seria o falso sucesso de novo, só que em outro lugar.
        let lower = name.to_lowercase();
        let partial = [".crdownload", ".download", ".part", ".tmp"]
            .iter()
            .any(|ext| lower.ends_with(ext));
        files.push((
            age,
            json!({
                "name": name,
                "bytes": meta.len(),
                "seconds_ago": age,
                "incomplete": partial,
            }),
        ));
    }
    files.sort_by_key(|(age, _)| *age);
    let listed: Vec<Value> = files.into_iter().take(10).map(|(_, v)| v).collect();
    Ok(json!({
        "ok": true,
        "dir": dir.to_string_lossy(),
        "count": listed.len(),
        "files": listed,
    }))
}

/// Tira o sufixo temporário do nome (`Print.zip.crdownload` → `Print.zip`).
///
/// Serve para casar a linha do disco com o item do navegador: enquanto baixa, o
/// Chrome escreve em `nome.crdownload` mas o `filename` da API já é o nome
/// FINAL. Sem isso o mesmo download apareceria duas vezes na lista.
fn strip_partial_suffix(name: &str) -> String {
    let lower = name.to_lowercase();
    for ext in [".crdownload", ".download", ".part", ".tmp"] {
        if lower.ends_with(ext) {
            return name[..name.len() - ext.len()].to_string();
        }
    }
    name.to_string()
}

/// Downloads vistos pelos DOIS sensores: o navegador primeiro, o disco depois.
///
/// Por que o navegador primeiro: a pasta não distingue "o clique não virou
/// download" de "o servidor ainda está zipando" — nos dois casos ela está
/// igual, vazia. É essa ambiguidade que fazia o agente reclicar em "Baixar"
/// (jobs 2c8cc496, 6142ed04, 0f87265f). O `chrome.downloads` cria o item no
/// instante em que o Chrome aceita o download, muito antes do primeiro byte, e
/// carrega `state`/`error`/`paused` — "começou" vira evento, e "falhou" ganha
/// motivo em vez de virar silêncio.
///
/// O disco continua na resposta por duas razões: ele enxerga arquivo que não
/// veio por download, e é o fallback inteiro quando a extensão não responde. O
/// campo `sensor` diz quem respondeu, para o core saber o quanto pode concluir.
async fn downloads_merged(seconds: u64, ids: Value) -> Result<Value, String> {
    // A pasta é lida sempre: além dos arquivos, é dela que sai o `dir`, única
    // fonte confiável da pasta de download DESTA máquina (o core usa para montar
    // o caminho do artefato em vez de adivinhar a partir do nome).
    let disco = list_downloads(seconds);

    let mut params = json!({ "since_s": seconds });
    if let Some(lista) = ids.as_array() {
        if !lista.is_empty() {
            params["ids"] = Value::Array(lista.clone());
        }
    }

    let navegador = match call("searchDownloads", params).await {
        Ok(v) => v,
        Err(e) => {
            // Extensão antiga (sem o método) ou navegador fechado. Não é erro
            // do turno: o disco responde igual ao que respondia antes.
            info!("[BrowserLocal] searchDownloads indisponível, caindo pro disco: {}", e);
            let mut fallback = disco?;
            fallback["sensor"] = json!("disk");
            return Ok(fallback);
        }
    };

    let items = navegador
        .get("items")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    // Nome final de cada download que o navegador conhece — o filtro do disco.
    let conhecidos: std::collections::HashSet<String> = items
        .iter()
        .filter_map(|i| i.get("name").and_then(|v| v.as_str()))
        .map(|n| n.to_string())
        .collect();

    let (dir, mut files) = match disco {
        Ok(v) => {
            let dir = v
                .get("dir")
                .and_then(|d| d.as_str())
                .unwrap_or_default()
                .to_string();
            let sobras: Vec<Value> = v
                .get("files")
                .and_then(|f| f.as_array())
                .map(|arr| {
                    arr.iter()
                        .filter(|f| {
                            let name = f.get("name").and_then(|n| n.as_str()).unwrap_or_default();
                            !conhecidos.contains(&strip_partial_suffix(name))
                        })
                        .cloned()
                        .collect()
                })
                .unwrap_or_default();
            (dir, sobras)
        }
        // Pasta ilegível não invalida o que o navegador contou.
        Err(e) => {
            warn!("[BrowserLocal] pasta de downloads ilegível: {}", e);
            (String::new(), Vec::new())
        }
    };

    // Navegador na frente: é a evidência mais forte e a lista é lida de cima.
    let mut todos = items;
    todos.append(&mut files);
    todos.truncate(10);

    Ok(json!({
        "ok": true,
        "sensor": "browser",
        "dir": dir,
        "count": todos.len(),
        "files": todos,
    }))
}

// ─── marcações desenhadas na imagem ─────────────────────────────────────────

/// Dígitos 3x5. Um "font" de 10 linhas evita puxar renderização de texto de
/// verdade só para escrever números de dois dígitos numa etiqueta.
const GLYPHS: [[u8; 5]; 10] = [
    [0b111, 0b101, 0b101, 0b101, 0b111], // 0
    [0b010, 0b110, 0b010, 0b010, 0b111], // 1
    [0b111, 0b001, 0b111, 0b100, 0b111], // 2
    [0b111, 0b001, 0b111, 0b001, 0b111], // 3
    [0b101, 0b101, 0b111, 0b001, 0b001], // 4
    [0b111, 0b100, 0b111, 0b001, 0b111], // 5
    [0b111, 0b100, 0b111, 0b101, 0b111], // 6
    [0b111, 0b001, 0b001, 0b001, 0b001], // 7
    [0b111, 0b101, 0b111, 0b101, 0b111], // 8
    [0b111, 0b101, 0b111, 0b001, 0b111], // 9
];

const MARK: [u8; 3] = [255, 45, 149];
const INK: [u8; 3] = [255, 255, 255];

fn put(img: &mut image::RgbImage, x: i64, y: i64, color: [u8; 3]) {
    if x < 0 || y < 0 || x >= img.width() as i64 || y >= img.height() as i64 {
        return;
    }
    img.put_pixel(x as u32, y as u32, image::Rgb(color));
}

fn fill(img: &mut image::RgbImage, x: i64, y: i64, w: i64, h: i64, color: [u8; 3]) {
    for dy in 0..h {
        for dx in 0..w {
            put(img, x + dx, y + dy, color);
        }
    }
}

/// Contorno de 2px — não preenche, senão esconde o que o modelo precisa ler.
fn outline(img: &mut image::RgbImage, x: i64, y: i64, w: i64, h: i64, color: [u8; 3]) {
    for t in 0..2 {
        fill(img, x + t, y + t, w - 2 * t, 1, color);
        fill(img, x + t, y + h - 1 - t, w - 2 * t, 1, color);
        fill(img, x + t, y + t, 1, h - 2 * t, color);
        fill(img, x + w - 1 - t, y + t, 1, h - 2 * t, color);
    }
}

fn digits(img: &mut image::RgbImage, x: i64, y: i64, scale: i64, value: i64) {
    let text = value.to_string();
    let mut cursor = x;
    for ch in text.chars() {
        let glyph = GLYPHS[ch.to_digit(10).unwrap_or(0) as usize];
        for (row, bits) in glyph.iter().enumerate() {
            for col in 0..3 {
                if bits & (1 << (2 - col)) != 0 {
                    fill(
                        img,
                        cursor + col * scale,
                        y + row as i64 * scale,
                        scale,
                        scale,
                        INK,
                    );
                }
            }
        }
        cursor += 4 * scale;
    }
}

/// Escreve os números dos `ref` em cima do print. Devolve `None` em qualquer
/// tropeço (jpeg estranho, viewport zerada): print sem marcação ainda é útil,
/// print nenhum não é.
fn draw_marks(jpeg: &[u8], rects: &[Value], css_width: f64, scale: f64) -> Option<Vec<u8>> {
    if (rects.is_empty() || css_width <= 0.0) && scale >= 0.999 {
        return None;
    }
    let decoded = image::load_from_memory_with_format(jpeg, image::ImageFormat::Jpeg).ok()?;
    let mut img = decoded.to_rgb8();

    // Reduzir AQUI e não no `clip` do CDP: pedir clip faz o Chrome relayoutar a
    // página para a captura, e o usuário vê a tela piscar e os elementos
    // mudarem de tamanho a cada passo (25 vezes por tarefa). Redimensionar o
    // JPEG depois não toca no navegador dele.
    if scale < 0.999 {
        let (w, h) = (
            ((img.width() as f64 * scale).round() as u32).max(1),
            ((img.height() as f64 * scale).round() as u32).max(1),
        );
        img = image::imageops::resize(&img, w, h, image::imageops::FilterType::Triangle);
    }
    if rects.is_empty() || css_width <= 0.0 {
        let mut out = Vec::with_capacity(jpeg.len());
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut std::io::Cursor::new(&mut out), 70)
            .encode(img.as_raw(), img.width(), img.height(), image::ExtendedColorType::Rgb8)
            .ok()?;
        return Some(out);
    }
    // A imagem pode vir em DPR 2 (retina) ou reduzida pelo `clip.scale` — o
    // fator sai da largura real contra a largura em px de CSS.
    let ratio = img.width() as f64 / css_width;
    let scale = ((ratio * 2.0).round() as i64).clamp(2, 6);
    let glyph_h = 5 * scale;

    for rect in rects {
        let values = rect.as_array()?;
        if values.len() < 5 {
            continue;
        }
        let number = values[0].as_i64().unwrap_or(-1);
        if number < 0 {
            continue;
        }
        let at = |i: usize| (values[i].as_f64().unwrap_or(0.0) * ratio).round() as i64;
        let (x, y, w, h) = (at(1), at(2), at(3), at(4));
        outline(&mut img, x, y, w, h, MARK);

        // Etiqueta acima do elemento; se não couber, por dentro dele.
        let label_w = (number.to_string().len() as i64) * 4 * scale + scale;
        let label_h = glyph_h + 2 * scale;
        let label_y = if y - label_h >= 0 { y - label_h } else { y };
        fill(&mut img, x, label_y, label_w, label_h, MARK);
        digits(&mut img, x + scale, label_y + scale, scale, number);
    }

    let mut out = Vec::with_capacity(jpeg.len());
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut std::io::Cursor::new(&mut out), 70)
        .encode(img.as_raw(), img.width(), img.height(), image::ExtendedColorType::Rgb8)
        .ok()?;
    Some(out)
}

/// Recorta o print num retângulo em px de CSS. Devolve `None` em qualquer
/// tropeço — print inteiro ainda serve, print nenhum não.
///
/// Existe para a TRIAGEM DE PAINEL: quando um modal aparece, a pergunta é "o que
/// está escrito aqui dentro?", e a tela inteira é o pior insumo possível para
/// isso. O print normal sai a `scale: 0.75, quality: 60` de uma tela de 2954px —
/// um modal de 600px vira texto ilegível. Aqui não há redução nem marcação: o
/// recorte sai na resolução nativa e com qualidade alta, porque o trabalho dele
/// é ser LIDO.
///
/// O recorte é feito no JPEG já capturado, nunca pelo `clip` do CDP — pedir clip
/// ao Chrome faz a página relayoutar e o usuário vê a tela piscar (é o mesmo
/// motivo documentado em `draw_marks`).
fn crop_jpeg(jpeg: &[u8], css_width: f64, rect: &[f64; 4]) -> Option<Vec<u8>> {
    if css_width <= 0.0 {
        return None;
    }
    let decoded = image::load_from_memory_with_format(jpeg, image::ImageFormat::Jpeg).ok()?;
    let img = decoded.to_rgb8();
    let ratio = img.width() as f64 / css_width;
    // Uma folga em volta: sem ela o X de fechar, que costuma morar em cima da
    // borda, sai cortado pela metade — e é justamente ele que a triagem precisa
    // descrever quando o veredito é "intruso".
    let pad = 12.0 * ratio;
    let x = ((rect[0] * ratio) - pad).max(0.0).round() as u32;
    let y = ((rect[1] * ratio) - pad).max(0.0).round() as u32;
    let w = ((rect[2] * ratio) + 2.0 * pad).round() as u32;
    let h = ((rect[3] * ratio) + 2.0 * pad).round() as u32;
    let w = w.min(img.width().saturating_sub(x));
    let h = h.min(img.height().saturating_sub(y));
    if w < 16 || h < 16 {
        return None;
    }
    let cortado = image::imageops::crop_imm(&img, x, y, w, h).to_image();
    let mut out = Vec::with_capacity(jpeg.len());
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut std::io::Cursor::new(&mut out), 82)
        .encode(
            cortado.as_raw(),
            cortado.width(),
            cortado.height(),
            image::ExtendedColorType::Rgb8,
        )
        .ok()?;
    Some(out)
}

/// Base64 padrão, nos dois sentidos. São ~30 linhas em vez de uma dependência
/// nova (mesmo critério do `base64_encode` em commands/mod.rs).
const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

fn b64_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | b[2] as u32;
        out.push(B64[(n >> 18) as usize & 63] as char);
        out.push(B64[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { B64[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { B64[n as usize & 63] as char } else { '=' });
    }
    out
}

fn b64_decode(text: &str) -> Option<Vec<u8>> {
    let mut table = [255u8; 256];
    for (i, c) in B64.iter().enumerate() {
        table[*c as usize] = i as u8;
    }
    let mut out = Vec::with_capacity(text.len() / 4 * 3);
    let (mut acc, mut bits) = (0u32, 0u32);
    for byte in text.bytes() {
        if byte == b'=' {
            break;
        }
        let value = table[byte as usize];
        if value == 255 {
            continue; // ignora espaço/quebra de linha
        }
        acc = (acc << 6) | value as u32;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
        }
    }
    Some(out)
}

/// Retângulos dos `ref` que estão na tela AGORA — leitura pura, sem tocar no
/// DOM. A primeira versão desenhava as marcações dentro da página e depois as
/// apagava: a tela do usuário piscava a cada passo e SPAs pesadas (o Drive é
/// uma) reagiam à mutação. Agora a página é só lida e quem desenha os números
/// é o Rust, em cima do JPEG — o navegador dele nunca fica marcado.
/// Termômetro de atividade de rede da página.
///
/// Responde à pergunta que separa "o clique não pegou" de "o servidor está
/// preparando o arquivo": desde o clique, a página BUSCOU alguma coisa? Site
/// que gera zip sob demanda dispara XHR/fetch e só depois emite o download —
/// nesse intervalo a pasta está vazia e o `chrome.downloads` ainda não tem item,
/// exatamente como quando o clique errou o alvo.
///
/// `resources` é o contador de recursos carregados e `last_response_end` o
/// relógio (em ms desde a navegação) da última resposta que chegou. Duas
/// leituras espaçadas dizem se a página continua puxando dados; uma leitura só
/// já diz se algo chegou agora há pouco.
const JS_NET: &str = r#"(() => {
  try {
    const list = performance.getEntriesByType('resource');
    let last = 0;
    for (const entry of list) {
      if (entry.responseEnd > last) last = entry.responseEnd;
    }
    return {
      ok: true,
      resources: list.length,
      last_response_end: Math.round(last),
      now: Math.round(performance.now()),
      ready_state: document.readyState
    };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
})()"#;

const JS_RECTS: &str = r#"(() => {
  const NEO = window.__neo;
  const rects = [];
  if (NEO && NEO.refs) {
    NEO.refs.forEach((el, ref) => {
      if (rects.length >= 150 || !el || !el.isConnected) return;
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) return;
      if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return;
      rects.push([ref, Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)]);
    });
  }
  return { ok: true, width: Math.round(innerWidth), rects };
})()"#;

fn js_fill(reference: i64, value: &str) -> String {
    let encoded = serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const rosto = NEO.el({reference});
  if (!rosto) return {{ ok: false, error: 'stale' }};
  const el = NEO.controleDe(rosto) || rosto;
  const value = {value};
  rosto.scrollIntoView({{ block: 'nearest' }});
  // Campo de LOGIN (e-mail, usuário, senha) não recebe foco. O gerenciador de
  // senhas do navegador abre o menu dele num iframe `chrome-extension://` quando
  // o campo é focado — e o Chrome solta o debugger de aba com página de outra
  // extensão, e recusa religar ("Cannot access a chrome-extension:// URL of
  // different extension"). Foi o "Debugger unattached" do cadastro do AppDemo, 5 de
  // 5 vezes logo depois de digitar o e-mail. O valor entra igual pelo setter
  // nativo + `input`/`change`, que é o que React e Vue escutam.
  const credencial = el.tagName === 'INPUT' && (
    /^(email|password)$/i.test(el.type || '') ||
    /(user|email|password)/i.test(el.getAttribute('autocomplete') || '') ||
    /(e-?mail|senha|password|passwd|login|usu[aá]rio|username)/i.test((el.name || '') + ' ' + (el.id || ''))
  );
  if (!credencial) el.focus();
  if (el.isContentEditable) {{
    el.textContent = value;
    NEO.fire(el, 'input');
    return {{ ok: true }};
  }}
  // Setter nativo: React/Vue ignoram atribuição direta em .value.
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
  if (setter && setter.set) setter.set.call(el, value); else el.value = value;
  NEO.fire(el, 'input');
  NEO.fire(el, 'change');
  return {{ ok: true, value: String(el.value || '').slice(0, 80) }};
}})()"#,
        helpers = JS_HELPERS,
        reference = reference,
        value = encoded
    )
}

fn js_select(reference: i64, option: &str) -> String {
    let encoded = serde_json::to_string(option).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const rosto = NEO.el({reference});
  if (!rosto) return {{ ok: false, error: 'stale' }};
  const el = NEO.controleDe(rosto) || rosto;
  if (el.tagName !== 'SELECT') return {{ ok: false, error: 'não é um <select>' }};
  const wanted = String({option}).trim().toLowerCase();
  const hit = Array.from(el.options).find(
    (o) => o.text.trim().toLowerCase() === wanted || String(o.value).toLowerCase() === wanted
  ) || Array.from(el.options).find((o) => o.text.trim().toLowerCase().includes(wanted));
  if (!hit) return {{ ok: false, error: 'opção não encontrada' }};
  el.value = hit.value;
  NEO.fire(el, 'input');
  NEO.fire(el, 'change');
  return {{ ok: true, selected: hit.text.trim() }};
}})()"#,
        helpers = JS_HELPERS,
        reference = reference,
        option = encoded
    )
}

/// Rolagem no container que REALMENTE rola.
///
/// `scrollBy` na janela não faz nada em app cujo conteúdo mora num painel com
/// `overflow:auto` — Google Drive é assim. Em 2026-08-29 o modelo pediu
/// scroll três vezes (600, 300, 600), a lista não saiu do lugar, o detector de
/// "a tela não mudou" barrou a repetição e o job morreu sem nunca ter chegado
/// na pasta. Aqui o alvo é o primeiro ancestral rolável a partir do centro da
/// tela (ou do `ref` pedido), e a janela é só o último recurso.
///
/// Rolagem que não move nada volta como ERRO de propósito: "ok" numa ação que
/// não fez nada é a mesma mentira do clique que não clica.
fn js_scroll(delta: i64, reference: i64) -> String {
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const rola = (el) => {{
    if (!el || el === document.body || el === document.documentElement) return false;
    const style = getComputedStyle(el);
    if (!/(auto|scroll|overlay)/.test(style.overflowY)) return false;
    return el.scrollHeight > el.clientHeight + 8;
  }};
  const subindo = (node) => {{
    while (node && node !== document.body && node !== document.documentElement) {{
      if (rola(node)) return node;
      node = node.parentElement;
    }}
    return null;
  }};

  // O ref apontado costuma ser um CONTROLE perto do painel, não o painel. No
  // job 9025d9f8 o modelo mandou rolar o botão da aba "Detalhes" (154×47px) 21
  // vezes: subindo dele não há scroller nenhum, e o painel que ele queria era
  // um irmão logo abaixo. Procurar também para BAIXO acerta esse caso sem
  // depender de o modelo apontar o nó exato.
  const descendo = (node) => {{
    if (!node) return null;
    const fila = [node];
    let visitados = 0;
    while (fila.length && visitados < 400) {{
      const atual = fila.shift();
      visitados += 1;
      if (atual !== node && rola(atual)) return atual;
      for (const filho of atual.children) fila.push(filho);
    }}
    return null;
  }};

  // Rolável VISÍVEL mais próximo de um retângulo. É o que traduz "role o painel
  // de detalhes" quando o modelo aponta o botão da aba que fica ACIMA dele:
  // sondando pontos da tela acha-se o scroller mesmo sendo irmão, e a distância
  // decide qual painel ele quis (job f27970d6, onde subir e descer do ref não
  // encontrava nada e a rolagem caía na janela, que já estava no fim).
  const perto = (alvoRect) => {{
    const achados = [];
    const visto = new Set();
    const noPonto = (x, y) => NEO.topo(x, y);
    for (let i = 1; i <= 5; i++) {{
      for (let j = 1; j <= 5; j++) {{
        let p = noPonto((innerWidth * i) / 6, (innerHeight * j) / 6);
        while (p && p !== document.body && p !== document.documentElement) {{
          if (visto.has(p)) break;
          visto.add(p);
          if (rola(p)) {{ achados.push(p); break; }}
          p = p.parentElement;
        }}
      }}
    }}
    if (!achados.length) return null;
    if (!alvoRect) return achados[0];
    const cx = alvoRect.left + alvoRect.width / 2;
    const cy = alvoRect.top + alvoRect.height / 2;
    let melhor = null, dist = Infinity;
    for (const el of achados) {{
      const r = el.getBoundingClientRect();
      // Distância do centro do ref ao retângulo do painel (0 se estiver dentro).
      const dx = Math.max(r.left - cx, 0, cx - r.right);
      const dy = Math.max(r.top - cy, 0, cy - r.bottom);
      const d = Math.hypot(dx, dy);
      if (d < dist) {{ melhor = el; dist = d; }}
    }}
    return melhor;
  }};

  let alvo = null;
  const pedido = {reference} >= 0 && NEO ? NEO.el({reference}) : null;
  if (pedido) alvo = subindo(pedido) || descendo(pedido);
  // Ainda nada: o painel costuma ser VIZINHO do que ele apontou, não parente.
  if (!alvo && pedido) alvo = descendo(pedido.parentElement) || perto(pedido.getBoundingClientRect());
  if (!alvo) alvo = subindo(NEO.topo(innerWidth / 2, innerHeight / 2));
  if (!alvo) alvo = perto(null);
  // Painel de conteúdo fora do centro (lista estreita, coluna lateral).
  if (!alvo) {{
    let melhor = null, area = 0;
    // Só papéis semânticos: varrer `div[class]` numa página como o Drive são
    // milhares de getComputedStyle e o passo estoura o timeout.
    // `[role=complementary]`/`aside`/`[role=tabpanel]` entraram por causa do
    // painel de detalhes do Drive, que é exatamente isso e não estava aqui.
    for (const el of document.querySelectorAll(
      'main,[role=main],[role=grid],[role=list],[role=listbox],[role=table],[role=tree],[role=feed],' +
      'aside,[role=complementary],[role=tabpanel],[role=dialog]'
    )) {{
      if (!rola(el)) continue;
      const r = el.getBoundingClientRect();
      const a = r.width * r.height;
      if (a > area) {{ melhor = el; area = a; }}
    }}
    alvo = melhor;
  }}

  // Onde a rolagem PAROU importa tanto quanto se ela andou: sem `de`, o modelo
  // não sabe se ainda falta conteúdo e continua rolando no escuro.
  if (alvo) {{
    const antes = alvo.scrollTop;
    alvo.scrollTop = antes + {delta};
    const fim = alvo.scrollHeight - alvo.clientHeight;
    if (Math.abs(alvo.scrollTop - antes) < 1) {{
      return {{
        ok: false,
        error:
          antes >= fim - 1
            ? 'o painel já está no FIM — o que você procura não está nele; leia o texto dele ou vá por outro caminho'
            : 'o painel recusou a rolagem programática — tente PageDown com ele focado, ou leia o texto dele',
      }};
    }}
    return {{ ok: true, y: Math.round(alvo.scrollTop), de: Math.round(fim), alvo: 'painel' }};
  }}

  const antes = scrollY;
  scrollBy(0, {delta});
  const fim = Math.max(0, document.documentElement.scrollHeight - innerHeight);
  if (Math.abs(scrollY - antes) < 1) {{
    return {{
      ok: false,
      error:
        antes >= fim - 1
          ? 'a página já está no FIM — não há mais nada abaixo'
          : 'não achei nada rolável a partir desse elemento — aponte o `ref` do PAINEL (ele vem marcado com "rola") ou leia o texto dele',
    }};
  }}
  return {{ ok: true, y: Math.round(scrollY), de: Math.round(fim), alvo: 'janela' }};
}})()"#,
        helpers = JS_HELPERS,
        delta = delta,
        reference = reference
    )
}

fn js_extract(reference: i64, chars: usize) -> String {
    format!(
        r#"{helpers};
(() => {{
  const el = window.__neo.el({reference});
  if (!el) return {{ ok: false, error: 'stale' }};
  // De ONDE veio o texto. Sem isto, leitura errada e leitura certa chegam
  // idênticas ao modelo — foi o que deixou ele repetir a mesma leitura inútil
  // sem ter como perceber.
  const bruto = String(el.innerText || el.textContent || '').replace(/\n{{3,}}/g, '\n\n').trim();
  // Imagem não tem texto: ler uma devolvia vazio, com ok, sem explicar nada.
  const foto = el.tagName === 'IMG' ? el : (bruto ? null : el.querySelector('img'));
  return {{
    ok: true,
    imagem: foto ? window.__neo.srcDe(foto) : '',
    alt: foto ? String(foto.alt || '') : '',
    label: window.__neo.shortLabel ? window.__neo.shortLabel(el) : '',
    // O rótulo sozinho NÃO bastou: no job 29c739d4 o modelo recebeu três vezes
    // 1500 caracteres de menu rotulados "Ir para" e não tinha como distinguir
    // leitura certa de leitura errada — chutou um `ref` do overlay de atalhos
    // de teclado e recebeu 18 caracteres sem nenhum sinal de erro. O flag já
    // era calculado por elemento no inventário; faltava chegar aqui.
    moldura: !!(el.closest && window.__neo.CHROME && el.closest(window.__neo.CHROME)),
    cortado: bruto.length > {chars},
    text: bruto.slice(0, {chars})
  }};
}})()"#,
        helpers = JS_HELPERS,
        reference = reference,
        chars = chars
    )
}

/// Lê a PÁGINA, sem menu, em janelas de `chars` a partir do caractere `de`.
///
/// É o que tira o teto do snapshot de cima do modelo. O aviso de corte mandava
/// "dar `extract` no `ref` do bloco", mas texto puro não tem `ref`: a saída que
/// o prompt oferecia não existia, e no job 45e85695 o modelo chutou `ref` 0 (um
/// skip-link) e 110 (uma imagem, que devolveu vazio). `body.innerText` também
/// não servia: sai em ordem de documento, com a moldura na frente, e no job
/// 6633351e isso foi lido cinco vezes. Agora tudo o que está escrito na página
/// é alcançável, sem rolar, e o texto continua de onde o snapshot parou.
///
/// Com modal aberto ele vem primeiro: é o que está na frente do usuário.
fn js_ler_pagina(de: usize, chars: usize) -> String {
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const fora = NEO.foraDaLeitura || ((el) => !!NEO.subir(el, NEO.CHROME));
  const l = NEO.ler(fora);
  const modal = NEO.modal && NEO.modal.isConnected
    ? String(NEO.modal.innerText || '').replace(/\n{{3,}}/g, '\n\n').trim()
    : '';
  const tudo = [modal, l.tela, l.resto].filter(Boolean).join('\n');
  const fim = {de} + {chars};
  return {{
    ok: true,
    label: 'a página, sem os menus',
    moldura: false,
    de: {de},
    total: tudo.length,
    proximo: fim < tudo.length ? fim : null,
    cortado: fim < tudo.length,
    text: tudo.slice({de}, fim)
  }};
}})()"#,
        helpers = JS_HELPERS,
        de = de,
        chars = chars
    )
}

/// Ctrl+F do agente: acha um termo no texto da página INTEIRA, moldura incluída,
/// e devolve cada trecho com a linha de antes e a de depois. Olha também o endereço
/// e o nome de links e imagens: "não achei" só pode ser dito se for verdade.
///
/// É a primitiva que mais economiza passo em site desconhecido. "Tem garantia?"
/// e "qual o CEP de entrega?" deixam de ser rolar e olhar até achar, e "não tem"
/// passa a ser uma resposta em vez de um passo a mais: o modelo morria no freio
/// de giro procurando o que não estava lá.
fn js_buscar(termo: &str, chars: usize) -> String {
    let encoded = serde_json::to_string(termo).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const alvo = NEO.normaliza({termo});
  if (alvo.length < 2) return {{ ok: false, error: 'termo curto demais para buscar' }};
  const l = NEO.ler(null);
  const linhas = [l.tela, l.resto].filter(Boolean).join('\n').split('\n');
  const usadas = new Set();
  const trechos = [];
  let achados = 0;
  linhas.forEach((linha, i) => {{
    if (!NEO.normaliza(linha).includes(alvo)) return;
    achados++;
    if (trechos.length >= 12 || usadas.has(i)) return;
    const pedaco = [];
    for (let k = Math.max(0, i - 1); k <= Math.min(linhas.length - 1, i + 1); k++) {{
      if (usadas.has(k)) continue;
      usadas.add(k);
      pedaco.push(linhas[k].slice(0, 240));
    }}
    trechos.push(pedaco.join('\n'));
  }});
  let enderecos = 0;
  for (const el of NEO.todos('img,a[href]')) {{
    if (enderecos >= 6) break;
    const ehImagem = el.tagName === 'IMG';
    const url = ehImagem ? NEO.srcDe(el) : String(el.href || '');
    const nome = String((ehImagem ? el.alt : el.title) || '');
    if (url.startsWith('data:') || !NEO.normaliza(url + ' ' + nome).includes(alvo)) continue;
    enderecos++;
    achados++;
    trechos.push((ehImagem ? 'imagem: ' : 'link: ') + (nome ? nome.slice(0, 80) + ' → ' : '') + url.slice(0, 240));
  }}
  return {{
    ok: true,
    busca: true,
    label: 'busca na página',
    moldura: false,
    achados,
    total: linhas.reduce((n, t) => n + t.length, 0),
    text: trechos.join('\n---\n').slice(0, {chars})
  }};
}})()"#,
        helpers = JS_HELPERS,
        termo = encoded,
        chars = chars
    )
}

/// Um evento para a linha do tempo do painel da extensão.
///
/// `kind` é o que dá cor e forma ao item: `fase` (preparação), `passo` (o que
/// está sendo feito agora), `ok` (o retorno), `erro`, `fim`. `detail` é o
/// retorno em si — a resposta da olhada com visão, o motivo da falha — e nasce
/// cortado em 3 linhas no painel, com clique para abrir.
fn js_banner(text: &str, active: bool, stopped: bool, kind: &str, detail: &str) -> String {
    let encoded = serde_json::to_string(text).unwrap_or_else(|_| "\"\"".to_string());
    let kind_enc = serde_json::to_string(kind).unwrap_or_else(|_| "\"passo\"".to_string());
    let detail_enc = serde_json::to_string(detail).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        r#"(() => {{
  window.postMessage({{ __neo_overlay: {{ active: {active}, text: {text}, stopped: {stopped}, kind: {kind}, detail: {detail} }} }}, '*');
  return {{ ok: true }};
}})()"#,
        active = active,
        stopped = stopped,
        text = encoded,
        kind = kind_enc,
        detail = detail_enc
    )
}

/// O painel da extensão fica na LATERAL, não mais numa faixa de 44px: deixá-lo
/// na foto come um quarto da largura útil e cobre conteúdo que o modelo de
/// visão precisa ler. Como o host do shadow é um nó comum da página (só o
/// conteúdo é que é `closed`), o mundo principal consegue apagá-lo pelo id —
/// síncrono, sem round-trip de mensagem e sem reflow (`visibility`, não
/// `display`).
///
/// `opacity` e não `visibility`: esconder com `visibility:hidden` um campo
/// FOCADO tira o foco dele (o navegador corrige o foco de quem deixa de ser
/// focável), e a cada print o Daniel perdia o cursor no meio da frase que
/// escrevia no painel. Com opacidade zero o painel some da foto igual e o foco
/// fica onde estava.
const JS_PAINEL_OCULTAR: &str = r#"(() => {
  const h = document.getElementById('neo-browser-local-overlay');
  if (h) h.style.opacity = '0';
  return { ok: true };
})()"#;

const JS_PAINEL_MOSTRAR: &str = r#"(() => {
  const h = document.getElementById('neo-browser-local-overlay');
  if (h) h.style.opacity = '';
  return { ok: true };
})()"#;

/// O Daniel está ESCREVENDO no painel da aba? O content script carimba
/// `data-neo-digitando` (epoch ms da página) a cada tecla e, com o campo
/// focado e rascunho, a cada segundo (até 30 s parado). Agir agora roubaria o
/// foco do campo dele — e uma tecla do agente cairia no rascunho.
const DIGITANDO_FRESCO_MS: f64 = 2500.0;
/// Quanto UMA ação espera aqui. O passo tem 15 s de prazo do lado do core;
/// passou disto, devolve `DIGITANDO:` e quem espera mais é o core.
const DIGITANDO_ESPERA: Duration = Duration::from_secs(8);

const JS_IDADE_DIGITANDO: &str = r#"(() => {
  const t = Number(document.documentElement.getAttribute('data-neo-digitando') || 0);
  return t ? Date.now() - t : 1e12;
})()"#;

/// Carimbo de "o agente vai agir agora": o painel tira o foco do campo do
/// Daniel (sem apagar o rascunho), e tecla que chegar logo depois não é dele.
const JS_AGINDO: &str =
    "document.documentElement.setAttribute('data-neo-agindo', String(Date.now()))";

async fn esperar_quem_digita(id: i64) -> Result<(), String> {
    if is_headless(id) {
        return Ok(());
    }
    let inicio = Instant::now();
    loop {
        let fresco = matches!(
            eval(id, JS_IDADE_DIGITANDO).await,
            Ok(Value::Number(ref n)) if n.as_f64().map(|v| v < DIGITANDO_FRESCO_MS).unwrap_or(false)
        );
        if !fresco {
            return Ok(());
        }
        if inicio.elapsed() >= DIGITANDO_ESPERA {
            return Err("DIGITANDO: o Daniel está escrevendo no painel da aba".to_string());
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
}

// ─── vocabulário ────────────────────────────────────────────────────────────

fn tab_id(args: &Value) -> Result<i64, String> {
    args.get("tab_ref")
        .or_else(|| args.get("tab_id"))
        .and_then(|v| v.as_i64())
        .ok_or_else(|| "faltou tab_ref (chame browser.claim antes)".to_string())
}

fn arg_str(args: &Value, key: &str) -> Result<String, String> {
    args.get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| format!("faltou o argumento '{}'", key))
}

fn arg_i64(args: &Value, key: &str) -> Result<i64, String> {
    args.get(key)
        .and_then(|v| v.as_i64())
        .ok_or_else(|| format!("faltou o argumento '{}'", key))
}

/// True se o usuário apertou PARE desde o último reset.
pub fn stop_requested() -> bool {
    STOP_REQUESTED.load(Ordering::SeqCst)
}

/// O device sabe dirigir o navegador local? (o host do open-browser-use está no ar)
pub fn available() -> bool {
    socket_path().is_ok()
}

/// Executa uma ação `browser.*` do canal de controle.
pub async fn execute(action: &str, args: &Value) -> Result<Value, String> {
    if ATUA.contains(&action) {
        let id = tab_id(args)?;
        if parado(id).await {
            return Err("PARE: o usuário mandou parar".to_string());
        }
        // Ele está escrevendo no painel: espera ele terminar antes de mexer na
        // página (neo-core/docs/browser-conversa-plan.md §9.3).
        esperar_quem_digita(id).await?;
        if !is_headless(id) {
            let _ = eval(id, JS_AGINDO).await;
        }
    }
    match action {
        // ── saúde / abas ──
        "browser.health" => {
            // O health TEM que responder pelo backend que a tarefa vai usar.
            //
            // Ele existia só para o caminho da extensão, e perguntava pelo
            // socket do host nativo. Numa tarefa headless isso é a pergunta
            // errada: o headless não depende de extensão nem de navegador
            // aberto. Como o runner confere a saúde ANTES de prometer, um
            // health preso à extensão matava todo job headless na largada —
            // com a mensagem errada ("a extensão não está ativa"), que manda o
            // usuário arrumar uma coisa que não é o problema.
            if args.get("mode").and_then(|v| v.as_str()) == Some("headless") {
                if !super::headless::available() {
                    return Err(
                        "não achei Chrome, Brave, Chromium nem Edge instalado nesta \
                         máquina — o navegador headless precisa de um deles"
                            .to_string(),
                    );
                }
                return Ok(json!({ "ok": true, "backend": "headless" }));
            }
            let info = call("getInfo", json!({})).await?;
            Ok(json!({ "ok": true, "backend": info }))
        }
        "browser.tabs" => {
            // "Abas do usuário" é uma pergunta sobre o navegador QUE ELE VÊ. No
            // headless não existem abas dele — existem as que o Neo abriu.
            if args.get("mode").and_then(|v| v.as_str()) == Some("headless") {
                return Ok(json!({ "tabs": [], "mode": "headless" }));
            }
            let tabs = call("getUserTabs", json!({})).await?;
            Ok(json!({ "tabs": tabs }))
        }

        // ── reivindicar a aba (é o que dá o tab_ref de todo o resto) ──
        // Estado do headless para a tela de Config (e para diagnóstico).
        "browser.headless_stats" => Ok(super::headless::stats().await),
        "browser.headless_shutdown" => {
            super::headless::shutdown().await;
            Ok(json!({ "ok": true }))
        }

        "browser.claim" => {
            STOP_REQUESTED.store(false, Ordering::SeqCst);
            let explicit = args.get("tab_id").and_then(|v| v.as_i64());
            let url = args.get("url").and_then(|v| v.as_str()).unwrap_or("");

            // Modo HEADLESS: mesma máquina, mesmo IP residencial, sem tomar a
            // tela do usuário. É o que resolve o bloqueio por origem sem o custo
            // de invadir o navegador que ele está usando.
            if args.get("mode").and_then(|v| v.as_str()) == Some("headless") {
                let alvo = if url.is_empty() { "about:blank" } else { url };
                let id = super::headless::open_tab(alvo).await?;
                let tab_ref = -id; // negativo = headless (ver `is_headless`)
                let _ = cdp(tab_ref, "Page.enable", json!({})).await;
                info!("[BrowserLocal] aba headless {} aberta (url={})", tab_ref, alvo);
                return Ok(json!({ "tab_ref": tab_ref, "mode": "headless" }));
            }

            let id = match explicit {
                Some(id) => {
                    call("claimUserTab", json!({ "tabId": id })).await?;
                    id
                }
                None => {
                    let created = call("createTab", json!({})).await?;
                    created
                        .get("tabId")
                        .or_else(|| created.get("id"))
                        .and_then(|v| v.as_i64())
                        .ok_or_else(|| "o navegador não devolveu o id da aba nova".to_string())?
                }
            };

            call("attach", json!({ "tabId": id })).await?;
            let _ = cdp(id, "Page.enable", json!({})).await;
            // O upstream cria a aba com `active: false` (agente trabalhando
            // escondido). Aqui é o contrário: a razão de existir deste modo é o
            // usuário VER. Page.bringToFront traz a aba para a frente sem
            // precisar de método novo na extensão.
            let _ = cdp(id, "Page.bringToFront", json!({})).await;
            if !url.is_empty() {
                navigate(id, url).await?;
            }
            let _ = call("nameSession", json!({ "name": "Neo — automação" })).await;
            info!("[BrowserLocal] aba {} reivindicada (url={})", id, url);
            Ok(json!({ "tab_ref": id }))
        }

        "browser.navigate" => {
            let id = tab_id(args)?;
            let url = arg_str(args, "url")?;
            navigate(id, &url).await
        }

        // ── percepção ──
        "browser.snapshot" => {
            let id = tab_id(args)?;
            // 200 e não 120: agora cada item de lista entra DUAS vezes (a linha
            // com o nome e o botão dela). Com o teto antigo, uma pasta no fim
            // da lista do Drive era cortada antes de virar `ref`.
            let max = args
                .get("max_elements")
                .and_then(|v| v.as_u64())
                .unwrap_or(200) as usize;
            let chars = args
                .get("text_chars")
                .and_then(|v| v.as_u64())
                .unwrap_or(3000) as usize;
            // Só depois de uma ação que pode trocar a tela (quem pede é o core).
            if let Some(teto) = args.get("assentar_ms").and_then(|v| v.as_u64()) {
                let antes = args.get("url_antes").and_then(|v| v.as_str());
                assentar(id, teto, antes).await;
            }
            let mut snapshot = eval(id, &js_snapshot(max, chars)).await?;
            // O flag do PARE tem duas fontes: o atributo no HTML (confiável,
            // sobrevive à notificação perdida) e a notificação (rápida).
            if stop_requested() {
                if let Some(object) = snapshot.as_object_mut() {
                    object.insert("stop".to_string(), Value::Bool(true));
                }
            }
            // O que o Daniel escreveu no painel desde o último passo. Headless não
            // tem painel: não mexe na fila (ela é de uma aba que ele vê).
            if !is_headless(id) {
                let fila = tomar_mensagens();
                if !fila.is_empty() {
                    if let Some(object) = snapshot.as_object_mut() {
                        object.insert("mensagens".to_string(), Value::Array(fila));
                    }
                }
            }
            Ok(snapshot)
        }

        // A espera de uma pergunta (`perguntar` no core): não há snapshot, então o
        // core pergunta aqui o que chegou do painel. O `eval` do PARE é também o
        // que bombeia as notificações paradas no socket.
        "browser.inbox" => {
            let id = tab_id(args)?;
            if is_headless(id) {
                return Ok(json!({ "mensagens": [], "parado": false }));
            }
            let parado = parado(id).await;
            Ok(json!({ "mensagens": tomar_mensagens(), "parado": parado }))
        }

        "browser.extract" => {
            let id = tab_id(args)?;
            let chars = args
                .get("chars")
                .and_then(|v| v.as_u64())
                .unwrap_or(4000) as usize;
            let termo = args
                .get("busca")
                .and_then(|v| v.as_str())
                .filter(|t| !t.trim().is_empty());
            if let Some(termo) = termo {
                return unwrap_ok(eval(id, &js_buscar(termo, chars)).await?);
            }
            match args.get("ref").and_then(|v| v.as_i64()) {
                Some(reference) => unwrap_ok(eval(id, &js_extract(reference, chars)).await?),
                None => {
                    let de = args.get("de").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                    unwrap_ok(eval(id, &js_ler_pagina(de, chars)).await?)
                }
            }
        }

        // ── atuação ──
        "browser.click" => {
            let id = tab_id(args)?;
            let reference = arg_i64(args, "ref")?;
            let r = clicar(id, js_mirar(reference, "esquerdo", &trava_de(args)), 1).await?;
            Ok(com_abas_novas(id, r).await)
        }
        // Saída para o que se lê e não se endereça — ver `js_click_text`.
        "browser.click_text" => {
            let id = tab_id(args)?;
            let texto = arg_str(args, "texto")?;
            let n = args.get("n").and_then(|v| v.as_i64()).unwrap_or(0);
            let r = clicar(id, js_click_text(&texto, n, &trava_de(args)), 1).await?;
            Ok(com_abas_novas(id, r).await)
        }
        // A varredura da DOM inteira. Só lê: não toca na página.
        "browser.find" => {
            let id = tab_id(args)?;
            let texto = arg_str(args, "texto")?;
            let limite = args.get("limite").and_then(|v| v.as_u64()).unwrap_or(8) as usize;
            unwrap_ok(eval(id, &js_find(&texto, limite.min(20))).await?)
        }
        "browser.dblclick" => {
            let id = tab_id(args)?;
            let reference = arg_i64(args, "ref")?;
            let r = clicar(id, js_mirar(reference, "duplo", &trava_de(args)), 2).await?;
            Ok(com_abas_novas(id, r).await)
        }
        "browser.right_click" => {
            let id = tab_id(args)?;
            let reference = arg_i64(args, "ref")?;
            clicar(id, js_mirar(reference, "direito", &trava_de(args)), 1).await
        }
        "browser.fill" => {
            let id = tab_id(args)?;
            let reference = arg_i64(args, "ref")?;
            let value = args.get("value").and_then(|v| v.as_str()).unwrap_or("");
            unwrap_ok(eval(id, &js_fill(reference, value)).await?)
        }
        "browser.select" => {
            let id = tab_id(args)?;
            let reference = arg_i64(args, "ref")?;
            let option = arg_str(args, "option")?;
            unwrap_ok(eval(id, &js_select(reference, &option)).await?)
        }
        "browser.press" => {
            let id = tab_id(args)?;
            let key = args.get("key").and_then(|v| v.as_str()).unwrap_or("Enter");
            let r = press_key(id, key).await?;
            Ok(com_abas_novas(id, r).await)
        }
        "browser.scroll" => {
            let id = tab_id(args)?;
            let delta = args.get("delta").and_then(|v| v.as_i64()).unwrap_or(600);
            let reference = args.get("ref").and_then(|v| v.as_i64()).unwrap_or(-1);
            unwrap_ok(eval(id, &js_scroll(delta, reference)).await?)
        }
        "browser.wait_for" => {
            let id = tab_id(args)?;
            let text = args.get("text").and_then(|v| v.as_str()).unwrap_or("");
            let timeout_ms = args
                .get("timeout_ms")
                .and_then(|v| v.as_u64())
                .unwrap_or(10_000);
            wait_for(id, text, timeout_ms).await
        }

        // ── faixa do Neo / freio / fim ──
        "browser.banner" => {
            let id = tab_id(args)?;
            // O banner existe para o usuário VER que o Neo está dirigindo a aba
            // dele. Sem janela não há quem veja: injetar DOM ali só custa um
            // round-trip e polui a página que vai ser lida no snapshot seguinte.
            if is_headless(id) {
                return Ok(json!({ "ok": true, "skipped": "headless" }));
            }
            let text = args.get("text").and_then(|v| v.as_str()).unwrap_or("");
            let active = args.get("active").and_then(|v| v.as_bool()).unwrap_or(true);
            let kind = args.get("kind").and_then(|v| v.as_str()).unwrap_or("passo");
            let detail = args.get("detail").and_then(|v| v.as_str()).unwrap_or("");
            let _ = eval(id, &js_banner(text, active, false, kind, detail)).await?;
            // A PERGUNTA vai também pelo canal privilegiado da extensão: o
            // `postMessage` acima a própria página consegue imitar, e uma pergunta
            // falsa com botão "Sim" ao lado da verdadeira ("posso excluir a
            // conta?") seria uma autorização forjada. Só o que vem por aqui ganha
            // botões e a caixa "respondendo a…". Extensão antiga: segue sem.
            if kind == "pergunta" {
                let opcoes: Vec<&str> = detail
                    .lines()
                    .map(|l| l.trim())
                    .filter(|l| !l.is_empty())
                    .collect();
                let aberta = call(
                    "neoPergunta",
                    json!({ "tabId": id, "texto": text, "opcoes": opcoes }),
                )
                .await
                .is_ok();
                if aberta {
                    if let Ok(mut abertas) = perguntas().lock() {
                        abertas.insert(id);
                    }
                }
            } else if perguntas().lock().map(|mut a| a.remove(&id)).unwrap_or(false) {
                let _ = call("neoPergunta", json!({ "tabId": id, "texto": "" })).await;
            }
            Ok(json!({ "ok": true }))
        }
        "browser.screenshot" => {
            let id = tab_id(args)?;
            let marks = args.get("marks").and_then(|v| v.as_bool()).unwrap_or(false);
            let quality = args
                .get("quality")
                .and_then(|v| v.as_u64())
                .unwrap_or(70)
                .clamp(20, 95);
            let scale = args
                .get("scale")
                .and_then(|v| v.as_f64())
                .unwrap_or(1.0)
                .clamp(0.25, 1.0);

            // Recorte pedido pelo core (triagem de painel): 4 números em px de
            // CSS. Quando vem, este print tem um trabalho só — ser LIDO — então
            // ele sai sem marcação e sem redução, e o caminho normal abaixo é
            // pulado inteiro.
            let clip: Option<[f64; 4]> = args.get("clip").and_then(|v| {
                let a = v.as_array()?;
                if a.len() < 4 {
                    return None;
                }
                let mut r = [0.0f64; 4];
                for (i, slot) in r.iter_mut().enumerate() {
                    *slot = a[i].as_f64()?;
                }
                (r[2] > 0.0 && r[3] > 0.0).then_some(r)
            });

            // Captura CRUA do viewport: sem `clip`, sem `captureBeyondViewport`,
            // sem nada que faça o Chrome relayoutar a página. A redução de
            // tamanho acontece depois, em cima do JPEG (draw_marks).
            let params = json!({ "format": "jpeg", "quality": quality });

            // Os retângulos são LIDOS antes da foto; nada é escrito na página.
            // O recorte também precisa da largura em px de CSS para converter o
            // retângulo — em retina a imagem sai no dobro do tamanho.
            let boxes = if marks || clip.is_some() {
                eval(id, JS_RECTS).await.ok()
            } else {
                None
            };

            // Esconde o painel do Neo só durante o clique do obturador. O
            // `let shot` separado do `?` é de propósito: captura que falha
            // ainda precisa devolver o painel para a tela.
            let esconder = !is_headless(id);
            if esconder {
                let _ = eval(id, JS_PAINEL_OCULTAR).await;
            }
            let shot = cdp(id, "Page.captureScreenshot", params).await;
            if esconder {
                let _ = eval(id, JS_PAINEL_MOSTRAR).await;
            }
            let shot = shot?;
            let mut data = shot
                .get("data")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string();
            if data.is_empty() {
                return Err("o navegador não devolveu a imagem".to_string());
            }

            if let Some(rect) = clip {
                let css_width = boxes
                    .as_ref()
                    .and_then(|b| b.get("width"))
                    .and_then(|v| v.as_f64())
                    .unwrap_or(0.0);
                if let Some(bytes) = b64_decode(&data) {
                    if let Some(cortado) = crop_jpeg(&bytes, css_width, &rect) {
                        return Ok(json!({
                            "ok": true, "format": "jpeg", "data": b64_encode(&cortado),
                            "marks": false, "marks_refs": [], "clipped": true
                        }));
                    }
                }
                // Recorte que não deu certo cai no print inteiro: cego é pior.
            }

            let mut marked = false;
            // Quais números foram REALMENTE desenhados. É o que permite dizer
            // aos olhos "só existem estes" — sem isso o modelo de visão, quando
            // mandado citar o número de um elemento sem marcação, inventa um
            // (job 9968e68a: citou 16..20 e 163; o 18 era "Meu Papel no Mundo",
            // no menu lateral, e o agente clicou lá cinco vezes).
            let mut painted_refs: Vec<i64> = Vec::new();
            if boxes.is_none() && scale < 0.999 {
                if let Some(bytes) = b64_decode(&data) {
                    if let Some(menor) = draw_marks(&bytes, &[], 0.0, scale) {
                        data = b64_encode(&menor);
                    }
                }
            }
            if let Some(boxes) = boxes {
                let css_width = boxes.get("width").and_then(|v| v.as_f64()).unwrap_or(0.0);
                let rects = boxes.get("rects").and_then(|v| v.as_array());
                if let (Some(rects), Some(bytes)) = (rects, b64_decode(&data)) {
                    if let Some(painted) = draw_marks(&bytes, rects, css_width, scale) {
                        data = b64_encode(&painted);
                        marked = true;
                        painted_refs = rects
                            .iter()
                            .filter_map(|r| r.as_array()?.first()?.as_i64())
                            .filter(|n| *n >= 0)
                            .collect();
                    }
                }
            }
            Ok(json!({
                "ok": true, "format": "jpeg", "data": data,
                "marks": marked, "marks_refs": painted_refs
            }))
        }
        "browser.network_activity" => {
            let id = tab_id(args)?;
            eval(id, JS_NET).await
        }
        "browser.downloads" => {
            // O headless não tem `chrome.downloads` (é API de extensão), e o
            // perfil dedicado nem baixa por padrão. Devolver lista vazia com a
            // razão é melhor que estourar: o driver já sabe lidar com "nenhum
            // download" e reporta honestamente, enquanto um erro cru mataria a
            // tarefa inteira por causa de uma sondagem acessória.
            //
            // LIMITAÇÃO CONHECIDA: baixar arquivo PELO headless ainda não é
            // suportado. Para pegar arquivo, o caminho é `web_fetch` (que
            // agora diz `download` quando a URL é um binário acessível) ou
            // curl — navegador é o pior jeito de pegar binário de qualquer
            // forma.
            if args.get("mode").and_then(|v| v.as_str()) == Some("headless")
                || tab_id(args).map(is_headless).unwrap_or(false)
            {
                return Ok(json!({
                    "files": [],
                    "unsupported": "o navegador headless não acompanha downloads",
                }));
            }
            let seconds = args
                .get("since_s")
                .and_then(|v| v.as_u64())
                .unwrap_or(900)
                .clamp(10, 86_400);
            let ids = args.get("ids").cloned().unwrap_or(Value::Null);
            downloads_merged(seconds, ids).await
        }
        "browser.release" => {
            let id = tab_id(args)?;
            // Headless: soltar é FECHAR. Não há usuário olhando para quem
            // devolver a aba, e deixá-la aberta é exatamente o vazamento que o
            // gerente de memória existe para evitar.
            if is_headless(id) {
                super::headless::close_tab(-id).await?;
                STOP_REQUESTED.store(false, Ordering::SeqCst);
                info!("[BrowserLocal] aba headless {} fechada", id);
                return Ok(json!({ "ok": true }));
            }
            // Melhor esforço: a aba pode já ter sido fechada pelo usuário, e
            // nesse caso "soltar" já está feito.
            let _ = eval(id, &js_banner("", false, false, "fim", "")).await;
            let _ = call("detach", json!({ "tabId": id })).await;
            let _ = call("turnEnded", json!({})).await;
            STOP_REQUESTED.store(false, Ordering::SeqCst);
            info!("[BrowserLocal] aba {} liberada", id);
            Ok(json!({ "ok": true }))
        }

        // ── várias abas (neo-core/docs/browser-multi-aba-plan.md) ──
        "browser.session_tabs" => {
            let id = tab_id(args)?;
            abas_da_sessao(id).await
        }
        // Fecha uma aba que o Neo ou a página abriu. A do usuário, a extensão recusa.
        "browser.close_tab" => {
            let id = tab_id(args)?;
            if is_headless(id) {
                super::headless::close_tab(-id).await?;
            } else {
                call("neoCloseTab", json!({ "tabId": id })).await?;
            }
            esquecer_janelas(id);
            info!("[BrowserLocal] aba {} fechada a pedido", id);
            Ok(json!({ "ok": true }))
        }
        // Traz a aba para a frente: é a que o usuário passa a ver. Reconecta antes:
        // a aba de uma tarefa retomada foi solta (`turnEnded`) quando ela parou.
        "browser.focus_tab" => {
            let id = tab_id(args)?;
            if !is_headless(id) {
                call("attach", json!({ "tabId": id })).await?;
                let _ = cdp(id, "Page.enable", json!({})).await;
            }
            cdp(id, "Page.bringToFront", json!({})).await?;
            Ok(json!({ "ok": true }))
        }
        // Aba que trabalha ESCONDIDA (subagente do `paralelo`). Sem isto o Chromium
        // para o `requestAnimationFrame`, roda timer a 1/s e não dispara o
        // `IntersectionObserver`: o preço que carrega ao rolar nunca aparece
        // (medido no F0, neo-core/docs/browser-multi-aba-plan.md §12).
        "browser.keep_alive" => {
            let id = tab_id(args)?;
            let ligado = args.get("ligado").and_then(|v| v.as_bool()).unwrap_or(true);
            if ligado {
                let _ = cdp(id, "Page.setWebLifecycleState", json!({ "state": "active" })).await;
            }
            cdp(id, "Emulation.setFocusEmulationEnabled", json!({ "enabled": ligado })).await?;
            Ok(json!({ "ok": true, "ligado": ligado }))
        }
        // Abre uma aba NOVA da sessão, em segundo plano: o agente continua onde
        // está (o clique do meio). `tab_ref` só diz o backend — a nova é irmã.
        "browser.open_tab" => {
            let origem = tab_id(args)?;
            let url = arg_str(args, "url")?;
            if !url.starts_with("http://") && !url.starts_with("https://") {
                return Err("só abro aba em endereço http(s)".to_string());
            }
            let tab = if is_headless(origem) {
                // Em branco e depois o `navigate`: o `createTarget` volta antes de a
                // página existir, e a irmã chegava sem título (pego pelo teste).
                -super::headless::open_tab("about:blank").await?
            } else {
                let criada = call("createTab", json!({})).await?;
                let id = criada
                    .get("tabId")
                    .or_else(|| criada.get("id"))
                    .and_then(|v| v.as_i64())
                    .ok_or_else(|| "o navegador não devolveu o id da aba nova".to_string())?;
                call("attach", json!({ "tabId": id })).await?;
                id
            };
            let _ = cdp(tab, "Page.enable", json!({})).await;
            let carregada = navigate(tab, &url).await?;
            info!("[BrowserLocal] aba {} aberta a pedido a partir da {} ({})", tab, origem, url);
            Ok(json!({
                "tab_ref": tab,
                "url": carregada.get("url").cloned().unwrap_or(json!(url)),
                "title": carregada.get("title").cloned().unwrap_or(Value::Null),
            }))
        }

        other => Err(format!("ação de navegador desconhecida: {}", other)),
    }
}

async fn navigate(id: i64, url: &str) -> Result<Value, String> {
    let _ = cdp(id, "Page.enable", json!({})).await;
    let result = cdp(id, "Page.navigate", json!({ "url": url })).await?;
    if let Some(error) = result.get("errorText").and_then(|v| v.as_str()) {
        return Err(format!("navegação falhou: {}", error));
    }
    // Espera o load de verdade: o Page.navigate volta antes de a página existir.
    let deadline = Instant::now() + Duration::from_secs(20);
    let mut last = Value::Null;
    while Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(250)).await;
        match eval(
            id,
            "({ ready: document.readyState, url: location.href, title: document.title || '' })",
        )
        .await
        {
            Ok(value) => {
                let ready = value
                    .get("ready")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                last = value;
                if ready == "complete" || ready == "interactive" {
                    break;
                }
            }
            // Durante a troca de documento o evaluate falha; é esperado.
            Err(_) => continue,
        }
    }
    Ok(json!({
        "ok": true,
        "url": last.get("url").cloned().unwrap_or(Value::String(url.to_string())),
        "title": last.get("title").cloned().unwrap_or(Value::Null),
    }))
}

async fn press_key(id: i64, key: &str) -> Result<Value, String> {
    // Tecla de verdade via CDP (não um KeyboardEvent sintético): formulário que
    // só reage a Enter nativo não reage ao evento fabricado em JS.
    let (code, vk, text) = match key {
        "Enter" => ("Enter", 13, "\r"),
        "Tab" => ("Tab", 9, "\t"),
        "Escape" => ("Escape", 27, ""),
        "Backspace" => ("Backspace", 8, ""),
        "ArrowDown" => ("ArrowDown", 40, ""),
        "ArrowUp" => ("ArrowUp", 38, ""),
        // Navegação de lista longa: às vezes é o único jeito de descer numa
        // grade virtualizada que ignora scroll programático.
        "PageDown" => ("PageDown", 34, ""),
        "PageUp" => ("PageUp", 33, ""),
        "End" => ("End", 35, ""),
        "Home" => ("Home", 36, ""),
        other => return Err(format!("tecla não suportada: {}", other)),
    };
    let base = json!({
        "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
    });
    let mut down = base.clone();
    down["type"] = json!("rawKeyDown");
    if !text.is_empty() {
        down["text"] = json!(text);
        down["type"] = json!("keyDown");
    }
    cdp(id, "Input.dispatchKeyEvent", down).await?;
    let mut up = base;
    up["type"] = json!("keyUp");
    cdp(id, "Input.dispatchKeyEvent", up).await?;
    Ok(json!({ "ok": true, "key": key }))
}

const JS_ASSENTOU: &str = r#"({
  ready: document.readyState,
  url: location.href,
  nos: document.getElementsByTagName('*').length
})"#;

/// Espera a página parar de mudar antes do snapshot. Melhor esforço: estourar o
/// teto só quer dizer que o snapshot sai como sairia sem esta espera.
///
/// Sem isto, o snapshot saía na mesma hora do clique, com a página pela metade:
/// 59 elementos, todos de cabeçalho (jobs 2d416462 e 45e85695, uma vez por
/// produto). Cada um custava uma rodada inteira de LLM para o modelo perceber e
/// mais uma de `wait_for`, e ainda rendia decisões erradas, como "a página não
/// tem dados do produto". O laço roda aqui e não no core de propósito: cada
/// consulta é local ao Mac, em vez de uma ida e volta até a VPS.
///
/// `url_antes` vale para o clique que deve NAVEGAR. Antes de a navegação
/// entregar, o documento velho está `complete` e parado, e pareceria assentado.
async fn assentar(id: i64, teto_ms: u64, url_antes: Option<&str>) {
    const PASSO: Duration = Duration::from_millis(150);
    const ESPERA_NAVEGAR: Duration = Duration::from_millis(1200);
    let inicio = Instant::now();
    let fim = inicio + Duration::from_millis(teto_ms.min(5000));
    let mut anterior: Option<(String, u64)> = None;
    loop {
        tokio::time::sleep(PASSO).await;
        // Erro aqui é o contexto sendo destruído pela navegação: é só esperar.
        if let Ok(v) = eval(id, JS_ASSENTOU).await {
            let url = v.get("url").and_then(|x| x.as_str()).unwrap_or("").to_string();
            let nos = v.get("nos").and_then(|x| x.as_u64()).unwrap_or(0);
            let pronta = v.get("ready").and_then(|x| x.as_str()) == Some("complete");
            let esperando_navegar = url_antes == Some(url.as_str())
                && inicio.elapsed() < ESPERA_NAVEGAR;
            // Até 1% de diferença: carrossel e contador nunca param de mexer, e
            // exigir silêncio absoluto gastaria o teto inteiro nesses sites.
            let parada = anterior
                .as_ref()
                .is_some_and(|(u, n)| *u == url && nos.abs_diff(*n) * 100 <= (*n).max(1));
            if pronta && parada && !esperando_navegar {
                return;
            }
            anterior = Some((url, nos));
        }
        if Instant::now() >= fim {
            return;
        }
    }
}

async fn wait_for(id: i64, text: &str, timeout_ms: u64) -> Result<Value, String> {
    let needle = serde_json::to_string(text).unwrap_or_else(|_| "\"\"".to_string());
    let expression = format!(
        "(() => {{ const t = {}; return {{ ok: !t || (document.body ? document.body.innerText : '').toLowerCase().includes(String(t).toLowerCase()) }}; }})()",
        needle
    );
    let deadline = Instant::now() + Duration::from_millis(timeout_ms.min(60_000));
    loop {
        if let Ok(value) = eval(id, &expression).await {
            if value.get("ok").and_then(|v| v.as_bool()) == Some(true) {
                return Ok(json!({ "ok": true }));
            }
        }
        if Instant::now() >= deadline {
            return Err(format!("'{}' não apareceu na página a tempo", text));
        }
        tokio::time::sleep(Duration::from_millis(400)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn jpeg(width: u32, height: u32) -> Vec<u8> {
        let img = image::RgbImage::from_pixel(width, height, image::Rgb([250, 250, 250]));
        let mut out = Vec::new();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut std::io::Cursor::new(&mut out), 90)
            .encode(
                img.as_raw(),
                img.width(),
                img.height(),
                image::ExtendedColorType::Rgb8,
            )
            .unwrap();
        out
    }

    /// O caso que derrubou dois jobs: host vivo, socket atendendo, e o
    /// `active.json` apagado pela limpeza diária do /tmp (que não apaga socket).
    /// Antes isso virava "a extensão do Neo não está ativa" — mentira que ainda
    /// mandava o usuário mexer no que estava funcionando.
    #[test]
    fn varredura_acha_o_socket_quando_o_registro_sumiu() {
        let base = std::env::temp_dir().join(format!("obu-teste-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();

        // Diretório existe mas está vazio: continua sendo "não está no ar".
        assert!(socket_por_varredura_em(&base).is_err());

        // Arquivo comum com nome de socket NÃO serve — descobrir isso só no
        // connect daria o erro errado.
        std::fs::write(base.join("impostor.sock"), b"nao sou um socket").unwrap();
        assert!(socket_por_varredura_em(&base).is_err());

        let alvo = base.join("a399412b.sock");
        let _listener = std::os::unix::net::UnixListener::bind(&alvo).unwrap();
        assert_eq!(socket_por_varredura_em(&base).unwrap(), alvo);

        std::fs::remove_dir_all(&base).ok();
    }

    /// A fila do painel (neo-core/docs/browser-conversa-plan.md §4.3): guarda só
    /// texto, corta no teto, passa a opção do botão e esvazia quando o core lê.
    /// Um teste só, porque a fila é global e os testes rodam em paralelo.
    #[test]
    fn fila_do_painel_guarda_corta_e_esvazia() {
        let _ = tomar_mensagens();
        guardar_mensagem(&json!({ "tabId": 7, "texto": "   " }));
        guardar_mensagem(&json!({ "tabId": 7, "texto": " avalie pelos critérios " }));
        guardar_mensagem(&json!({ "tabId": 7, "texto": "Pode enviar", "opcao": 0 }));
        guardar_mensagem(&json!({ "tabId": 7, "texto": "x".repeat(5000) }));
        let lidas = tomar_mensagens();
        assert_eq!(lidas.len(), 3, "texto vazio não entra: {lidas:?}");
        assert_eq!(lidas[0]["texto"], "avalie pelos critérios");
        assert_eq!(lidas[0]["tab"], 7);
        assert!(lidas[0].get("opcao").is_none());
        assert_eq!(lidas[1]["opcao"], 0);
        assert_eq!(lidas[2]["texto"].as_str().unwrap().chars().count(), MENSAGEM_CHARS);
        assert!(tomar_mensagens().is_empty(), "lida uma vez, some");

        for i in 0..(MENSAGENS_MAX + 5) {
            guardar_mensagem(&json!({ "texto": format!("m{i}") }));
        }
        let lidas = tomar_mensagens();
        assert_eq!(lidas.len(), MENSAGENS_MAX);
        assert_eq!(lidas[0]["texto"], "m5", "o teto descarta as mais antigas");
    }

    /// O `fill` não foca campo de login: o gerenciador de senhas abriria o menu
    /// dele (iframe de outra extensão) e o navegador soltaria a aba. O JS gerado
    /// também precisa ser JS válido — confere com o node quando ele existe.
    #[test]
    fn fill_nao_foca_campo_de_login_e_gera_js_valido() {
        let js = js_fill(8, "teste@exemplo.com");
        assert!(js.contains("const credencial"), "{js}");
        assert!(js.contains("if (!credencial) el.focus();"));
        let caminho = std::env::temp_dir().join(format!("neo-fill-{}.js", std::process::id()));
        std::fs::write(&caminho, &js).unwrap();
        if let Ok(saida) = std::process::Command::new("node").arg("--check").arg(&caminho).output() {
            assert!(
                saida.status.success(),
                "js_fill não é JS válido: {}",
                String::from_utf8_lossy(&saida.stderr)
            );
        }
        std::fs::remove_file(&caminho).ok();
    }

    #[test]
    fn debugger_solto_diz_o_motivo_quando_sabe() {
        anotar_desligamento(9101, "target_closed");
        assert_eq!(
            com_motivo_do_desligamento(9101, "Debugger unattached".to_string()),
            "Debugger unattached (a aba foi fechada)"
        );
        anotar_desligamento(9102, "canceled_by_user");
        assert!(com_motivo_do_desligamento(9102, "Debugger unattached".to_string())
            .contains("cancelou"));
        assert_eq!(
            com_motivo_do_desligamento(9103, "Debugger unattached".to_string()),
            "Debugger unattached"
        );
        assert_eq!(com_motivo_do_desligamento(9101, "outro erro".to_string()), "outro erro");
    }

    #[test]
    fn base64_ida_e_volta() {
        for original in [&b""[..], b"a", b"ab", b"abc", b"\x00\xff\x10dados"] {
            let encoded = b64_encode(original);
            assert_eq!(b64_decode(&encoded).unwrap(), original.to_vec());
        }
    }

    #[test]
    fn marcacao_pinta_a_imagem_sem_mexer_no_tamanho() {
        let source = jpeg(400, 200);
        // Retângulo em coordenadas de CSS; a imagem tem o DOBRO da largura
        // (retina), então o fator tem que dobrar as coordenadas sozinho.
        let rects = vec![json!([7, 50, 60, 100, 40])];
        let painted = draw_marks(&source, &rects, 200.0, 1.0).expect("devia desenhar");

        let img = image::load_from_memory_with_format(&painted, image::ImageFormat::Jpeg)
            .unwrap()
            .to_rgb8();
        assert_eq!((img.width(), img.height()), (400, 200));

        // A borda do elemento cai em (100, 120) na imagem: rosa, não o fundo.
        let pixel = img.get_pixel(101, 121);
        assert!(
            pixel[0] > 180 && pixel[2] > 90 && pixel[1] < 140,
            "esperava a marca rosa em (101,121), veio {:?}",
            pixel
        );
        // E um canto distante continua sendo o fundo claro.
        let limpo = img.get_pixel(390, 10);
        assert!(limpo[1] > 200, "o resto da imagem não pode ser pintado");
    }

    #[test]
    fn sem_retangulo_nao_reencoda_a_imagem() {
        assert!(draw_marks(&jpeg(100, 100), &[], 100.0, 1.0).is_none());
        assert!(draw_marks(&jpeg(100, 100), &[json!([1, 0, 0, 10, 10])], 0.0, 1.0).is_none());
    }

    #[test]
    fn reduz_a_imagem_sem_pedir_clip_ao_navegador() {
        // O `clip` do CDP fazia o Chrome relayoutar a página e a tela do usuário
        // piscava a cada passo. Agora a redução é por software, aqui.
        let menor = draw_marks(&jpeg(1000, 600), &[], 0.0, 0.5).expect("devia reduzir");
        let img = image::load_from_memory_with_format(&menor, image::ImageFormat::Jpeg).unwrap();
        assert_eq!((img.width(), img.height()), (500, 300));
    }

    #[test]
    fn marcacao_acompanha_a_reducao() {
        let rects = vec![json!([3, 100, 50, 200, 80])];
        // imagem 800px para 400px de CSS (retina), reduzida à metade → ratio 1.0
        let pintada = draw_marks(&jpeg(800, 400), &rects, 400.0, 0.5).unwrap();
        let img = image::load_from_memory_with_format(&pintada, image::ImageFormat::Jpeg)
            .unwrap()
            .to_rgb8();
        assert_eq!((img.width(), img.height()), (400, 200));
        let pixel = img.get_pixel(101, 51);
        assert!(
            pixel[0] > 180 && pixel[2] > 90 && pixel[1] < 140,
            "a marca tem que cair sobre o elemento na imagem JÁ reduzida, veio {:?}",
            pixel
        );
    }

    #[test]
    fn jpeg_invalido_nao_derruba_o_print() {
        assert!(draw_marks(b"nao sou uma imagem", &[json!([1, 0, 0, 10, 10])], 100.0, 1.0).is_none());
    }
}

// ─── Testes ─────────────────────────────────────────────────────────────────
//
// Exercitam o DISPATCHER real (`execute`) contra um Chromium de verdade, em
// modo headless. É o que prova o que o `cargo check` não prova: que o
// vocabulário de ações e o JS de página — os mesmos ~1.100 linhas que rodam no
// navegador visível — funcionam sem janela nenhuma.
//
//     cargo test --lib browser_local -- --ignored --test-threads=1
#[cfg(test)]
mod tests_headless {
    use super::*;

    #[test]
    fn o_sinal_do_tab_ref_escolhe_o_backend() {
        assert!(is_headless(-1), "negativo é headless");
        assert!(!is_headless(1), "positivo é a aba do usuário");
        assert!(!is_headless(0));
    }

    #[test]
    fn janela_pedida_fica_ate_ser_consumida_e_nao_cresce_sem_fim() {
        esquecer_janelas(-9001);
        anotar_janela(-9001, "https://x/a", true);
        anotar_janela(-9001, "https://x/b", false);
        assert_eq!(janelas_de(-9001).len(), 2);
        assert_eq!(
            janelas_de(-9001)[1],
            JanelaPedida { url: "https://x/b".into(), gesto: false }
        );
        esquecer_janelas(-9001);
        assert!(janelas_de(-9001).is_empty());
        // Página que abre janela em laço: fica só o fim da fila.
        for i in 0..20 {
            anotar_janela(-9002, &format!("u{i}"), true);
        }
        assert_eq!(janelas_de(-9002).len(), 8);
        assert_eq!(janelas_de(-9002)[0].url, "u12");
        esquecer_janelas(-9002);
    }

    /// Servidor HTTP de uma página só, local: `open_tab` só aceita http(s), e o
    /// teste não pode depender da internet.
    fn servidor_local(html: &'static str) -> String {
        use std::io::{Read, Write};
        let ouvinte = std::net::TcpListener::bind("127.0.0.1:0").expect("porta local");
        let porta = ouvinte.local_addr().expect("endereço").port();
        std::thread::spawn(move || {
            for conexao in ouvinte.incoming().flatten() {
                let mut conexao = conexao;
                let mut lixo = [0u8; 2048];
                let _ = conexao.read(&mut lixo);
                let resposta = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    html.len(),
                    html
                );
                let _ = conexao.write_all(resposta.as_bytes());
            }
        });
        format!("http://127.0.0.1:{porta}/")
    }

    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn open_tab_abre_irma_em_segundo_plano_e_a_origem_continua_valendo() {
        if super::super::headless::available() == false {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        super::super::headless::shutdown().await;
        let url = servidor_local("<html><head><title>irma</title></head><body>anuncio</body></html>");

        let claim = execute("browser.claim", &json!({ "mode": "headless", "url": PAGINA_QUE_ABRE }))
            .await
            .expect("claim headless");
        let tab = claim["tab_ref"].as_i64().expect("tab_ref");

        let aberta = execute("browser.open_tab", &json!({ "tab_ref": tab, "url": url }))
            .await
            .expect("open_tab");
        let irma = aberta["tab_ref"].as_i64().expect("tab_ref da irmã");
        assert!(irma < 0 && irma != tab, "{aberta}");

        // A irmã carregou a página pedida, e a origem segue aceitando comando.
        let titulo = eval(irma, "document.title").await.expect("eval na irmã");
        assert_eq!(titulo, json!("irma"));
        assert!(eval(tab, "document.body.innerText").await.expect("eval na origem").is_string());

        let sessao = execute("browser.session_tabs", &json!({ "tab_ref": tab })).await.expect("session_tabs");
        assert_eq!(sessao["tabs"].as_array().map(|a| a.len()), Some(2), "{sessao}");

        // Endereço que não é http(s) não abre.
        let erro = execute("browser.open_tab", &json!({ "tab_ref": tab, "url": "file:///etc/passwd" }))
            .await
            .unwrap_err();
        assert!(erro.contains("http"), "{erro}");

        execute("browser.close_tab", &json!({ "tab_ref": irma })).await.expect("fecha a irmã");
        execute("browser.release", &json!({ "tab_ref": tab })).await.expect("release");
        assert_eq!(super::super::headless::stats().await["tabs"], 0);
        super::super::headless::shutdown().await;
    }

    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn keep_alive_faz_a_aba_escondida_trabalhar_como_se_estivesse_na_frente() {
        if super::super::headless::available() == false {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        super::super::headless::shutdown().await;
        let url = servidor_local(
            "<html><body><script>window.raf=0;(function l(){window.raf++;requestAnimationFrame(l)})();</script></body></html>",
        );
        let claim = execute("browser.claim", &json!({ "mode": "headless", "url": url }))
            .await
            .expect("claim headless");
        let escondida = claim["tab_ref"].as_i64().expect("tab_ref");
        let outra = execute("browser.open_tab", &json!({ "tab_ref": escondida, "url": url }))
            .await
            .expect("open_tab")["tab_ref"]
            .as_i64()
            .expect("tab_ref");
        execute("browser.focus_tab", &json!({ "tab_ref": outra })).await.expect("focus");

        let raf = |tab: i64| async move { eval(tab, "window.raf").await.expect("raf").as_i64().unwrap_or(-1) };
        let ritmo = |tab: i64| async move {
            let a = raf(tab).await;
            tokio::time::sleep(Duration::from_millis(600)).await;
            raf(tab).await - a
        };
        let vis = eval(escondida, "document.visibilityState").await.expect("vis");
        assert_eq!(vis, json!("hidden"), "a de trás tinha de estar escondida");
        assert!(ritmo(escondida).await <= 2, "escondida, o rAF para");

        execute("browser.keep_alive", &json!({ "tab_ref": escondida })).await.expect("keep_alive");
        assert!(ritmo(escondida).await > 10, "com o ciclo de vida forçado, o rAF volta");

        execute("browser.close_tab", &json!({ "tab_ref": outra })).await.expect("fecha");
        execute("browser.release", &json!({ "tab_ref": escondida })).await.expect("release");
        super::super::headless::shutdown().await;
    }

    /// Um botão que abre aba e um que não abre — o job 3512cdd2 em miniatura.
    const PAGINA_QUE_ABRE: &str = "data:text/html,<html><body>\
        <button style='width:300px;height:80px' onclick=\"window.open('about:blank','filha')\">abre</button>\
        <button style='width:300px;height:80px'>nada</button>\
        </body></html>";

    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn clique_que_abre_aba_devolve_a_filha_e_o_que_nao_abre_volta_na_hora() {
        if super::super::headless::available() == false {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        super::super::headless::shutdown().await;

        let claim = execute("browser.claim", &json!({ "mode": "headless", "url": PAGINA_QUE_ABRE }))
            .await
            .expect("claim headless");
        let tab = claim["tab_ref"].as_i64().expect("tab_ref");

        let r = execute("browser.click_text", &json!({ "tab_ref": tab, "texto": "abre" }))
            .await
            .expect("clique que abre");
        let novas = r["abas_novas"].as_array().cloned().unwrap_or_default();
        assert_eq!(novas.len(), 1, "o clique abriu uma aba e o resultado não disse: {r}");
        let filha = novas[0]["tab_ref"].as_i64().expect("tab_ref da filha");
        assert!(filha < 0 && filha != tab, "filha tem que ser outra aba headless: {filha}");
        assert_eq!(novas[0]["aberta_por"], json!(tab));

        // A filha já aceita comando — é o que deixa o agente SEGUIR para ela.
        let nome = eval(filha, "window.name").await.expect("eval na filha");
        assert_eq!(nome, json!("filha"));

        let sessao = execute("browser.session_tabs", &json!({ "tab_ref": tab }))
            .await
            .expect("session_tabs");
        let abas = sessao["tabs"].as_array().cloned().unwrap_or_default();
        assert_eq!(abas.len(), 2, "{sessao}");
        assert!(abas.iter().any(|a| a["tab_ref"] == json!(filha) && a["aberta_por"] == json!(tab)));

        // Clique que não abre nada não paga a espera da aba nova.
        let t0 = Instant::now();
        let r2 = execute("browser.click_text", &json!({ "tab_ref": tab, "texto": "nada" }))
            .await
            .expect("clique que não abre");
        assert!(r2.get("abas_novas").is_none(), "{r2}");
        assert!(t0.elapsed() < Duration::from_millis(900), "esperou {:?}", t0.elapsed());

        execute("browser.close_tab", &json!({ "tab_ref": filha })).await.expect("fecha a filha");
        let sessao = execute("browser.session_tabs", &json!({ "tab_ref": tab })).await.expect("session_tabs");
        assert_eq!(sessao["tabs"].as_array().map(|a| a.len()), Some(1), "{sessao}");

        execute("browser.release", &json!({ "tab_ref": tab })).await.expect("release");
        assert_eq!(super::super::headless::stats().await["tabs"], 0);
        super::super::headless::shutdown().await;
    }

    /// Página autossuficiente: sem rede, o teste mede o NOSSO código e não a
    /// internet (nem o humor de um site de terceiro num dia qualquer).
    const PAGINA: &str = "data:text/html,\
        <html><head><title>Teste Neo</title></head><body>\
        <h1>Ola headless</h1>\
        <a href='https://exemplo.com'>um link</a>\
        <button id='b'>Clicar</button>\
        <input id='campo' type='text'>\
        </body></html>";

    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn snapshot_e_screenshot_funcionam_sem_janela() {
        if super::super::headless::available() == false {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        super::super::headless::shutdown().await;

        let claim = execute("browser.claim", &json!({ "mode": "headless", "url": PAGINA }))
            .await
            .expect("claim headless");
        let tab = claim["tab_ref"].as_i64().expect("tab_ref");
        assert!(tab < 0, "claim headless tem que devolver id negativo: {tab}");
        assert_eq!(claim["mode"], "headless");

        // O inventário é o miolo caro (js_snapshot, 635 linhas). Se ele funciona
        // aqui, o vocabulário inteiro funciona neste backend.
        let snap = execute("browser.snapshot", &json!({ "tab_ref": tab }))
            .await
            .expect("snapshot");
        let texto = serde_json::to_string(&snap).unwrap_or_default();
        assert!(texto.contains("Ola headless"), "o snapshot não viu a página: {texto:.400}");
        assert!(texto.contains("um link"), "o inventário perdeu o link: {texto:.400}");

        // Print sem janela: o `--window-size` do launcher é o que dá tamanho.
        let shot = execute("browser.screenshot", &json!({ "tab_ref": tab }))
            .await
            .expect("screenshot");
        assert_eq!(shot["format"], "jpeg");
        let b64 = shot.get("data").and_then(|v| v.as_str()).unwrap_or("");
        assert!(b64.len() > 1000, "print vazio ou minúsculo ({} chars)", b64.len());
        // JPEG de verdade começa com os bytes ffd8ff — em base64, "/9j/".
        assert!(b64.starts_with("/9j/"), "não é um JPEG: {:.20}", b64);

        // Banner é no-op aqui: não há quem veja, e injetar DOM sujaria o
        // snapshot seguinte.
        let banner = execute("browser.banner", &json!({ "tab_ref": tab, "text": "oi" }))
            .await
            .expect("banner");
        assert_eq!(banner["skipped"], "headless");

        // Sondagem acessória não pode matar a tarefa por não existir aqui.
        let dls = execute("browser.downloads", &json!({ "tab_ref": tab }))
            .await
            .expect("downloads degrada, não estoura");
        assert!(dls["unsupported"].is_string());

        // Soltar, no headless, é FECHAR.
        execute("browser.release", &json!({ "tab_ref": tab }))
            .await
            .expect("release");
        assert_eq!(super::super::headless::stats().await["tabs"], 0);

        super::super::headless::shutdown().await;
    }

    /// Job 9a3223a4: o link do AliExpress (~600 chars) chegava cortado em 300, e o
    /// `abrir_aba` abria a metade. Agora vem inteiro; só o enorme é cortado, e marcado.
    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn link_comprido_chega_inteiro_e_o_enorme_vem_marcado_como_cortado() {
        if super::super::headless::available() == false {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        super::super::headless::shutdown().await;

        let medio = format!("https://exemplo.com/item/1.html?r={}", "a".repeat(600));
        let enorme = format!("https://exemplo.com/item/2.html?r={}", "b".repeat(2500));
        let pagina = format!(
            "data:text/html,<html><body><a href='{medio}'>medio</a><a href='{enorme}'>enorme</a></body></html>"
        );
        let claim = execute("browser.claim", &json!({ "mode": "headless", "url": pagina }))
            .await
            .expect("claim headless");
        let tab = claim["tab_ref"].as_i64().expect("tab_ref");
        let snap = execute("browser.snapshot", &json!({ "tab_ref": tab }))
            .await
            .expect("snapshot");
        let link = |texto: &str| -> Value {
            snap["elements"]
                .as_array()
                .and_then(|els| els.iter().find(|e| e["text"] == texto).cloned())
                .unwrap_or_else(|| panic!("o snapshot perdeu o link «{texto}»: {snap:.400}"))
        };
        assert_eq!(link("medio")["href"], medio);
        assert!(link("medio").get("href_cortado").is_none());
        assert_eq!(link("enorme")["href"].as_str().unwrap_or("").len(), 2000);
        assert_eq!(link("enorme")["href_cortado"], true);

        execute("browser.release", &json!({ "tab_ref": tab }))
            .await
            .expect("release");
        super::super::headless::shutdown().await;
    }

    /// O botão da Amazon em miniatura: input transparente sobre um desenho, com o
    /// nome em `aria-labelledby`, sem nó de texto próprio. Só o input escuta.
    const BOTAO_DISFARCADO: &str = "data:text/html;charset=utf-8,\
        <html><body style='margin:40px'>\
        <span style='position:relative;display:inline-block;width:200px;height:32px'>\
        <span id='nome' aria-hidden='true'>Adicionar ao carrinho</span>\
        <input type='button' aria-labelledby='nome' onclick='window.n=(window.n||0)+1' \
        style='position:absolute;left:0;top:0;width:200px;height:32px;margin:0;opacity:.01'>\
        </span></body></html>";

    async fn contagem(tab: i64) -> i64 {
        eval(tab, "window.n || 0").await.expect("contagem").as_i64().unwrap_or(-1)
    }

    /// O caminho inteiro em Rust: mira, mouse real pelo CDP, sonda. A conta tem de
    /// subir de 1 em 1: 0 é o clique morto do job 8bc25dd1, 2 é a reserva
    /// disparando em cima do mouse real (item em dobro no carrinho).
    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn radio_que_assenta_depois_do_clique_conta_como_selecionado() {
        // O rádio da grade do Google Forms: div role=radio que só vira
        // aria-checked=true pelo JS da página, um instante depois do clique. Lido
        // na hora dava "NÃO ficou selecionado" (sessão 74408e70).
        if super::super::headless::available() == false {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        super::super::headless::shutdown().await;
        let pagina = "data:text/html;charset=utf-8,<div role=radiogroup aria-label=Clareza>\
            <div id=r role=radio aria-checked=false aria-label='Conforme, resposta para Clareza' \
            style='width:40px;height:40px;background:%23ccc' \
            onclick=\"setTimeout(()=>this.setAttribute('aria-checked','true'),80)\"></div></div>";
        let claim = execute("browser.claim", &json!({ "mode": "headless", "url": pagina }))
            .await
            .expect("claim headless");
        let tab = claim["tab_ref"].as_i64().expect("tab_ref");
        let snap = execute("browser.snapshot", &json!({ "tab_ref": tab })).await.expect("snapshot");
        let radio = snap["elements"]
            .as_array()
            .expect("elements")
            .iter()
            .find(|e| e["text"] == "Conforme, resposta para Clareza")
            .unwrap_or_else(|| panic!("o rádio não entrou no inventário: {snap}"))
            .clone();
        let r = execute("browser.click", &json!({ "tab_ref": tab, "ref": radio["ref"] }))
            .await
            .expect("click");
        assert_eq!(r["checked"], true, "o estado assentou depois do clique: {r}");
        assert!(r["assentou_ms"].as_u64().is_some(), "{r}");
        super::super::headless::shutdown().await;
    }

    /// A estrutura de grade vai em cada rádio, em qualquer site (core/browser_local/grade.py).
    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn o_radio_de_grade_traz_grupo_linha_coluna_e_pergunta() {
        if super::super::headless::available() == false {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        super::super::headless::shutdown().await;
        let radio = |rotulo: &str, extra: &str| {
            format!("<div role=radio aria-checked={extra} aria-label='{rotulo}' style='display:inline-block;width:20px;height:20px;background:#ccc;margin:2px'></div>")
        };
        let mut html = String::from("<div role=list>");
        // 1. grade ARIA como a do Google, em inglês
        html += "<div role=listitem><div role=heading>RF-01: O sistema deve permitir sair. *</div>";
        for linha in ["Clareza", "Completude"] {
            html += &format!("<div role=radiogroup aria-label={linha}>");
            for coluna in ["Conforme", "Parcialmente"] {
                let marcado = if linha == "Completude" && coluna == "Parcialmente" { "true" } else { "false" };
                html += &radio(&format!("{coluna}, response for {linha}"), marcado);
            }
            html += "</div>";
        }
        html += "</div>";
        // 2. escolha simples ARIA: o grupo é a própria pergunta
        html += "<div role=listitem><div role=heading id=t2>Qual a sua escolaridade? *</div>\
                 <div role=radiogroup aria-labelledby=t2>";
        html += &radio("Superior", "false");
        html += &radio("Mestrado", "false");
        html += "</div></div></div>";
        // 3. matriz em tabela de rádios comuns
        html += "<table><caption>RF-02: O sistema deve exibir o perfil.</caption>\
                 <thead><tr><th></th><th>Conforme</th><th>Parcialmente</th></tr></thead><tbody>";
        for (i, linha) in ["Clareza", "Completude"].iter().enumerate() {
            html += &format!("<tr><td>{linha}</td><td><input type=radio name=rf02_{i} value=1></td>\
                              <td><input type=radio name=rf02_{i} value=2{}></td></tr>",
                             if i == 0 { " checked" } else { "" });
        }
        html += "</tbody></table>";
        // 4. escolha simples em fieldset
        html += "<fieldset><legend>Cor favorita</legend><label><input type=radio name=cor> Azul</label>\
                 <label><input type=radio name=cor> Verde</label></fieldset>";
        let pagina = format!(
            "data:text/html;charset=utf-8,{}",
            html.replace('%', "%25").replace('#', "%23")
        );
        let claim = execute("browser.claim", &json!({ "mode": "headless", "url": pagina }))
            .await
            .expect("claim headless");
        let tab = claim["tab_ref"].as_i64().expect("tab_ref");
        let snap = execute("browser.snapshot", &json!({ "tab_ref": tab })).await.expect("snapshot");
        if let Ok(caminho) = std::env::var("NEO_GRADE_SNAPSHOT_OUT") {
            std::fs::write(caminho, snap.to_string()).expect("gravar o snapshot");
        }
        let els = snap["elements"].as_array().expect("elements").clone();
        let ache = |texto: &str| {
            els.iter()
                .find(|e| e["text"] == texto)
                .unwrap_or_else(|| panic!("«{texto}» não entrou no inventário: {snap}"))
                .clone()
        };
        let a = ache("Conforme, response for Clareza");
        assert_eq!(a["grade"]["linha"], "Clareza", "{a}");
        assert_eq!(a["grade"]["pergunta"], "RF-01: O sistema deve permitir sair. *", "{a}");
        assert!(a["grade"]["grupo"].as_str().unwrap_or("").starts_with('g'), "{a}");
        let b = ache("Parcialmente, response for Completude");
        assert_ne!(a["grade"]["grupo"], b["grade"]["grupo"], "cada linha é um grupo");
        assert_eq!(b["checked"], true, "{b}");
        assert!(a.get("grupo").is_none(), "rádio de grade não é caixa: {a}");
        let s = ache("Superior");
        assert_eq!(s["grade"]["linha"], s["grade"]["pergunta"], "escolha simples: o grupo é a pergunta — {s}");
        let nativos: Vec<_> = els.iter().filter(|e| e["grade"]["grupo"] == "n:rf02_0").cloned().collect();
        assert_eq!(nativos.len(), 2, "{snap}");
        assert_eq!(nativos[0]["grade"]["linha"], "Clareza", "{}", nativos[0]);
        assert_eq!(nativos[0]["grade"]["coluna"], "Conforme", "{}", nativos[0]);
        assert_eq!(nativos[1]["grade"]["coluna"], "Parcialmente", "{}", nativos[1]);
        assert_eq!(nativos[1]["checked"], true, "{}", nativos[1]);
        assert_eq!(nativos[0]["grade"]["pergunta"], "RF-02: O sistema deve exibir o perfil.", "{}", nativos[0]);
        let cor: Vec<_> = els.iter().filter(|e| e["grade"]["grupo"] == "n:cor").cloned().collect();
        assert!(!cor.is_empty(), "{snap}");
        assert!(cor.iter().all(|e| e["grade"]["pergunta"] == "Cor favorita" && e["grade"].get("linha").is_none()), "{cor:?}");
        super::super::headless::shutdown().await;
    }

    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn o_clique_chega_no_controle_transparente_uma_vez_so() {
        if super::super::headless::available() == false {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        super::super::headless::shutdown().await;
        let claim = execute("browser.claim", &json!({ "mode": "headless", "url": BOTAO_DISFARCADO }))
            .await
            .expect("claim headless");
        let tab = claim["tab_ref"].as_i64().expect("tab_ref");

        let snap = execute("browser.snapshot", &json!({ "tab_ref": tab }))
            .await
            .expect("snapshot");
        assert_eq!(snap["capacidades"][0], "find");
        let botao = snap["elements"]
            .as_array()
            .expect("elements")
            .iter()
            .find(|e| e["tag"] == "input" && e["text"] == "Adicionar ao carrinho")
            .unwrap_or_else(|| panic!("o input transparente não entrou no inventário: {snap}"))
            .clone();

        let r = execute("browser.click", &json!({ "tab_ref": tab, "ref": botao["ref"] }))
            .await
            .expect("click");
        assert_eq!(r["via"], "pixel", "{r}");
        assert_eq!(contagem(tab).await, 1, "clique por ref");

        let r = execute("browser.click_text", &json!({ "tab_ref": tab, "texto": "Adicionar ao carrinho" }))
            .await
            .expect("click_text");
        assert_eq!(r["via"], "pixel", "{r}");
        assert_eq!(contagem(tab).await, 2, "clique por nome");

        let achados = execute("browser.find", &json!({ "tab_ref": tab, "texto": "adicionar ao carrinho" }))
            .await
            .expect("find");
        assert_eq!(achados["lista"][0]["tag"], "input", "{achados}");
        assert_eq!(achados["lista"][0]["recebe"], true, "{achados}");

        // A trava confere o alvo real, e clique barrado não encosta na página.
        let barrado = execute(
            "browser.click_text",
            &json!({ "tab_ref": tab, "texto": "carrinho", "trava": "\\badicionar\\b", "liberado": "" }),
        )
        .await
        .expect_err("a trava tinha de barrar");
        assert!(barrado.starts_with("SENSIVEL:Adicionar"), "{barrado}");
        assert_eq!(contagem(tab).await, 2, "clique barrado não pode chegar");

        // PARE apertado enquanto o modelo decidia: nada mais sai.
        eval(tab, "document.documentElement.setAttribute('data-neo-stop', '1')")
            .await
            .expect("marca o PARE");
        let parado = execute("browser.click", &json!({ "tab_ref": tab, "ref": botao["ref"] }))
            .await
            .expect_err("o PARE tinha de barrar");
        assert!(parado.starts_with("PARE:"), "{parado}");
        assert_eq!(contagem(tab).await, 2, "nada sai depois do PARE");

        execute("browser.release", &json!({ "tab_ref": tab })).await.expect("release");
        super::super::headless::shutdown().await;
    }

    #[test]
    fn a_trava_chega_na_pagina_como_js_valido() {
        let trava = trava_de(&json!({ "trava": "\\b(comprar|pagar)\\b", "liberado": "\"x\"" }));
        let valor: Value = serde_json::from_str(&trava).expect("a trava tem de ser JSON");
        assert_eq!(valor["trava"], "\\b(comprar|pagar)\\b");
        assert!(js_mirar(3, "duplo", &trava).contains("tipo: 'duplo'"));
        // Sem nada do core, nada é travado: é o device novo com o core antigo.
        assert_eq!(trava_de(&json!({})), r#"{"liberado":"","trava":""}"#);
    }

    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn health_headless_nao_depende_da_extensao() {
        if super::super::headless::available() == false {
            return;
        }
        // Este é o teste que importa: o host da extensão NÃO está no ar nesta
        // máquina, e mesmo assim o health tem que dizer que dá para trabalhar.
        // Antes, um health preso à extensão matava todo job headless na largada
        // — com a mensagem errada, mandando o usuário arrumar o que não era o
        // problema.
        let h = execute("browser.health", &json!({ "mode": "headless" }))
            .await
            .expect("health headless");
        assert_eq!(h["ok"], true);
        assert_eq!(h["backend"], "headless");
    }
}
