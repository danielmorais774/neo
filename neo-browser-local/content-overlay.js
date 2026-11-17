// Painel do Neo na aba controlada: a LINHA DO TEMPO do que o agente está
// fazendo, na lateral, com o botão PARE.  [ADIÇÃO DO FORK — não existe no upstream]
//
// Era uma faixa no topo com UMA linha de texto, substituída a cada passo. O que
// o agente fez há dois passos sumia sem deixar rastro, e o que ele PRODUZIU
// (resposta do `look`, o que anotou no caderno, o que falhou e por quê) nunca
// chegava a aparecer: cabia uma frase só. Quem estava olhando a aba mexer
// sozinha via o presente e nada mais.
//
// Agora cada evento vira um item EMPILHADO: hora, o que está sendo feito, e o
// retorno. O passo atual é o último da lista; o histórico fica acima, rolável.
//
// Por que um content script e não um <div> injetado por CDP: o content script é
// re-executado pelo próprio Chrome a cada navegação, então o painel sobrevive ao
// clique que troca de página. Um div injetado por Runtime.evaluate morre no
// primeiro load e o usuário fica sem saber que o Neo ainda está agindo. É também
// por isso que a lista é persistida no background (canal 2): a linha do tempo
// não pode recomeçar do zero toda vez que o agente navega.
//
// Três canais, todos de mão única para dentro daqui:
//   • mundo principal (CDP Runtime.evaluate) → window.postMessage({__neo_overlay})
//     — é assim que o neo-desktop empurra cada evento;
//   • background (chrome.runtime) → estado persistido, para re-montar o painel
//     depois de uma navegação;
//   • PARE → atributo no <html> (o driver lê no próximo snapshot) + notificação
//     neo.stop para o host (caminho rápido).
//
// O atributo data-neo-stop é a via CONFIÁVEL: o snapshot já roda a cada passo,
// então o freio funciona mesmo se a notificação se perder.
//
// A CONVERSA (neo-core/docs/browser-conversa-plan.md) acrescenta dois:
//   • a caixa de texto → `NEO_MSG` para o background → `neo.msg` → o driver lê
//     no próximo passo. NUNCA pelo DOM, como o PARE: um atributo a página
//     escreve; a mensagem tem a autoridade do Daniel. Só evento CONFIÁVEL
//     (`isTrusted`) envia — script da página não digita aqui;
//   • a PERGUNTA do Neo (com botões) chega só pelo background
//     (`NEO_PERGUNTA` / estado restaurado), nunca pelo `postMessage` do canal 1,
//     que a página imita. Item "pergunta" vindo do canal 1 é só texto na lista.
//
// ⚠️ O id do host (`neo-browser-local-overlay`) é EXCLUÍDO do inventário de
// elementos pelo driver (browser_local.rs). Mudar o id faz o agente enxergar o
// próprio painel como se fosse conteúdo da página.

const STOP_ATTR = "data-neo-stop";
const HOST_ID = "neo-browser-local-overlay";
const MAX_ITENS = 60; // teto da lista — também é o que vai para o storage da sessão
const CHAVE_RECOLHIDO = "neoPainelRecolhido";

let host = null;
let raizEl = null;
let painelEl = null;
let railEl = null;
let listaEl = null;
let estadoEl = null;
let contaEl = null;
let stopEl = null;
let fxEl = null;
let perguntaEl = null;
let ptxtEl = null;
let popcoesEl = null;
let caixaEl = null;
let msgEl = null;
/** A pergunta que o Neo fez e está esperando — só o background escreve isto. */
let pergunta = null;
const MSG_MAX = 1500;

