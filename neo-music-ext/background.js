// Neo Music Bridge — MV3 service worker.
//
// Bridges the browser to the Neo Desktop app over a local WebSocket
// (ws://127.0.0.1:17872):
//   • reports open music tabs (YouTube/Spotify) → Neo reuses an existing tab
//     instead of opening a new one (fed to the core as media_context.browser_tabs);
//   • executes "play" commands from Neo (focus the tab + navigate to the videoId);
//   • enquanto o Neo toca uma FILA, instala um vigia DENTRO da aba que pausa a
//     faixa antes do fim (a trava de autoplay) e reporta o andamento dela.
//
// MV3 service workers are ephemeral. A chrome.alarms keepalive + reconnect-on-close
// keeps the bridge effectively persistent — e é por isso que o vigia de fila mora
// na página, não aqui (ver "Vigia de fila" abaixo). No own AI loop; o único script
// injetado é o vigia, e só enquanto uma fila está tocando.

const WS_BASE = "ws://127.0.0.1:17872";
// Compatibilidade: o popup ainda mostra a URL base.
const WS_URL = WS_BASE;
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 15000;
const REPORT_DEBOUNCE_MS = 400;

let ws = null;
let reconnectDelay = RECONNECT_MIN_MS;
let reportTimer = null;

// ── Credencial da ponte ──────────────────────────────────────────────────────
//
// A ponte do Neo Desktop passou a exigir `?token=` no handshake. Não é cerimônia:
// loopback não é fronteira de confiança — qualquer processo da máquina alcança a
// porta, e uma PÁGINA WEB consegue abrir WebSocket para 127.0.0.1. Como a ponte
// guarda uma conexão só, a conexão hostil DESLOCAVA esta extensão, passava a
// receber os comandos do Neo e a mandar títulos de aba (que entram no prompt dele).
//
// O token sai da aba Dispositivos do Neo Desktop e é colado no popup uma vez.
// `chrome.storage.local` porque o service worker MV3 é efêmero: variável em
// memória não sobrevive ao reciclo, e pedir o token de novo a cada reciclo seria
// insuportável.
let extToken = null;

async function carregarToken() {
  try {
    const { neoExtToken } = await chrome.storage.local.get("neoExtToken");
    extToken = typeof neoExtToken === "string" && neoExtToken ? neoExtToken : null;
  } catch {
    extToken = null;
  }
  return extToken;
}

// Motivo da última recusa, para o popup poder dizer o que fazer. O WebSocket do
// navegador NÃO expõe o status HTTP de um handshake recusado (vira só um close
// 1006), então isto é inferência honesta, não leitura do servidor.
let ultimoMotivo = null;


function isMusicUrl(url) {
  return (
    /:\/\/([^/]*\.)?youtube\.com\//.test(url) ||
    /:\/\/open\.spotify\.com\//.test(url)
  );
}

function siteOf(url) {
  if (/youtube\.com/.test(url)) return "youtube";
  if (/open\.spotify\.com/.test(url)) return "spotify";
  return "";
}

// Que TIPO de página do YouTube é esta, lido só da URL — de graça e confiável
// mesmo quando a injeção falha (aba descarregada, ainda carregando, bloqueada).
// É isto que separa "aba do YouTube" de "aba do YouTube com vídeo": a home
// (`youtube.com/`) é uma aba do YouTube que não tem vídeo nenhum pra controlar,
// e antes ela entrava no snapshot indistinguível de uma aba tocando.
function youtubeKind(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return "other";
  }
  const path = u.pathname;
  if (/(^|\.)music\.youtube\.com$/.test(u.hostname)) {
    return path === "/" ? "music_home" : "music";
  }
  if (path === "/watch") return "watch";
  if (path === "/" || path === "") return "home";
  if (path.startsWith("/shorts/")) return "shorts";
  if (path.startsWith("/results")) return "search";
  if (path.startsWith("/playlist")) return "playlist";
  if (path.startsWith("/feed")) return "feed";
  if (path.startsWith("/channel/") || path.startsWith("/@") || path.startsWith("/c/")) {
    return "channel";
  }
  if (path.startsWith("/embed/")) return "embed";
  return "other";
}

function idsFromUrl(url) {
  try {
    const u = new URL(url);
    const shorts = u.pathname.startsWith("/shorts/")
      ? u.pathname.slice("/shorts/".length).split("/")[0]
      : "";
    return {
      videoId: u.searchParams.get("v") || shorts || "",
      listId: u.searchParams.get("list") || "",
    };
  } catch {
    return { videoId: "", listId: "" };
  }
}

// A URL CANÔNICA do vídeo — o link que se manda pra alguém. A URL da aba serve
// pra reusar a aba, não pra citar: ela vem com `list=`, `index=`, `t=`, `pp=` e,
// nos Shorts, num formato (`/shorts/<id>`) que não é o que se cola num chat.
// Vem da URL, não da sonda, de propósito: assim a aba descartada, a que ainda
// carrega e a que recusou a injeção também têm link.
function watchUrl(url) {
  const { videoId } = idsFromUrl(url);
  if (!videoId) return "";
  let host = "www.youtube.com";
  try {
    if (/(^|\.)music\.youtube\.com$/.test(new URL(url).hostname)) {
      host = "music.youtube.com";
    }
  } catch {
    /* URL impossível de parsear — vale o host padrão */
  }
  return "https://" + host + "/watch?v=" + encodeURIComponent(videoId);
}

// ── a sonda ────────────────────────────────────────────────────────────────
//
// Roda DENTRO da página do YouTube. Precisa ser autocontida (o executeScript
// serializa a função) e não pode lançar: qualquer erro aqui viraria uma aba
// ausente do snapshot, que é pior que uma aba sem detalhe.
//
// Injetada no mundo MAIN porque três das melhores fontes só existem lá:
// `ytInitialPlayerResponse` (a categoria que o próprio YouTube atribuiu ao
// vídeo), `navigator.mediaSession.metadata` (faixa/artista/álbum já
// normalizados pelo player) e o `<video>` com o estado real de play/pause. O
// mundo ISOLATED enxerga o DOM mas não os globais da página — dá pra cair nele,
// e aí sobram só os sinais de microdata.
function probeYoutube() {
  const out = {
    probe: "ok",
    hasMedia: false,
    paused: null,
    ended: false,
    adShowing: false,
    currentTime: null,
    duration: null,
    volume: null,
    playbackRate: null,
    pageMuted: null,
    contentType: "unknown",
    contentTypeSource: "",
    track: "",
    artist: "",
    album: "",
    channel: "",
    live: false,
  };
  try {
    // ── player ──
    // O <video> da home também existe (as prévias mudas do feed), então quem
    // decide se há mídia de verdade é a DURAÇÃO, não a presença do elemento.
    const vids = Array.from(document.querySelectorAll("video"));
    const v =
      vids.find((x) => x.duration > 0 && !x.paused) ||
      vids.find((x) => x.duration > 0) ||
      null;
    if (v) {
      out.hasMedia = true;
      out.paused = !!v.paused;
      out.ended = !!v.ended;
      out.currentTime = Math.round((v.currentTime || 0) * 10) / 10;
      out.duration = Number.isFinite(v.duration)
        ? Math.round(v.duration * 10) / 10
        : null;
      out.live = !Number.isFinite(v.duration) || v.duration === Infinity;
      out.volume = Math.round((v.volume ?? 0) * 100) / 100;
      out.pageMuted = !!v.muted;
      out.playbackRate = v.playbackRate ?? null;
    }
    // Anúncio: o player ganha `.ad-showing`. Sem isto, "está tocando" pode ser
    // o comercial, e o título/duração seriam os do anúncio, não os da música.
    const player = document.querySelector("#movie_player");
    out.adShowing = !!(player && player.classList.contains("ad-showing"));

    // ── faixa/artista ──
    // O próprio player já normaliza isso para o SO (é o que aparece no centro
    // de mídia do macOS/Windows); aproveitar é melhor que raspar o DOM.
    try {
      const md = navigator.mediaSession && navigator.mediaSession.metadata;
      if (md) {
        out.track = md.title || "";
        out.artist = md.artist || "";
        out.album = md.album || "";
      }
    } catch {
      /* mediaSession indisponível neste mundo/aba */
    }

    // ── canal ──
    // QUEM publicou. `videoDetails.author` é a resposta do próprio player e é a
    // única que continua certa depois da navegação por SPA; as demais existem
    // para o mundo ISOLATED (sem os globais da página), em ordem de confiança.
    // Não é a mesma coisa que `artist`: em vídeo comum o mediaSession não
    // publica artista nenhum, e em música o artista pode não ser o canal.
    try {
      const ipr = typeof window !== "undefined" ? window.ytInitialPlayerResponse : null;
      const det = ipr && ipr.videoDetails;
      const micro =
        ipr && ipr.microformat && ipr.microformat.playerMicroformatRenderer;
      const doDom = (sel, attr) => {
        const el = document.querySelector(sel);
        if (!el) return "";
        const txt = attr ? el.getAttribute(attr) : el.textContent;
        return (txt || "").trim();
      };
      out.channel = String(
        (det && det.author) ||
          (micro && micro.ownerChannelName) ||
          // Microdata do `<span itemprop="author">` — sobrevive ao ISOLATED.
          doDom('span[itemprop="author"] link[itemprop="name"]', "content") ||
          doDom('link[itemprop="name"]', "content") ||
          doDom("#owner #channel-name a") ||
          doDom("ytd-channel-name a") ||
          doDom(".ytp-title-channel-name") ||
          ""
      ).trim();
    } catch {
      /* mundo ISOLATED, DOM ainda não montado, ou página sem dono (home/feed) */
    }

    // ── música ou vídeo comum ──
    // Em ordem de confiança. O YouTube SABE a resposta: ele carimba uma
    // `category` no microformat de cada vídeo ("Music"/"Música"), que é a mesma
    // coisa mostrada como categoria na página.
    const host = location.hostname;
    if (/(^|\.)music\.youtube\.com$/.test(host)) {
      out.contentType = "music";
      out.contentTypeSource = "music.youtube.com";
    }
    if (out.contentType === "unknown") {
      try {
        const cat =
          window.ytInitialPlayerResponse &&
          window.ytInitialPlayerResponse.microformat &&
          window.ytInitialPlayerResponse.microformat.playerMicroformatRenderer &&
          window.ytInitialPlayerResponse.microformat.playerMicroformatRenderer
            .category;
        if (cat) {
          out.contentType = /^(music|música|musica)$/i.test(cat)
            ? "music"
            : "video";
          out.contentTypeSource = "microformat-category:" + cat;
        }
      } catch {
        /* mundo ISOLATED, ou o global ainda não existe */
      }
    }
    if (out.contentType === "unknown") {
      // Microdata do DOM — sobrevive ao mundo ISOLATED.
      const genre = document.querySelector('meta[itemprop="genre"]');
      const g = genre && genre.getAttribute("content");
      if (g) {
        out.contentType = /^(music|música|musica)$/i.test(g) ? "music" : "video";
        out.contentTypeSource = "meta-genre:" + g;
      }
    }
    if (out.contentType === "unknown") {
      // O painel "Música" que o YouTube abre sob a descrição de conteúdo
      // licenciado (faixa, artista, álbum). Só existe em vídeo musical.
      const sec = document.querySelector(
        "ytd-video-description-music-section-renderer, ytd-metadata-row-container-renderer ytd-metadata-row-renderer"
      );
      if (sec && /m[úu]sica|music/i.test(sec.textContent || "")) {
        out.contentType = "music";
        out.contentTypeSource = "description-music-section";
      }
    }
    if (out.contentType === "unknown" && out.hasMedia) {
      // Mix/rádio (`list=RD…`) é a fila automática de música do YouTube.
      const list = new URLSearchParams(location.search).get("list") || "";
      if (/^RD/.test(list)) {
        out.contentType = "music";
        out.contentTypeSource = "radio-mix";
      }
    }
  } catch (e) {
    out.probe = "error";
  }
  return out;
}

