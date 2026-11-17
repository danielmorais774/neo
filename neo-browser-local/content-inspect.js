// Inspetor visual: desenha na página o que o AGENTE enxerga.  [ADIÇÃO DO FORK]
//
// Serve para responder, em um clique, a pergunta que antes custava um job de 25
// passos: "o alvo que eu quero está no inventário?". No job 9968e68a as cinco
// alternativas de um questionário não estavam — e a única forma de descobrir
// isso foi ler o diário depois que a tarefa já tinha falhado.
//
// O mapeamento NÃO é reimplementado aqui: `__neoMapear` é gerado a partir do
// mesmo `js_snapshot` que o neo-core injeta por CDP (ver scripts/gerar-mapa.py).
// Um inspetor que enxerga diferente do driver não testa nada.
//
// TRÊS REGRAS DE DESENHO, e todas existem por um bug que já aconteceu:
//
//  1. NADA é escrito na página. A v1 das marcações do driver desenhava
//     retângulos no DOM e os apagava a cada passo: a tela piscava e SPAs
//     pesadas (o Drive é uma) reagiam à mutação. Aqui o único nó adicionado é
//     um host `position:fixed` com shadow root fechado, fora do fluxo — ele não
//     participa de layout nenhum.
//  2. Um ÚNICO <canvas>, nunca N divs. Canvas é uma camada composta própria:
//     redesenhar não recalcula estilo nem layout de nada da página. Duzentos
//     divs posicionados custariam duzentos layouts a cada rolagem.
//  3. `all:initial` PRIMEIRO. É um atalho que reseta TODA propriedade, então
//     declarado por último ele apaga o position/z-index/pointer-events que vêm
//     antes — foi assim que a faixa do Neo virou um bloco estático no fim do
//     documento, ocupou altura de verdade e roubou clique (v0.1.42).

