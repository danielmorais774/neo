# Upstream

Fork de [iFurySt/open-browser-use](https://github.com/iFurySt/open-browser-use)
(`apps/chrome-extension`), licença MIT — o `LICENSE` original está preservado
na raiz deste projeto.

- **Commit de origem:** `07a8014d18e05d8579aa9dcb380a3d3b5d4f2a76`
- **Versão da extensão:** 0.1.45
- **Host nativo:** NÃO forkado. Continuamos usando o binário `obu` publicado no
  npm (`npm i -g open-browser-use`) e o mesmo nome de host
  (`com.ifuryst.open_browser_use.extension`). É isso que mantém
  `npm update -g open-browser-use` valendo.

## O que divergiu

| Arquivo | Mudança |
|---|---|
| `manifest.json` | nome/descrição do Neo; `key` fixa (ID estável, ver README); registro do content script `content-overlay.js`; **`connect-src` do `extension_pages`** (ver o aviso abaixo) |
| `background.js` | bloco **apêndice** no fim do arquivo (nada acima foi tocado): estado do painel por aba (a linha do tempo inteira, com teto), `NEO_STOP` → notificação `neo.stop`, badge de conexão, método `searchDownloads` (no prototype do `BrowserBackend`) |
| `content-overlay.js` | **novo** — o painel lateral do Neo (linha do tempo dos passos e do que cada um devolveu), o pulso do clique e o botão PARE |
| `background.js` | bloco **apêndice** (2º): superfície do neo-core — pareamento por código, canal `/ws/devices/control`, `hello` e snapshot de abas opt-in |
| `background.js` | bloco **apêndice** (3º): abas que a PÁGINA abre — `tabs.onCreated` com `openerTabId` da sessão entra no grupo com origem `site`; `neoTakeNewTabs`, `neoSessionTabs`, `neoCloseTab`; `requireSessionTab` embrulhado no prototype para aceitar popup `site` fora do grupo; `claimUserTab` embrulhado para não rebaixar a origem `site`/`agent` na retomada (`neo-core/docs/browser-multi-aba-plan.md`) |
| `popup.html` / `popup.js` / `popup.css` | seção **"Conta do Neo"**: campo do código, nome, avançado (endereço do core + reportar abas), desparear |
| `scripts/setup.sh` | **novo** — registra o host nativo para Brave/Chrome/Edge com o NOSSO extension id (o `obu setup` só conhece Chrome) |
| `scripts/check-search-downloads.mjs` | **novo** — checagem do `searchDownloads` com o `chrome.downloads` dublado (`node scripts/check-search-downloads.mjs`) |
| `neo-map.generated.js` | **novo, GERADO** — o inventário de elementos extraído do `js_snapshot` do neo-desktop. Não editar: rode `python3 scripts/gerar-mapa.py` |
| `content-inspect.js` | **novo** — inspetor visual: desenha na página o que o agente enxerga (canvas em shadow DOM, zero mutação da página). **Não** vai no `content_scripts`: é injetado sob demanda pelo popup |
| `scripts/gerar-mapa.py` | **novo** — gera o `neo-map.generated.js` a partir do Rust; `--check` falha se estiver desatualizado |
| `scripts/testar-inspetor.py` | **novo** — roda o inspetor num Chromium headless e prova que ele não mexe no layout |
| `scripts/testar-painel.py` | **novo** — roda o painel num Chromium headless: os eventos empilham, a página não se mexe, o PARE (achado por pixel, shadow fechado) marca `data-neo-stop`, e esconder pelo id limpa a foto |
| `popup.html` / `popup.css` / `popup.js` | painel "Inspetor do Neo" (botão Mapear/Limpar + resumo). O resto é upstream |

Nada mais foi alterado. `content-cursor.js` e os ícones são upstream intocados;
o `popup.*` ganhou só o painel do inspetor, no fim de cada arquivo.

⚠️ **Content scripts do mesmo item do `content_scripts` compartilham UM escopo
de topo.** `content-overlay.js` declara `const HOST_ID`; o `content-inspect.js`
declarava outro, e o Chrome matava o arquivo inteiro com "Identifier 'HOST_ID'
has already been declared" — sem listener, o popup recebia `undefined` do OUTRO
content script e dizia "esta aba não aceita o inspetor" em TODO site. Por isso o
inspetor vive dentro de uma IIFE com guarda de instalação, e é injetado sob
demanda (`chrome.scripting.executeScript`) em vez de carregar em toda página:
33 KB por page load em toda aba, para uma ferramenta ocasional, não se paga — e
injetar sob demanda ainda resolve a aba que já estava aberta quando a extensão
foi recarregada.

⚠️ **O `connect-src` do upstream é `'self'` — e isso mata a Conta do Neo.**
Upstream a extensão só fala por *native messaging*, que não passa por CSP; então
`connect-src 'self'` nunca incomodou ninguém lá. O nosso apêndice acrescentou um
`fetch` (`/devices/pair`) e um `WebSocket` (`/ws/devices/control`) — os dois
morrem calados sob aquele CSP, o `fetch` como `TypeError: Failed to fetch` (que
é idêntico ao erro de túnel fora do ar) e o `WebSocket` como reconexão eterna.
Por isso o `extension_pages` daqui abre `127.0.0.1`/`localhost` em qualquer porta
(o túnel) e `https:`/`wss:` (uma entrada pública no futuro), e segue recusando
`http://` para fora da máquina. **Ao trazer o manifest do upstream por cima,
re-aplique isto** — e mantenha `neoSurfForaDoAlcance()` no `background.js` em
sincronia, que é quem transforma o bloqueio numa frase em vez de num
"Failed to fetch".

⚠️ **`neo-map.generated.js` é derivado do neo-desktop.** Mexeu no `js_snapshot`
do `browser_local.rs`? Rode `python3 scripts/gerar-mapa.py` e recarregue a
extensão, senão o inspetor passa a mostrar uma versão antiga do que o agente
enxerga — o que é pior que não mostrar nada.

## Como atualizar o upstream

```bash
git clone --depth 1 https://github.com/iFurySt/open-browser-use.git /tmp/obu-src
diff -u /tmp/obu-src/apps/chrome-extension/background.js background.js   # só o apêndice deve aparecer
```

Copie os arquivos upstream por cima, re-aplique o apêndice do `background.js` e
os campos do `manifest.json` da tabela acima, e anote o novo commit aqui.