// Injeta a sonda numa aba. MAIN primeiro (onde estão os globais do YouTube),
// ISOLATED como rede de segurança — em navegador que não aceita `world`, a
// chamada rejeita inteira e sem o fallback a aba perderia TODO o detalhe.
async function probeTab(tabId) {
  for (const world of ["MAIN", "ISOLATED"]) {
    try {
      const res = await chrome.scripting.executeScript({
        target: { tabId },
        func: probeYoutube,
        world,
      });
      const r = res && res[0] && res[0].result;
      if (r) return { ...r, probeWorld: world };
    } catch (e) {
      if (world === "ISOLATED") {
        return { probe: "denied", probeError: e && e.message ? e.message : String(e) };
      }
    }
  }
  return { probe: "denied" };
}

// Quantas abas de mídia vale sondar num relatório. O snapshot sai a cada evento
// de aba (debounced) e a cada keepalive; sem teto, uma janela com 40 abas do
// YouTube pagaria 40 injeções por ciclo.
const MAX_PROBED_TABS = 12;

async function collectTabs() {
  const tabs = await chrome.tabs.query({});
  const alvos = tabs.filter((t) => t.url && isMusicUrl(t.url));
  // Quem tem som, quem está em foco e as páginas de vídeo primeiro: se o teto
  // cortar, corta o que menos importa.
  const ordem = alvos
    .map((t) => ({
      t,
      peso:
        (t.audible ? 8 : 0) +
        (t.active ? 4 : 0) +
        (youtubeKind(t.url) === "watch" || youtubeKind(t.url) === "music" ? 2 : 0),
    }))
    .sort((a, b) => b.peso - a.peso)
    .map((x) => x.t);

  const out = [];
  let sondadas = 0;
  for (const t of ordem) {
    const site = siteOf(t.url);
    const kind = site === "youtube" ? youtubeKind(t.url) : "";
    const { videoId, listId } = idsFromUrl(t.url);
    const base = {
      site,
      tabId: t.id,
      url: t.url,
      title: t.title || "",
      // Compatibilidade: `playing` sempre existiu e significava "a aba emite
      // som". Continua valendo como piso — a sonda refina abaixo, porque um
      // vídeo tocando MUDO não é audível e mesmo assim está tocando.
      playing: !!t.audible,
      audible: !!t.audible,
      muted: !!(t.mutedInfo && t.mutedInfo.muted),
      active: !!t.active,
      windowId: t.windowId ?? null,
      discarded: !!t.discarded,
      status: t.status || "",
      kind,
      videoId,
      listId,
      // O link pra citar/compartilhar, limpo dos parâmetros da sessão de quem
      // assiste (ver `watchUrl`). Vazio quando a aba não tem vídeo nenhum.
      videoUrl: site === "youtube" ? watchUrl(t.url) : "",
    };

    // Sondar só o que pode responder: aba do YouTube (é onde temos permissão de
    // host), carregada e não descartada.
    const sondavel =
      site === "youtube" &&
      !t.discarded &&
      t.status !== "loading" &&
      sondadas < MAX_PROBED_TABS;
    if (sondavel) {
      sondadas += 1;
      const det = await probeTab(t.id);
      Object.assign(base, det);
      if (det.paused === true) base.playing = false;
      if (det.paused === false && det.hasMedia) base.playing = true;
      if (base.duration && base.currentTime != null) {
        base.progress = Math.min(
          1,
          Math.round((base.currentTime / base.duration) * 1000) / 1000
        );
      }
    } else {
      // Por que NÃO sondou — silêncio aqui vira "não tem player" lá na frente.
      // Spotify cai em `skipped` por falta de permissão de host, não por não
      // ter o que tocar.
      base.probe = t.discarded
        ? "discarded"
        : t.status === "loading"
          ? "loading"
          : "skipped";
    }

    // A home NUNCA tem vídeo para controlar, mesmo que a prévia do feed tenha
    // criado um <video>. Dizer isso explicitamente é o ponto: o Neo pedia pause
    // para uma aba que não tinha o que pausar.
    if (kind === "home" || kind === "feed" || kind === "search" || kind === "channel") {
      base.hasMedia = base.hasMedia === true && base.duration ? true : false;
      if (!base.hasMedia) base.playing = false;
    }
    out.push(base);
  }
  return out;
}

function wsOpen() {
  return ws && ws.readyState === WebSocket.OPEN;
}

// Toolbar badge: verde = ponte conectada, cinza = não. Enquanto o Neo está
// dirigindo uma fila o badge vira ROXO com "▶" — dá para ver, sem abrir nada,
// que esta aba está sob controle do neo-core.
function setBadge(connected) {
  try {
    const controlado = !!watchState;
    chrome.action.setBadgeText({
      text: controlado ? "▶" : connected ? "●" : "○",
    });
    chrome.action.setBadgeBackgroundColor({
      color: controlado ? "#8b5cf6" : connected ? "#22c55e" : "#6b7280",
    });
    chrome.action.setTitle({
      title: controlado
        ? "Neo Music Bridge — fila do Neo tocando nesta janela"
        : "Neo Music Bridge",
    });
  } catch {
    /* action API may be briefly unavailable during worker spin-up */
  }
}

async function reportTabs() {
  // As duas pontas recebem o MESMO snapshot: o desktop (que o repassa no turno)
  // e o core (que passa a saber das abas mesmo sem turno e sem desktop).
  // Coletar uma vez e mandar aos dois evita duas varreduras de aba por ciclo.
  if (!wsOpen() && !superficieAberta()) return;
  try {
    const tabs = await collectTabs();
    if (wsOpen()) ws.send(JSON.stringify({ type: "tabs", tabs }));
    if (superficieAberta()) SURF.ws.send(JSON.stringify({ type: "tabs", tabs }));
  } catch (e) {
    console.warn("[neo-music] reportTabs failed", e);
  }
}

function scheduleReport() {
  if (reportTimer) clearTimeout(reportTimer);
  reportTimer = setTimeout(reportTabs, REPORT_DEBOUNCE_MS);
}

