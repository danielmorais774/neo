// Testa os sinais que a ponte reporta, sem navegador: `node scripts/testar-sinais.mjs`
//
// Carrega o `background.js` DE VERDADE num VM com `chrome`/`WebSocket` dublados
// (rodar o arquivo real é o ponto — uma cópia das funções aqui envelheceria em
// silêncio) e exercita as três decisões que não dependem de rede:
//
//   1. `youtubeKind` — que TIPO de página é, só pela URL. É o sinal que separa
//      "aba do YouTube" de "aba do YouTube com vídeo" mesmo quando a injeção
//      falha, e é o único que responde pela home.
//   2. `probeYoutube` — a sonda, contra DOMs falsos que imitam as páginas reais.
//   3. `collectTabs` — a montagem do snapshot, incluindo a degradação quando o
//      navegador recusa a injeção.

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

// ── o ambiente do service worker ───────────────────────────────────────────

function carregar({ tabs = [], executeScript } = {}) {
  const listeners = {};
  const chrome = {
    tabs: {
      query: async () => tabs,
      get: async (id) => tabs.find((t) => t.id === id),
      update: async () => {},
      create: async () => {},
      onUpdated: { addListener: () => {} },
      onActivated: { addListener: () => {} },
      onRemoved: { addListener: () => {} },
      onCreated: { addListener: () => {} },
    },
    alarms: { create: () => {}, onAlarm: { addListener: () => {} } },
    runtime: {
      onMessage: { addListener: (f) => (listeners.msg = f) },
      onStartup: { addListener: () => {} },
      onInstalled: { addListener: () => {} },
    },
    action: { setBadgeText: () => {}, setBadgeBackgroundColor: () => {} },
    scripting: {
      executeScript:
        executeScript ||
        (async () => {
          throw new Error("sem sonda neste teste");
        }),
    },
  };
  // WebSocket que nunca conecta: o snapshot é montado igual, só não é enviado.
  class FakeWS {
    constructor() {
      this.readyState = 0;
    }
    close() {}
  }
  FakeWS.OPEN = 1;
  FakeWS.CONNECTING = 0;

  const ctx = vm.createContext({
    chrome,
    WebSocket: FakeWS,
    console: { log: () => {}, warn: () => {} },
    setTimeout: () => 0,
    clearTimeout: () => {},
    URL,
    URLSearchParams,
  });
  vm.runInContext(fonte, ctx);
  return ctx;
}

// ── 1. tipo de página pela URL ─────────────────────────────────────────────

console.log("\n# youtubeKind (só a URL)");
{
  const { youtubeKind, idsFromUrl } = carregar();
  eq("watch", youtubeKind("https://www.youtube.com/watch?v=abc"), "watch");
  eq("home", youtubeKind("https://www.youtube.com/"), "home");
  eq("feed", youtubeKind("https://www.youtube.com/feed/subscriptions"), "feed");
  eq("busca", youtubeKind("https://www.youtube.com/results?search_query=rock"), "search");
  eq("playlist", youtubeKind("https://www.youtube.com/playlist?list=PL1"), "playlist");
  eq("shorts", youtubeKind("https://www.youtube.com/shorts/xyz"), "shorts");
  eq("canal @", youtubeKind("https://www.youtube.com/@canal"), "channel");
  eq("YouTube Music", youtubeKind("https://music.youtube.com/watch?v=abc"), "music");
  eq("Music home", youtubeKind("https://music.youtube.com/"), "music_home");
  eq("URL quebrada", youtubeKind("nao sou url"), "other");

  const ids = idsFromUrl("https://www.youtube.com/watch?v=c-g6X&list=RDc-g6X");
  eq("videoId", ids.videoId, "c-g6X");
  eq("listId (mix)", ids.listId, "RDc-g6X");
  eq("shorts vira videoId", idsFromUrl("https://www.youtube.com/shorts/zz9").videoId, "zz9");
}

console.log("\n# watchUrl (o link que se manda pra alguém)");
{
  const { watchUrl } = carregar();
  eq(
    "limpa list/index/t da URL da aba",
    watchUrl("https://www.youtube.com/watch?v=c-g6X&list=RDc-g6X&index=4&t=91s"),
    "https://www.youtube.com/watch?v=c-g6X"
  );
  eq(
    "Shorts vira link de watch",
    watchUrl("https://www.youtube.com/shorts/zz9"),
    "https://www.youtube.com/watch?v=zz9"
  );
  eq(
    "YouTube Music mantém o host",
    watchUrl("https://music.youtube.com/watch?v=abc&list=RDAMVM"),
    "https://music.youtube.com/watch?v=abc"
  );
  eq("home não tem link de vídeo", watchUrl("https://www.youtube.com/"), "");
  eq("URL quebrada não inventa link", watchUrl("nao sou url"), "");
}

