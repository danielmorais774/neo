import { readFileSync } from "node:fs";

// Checagem do `searchDownloads` (apêndice do background.js) com o
// `chrome.downloads` dublado. Rode com: node scripts/check-search-downloads.mjs
//
// A extensão não tem suíte de testes e o background.js não é importável (ele
// toca `chrome.*` no topo, fora de qualquer função). Então isto recorta só o
// apêndice do Neo e o avalia com os globais que ele precisa — o suficiente para
// pegar regressão no formato que o neo-core lê, sem inventar infra de teste
// num fork que quer divergir o mínimo do upstream.
const src = readFileSync(
  new URL("../background.js", import.meta.url), "utf8"
);
// Até o apêndice seguinte (a superfície do neo-core), que toca `chrome.alarms`
// no topo e não é assunto desta checagem.
const inicio = src.indexOf("// NEO — `searchDownloads`");
const apendice = src.slice(inicio, src.indexOf("// NEO — SUPERFÍCIE DO NEO-CORE", inicio));

class BrowserBackend {}
let consultas = [];
const chrome = {
  downloads: {
    async search(q) {
      consultas.push(q);
      return [
        { id: 77, filename: "/Users/daniel/Downloads/Print.zip", url: "https://x/p.zip",
          finalUrl: "", state: "in_progress", paused: false, bytesReceived: 1024,
          totalBytes: 5000000, startTime: new Date(Date.now() - 3000).toISOString() },
        { id: 78, filename: "/Users/daniel/Downloads/nfse.pdf", url: "https://x/n.pdf",
          state: "interrupted", error: "NETWORK_FAILED", paused: false,
          bytesReceived: 512, totalBytes: 0, exists: false,
          startTime: new Date(Date.now() - 9000).toISOString() },
        { id: 79, filename: "/Users/daniel/Downloads/ok.csv", state: "complete",
          bytesReceived: 200, totalBytes: 200,
          startTime: new Date(Date.now() - 1000).toISOString() },
      ];
    },
  },
};

new Function("BrowserBackend", "chrome", apendice)(BrowserBackend, chrome);

const b = new BrowserBackend();
const r = await b.searchDownloads({ since_s: 900 });

const falhas = [];
const eq = (nome, a, e) => { if (a !== e) falhas.push(`${nome}: ${JSON.stringify(a)} != ${JSON.stringify(e)}`); };

eq("ok", r.ok, true);
eq("count", r.count, 3);
eq("ordem (mais recente 1º)", r.items[0].name, "ok.csv");
eq("basename do caminho absoluto", r.items[1].name, "Print.zip");
eq("complete não é incomplete", r.items[0].incomplete, false);
eq("in_progress é incomplete", r.items[1].incomplete, true);
eq("interrupted TAMBÉM é incomplete", r.items[2].incomplete, true);
eq("erro preservado", r.items[2].error, "NETWORK_FAILED");
eq("caminho absoluto", r.items[1].path, "/Users/daniel/Downloads/Print.zip");
eq("download_id string", typeof r.items[1].download_id, "string");
eq("since vira startedAfter", "startedAfter" in consultas[0], true);

consultas = [];
const porId = await b.searchDownloads({ ids: [77] });
eq("busca por id", consultas[0].id, 77);
eq("id devolve item", porId.items.length > 0, true);

if (falhas.length) { console.log("FALHOU:\n" + falhas.join("\n")); process.exit(1); }
console.log("searchDownloads: 13 checagens OK");
console.log(JSON.stringify(r.items[2], null, 1));