// ---------------------------------------------------------------------------
// Vigia de fila (docs/playlists-skill-plan.md §7)
// ---------------------------------------------------------------------------
// Quando o Neo está tocando uma FILA, ele manda `{type:"watch"}` com a aba, a
// faixa e o `codigo` do despacho. Daí em diante quem vigia a faixa é a PRÓPRIA
// PÁGINA: instalamos um vigia no mundo ISOLATED da aba, e é ele que
//
//   • dá PAUSE a ~0,9s do fim — a trava de autoplay;
//   • avisa `faixa_iniciou`, `faixa_terminando` (~6s do fim) e `faixa_terminou`;
//   • devolve em todo aviso o `codigo` que veio no `watch`.
//
// A vigia mora na PÁGINA porque o service worker do MV3 é descartável: o
// `setInterval` dele some junto, e o `chrome.alarms` tem piso de ~30s — não dá
// precisão de segundo. Um timer da página sobrevive à morte do worker, e o
// `chrome.runtime.sendMessage` do vigia ACORDA o worker de volta. O worker aqui
// só instala, encaminha e solta.
//
// Por que PAUSE, e não o toggle de autoplay: em vídeo de música o YouTube monta
// um mix/rádio (`list=RD…&start_radio=1`) e nem oferece o botão de desligar.
// Sem `ended` não há o que emendar — nem o autonav, nem o rádio. E o toggle é
// PREFERÊNCIA do usuário: desligar por baixo dos panos deixaria rastro depois do
// `unwatch`. (A versão anterior apostava nele, e era metade do motivo de o
// autoplay ter passado por cima da fila em 2026-09-08.)

// Faltando isto (segundos) para o fim, já pedimos a próxima ao neo-core. O
// evento certo é "está acabando", não "acabou": dá tempo de trocar antes de a
// faixa morrer, e não deixa buraco de silêncio entre as faixas.
const ENDING_WINDOW_S = 6;
// A que distância do fim damos PAUSE. Com o `timeupdate` como relógio (~4x/s), a
// pausa cai entre ~0,65s e ~0,9s do fim — dentro da janela que o Neo pede.
const PAUSE_BEFORE_END_S = 0.9;
// Para ONDE recuamos o playhead depois de pausar. Pausar sozinho NÃO segura o
// YouTube: medido no navegador em 2026-09-08 — com o <video> em `paused=true,
// ended=false` a 0,88s do fim, o player mesmo assim declarou
// `getPlayerState()===0` (ENDED) e o autonav emendou outro vídeo em ~6s. Com o
// playhead estacionado aqui, o player volta para 2 (PAUSED) e fica: não há mais
// fim nenhum para ele detectar. Como já estamos pausados, recuar é silencioso.
const PARK_BEFORE_END_S = 4;
// Relógio de reserva do vigia. O relógio BOM é o `timeupdate` do próprio
// <video>: ele vem do player, não da fila de timers, e por isso NÃO é
// estrangulado quando a aba está em segundo plano — que é exatamente onde a
// música fica. Este intervalo é manutenção (URL, batida, TTL) e, em aba
// escondida, o Chrome o segura em ~1x/s.
const GUARD_TICK_MS = 1000;
// De quanto em quanto o vigia cutuca o worker. Não é enfeite: manter o worker
// acordado mantém o WebSocket aberto, e o desktop DESARMA a vigia dele quando o
// socket cai (ext_bridge.rs, `clear_connection`) — socket caído no meio da
// faixa é fila interrompida.
const GUARD_HEARTBEAT_MS = 20000;
// Teto de vida do vigia numa página, se todo o resto falhar.
const GUARD_TTL_MS = 30 * 60 * 1000;
// Ritmo do laço que instala/reinstala o vigia (e, enquanto ele não pegou,
// mantém o snapshot rápido que é a via de reserva do desktop).
const WATCH_LOOP_MS = 2000;
// Quanto esperamos a aba realmente abrir a faixa que mandamos tocar.
const ARRIVAL_GRACE_MS = 30000;

// { tabId, videoId, codigo, fase, armada, confirmado, desde }
let watchState = null;

// A ABA DA FILA: onde a fila está tocando. Ela nasce no primeiro play da fila,
// vive enquanto a fila viver (o `stopWatch` a esquece) e é ELA — não o alvo que
// vem do servidor — que decide para onde vai a próxima faixa.
//
// Existe porque o alvo do servidor é um chute: o `event_subscribe` o adivinha
// pelo snapshot de ANTES de a fila começar, e no primeiro despacho ele vem vazio
// (log do desktop, 12/09 15:39:38: `watch armado: aba None`). Quando a faixa
// seguinte chegava com aquele id velho, a fila pulava para outra guia e deixava
// a anterior pausada na guia em que o usuário estava ouvindo.
let abaDaFila = null;

// Chamado em todo ponto onde alguém DESCOBRE a aba: o play que acabou de navegar,
// o vigia que mandou evento de dentro da página, a busca pela faixa.
function fixarAbaDaFila(tabId) {
  if (tabId == null) return;
  if (abaDaFila !== tabId) console.log("[neo-music] aba da fila:", tabId);
  abaDaFila = tabId;
  if (watchState) watchState.tabId = tabId;
}
let watchTimer = null;
// `codigo:evento:faixa` já enviados. O vigia da página já manda um de cada; isto
// é cinto de segurança para reinstalação e para o laço do worker.
let eventosEnviados = new Set();
// Eventos que chegaram com o socket fechado (worker recém-acordado): saem no
// `onopen`. Sem isto, o aviso que ACORDOU o worker seria justamente o perdido.
let eventosPendentes = [];