// ── 2. a sonda contra páginas falsas ───────────────────────────────────────

function domFalso({
  href = "https://www.youtube.com/watch?v=abc",
  videos = [],
  genre = null,
  category = null,
  adShowing = false,
  mediaSession = null,
  musicSection = false,
  author = null,
  canalNoDom = null,
} = {}) {
  const el = (sel) => {
    if (sel === "#movie_player") {
      return { classList: { contains: (c) => adShowing && c === "ad-showing" } };
    }
    if (sel === 'meta[itemprop="genre"]') {
      return genre ? { getAttribute: () => genre } : null;
    }
    if (sel.includes("music-section")) {
      return musicSection ? { textContent: "Música" } : null;
    }
    if (sel === "#owner #channel-name a") {
      return canalNoDom ? { textContent: "  " + canalNoDom + "\n" } : null;
    }
    return null;
  };
  const url = new URL(href);
  return {
    document: { querySelectorAll: () => videos, querySelector: el },
    location: { hostname: url.hostname, search: url.search },
    navigator: { mediaSession: mediaSession ? { metadata: mediaSession } : null },
    window:
      category || author
        ? {
            ytInitialPlayerResponse: {
              microformat: { playerMicroformatRenderer: { category } },
              videoDetails: author ? { author } : undefined,
            },
          }
        : {},
  };
}

function sondar(cenario) {
  const { probeYoutube } = carregar();
  const amb = domFalso(cenario);
  const ctx = vm.createContext({ ...amb, URLSearchParams });
  ctx.window.ytInitialPlayerResponse = amb.window.ytInitialPlayerResponse;
  vm.runInContext(`(${probeYoutube.toString()})()`, ctx);
  return vm.runInContext(`(${probeYoutube.toString()})()`, ctx);
}

console.log("\n# probeYoutube (sonda na página)");
{
  const tocando = sondar({
    videos: [{ duration: 245, paused: false, currentTime: 72.4, volume: 1, muted: false, playbackRate: 1 }],
    category: "Music",
    author: "Papa Roach",
    mediaSession: { title: "No Matter What", artist: "Papa Roach", album: "" },
  });
  eq("tem mídia", tocando.hasMedia, true);
  eq("não pausado", tocando.paused, false);
  eq("posição", tocando.currentTime, 72.4);
  eq("duração", tocando.duration, 245);
  eq("classificado como música", tocando.contentType, "music");
  ok("fonte auditável", tocando.contentTypeSource.startsWith("microformat-category"), tocando.contentTypeSource);
  eq("faixa", tocando.track, "No Matter What");
  eq("artista", tocando.artist, "Papa Roach");
  eq("canal", tocando.channel, "Papa Roach");

  // Vídeo comum: o mediaSession não publica artista, e o canal continua vindo.
  const semArtista = sondar({
    videos: [{ duration: 600, paused: false }],
    category: "Gaming",
    author: "Canal do Zé",
  });
  eq("canal em vídeo sem artista", semArtista.channel, "Canal do Zé");
  eq("sem artista mesmo", semArtista.artist, "");

  // Mundo ISOLATED (sem `ytInitialPlayerResponse`): sobra o DOM.
  const canalDom = sondar({ videos: [{ duration: 10, paused: false }], canalNoDom: "Lofi Girl" });
  eq("canal pelo DOM, já sem espaços", canalDom.channel, "Lofi Girl");

  const semCanal = sondar({ href: "https://www.youtube.com/", videos: [] });
  eq("home não inventa canal", semCanal.channel, "");

  const comum = sondar({ videos: [{ duration: 600, paused: true, currentTime: 3 }], category: "Gaming" });
  eq("vídeo comum", comum.contentType, "video");
  eq("pausado", comum.paused, true);

  const semGlobal = sondar({ videos: [{ duration: 10, paused: false }], genre: "Music" });
  eq("cai no microdata", semGlobal.contentType, "music");
  ok("fonte diz qual valeu", semGlobal.contentTypeSource.startsWith("meta-genre"), semGlobal.contentTypeSource);

  const painel = sondar({ videos: [{ duration: 10, paused: false }], musicSection: true });
  eq("cai no painel Música", painel.contentType, "music");

  const mix = sondar({
    href: "https://www.youtube.com/watch?v=a&list=RDabc",
    videos: [{ duration: 10, paused: false }],
  });
  eq("mix/rádio conta como música", mix.contentType, "music");
  eq("fonte do mix", mix.contentTypeSource, "radio-mix");

  const home = sondar({ href: "https://www.youtube.com/", videos: [] });
  eq("home não tem mídia", home.hasMedia, false);
  eq("home não classifica", home.contentType, "unknown");

  // A prévia muda do feed cria um <video> — sem duração, não conta como mídia.
  const previa = sondar({ href: "https://www.youtube.com/", videos: [{ duration: 0, paused: false }] });
  eq("prévia do feed não é mídia", previa.hasMedia, false);

  const anuncio = sondar({ videos: [{ duration: 30, paused: false }], adShowing: true });
  eq("anúncio detectado", anuncio.adShowing, true);

  const aoVivo = sondar({ videos: [{ duration: Infinity, paused: false }] });
  eq("ao vivo", aoVivo.live, true);
  eq("ao vivo sem duração falsa", aoVivo.duration, null);

  const musicYt = sondar({ href: "https://music.youtube.com/watch?v=a", videos: [{ duration: 100, paused: false }] });
  eq("music.youtube.com é música", musicYt.contentType, "music");
}

