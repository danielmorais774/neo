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
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::sync::Mutex;

const SOCKET_RECORD: &str = "/tmp/open-browser-use/active.json";
const SESSION_ID: &str = "neo-browser-local";
/// Teto de uma chamada ao backend. O passo inteiro tem timeout do lado do core;
/// este aqui só impede que uma chamada pendurada segure a conexão para sempre.
const CALL_TIMEOUT: Duration = Duration::from_secs(30);

/// PARE apertado na faixa da página. A extensão manda a notificação `neo.stop`
/// pelo socket; guardamos aqui para o próximo `snapshot` responder na hora.
/// (O caminho CONFIÁVEL continua sendo o atributo `data-neo-stop` no HTML — se
/// a notificação se perder, o snapshot vê o atributo mesmo assim.)
static STOP_REQUESTED: AtomicBool = AtomicBool::new(false);

static CONN: OnceLock<Mutex<Option<Conn>>> = OnceLock::new();

fn conn_slot() -> &'static Mutex<Option<Conn>> {
    CONN.get_or_init(|| Mutex::new(None))
}

struct Conn {
    stream: UnixStream,
    next_id: u64,
    turn_id: String,
}

fn socket_path() -> Result<PathBuf, String> {
    let raw = std::fs::read_to_string(SOCKET_RECORD).map_err(|_| {
        "o navegador local não está com a extensão do Neo ativa (host do open-browser-use \
         não está no ar)"
            .to_string()
    })?;
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

            // Notificação (sem id): PARE é a única que nos interessa.
            if frame.get("id").is_none() {
                if frame.get("method").and_then(|v| v.as_str()) == Some("neo.stop") {
                    info!("[BrowserLocal] PARE recebido da faixa da página");
                    STOP_REQUESTED.store(true, Ordering::SeqCst);
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

async fn cdp(tab_id: i64, method: &str, params: Value) -> Result<Value, String> {
    call(
        "executeCdp",
        json!({
            "target": { "tabId": tab_id },
            "method": method,
            "commandParams": params,
        }),
    )
    .await
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
  NEO.el = (ref) => {
    const el = NEO.refs[ref];
    if (!el || !el.isConnected) return null;
    return el;
  };
  NEO.fire = (el, type, extra) =>
    el.dispatchEvent(new Event(type, { bubbles: true, cancelable: true, ...(extra || {}) }));
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
  let orcamento = 3000;
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
  const cands = [];
  const vistos = new Set();
  const origens = new Map(); // el -> como ele foi achado (entra no diário)
  const olhar = (el, origem) => {{
    if (!el || vistos.has(el)) return;
    if (el.closest('#neo-browser-local-overlay') || el.closest('#neo-marks')) return;
    // O inspetor visual da extensão desenha num overlay próprio. Ele já é
    // invisível ao mapeamento por não ter evidência e por ser pointer-events:none,
    // mas depender disso seria depender de um acidente.
    if (el.closest('#neo-map-overlay')) return;
    if (!visivel(el)) return;
    vistos.add(el);
    origens.set(el, origem || 'declarado');
    cands.push(el);
  }};
  for (const el of document.querySelectorAll(SEL)) {{
    if (cands.length >= {max}) break;
    olhar(el, 'declarado');
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
  let examinados = 0;
  for (const inp of document.querySelectorAll('input:not([type=hidden]),select,textarea')) {{
    if (examinados++ > 200 || cands.length >= {max}) break;
    if (vistos.has(inp)) continue;
    if (visivel(inp)) continue;
    // O `<label>` é o rosto de UM controle por definição — não precisa de limite.
    let alvo = (inp.labels && inp.labels[0]) || inp.closest('label') || null;
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
    if (!procuradores.has(alvo)) procuradores.set(alvo, inp);
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
      let no = null;
      try {{ no = document.elementFromPoint(x, y); }} catch (_) {{ continue; }}
      if (no) alvos.add(no);
    }}
  }}
  const negativos = new Set();
  const provas = new Map(); // el -> por que ele entrou (a poda abaixo usa isto)
  for (const no of alvos) {{
    if (cands.length >= {max} || orcamento <= 0) break;
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
    if (cands.length >= {max}) break;
    olhar(el, 'painel');
  }}

  for (const el of cands.slice()) {{
    if (cands.length >= {max}) break;
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
  const CHROME = 'nav,[role=navigation],header,[role=banner],footer,[role=contentinfo]';

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
    const item = {{ ref, tag: el.tagName.toLowerCase(), text: row ? NEO.shortLabel(el) : NEO.label(el) }};
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
          let topo = null;
          try {{ topo = document.elementFromPoint(x, y); }} catch (_) {{ continue; }}
          if (!topo) continue;
          if (topo === el || el.contains(topo) || topo.contains(el)) {{ acertou = true; break; }}
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
          if (camada) {{
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
    if (el.closest(CHROME)) item.chrome = true;

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
    if (el.matches(OPEN)) {{
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
      item.menu = true;
      const nome = NEO.shortLabel(painel);
      if (nome) item.panel = nome;
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
    if (el.tagName === 'A') item.href = String(el.href || '').slice(0, 300);
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
    const r = box(el);
    if (r.width * r.height < innerWidth * innerHeight * 0.08) return;
    const t = String(el.innerText || '').replace(/\n{{3,}}/g, '\n\n').trim();
    if (t.length < 150) return;
    if (!principal || t.length > principal.texto.length) principal = {{ el, texto: t }};
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
      let pp = el.parentElement, n = 0;
      while (pp && pp !== document.body && n++ < 6) {{
        if (!olhados.has(pp)) {{ olhados.add(pp); considerar(pp); }}
        pp = pp.parentElement;
      }}
    }}
  }}
  // MODAL ABERTO GANHA o lugar de conteúdo principal — enquanto está aberto, ele
  // É o conteúdo. Não passa pelo `considerar` de propósito: aquele exige 8% do
  // viewport e 150 caracteres, e escolhe o MAIOR texto — três critérios que um
  // modal legítimo perde para o `<main>` atrás dele. Na Amazon o vencedor foi um
  // `<nav>` de skip-links ("Ir para / Conteúdo principal / Atalhos do teclado"),
  // enquanto o modal com a resposta não entrou em lugar nenhum.
  const modal = [...document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal="true"]')]
    .filter((el) => visivel(el))
    .sort((a, b) => {{ const ra = box(a), rb = box(b); return rb.width * rb.height - ra.width * ra.height; }})[0];
  if (modal) {{
    const t = String(modal.innerText || modal.textContent || '').replace(/\n{{3,}}/g, '\n\n').trim();
    if (t.length >= 40) principal = {{ el: modal, texto: t }};
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
    // Com conteúdo principal identificado, o corpo (que é quase todo moldura)
    // fica com orçamento menor: o total no prompt não cresce, só muda de mãos.
    text: corpo.replace(/\n{{3,}}/g, '\n\n').trim().slice(0, principal ? 1500 : {chars}),
    main: principal
      ? {{ label: NEO.shortLabel(principal.el), text: principal.texto.slice(0, 2200) }}
      : null,
    truncated: elements.length >= {max},
    midia,
    elements,
  }};
}})()"#,
        helpers = JS_HELPERS,
        max = max_elements,
        chars = text_chars
    )
}

fn js_click(reference: i64) -> String {
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const el = NEO.el({reference});
  if (!el) return {{ ok: false, error: 'stale' }};
  if (el.disabled) return {{ ok: false, error: 'elemento desabilitado' }};
  el.scrollIntoView({{ block: 'nearest', inline: 'nearest' }});
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  // O rótulo é lido ANTES de clicar: num INTERRUPTOR ele vira no próprio clique,
  // e o histórico registrava o estado NOVO como se fosse o que foi clicado —
  // "Reproduzir" no clique que acabara de PAUSAR (job 717a8fbb).
  const rotulo = NEO.label(el);
  const opts = {{ bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, button: 0 }};
  // Sequência completa: sites que só escutam pointerdown/mouseup não reagem a
  // um .click() seco.
  // Pulso visual no ponto do clique: a aba mexendo sozinha sem nenhum sinal é
  // desconcertante para quem está olhando. Quem desenha é o shadow DOM da
  // extensão (nenhum nó novo na página) — daí ser um postMessage e não um
  // appendChild aqui.
  postMessage({{ __neo_ping: {{ x, y, tipo: 'esquerdo' }} }}, '*');
  el.dispatchEvent(new PointerEvent('pointerdown', opts));
  el.dispatchEvent(new MouseEvent('mousedown', opts));
  el.focus && el.focus();
  el.dispatchEvent(new PointerEvent('pointerup', opts));
  el.dispatchEvent(new MouseEvent('mouseup', opts));
  el.click();
  // O clique virou SELEÇÃO? É a única evidência de efeito que existe para
  // "marque a alternativa": marcar um rádio não muda URL, nem título, nem
  // texto — o freio de tela repetida é cego para isso por construção. O estado
  // sai do CONTROLE, inclusive do input escondido atrás do label.
  const out = {{ ok: true, url: location.href, label: rotulo }};
  const ctl = (el.tagName === 'LABEL' ? el.control : null) ||
    (el.tagName === 'INPUT' ? el : null) ||
    el.querySelector('input[type=radio],input[type=checkbox]');
  const tipo = ctl && ctl.getAttribute('type');
  if (tipo === 'radio' || tipo === 'checkbox') out.checked = !!ctl.checked;
  else {{
    const aria = el.getAttribute('aria-checked') || el.getAttribute('aria-selected');
    if (aria === 'true' || aria === 'false') out.checked = aria === 'true';
  }}
  return out;
}})()"#,
        helpers = JS_HELPERS,
        reference = reference
    )
}