// O vigia, tal como roda DENTRO da página (mundo ISOLATED: precisa do <video> e
// do `chrome.runtime`, não dos globais do YouTube). Autocontido — o
// `executeScript` serializa a função, então tudo entra por `cfg`.
function vigiaDeFaixa(cfg) {
  const idDaUrl = () => {
    try {
      const u = new URL(location.href);
      const s = u.pathname.startsWith("/shorts/")
        ? u.pathname.slice("/shorts/".length).split("/")[0]
        : "";
      return u.searchParams.get("v") || s || "";
    } catch {
      return "";
    }
  };

  try {
    // Documento errado: o `watch` chega ANTES do `play` (o desktop re-arma antes
    // de mandar tocar), então na primeira tentativa a aba ainda mostra a faixa
    // anterior. Armar aqui seria armar num documento prestes a ser substituído.
    const naUrl = idDaUrl();
    if (naUrl !== cfg.videoId) return "outra-faixa:" + (naUrl || "-");

    const velha = window.__neoVigia;
    if (velha && velha.codigo === cfg.codigo) return "ja-armada";
    if (velha && typeof velha.desarmar === "function") velha.desarmar();

    const g = {
      codigo: cfg.codigo,
      videoId: cfg.videoId,
      fase: "armada",
      nascida: Date.now(),
      batida: Date.now(),
      // `pausou` é a trava de UMA pausa por aproximação do fim (resetada quando o
      // playhead volta para longe). `travou` é definitivo: já seguramos o fim
      // desta faixa, o serviço está prestado.
      pausou: false,
      travou: false,
      estacionou: 0,
      // O switch de autoplay do próprio YouTube (segunda trava, a pedido do
      // Daniel). `tratado` = já decidimos uma vez; `desligamos` = fomos NÓS que
      // desligamos, então é nosso dever religar ao soltar.
      autonav: { tratado: false, desligamos: false },
      // "a fila acabou, mas esta faixa ainda é nossa": para de reportar e de
      // cutucar o worker, e fica só com a TRAVA até o fim desta faixa.
      solta: false,
      morta: false,
      timer: null,
      video: null,
    };

    // O MESMO critério da sonda e do controle: quem tem DURAÇÃO. O primeiro
    // <video> do DOM numa página do YouTube pode ser a prévia muda do feed —
    // vigiar ela é vigiar um vídeo que nunca toca.
    const achaVideo = () => {
      const vs = Array.prototype.slice.call(document.querySelectorAll("video"));
      return (
        vs.find((v) => v.duration > 0 && !v.paused) ||
        vs.find((v) => v.duration > 0) ||
        null
      );
    };

    const vivo = () => {
      try {
        return !!(chrome && chrome.runtime && chrome.runtime.id);
      } catch {
        return false;
      }
    };

    const desarmar = () => {
      g.morta = true;
      // Se fomos nós que desligamos o autoplay, religamos: a preferência é DELE.
      // Deixar desligado seria rastro nosso na conta do usuário depois que a
      // fila acabou — o mesmo motivo pelo qual isto não é a trava principal.
      if (g.autonav && g.autonav.desligamos) {
        g.autonav.desligamos = false;
        try {
          const botao = document.querySelector(".ytp-autonav-toggle-button");
          if (botao && botao.getAttribute("aria-checked") === "false") botao.click();
        } catch {
          /* documento indo embora: nada a restaurar */
        }
      }
      try {
        clearInterval(g.timer);
      } catch {
        /* nada a limpar */
      }
      try {
        if (g.video) {
          g.video.removeEventListener("timeupdate", passo);
          g.video.removeEventListener("ended", passo);
        }
      } catch {
        /* elemento já foi embora com o documento */
      }
      g.video = null;
      if (window.__neoVigia === g) delete window.__neoVigia;
    };

    // A fila acabou (`unwatch`), mas ESTA faixa foi o Neo que mandou tocar: quem
    // termina ela não pode ser o autoplay do YouTube. Some tudo que é de fila
    // (eventos, batida) e sobra a trava, até esta faixa acabar — aí o vigia
    // morre sozinho. Faixa diferente não é problema nosso: some na hora.
    //
    // Isto NÃO é preciosismo: o `faixa_terminando` sai 6s antes do fim, e quando
    // a faixa é a ÚLTIMA da fila o neo-core responde `desarmar` na mesma hora.
    // Soltar a trava ali devolvia o fim da música para o YouTube — que emendava
    // um vídeo aleatório. Acontecia no fim de TODA fila (medido 2026-09-09,
    // "Erase My Scars": watch armado 02:55:45, desarmado 02:56:27, autoplay em
    // seguida).
    const soltar = () => {
      if (g.morta) return;
      // Já seguramos o fim desta faixa: não há mais nada a fazer, some agora.
      // (`pausou` não serve aqui: ele volta a false no tique seguinte ao recuo,
      // e a vigia ficaria pendurada até o TTL, com um timer por segundo na
      // página do usuário, sem nenhuma função.)
      if (g.travou) return desarmar();
      g.solta = true;
    };

    const avisar = (evento, videoId) => {
      // Solta = calada. Um evento agora falaria de uma fila que já acabou.
      if (g.solta) return;
      if (!vivo()) return desarmar();
      try {
        chrome.runtime.sendMessage(
          {
            type: "neo-vigia",
            evento,
            videoId: videoId || g.videoId,
            codigo: g.codigo,
          },
          (resp) => {
            // Worker dormindo/ocupado: ler o lastError é o que evita o barulho
            // de "message port closed" no console.
            void chrome.runtime.lastError;
            // O worker já está em OUTRO despacho: este vigia é passado.
            if (resp && resp.desarmar) desarmar();
          }
        );
      } catch {
        desarmar();
      }
    };

    const passo = () => {
      if (g.morta) return;
      // A extensão foi recarregada/atualizada: este vigia virou órfão. Sumir é
      // obrigatório — senão o vídeo do usuário passaria a pausar sozinho no fim,
      // para sempre, fora de qualquer fila.
      if (!vivo()) return desarmar();
      if (Date.now() - g.nascida > cfg.ttlMs) return desarmar();

      const atual = idDaUrl();
      if (atual !== g.videoId) {
        // Trocou de vídeo sem ser por nós (o usuário navegou, ou o autoplay
        // escapou). Reportamos o que ESTÁ tocando: o neo-core vê que não é a
        // faixa esperada e PAUSA a fila em vez de brigar pelo player. Sem vídeo
        // nenhum (home, busca) calamos — "terminou" ali seria lido como "pode
        // mandar a próxima".
        if (atual && g.fase !== "terminou") {
          g.fase = "terminou";
          avisar("faixa_terminou", atual);
        }
        return desarmar();
      }

      // A batida vem ANTES de qualquer outro `return`: comercial longo, vídeo
      // sem metadados e faixa pausada também precisam manter o worker acordado —
      // é o socket dele que segura a vigia do lado do desktop.
      if (!g.solta && Date.now() - g.batida > cfg.batidaMs) {
        g.batida = Date.now();
        avisar("batida");
      }

      const v = achaVideo();
      if (v !== g.video) {
        if (g.video) {
          g.video.removeEventListener("timeupdate", passo);
          g.video.removeEventListener("ended", passo);
        }
        g.video = v;
        // O relógio de verdade. Um listener por elemento, trocado junto com ele:
        // o YouTube reaproveita o mesmo <video> entre faixas, e listener
        // duplicado aqui viraria evento duplicado lá.
        if (v) {
          v.addEventListener("timeupdate", passo);
          v.addEventListener("ended", passo);
        }
      }
      if (!v) return;

      // Segunda trava: o switch de autoplay do próprio YouTube. Ele NÃO cobre o
      // caso que mais quebra (em mix/rádio o botão nem existe) e por isso não
      // substitui a pausa — mas quando existe, desligar é de graça e evita a
      // corrida no fim da faixa. Uma decisão só por vigia: se o usuário religar
      // no meio, ele ganha, não ficamos brigando com a UI dele.
      if (!g.autonav.tratado && !g.solta) {
        const botao = document.querySelector(".ytp-autonav-toggle-button");
        if (botao) {
          g.autonav.tratado = true;
          if (botao.getAttribute("aria-checked") === "true") {
            try {
              botao.click();
              g.autonav.desligamos = true;
            } catch {
              /* UI recusou o clique: sobra a pausa, que é a trava de verdade */
            }
          }
        }
      }

      // Comercial: o <video> é o do anúncio, com OUTRA duração. Sem esta guarda
      // pausaríamos o fim do comercial (a faixa nunca começaria) e pediríamos a
      // próxima antes de a música tocar um segundo.
      const player = document.querySelector("#movie_player");
      if (player && player.classList.contains("ad-showing")) return;

      if (g.fase === "armada" && !v.paused && v.currentTime > 0) {
        g.fase = "tocando";
        avisar("faixa_iniciou");
      }

      const d = v.duration;
      if (!d || !isFinite(d)) return; // ao vivo, ou metadados ainda não vieram
      const falta = d - v.currentTime;

      if (falta <= cfg.fimS && (g.fase === "armada" || g.fase === "tocando")) {
        g.fase = "terminando";
        avisar("faixa_terminando");
      }

      // A TRAVA, em dois tempos.
      //
      // 1) PAUSE, para o som parar na hora certa.
      // 2) RECUAR o playhead alguns segundos. Só pausar não basta: o player do
      //    YouTube tem detecção de fim PRÓPRIA e, com o <video> parado a 0,88s
      //    do fim, ele declarou `getPlayerState()===0` (ENDED) e emendou o
      //    próximo vídeo mesmo assim (medido no navegador, 2026-09-08). Longe do
      //    fim ele volta para 2 (PAUSED) e não tem mais o que emendar — nem o
      //    autonav, nem o mix/rádio. Já estamos pausados: recuar não faz som.
      //
      // Estacionamos no máximo duas vezes: se o usuário insistir em tocar o
      // último segundo, quem ganha é ele (e o `ended` abaixo conta a história),
      // em vez de ficar preso num laço de 4s que ele não consegue vencer.
      if (falta > cfg.pausaS) {
        g.pausou = false;
      } else if (!g.pausou && !v.paused && !v.ended) {
        g.pausou = true;
        g.travou = true;
        try {
          v.pause();
        } catch {
          /* player recusou: o `ended` abaixo ainda reporta */
        }
        if (g.estacionou < 2) {
          g.estacionou += 1;
          try {
            v.currentTime = Math.max(0, d > cfg.recuoS ? d - cfg.recuoS : d / 2);
          } catch {
            /* seek recusado: fica só a pausa */
          }
        }
        // Vigia solto já fez o que tinha para fazer: segurou o fim da última
        // faixa. Some agora, sem esperar TTL nenhum.
        if (g.solta) return desarmar();
      }

      if (v.ended && g.fase !== "terminou") {
        g.fase = "terminou";
        avisar("faixa_terminou");
        return desarmar();
      }

    };

    g.desarmar = desarmar;
    g.soltar = soltar;
    g.timer = setInterval(passo, cfg.tickMs);
    window.__neoVigia = g;
    passo();
    return "armada";
  } catch (e) {
    return "erro:" + (e && e.message ? e.message : String(e));
  }
}

async function instalarVigia(tabId) {
  if (!watchState) return "sem-vigia";
  try {
    const res = await chrome.scripting.executeScript({
      target: { tabId },
      world: "ISOLATED",
      func: vigiaDeFaixa,
      args: [
        {
          codigo: watchState.codigo,
          videoId: watchState.videoId,
          pausaS: PAUSE_BEFORE_END_S,
          recuoS: PARK_BEFORE_END_S,
          fimS: ENDING_WINDOW_S,
          tickMs: GUARD_TICK_MS,
          batidaMs: GUARD_HEARTBEAT_MS,
          ttlMs: GUARD_TTL_MS,
        },
      ],
    });
    return (res && res[0] && res[0].result) || "sem-resposta";
  } catch (e) {
    return "erro:" + (e && e.message ? e.message : String(e));
  }
}

// Tira o vigia de uma aba. Dois modos, e a diferença é o fim da faixa ATUAL:
//
//   `solta` (fim de fila / `unwatch`) — para de reportar, mas segura a trava até
//     esta faixa acabar. É o certo porque o `faixa_terminando` sai 6s antes do
//     fim: na última faixa da fila o neo-core responde `desarmar` ali, e largar
//     a trava nesse instante devolvia o fim da música ao autoplay do YouTube.
//   duro (default) — some agora. Para vigia de faixa passada, órfão, ou aba que
//     já trocou de vídeo: nesses casos não há faixa nossa tocando para proteger.
async function desarmarVigia(tabId, { solta = false } = {}) {
  if (tabId == null) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "ISOLATED",
      args: [solta],
      func: (soltar) => {
        const g = window.__neoVigia;
        if (!g) return "sem-vigia";
        if (soltar && typeof g.soltar === "function") {
          g.soltar();
          return "solta";
        }
        if (typeof g.desarmar === "function") g.desarmar();
        return "desarmada";
      },
    });
  } catch {
    /* aba fechada ou sem permissão: não há o que soltar */
  }
}

// Sem saber a aba (o worker pode ter sido reciclado desde o `watch`), soltamos o
// vigia de TODAS as abas do YouTube: onde não há vigia a injeção é no-op, e
// deixar um pendurado é o pior desfecho possível — vídeo do usuário pausando
// sozinho no fim, fora de qualquer fila.
async function desarmarTodas(opcoes) {
  try {
    const tabs = await chrome.tabs.query({ url: "*://*.youtube.com/*" });
    await Promise.all(tabs.map((t) => desarmarVigia(t.id, opcoes)));
  } catch {
    /* sem permissão de tabs: nada a fazer */
  }
}