// ESCOPO PRÓPRIO, obrigatório. Content scripts da mesma entrada do manifest
// compartilham UM escopo de topo: o `const HOST_ID` daqui colidiu com o de
// `content-overlay.js` e o Chrome matou este arquivo inteiro com
// "Identifier 'HOST_ID' has already been declared" — o listener nunca era
// registrado, o popup recebia `undefined` do OUTRO content script e dizia "esta
// aba não aceita o inspetor" em todo site. A IIFE também é o que torna a
// injeção sob demanda repetível: injetar duas vezes na mesma aba não redeclara
// nada.
(() => {
  if (globalThis.__neoInspetorInstalado) return;
  globalThis.__neoInspetorInstalado = true;

const HOST_ID = "neo-map-overlay";

// Cor por PASSADA do inventário. É o que transforma o inspetor em diagnóstico:
// azul é o que qualquer allowlist já pegava; verde e âmbar são o que só existe
// desde 2026-09-05, e ver um alvo verde/âmbar é ver a correção funcionando.
const CORES = {
  declarado: "#38bdf8",   // o site declarou (tag, papel, handler)
  procurador: "#22c55e",  // rosto de um controle escondido por CSS
  transparente: "#22c55e", // controle invisível que recebe o clique de verdade
  grade: "#f59e0b",       // achado pela varredura por pixel
  "contêiner": "#a78bfa", // linha de lista promovida
  painel: "#a78bfa",      // região que rola
  coberto: "#ef4444",     // tem alguma coisa na frente: o clique não chega
};

let host = null;
let canvas = null;
let ctx = null;
let itens = [];
let pendente = false;
let ativo = false;

function criarHost() {
  if (host && host.isConnected) return host;
  if (!document.documentElement) return null;

  host = document.createElement("div");
  host.id = HOST_ID;
  host.style.cssText = [
    "all:initial",
    "position:fixed", "top:0", "left:0", "width:100%", "height:100%",
    "z-index:2147483646", "pointer-events:none",
  ].join(";");

  const shadow = host.attachShadow({ mode: "closed" });
  canvas = document.createElement("canvas");
  // `will-change:transform` força a camada própria: o repaint do canvas nunca
  // sobe para a árvore de renderização da página.
  canvas.style.cssText =
    "position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;will-change:transform";
  shadow.append(canvas);
  document.documentElement.appendChild(host);
  ctx = canvas.getContext("2d");
  return host;
}

function dimensionar() {
  if (!canvas) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = Math.floor(innerWidth * dpr);
  const h = Math.floor(innerHeight * dpr);
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

/** Cor de um item: cobertura ganha de origem — é a informação mais grave.
 *
 * `controla` vence a origem porque é o achado mais importante de todos: o
 * elemento é o ROSTO de um controle que o CSS escondeu. Ele costuma vir da
 * primeira passada (o `<label>` está no seletor), mas marcá-lo de "declarado"
 * esconderia justamente o que o inspetor existe para mostrar. */
function corDe(item) {
  if (item.coberto) return CORES.coberto;
  if (item.controla) return CORES.procurador;
  return CORES[item.via] || CORES.declarado;
}

function desenhar() {
  pendente = false;
  if (!ativo || !ctx) return;
  dimensionar();
  ctx.clearRect(0, 0, innerWidth, innerHeight);
  ctx.font = "600 11px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";
  ctx.textBaseline = "top";

  // As caixas são RELIDAS a cada quadro a partir dos elementos vivos: rolar não
  // remapeia (o que custaria dezenas de ms), só reposiciona.
  const vivos = (globalThis.__neo && globalThis.__neo.refs) || [];
  let visiveis = 0;

  for (const item of itens) {
    const el = vivos[item.ref];
    if (!el || !el.isConnected) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    visiveis++;

    const cor = corDe(item);
    ctx.strokeStyle = cor;
    ctx.lineWidth = item.coberto ? 2 : 1.5;
    ctx.setLineDash(item.coberto ? [5, 3] : []);
    ctx.strokeRect(r.left + 0.5, r.top + 0.5, r.width - 1, r.height - 1);
    ctx.setLineDash([]);

    // Etiqueta ACIMA da caixa; se não couber, por dentro dela. Mesma regra do
    // `draw_marks` no Rust, para o inspetor parecer com o print que o modelo vê.
    const marca = item.checked === true ? "●" : item.checked === false ? "○" : "";
    const texto = `${item.ref}${marca}`;
    const largura = ctx.measureText(texto).width + 8;
    const topo = r.top >= 15 ? r.top - 15 : r.top;
    ctx.fillStyle = cor;
    ctx.fillRect(r.left, topo, largura, 15);
    ctx.fillStyle = "#0b1220";
    ctx.fillText(texto, r.left + 4, topo + 2);
  }

  legenda(visiveis);
}

function legenda(visiveis) {
  const contagem = new Map();
  for (const item of itens) {
    const chave = item.coberto
      ? "coberto"
      : item.controla
        ? "procurador"
        : item.via || "declarado";
    contagem.set(chave, (contagem.get(chave) || 0) + 1);
  }
  const linhas = [`${itens.length} elementos · ${visiveis} na tela`];
  for (const [chave, n] of contagem) linhas.push(`${chave}: ${n}`);

  // Canto INFERIOR ESQUERDO: qualquer lugar cobre alguma coisa, mas o topo tem
  // a faixa do Neo e a direita costuma ter a coluna de conteúdo. Embaixo à
  // esquerda é o canto mais vazio da web e o mesmo que os dev overlays usam.
  const larg = 168;
  const alt = 12 + linhas.length * 15;
  const x = 12;
  const y = innerHeight - alt - 12;
  ctx.fillStyle = "rgba(11,18,32,.92)";
  ctx.fillRect(x, y, larg, alt);
  ctx.strokeStyle = "#1e3a5f";
  ctx.lineWidth = 1;
  ctx.strokeRect(x + 0.5, y + 0.5, larg - 1, alt - 1);

  linhas.forEach((linha, i) => {
    const chave = i === 0 ? null : linha.split(":")[0];
    ctx.fillStyle = i === 0 ? "#e6f1ff" : CORES[chave] || "#e6f1ff";
    ctx.fillText(linha, x + 8, y + 6 + i * 15);
  });
}

function agendar() {
  if (pendente || !ativo) return;
  pendente = true;
  requestAnimationFrame(desenhar);
}

/** Roda o inventário. É a única parte cara — por isso não acontece na rolagem. */
function mapear() {
  if (typeof globalThis.__neoMapear !== "function") {
    // O mapeador é gerado a partir do Rust; se ele não carregou, o inspetor não
    // tem o que desenhar — e adivinhar seria pior que avisar.
    throw new Error(
      "neo-map.generated.js não carregou — rode `python3 scripts/gerar-mapa.py` " +
        "e recarregue a extensão"
    );
  }
  const começo = performance.now();
  // `chars` mínimo: o inspetor não usa o texto da página, e pedi-lo grande só
  // faria o `innerText` do body ser calculado à toa.
  const snapshot = globalThis.__neoMapear(200, 1);
  itens = (snapshot.elements || []).map((e) => ({
    ref: e.ref,
    via: e.via || "declarado",
    coberto: e.coberto || null,
    controla: e.controla || null,
    checked: e.checked,
    tag: e.tag,
    text: e.text,
  }));
  return {
    total: itens.length,
    ms: Math.round(performance.now() - começo),
    cobertos: itens.filter((i) => i.coberto).length,
    porOrigem: itens.reduce((acc, i) => {
      const chave = i.controla ? "procurador" : i.via;
      acc[chave] = (acc[chave] || 0) + 1;
      return acc;
    }, {}),
    // O que a allowlist de tag NÃO alcançava: rosto de controle escondido e
    // achado por varredura de pixel. É a linha que responde "a correção pegou
    // neste site?" sem precisar abrir diário nenhum.
    amostraNova: itens
      .filter((i) => i.controla || i.via === "grade")
      .slice(0, 6)
      .map(
        (i) =>
          `[${i.ref}] ${i.tag} "${String(i.text || "").slice(0, 38)}"` +
          (i.controla ? ` → ${i.controla}` : " (pixel)")
      ),
  };
}

function ligar() {
  criarHost();
  ativo = true;
  const resumo = mapear();
  addEventListener("scroll", agendar, { passive: true, capture: true });
  addEventListener("resize", agendar, { passive: true });
  agendar();
  return resumo;
}

function desligar() {
  ativo = false;
  removeEventListener("scroll", agendar, { capture: true });
  removeEventListener("resize", agendar);
  if (host && host.isConnected) host.remove();
  host = null;
  canvas = null;
  ctx = null;
  itens = [];
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "NEO_MAP") return;
  try {
    if (message.modo === "off" || (message.modo === "toggle" && ativo)) {
      desligar();
      sendResponse({ ok: true, ativo: false });
      return;
    }
    sendResponse({ ok: true, ativo: true, ...ligar() });
  } catch (erro) {
    // Página que recusa canvas ou CSP exótica não pode virar popup travado.
    desligar();
    sendResponse({ ok: false, erro: String((erro && erro.message) || erro) });
  }
});
})();