// Enquanto ele ESCREVE, o agente não mexe na página (relato de 2026-11-12: a
// cada clique do agente o cursor saía do campo). O carimbo `data-neo-digitando`
// é lido pelo desktop antes de cada ação; com o campo focado e rascunho, ele é
// renovado a cada segundo até 30 s sem tecla. Antes de agir, o desktop carimba
// `data-neo-agindo` e o campo perde o foco (o texto fica): tecla do agente
// nunca cai no rascunho — um Enter dele mandaria a frase pela metade.
const DIGITANDO_ATTR = "data-neo-digitando";
const AGINDO_ATTR = "data-neo-agindo";
const RASCUNHO_PARADO_MS = 30000;
let sombra = null;
let ultimaTecla = 0;
let vigiaDigitando = null;
let rascunhoTimer = null;
let observandoAgente = false;

let state = { active: false, stopped: false, items: [] };
let recolhido = false;
let grudado = true; // a lista acompanha o fim, a não ser que o usuário role para cima
let seq = 0;

// ── montagem ─────────────────────────────────────────────────────────────────

function ensureHost() {
  if (host && host.isConnected) return host;
  if (!document.documentElement) return null;

  host = document.createElement("div");
  host.id = HOST_ID;
  // pointer-events: none no container — só o painel em si captura clique. Sem
  // isso a lateral roubaria clique da página em toda a altura da janela.
  // `all:initial` PRIMEIRO, sempre. Ele é um atalho que reseta TODA propriedade,
  // então declarado por último ele apagava o position/top/z-index/pointer-events
  // que vinham antes: o painel virava um bloco estático no fim do documento,
  // ocupava espaço de verdade na página e ainda roubava clique (sem
  // pointer-events:none). Era isso que empurrava o conteúdo do site.
  host.style.cssText = [
    "all:initial",
    "position:fixed", "top:0", "right:0", "bottom:0", "left:auto", "width:auto",
    "z-index:2147483647", "pointer-events:none",
  ].join(";");

  const shadow = host.attachShadow({ mode: "closed" });
  sombra = shadow;
  const style = document.createElement("style");
  style.textContent = CSS;

  // Camada de efeitos: o usuário precisa VER onde o Neo clicou, senão a aba
  // parece mexer sozinha. Fica no mesmo shadow do painel — nenhum nó novo na
  // página, nenhum reflow (fixed + pointer-events:none).
  fxEl = document.createElement("div");
  fxEl.className = "fx";

  painelEl = document.createElement("div");
  painelEl.className = "painel";
  painelEl.innerHTML = `
    <div class="cabeca">
      <span class="dot"></span>
      <span class="tag">Neo</span>
      <span class="estado"></span>
      <button class="recolher" title="Recolher">&#8211;</button>
    </div>
    <ol class="trilha"></ol>
    <div class="pergunta" hidden>
      <div class="prot">o Neo está esperando você</div>
      <div class="ptxt"></div>
      <div class="popcoes"></div>
    </div>
    <form class="caixa" autocomplete="off" hidden>
      <textarea class="msg" rows="2" maxlength="${MSG_MAX}"
        placeholder="Fale com o Neo — ele lê no próximo passo"></textarea>
      <button type="submit" class="enviar" title="Enviar (Enter)">Enviar</button>
    </form>
    <div class="pe"><button class="stop">PARE</button></div>
  `;

  // Recolhido, o painel vira uma pastilha: continua dizendo que o Neo está
  // agindo (e quantos passos já deu) sem cobrir a página.
  railEl = document.createElement("button");
  railEl.className = "rail";
  railEl.innerHTML = `<span class="dot"></span><span class="tag">Neo</span><span class="conta">0</span>`;
  railEl.addEventListener("click", () => setRecolhido(false));

  // As classes de modo (recolhido / parando / encerrado) valem para o painel E
  // para a pastilha, então moram num nó que contém os dois: o host do shadow
  // não serve, por ser `closed`.
  raizEl = document.createElement("div");
  raizEl.className = "raiz";
  raizEl.append(painelEl, railEl);
  shadow.append(style, fxEl, raizEl);

  listaEl = painelEl.querySelector(".trilha");
  estadoEl = painelEl.querySelector(".estado");
  contaEl = railEl.querySelector(".conta");
  stopEl = painelEl.querySelector(".stop");
  stopEl.addEventListener("click", requestStop);
  perguntaEl = painelEl.querySelector(".pergunta");
  ptxtEl = painelEl.querySelector(".ptxt");
  popcoesEl = painelEl.querySelector(".popcoes");
  caixaEl = painelEl.querySelector(".caixa");
  msgEl = painelEl.querySelector(".msg");
  caixaEl.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!e.isTrusted) return;
    enviarMensagem(msgEl.value);
  });
  // O que se digita aqui é para o Neo, não para a página: sem isto a tecla
  // subia até o `document` e disparava atalho do site (o "k" que pausa vídeo).
  msgEl.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (!e.isTrusted) return;
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      enviarMensagem(msgEl.value);
      return;
    }
    tocou();
  });
  msgEl.addEventListener("input", (e) => {
    e.stopPropagation();
    if (!e.isTrusted) return;
    tocou();
    agendarRascunho();
  });
  for (const tipo of ["keyup", "keypress", "paste", "copy", "cut"]) {
    msgEl.addEventListener(tipo, (e) => e.stopPropagation());
  }
  msgEl.addEventListener("focus", (e) => {
    if (!e.isTrusted) return;
    tocou();
    clearInterval(vigiaDigitando);
    vigiaDigitando = setInterval(() => {
      const focado = sombra && sombra.activeElement === msgEl;
      if (focado && msgEl.value.trim() && Date.now() - ultimaTecla < RASCUNHO_PARADO_MS) {
        marcarDigitando();
      }
    }, 1000);
  });
  msgEl.addEventListener("blur", () => {
    clearInterval(vigiaDigitando);
    vigiaDigitando = null;
  });
  observarAgente();
  painelEl.querySelector(".recolher").addEventListener("click", () => setRecolhido(true));

  // Rolou para cima = quer ler o histórico; o painel para de puxar o fim até
  // ele voltar para baixo.
  listaEl.addEventListener("scroll", () => {
    grudado = listaEl.scrollHeight - listaEl.scrollTop - listaEl.clientHeight < 28;
  });

  document.documentElement.appendChild(host);
  return host;
}

