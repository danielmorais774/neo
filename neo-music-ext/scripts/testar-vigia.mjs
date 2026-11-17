// Testa a VIGIA DE FILA sem navegador: `node scripts/testar-vigia.mjs`
//
// O ponto é exercitar o código REAL dos dois lados da injeção:
//   • o service worker (`background.js` carregado num VM, com `chrome` e
//     `WebSocket` dublados);
//   • o vigia que ele injeta na página — a mesma função, rodada num segundo VM
//     que finge ser a aba do YouTube (um <video> com relógio, `#movie_player`,
//     `location` mutável e `chrome.runtime.sendMessage` ligado de volta no
//     worker).
//
// Com isso dá para medir, sem esperar música tocando, exatamente o que os
// critérios pedem: a QUE DISTÂNCIA do fim a pausa acontece, quantos eventos
// saem, se o `codigo` volta inteiro e se sobra listener depois do `unwatch`.
//
// O que ele NÃO cobre (só o navegador cobre): a permissão de injeção de
// verdade, o ciclo de vida do service worker do MV3 e o DOM real do YouTube.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const aqui = dirname(fileURLToPath(import.meta.url));
const fonte = readFileSync(join(aqui, "..", "background.js"), "utf8");

let falhas = 0;
let passes = 0;
function ok(nome, cond, detalhe = "") {
  if (cond) {
    passes += 1;
    console.log(`  ok   ${nome}`);
  } else {
    falhas += 1;
    console.log(`  FALHA ${nome}${detalhe ? " — " + detalhe : ""}`);
  }
}
function eq(nome, got, esperado) {
  ok(nome, got === esperado, `esperado ${JSON.stringify(esperado)}, veio ${JSON.stringify(got)}`);
}
const microtarefas = () => new Promise((r) => setImmediate(r));

// ── um relógio só para os dois mundos ──────────────────────────────────────
// O worker e a página têm timers próprios, e o vídeo anda junto. Um relógio
// virtual único deixa medir "faltavam X segundos quando pausou" com precisão de
// milissegundo, que é o que o critério pede.

function criarRelogio() {
  // `dono` diz de qual mundo é cada timer ("worker" ou "pagina"): é o que deixa
  // matar o worker no teste sem chutar qual timer era dele.
  const r = { agora: 1e12, timers: [], seq: 0, dono: "?" };
  const marcar = (fn, ms, repete) => {
    const id = ++r.seq;
    r.timers.push({ id, fn, dono: r.dono, ms: Math.max(1, ms || 1), proximo: r.agora + Math.max(1, ms || 1), repete });
    return id;
  };
  r.paraDono = (dono, fn) => {
    const antes = r.dono;
    r.dono = dono;
    try {
      return fn();
    } finally {
      r.dono = antes;
    }
  };
  r.setInterval = (fn, ms) => marcar(fn, ms, true);
  r.setTimeout = (fn, ms) => marcar(fn, ms, false);
  r.clear = (id) => {
    r.timers = r.timers.filter((t) => t.id !== id);
  };
  r.disparar = () => {
    for (const t of r.timers.slice()) {
      if (t.proximo <= r.agora) {
        if (t.repete) t.proximo = r.agora + t.ms;
        else r.clear(t.id);
        try {
          r.paraDono(t.dono, t.fn);
        } catch (e) {
          console.log("  (timer explodiu:", e.message, ")");
        }
      }
    }
  };
  return r;
}

// ── a aba ──────────────────────────────────────────────────────────────────

