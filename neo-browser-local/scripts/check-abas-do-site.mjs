import { readFileSync } from "node:fs";

// Checagem do 3º apêndice do background.js (abas que a página abre) com o
// `chrome.tabs` dublado. Rode com: node scripts/check-abas-do-site.mjs
//
// Mesmo recorte do `check-search-downloads.mjs`: o background.js não é
// importável, então só o apêndice é avaliado, com os globais que ele usa.
const src = readFileSync(new URL("../background.js", import.meta.url), "utf8");
// Até o 4º apêndice (a conversa), que precisa de globais que este dublê não tem.
const fimDoApendice = src.indexOf("// Neo — apêndice (4º)");
const apendice = src.slice(
  src.indexOf("// Neo — apêndice (3º)"),
  fimDoApendice > 0 ? fimDoApendice : undefined,
);

const SESSAO = { session_id: "neo-browser-local", turn_id: "t1" };
const GRUPO = 7;

const abas = new Map([
  [10, { id: 10, groupId: GRUPO, url: "https://pt.aliexpress.com/w/busca.html", title: "busca" }],
  [99, { id: 99, groupId: -1, url: "https://outra.com/", title: "aba do usuário, fora" }],
]);
const ouvintes = { created: [], removed: [] };
const removidas = [];
const agrupadas = [];
const chrome = {
  tabs: {
    onCreated: { addListener: (f) => ouvintes.created.push(f) },
    onRemoved: { addListener: (f) => ouvintes.removed.push(f) },
    async get(id) {
      if (!abas.has(id)) throw new Error(`No tab with id: ${id}`);
      return abas.get(id);
    },
    async group({ groupId, tabIds }) {
      for (const id of tabIds) {
        if (abas.get(id)?.popup) throw new Error("Tabs can only be grouped in normal windows");
        abas.get(id).groupId = groupId;
        agrupadas.push(id);
      }
      return groupId;
    },
    async remove(id) {
      removidas.push(id);
      abas.delete(id);
    },
  },
};

const estado = { chromeGroupId: GRUPO, tabOrigins: { 10: "user" } };
const store = {
  saves: 0,
  async findSessionByGroup(g) {
    return g === GRUPO ? { sessionId: SESSAO.session_id, session: estado } : null;
  },
  async getSession() {
    return estado;
  },
  async save() {
    this.saves++;
  },
};

class BrowserBackend {
  constructor() {
    this.store = store;
    this.desconectadas = [];
  }
  async requireSession(p) {
    if (p?.session_id !== SESSAO.session_id) throw new Error("Missing browser session params");
    return { sessionId: p.session_id, turnId: p.turn_id };
  }
  async getSessionTabs() {
    return [...abas.values()].filter((t) => t.groupId === GRUPO);
  }
  async detachTab(id) {
    this.desconectadas.push(id);
  }
  async requireSessionTab(p) {
    if (!(await this.getSessionTabs()).some((t) => t.id === p.tabId)) {
      throw new Error(`Tab ${p.tabId} is not part of browser session`);
    }
  }
}
const hasTabId = (t) => typeof t?.id === "number";
const requireTabId = (p, c) => {
  if (!Number.isInteger(p.tabId)) throw new Error(`${c} requires an integer tabId`);
  return p.tabId;
};
const backend = new BrowserBackend();

new Function("BrowserBackend", "chrome", "backend", "hasTabId", "requireTabId", apendice)(
  BrowserBackend, chrome, backend, hasTabId, requireTabId,
);

const falhas = [];
const eq = (nome, a, e) => {
  if (JSON.stringify(a) !== JSON.stringify(e)) falhas.push(`${nome}: ${JSON.stringify(a)} != ${JSON.stringify(e)}`);
};
const nasce = async (aba) => {
  abas.set(aba.id, aba);
  for (const f of ouvintes.created) f(aba);
  await new Promise((r) => setTimeout(r, 5));
};