const CSS = `
  .painel {
    pointer-events:auto;
    /* Sem "bottom": a altura acompanha a lista e só então bate no teto. Preso
       em cima E embaixo, o painel cobria a lateral inteira da página para
       mostrar dois itens. */
    position:fixed; top:12px; right:12px; max-height:calc(100vh - 24px);
    width:clamp(232px, 26vw, 312px);
    display:flex; flex-direction:column; overflow:hidden;
    font:500 12.5px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    color:#e6f1ff; background:linear-gradient(180deg,#0b1220 0%,#0e1829 100%);
    border:1px solid #1e3a5f; border-radius:12px;
    box-shadow:0 10px 36px rgba(0,0,0,.45);
  }
  .recolhido .painel { display:none; }
  .rail { display:none; }
  .recolhido .rail {
    pointer-events:auto; cursor:pointer;
    position:fixed; top:12px; right:12px;
    display:flex; align-items:center; gap:7px;
    font:600 12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    color:#e6f1ff; background:linear-gradient(90deg,#0b1220,#122036);
    border:1px solid #1e3a5f; border-radius:999px; padding:8px 12px;
    box-shadow:0 6px 20px rgba(0,0,0,.4);
  }
  .rail .conta { color:#7dd3fc; font-variant-numeric:tabular-nums; }

  .cabeca {
    display:flex; align-items:center; gap:8px; flex:0 0 auto;
    padding:10px 10px 10px 12px; border-bottom:1px solid #16304f;
    background:linear-gradient(90deg,#0b1220,#122036);
  }
  .dot { width:8px; height:8px; border-radius:50%; background:#22c55e;
         box-shadow:0 0 8px #22c55e; animation:pulse 1.4s ease-in-out infinite; flex:0 0 auto; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  .tag { color:#7dd3fc; letter-spacing:.08em; text-transform:uppercase; font-size:10.5px; font-weight:700; flex:0 0 auto; }
  .estado { flex:1 1 auto; color:#93a8c4; font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .recolher {
    flex:0 0 auto; cursor:pointer; width:22px; height:22px; padding:0;
    font:700 15px/1 -apple-system,sans-serif; color:#93a8c4;
    background:transparent; border:1px solid #1e3a5f; border-radius:6px;
  }
  .recolher:hover { color:#e6f1ff; border-color:#2f598f; }

  .trilha {
    flex:1 1 auto; overflow-y:auto; overflow-x:hidden; margin:0;
    padding:8px 10px 10px 12px; list-style:none;
    scrollbar-width:thin; scrollbar-color:#1e3a5f transparent;
  }
  .trilha::-webkit-scrollbar { width:8px; }
  .trilha::-webkit-scrollbar-thumb { background:#1e3a5f; border-radius:8px; }

  .it {
    position:relative; display:grid; grid-template-columns:auto 1fr; gap:0 8px;
    padding:5px 0 5px 16px; cursor:default;
  }
  /* o fio da linha do tempo */
  .it::before {
    content:""; position:absolute; left:3px; top:0; bottom:0; width:1px; background:#1b3254;
  }
  .it:first-child::before { top:11px; }
  .it:last-child::before { bottom:auto; height:11px; }
  .marca {
    position:absolute; left:0; top:8px; width:7px; height:7px; border-radius:50%;
    background:#7dd3fc; box-shadow:0 0 0 2px #0b1220;
  }
  .hora { grid-column:1; color:#5d799e; font-size:10.5px; font-variant-numeric:tabular-nums; padding-top:1px; }
  .corpo { grid-column:2; min-width:0; }
  .txt { color:#dce9fb; overflow-wrap:anywhere; }
  .det {
    margin-top:2px; color:#8fa6c2; font-size:11.5px; overflow-wrap:anywhere;
    display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden;
  }
  .aberto .det { -webkit-line-clamp:unset; }
  .det.cortavel { cursor:pointer; }
  .vezes { color:#5d799e; font-size:10.5px; }

  .it--fase .marca { background:#64748b; }
  .it--fase .txt { color:#9fb3cd; }
  .it--passo .marca { background:#7dd3fc; }
  .it--ok .marca { background:#22c55e; }
  .it--erro .marca { background:#ef4444; }
  .it--erro .txt { color:#fca5a5; }
  .it--fim .marca { background:#22c55e; box-shadow:0 0 0 2px #0b1220, 0 0 9px #22c55e; }
  .it--fim .txt { color:#bbf7d0; }
  .it--parado .marca { background:#f59e0b; }
  .it:last-child .marca { animation:pulse 1.4s ease-in-out infinite; }

  .it--voce .marca { background:#e9d5ff; }
  .it--voce .txt { color:#e9d5ff; }
  .it--aviso .marca { background:#a78bfa; }
  .it--aviso .txt { color:#ddd6fe; }
  .it--pergunta .marca { background:#f59e0b; }
  .it--pergunta .txt { color:#fde68a; }
  /* Print de confirmação (📷) e a conferência da visão (👁): marcador QUADRADO,
     para o olho achar na timeline onde o Neo fotografou. */
  .it--print .marca { background:#c084fc; border-radius:2px; }
  .it--print .txt { color:#e9d5ff; }
  .it--visto .marca { background:#34d399; border-radius:2px; }
  .it--visto .txt { color:#a7f3d0; }

  .pergunta {
    flex:0 0 auto; padding:9px 10px 10px 12px; border-top:1px solid #4a3a12;
    background:linear-gradient(180deg,#1f1a0b,#171307);
  }
  .pergunta[hidden], .caixa[hidden] { display:none; }
  .prot { color:#fbbf24; font-size:10.5px; letter-spacing:.06em; text-transform:uppercase; font-weight:700; }
  .ptxt {
    margin-top:4px; color:#fde68a; white-space:pre-wrap; overflow-wrap:anywhere;
    max-height:32vh; overflow-y:auto;
  }
  .popcoes { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .opcao {
    cursor:pointer; font:600 12px/1.2 -apple-system,BlinkMacSystemFont,sans-serif;
    color:#fff; background:#7c3aed; border:0; border-radius:7px; padding:7px 10px;
  }
  .opcao:hover { background:#8b5cf6; }

  .caixa { flex:0 0 auto; display:flex; gap:6px; align-items:flex-end; padding:8px 10px 0; }
  .msg {
    flex:1 1 auto; min-width:0; resize:none; box-sizing:border-box;
    font:500 12.5px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    color:#e6f1ff; background:#0a1424; border:1px solid #1e3a5f; border-radius:7px;
    padding:7px 8px; outline:none;
  }
  .msg:focus { border-color:#2f598f; }
  .enviar {
    flex:0 0 auto; cursor:pointer; font:700 12px/1 -apple-system,BlinkMacSystemFont,sans-serif;
    color:#fff; background:#2563eb; border:0; border-radius:7px; padding:9px 11px;
  }
  .enviar:hover { background:#3b82f6; }

  .pe { flex:0 0 auto; padding:8px 10px; border-top:1px solid #16304f; }
  .stop {
    width:100%; cursor:pointer;
    font:700 12px/1 -apple-system,BlinkMacSystemFont,sans-serif; letter-spacing:.06em;
    color:#fff; background:#dc2626; border:0; border-radius:7px; padding:9px 14px;
  }
  .stop:hover { background:#ef4444; }

  .parando .dot, .parando .it:last-child .marca { background:#f59e0b; box-shadow:0 0 8px #f59e0b; animation:none; }
  .parando .stop { background:#4b5563; cursor:default; }
  .encerrado .dot { background:#64748b; box-shadow:none; animation:none; }
  .encerrado .it:last-child .marca { animation:none; }
  .encerrado .stop { display:none; }

  .fx { position:fixed; inset:0; pointer-events:none; }
  .ping {
    position:fixed; width:34px; height:34px; margin:-17px 0 0 -17px;
    border-radius:50%; border:2px solid #7dd3fc; background:rgba(125,211,252,.18);
    animation:ping .5s ease-out forwards;
  }
  .ping.direito { border-color:#fbbf24; background:rgba(251,191,36,.18); }
  @keyframes ping {
    0%   { transform:scale(.25); opacity:1 }
    100% { transform:scale(1.6); opacity:0 }
  }
`;