function criarPagina(relogio, { videoId, duracao = 200 }) {
  const ouvintes = { timeupdate: [], ended: [] };
  let posicao = 0;
  const video = {
    duration: duracao,
    get currentTime() {
      return posicao;
    },
    set currentTime(x) {
      // Escrita vinda do vigia (recuo) ou do relógio do teste.
      if (pag && pag.gravandoRecuo) pag.recuos.push(+x.toFixed(3));
      posicao = x;
    },
    paused: true,
    ended: false,
    muted: false,
    volume: 1,
    playbackRate: 1,
    addEventListener: (tipo, fn) => {
      (ouvintes[tipo] = ouvintes[tipo] || []).push(fn);
    },
    removeEventListener: (tipo, fn) => {
      ouvintes[tipo] = (ouvintes[tipo] || []).filter((x) => x !== fn);
    },
    pause: () => {
      video.paused = true;
      pag.pausadoEm = duracao - video.currentTime;
      pag.pausas += 1;
    },
    get currentTimeEscrito() {
      return pag.recuos;
    },
    play: () => {
      video.paused = false;
    },
  };
  // A prévia muda do feed: o primeiro <video> do DOM numa página do YouTube pode
  // ser ela. Vigiar a prévia é vigiar um vídeo que nunca toca.
  const previa = { duration: 0, currentTime: 0, paused: true, addEventListener: () => {}, removeEventListener: () => {}, pause: () => { pag.pausouPrevia = true; } };

  const pag = {
    gravandoRecuo: true,
    // O switch de autoplay do YouTube: existe em vídeo comum, some em mix/rádio.
    temBotaoAutonav: true,
    autoplayLigado: true,
    cliquesAutonav: 0,
    video,
    ouvintes,
    pausas: 0,
    recuos: [],
    pausadoEm: null,
    pausouPrevia: false,
    anuncio: false,
    href: "https://www.youtube.com/watch?v=" + videoId,
    contextoVivo: true,
    mensagens: [],
  };

  const ctx = vm.createContext({
    window: {},
    document: {
      querySelectorAll: () => [previa, video],
      querySelector: (sel) => {
        if (sel === "#movie_player") {
          return { classList: { contains: (c) => pag.anuncio && c === "ad-showing" } };
        }
        if (sel === ".ytp-autonav-toggle-button") {
          if (!pag.temBotaoAutonav) return null;
          return {
            getAttribute: () => (pag.autoplayLigado ? "true" : "false"),
            click: () => {
              pag.autoplayLigado = !pag.autoplayLigado;
              pag.cliquesAutonav += 1;
            },
          };
        }
        return null;
      },
    },
    location: {
      get href() {
        return pag.href;
      },
      get hostname() {
        return new URL(pag.href).hostname;
      },
      get search() {
        return new URL(pag.href).search;
      },
      get pathname() {
        return new URL(pag.href).pathname;
      },
    },
    navigator: { mediaSession: null },
    chrome: {
      get runtime() {
        // Extensão recarregada = contexto invalidado: `chrome.runtime.id` some.
        return pag.contextoVivo
          ? {
              id: "abcdef",
              lastError: undefined,
              sendMessage: (msg, cb) => {
                pag.mensagens.push(msg);
                const resp = pag.aoReceber ? pag.aoReceber(msg) : undefined;
                if (cb) cb(resp);
              },
            }
          : { id: undefined, lastError: undefined, sendMessage: () => { throw new Error("Extension context invalidated"); } };
      },
    },
    Date: { now: () => relogio.agora },
    setInterval: (fn, ms) => relogio.paraDono("pagina", () => relogio.setInterval(fn, ms)),
    clearInterval: relogio.clear,
    setTimeout: (fn, ms) => relogio.paraDono("pagina", () => relogio.setTimeout(fn, ms)),
    clearTimeout: relogio.clear,
    URL,
    URLSearchParams,
    console: { log: () => {}, warn: () => {} },
  });
  pag.ctx = ctx;
  pag.rodar = (fn, args) =>
    vm.runInContext(`(${fn.toString()}).apply(null, ${JSON.stringify(args || [])})`, ctx);
  pag.temVigia = () => vm.runInContext("!!window.__neoVigia", ctx);
  return pag;
}

// ── o service worker ───────────────────────────────────────────────────────