// ── 3. o snapshot inteiro ──────────────────────────────────────────────────

console.log("\n# collectTabs (o snapshot que vai pro Neo)");
{
  const tabs = [
    { id: 1, url: "https://www.youtube.com/watch?v=abc&list=RDx", title: "Clipe", audible: true, active: true, status: "complete", mutedInfo: { muted: false }, windowId: 7 },
    { id: 2, url: "https://www.youtube.com/", title: "YouTube", audible: false, status: "complete", mutedInfo: { muted: false } },
    { id: 3, url: "https://open.spotify.com/playlist/x", title: "Rock", audible: false, status: "complete" },
    { id: 4, url: "https://example.com/", title: "nada a ver", audible: false, status: "complete" },
  ];
  const ctx = carregar({
    tabs,
    executeScript: async ({ target }) => {
      if (target.tabId === 1) {
        return [{ result: { probe: "ok", hasMedia: true, paused: false, duration: 245, currentTime: 10, contentType: "music", contentTypeSource: "microformat-category:Music", track: "T", artist: "A", channel: "Canal C", adShowing: false } }];
      }
      return [{ result: { probe: "ok", hasMedia: false, paused: null, contentType: "unknown" } }];
    },
  });
  const snap = await ctx.collectTabs();

  eq("só abas de mídia entram", snap.length, 3);
  const yt = snap.find((t) => t.tabId === 1);
  const home = snap.find((t) => t.tabId === 2);
  const sp = snap.find((t) => t.tabId === 3);

  eq("a que toca vem primeiro", snap[0].tabId, 1);
  eq("kind watch", yt.kind, "watch");
  eq("videoId", yt.videoId, "abc");
  eq("listId", yt.listId, "RDx");
  eq("som na guia", yt.audible, true);
  eq("tocando", yt.playing, true);
  eq("progresso", yt.progress, 0.041);
  eq("música", yt.contentType, "music");
  eq("canal chega no snapshot", yt.channel, "Canal C");
  // O link limpo, e não a URL da aba (que carrega o `list=RDx`).
  eq("link do vídeo", yt.videoUrl, "https://www.youtube.com/watch?v=abc");

  eq("home não tem link de vídeo", home.videoUrl, "");
  eq("spotify não ganha link do YouTube", sp.videoUrl, "");

  eq("home é home", home.kind, "home");
  eq("home sem mídia", home.hasMedia, false);
  eq("home não toca", home.playing, false);

  eq("spotify entra", sp.site, "spotify");
  eq("spotify não é sondado", sp.probe, "skipped");

  // Injeção recusada (aba protegida, política do navegador): a aba continua no
  // snapshot, só sem detalhe — sumir seria pior.
  const ctx2 = carregar({
    tabs: [tabs[0]],
    executeScript: async () => {
      throw new Error("Cannot access contents of the page");
    },
  });
  const snap2 = await ctx2.collectTabs();
  eq("aba recusada continua no snapshot", snap2.length, 1);
  eq("recusa é dita", snap2[0].probe, "denied");
  eq("cai no som da guia", snap2[0].playing, true);
  // O link vem da URL: aba sem sonda perde o canal, não o link.
  eq("aba recusada ainda tem link", snap2[0].videoUrl, "https://www.youtube.com/watch?v=abc");

  // Aba ainda carregando não é sondada (a página não responderia).
  const ctx3 = carregar({
    tabs: [{ id: 9, url: "https://www.youtube.com/watch?v=z", title: "", status: "loading" }],
  });
  const snap3 = await ctx3.collectTabs();
  eq("carregando não sonda", snap3[0].probe, "loading");
  eq("carregando ainda tem link", snap3[0].videoUrl, "https://www.youtube.com/watch?v=z");
}