/** Pulso no ponto do clique. Some sozinho — nada fica pendurado no DOM. */
function ping(x, y, tipo) {
  const h = ensureHost();
  if (!h || !fxEl) return;
  const dot = document.createElement("div");
  dot.className = tipo === "direito" ? "ping direito" : "ping";
  dot.style.left = `${Math.round(x)}px`;
  dot.style.top = `${Math.round(y)}px`;
  fxEl.appendChild(dot);
  setTimeout(() => dot.remove(), 600);
}

// ── linha do tempo ───────────────────────────────────────────────────────────

function hora(ts) {
  const d = new Date(ts || Date.now());
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function itemEl(item) {
  const li = document.createElement("li");
  li.className = `it it--${item.kind || "passo"}`;
  li.dataset.id = String(item.id);

  const marca = document.createElement("span");
  marca.className = "marca";

  const h = document.createElement("span");
  h.className = "hora";
  h.textContent = hora(item.at);

  const corpo = document.createElement("div");
  corpo.className = "corpo";
  const txt = document.createElement("div");
  txt.className = "txt";
  txt.textContent = item.text || "";
  corpo.append(txt);
  if (item.vezes > 1) {
    const v = document.createElement("span");
    v.className = "vezes";
    v.textContent = ` ×${item.vezes}`;
    txt.append(v);
  }
  if (item.detail) {
    const det = document.createElement("div");
    det.className = "det";
    det.textContent = item.detail;
    corpo.append(det);
    // Detalhe longo (resposta do `look`, erro inteiro) nasce cortado em 3
    // linhas: a lista tem que caber na lateral. Um clique abre o que sobrou —
    // sem isso o retorno do agente ficaria pela metade, que é o problema que
    // este painel existe para resolver.
    requestAnimationFrame(() => {
      if (det.scrollHeight > det.clientHeight + 2) {
        det.classList.add("cortavel");
        det.title = "clique para ver tudo";
        det.addEventListener("click", () => li.classList.toggle("aberto"));
      }
    });
  }

  li.append(marca, h, corpo);
  return li;
}

/** Re-desenha só o que falta — a lista não pode piscar a cada passo. */
function renderLista() {
  if (!listaEl) return;
  const itens = state.items;
  const ultimoDesenhado = listaEl.lastElementChild;
  const idDesenhado = ultimoDesenhado ? Number(ultimoDesenhado.dataset.id) : null;
  let desde = 0;

  if (idDesenhado !== null) {
    const pos = itens.findIndex((i) => i.id === idDesenhado);
    if (pos < 0) {
      listaEl.textContent = ""; // veio outra lista (restauração) — redesenha
    } else {
      // O último item pode ter mudado embaixo (contador de repetição, detalhe
      // que chegou depois): troca só ele.
      ultimoDesenhado.replaceWith(itemEl(itens[pos]));
      desde = pos + 1;
    }
  }
  for (let k = desde; k < itens.length; k++) listaEl.appendChild(itemEl(itens[k]));

  // Teto de nós no DOM: o array já é cortado em MAX_ITENS, a lista acompanha.
  while (listaEl.childElementCount > itens.length) listaEl.firstElementChild.remove();

  if (grudado) listaEl.scrollTop = listaEl.scrollHeight;
}

/** Empilha um evento. Repetição idêntica vira contador, não item novo. */
function push(evento) {
  const item = {
    id: ++seq,
    kind: evento.kind || "passo",
    text: String(evento.text || ""),
    // Teto no detalhe: a resposta de uma olhada com visão pode vir com
    // milhares de caracteres, e esta lista inteira é persistida no
    // storage.session a cada passo.
    detail: String(evento.detail || "").slice(0, 1200),
    at: evento.at || Date.now(),
    vezes: 1,
  };
  if (!item.text && !item.detail) return;

  const ultimo = state.items[state.items.length - 1];
  if (ultimo && ultimo.kind === item.kind && ultimo.text === item.text && ultimo.detail === item.detail) {
    ultimo.vezes += 1;
    ultimo.at = item.at;
    return;
  }
  state.items.push(item);
  if (state.items.length > MAX_ITENS) state.items.splice(0, state.items.length - MAX_ITENS);
}

// ── render ───────────────────────────────────────────────────────────────────

function render() {
  // Sem tarefa e sem histórico não há o que mostrar: o painel some da página.
  if (!state.active && state.items.length === 0) {
    if (host && host.isConnected) host.remove();
    host = null;
    raizEl = painelEl = listaEl = estadoEl = contaEl = stopEl = fxEl = railEl = null;
    perguntaEl = ptxtEl = popcoesEl = caixaEl = msgEl = null;
    return;
  }
  const h = ensureHost();
  if (!h) return;

  raizEl.classList.toggle("recolhido", recolhido);
  raizEl.classList.toggle("parando", state.stopped);
  raizEl.classList.toggle("encerrado", !state.stopped && !state.active);
  estadoEl.textContent = state.stopped
    ? "parando a pedido seu…"
    : state.active
      ? "agindo nesta aba"
      : "encerrado";
  contaEl.textContent = String(state.items.length);
  // Falar com o Neo só faz sentido com ele trabalhando nesta aba.
  const ouvindo = state.active && !state.stopped;
  caixaEl.hidden = !ouvindo;
  renderPergunta(ouvindo);
  renderLista();
}

/** A pergunta pendente, com um botão por opção. Só o background a escreve. */
function renderPergunta(ouvindo) {
  if (!perguntaEl) return;
  if (!ouvindo || !pergunta || !pergunta.texto) {
    perguntaEl.hidden = true;
    popcoesEl.textContent = "";
    return;
  }
  perguntaEl.hidden = false;
  if (ptxtEl.textContent !== pergunta.texto) ptxtEl.textContent = pergunta.texto;
  const chave = JSON.stringify(pergunta.opcoes || []);
  if (popcoesEl.dataset.chave === chave) return;
  popcoesEl.dataset.chave = chave;
  popcoesEl.textContent = "";
  (pergunta.opcoes || []).forEach((opcao, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "opcao";
    b.textContent = opcao;
    b.addEventListener("click", (e) => {
      if (!e.isTrusted) return;
      enviarMensagem(opcao, i);
    });
    popcoesEl.append(b);
  });
}

// ── escrevendo ───────────────────────────────────────────────────────────────

function marcarDigitando() {
  try {
    document.documentElement.setAttribute(DIGITANDO_ATTR, String(Date.now()));
  } catch {
    /* noop */
  }
}

function soltarDigitando() {
  try {
    document.documentElement.removeAttribute(DIGITANDO_ATTR);
  } catch {
    /* noop */
  }
}

function tocou() {
  ultimaTecla = Date.now();
  marcarDigitando();
}

/** O rascunho vai para o background: a página que navega não leva o texto junto. */
function agendarRascunho() {
  clearTimeout(rascunhoTimer);
  rascunhoTimer = setTimeout(() => {
    try {
      chrome.runtime.sendMessage({ type: "NEO_RASCUNHO", texto: msgEl ? msgEl.value : "" });
    } catch {
      /* noop */
    }
  }, 300);
}

/** O agente vai agir: o campo solta o foco (o texto fica). */
function observarAgente() {
  if (observandoAgente || !document.documentElement) return;
  observandoAgente = true;
  new MutationObserver(() => {
    if (sombra && msgEl && sombra.activeElement === msgEl) msgEl.blur();
  }).observe(document.documentElement, { attributes: true, attributeFilter: [AGINDO_ATTR] });
}

/** O rascunho que o background guardou volta para o campo depois de navegar. */
function restaurarRascunho(rascunho) {
  if (!msgEl || !rascunho || !rascunho.texto || msgEl.value) return;
  msgEl.value = String(rascunho.texto).slice(0, MSG_MAX);
  // Estava escrevendo quando a página recarregou: devolve o cursor ao fim.
  if (state.active && !state.stopped && Date.now() - (Number(rascunho.at) || 0) < 15000) {
    ultimaTecla = Number(rascunho.at) || Date.now();
    msgEl.focus();
    const fim = msgEl.value.length;
    try {
      msgEl.setSelectionRange(fim, fim);
    } catch {
      /* noop */
    }
  }
}

/** O que o Daniel escreveu (ou o botão que clicou) vai para o Neo. */
function enviarMensagem(texto, opcao) {
  const t = String(texto || "").trim().slice(0, MSG_MAX);
  if (!t) return;
  push({ kind: "voce", text: `você: ${t}` });
  if (msgEl) msgEl.value = "";
  // Mandou: o agente pode voltar a agir já, e o rascunho guardado some.
  soltarDigitando();
  try {
    chrome.runtime.sendMessage({ type: "NEO_RASCUNHO", texto: "" });
  } catch {
    /* noop */
  }
  // Respondeu pelo botão: a caixa da pergunta fecha já (o Neo confirma depois).
  if (Number.isInteger(opcao)) pergunta = null;
  render();
  salvar();
  try {
    chrome.runtime.sendMessage({
      type: "NEO_MSG",
      texto: t,
      ...(Number.isInteger(opcao) ? { opcao } : {}),
    });
  } catch {
    /* service worker dormindo: o Chrome acorda ele com a mensagem */
  }
}

function setRecolhido(valor) {
  recolhido = !!valor;
  try {
    chrome.storage.local.set({ [CHAVE_RECOLHIDO]: recolhido });
  } catch {
    /* noop */
  }
  render();
}

// ── freio ────────────────────────────────────────────────────────────────────

function requestStop() {
  if (state.stopped) return;
  state.stopped = true;
  push({ kind: "parado", text: "PARE — você pediu para parar" });
  markStopAttribute();
  render();
  salvar();
  try {
    chrome.runtime.sendMessage({ type: "NEO_STOP" });
  } catch {
    /* service worker dormindo — o atributo já garante o freio no próximo passo */
  }
}

// O driver lê isto no snapshot. Fica no <html> porque o mundo principal (CDP)
// enxerga o DOM, mas não enxerga o mundo isolado do content script.
function markStopAttribute() {
  try {
    document.documentElement.setAttribute(STOP_ATTR, "1");
  } catch {
    /* noop */
  }
}

function salvar() {
  try {
    chrome.runtime.sendMessage({ type: "NEO_OVERLAY_SET", state });
  } catch {
    /* noop */
  }
}

/** Um evento vindo do neo-desktop (ou do background, na re-montagem). */
function apply(payload) {
  const tinha = state.active;
  state.active = payload.active !== false;
  if (payload.stopped === true) state.stopped = true;
  // Tarefa nova na mesma aba: a linha do tempo da anterior já foi lida.
  if (state.active && !tinha && payload.text) {
    state.items = [];
    state.stopped = false;
    pergunta = null;
  }
  if (payload.text || payload.detail) {
    push({ kind: payload.kind, text: payload.text, detail: payload.detail, at: payload.at });
  }
  if (state.stopped) markStopAttribute();
  render();
  salvar();
}

/** Estado inteiro vindo do background (re-montagem depois de navegar). */
function restore(next) {
  if (!next || typeof next !== "object") return;
  state = {
    active: next.active === true,
    stopped: next.stopped === true,
    items: Array.isArray(next.items) ? next.items.slice(-MAX_ITENS) : [],
  };
  // Vem do background (canal privilegiado): é a pergunta de verdade.
  pergunta = next.pergunta && next.pergunta.texto ? next.pergunta : null;
  const rascunho = next.rascunho;
  seq = state.items.reduce((m, i) => Math.max(m, Number(i.id) || 0), 0);
  if (state.stopped) markStopAttribute();
  render();
  restaurarRascunho(rascunho);
}

// ── canais ───────────────────────────────────────────────────────────────────

// Canal 1: mundo principal → aqui (é o que o neo-desktop usa por CDP).
window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const payload = event.data && event.data.__neo_overlay;
  if (!payload || typeof payload !== "object") return;
  apply(payload);
});