/// Clique por TEXTO VISÍVEL — a saída para o que se LÊ e não se consegue
/// endereçar.
///
/// Existe porque todo inventário de elementos é uma aposta, e aposta erra: no
/// job 9968e68a as 5 alternativas do questionário não tinham `ref` nenhum, o
/// modelo de visão LEU as cinco corretamente e, obrigado a citar um número que
/// não existia, inventou — clicou no menu lateral cinco vezes. Com esta ação a
/// pergunta deixa de ser "qual o número?" e passa a ser "qual o texto?", que é
/// verificável e que o modelo sempre tem.
///
/// É o `getByText` do Playwright: acha o MENOR elemento visível cujo texto
/// contém a frase e clica no ancestral acionável mais próximo dele.
fn js_click_text(needle: &str) -> String {
    let encoded = serde_json::to_string(needle).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  // Acento e caixa não podem decidir se o clique acontece: o modelo escreve a
  // frase de memória, a partir do que leu no print ou no texto da página.
  const norm = (v) => String(v || '')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, ' ').trim().toLowerCase();
  const alvo = norm({needle});
  if (alvo.length < 3) return {{ ok: false, error: 'texto curto demais para achar com segurança (mínimo 3 letras)' }};

  const visivel = (el) => {{
    if (!el || el.nodeType !== 1) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') >= 0.05;
  }};
  const ACIONAVEL = 'a[href],button,input,select,textarea,label,summary,[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=option],[role=menuitem],[role=tab],[role=row],' +
    '[onclick],[ng-click],[data-ng-click],[jsaction],[data-action],[tabindex="0"]';

  // Nós de TEXTO, não elementos: é o que garante que a frase casada é a que o
  // usuário lê, e não a concatenação do innerText de meia página.
  const achados = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let no;
  while ((no = walker.nextNode())) {{
    if (achados.length >= 40) break;
    if (!no.nodeValue || no.nodeValue.length < 3) continue;
    if (!norm(no.nodeValue).includes(alvo)) continue;
    const el = no.parentElement;
    if (!el || el.closest('#neo-browser-local-overlay,#neo-marks')) continue;
    if (!visivel(el)) continue;
    achados.push(el);
  }}
  if (!achados.length) {{
    return {{ ok: false, error: 'não achei esse texto na página. Confira a grafia, ou role até a parte onde ele está' }};
  }}

  // De cada casamento, sobe até o ancestral que de fato recebe clique. Sem
  // isso o clique cai no <span> do rótulo, que em site com handler no
  // contêiner não faz nada.
  const clicaveis = [];
  for (const el of achados) {{
    let escolhido = el.closest(ACIONAVEL);
    if (!escolhido || !visivel(escolhido)) {{
      let p = el, n = 0;
      escolhido = el;
      while (p && n++ < 6) {{
        if (visivel(p) && getComputedStyle(p).cursor === 'pointer') {{ escolhido = p; break; }}
        p = p.parentElement;
      }}
    }}
    if (escolhido && visivel(escolhido) && !clicaveis.includes(escolhido)) clicaveis.push(escolhido);
  }}
  if (!clicaveis.length) return {{ ok: false, error: 'achei o texto mas nada clicável em volta dele' }};

  // O MENOR: o mais específico. Um texto casa com a página inteira, com a
  // seção e com a opção — a opção é a que tem a menor área.
  clicaveis.sort((a, b) => {{
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return ra.width * ra.height - rb.width * rb.height;
  }});
  const el = clicaveis[0];
  el.scrollIntoView({{ block: 'nearest', inline: 'nearest' }});
  const r = el.getBoundingClientRect();
  const x = Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1);
  const y = Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1);
  // Coberto = o usuário não conseguiria clicar aqui. Despachar o evento assim
  // mesmo devolveria "ok" com a página inerte, que é a mentira que este
  // desenho inteiro combate.
  let topo = null;
  try {{ topo = document.elementFromPoint(x, y); }} catch (_) {{ topo = null; }}
  if (topo && topo !== el && !el.contains(topo) && !topo.contains(el)) {{
    return {{
      ok: false,
      error: 'esse texto está COBERTO por "' + (NEO.shortLabel(topo) || topo.tagName.toLowerCase()) +
        '" — feche ou dispense o que está na frente antes de clicar',
    }};
  }}
  const rotulo = NEO.label(el);
  const opts = {{ bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, button: 0 }};
  postMessage({{ __neo_ping: {{ x, y, tipo: 'esquerdo' }} }}, '*');
  el.dispatchEvent(new PointerEvent('pointerdown', opts));
  el.dispatchEvent(new MouseEvent('mousedown', opts));
  el.focus && el.focus();
  el.dispatchEvent(new PointerEvent('pointerup', opts));
  el.dispatchEvent(new MouseEvent('mouseup', opts));
  el.click();
  const out = {{ ok: true, url: location.href, label: rotulo, achados: clicaveis.length }};
  const ctl = (el.tagName === 'LABEL' ? el.control : null) ||
    (el.tagName === 'INPUT' ? el : null) ||
    el.querySelector('input[type=radio],input[type=checkbox]');
  const tipo = ctl && ctl.getAttribute('type');
  if (tipo === 'radio' || tipo === 'checkbox') out.checked = !!ctl.checked;
  else {{
    const aria = el.getAttribute('aria-checked') || el.getAttribute('aria-selected');
    if (aria === 'true' || aria === 'false') out.checked = aria === 'true';
  }}
  // O número, quando existe: o modelo volta a poder usar `click`/`extract` nele.
  if (NEO && NEO.refs) {{
    const i = NEO.refs.indexOf(el);
    if (i >= 0) out.ref = i;
  }}
  return out;
}})()"#,
        helpers = JS_HELPERS,
        needle = encoded
    )
}

