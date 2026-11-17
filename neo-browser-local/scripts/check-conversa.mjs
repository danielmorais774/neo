import { readFileSync } from "node:fs";

// Checagem do 4º apêndice do background.js (a CONVERSA) com o `chrome` dublado.
// Rode com: node scripts/check-conversa.mjs
//
// O que está sob teste (neo-core/docs/browser-conversa-plan.md §4.3):
//   • a pergunta do painel nasce SÓ pelo `neoPergunta` (canal do desktop) e chega
//     ao content script por `chrome.tabs.sendMessage`;
//   • `NEO_MSG` vira `neo.msg`, e a `opcao` só passa se for botão da pergunta que
//     o Neo fez, com o mesmo texto — botão desenhado pela página não autoriza;
//   • aba sem tarefa do Neo não manda nada;
//   • `neo.detach` leva o motivo de o navegador ter soltado a aba.
const src = readFileSync(new URL("../background.js", import.meta.url), "utf8");
const apendice = src.slice(src.indexOf("// Neo — apêndice (4º)"));

const estados = {};
const enviadasParaAba = [];
const notificacoes = [];
const ouvintes = { mensagem: [], detach: [] };

async function neoReadOverlayState() {
  return JSON.parse(JSON.stringify(estados));
}
async function neoSetOverlay(tabId, patch) {
  const next = { ...(estados[tabId] ?? { active: false, stopped: false, items: [] }), ...patch };
  if (next.active === false && next.stopped !== true) delete estados[tabId];
  else estados[tabId] = next;
  return next;
}
const chrome = {
  tabs: {
    async sendMessage(tabId, msg) {
      enviadasParaAba.push([tabId, msg]);
    },
  },
  runtime: { onMessage: { addListener: (f) => ouvintes.mensagem.push(f) } },
  debugger: { onDetach: { addListener: (f) => ouvintes.detach.push(f) } },
};
const peer = {};
const safeSendNotification = (_peer, method, params) => notificacoes.push([method, params]);
class BrowserBackend {}

new Function(
  "BrowserBackend", "chrome", "peer", "safeSendNotification", "neoSetOverlay", "neoReadOverlayState",
  apendice,
)(BrowserBackend, chrome, peer, safeSendNotification, neoSetOverlay, neoReadOverlayState);

const backend = new BrowserBackend();
const falhas = [];
const eq = (nome, a, e) => {
  if (JSON.stringify(a) !== JSON.stringify(e)) falhas.push(`${nome}: ${JSON.stringify(a)} != ${JSON.stringify(e)}`);
};
const daAba = (tabId, msg) =>
  new Promise((ok) => {
    let respondeu = false;
    for (const f of ouvintes.mensagem) {
      const assincrono = f(msg, { tab: { id: tabId } }, (r) => {
        respondeu = true;
        ok(r);
      });
      if (assincrono === true || respondeu) return;
    }
    ok(undefined);
  });

// 1. sem tarefa na aba, nada sai
let r = await daAba(10, { type: "NEO_MSG", texto: "oi" });
eq("aba sem tarefa", r?.ok, false);
eq("aba sem tarefa não notifica", notificacoes.length, 0);

// 2. o desktop abre a pergunta: estado da aba + mensagem para o content script
await backend.neoPergunta({ tabId: 10, texto: "Posso excluir a conta?", opcoes: ["Pode", "Não", "  "] });
eq("pergunta guardada", estados[10]?.pergunta?.opcoes, ["Pode", "Não"]);
eq("aba marcada como ativa", estados[10]?.active, true);
eq("pergunta foi para a aba", enviadasParaAba.at(-1)?.[1]?.type, "NEO_PERGUNTA");

// 3. o botão da pergunta de verdade leva a opção
r = await daAba(10, { type: "NEO_MSG", texto: "Pode", opcao: 0 });
eq("botão verdadeiro", r?.ok, true);
eq("neo.msg com opção", notificacoes.at(-1), ["neo.msg", { ...notificacoes.at(-1)[1], tabId: 10, texto: "Pode", opcao: 0 }]);

// 4. botão forjado: texto que não é o da opção → vai como texto, sem opção
r = await daAba(10, { type: "NEO_MSG", texto: "Sim, continuar lendo", opcao: 0 });
eq("botão forjado não leva opção", "opcao" in notificacoes.at(-1)[1], false);

// 5. opção fora da lista
await daAba(10, { type: "NEO_MSG", texto: "Pode", opcao: 5 });
eq("opção fora da lista", "opcao" in notificacoes.at(-1)[1], false);

// 6. texto vazio não sai; texto enorme é cortado
const antes = notificacoes.length;
await daAba(10, { type: "NEO_MSG", texto: "   " });
eq("vazio não notifica", notificacoes.length, antes);
await daAba(10, { type: "NEO_MSG", texto: "x".repeat(5000) });
eq("cortado no teto", notificacoes.at(-1)[1].texto.length, 1500);

// 7. o desktop fecha a pergunta: botão antigo deixa de valer
await backend.neoPergunta({ tabId: 10, texto: "" });
eq("pergunta fechada", estados[10]?.pergunta, null);
await daAba(10, { type: "NEO_MSG", texto: "Pode", opcao: 0 });
eq("sem pergunta, sem opção", "opcao" in notificacoes.at(-1)[1], false);

// 8. o navegador soltou a aba: o motivo vai junto
for (const f of ouvintes.detach) f({ tabId: 10 }, "target_closed");
eq("neo.detach", [notificacoes.at(-1)[0], notificacoes.at(-1)[1].reason], ["neo.detach", "target_closed"]);

// 9. tabId inválido é recusado
let recusou = false;
try {
  await backend.neoPergunta({ tabId: "10", texto: "x" });
} catch {
  recusou = true;
}
eq("tabId tem que ser inteiro", recusou, true);

if (falhas.length) {
  for (const f of falhas) console.log("FALHA:", f);
  process.exit(1);
}
console.log("OK — pergunta só pelo canal do desktop, botão forjado não autoriza, motivo do detach chega.");