function criarWorker(relogio, { aba, alvo, pagina }) {
  if (!alvo) alvo = { pagina };
  const enviados = [];
  let listenerMsg = null;
  const listenersUpdated = [];
  let wsAtual = null;

  const chrome = {
    tabs: {
      query: async () => [aba],
      get: async (id) => {
        if (id !== aba.id) throw new Error("No tab with id");
        return aba;
      },
      update: async (id, props) => {
        if (props && props.url) aba.url = props.url;
        return aba;
      },
      create: async () => aba,
      onUpdated: { addListener: (f) => listenersUpdated.push(f) },
      onActivated: { addListener: () => {} },
      onRemoved: { addListener: () => {} },
      onCreated: { addListener: () => {} },
    },
    alarms: { create: () => {}, onAlarm: { addListener: () => {} } },
    runtime: {
      onMessage: { addListener: (f) => (listenerMsg = f) },
      onStartup: { addListener: () => {} },
      onInstalled: { addListener: () => {} },
    },
    action: { setBadgeText: () => {}, setBadgeBackgroundColor: () => {}, setTitle: () => {} },
    scripting: {
      // A injeção de verdade: roda a MESMA função no contexto da página.
      executeScript: async ({ target, func, args }) => {
        if (!alvo.pagina.injetavel) throw new Error("Cannot access contents of the page");
        if (target.tabId !== aba.id) throw new Error("No tab with id");
        return [{ result: alvo.pagina.rodar(func, args) }];
      },
    },
  };

  class FakeWS {
    constructor() {
      this.readyState = 0;
      wsAtual = this;
      // Conecta no próximo tique do relógio, como o de verdade.
      relogio.setTimeout(() => {
        this.readyState = 1;
        if (this.onopen) this.onopen();
      }, 10);
    }
    send(txt) {
      enviados.push(JSON.parse(txt));
    }
    close() {
      this.readyState = 3;
      if (this.onclose) this.onclose();
    }
  }
  FakeWS.OPEN = 1;
  FakeWS.CONNECTING = 0;

  const ctx = vm.createContext({
    chrome,
    WebSocket: FakeWS,
    console: { log: () => {}, warn: () => {} },
    setTimeout: (fn, ms) => relogio.paraDono("worker", () => relogio.setTimeout(fn, ms)),
    clearTimeout: relogio.clear,
    setInterval: (fn, ms) => relogio.paraDono("worker", () => relogio.setInterval(fn, ms)),
    clearInterval: relogio.clear,
    Date: { now: () => relogio.agora },
    URL,
    URLSearchParams,
    Promise,
  });
  vm.runInContext(fonte, ctx);

  // A página fala com o worker pelo mesmo caminho do navegador.
  const receber = (msg) => {
    const pagina = alvo.pagina;
    const v = pagina.video;
    pagina.faltaNoEvento = pagina.faltaNoEvento || {};
    if (msg && msg.evento && msg.evento !== "batida") {
      pagina.faltaNoEvento[msg.evento] = v.duration - v.currentTime;
    }
    let resposta;
    if (listenerMsg) listenerMsg(msg, { tab: { id: aba.id } }, (r) => (resposta = r));
    return resposta;
  };
  alvo.pagina.aoReceber = receber;
  alvo.receber = receber;

  return {
    ctx,
    enviados,
    ws: () => wsAtual,
    navegou: (url) => {
      aba.url = url;
      for (const f of listenersUpdated) f(aba.id, { url }, aba);
    },
    // O que o desktop manda pela ponte.
    receber: (msg) => wsAtual.onmessage({ data: JSON.stringify(msg) }),
  };
}

// Avança o relógio dos dois mundos, tocando o vídeo enquanto isso.
async function avancar(relogio, pagina, ms, passo = 25) {
  for (let i = 0; i < ms; i += passo) {
    relogio.agora += passo;
    const v = pagina.video;
    if (!v.paused && !v.ended) {
      pagina.gravandoRecuo = false;
      v.currentTime = Math.min(v.duration, v.currentTime + (passo / 1000) * v.playbackRate);
      pagina.gravandoRecuo = true;
      if (v.currentTime >= v.duration) {
        v.ended = true;
        v.paused = true;
        for (const f of pagina.ouvintes.ended.slice()) f();
      }
      // `timeupdate` do player: ~4x/s, e é ele o relógio bom (não é
      // estrangulado em aba de segundo plano).
      pagina.desdeTimeupdate = (pagina.desdeTimeupdate || 0) + passo;
      if (pagina.desdeTimeupdate >= 250) {
        pagina.desdeTimeupdate = 0;
        for (const f of pagina.ouvintes.timeupdate.slice()) f();
      }
    }
    relogio.disparar();
    await microtarefas();
  }
}

// Monta o cenário completo: worker conectado + `watch` chegando ANTES do `play`
// (é a ordem real: o ext_bridge re-arma a vigia antes de mandar tocar).
async function cenario({ duracao = 200, videoAnterior = "VELHO1", videoNovo = "NOVO123", codigo = "cod-1", injetavel = true } = {}) {
  const relogio = criarRelogio();
  const aba = { id: 7, url: "https://www.youtube.com/watch?v=" + videoAnterior, title: "…", audible: true, active: true, status: "complete", windowId: 1, mutedInfo: { muted: false } };
  const pagina = criarPagina(relogio, { videoId: videoAnterior, duracao });
  pagina.injetavel = injetavel;
  const alvo = { pagina };
  const worker = criarWorker(relogio, { aba, alvo });
  await avancar(relogio, pagina, 50);
  worker.receber({ type: "watch", tabId: aba.id, videoId: videoNovo, codigo });
  await avancar(relogio, pagina, 100);
  return {
    relogio,
    aba,
    pagina,
    worker,
    videoNovo,
    codigo,
    // Navegar de verdade troca o DOCUMENTO: o vigia injetado morre junto, e o
    // worker tem que reinstalar sozinho no documento novo.
    trocarPagina: (nova) => {
      alvo.pagina = nova;
    },
  };
}