/// Duplo clique. Em lista de arquivos (Drive, Finder web, webmail) o clique
/// simples só SELECIONA — abrir é sempre `dblclick`.
fn js_dblclick(reference: i64) -> String {
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const el = NEO.el({reference});
  if (!el) return {{ ok: false, error: 'stale' }};
  if (el.disabled) return {{ ok: false, error: 'elemento desabilitado' }};
  el.scrollIntoView({{ block: 'nearest', inline: 'nearest' }});
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  // O rótulo é lido ANTES de clicar: num INTERRUPTOR ele vira no próprio clique,
  // e o histórico registrava o estado NOVO como se fosse o que foi clicado —
  // "Reproduzir" no clique que acabara de PAUSAR (job 717a8fbb).
  const rotulo = NEO.label(el);
  const opts = {{ bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, button: 0 }};
  // Pulso visual no ponto do clique: a aba mexendo sozinha sem nenhum sinal é
  // desconcertante para quem está olhando. Quem desenha é o shadow DOM da
  // extensão (nenhum nó novo na página) — daí ser um postMessage e não um
  // appendChild aqui.
  postMessage({{ __neo_ping: {{ x, y, tipo: 'esquerdo' }} }}, '*');
  for (let i = 1; i <= 2; i++) {{
    el.dispatchEvent(new PointerEvent('pointerdown', opts));
    el.dispatchEvent(new MouseEvent('mousedown', opts));
    el.dispatchEvent(new PointerEvent('pointerup', opts));
    el.dispatchEvent(new MouseEvent('mouseup', opts));
    el.dispatchEvent(new MouseEvent('click', {{ ...opts, detail: i }}));
  }}
  el.dispatchEvent(new MouseEvent('dblclick', {{ ...opts, detail: 2 }}));
  return {{ ok: true, url: location.href, label: rotulo }};
}})()"#,
        helpers = JS_HELPERS,
        reference = reference
    )
}