function startWatch(tabId, videoId, codigo) {
  const anterior = watchState;
  let alvo = typeof tabId === "number" ? tabId : null;
  // A FILA NÃO TROCA DE GUIA NO MEIO: se ela já tem aba, é nela. O id que vem
  // no `watch` só vale para começar uma fila — depois disso quem sabe a aba é
  // este lado, que viu a música cair lá.
  if (abaDaFila != null && alvo !== abaDaFila) {
    if (alvo != null) {
      console.warn(
        "[neo-music] fila continua na aba",
        abaDaFila,
        "— o desktop mandou",
        alvo
      );
    }
    alvo = abaDaFila;
  }
  abaDaFila = alvo;
  // Re-armou o MESMO despacho: não reiniciamos nada — o vigia da página é
  // idempotente e continua valendo.
  if (anterior && anterior.codigo === codigo && anterior.videoId === videoId) {
    if (alvo != null) anterior.tabId = alvo;
    return;
  }
  // Fila mudou de aba: solta o vigia velho antes de armar o novo.
  if (anterior && anterior.tabId != null && anterior.tabId !== alvo) {
    desarmarVigia(anterior.tabId);
  }
  watchState = {
    tabId: alvo,
    videoId: videoId || "",
    codigo: codigo || "",
    fase: "armada",
    armada: false,
    // Já vimos a NOSSA faixa nesta aba? Enquanto não, um vídeo diferente ali
    // significa "a navegação ainda não chegou", não "o usuário trocou". Sem esta
    // distinção a vigia se auto-sabota: ela é armada no mesmo instante em que a
    // aba ainda mostra a música anterior, reporta "faixa inesperada" e a fila
    // pausa sozinha no primeiro segundo (visto em produção, 2026-09-08).
    confirmado: false,
    desde: Date.now(),
  };
  eventosEnviados = new Set();
  setBadge(wsOpen());
  if (watchTimer) clearInterval(watchTimer);
  watchTimer = setInterval(manterVigia, WATCH_LOOP_MS);
  manterVigia();
}

// `solta` = a fila acabou por decisão do neo-core (ou o usuário mandou parar):
// a vigia sai, mas o fim da faixa que ESTÁ tocando continua travado (ver
// `desarmarVigia`). Sem isso, toda última faixa de fila terminava em autoplay.
function stopWatch({ solta = false } = {}) {
  const alvo = watchState ? watchState.tabId : null;
  watchState = null;
  // A fila acabou (ou o usuário mandou parar): a próxima fila escolhe a aba dela
  // de novo. Segurar este id manteria a fila de amanhã presa na guia de hoje.
  abaDaFila = null;
  if (watchTimer) clearInterval(watchTimer);
  watchTimer = null;
  // `eventosEnviados` NÃO é limpo aqui de propósito: a troca de vídeo é vista
  // pelos dois lados (o vigia da página e o `vigiarNavegacao` do worker), e
  // quem chega primeiro chama este `stopWatch`. Limpar a marca agora deixaria o
  // segundo passar como se fosse novo — dois `faixa_terminou` para a mesma
  // faixa. A marca leva o código do despacho, então a próxima vigia não esbarra
  // nela; quem limpa é o `startWatch`.
  if (alvo != null) desarmarVigia(alvo, { solta });
  else desarmarTodas({ solta });
  setBadge(wsOpen());
}

// Acha a aba que a vigia deve olhar.
//
// Regra: se o `tabId` que veio com a vigia ainda existe, é ELE — mesmo que o
// vídeo de lá seja outro, porque "trocou de vídeo na minha aba" é justamente o
// sinal que o neo-core precisa receber (ele pausa a fila). A busca pelo vídeo é
// para o caso em que não temos aba: o `open_url` abre uma aba NOVA e o id que
// chegou junto com a vigia aponta para a aba velha.
async function findWatchTab() {
  if (!watchState) return null;
  if (watchState.tabId != null) {
    try {
      return await chrome.tabs.get(watchState.tabId);
    } catch {
      /* aba fechada — procura pelo vídeo abaixo */
    }
  }
  if (!watchState.videoId) return null;
  try {
    const tabs = await chrome.tabs.query({});
    const alvo = tabs.find(
      (t) => t.url && idsFromUrl(t.url).videoId === watchState.videoId
    );
    if (alvo) {
      fixarAbaDaFila(alvo.id);
      return alvo;
    }
  } catch {
    /* sem permissão de tabs: nada a fazer */
  }
  return null;
}

// O laço do worker. Ele NÃO decide nada sobre a faixa — só garante que o vigia
// da página exista (a instalação é idempotente e barata) e, enquanto ele não
// pegou, mantém o snapshot rápido que é a via de reserva do desktop.
async function manterVigia() {
  if (!watchState) {
    if (watchTimer) clearInterval(watchTimer);
    watchTimer = null;
    return;
  }
  const tab = await findWatchTab();
  const expirou = Date.now() - watchState.desde > ARRIVAL_GRACE_MS;
  if (!tab) {
    // Sem aba: ou a navegação ainda não chegou (esperamos o prazo), ou a aba foi
    // fechada de vez. Nos dois casos paramos CALADOS — inventar "terminou" aqui
    // faria a fila pular uma faixa que nunca tocou; a inscrição expira sozinha
    // no servidor.
    if (watchState.confirmado || expirou) stopWatch();
    return;
  }
  fixarAbaDaFila(tab.id);
  const atual = idsFromUrl(tab.url || "").videoId || "";
  if (!watchState.videoId || atual !== watchState.videoId) {
    // A aba ainda mostra a faixa anterior (o `watch` chega antes do `play`).
    if (expirou) stopWatch();
    else if (!watchState.confirmado) reportTabs();
    return;
  }
  watchState.confirmado = true;

  const status = await instalarVigia(tab.id);
  const armada = status === "armada" || status === "ja-armada";
  if (armada && !watchState.armada) {
    console.log(
      "[neo-music] vigia instalada na aba",
      tab.id,
      watchState.videoId,
      "código",
      watchState.codigo
    );
  }
  if (!armada && watchState.armada) {
    console.warn("[neo-music] vigia caiu, reinstalando:", status);
  }
  watchState.armada = armada;
  // Enquanto o vigia não pega (aba bloqueada, player que não montou), a fila não
  // fica órfã: sem `media_event` o desktop continua deduzindo o fim pelo
  // snapshot — que é o caminho antigo, e precisa de relatório frequente.
  if (!armada) reportTabs();
}

// O que o vigia da página manda de volta. É AQUI que o `codigo` fecha o
// círculo: ele veio no `watch`, ficou guardado na página e volta em todo aviso —
// mesmo que este worker tenha sido reciclado no meio e não lembre de nada.
function receberDaVigia(msg, sender) {
  const codigo = msg && msg.codigo ? String(msg.codigo) : "";
  const evento = msg && msg.evento ? String(msg.evento) : "";
  const tabId = sender && sender.tab ? sender.tab.id : null;

  // Vigia de uma faixa VELHA (o worker já está em outro despacho): mandamos ela
  // sumir em vez de deixar um evento fora de hora mexer na fila.
  if (watchState && codigo && watchState.codigo !== codigo) {
    return { ok: false, desarmar: true };
  }
  if (evento === "batida") {
    // Só ter chegado já fez o trabalho: o worker acordou. Se o socket tinha
    // caído, reconecta.
    if (!wsOpen()) connect();
    return { ok: true };
  }
  if (!evento) return { ok: false };

  if (watchState) {
    // O vigia mora na página: o `sender.tab.id` dele é a aba onde a música está
    // tocando de verdade. Não há informante melhor.
    if (tabId != null) fixarAbaDaFila(tabId);
    watchState.armada = true;
    watchState.confirmado = true;
    watchState.fase =
      evento === "faixa_iniciou"
        ? "tocando"
        : evento === "faixa_terminando"
          ? "terminando"
          : "terminou";
    setBadge(wsOpen());
  }
  enviarMediaEvent(evento, msg.videoId || "", codigo, tabId);
  return { ok: true };
}

function enviarMediaEvent(evento, videoId, codigo, tabId) {
  const marca = codigo + ":" + evento + ":" + videoId;
  if (eventosEnviados.has(marca)) return;
  eventosEnviados.add(marca);
  const payload = {
    type: "media_event",
    event: evento,
    videoId,
    codigo,
    tabId,
  };
  console.log("[neo-music] media_event", evento, videoId, "código", codigo);
  if (wsOpen()) {
    ws.send(JSON.stringify(payload));
    return;
  }
  // Socket fechado (worker recém-acordado pelo próprio aviso): guarda e manda no
  // `onopen`. Perder justamente o evento que acordou o worker seria o pior caso.
  eventosPendentes.push({ payload, em: Date.now() });
  if (eventosPendentes.length > 8) eventosPendentes.shift();
  connect();
}

function drenarPendentes() {
  const fila = eventosPendentes;
  eventosPendentes = [];
  for (const p of fila) {
    // Velho demais: a fila do outro lado já seguiu, e um "está acabando"
    // atrasado pularia uma faixa.
    if (Date.now() - p.em > 15000) continue;
    try {
      ws.send(JSON.stringify(p.payload));
    } catch {
      /* socket caiu de novo: o próximo aviso do vigia refaz */
    }
  }
}