// 1. o anúncio abre aba nova a partir da aba da sessão
await nasce({ id: 11, groupId: -1, openerTabId: 10, url: "", pendingUrl: "https://pt.aliexpress.com/item/1.html", title: "" });
eq("entrou no grupo", abas.get(11).groupId, GRUPO);
eq("origem site", estado.tabOrigins["11"], "site");
eq("quem abriu", estado.tabOpeners["11"], 10);
const novas = await backend.neoTakeNewTabs({ ...SESSAO, openerTabId: 10 });
eq("entregue uma vez", novas.map((t) => [t.id, t.url, t.origin, t.openerTabId]), [[11, "https://pt.aliexpress.com/item/1.html", "site", 10]]);
eq("não entrega de novo", await backend.neoTakeNewTabs({ ...SESSAO, openerTabId: 10 }), []);

// 2. aba aberta por aba que NÃO é da sessão: não é da nossa conta
await nasce({ id: 12, groupId: -1, openerTabId: 99, url: "https://x.com" });
eq("aba alheia fica de fora", estado.tabOrigins["12"], undefined);
eq("nada a entregar para a alheia", await backend.neoTakeNewTabs({ ...SESSAO, openerTabId: 99 }), []);

// 3. popup (janela sem grupos): fica fora do grupo, mas a sessão a reconhece
await nasce({ id: 13, groupId: -1, openerTabId: 10, url: "https://login.exemplo/popup", popup: true });
eq("popup com origem site", estado.tabOrigins["13"], "site");
eq("popup fora do grupo", abas.get(13).groupId, -1);
let aceitou = true;
try { await backend.requireSessionTab({ ...SESSAO, tabId: 13 }, "executeCdp"); } catch { aceitou = false; }
eq("CDP aceito no popup", aceitou, true);
let recusou = false;
try { await backend.requireSessionTab({ ...SESSAO, tabId: 99 }, "executeCdp"); } catch { recusou = true; }
eq("aba alheia continua recusada", recusou, true);
const lista = await backend.neoSessionTabs(SESSAO);
eq("sessão lista grupo + popup", lista.map((t) => [t.id, t.origin]).sort(), [[10, "user"], [11, "site"], [13, "site"]]);

// 4. filha que fecha antes de ser vista some da entrega
await nasce({ id: 14, groupId: -1, openerTabId: 10, url: "about:blank" });
abas.delete(14);
for (const f of ouvintes.removed) f(14);
eq("filha fechada não é entregue", (await backend.neoTakeNewTabs({ ...SESSAO, openerTabId: 10 })).map((t) => t.id), [13]);

// 5. fechar: só o que o Neo ou a página abriu
eq("fecha a do site", await backend.neoCloseTab({ ...SESSAO, tabId: 11 }), { ok: true });
eq("removida", removidas, [11]);
eq("desconectada antes", backend.desconectadas, [11]);
let protegida = false;
try { await backend.neoCloseTab({ ...SESSAO, tabId: 10 }); } catch (e) { protegida = /não fecho/.test(e.message); }
eq("a do usuário nunca fecha", protegida, true);
eq("só a 11 foi removida", removidas, [11]);

if (falhas.length) {
  console.log("FALHOU:\n" + falhas.join("\n"));
  process.exit(1);
}
console.log("abas do site: 19 checagens OK");

// 6. reivindicar de novo (retomada) não rebaixa a origem
{
  BrowserBackend.prototype.claimUserTab = async function (p) {
    estado.tabOrigins[String(p.tabId)] = "user"; // o que o upstream faz
    return { id: p.tabId };
  };
  new Function("BrowserBackend", "chrome", "backend", "hasTabId", "requireTabId",
    apendice.slice(apendice.indexOf("const neoClaimUserTabUpstream")))(
    BrowserBackend, chrome, backend, hasTabId, requireTabId,
  );
  estado.tabOrigins["13"] = "site";
  await backend.claimUserTab({ ...SESSAO, tabId: 13 });
  const ok1 = estado.tabOrigins["13"] === "site";
  estado.tabOrigins["10"] = "user";
  await backend.claimUserTab({ ...SESSAO, tabId: 10 });
  const ok2 = estado.tabOrigins["10"] === "user";
  if (!ok1 || !ok2) {
    console.log("FALHOU: claimUserTab rebaixou a origem", estado.tabOrigins);
    process.exit(1);
  }
  console.log("claimUserTab preserva a origem: 2 checagens OK");
}