// Simula o `play`: a aba navega (documento novo) e o vídeo começa.
async function tocar(c, { atraso = 300 } = {}) {
  await avancar(c.relogio, c.pagina, atraso);
  c.pagina.href = "https://www.youtube.com/watch?v=" + c.videoNovo;
  c.worker.navegou(c.pagina.href);
  c.pagina.video.paused = false;
  await avancar(c.relogio, c.pagina, 500);
}

const eventos = (worker) => worker.enviados.filter((m) => m.type === "media_event");

// ── 1. o caminho feliz ─────────────────────────────────────────────────────

console.log("\n# fila tocando: um evento de cada, e a pausa antes do fim");
{
  const c = await cenario({ duracao: 60 });
  await tocar(c);
  ok("vigia instalada na página", c.pagina.temVigia());
  await avancar(c.relogio, c.pagina, 60000, 50);

  const evs = eventos(c.worker);
  const nomes = evs.map((e) => e.event);
  eq("um faixa_iniciou", nomes.filter((n) => n === "faixa_iniciou").length, 1);
  eq("um faixa_terminando", nomes.filter((n) => n === "faixa_terminando").length, 1);
  eq("nada de faixa_terminou (pausamos antes)", nomes.filter((n) => n === "faixa_terminou").length, 0);
  eq("ordem", nomes.join(">"), "faixa_iniciou>faixa_terminando");
  ok("todo evento leva o código do watch", evs.every((e) => e.codigo === c.codigo), JSON.stringify(evs));
  ok("todo evento leva a faixa certa", evs.every((e) => e.videoId === c.videoNovo), JSON.stringify(evs));
  ok("todo evento leva a aba", evs.every((e) => e.tabId === 7));

  ok("pausou", c.pagina.pausas > 0);
  eq("pausou UMA vez", c.pagina.pausas, 1);
  ok(
    "pausou entre 0,5s e 1,5s do fim",
    c.pagina.pausadoEm >= 0.5 && c.pagina.pausadoEm <= 1.5,
    "faltavam " + (c.pagina.pausadoEm || 0).toFixed(3) + "s"
  );
  console.log("       (faltavam " + c.pagina.pausadoEm.toFixed(3) + "s para o fim)");
  eq("estacionou o playhead UMA vez", c.pagina.recuos.length, 1);
  ok(
    "e estacionou longe do fim (o player do YouTube não vê mais fim nenhum)",
    c.pagina.recuos[0] !== undefined && c.pagina.video.duration - c.pagina.recuos[0] >= 3.5,
    "recuou para " + JSON.stringify(c.pagina.recuos)
  );
  eq("o vídeo NÃO terminou", c.pagina.video.ended, false);
  eq("não encostou na prévia muda", c.pagina.pausouPrevia, false);
  const faltaTerminando = c.pagina.faltaNoEvento.faixa_terminando;
  ok(
    "faixa_terminando saiu faltando ~6s",
    faltaTerminando <= 6 && faltaTerminando >= 5,
    "faltavam " + (faltaTerminando || 0).toFixed(2) + "s"
  );
  console.log("       (faixa_terminando com " + faltaTerminando.toFixed(2) + "s de folga)");
}

// ── 2. o watch chega antes da navegação ────────────────────────────────────

console.log("\n# o `watch` chega ANTES do `play` (ordem real do desktop)");
{
  const c = await cenario({ duracao: 30 });
  eq("não arma no documento velho", c.pagina.temVigia(), false);
  eq("nem manda evento", eventos(c.worker).length, 0);
  await tocar(c);
  ok("arma quando a faixa certa aparece", c.pagina.temVigia());
}

// ── 3. anúncio ─────────────────────────────────────────────────────────────

console.log("\n# comercial antes da música");
{
  const c = await cenario({ duracao: 15 });
  c.pagina.anuncio = true;
  await tocar(c);
  await avancar(c.relogio, c.pagina, 14000, 50);
  eq("não pausa o comercial", c.pagina.pausas, 0);
  eq("não inventa evento no comercial", eventos(c.worker).length, 0);

  // O anúncio acaba e entra a música (o mesmo <video>, outra duração).
  c.pagina.anuncio = false;
  c.pagina.video.duration = 40;
  c.pagina.video.currentTime = 0;
  c.pagina.video.ended = false;
  c.pagina.video.paused = false;
  await avancar(c.relogio, c.pagina, 41000, 50);
  eq("aí sim, um iniciou", eventos(c.worker).filter((e) => e.event === "faixa_iniciou").length, 1);
  eq("e um terminando", eventos(c.worker).filter((e) => e.event === "faixa_terminando").length, 1);
  eq("e pausou a música", c.pagina.pausas, 1);
}