// Além do vigia da página, o worker olha a navegação da aba observada: quando o
// DOCUMENTO é trocado (recarregar, voltar, um link que não é SPA) o vigia morre
// junto e não teria como avisar. Só vale depois de a nossa faixa ter aparecido
// ali — antes disso, "outro vídeo nesta aba" é a faixa anterior, não uma troca.
function vigiarNavegacao(tabId, changeInfo) {
  if (!watchState || tabId !== watchState.tabId) return;
  if (!changeInfo || !changeInfo.url) return;
  const atual = idsFromUrl(changeInfo.url).videoId || "";
  if (watchState.confirmado && atual && atual !== watchState.videoId) {
    watchState.fase = "terminou";
    enviarMediaEvent("faixa_terminou", atual, watchState.codigo, tabId);
    stopWatch();
    return;
  }
  // Mesma faixa, documento novo: reinstala o vigia na hora, sem esperar o laço.
  manterVigia();
}
// `tabId` volta de propósito: só a extensão sabe em que aba o play realmente
// caiu (pode ter sido uma aba nova). É por este campo que o desktop corrige o
// alvo da vigia — e, pelo evento seguinte, o neo-core corrige o da inscrição.
function reply(id, success, error, tabId = null) {
  if (wsOpen()) {
    ws.send(
      JSON.stringify({ type: "result", id, success, error: error || "", tabId })
    );
  }
}

// Pick the tab to act on, em três degraus — e a ORDEM é o conserto de 12/09.
//
//   1. **A aba da fila**, quando há fila tocando. Ela ganha até do `tabId` que
//      vem do servidor: o alvo gravado na inscrição pode ser de ANTES de a fila
//      começar (o `event_subscribe` o adivinha pelo snapshot do turno), e era
//      esse id velho que jogava a faixa seguinte em outra guia — deixando a
//      anterior pausada na guia onde o usuário estava ouvindo.
//   2. **O pin do neo-core** (`tabId`): ele decidiu o reuso sobre o snapshot que
//      nós reportamos, então numa tocada avulsa a escolha é dele. Re-escolher
//      aqui por conta própria já foi bug: com duas abas do YouTube abertas, o
//      `find` caía numa aba diferente daquela sobre a qual a decisão foi tomada.
//   3. **A primeira aba do YouTube** — só para tocada avulsa sem pin. Fila NUNCA
//      chega aqui: ela abre a sua própria aba em vez de tomar uma que ninguém
//      escolheu (é a mesma regra do `pick_reuse_tab` no neo-core, que se recusa
//      a tomar aba de vídeo do usuário).
async function resolveYoutubeTab(tabId, { fila = false, novaAba = false } = {}) {
  if (fila && watchState) {
    const daFila = await findWatchTab();
    if (daFila) {
      if (tabId != null && tabId !== daFila.id) {
        console.warn(
          "[neo-music] fila fica na aba",
          daFila.id,
          "— o desktop apontou",
          tabId
        );
      }
      return daFila;
    }
  }
  if (tabId !== null && tabId !== undefined) {
    try {
      const pinned = await chrome.tabs.get(tabId);
      if (pinned && pinned.url && /youtube\.com/.test(pinned.url)) return pinned;
    } catch {
      /* aba fechada/navegada desde o snapshot — cai no fallback */
    }
  }
  if (fila || novaAba) return null;
  const tabs = await chrome.tabs.query({});
  return tabs.find((t) => t.url && /youtube\.com/.test(t.url)) || null;
}

// Play a videoId: reuse an open YouTube tab (navigate it) or open a new one.
// Returns { reused, tabId }; throws on failure. Shared by the desktop command
// (handleCommand) and the popup self-test (testPlay) so both exercise the SAME path.
//
// `fila` = este play é de uma fila (há vigia armada): a aba é a da fila, e ela
// fica GRUDADA aqui — é o id que o vigia observa, que volta no `result` para o
// desktop e, por ele, chega ao neo-core. `novaAba` = o neo-core decidiu não
// reusar nada (não achou aba sem dono): abrir a nossa é melhor do que tomar a do
// usuário.
async function playYoutube(videoId, activate = true, tabId = null, opcoes = {}) {
  const { fila = false, novaAba = false } = opcoes;
  const url = "https://www.youtube.com/watch?v=" + encodeURIComponent(videoId);
  const yt = await resolveYoutubeTab(tabId, { fila, novaAba });
  let alvo;
  if (yt) {
    // Focus is desired for the real play (bring music to front). But the popup
    // self-test must NOT steal focus — doing so closes the popup before it reads
    // the response ("message port closed"). So the test navigates in background.
    await chrome.tabs.update(yt.id, activate ? { active: true, url } : { url });
    if (activate && yt.windowId != null) {
      await chrome.windows.update(yt.windowId, { focused: true });
    }
    alvo = { reused: true, tabId: yt.id };
  } else {
    const nova = await chrome.tabs.create({ active: activate, url });
    alvo = { reused: false, tabId: nova && nova.id != null ? nova.id : null };
  }
  if (fila && watchState && alvo.tabId != null && watchState.tabId !== alvo.tabId) {
    // A fila mudou de aba de verdade (a anterior foi fechada, ou esta é a
    // primeira faixa e a aba acabou de nascer): o vigia velho sai, e daqui para
    // frente esta é A aba da fila.
    if (watchState.tabId != null) desarmarVigia(watchState.tabId);
  }
  if (fila) fixarAbaDaFila(alvo.tabId);
  return alvo;
}

// Roda DENTRO da página, como a sonda: autocontida (o executeScript serializa) e
// ASSÍNCRONA de propósito — o player leva alguns ms para aplicar, e é a
// CONFERÊNCIA depois de aplicar que separa "mandei" de "aconteceu".
//
// Duas correções em cima da versão que dizia sucesso sem fazer nada (sessão
// 7687b737, "muta essa aba" → `media_command success=True` e o som continuou):
//   1. escolhe o MESMO <video> que a sonda escolhe (o que tem DURAÇÃO). O
//      `querySelector("video")` antigo pegava o PRIMEIRO do DOM, que numa
//      página do YouTube pode ser a prévia muda — mutar ela é um no-op perfeito;
//   2. usa a API do player (`#movie_player`) quando existe. Ela é quem manda no
//      som de verdade, sincroniza o slider da UI e — o que o `.ytp-*-button` não
//      fazia — funciona também no YouTube Music, que tem outra barra de controle.
async function applyMediaAction(action) {
  const espera = (ms) => new Promise((r) => setTimeout(r, ms));
  // A API só existe no mundo MAIN; no ISOLATED sobra o <video>, que ainda
  // resolve pause/volume/mudo.
  const el = document.querySelector("#movie_player");
  const api = el && typeof el.getPlayerState === "function" ? el : null;
  const vids = Array.from(document.querySelectorAll("video"));
  const v =
    vids.find((x) => x.duration > 0 && !x.paused) ||
    vids.find((x) => x.duration > 0) ||
    null;
  if (!v && !api) return { ok: false, error: "não achei o player na aba" };

  const mudo = () =>
    api && typeof api.isMuted === "function" ? !!api.isMuted() : v ? !!v.muted : null;
  const volume = () =>
    api && typeof api.getVolume === "function"
      ? Math.round(api.getVolume())
      : v
        ? Math.round((v.volume || 0) * 100)
        : null;
  const pausado = () => (v ? !!v.paused : null);
  const estado = (ok, extra) => ({
    ok,
    action,
    muted: mudo(),
    paused: pausado(),
    volume: volume(),
    source: api ? "player-api" : "element",
    ...(extra || {}),
  });

  switch (action) {
    case "pause":
    case "stop": {
      if (api && typeof api.pauseVideo === "function") api.pauseVideo();
      else v.pause();
      await espera(150);
      return estado(pausado() === true);
    }
    case "resume": {
      if (api && typeof api.playVideo === "function") api.playVideo();
      else v.play();
      await espera(150);
      return estado(pausado() === false);
    }
    case "mute": {
      // Alterna, e diz qual dos dois aconteceu — o lado do Neo usa isto para
      // não anunciar "Mutei" quando na verdade desmutou.
      const antes = mudo();
      const alvo = !antes;
      if (api && typeof api.mute === "function" && typeof api.unMute === "function") {
        if (alvo) api.mute();
        else api.unMute();
      }
      // O elemento vai junto: é ele que faz som. Mexer só na API deixaria o
      // caso "ícone mudo na UI, áudio saindo".
      if (v) v.muted = alvo;
      await espera(150);
      return estado(mudo() === alvo, { era: antes, virou: alvo });
    }
    case "volume_up":
    case "volume_down": {
      const passo = action === "volume_up" ? 20 : -20;
      const antes = volume();
      const alvo = Math.max(0, Math.min(100, (antes === null ? 0 : antes) + passo));
      if (passo > 0) {
        // Subir volume no mudo não faz som nenhum.
        if (api && typeof api.unMute === "function") api.unMute();
        if (v) v.muted = false;
      }
      if (api && typeof api.setVolume === "function") api.setVolume(alvo);
      if (v) v.volume = alvo / 100;
      await espera(150);
      const agora = volume();
      // Tolerância: a API guarda inteiro, o elemento guarda float.
      return estado(agora !== null && Math.abs(agora - alvo) <= 2, { de: antes, para: alvo });
    }
    case "next":
    case "previous": {
      const metodo = action === "next" ? "nextVideo" : "previousVideo";
      if (api && typeof api[metodo] === "function") {
        api[metodo]();
        return estado(true, { via: "player-api" });
      }
      // Fallback por botão. `.ytp-*` é o player padrão do youtube.com; os dois
      // seguintes são a barra do YouTube Music, onde o `.ytp-` não existe.
      const seletores =
        action === "next"
          ? [".ytp-next-button", "tp-yt-paper-icon-button.next-button", ".next-button"]
          : [".ytp-prev-button", "tp-yt-paper-icon-button.previous-button", ".previous-button"];
      for (const sel of seletores) {
        const b = document.querySelector(sel);
        if (b) {
          b.click();
          return estado(true, { via: sel });
        }
      }
      return { ok: false, error: "não achei o botão de " + action + " nesta página" };
    }
    default:
      return { ok: false, error: "ação desconhecida: " + action };
  }
}

