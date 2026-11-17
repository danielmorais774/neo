// Testa EM QUE ABA a fila toca: `node scripts/testar-aba-da-fila.mjs`
//
// O defeito que este arquivo guarda (medido em prod em 12/09, log do desktop
// 15:39:58): a segunda faixa da fila ia tocar em OUTRA GUIA, deixando a primeira
// pausada na guia em que o usuário estava ouvindo. A causa não era uma só — era
// a aba se perdendo em cada salto — e o `background.js` é o último elo, o único
// que sabe onde a música caiu de verdade.
//
// Aqui o `background.js` roda de verdade (num VM, com `chrome` e `WebSocket`
// dublados) com VÁRIAS abas do YouTube abertas, que é a condição em que o bug
// aparece: com uma aba só, escolher errado é impossível.
//
// O que ele NÃO cobre: o vigia injetado na página (isso é o `testar-vigia.mjs`)
// e a navegação real do player.

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
const passar = () => new Promise((r) => setImmediate(r));
const assentar = async () => {
  for (let i = 0; i < 12; i += 1) await passar();
};

const watchUrl = (id) => "https://www.youtube.com/watch?v=" + id;

// ── o navegador ────────────────────────────────────────────────────────────
// Várias abas, cada uma com sua URL. `criadas` registra aba nova; `navegadas`
// registra quem foi mandada para outra URL — é a diferença entre "reaproveitou"
// e "abriu outra guia".

function criarNavegador(abas) {
  const estado = {
    abas: abas.map((a) => ({ windowId: 1, ...a })),
    criadas: [],
    navegadas: [],
    injecoes: [],
    proximoId: 900,
  };
  const achar = (id) => estado.abas.find((t) => t.id === id);

  const chrome = {
    tabs: {
      query: async (q) => {
        if (q && q.url) {
          return estado.abas.filter((t) => /youtube\.com/.test(t.url || ""));
        }
        return estado.abas.slice();
      },
      get: async (id) => {
        const t = achar(id);
        if (!t) throw new Error("No tab with id " + id);
        return t;
      },
      update: async (id, props) => {
        const t = achar(id);
        if (!t) throw new Error("No tab with id " + id);
        if (props && props.url) {
          t.url = props.url;
          estado.navegadas.push({ id, url: props.url });
        }
        if (props && props.active) t.active = true;
        return t;
      },
      create: async (props) => {
        const t = { id: (estado.proximoId += 1), windowId: 1, url: props.url, active: !!props.active };
        estado.abas.push(t);
        estado.criadas.push(t);
        return t;
      },
      onUpdated: { addListener: () => {} },
      onActivated: { addListener: () => {} },
      onRemoved: { addListener: () => {} },
      onCreated: { addListener: () => {} },
    },
    windows: { update: async () => {} },
    alarms: { create: () => {}, onAlarm: { addListener: () => {} } },
    runtime: {
      id: "abcdef",
      getManifest: () => ({ version: "teste" }),
      onMessage: { addListener: () => {} },
      onStartup: { addListener: () => {} },
      onInstalled: { addListener: () => {} },
    },
    action: { setBadgeText: () => {}, setBadgeBackgroundColor: () => {}, setTitle: () => {} },
    scripting: {
      // A vigia: aqui só registramos ONDE ela foi instalada/desarmada. O
      // comportamento dela é assunto do testar-vigia.mjs.
      executeScript: async ({ target }) => {
        estado.injecoes.push(target.tabId);
        return [{ result: "armada" }];
      },
    },
  };
  return { chrome, estado };
}

function criarWorker(chrome) {
  const enviados = [];
  let wsAtual = null;

  class FakeWS {
    constructor() {
      this.readyState = 1; // já aberto: aqui o assunto é a escolha da aba
      wsAtual = this;
      setImmediate(() => this.onopen && this.onopen());
    }
    send(txt) {
      enviados.push(JSON.parse(txt));
    }
    close() {
      this.readyState = 3;
    }
  }
  FakeWS.OPEN = 1;
  FakeWS.CONNECTING = 0;

  const ctx = vm.createContext({
    chrome,
    WebSocket: FakeWS,
    console: { log: () => {}, warn: () => {} },
    setTimeout,
    clearTimeout,
    setInterval: () => 1,
    clearInterval: () => {},
    Date,
    URL,
    URLSearchParams,
    Promise,
  });
  vm.runInContext(fonte, ctx);

  return {
    enviados,
    receber: (msg) => wsAtual.onmessage({ data: JSON.stringify(msg) }),
    resultados: () => enviados.filter((m) => m.type === "result"),
  };
}

// ── o caso que quebrou em produção ─────────────────────────────────────────