/// Botão direito. É o único caminho para o menu de item ("Baixar", "Renomear")
/// na maioria dos gerenciadores de arquivo na web.
fn js_right_click(reference: i64) -> String {
    format!(
        r#"{helpers};
(() => {{
  const NEO = window.__neo;
  const el = NEO.el({reference});
  if (!el) return {{ ok: false, error: 'stale' }};
  el.scrollIntoView({{ block: 'nearest', inline: 'nearest' }});
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  // O rótulo é lido ANTES de clicar: num INTERRUPTOR ele vira no próprio clique,
  // e o histórico registrava o estado NOVO como se fosse o que foi clicado —
  // "Reproduzir" no clique que acabara de PAUSAR (job 717a8fbb).
  const rotulo = NEO.label(el);
  const opts = {{ bubbles: true, cancelable: true, view: window, clientX: x, clientY: y, button: 2, buttons: 2 }};
  // Pulso visual no ponto do clique: a aba mexendo sozinha sem nenhum sinal é
  // desconcertante para quem está olhando. Quem desenha é o shadow DOM da
  // extensão (nenhum nó novo na página) — daí ser um postMessage e não um
  // appendChild aqui.
  postMessage({{ __neo_ping: {{ x, y, tipo: 'direito' }} }}, '*');
  el.dispatchEvent(new PointerEvent('pointerdown', opts));
  el.dispatchEvent(new MouseEvent('mousedown', opts));
  el.focus && el.focus();
  el.dispatchEvent(new PointerEvent('pointerup', opts));
  el.dispatchEvent(new MouseEvent('mouseup', opts));
  el.dispatchEvent(new MouseEvent('contextmenu', opts));
  return {{ ok: true, url: location.href, label: rotulo }};
}})()"#,
        helpers = JS_HELPERS,
        reference = reference
    )
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
  const el = NEO.el({reference});
  if (!el) return {{ ok: false, error: 'stale' }};
  const value = {value};
  el.scrollIntoView({{ block: 'nearest' }});
  el.focus();
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
  const el = NEO.el({reference});
  if (!el) return {{ ok: false, error: 'stale' }};
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
    const noPonto = (x, y) => {{
      try {{
        return document.elementFromPoint ? document.elementFromPoint(x, y) : null;
      }} catch (_) {{
        return null;
      }}
    }};
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
  if (!alvo) alvo = subindo(document.elementFromPoint(innerWidth / 2, innerHeight / 2));
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

fn js_extract(reference: Option<i64>, chars: usize) -> String {
    let target = match reference {
        Some(r) => format!("window.__neo.el({})", r),
        None => "document.body".to_string(),
    };
    format!(
        r#"{helpers};
(() => {{
  const el = {target};
  if (!el) return {{ ok: false, error: 'stale' }};
  return {{ ok: true, text: String(el.innerText || el.textContent || '').replace(/\n{{3,}}/g, '\n\n').trim().slice(0, {chars}) }};
}})()"#,
        helpers = JS_HELPERS,
        target = target,
        chars = chars
    )
}

fn js_banner(text: &str, active: bool, stopped: bool) -> String {
    let encoded = serde_json::to_string(text).unwrap_or_else(|_| "\"\"".to_string());
    format!(
        r#"(() => {{
  window.postMessage({{ __neo_overlay: {{ active: {active}, text: {text}, stopped: {stopped} }} }}, '*');
  return {{ ok: true }};
}})()"#,
        active = active,
        stopped = stopped,
        text = encoded
    )
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
    match action {
        // ── saúde / abas ──
        "browser.health" => {
            let info = call("getInfo", json!({})).await?;
            Ok(json!({ "ok": true, "backend": info }))
        }
        "browser.tabs" => {
            let tabs = call("getUserTabs", json!({})).await?;
            Ok(json!({ "tabs": tabs }))
        }

        // ── reivindicar a aba (é o que dá o tab_ref de todo o resto) ──
        "browser.claim" => {
            STOP_REQUESTED.store(false, Ordering::SeqCst);
            let explicit = args.get("tab_id").and_then(|v| v.as_i64());
            let url = args.get("url").and_then(|v| v.as_str()).unwrap_or("");

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
            let mut snapshot = eval(id, &js_snapshot(max, chars)).await?;
            // O flag do PARE tem duas fontes: o atributo no HTML (confiável,
            // sobrevive à notificação perdida) e a notificação (rápida).
            if stop_requested() {
                if let Some(object) = snapshot.as_object_mut() {
                    object.insert("stop".to_string(), Value::Bool(true));
                }
            }
            Ok(snapshot)
        }

        "browser.extract" => {
            let id = tab_id(args)?;
            let reference = args.get("ref").and_then(|v| v.as_i64());
            let chars = args
                .get("chars")
                .and_then(|v| v.as_u64())
                .unwrap_or(4000) as usize;
            unwrap_ok(eval(id, &js_extract(reference, chars)).await?)
        }

        // ── atuação ──
        "browser.click" => {
            let id = tab_id(args)?;
            let reference = arg_i64(args, "ref")?;
            unwrap_ok(eval(id, &js_click(reference)).await?)
        }
        // Saída para o que se lê e não se endereça — ver `js_click_text`.
        "browser.click_text" => {
            let id = tab_id(args)?;
            let texto = arg_str(args, "texto")?;
            unwrap_ok(eval(id, &js_click_text(&texto)).await?)
        }
        "browser.dblclick" => {
            let id = tab_id(args)?;
            let reference = arg_i64(args, "ref")?;
            unwrap_ok(eval(id, &js_dblclick(reference)).await?)
        }
        "browser.right_click" => {
            let id = tab_id(args)?;
            let reference = arg_i64(args, "ref")?;
            unwrap_ok(eval(id, &js_right_click(reference)).await?)
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
            press_key(id, key).await
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
            let text = args.get("text").and_then(|v| v.as_str()).unwrap_or("");
            let active = args.get("active").and_then(|v| v.as_bool()).unwrap_or(true);
            let _ = eval(id, &js_banner(text, active, false)).await?;
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

            let shot = cdp(id, "Page.captureScreenshot", params).await?;
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
            // Melhor esforço: a aba pode já ter sido fechada pelo usuário, e
            // nesse caso "soltar" já está feito.
            let _ = eval(id, &js_banner("", false, false)).await;
            let _ = call("detach", json!({ "tabId": id })).await;
            let _ = call("turnEnded", json!({})).await;
            STOP_REQUESTED.store(false, Ordering::SeqCst);
            info!("[BrowserLocal] aba {} liberada", id);
            Ok(json!({ "ok": true }))
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