// Executa a ação na aba escolhida. MAIN primeiro (é onde vive a API do player),
// ISOLATED só quando o navegador RECUSA a injeção no MAIN — os dois enxergam o
// mesmo DOM, então repetir depois de um "não confirmou" seria só perder tempo.
async function controlYoutube(action, tabId = null) {
  // Com fila tocando, "pausa/mudo/próxima" é sobre a aba DA FILA. O segundo
  // `resolve` é a rede: fila armada mas aba já fechada ainda pode controlar a
  // aba que existir (melhor que recusar).
  const yt =
    (await resolveYoutubeTab(tabId, { fila: !!watchState })) ||
    (await resolveYoutubeTab(tabId));
  if (!yt) throw new Error("nenhuma aba do YouTube aberta");
  let resultado = null;
  for (const world of ["MAIN", "ISOLATED"]) {
    try {
      const res = await chrome.scripting.executeScript({
        target: { tabId: yt.id },
        func: applyMediaAction,
        args: [action],
        world,
      });
      resultado = res && res[0] ? res[0].result : null;
      break;
    } catch (e) {
      if (world === "ISOLATED") {
        throw new Error(
          "não consegui injetar na aba: " + (e && e.message ? e.message : String(e))
        );
      }
    }
  }
  if (!resultado || resultado.ok !== true) {
    // Mentir aqui é o pior desfecho possível: o Neo anuncia "Mutei" e o som
    // continua. Sem confirmação, isto vira erro no round-trip.
    throw new Error(
      (resultado && resultado.error) || "o player não confirmou a ação"
    );
  }
  return { ...resultado, tabId: yt.id };
}

async function handleCommand(msg) {
  const { id, action, target, videoId, tabId } = msg;
  try {
    if (target !== "youtube") throw new Error("alvo não suportado");
    if (action === "play") {
      if (!videoId) throw new Error("videoId ausente");
      // Fila = existe vigia armada. O desktop arma o `watch` ANTES de mandar
      // tocar justamente para que este play saiba que é de fila e vá para a aba
      // dela. `novaAba` chega quando o neo-core não achou aba reusável.
      const fila = !!watchState;
      const r = await playYoutube(videoId, true, tabId, {
        fila,
        novaAba: msg.novaAba === true || msg.reuseTab === false,
      });
      reply(id, true, "", r.tabId);
      return;
    }
    const r = await controlYoutube(action, tabId);
    reply(id, true, "", r && r.tabId != null ? r.tabId : null);
  } catch (e) {
    reply(id, false, e && e.message ? e.message : String(e));
  }
}

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (!extToken) await carregarToken();
  if (!extToken) {
    // Sem credencial não adianta tentar: o handshake seria recusado e o
    // backoff esconderia o motivo real atrás de "desconectado".
    ultimoMotivo = "sem_token";
    console.warn(
      "[neo-music] sem token da ponte — abra o popup e cole o token que aparece " +
        "na aba Dispositivos do Neo Desktop",
    );
    setBadge(false);
    scheduleReconnect();
    return;
  }
  try {
    ws = new WebSocket(WS_BASE + "?token=" + encodeURIComponent(extToken));
  } catch (e) {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    // A VERSÃO no log de conexão: o deploy desta extensão é manual (recarregar
    // em chrome://extensions), e sem isto não há como saber, olhando o console,
    // se o navegador está com o código novo ou com o de duas semanas atrás.
    let versao = "?";
    try {
      versao = chrome.runtime.getManifest().version;
    } catch {
      /* manifest indisponível: o log vale do mesmo jeito */
    }
    console.log("[neo-music] connected to Neo Desktop — extensão v" + versao);
    ultimoMotivo = null;
    reconnectDelay = RECONNECT_MIN_MS;
    setBadge(true);
    // Primeiro os avisos que ficaram na mão (ver `enviarMediaEvent`): a fila
    // está esperando por eles, o snapshot não.
    drenarPendentes();
    reportTabs();
  };
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (msg && msg.type === "command") handleCommand(msg);
    else if (msg && msg.type === "watch") startWatch(msg.tabId, msg.videoId, msg.codigo);
    else if (msg && msg.type === "unwatch") stopWatch({ solta: true });
    else if (msg && msg.type === "pair_code") redimirCodigo(msg);
    else if (msg && msg.type === "pair_denied") {
      SURF.ultimoErro = msg.reason || "pareamento não autorizado";
      console.warn("[neo-surface] pareamento recusado:", SURF.ultimoErro);
    }
  };
  ws.onclose = (ev) => {
    ws = null;
    setBadge(false);
    // Handshake recusado chega como close anormal SEM ter aberto. O navegador não
    // entrega o status HTTP, então o mais honesto que dá para dizer é "o token
    // pode estar errado" — e só quando nunca abriu.
    if (ultimoMotivo !== "sem_token" && ev && ev.code === 1006) {
      ultimoMotivo = "recusado";
    }
    scheduleReconnect();
  };
  ws.onerror = () => {
    try {
      ws.close();
    } catch {
      /* noop */
    }
  };
}

// ===========================================================================
// SUPERFÍCIE DO NEO-CORE (neo-core/docs/browser-surface-plan.md)
// ===========================================================================
//
// Além de ser a ponte do desktop, esta extensão pode ser um **dispositivo
// pareado do neo-core**: um segundo canal, direto, que continua de pé com o
// Neo Desktop FECHADO.
//
// Por que isso importa: hoje, pedido que não vem do desktop (painel, celular)
// cai no navegador do SERVIDOR — IP de datacenter, que loja brasileira bloqueia.
// Com esta conexão, o core sabe quais abas estão abertas aqui e pode dirigir
// este navegador de qualquer canal.
//
// O que declaramos saber fazer, hoje, é só `tabs` — ler abas. Ação de navegação
// (`click`, `fill`, …) é o vocabulário `browser.*`, que continua sendo executado
// pelo Neo Desktop; o core sabe disso pela declaração e recusa com mensagem
// própria em vez de tentar e falhar. Ver §13.3 do plano.
//
// O pareamento é de UM CLIQUE e o consentimento é um DIÁLOGO NATIVO do desktop:
// pedimos pela ponte (`pair_request`), você autoriza na janela do app, e o
// desktop devolve um código estampado com a máquina dele (`pair_code`). Não
// existe endpoint local que emita credencial — se existisse, qualquer aba
// alcançaria.

const SURF = {
  ws: null,
  reconnectDelay: RECONNECT_MIN_MS,
  ultimoErro: null,
};

async function surfaceState() {
  try {
    const g = await chrome.storage.local.get([
      "neoSurfaceToken",
      "neoSurfaceDeviceId",
      "neoCoreUrl",
    ]);
    return {
      token: typeof g.neoSurfaceToken === "string" ? g.neoSurfaceToken : "",
      deviceId: typeof g.neoSurfaceDeviceId === "string" ? g.neoSurfaceDeviceId : "",
      // O core mora atrás do TÚNEL na máquina dele. Configurável desde o dia um
      // para que apontar para uma entrada pública (quando existir) seja config,
      // não reescrita.
      coreUrl: typeof g.neoCoreUrl === "string" && g.neoCoreUrl ? g.neoCoreUrl : "http://127.0.0.1:8000",
    };
  } catch {
    return { token: "", deviceId: "", coreUrl: "http://127.0.0.1:8000" };
  }
}