// ── 4. a AÇÃO (pausar/mudo/volume/próxima) ─────────────────────────────────
//
// A sessão 7687b737 é o motivo desta seção existir: "muta essa aba" voltou
// `success=True` e o som continuou. A ação pegava o PRIMEIRO <video> do DOM
// (podia ser a prévia muda) e ninguém conferia o resultado.

console.log("\n# applyMediaAction (a ação na página)");
{
  const { applyMediaAction } = carregar();

  // Página do YouTube como ela é: a prévia SEM duração vem antes do player.
  function pagina({ comApi = false, mudoInicial = false, volumeInicial = 0.5, botoes = [] } = {}) {
    const previa = { duration: 0, paused: false, muted: false, volume: 1 };
    const player = {
      duration: 245, paused: false, muted: mudoInicial, volume: volumeInicial,
      pause() { this.paused = true; }, play() { this.paused = false; },
    };
    const api = comApi
      ? {
          _mudo: mudoInicial, _vol: Math.round(volumeInicial * 100), _chamou: [],
          getPlayerState() { return 1; },
          isMuted() { return this._mudo; },
          mute() { this._mudo = true; player.muted = true; },
          unMute() { this._mudo = false; player.muted = false; },
          getVolume() { return this._vol; },
          setVolume(n) { this._vol = n; player.volume = n / 100; },
          pauseVideo() { player.paused = true; },
          playVideo() { player.paused = false; },
          nextVideo() { this._chamou.push("next"); },
        }
      : null;
    const clicados = [];
    const doc = {
      querySelectorAll: () => [previa, player],
      querySelector: (sel) => {
        if (sel === "#movie_player") return api;
        if (botoes.includes(sel)) return { click: () => clicados.push(sel) };
        return null;
      },
    };
    return { doc, previa, player, api, clicados };
  }

  async function agir(acao, cenario) {
    const p = pagina(cenario);
    const ctx = vm.createContext({ document: p.doc, setTimeout: (f) => f(), Promise, Math, Array, Object });
    const fn = vm.runInContext(`(${applyMediaAction.toString()})`, ctx);
    return { r: await fn(acao), p };
  }

  {
    // O caso da sessão: mutar tem que mexer no PLAYER, não na prévia.
    const { r, p } = await agir("mute", {});
    eq("mudo confirmado", r.ok, true);
    eq("mutou o player (o que tem duração)", p.player.muted, true);
    eq("não tocou na prévia", p.previa.muted, false);
    eq("diz que virou mudo", r.virou, true);
  }
  {
    const { r, p } = await agir("mute", { mudoInicial: true });
    eq("desmutou", p.player.muted, false);
    eq("diz que era mudo", r.era, true);
    eq("diz que virou som", r.virou, false);
  }
  {
    const { r, p } = await agir("mute", { comApi: true });
    eq("usa a API do player quando existe", r.source, "player-api");
    eq("API mutou", p.api._mudo, true);
  }
  {
    const { r, p } = await agir("pause", {});
    eq("pausou de verdade", p.player.paused, true);
    eq("confirmado", r.ok, true);
  }
  {
    const { r, p } = await agir("volume_up", { volumeInicial: 0.5, mudoInicial: true });
    eq("subiu 20", r.para, 70);
    eq("volume aplicado", Math.round(p.player.volume * 100), 70);
    eq("subir volume tira do mudo", p.player.muted, false);
    eq("confirmado", r.ok, true);
  }
  {
    // YouTube Music: `.ytp-next-button` não existe; a barra é outra.
    const { r, p } = await agir("next", { botoes: ["tp-yt-paper-icon-button.next-button"] });
    eq("achou o botão do YouTube Music", r.ok, true);
    eq("clicou nele", p.clicados[0], "tp-yt-paper-icon-button.next-button");
  }
  {
    const { r } = await agir("next", { comApi: true });
    eq("com API, nem precisa de botão", r.via, "player-api");
  }
  {
    // Página sem player nenhum: erro explícito, não sucesso silencioso.
    const ctx = vm.createContext({
      document: { querySelectorAll: () => [], querySelector: () => null },
      setTimeout: (f) => f(), Promise, Math, Array, Object,
    });
    const fn = vm.runInContext(`(${applyMediaAction.toString()})`, ctx);
    const r = await fn("mute");
    eq("sem player não é sucesso", r.ok, false);
    ok("e diz por quê", !!r.error, JSON.stringify(r));
  }
}

console.log(`\n${passes} ok, ${falhas} falha(s)`);
process.exit(falhas ? 1 : 0);