// ── 4. o usuário troca de vídeo (navegação por SPA) ────────────────────────

console.log("\n# o vídeo da aba muda sem ser por nós");
{
  const c = await cenario({ duracao: 120 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 3000);
  c.pagina.href = "https://www.youtube.com/watch?v=OUTRO99";
  await avancar(c.relogio, c.pagina, 1500);
  const fim = eventos(c.worker).filter((e) => e.event === "faixa_terminou");
  eq("um faixa_terminou", fim.length, 1);
  eq("com a faixa que ESTÁ tocando", fim[0] && fim[0].videoId, "OUTRO99");
  eq("com o código do despacho", fim[0] && fim[0].codigo, c.codigo);
  eq("e o vigia se solta", c.pagina.temVigia(), false);
}

console.log("\n# a aba vai para uma página sem vídeo");
{
  const c = await cenario({ duracao: 120 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 2000);
  c.pagina.href = "https://www.youtube.com/feed/subscriptions";
  await avancar(c.relogio, c.pagina, 1500);
  eq("não reporta fim nenhum (seria lido como 'manda a próxima')", eventos(c.worker).filter((e) => e.event === "faixa_terminou").length, 0);
  eq("mas se solta", c.pagina.temVigia(), false);
}

// ── 5. unwatch ─────────────────────────────────────────────────────────────

console.log("\n# depois do `unwatch`, a faixa ATUAL ainda é protegida");
{
  // Esta era a asserção errada da primeira versão: eu exigia que o vídeo
  // terminasse sozinho depois do unwatch — e era exatamente isso que devolvia o
  // fim da última faixa da fila para o autoplay do YouTube (medido em produção
  // 2026-09-09). O unwatch tira a FILA, não a trava da faixa que está tocando.
  const c = await cenario({ duracao: 40 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 3000);
  c.worker.receber({ type: "unwatch" });
  await avancar(c.relogio, c.pagina, 300);
  const antes = eventos(c.worker).length;
  await avancar(c.relogio, c.pagina, 40000, 50);
  eq("a faixa do Neo NÃO termina sozinha", c.pagina.video.ended, false);
  eq("ela pausou antes do fim", c.pagina.pausas, 1);
  ok(
    "na mesma janela de sempre",
    c.pagina.pausadoEm >= 0.5 && c.pagina.pausadoEm <= 1.5,
    "faltavam " + (c.pagina.pausadoEm || 0).toFixed(3) + "s"
  );
  eq("e o playhead foi estacionado", c.pagina.recuos.length, 1);
  eq("sem nenhum evento novo (a fila acabou)", eventos(c.worker).length, antes);
  eq("cumprido o papel, o vigia some", c.pagina.temVigia(), false);
}

console.log("\n# a sequência REAL do fim de fila (produção 2026-09-09)");
{
  // "Erase My Scars", última da fila: watch armado → faixa_terminando a 6s do
  // fim → o neo-core responde `desarmar` → unwatch chega com a música ainda
  // tocando. O vídeo terminava e o YouTube emendava outro.
  const c = await cenario({ duracao: 30 });
  await tocar(c);
  // pula para os últimos 8s, como o Daniel fez no teste (o pulo não é "recuo"
  // do vigia — é o usuário mexendo na barra)
  c.pagina.gravandoRecuo = false;
  c.pagina.video.currentTime = 22;
  c.pagina.gravandoRecuo = true;
  await avancar(c.relogio, c.pagina, 2500, 50);
  const term = eventos(c.worker).filter((e) => e.event === "faixa_terminando");
  eq("saiu o faixa_terminando", term.length, 1);
  // o desktop responde ao `desarmar` do neo-core
  c.worker.receber({ type: "unwatch" });
  await avancar(c.relogio, c.pagina, 12000, 50);
  eq("o autoplay NÃO ganha o fim da última faixa", c.pagina.video.ended, false);
  eq("porque a trava continuou de pé", c.pagina.pausas, 1);
}

console.log("\n# unwatch não deixa a vigia grudada em outro vídeo");
{
  const c = await cenario({ duracao: 60 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 2000);
  c.worker.receber({ type: "unwatch" });
  await avancar(c.relogio, c.pagina, 300);
  // o usuário navega para outro vídeo: a vigia solta não tem nada a ver com ele
  c.pagina.href = "https://www.youtube.com/watch?v=OUTRO55";
  await avancar(c.relogio, c.pagina, 2000);
  eq("a vigia sumiu", c.pagina.temVigia(), false);
  eq("e não reportou nada (a fila já tinha acabado)", eventos(c.worker).filter((e) => e.event === "faixa_terminou").length, 0);
}

// ── 6. sem vigia armada, nada acontece ─────────────────────────────────────

console.log("\n# vídeo do usuário, sem fila");
{
  const relogio = criarRelogio();
  const aba = { id: 7, url: "https://www.youtube.com/watch?v=QUALQUER", title: "…", audible: true, active: true, status: "complete", windowId: 1, mutedInfo: { muted: false } };
  const pagina = criarPagina(relogio, { videoId: "QUALQUER", duracao: 20 });
  pagina.injetavel = true;
  const worker = criarWorker(relogio, { aba, pagina });
  pagina.video.paused = false;
  await avancar(relogio, pagina, 21000, 50);
  eq("terminou normalmente", pagina.video.ended, true);
  eq("ninguém pausou", pagina.pausas, 0);
  eq("ninguém mexeu no playhead", pagina.recuos.length, 0);
  eq("nenhum media_event", eventos(worker).length, 0);
  eq("nenhum vigia na página", pagina.temVigia(), false);
}

// ── 7. o worker morre no meio da faixa ─────────────────────────────────────

console.log("\n# o service worker é descartado no meio da faixa");
{
  const c = await cenario({ duracao: 30 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 2000);
  // Descartar o worker = perder os timers e o socket dele. O vigia da PÁGINA
  // continua: é esse o ponto do desenho.
  const timersDoWorker = c.relogio.timers.filter((t) => t.dono === "worker").length;
  c.relogio.timers = c.relogio.timers.filter((t) => t.dono !== "worker");
  c.pagina.aoReceber = () => undefined; // worker dormindo: ninguém responde
  await avancar(c.relogio, c.pagina, 30000, 50);
  ok("mesmo sem worker, a página pausou antes do fim", c.pagina.pausas === 1, "pausas=" + c.pagina.pausas);
  ok(
    "e pausou na janela certa",
    c.pagina.pausadoEm >= 0.5 && c.pagina.pausadoEm <= 1.5,
    "faltavam " + (c.pagina.pausadoEm || 0).toFixed(3) + "s"
  );
  eq("o vídeo não terminou (autoplay não tem o que emendar)", c.pagina.video.ended, false);
  ok("o worker realmente tinha timers (e foram embora)", timersDoWorker > 0, "timers=" + timersDoWorker);
}

// ── 8. a extensão é recarregada ────────────────────────────────────────────

console.log("\n# a extensão é recarregada com o vigia armado");
{
  const c = await cenario({ duracao: 40 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 2000);
  ok("vigia armada", c.pagina.temVigia());
  c.pagina.contextoVivo = false; // chrome.runtime.id some
  await avancar(c.relogio, c.pagina, 2000);
  eq("o vigia órfão se destrói", c.pagina.temVigia(), false);
  const pausasAntes = c.pagina.pausas;
  await avancar(c.relogio, c.pagina, 40000, 50);
  eq("e o vídeo do usuário volta a terminar sozinho", c.pagina.video.ended, true);
  eq("sem pausa fantasma", c.pagina.pausas, pausasAntes);
}

// ── 9. eventos com o socket fechado ────────────────────────────────────────

console.log("\n# o socket cai bem na hora do aviso");
{
  const c = await cenario({ duracao: 20 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 2000);
  c.worker.ws().readyState = 3; // socket morto, sem onclose (worker reciclado)
  await avancar(c.relogio, c.pagina, 20000, 50);
  ok("pausou mesmo assim", c.pagina.pausas === 1, "pausas=" + c.pagina.pausas);
  // Ao reconectar, os avisos guardados saem.
  const novo = c.worker.ws();
  await avancar(c.relogio, c.pagina, 2000);
  const evs = eventos(c.worker);
  ok("os avisos guardados foram entregues na reconexão", evs.some((e) => e.event === "faixa_terminando"), JSON.stringify(evs));
  ok("com o código intacto", evs.every((e) => e.codigo === c.codigo));
  ok("socket novo existe", !!novo);
}

// ── 10. injeção recusada: a via de reserva continua ────────────────────────

console.log("\n# a injeção é recusada (aba bloqueada)");
{
  const c = await cenario({ duracao: 30, injetavel: false });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 3000);
  eq("nenhum media_event inventado", eventos(c.worker).length, 0);
  ok(
    "mas o snapshot continua saindo (é a reserva do desktop)",
    c.worker.enviados.filter((m) => m.type === "tabs").length > 1,
    "tabs=" + c.worker.enviados.filter((m) => m.type === "tabs").length
  );
}

// ── 11. duas faixas seguidas: o laço da fila ───────────────────────────────

console.log("\n# a fila anda: faixa 1 → faixa 2 na mesma aba");
{
  const c = await cenario({ duracao: 30, videoNovo: "FAIXA_1", codigo: "cod-1" });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 30000, 50);
  eq("faixa 1 pediu a próxima", eventos(c.worker).filter((e) => e.event === "faixa_terminando" && e.codigo === "cod-1").length, 1);
  eq("faixa 1 pausou", c.pagina.pausas, 1);

  // É o que o desktop faz ao receber o terminando: re-arma a vigia com o código
  // NOVO e só então manda tocar (`report_media_event` → `arm_watch` → `play`).
  c.worker.receber({ type: "watch", tabId: 7, videoId: "FAIXA_2", codigo: "cod-2" });
  await avancar(c.relogio, c.pagina, 100);
  // O `play` navega a aba: documento novo, vigia velho morre junto.
  const paginaAntiga = c.pagina;
  const pagina2 = criarPagina(c.relogio, { videoId: "FAIXA_2", duracao: 25 });
  pagina2.injetavel = true;
  pagina2.aoReceber = paginaAntiga.aoReceber;
  // troca o documento por baixo do worker (é o que uma navegação de verdade faz)
  c.trocarPagina(pagina2);
  c.worker.navegou("https://www.youtube.com/watch?v=FAIXA_2");
  pagina2.video.paused = false;
  await avancar(c.relogio, pagina2, 26000, 50);

  const evs2 = eventos(c.worker).filter((e) => e.codigo === "cod-2");
  eq("faixa 2 teve um iniciou", evs2.filter((e) => e.event === "faixa_iniciou").length, 1);
  eq("faixa 2 teve um terminando", evs2.filter((e) => e.event === "faixa_terminando").length, 1);
  ok("faixa 2 leva o código NOVO", evs2.every((e) => e.codigo === "cod-2") && evs2.length === 2);
  ok("todos os eventos da faixa 2 são da faixa 2", evs2.every((e) => e.videoId === "FAIXA_2"));
  eq("faixa 2 também pausou antes do fim", pagina2.pausas, 1);
  ok(
    "e na mesma janela",
    pagina2.pausadoEm >= 0.5 && pagina2.pausadoEm <= 1.5,
    "faltavam " + (pagina2.pausadoEm || 0).toFixed(3) + "s"
  );
  eq("nenhum evento a mais no total", eventos(c.worker).length, 4);
}

// ── 12. vigia velho falando fora de hora ───────────────────────────────────

console.log("\n# um vigia de faixa passada tenta falar");
{
  const c = await cenario({ duracao: 60, videoNovo: "FAIXA_1", codigo: "cod-1" });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 2000);
  ok("vigia da faixa 1 armada", c.pagina.temVigia());
  // O desktop já mudou de despacho (a fila andou por outro caminho).
  c.worker.receber({ type: "watch", tabId: 7, videoId: "FAIXA_2", codigo: "cod-2" });
  await avancar(c.relogio, c.pagina, 100);
  const antes = eventos(c.worker).filter((e) => e.codigo === "cod-1").length;
  await avancar(c.relogio, c.pagina, 3000);
  eq("nada do código velho passa", eventos(c.worker).filter((e) => e.codigo === "cod-1").length, antes);
  // De propósito ele NÃO some na hora: o desktop arma a próxima ~6s antes do
  // fim da atual e só depois manda navegar. Soltar a trava nesse intervalo seria
  // devolver o fim da faixa atual para o autoplay — o bug que estamos matando.
  ok("continua protegendo a faixa atual até o documento trocar", c.pagina.temVigia());
  // Na primeira vez que ele fala (batida de 20s), o worker manda sumir.
  await avancar(c.relogio, c.pagina, 21000, 100);
  eq("aí sim ele se desarma", c.pagina.temVigia(), false);
  eq("e nunca passou nada do código velho", eventos(c.worker).filter((e) => e.codigo === "cod-1").length, antes);
}

// ── 13. os dois caminhos veem a MESMA troca de vídeo ──────────────────────

console.log("\n# a troca de vídeo é vista pelo vigia E pelo worker");
{
  const c = await cenario({ duracao: 120 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 2000);
  // O worker vê pela navegação da aba…
  c.worker.navegou("https://www.youtube.com/watch?v=INTRUSO7");
  // …e o vigia da página vê pela URL, no tique seguinte.
  c.pagina.href = "https://www.youtube.com/watch?v=INTRUSO7";
  await avancar(c.relogio, c.pagina, 2000);
  const fins = eventos(c.worker).filter((e) => e.event === "faixa_terminou");
  eq("um único faixa_terminou", fins.length, 1);
  eq("com o vídeo que está tocando", fins[0] && fins[0].videoId, "INTRUSO7");
}

// ── 14. unwatch DEPOIS da trava não deixa vigia pendurada ─────────────────

console.log("\n# unwatch chega depois de a trava já ter agido");
{
  const c = await cenario({ duracao: 20 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 21000, 50);
  eq("a trava agiu", c.pagina.pausas, 1);
  ok("a vigia ainda está lá (a fila continua)", c.pagina.temVigia());
  c.worker.receber({ type: "unwatch" });
  await avancar(c.relogio, c.pagina, 500);
  eq("com o serviço prestado, o unwatch a remove na hora", c.pagina.temVigia(), false);
  // e nada de timer sobrando na página
  eq("sem timer órfão", c.relogio.timers.filter((t) => t.dono === "pagina").length, 0);
}

// ── 15. o switch de autoplay do YouTube (segunda trava) ──────────────────

console.log("\n# o switch de autoplay, quando o YouTube mostra");
{
  const c = await cenario({ duracao: 40 });
  await tocar(c);
  await avancar(c.relogio, c.pagina, 1500);
  eq("desligou o autoplay do YouTube", c.pagina.autoplayLigado, false);
  eq("com um clique só", c.pagina.cliquesAutonav, 1);
  await avancar(c.relogio, c.pagina, 5000);
  eq("e não fica brigando com a UI", c.pagina.cliquesAutonav, 1);
  // ao soltar, a preferência do usuário volta como estava
  c.worker.receber({ type: "unwatch" });
  await avancar(c.relogio, c.pagina, 40000, 50);
  eq("religou ao terminar", c.pagina.autoplayLigado, true);
  eq("a pausa continua sendo a trava de verdade", c.pagina.pausas, 1);
}

console.log("\n# autoplay já desligado: não mexemos");
{
  const c = await cenario({ duracao: 30 });
  c.pagina.autoplayLigado = false;
  await tocar(c);
  await avancar(c.relogio, c.pagina, 2000);
  eq("nenhum clique", c.pagina.cliquesAutonav, 0);
  c.worker.receber({ type: "unwatch" });
  await avancar(c.relogio, c.pagina, 30000, 50);
  eq("e continua desligado, como o usuário deixou", c.pagina.autoplayLigado, false);
  eq("sem clique nenhum no fim também", c.pagina.cliquesAutonav, 0);
}

console.log("\n# mix/rádio: o botão nem existe — a pausa é que segura");
{
  const c = await cenario({ duracao: 30 });
  c.pagina.temBotaoAutonav = false;
  await tocar(c);
  await avancar(c.relogio, c.pagina, 31000, 50);
  eq("sem botão, sem clique", c.pagina.cliquesAutonav, 0);
  eq("mas pausou do mesmo jeito", c.pagina.pausas, 1);
  eq("e o vídeo não terminou", c.pagina.video.ended, false);
}

console.log("\n# vídeo do usuário, sem fila: o autoplay dele não é tocado");
{
  const relogio = criarRelogio();
  const aba = { id: 7, url: "https://www.youtube.com/watch?v=SOZINHO", title: "…", audible: true, active: true, status: "complete", windowId: 1, mutedInfo: { muted: false } };
  const pagina = criarPagina(relogio, { videoId: "SOZINHO", duracao: 20 });
  pagina.injetavel = true;
  criarWorker(relogio, { aba, pagina });
  pagina.video.paused = false;
  await avancar(relogio, pagina, 21000, 50);
  eq("autoplay intacto", pagina.autoplayLigado, true);
  eq("nenhum clique", pagina.cliquesAutonav, 0);
}

console.log(`\n${passes} ok, ${falhas} falha(s)`);
process.exit(falhas ? 1 : 0);