// Canal 1b: o pulso do clique, pedido pelo mesmo caminho dos eventos.
window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const p = event.data && event.data.__neo_ping;
  if (!p || typeof p !== "object") return;
  ping(Number(p.x) || 0, Number(p.y) || 0, p.tipo);
});

// Canal 2: background → aqui (re-monta o painel depois de uma navegação).
chrome.runtime.onMessage.addListener((message) => {
  if (message?.type !== "NEO_OVERLAY_STATE") return;
  restore(message.state || { active: false, items: [] });
});

// Canal 3: a PERGUNTA do Neo, pelo background (`neoPergunta`). É o único lugar
// de onde ela nasce — o canal 1 a página imita.
chrome.runtime.onMessage.addListener((message) => {
  if (message?.type !== "NEO_PERGUNTA") return;
  pergunta = message.pergunta && message.pergunta.texto ? message.pergunta : null;
  // Recolhido, ele não veria a pergunta: abre.
  if (pergunta && recolhido) {
    setRecolhido(false);
    return;
  }
  render();
});

// Ao carregar (inclusive depois de navegar), pergunta ao background se este
// tab ainda está sob controle do Neo — e com qual linha do tempo.
try {
  chrome.storage.local.get(CHAVE_RECOLHIDO, (v) => {
    if (chrome.runtime.lastError) return;
    recolhido = v?.[CHAVE_RECOLHIDO] === true;
    if (host) render();
  });
  chrome.runtime.sendMessage({ type: "NEO_OVERLAY_QUERY" }, (response) => {
    if (chrome.runtime.lastError) return;
    if (response?.state) restore(response.state);
  });
} catch {
  /* noop */
}