async function filaNaoTrocaDeGuia() {
  console.log("\nfila não troca de guia no meio (o bug de 12/09)");
  // Duas abas do YouTube: a do usuário (primeira da lista, é ela que o
  // `tabs.find` pegava) e a aba 77, onde a fila está tocando.
  const { chrome, estado } = criarNavegador([
    { id: 10, url: watchUrl("AULADEMATEM") },
    { id: 77, url: watchUrl("FAIXA000001") },
  ]);
  const w = criarWorker(chrome);
  await assentar();

  // Faixa 1 tocando na aba 77, com a vigia confirmada ali (é o que o
  // `media_event`/snapshot produz na vida real).
  w.receber({ type: "watch", tabId: 77, videoId: "FAIXA000001", codigo: "ab12" });
  await assentar();

  // O avanço da fila: o desktop re-arma e manda tocar. O `tabId` vem do alvo
  // gravado na inscrição — AQUI ELE ESTÁ ERRADO (aponta a aba do usuário), que
  // é exatamente o que o log de produção mostrou.
  w.receber({ type: "watch", tabId: 10, videoId: "FAIXA000002", codigo: "cd34" });
  w.receber({
    type: "command",
    id: "c1",
    action: "play",
    target: "youtube",
    videoId: "FAIXA000002",
    tabId: 10,
  });
  await assentar();

  eq("tocou na aba da fila", estado.navegadas.length && estado.navegadas[0].id, 77);
  ok(
    "não mexeu na aba do usuário",
    !estado.navegadas.some((n) => n.id === 10),
    JSON.stringify(estado.navegadas)
  );
  eq("não abriu guia nova", estado.criadas.length, 0);
  eq("a aba usada volta no result", w.resultados()[0] && w.resultados()[0].tabId, 77);
  eq("result com sucesso", w.resultados()[0] && w.resultados()[0].success, true);
}

async function filaSemAbaAbreASuaPropria() {
  console.log("\nfila sem aba reusável abre a SUA aba (não toma a do usuário)");
  // Só a aba do usuário está aberta. O neo-core já recusou tomá-la
  // (`pick_reuse_tab`) e converteu o `open_url` em `media_command` com
  // `novaAba`. Sem esse sinal a extensão navegava a aba 10 — o vídeo do usuário
  // morria no meio.
  const { chrome, estado } = criarNavegador([{ id: 10, url: watchUrl("AULADEMATEM") }]);
  const w = criarWorker(chrome);
  await assentar();

  w.receber({ type: "watch", tabId: null, videoId: "FAIXA000001", codigo: "ab12" });
  w.receber({
    type: "command",
    id: "c1",
    action: "play",
    target: "youtube",
    videoId: "FAIXA000001",
    tabId: null,
    novaAba: true,
  });
  await assentar();

  eq("abriu uma guia própria", estado.criadas.length, 1);
  ok(
    "não tomou a aba do usuário",
    !estado.navegadas.some((n) => n.id === 10),
    JSON.stringify(estado.navegadas)
  );
  const nova = estado.criadas[0].id;
  eq("o result diz qual aba nasceu", w.resultados()[0] && w.resultados()[0].tabId, nova);

  // E a faixa seguinte fica NELA, mesmo com o desktop mandando `tabId: null`
  // (o alvo da inscrição continua vazio até o primeiro evento chegar).
  w.receber({ type: "watch", tabId: null, videoId: "FAIXA000002", codigo: "cd34" });
  w.receber({
    type: "command",
    id: "c2",
    action: "play",
    target: "youtube",
    videoId: "FAIXA000002",
    tabId: null,
  });
  await assentar();
  eq("a 2ª faixa foi para a aba da fila", estado.navegadas.length, 1);
  eq("…que é a que nasceu", estado.navegadas[0].id, nova);
  eq("e nenhuma outra guia foi aberta", estado.criadas.length, 1);
}

async function tocadaAvulsaContinuaComoEra() {
  console.log("\nsem fila armada, a tocada avulsa reusa a aba como antes");
  const { chrome, estado } = criarNavegador([
    { id: 10, url: "https://www.youtube.com/" },
    { id: 77, url: watchUrl("FAIXA000001") },
  ]);
  const w = criarWorker(chrome);
  await assentar();

  // Nenhum `watch`: é "toca um AC/DC". Sem pin, vale a primeira aba do YouTube
  // (o comportamento antigo, que o neo-core espera quando decide sozinho).
  w.receber({
    type: "command",
    id: "c1",
    action: "play",
    target: "youtube",
    videoId: "ACDC0000001",
  });
  await assentar();
  eq("reusou a primeira aba do YouTube", estado.navegadas[0] && estado.navegadas[0].id, 10);
  eq("sem guia nova", estado.criadas.length, 0);

  // Com pin, manda o pin (a decisão foi tomada sobre o snapshot).
  w.receber({
    type: "command",
    id: "c2",
    action: "play",
    target: "youtube",
    videoId: "ACDC0000002",
    tabId: 77,
  });
  await assentar();
  eq("com pin, vale o pin", estado.navegadas[1] && estado.navegadas[1].id, 77);
}

async function main() {
  await filaNaoTrocaDeGuia();
  await filaSemAbaAbreASuaPropria();
  await tocadaAvulsaContinuaComoEra();
  console.log(`\n${passes} ok, ${falhas} falha(s)`);
  process.exit(falhas ? 1 : 0);
}

main();