function nomeDoNavegador() {
  const ua = navigator.userAgent || "";
  if (/Edg\//.test(ua)) return "Edge";
  if (/OPR\//.test(ua)) return "Opera";
  if (/Brave/.test(ua)) return "Brave";
  if (/Firefox\//.test(ua)) return "Firefox";
  if (/Chrome\//.test(ua)) return "Chrome";
  return "Navegador";
}

/** Pede o pareamento ao desktop pela ponte. O desktop mostra o diálogo. */
function pedirPareamento() {
  if (!wsOpen()) {
    SURF.ultimoErro = "o Neo Desktop precisa estar aberto para autorizar";
    return false;
  }
  ws.send(JSON.stringify({ type: "pair_request", browser: nomeDoNavegador() }));
  SURF.ultimoErro = null;
  return true;
}

/** Troca o código por um token de device e guarda. */
async function redimirCodigo(msg) {
  const coreUrl = (msg.core_url || "").replace(/\/$/, "") || "http://127.0.0.1:8000";
  const { deviceId: existente } = await surfaceState();
  // Um device_id estável por perfil de navegador, como o desktop guarda o dele
  // em disco. Reusar o mesmo em re-pareamento ROTACIONA o token no core em vez
  // de criar uma linha nova a cada vez.
  const deviceId = existente || crypto.randomUUID();

  const corpo = {
    code: String(msg.code || ""),
    device_id: deviceId,
    device_type: "neo-browser-ext",
    model: nomeDoNavegador(),
    os_platform: navigator.platform || "unknown",
    os_version: "",
    client_version: chrome.runtime.getManifest().version,
    display_name: msg.sugestao_nome || nomeDoNavegador(),
    // O que esta superfície sabe fazer. Declarar MENOS é o erro barato: o core
    // trata quem não declara como a mais pobre, e prometer o que não se faz é o
    // modo de falha que a declaração existe para matar.
    capabilities: {
      browser_local: true,
      browser_headless: false,
      primitives: { tabs: true, eval: false, cdp: false },
    },
  };

  try {
    const res = await fetch(`${coreUrl}/api/v1/devices/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corpo),
    });
    if (!res.ok) {
      SURF.ultimoErro = `o neo-core recusou o pareamento (${res.status})`;
      return;
    }
    const data = await res.json();
    await chrome.storage.local.set({
      neoSurfaceToken: data.device_token || "",
      neoSurfaceDeviceId: data.device_id || deviceId,
      neoCoreUrl: coreUrl,
    });
    console.log(
      "[neo-surface] pareada como",
      data.display_name || corpo.display_name,
      "- host:",
      data.host_device_id || "(sem vínculo)",
    );
    SURF.ultimoErro = null;
    conectarSuperficie();
  } catch (e) {
    SURF.ultimoErro = `não alcancei o neo-core: ${e && e.message ? e.message : e}`;
  }
}

/** Abre (ou reabre) o canal de controle com o core. */
async function conectarSuperficie() {
  if (SURF.ws && (SURF.ws.readyState === WebSocket.OPEN || SURF.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const { token, deviceId, coreUrl } = await surfaceState();
  if (!token || !deviceId) return; // não pareada: silêncio, não erro

  const base = coreUrl.replace(/^http/, "ws").replace(/\/$/, "");
  const url =
    `${base}/api/v1/ws/devices/control/${encodeURIComponent(deviceId)}` +
    `?token=${encodeURIComponent(token)}`;
  try {
    SURF.ws = new WebSocket(url);
  } catch (e) {
    agendarReconexaoSuperficie();
    return;
  }

  SURF.ws.onopen = () => {
    console.log("[neo-surface] canal aberto com o neo-core");
    SURF.reconnectDelay = RECONNECT_MIN_MS;
    SURF.ultimoErro = null;
    // `hello` primeiro: é ele que diz ao core o que esta superfície sabe fazer.
    SURF.ws.send(
      JSON.stringify({
        type: "hello",
        surface: {
          provider: "extension",
          engine: nomeDoNavegador(),
          primitives: { tabs: true, eval: false, cdp: false, screenshot: false },
        },
      }),
    );
    empurrarAbas();
  };

  SURF.ws.onmessage = async (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    // O envelope do canal é {id, action, args}. Nada do vocabulário `browser.*`
    // é implementado aqui ainda: responder `ok:false` com o motivo é honesto e
    // é o que o core espera — ele já recusa antes de chegar aqui, pela
    // declaração, mas a rede de segurança vale.
    if (msg && msg.id) {
      SURF.ws.send(
        JSON.stringify({
          id: msg.id,
          ok: false,
          error:
            "esta superfície só reporta abas; ação de navegação precisa do Neo Desktop aberto nesta máquina",
        }),
      );
    }
  };

  SURF.ws.onclose = (ev) => {
    SURF.ws = null;
    // 4401 = o core não reconhece mais este token (device removido). Insistir
    // seria o ciclo de reconexão infinita; apagar o token local é o certo.
    if (ev && ev.code === 4401) {
      console.warn("[neo-surface] token recusado — despareando");
      chrome.storage.local.remove(["neoSurfaceToken", "neoSurfaceDeviceId"]);
      SURF.ultimoErro = "o Neo removeu este navegador — pareie de novo";
      return;
    }
    agendarReconexaoSuperficie();
  };

  SURF.ws.onerror = () => {
    try {
      SURF.ws.close();
    } catch {
      /* noop */
    }
  };
}

function agendarReconexaoSuperficie() {
  setTimeout(conectarSuperficie, SURF.reconnectDelay);
  SURF.reconnectDelay = Math.min(SURF.reconnectDelay * 2, RECONNECT_MAX_MS);
}

function superficieAberta() {
  return !!SURF.ws && SURF.ws.readyState === WebSocket.OPEN;
}

/** Empurra o snapshot de abas para o core (§7 do plano). */
async function empurrarAbas() {
  if (!superficieAberta()) return;
  try {
    const tabs = await collectTabs();
    SURF.ws.send(JSON.stringify({ type: "tabs", tabs }));
  } catch (e) {
    console.warn("[neo-surface] empurrarAbas falhou", e);
  }
}

function scheduleReconnect() {
  setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

// Re-report when the set of tabs changes (debounced).
chrome.tabs.onUpdated.addListener(scheduleReport);
// A aba observada navegou: reinstala o vigia (documento novo) ou reporta que a
// faixa trocou sem ser por nós. Ver `vigiarNavegacao`.
chrome.tabs.onUpdated.addListener(vigiarNavegacao);
chrome.tabs.onActivated.addListener(scheduleReport);
chrome.tabs.onRemoved.addListener(scheduleReport);
chrome.tabs.onCreated.addListener(scheduleReport);

// Keepalive: periodically wake the worker and ensure the socket is up.
chrome.alarms.create("neo-music-keepalive", { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== "neo-music-keepalive") return;
  if (!wsOpen()) connect();
  else reportTabs();
  // O canal do core tem o MESMO problema do service worker efêmero: ele morre no
  // reciclo. Mesmo keepalive, mesma razão.
  if (!superficieAberta()) conectarSuperficie();
  else empurrarAbas();
  // O worker pode ter sido reciclado desde o `watch` e levado o `setInterval`
  // junto: re-arma o laço da vigia. (O vigia da PÁGINA continua lá — é ele que
  // segura o autoplay enquanto isto aqui não existe.)
  if (watchState && !watchTimer) {
    watchTimer = setInterval(manterVigia, WATCH_LOOP_MS);
    manterVigia();
  }
});

// Status API for the popup UI.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // O vigia da página. Resposta SÍNCRONA de propósito: é ela que manda um vigia
  // órfão se desarmar.
  if (msg && msg.type === "neo-vigia") {
    sendResponse(receberDaVigia(msg, sender));
    return;
  }
  if (msg && msg.type === "getStatus") {
    collectTabs().then((tabs) => {
      sendResponse({
        connected: wsOpen(),
        connecting: !!ws && ws.readyState === WebSocket.CONNECTING,
        wsUrl: WS_URL,
        hasToken: !!extToken,
        motivo: wsOpen() ? null : ultimoMotivo,
        tabs,
        // Quando existe, esta aba está sendo dirigida pelo neo-core (fila).
        watch: watchState
          ? {
              tabId: watchState.tabId,
              videoId: watchState.videoId,
              codigo: watchState.codigo,
              fase: watchState.fase,
            }
          : null,
      });
    });
    return true; // keep the message channel open for the async response
  }
  if (msg && msg.type === "surfaceStatus") {
    surfaceState().then((s) =>
      sendResponse({
        pareada: !!s.token,
        conectada: superficieAberta(),
        coreUrl: s.coreUrl,
        erro: SURF.ultimoErro,
      }),
    );
    return true; // async
  }
  if (msg && msg.type === "surfacePair") {
    const ok = pedirPareamento();
    sendResponse({ ok, erro: SURF.ultimoErro });
    return;
  }
  if (msg && msg.type === "surfaceUnpair") {
    chrome.storage.local.remove(["neoSurfaceToken", "neoSurfaceDeviceId"]).then(() => {
      try {
        if (SURF.ws) SURF.ws.close();
      } catch {
        /* noop */
      }
      SURF.ws = null;
      SURF.ultimoErro = null;
      sendResponse({ ok: true });
    });
    return true; // async
  }
  if (msg && msg.type === "reconnect") {
    try {
      if (ws) ws.close();
    } catch {
      /* noop */
    }
    ws = null;
    reconnectDelay = RECONNECT_MIN_MS;
    connect();
    sendResponse({ ok: true });
  }
  // Colar o token vindo do Neo Desktop. Guarda, limpa o motivo antigo e reconecta
  // na hora — sem isso o usuário cola e fica olhando "desconectado" até o próximo
  // backoff, que chega a 15s.
  if (msg && msg.type === "setToken") {
    const valor = typeof msg.token === "string" ? msg.token.trim() : "";
    chrome.storage.local
      .set({ neoExtToken: valor })
      .then(() => {
        extToken = valor || null;
        ultimoMotivo = null;
        try {
          if (ws) ws.close();
        } catch {
          /* noop */
        }
        ws = null;
        reconnectDelay = RECONNECT_MIN_MS;
        connect();
        sendResponse({ ok: true, hasToken: !!extToken });
      })
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true; // async response
  }

  if (msg && msg.type === "testPlay") {
    // Self-test: run the play path directly (bypasses neo-core/desktop) to prove
    // the extension can put a video in the YouTube tab on its own.
    playYoutube(msg.videoId || "dQw4w9WgXcQ", false)
      .then((r) => sendResponse({ ok: true, reused: r.reused }))
      .catch((e) =>
        sendResponse({ ok: false, error: e && e.message ? e.message : String(e) })
      );
    return true; // async response
  }
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);

// Connect as soon as the worker spins up.
setBadge(false);
connect();
// O canal do core é independente da ponte: ele sobe mesmo com o Neo Desktop
// fechado, que é o ponto inteiro dele.
conectarSuperficie();
