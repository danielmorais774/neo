# Plano: o Neo trabalha com várias abas — segue a nova, volta, e roda em paralelo

**Data:** 2026-11-02
**Status:** F0 medido (§12); F1 implementada em 2026-11-02 (§13, falta o E2E);
F2 implementada e com E2E em 2026-11-02 (§15); F3 implementada em 2026-11-02 (§16, falta o E2E). Origem: `session-bea5f2e6`,
job `3512cdd2`.
**Depende de:** `docs/browser-local-plan.md` (driver, desktop, extensão) e
`docs/browser-surface-plan.md` (headless × navegador do usuário).

## 0. O pedido

O Neo tem que trabalhar como uma pessoa trabalha com abas:

- **seguir a aba nova** quando um clique abre uma, sem se perder;
- **concluir numa aba e voltar** para a original (o popup de login que se fecha
  sozinho, o PDF que abriu ao lado, o "termos de uso");
- **concluir na aba nova e não voltar** (o resultado de busca que abre o site
  noutra aba, o "Acessar o sistema");
- **trabalhar em várias ao mesmo tempo**: numa loja, abrir os 3 anúncios e ler
  os 3 em paralelo, e abrir mais se precisar.

Descartado de propósito: "trazer tudo para uma aba só" (reescrever
`target=_blank`, fechar a aba nova e navegar a original). Resolve o caso de
hoje e proíbe o resto do pedido.

## 1. O que aconteceu (job 3512cdd2, 2026-11-02)

"Veja o preço da AMD BC-250 no AliExpress", no navegador dele.

| Passo | Ação | O que o agente viu | O que de fato houve |
|---|---|---|---|
| 4 | clique no anúncio 1 | "ok", tela **não** mudou | o produto abriu numa **aba nova**, e o Edge foi para ela |
| 5 | `navigate` para `/item/…` (o link estava no snapshot) | página do produto | contorno do modelo, na aba da busca |
| 7 | clique no anúncio 2 | "ok", tela "mudou" | outra aba nova; ele achou que seguia na busca |
| 8–13 | busca ↔ anúncio 1, três vezes | — | 10 notas para 3 produtos |

Os efeitos:

- o agente fica **cego**: nem o resultado do clique nem o snapshot dizem que nasceu
  uma aba. Saiu porque o AliExpress põe o endereço no link; num card que abre
  aba por script, giraria até o freio;
- o usuário **olhava a aba errada**: o Edge foi para a aba nova, e o agente
  seguiu na outra, em segundo plano, com o painel lá;
- **abas órfãs**: `browser.release` só desconecta, não fecha. As duas páginas de
  produto ficaram abertas no Edge dele;
- **headless pior**: o gerente de abas (`services/headless.rs`) só conhece as abas
  que ele abriu. As do site não contam no teto de abas e só morrem quando o
  navegador inteiro reinicia pelo teto de RAM;
- **entrega com ressalva errada**: as notas diziam "Busca AliExpress…", mas frete,
  vendedor e impostos vieram das páginas de produto. A aba terminou na busca, e o
  cérebro escreveu "isso é o que a página de BUSCA mostrou".

## 2. Estado atual: o que existe e o que falta

| Camada | Hoje | Para várias abas |
|---|---|---|
| Extensão (`neo-browser-local/background.js`) | sessão com **grupo de abas**, `tabOrigins` (agent/user), `getTabs` da sessão, `finalizeTabs(keep=[{tabId, status: deliverable\|handoff}])` que fecha as do agente e solta as do usuário — herança do open-browser-use, **nunca usada pelo Neo**. `onCreated` só de downloads. | registrar aba criada com `openerTabId` de uma aba da sessão como origem `site`; `onRemoved`; expor a lista com opener e origem |
| Desktop (`services/browser_local.rs`) | todas as ações recebem `tab_ref` explícito (bom). `call()` à extensão sob `Mutex` global, por chamada (ms). Release = `detach`. | depois de ação que atua, devolver `abas_novas`; `browser.session_tabs`; `browser.close_tab`; `browser.focus_tab`; release que fecha as auxiliares |
| Desktop (`websocket.rs`) | **comandos atendidos em fila**: cada um é `await`ado no loop de leitura. Um `navigate` (até 20 s de espera de load) trava qualquer comando de outra aba | atender cada comando numa task própria (`spawn`), com o `write` atrás de um canal |
| Headless (`services/headless.rs`) | `tabs` = só as abertas por `open_tab`; teto e ociosidade contam só essas | `Target.setDiscoverTargets` + `targetCreated` com `openerId`: registrar, contar no teto, fechar no fim |
| Canal (`core/notifications/device_control.py`) | `correlation_id` por pedido: já aceita chamadas simultâneas | nada |
| Driver (`core/browser_local/driver.py`) | UM `_tab_ref`; escopo, `_last_seen_url` (liberação), freio de giro, `_navegou` e caderno, todos por job | registro de abas, aba ativa, estado por aba, ações de aba, subagentes |
| Runner/sessão (`browser_session`) | `tab_ref` e `url` de UMA aba; retomadas reabrem uma | o conjunto de abas e qual é a ativa |

## 3. O modelo: o job tem um CONJUNTO de abas

Cada aba do job ganha um **rótulo curto e estável** (`A`, `B`, `C`…) — é o que o
modelo lê e escreve, nunca o `tab_ref`. No driver:

```
Aba(rotulo, tab_ref, origem, aberta_por, url, titulo, viva,
    ultimo_url_visto, navegou, travado, impressao_digital)
origem ∈ {usuario, agente, site}
```

- `usuario`: a aba que ele já tinha e o job reivindicou. Nunca é fechada pelo job.
- `agente`: aberta pelo Neo (`claim` sem aba, `abrir_aba`).
- `site`: aberta pela página (link `_blank`, `window.open`, popup), com
  `aberta_por` apontando a aba de origem.

Tudo que hoje é "do job" e na verdade é "da aba" passa a viver na aba: o
`_last_seen_url` (que escopa a liberação de dados), o freio de giro, a impressão
digital da tela, o `_navegou`. O que é do job continua do job: o caderno, o
orçamento de passos, as respostas da memória, a allowlist de domínios.

Todo passo do agente mostra um bloco curto, barato (título e URL; o snapshot é só
da ativa):

```
ABAS (você age na ▶):
  A  busca "AMD BC-250" · pt.aliexpress.com/w/wholesale-AMD-BC-250.html
▶ B  Placa de Vídeo AMD BC 250 16GB · /item/1005008976362329.html  (aberta pelo site, a partir de A)
```

### 3.1 Regra padrão: seguir a aba nova

Clique que abre aba → a nova vira a **ativa**, vem para a frente no modo com
janela, e o passo diz:

> o clique abriu a aba **B** (/item/1005008976362329.html). Você está nela agora.
> A aba A (busca) continua aberta — `trocar_aba` volta para lá.

Seguir é o padrão porque é o que o clique quis dizer. Se o modelo queria ficar,
troca de volta, e isso custa um passo, não a tarefa.

### 3.2 Os três desfechos do pedido

1. **Concluir na nova e voltar**: `fechar_aba` na B volta para quem a abriu. Se a
   página se fecha sozinha (popup de login, `window.close()`), o driver percebe
   no próximo passo e volta para a origem sozinho, avisando.
2. **Concluir na nova e não voltar**: `done` com a B ativa. No fim ela é a
   **entregue**: fica aberta e na frente. As auxiliares (origem `agente`/`site`)
   são fechadas; a do usuário é só solta.
3. **Várias ao mesmo tempo**: `abrir_aba` para abrir anúncios e `paralelo` para
   trabalhar neles (§5).

### 3.3 Vocabulário novo (pequeno de propósito)

| Ação | Faz | Notas |
|---|---|---|
| `trocar_aba {"aba": "A"}` | muda a ativa (e traz para a frente no modo com janela) | snapshot da nova no próximo passo |
| `fechar_aba {"aba": "B"}` | fecha; se era a ativa, volta para a que a abriu | nunca fecha aba `usuario`; sem `aba` = a ativa |
| `abrir_aba {"url": "…"}` / `{"ref": 27}` | abre em aba nova e PASSA para ela (como 3.1) | `ref` = o link do elemento; `url` passa pelo escopo de domínio |
| `paralelo {...}` | subagentes, um por aba (§5) | — |
| `ler_abas {"abas": ["B","C"], "busca": "frete"}` | `extract` em várias abas de uma vez, sem LLM por aba | o barato do paralelo (§5.1) |

As ações que já existem continuam agindo na **ativa**: não ganham parâmetro
`aba`. Um vocabulário onde todo verbo aceita "em qual aba" é o jeito mais curto
de o modelo clicar na aba errada.

### 3.4 O caderno sabe de onde veio

Cada nota ganha, automaticamente, a origem: aba e caminho da URL onde estava
ativa quando foi anotada (`B · /item/1005008976362329.html`). A entrega mostra a
origem, e o rótulo inventado pelo modelo ("Busca AliExpress…") deixa de ser a
única pista de onde o dado veio.

## 4. Detecção: quem fica sabendo que nasceu uma aba

**Extensão (navegador dele).** `chrome.tabs.onCreated` com `openerTabId` de uma
aba da sessão → entra na sessão com origem `site` e `aberta_por` (a permissão
`tabs` já existe; não precisa de `webNavigation`). `chrome.tabs.onRemoved` →
marca fechada. A URL chega por `onUpdated` (a aba nasce em `about:blank` ou com
`pendingUrl`). RPC nova `getSessionTabsDetailed` → `[{tabId, url, title,
openerTabId, origin, active}]`.

**Desktop.** O gatilho é o evento `Page.windowOpen` da aba onde a ação
aconteceu (medido no §12: chega 9–31 ms depois do clique, já com a URL e com
`userGesture`). Sem o evento, o resultado volta na hora: **nenhum clique paga
espera**. Com ele, o desktop espera a aba existir (40–90 ms medidos; teto de
1 s) e devolve no resultado:

```json
{"ok": true, "label": "…", "abas_novas": [{"tab_ref": 1976622001, "url": "https://…", "aberta_por": 1976621691}]}
```

e já faz `attach` + `Page.enable` na nova. Hoje o `Conn::request` do desktop lê
as notificações da extensão (`onCDPEvent`) só enquanto espera uma resposta, e
descarta tudo menos o PARE: ele passa a guardar o `Page.windowOpen` por aba.
`windowOpen` com `userGesture: false` e nenhuma aba depois = o navegador
bloqueou o popup, e o resultado diz isso. Ações novas: `browser.session_tabs`,
`browser.close_tab`, `browser.focus_tab` (`Page.bringToFront`). Domínio da lista
PROIBIDA aberto pelo site é fechado na hora, e o resultado diz isso.

**Headless.** `Target.setDiscoverTargets {discover: true}`; `targetCreated` com
`openerId` de uma aba do gerente → entra em `tabs` com origem `site`, conta no
`max_tabs` e na ociosidade, e fecha no fim do job. É o que acaba com a aba que
vaza RAM até o reinício.

**Driver (rede de segurança).** Cada snapshot compara a lista da sessão com o
registro: aba que sumiu (o usuário fechou, a página se fechou) é marcada morta. Se
era a ativa, a ativa volta para quem a abriu ou para a última viva, e o próximo
passo diz o que houve.

## 5. Paralelo

### 5.1 `ler_abas`: o barato, sem LLM por aba

O caso da loja quase sempre é LER (preço, frete, vendedor). `ler_abas` faz o
`extract` (página inteira ou `busca`) nas abas pedidas ao mesmo tempo e devolve um
bloco por aba. O modelo anota. Custo: uma rodada de LLM, não N.

### 5.2 `paralelo`: subagentes, um por aba

Quando cada aba precisa de INTERAÇÃO (escolher variação, abrir "Especificações",
calcular frete):

```json
{"action": "paralelo", "args": {"tarefas": [
  {"aba": "B", "objetivo": "preço da variação 16GB, frete para o Brasil e vendedor"},
  {"aba": "C", "objetivo": "o mesmo"},
  {"aba": "D", "objetivo": "o mesmo"}]}}
```

- Cada tarefa vira um **driver filho** (a mesma classe, em "modo filho") com o
  seu LLM, a sua aba, o seu caderno e orçamento próprio (padrão 6 passos, teto 10).
- **Concorrência máxima 3** (config). Com 5 abas, rodam 3 e depois 2.
- **O pai espera** (um passo do pai = o `paralelo` inteiro). O pai agindo junto
  com os filhos no mesmo navegador é corrida de graça; o ganho de tempo está nos
  filhos, cuja decisão de LLM (o que demora) se sobrepõe.
- O filho **não aninha** (sem `paralelo`, sem `abrir_aba`), **não faz o
  irreversível** e **não preenche dado protegido**: ao bater em `_check_sensitive`,
  `_check_revisao` ou em `«pks:…»`, ele PARA e devolve "precisa do principal:
  …". Quem decide parar a tarefa e pedir ao Daniel é o pai — as paradas
  (`NeedsConfirmation`, `NeedsRelease`) só existem no pai.
- **Volta**: o caderno de cada filho entra no do pai com a origem (aba B, URL) e
  o fecho do filho vira uma linha. Filho que falhou vira "C: não consegui — <motivo>".
- **Diário**: o principal registra "paralelo: B, C, D" e o resultado de cada um;
  o passo a passo de cada filho vai em `trace-B.md`, `trace-C.md`, na mesma pasta.
- **Painel**: cada aba filha mostra o seu próprio painel (o banner já é por aba);
  o PARE para todos (`STOP_REQUESTED` é global no desktop).

### 5.3 O que precisa existir antes

1. **Desktop concorrente** (§2, `websocket.rs`): sem isto, o `navigate` de um filho
   segura os outros por até 20 s.
2. **Ciclo de vida forçado em toda aba que trabalha escondida** (medido no §12):
   sem ele, a aba escondida para o `requestAnimationFrame`, roda timer a 1/s e
   **não dispara o `IntersectionObserver`** — preço e foto que carregam ao rolar
   nunca aparecem, e o filho leria "carregando…". Com `Page.setWebLifecycleState
   {state: "active"}` + `Emulation.setFocusEmulationEnabled {enabled: true}` na
   aba, ela se comporta como se estivesse na frente. Vale também para a aba de
   origem quando o agente SEGUE uma filha e depois volta (§3.1).

## 6. Segurança (nada afrouxa)

- **Escopo de domínio**: `navigate` e `abrir_aba {url}` seguem a allowlist do job
  (§6.1 do plano do navegador local). Aba aberta pelo SITE é clique, como hoje: o
  domínio dela entra no escopo, e o bloco ABAS marca "outro site".
- **Lista proibida**: aba aberta pelo site para domínio proibido é fechada na hora.
- **Liberação de dados**: o domínio que escopa `«pks:…»` passa a ser o da aba onde
  o `fill` acontece (`ultimo_url_visto` por aba). Hoje é "a última URL vista pelo
  job" — com várias abas, isso preencheria o CEP liberado para a loja na aba de
  outro site.
- **Conferência antes de enviar e cliques irreversíveis**: valem em qualquer aba;
  no filho, viram parada para o pai.
- **Aba do usuário**: nunca é fechada; no fim é só solta (`finalizeTabs` já faz a
  diferença entre `agent` e `user`).
- **Tetos**: no máximo 6 abas vivas por job (config); abrir além disso é recusado
  com a frase dizendo para fechar alguma.

## 7. Persistência e retomada

`browser_session` passa de uma aba para o conjunto:

```json
{"abas": [{"rotulo": "A", "tab_ref": 1976621691, "url": "…", "origem": "usuario"},
          {"rotulo": "B", "tab_ref": 1976622001, "url": "…", "origem": "site", "aberta_por": "A"}],
 "ativa": "B", "tab_ref": 1976622001, "url": "…"}
```

`tab_ref`/`url` continuam iguais à ativa (compatível com quem lê hoje). A retomada
(`continue_job`, continuação por liberação, `respostas_memoria`) reconstrói o
conjunto: abas vivas são reivindicadas de novo; a ativa morta é reaberta pela URL;
as auxiliares mortas são esquecidas.

## 8. Fases

### F0 — Medir (sem código em produção) ✅ 2026-11-02, ver §12

No Edge dele, com a extensão, e no headless:

1. aba aberta por `_blank` e por `window.open` a partir de aba da sessão: chega com
   `openerTabId`? entra no grupo "Neo — automação" sozinha? quanto demora?
2. aba em segundo plano: snapshot, `eval`, clique por CDP, `captureScreenshot`,
   preço carregado preguiçosamente — funciona? com o ciclo de vida forçado?
3. headless: `targetCreated` com `openerId`;
4. o loop em fila do desktop: quanto um `navigate` lento segura um `snapshot` de
   outra aba.

Página de teste própria (link `_blank`, `window.open`, popup que se fecha) para
não depender de loja.

### F1 — Detectar e seguir (resolve o caso de hoje) ✅ código, ver §13

Extensão (`onCreated`/`onRemoved`, lista detalhada), desktop (`abas_novas`,
`session_tabs`, `close_tab`, `focus_tab`, release que fecha auxiliares e deixa a
entregue na frente), headless (alvos do site no gerente), driver (registro de
abas, seguir por padrão, bloco ABAS, `trocar_aba`, `fechar_aba`, volta automática
quando a aba some, estado por aba, nota com origem).

### F2 — Abrir abas por iniciativa própria ✅ código, ver §15

`abrir_aba {url|ref}`, teto de abas, regra no prompt de quando vale abrir (comparar
N itens de uma lista) e retomada com o conjunto de abas (§7).

### F3 — Paralelo ✅ código, ver §16

Desktop concorrente (pré-requisito), `ler_abas`, `paralelo` com driver filho,
diários por filho, entrega mesclada, orçamento total visível no diário.

### F4 — E2E reais

1. **AliExpress BC-250 de novo**: o clique abre B → segue, lê, fecha, volta; ou
   `abrir_aba` × 3 + `ler_abas`/`paralelo`. Fim sem aba órfã, 3 notas com a URL
   de origem, entrega sem "só da busca".
2. **Popup que se fecha** (página de teste): volta sozinho para a origem.
3. **Conclui na nova**: resultado de busca que abre o site noutra aba; `done` na B,
   B na frente, A fechada se era do agente.
4. **Headless**: o caso 1 sem janela, e `headless_stats` sem aba sobrando.
5. **Cinco anúncios com teto 3**: dois lotes.
6. **Liberação na aba B de outro domínio**: o card sai com o domínio da B.

## 9. Testes (unitários, sem rede)

Dublê de `act` com várias abas (o clique devolve `abas_novas`, a sessão perde aba):
segue por padrão; `trocar_aba`; `fechar_aba` volta para quem abriu; aba que some
volta sozinho; aba do usuário não fecha; release fecha auxiliares e entrega a
ativa; escopo da liberação pela aba do `fill`; domínio proibido aberto pelo site
é fechado; filhos em paralelo com LLM falso por filho, teto de concorrência,
parada do filho subindo para o pai, caderno mesclado com origem; retomada
reconstrói o conjunto.

## 10. Riscos

- **Aba escondida estrangulada** (timers, `requestAnimationFrame`, print): é o
  que o F0 decide. Saída de emergência: filhos só com `extract`/`ler_abas` no
  modo com janela.
- **Custo**: 3 filhos × 6 passos são 18 chamadas a mais. Por isso o `ler_abas`
  vem antes, e o orçamento total do job é teto, não sugestão.
- **Modelo se perdendo entre abas**: vocabulário pequeno, a ativa marcada com ▶,
  seguir por padrão, e as ações velhas sem parâmetro de aba.
- **Foco roubado**: seguir a aba nova traz para a frente. No modo com janela é o
  objetivo (ele quer VER); os filhos ficam atrás.
- **O usuário mexe nas abas durante o job**: aba fechada por ele vira morta, e a
  ativa cai para a origem — sem derrubar o job.

## 11. Decisões

- **Seguir** a aba nova por padrão, em vez de ignorar ou de trazer para a mesma
  aba: é o que o clique quis dizer e é o que permite os três desfechos do §0.
- **Pai espera os filhos** em vez de agir junto: o ganho está na sobreposição das
  decisões de LLM dos filhos; o pai agindo junto só traz corrida.
- **Filho sem poder de parar a tarefa**: as paradas que envolvem o Daniel
  (confirmação, liberação, conferência) continuam tendo um dono só.
- **Um job por navegador** continua: o paralelo é dentro do job, não dois jobs
  disputando o mesmo navegador.

## 12. F0 — medido (2026-11-02)

Edge 156, instância própria com perfil temporário e CDP direto, nos dois modos
(janela e headless), contra uma página de teste local: link `_blank`,
`window.open`, popup com features que se fecha em 3 s, e contadores de timer,
`requestAnimationFrame` e `IntersectionObserver`. Scripts em
`scratchpad/f0/medir_cdp.py` e `medir_io.py` desta sessão.

**Como nasce uma aba** (os dois modos dão o mesmo):

| Caso | `Page.windowOpen` na origem | `Target.targetCreated` | URL da nova conhecida | Foco |
|---|---|---|---|---|
| link `_blank` | 12–31 ms, `noopener`, `userGesture: true` | 24–40 ms, `openerId` = origem | 54–86 ms | a nova vai para a frente; a origem fica `hidden` |
| `window.open` no clique | 17 ms | 27–37 ms, `openerId` = origem | 42–64 ms | — |
| popup com features | 9–22 ms | 39–59 ms | 64–79 ms | — |
| popup que se fecha | — | — | — | `Target.targetDestroyed` em 3,1–3,7 s |
| `window.open` sem gesto (`Runtime.evaluate`) | 1–2 ms, `userGesture: false` | **não nasce** (bloqueado) | — | — |

Consequências: `windowOpen` é o gatilho barato (sem ele, nenhum clique paga
espera); a aba existe em menos de 100 ms; o popup que se fecha é detectável; e o
`abrir_aba` não pode ser `window.open` por script — tem que ser `createTab`/
`Target.createTarget`.

**Aba escondida** (a origem depois que a filha veio para a frente):

| | Na frente | Escondida | Escondida + ciclo de vida forçado |
|---|---|---|---|
| `setInterval` de 100 ms | 10/s | **1/s** | 10/s |
| `requestAnimationFrame` | 60/s | **0** | 51–60/s |
| `IntersectionObserver` (preço ao rolar) | dispara | **não dispara** | dispara |
| `setTimeout` de 1,5 s | roda | roda (atrasado) | roda |
| clique por CDP | pega | pega | pega |
| `captureScreenshot` | 180–200 ms | 43–44 ms, conteúdo real | 26–50 ms |
| `visibilityState` / foco | visible / — | hidden / false | visible / true |

Consequências: o paralelo funciona no modo com janela, mas SÓ com o ciclo de vida
forçado em cada aba que trabalha escondida (§5.3); o `look` funciona em aba
escondida.

**Medido pelo caminho, que muda o desenho:**

- **O host nativo do open-browser-use atende UM cliente por vez.** Com o desktop
  conectado, uma segunda conexão ao socket é aceita pelo sistema e nunca
  servida (nem `ping`). Toda ação do navegador local passa pelo desktop — não
  existe "falar com a extensão por fora" para ferramenta nenhuma.
- **O desktop atende os comandos em fila** (`websocket.rs`, lido no código): é
  pré-requisito do F3.

**O que o F0 não mediu:** se a aba aberta pelo site entra SOZINHA no grupo de
abas da sessão no Edge com a extensão (o CDP não enxerga grupos, e o host não
aceita um segundo cliente). Não bloqueia: o F1 agrupa explicitamente no
`chrome.tabs.onCreated` (a extensão só aceita CDP em aba do grupo —
`requireSessionTab`), dê o Edge o comportamento que der.

## 13. F1 — implementada (2026-11-02)

**Extensão** (`neo-browser-local` 0.2.2, 3º apêndice do `background.js`, nada
acima da linha do fork):
- `chrome.tabs.onCreated` com `openerTabId` de aba da sessão → entra no grupo,
  com origem `site` e quem abriu;
- `neoTakeNewTabs` (entrega cada filha uma vez), `neoSessionTabs` (inclui popup
  fora do grupo), `neoCloseTab` (recusa aba de origem `user`);
- `requireSessionTab` embrulhado no prototype para aceitar o popup `site` que
  ficou fora do grupo (janela `popup` não tem tab groups).

Checagem: `node scripts/check-abas-do-site.mjs` (19). O
`check-search-downloads.mjs` estava quebrado desde o 2º apêndice (o recorte ia
até o fim do arquivo e pegava o `chrome.alarms`); o recorte foi corrigido.

**Desktop** (`services/browser_local.rs`, `services/headless.rs`):
- `Conn::request` guarda o `Page.windowOpen` por aba (antes jogava fora);
- `click`, `click_text`, `dblclick` e `press` devolvem `abas_novas` (já
  conectadas) ou `janela_sem_aba`. Sem `windowOpen`, voltam na hora; extensão
  antiga = nada é afirmado (não diz "não abriu" quando só não foi relatado);
- ações `browser.session_tabs`, `browser.close_tab`, `browser.focus_tab`;
- headless: filhas pelo `Target.getTargets` (`openerId`), entram no gerente
  (teto e ociosidade passam a contá-las), `live_tabs` esquece as que sumiram.

Testes: `cargo test --lib` (88) e `-- --ignored` (6, um novo:
`clique_que_abre_aba_devolve_a_filha_e_o_que_nao_abre_volta_na_hora`).

**neo-core** (`core/browser_local/driver.py`, `core/jobs/runner_browser_local.py`):
- registro de abas com rótulos A/B/C; a ativa é o `_tab_ref`;
- o resultado da ação com `abas_novas` → a tarefa SEGUE para a nova (foco,
  estado de tela zerado, domínio no escopo); domínio proibido e passar de 6
  abas → a nova é fechada na hora; `janela_sem_aba` é dito;
- `trocar_aba`, `fechar_aba` (volta para quem abriu; a inicial não fecha);
- entre passos, com 2+ abas, `session_tabs` detecta a que a página fechou e
  volta sozinho, com aviso no prompt;
- bloco ABAS no prompt só com 2+ abas (tarefa de uma aba não muda nada);
- nota lida com 2+ abas leva `[aba B]`; a entrega ganha o rodapé
  "(abas desta tarefa: …)" com o endereço de cada uma;
- no fim, fecha as auxiliares e solta a entregue (a ativa); a inicial fica, salvo
  no headless, onde soltar é fechar.

Testes: `tests/browser_local/test_varias_abas.py` (9, todos falham no driver
anterior), suíte 3713.

**Para valer no navegador dele:** recarregar a extensão à mão
(`edge://extensions` → Neo → recarregar; a versão passa a 0.2.2). O desktop já
foi recompilado pelo `tauri dev`. Sem a extensão nova, tudo continua como antes.

**E2E esperado (o caso 1 do F4):** o mesmo pedido do BC-250 → o clique no anúncio
diz "o clique abriu a aba B… você está NELA"; o agente lê, `fechar_aba`, volta
para a busca; no fim não sobra aba de produto aberta; as notas saem com `[aba B]`
e o rodapé traz os endereços.

## 14. Primeiro E2E (2026-11-02, `session-30dc2798`, job `e8092a4b`)

O mesmo pedido do BC-250 no AliExpress, no Edge dele, com a extensão 0.2.2.
**O F1 funcionou**: cada clique em anúncio abriu aba, o agente seguiu para ela,
leu, `fechar_aba` e voltou para a busca — 8 abas no total (B–H), todas
fechadas no fim, só a busca ficou. 4 anúncios lidos na página do produto, notas
com `[aba B]`…, rodapé com os endereços. 16 passos, 66 s (antes: 13 passos e o
modelo navegando na mão pela URL, com 10 notas para 3 produtos).

Dois defeitos novos, os dois corrigidos no mesmo dia:

1. **O mesmo anúncio aberto três vezes (abas D, E, F).** O agente abriu a D,
   pensou "é uma placa de banda base 5G, não a BC-250" e fechou sem anotar. O
   "não serve" ficou só no pensamento do passo, que não entra no histórico: o
   histórico mostrava "abriu D" e "fechou D", e na busca ele concluiu "foi aberto
   antes e fechado sem anotar — preciso reabri-lo". O freio de repetição não pega
   isso: cada clique MUDA a tela (abre aba). Agora `fechar_aba` aceita `motivo`;
   o prompt lista as abas já fechadas (endereço, notas, motivo) mesmo com uma
   aba viva; e abrir de novo um endereço já visto é avisado no próprio passo.
2. **A entrega negou o que a tarefa fez.** "Não dá pra cravar um preço… quer que
   eu abra as listagens individuais?" — com as 4 páginas já lidas. O redator do
   dispatcher só recebe o FECHO ("Terminei… 4 itens" e "(a aba ficou em
   …/wholesale…)"); itens e rodapé vão por fora dele. Juntou os dois e inventou
   que os preços eram da lista. Agora o fecho ganha "Abri e li N página(s) em
   abas separadas (B, C…): os itens foram lidos nessas páginas…".

Testes: +3 em `test_varias_abas.py` (12 no total; os 3 falham no código do F1).

### 14.1 A correção do item 1 não bastava — medido e refeito (2026-11-02)

Reencenado no container de produção, com o modelo do passo (`deepseek-v4.1-flash`),
o estado e a página EXATOS do passo 7 do job e8092a4b (de volta à busca, depois de
fechar a D), 6 amostras por variante:

| Variante | Reabriu o anúncio errado | Fez o certo (buscar "BC-250 16GB") |
|---|---|---|
| lista "JÁ ABERTAS E FECHADAS" sem motivo (o que a 1ª correção dava) | 6/6 | 0/6 |
| só a marca no elemento | 3/6 | 3/6 |
| motivo no bloco | 0/6 | 6/6 |
| motivo + marca | 0/6 | 6/6 |

A lista sozinha não ligava o `/item/1005011777594358` ao `[30] R$12.217,99` em que
ele ia clicar. O que decide é o MOTIVO; a marca na própria linha do elemento é a
segunda camada (a posição do aviso vale mais que a regra, como no A/B de 31/10).
Na D (passo 6), com a instrução nova, ele deu o motivo 6 de 6 vezes.

Publicado às 18:13 UTC:
- `_marcar_vistos`: a linha do link que aponta para endereço já aberto ganha
  "⟪já aberto na aba D: <motivo>⟫" / "⟪já lido e anotado na aba C — não
  precisa reabrir⟫" / "⟪aberto na aba B — `trocar_aba`…⟫";
- `fechar_aba` numa aba sem nenhuma nota EXIGE `motivo` (o passo é recusado com
  a frase pronta).

Validação final com o código publicado: com motivo, 0/6 reabriram (4 buscaram
outro termo, 1 rolou, 1 procurou na página); sem motivo, 1/6. A mensagem final,
com o fecho novo, disse que leu na página de cada anúncio 5/6 (antes 0/6) e não
atribuiu os preços à busca nenhuma vez (antes 1/6). Gotcha da reencenação: as
notas têm de ser as REAIS — encurtadas para nome e preço, o modelo "completa" o
que achou faltando e reabre o anúncio já lido.

Testes: +2 em `test_varias_abas.py` (14; os 2 falham no código anterior), suíte 3717.

## 15. F2 — implementada (2026-11-02)

**Mudança de desenho em relação ao §3.3:** `abrir_aba` abre em SEGUNDO PLANO e o
agente fica onde está (o clique do meio), em vez de passar para a nova. Motivo
medido no job e8092a4b: toda volta à busca fazia o AliExpress refazer a lista
("pc_back_same_best") e os números dos elementos mudavam. Com as abas abertas a
partir da lista, ela nunca sai do lugar. Abrir e ir já existe: é o clique.

- **`abrir_aba {"ref": 27}`** usa o `href` do elemento do snapshot (relativo →
  `urljoin` com a página); **`{"url": …}`** passa pelo escopo do `navigate`. Recusa:
  elemento que não é link, domínio proibido, teto de abas, endereço já aberto
  ("use `trocar_aba`") ou já visto e fechado (com o motivo).
- **Teto:** 6 abas no navegador dele; **3 no headless**, porque o gerente de abas do
  desktop fecha a mais antiga acima de 3 — e a mais antiga é a lista.
- **Pausa guarda as abas.** Parou para o Daniel (confirmação, liberação): as
  auxiliares não fecham no navegador dele (no headless, fecham — soltar é fechar
  lá). `browser_session["abas"]` guarda o conjunto (rótulo, `tab_ref`, origem,
  quem abriu, motivo, qual era a ativa); `continue_job` e a continuação por
  liberação o levam no spec; o driver (`abas_previas`) o reconstrói com os
  MESMOS rótulos — a ativa é a que foi reivindicada, as outras valem se o
  navegador ainda as tem, as fechadas continuam lembradas.
- **Desktop:** `browser.open_tab` (extensão: `createTab` na sessão + `attach` +
  `navigate`; headless: aba em branco + `navigate`, porque o `createTarget` volta
  antes da página existir — pego pelo teste). `browser.focus_tab` passa a
  reconectar o debugger (a aba de uma tarefa retomada foi solta no `turnEnded`).
- **Extensão 0.2.3:** `claimUserTab` embrulhado para não rebaixar a origem
  `site`/`agent` para `user` na retomada (senão o Neo não fecharia mais a aba que
  a página abriu). Só a retomada depende disto; o resto da F2 funciona com a 0.2.2.

Medido no passo 1 do job e8092a4b com o modelo de produção e o prompt da F2: 6 de
6 vezes o agente começou por `abrir_aba {"ref": 27}`.

Testes: `test_varias_abas.py` 22 (8 da F2, que falham no código da F1), desktop
88 + 7 ignorados (um novo: `open_tab_abre_irma_em_segundo_plano…`), extensão 21,
suíte 3725.

**E2E da F2 (job d77daa59, 19:25 UTC):** o pedido de comparar os 3 anúncios rodou
limpo — `abrir_aba` → `trocar_aba` → anotar → `fechar_aba {motivo}`, três vezes, e
`done`. 11 passos em 37 s (o E2E da F1 levou 16 passos e 66 s); nenhum anúncio
reaberto, a lista não recarregou, nenhuma aba sobrou.

## 16. F3 — implementada (2026-11-02)

**Desktop:**
- `websocket.rs`: cada comando do canal de controle roda na sua tarefa, e as
  respostas saem por uma escritora só (`mpsc`). Em fila, um `navigate` de até 20 s
  numa aba segurava o snapshot de qualquer outra. O core casa a resposta pelo
  `id`, não pela ordem;
- `browser.keep_alive {ligado}`: `Page.setWebLifecycleState active` +
  `Emulation.setFocusEmulationEnabled` (o F0, §12). Teste com navegador real: a
  aba escondida tinha o rAF parado e, com isto, volta a rodar.

**neo-core:**
- **`ler_abas {"abas": [...], "busca"?}`:** `extract` em várias abas ao mesmo
  tempo, com o ciclo de vida forçado durante a leitura. 3000 caracteres por aba —
  medido nas páginas de anúncio do job d77daa59: preço perto do caractere 350,
  vendedor do 600, frete/entrega só entre 1800 e 2350 (com 1500 o frete ficava de
  fora);
- **`paralelo {"tarefas": [{"aba", "objetivo"}], "passos"?}`:** um driver filho por
  aba (o MESMO driver, `filho=True`), até 3 ao mesmo tempo, 6 passos cada (teto
  10); o principal espera. O filho não reivindica, não solta e não sonda downloads
  (interceptado no `_call`); não usa ações de aba, `paralelo` nem `consultar`; o
  irreversível vira "parou antes de «…»" para o principal decidir; no headless ele
  não fecha a aba do principal. As notas dele entram no caderno do principal com
  `[aba B]`; o diário dele é `trace-B.md` na mesma pasta, com prints `B-03-tela.jpg`;
- **freio de giro:** ação de aba bem-sucedida zera a contagem de "mesma tela" —
  abrir/ler/visitar abas não muda a tela da lista, e três anúncios descartados
  seguidos matavam a tarefa por giro (pego pelos testes do `ler_abas`). O
  `_sem_ganho` continua pegando a troca de aba à toa;
- **o caminho curto onde a decisão acontece:** com 2+ abas ainda não lidas, o
  bloco ABAS diz "→ ainda não lidas: B, C, D. Para LER todas de uma vez:
  `ler_abas …`". Medido com o modelo de produção no ponto do job d77daa59 depois
  dos três `abrir_aba`: só com a regra no prompt de sistema, 1–2 de 6 leram tudo
  de uma vez (as outras visitaram uma por uma); com a dica no bloco, 5 de 6.

Testes: `test_varias_abas.py` 31 (os 8 novos da F3 falham no código da F2, salvo o
do "não é giro", que é guarda de regressão), desktop 88 + 8 ignorados, suíte 3734.

## 17. Primeiro E2E da F3 (2026-11-02, `session-69958683`, job `b8648f0e`)

O pedido de comparar os 3 anúncios usou o caminho novo: `abrir_aba` × 3,
`ler_abas` em C e D, e os 3 anúncios saíram completos (preço, frete, prazo,
vendedor) em 12 passos e 46 s. O que saiu estranho, e foi corrigido às 20:08 UTC:

1. **"2 passos falharam".** (a) `aliexpress.com` redirecionou para
   `pt.aliexpress.com`, e o `navigate` para a página em que a aba já estava foi
   recusado "fora do escopo" — o escopo comparava o domínio exato. Agora
   subdomínio do que está no escopo passa (com o ponto antes: `aliexpress.com.golpe.tld`
   não). (b) O modelo anotou os dois anúncios em `anotar` sem dizer a ação; as
   notas entraram, mas o passo contava como falha. Agora é passo de anotação.
2. **`[aba C] [aba A]`.** A nota lida na C pelo `ler_abas` foi escrita com a A
   ativa; o modelo já tinha marcado `[aba C]` e o sistema carimbou a ativa por
   cima. Nota que já tem a marca não ganha outra.
3. **"li 1 página"** (eram 3): a contagem procurava a marca no FIM da nota.
4. **"Ainda rodando…" e o veredito perdido.** A resposta prometeu "te aviso com o
   veredito de qual compensa"; o verificador de promessa rodou a continuação NA
   HORA, com o job andando — ela só pôde consultar o status. Quando o job acabou,
   ninguém ficou encarregado do veredito, e a mensagem final só listou os
   anúncios. Agora: `browser_local_task` aceita `then`; com job aberto, o
   verificador PENDURA a continuação no `then` do job (`_pendurar_no_job`), e o
   JobManager relê o `then` do banco no fim (`_then_pendurado` — o objeto em
   memória é de quando o job começou). No navegador, a mensagem do job NÃO é
   silenciada (os itens são a entrega, íntegros), e a continuação responde só o
   veredito, sem repetir a lista.
5. **Dois `ler_abas` seguidos** (o primeiro só com `busca: "frete"`): a dica diz
   que sem `busca` vem a página com preço, frete e vendedor.

Testes: +3 em `test_varias_abas.py` (34) e `tests/test_continuacao_pendurada.py`
(6), suíte 3743.

## 18. Os pendentes do navegador, resolvidos (2026-11-02, 20:53 UTC)

1. **Sem `start_url`, a URL da tarefa é onde começar** (`_ir_para_a_url_da_tarefa`).
   Job 65e940af: a aba reaproveitada estava na confirmação de OUTRO formulário,
   o agente encerrou em 3 s e o Neo disse "deu tudo certo". Só navega se a aba
   estiver noutro lugar (`_mesmo_lugar`: outro site, ou o mesmo site com outro id
   no caminho — o id do formulário); a que já está no formulário certo não
   recarrega. `www.` não distingue site.
2. **Enter que envia passa pela conferência** (`_check_revisao_enter`): com 2+
   campos preenchidos e um botão de envio na página. Caixa de busca segue. Na
   retomada da conferência, Enter e clique em "Enviar" valem o mesmo envio.
3. **Card que vence encerra a espera** (`encerrar_esperas_vencidas`, varredura de
   60 s no tick do JobManager via `add_periodic`). A liberação vencia
   preguiçosamente e nada avisava a tarefa: ela ficava "esperando você" para
   sempre. Agora termina cancelada, com "⌛ O pedido para liberar … venceu sem
   resposta, então encerrei …" no chat.
4. **`search_pks` escolhe pela forma da pergunta.** 3+ palavras de conteúdo → o
   ranking v2 (com os acertos exatos de nome da busca antiga na frente); pergunta
   curta → a busca antiga, com o v2 de reserva quando ela não acha nada. Medido
   em prod: "treino segunda … exercícios repetições" passou de 5 exercícios do
   antebraço para os 5 treinos de cada dia; "meu cep" continua achando o dono
   (o v2 sozinho achava Academia Exemplo).
5. **Cancelar a tarefa que espera um card encerra o card** (decidido como recusado,
   `source="tarefa_cancelada"`), e a janela de Tarefas mostra "Cancelar" também
   para "esperando você". `continued` entrou nos estados terminais.

Testes: `tests/browser_local/test_pendentes_navegador.py` (11), suíte 3754.

### 18.1 E2E do pedido de variações (job a0c7951a, 21:30 UTC)

Rodou limpo: 3 anúncios abertos em abas, lidos e fechados com motivo, 18 passos em
56 s, nenhum passo falho, B e C fechadas e a D (onde terminou) entregue. O agente
escreveu o veredito no caderno ("MAIS BARATA: anúncio 3, R$1.975,70"), porque a
própria tarefa pedia — o verificador de promessa leu "só aviso de fim" e não
pendurou nada, corretamente.

- **O `paralelo` não foi exercitado:** nenhum dos 3 anúncios tinha variação para
  escolher, então não havia interação por aba e a leitura uma a uma bastou.
- **A mensagem perguntou "quer que eu compare?"** com o veredito no fim da lista:
  o redator não vê os itens. Agora o fecho leva a linha de conclusão da ÚLTIMA
  nota (`_frase_da_conclusao`). Reencenado com o redator real: antes, 0/6
  disseram o veredito (2/6 perguntaram se comparavam); com só "a conclusão está
  no último item", 6/6 pararam de perguntar mas nenhuma disse QUAL; com a linha,
  6/6 disseram "a mais barata é o anúncio 3, a R$1.975,70".
- O cérebro inventou um `continue_job` (`f1c1d3f0…`) na primeira chamada; a
  recusa ("não achei o job para continuar") o fez refazer certo na hora.

## 19. E2E das capacidades (2026-11-02, `session-9224924d`, job `9a3223a4`)

"Abra os 3 primeiros anúncios de SSD Kingston NV3, escolha cada capacidade e anote
o preço; diga qual tem o melhor preço por GB". Era o caso feito para o `paralelo`
(interação em cada aba). A coleta saiu certa (21 passos, 71 s, as 7 combinações
de anúncio e capacidade, nenhuma aba sobrando), mas **o `paralelo` de novo não
rodou**: o agente abriu um anúncio por vez, foi até ele, clicou nas capacidades e
fechou, três vezes.
- A dica "ainda não lidas → `ler_abas`/`paralelo`" exige 2+ abas não lidas, e
  nunca houve duas ao mesmo tempo;
- a regra da F2 no prompt de sistema ("abra cada um com `abrir_aba` e depois
  visite um por um") descreve exatamente o que ele fez.

Corrigido às 22:36 UTC (§19.1).

Dois defeitos, corrigidos às 22:20 UTC:

1. **Link cortado em 300 caracteres.** O snapshot cortava o `href`, e o `abrir_aba
   {ref}` abria a metade: as três abas abriram URLs de exatamente 300 caracteres.
   Na D, o corte caiu num `%xx` (`…110.02%`), e a página veio só com cabeçalho e
   rodapé. Foram 4 passos perdidos (13–16), e o `wait_for` de 10 s virou "1 passo
   falhou"; ele se salvou navegando para a URL limpa. Agora o snapshot manda até
   2000 caracteres e marca `href_cortado` acima disso. O driver abre o link
   marcado sem o último parâmetro (`_sem_parametro_cortado`). O prompt não muda:
   ele já mostrava o link encurtado.
2. **O veredito não chegou à mensagem.** A última nota ("RESULTADO FINAL — melhor
   preço por GB: Anúncio 2 … = R$0,79/GB. Comparativo: …") tem 271 caracteres e
   não casava a regex do `_frase_da_conclusao`. A mensagem abriu com "um passo
   falhou… quer que eu detalhe qual passo falhou?". Agora "melhor preço", "menor
   preço", "melhor custo", "mais em conta" e "veredito" contam, e a linha comprida
   dá a FRASE do veredito em vez de ser descartada. "Resultado final" sozinho
   continua não contando: é título. Reencenado com o redator real e o job do
   banco, 6 amostras por braço:

   | Fecho | Disse o veredito | "Quer que eu…?" |
   |---|---|---|
   | o guardado | 0/6 | 6/6 |
   | com a linha de conclusão | 6/6 | 0/6 |

Testes: +1 em `test_varias_abas.py`, +1 em `test_pendentes_navegador.py`, desktop
88 + 9 ignorados (um novo com Chromium de verdade:
`link_comprido_chega_inteiro_e_o_enorme_vem_marcado_como_cortado`), suíte 3758.
A extensão não precisa de reload: o `neo-map.generated.js` regenerado só alimenta
o inspetor visual.

### 19.1 Abrir todos antes, e então `paralelo` (22:36 UTC)

Reencenado no container de produção com o modelo do passo (`deepseek-v4.1-flash`),
a página real da lista e o estado de cada ponto do job 9a3223a4, 6 amostras por
braço. P4 é a regressão do caso só de leitura (job d77daa59).

| Ponto | Antes | Depois |
|---|---|---|
| P1 · B aberta | `trocar_aba` 6/6 | abre o próximo 6/6 |
| P2 · B, C abertas | `ler_abas` só das duas 4/6 (lê só a capacidade padrão) | abre a D 6/6 |
| P3 · B, C, D abertas | `paralelo` válido 5/6 | `paralelo ×3` válido 6/6 |
| P4 · só leitura | `ler_abas` 5/6 | `ler_abas` 6/6 |

O que mudou:
- **regra do sistema:** "abra TODOS primeiro, um `abrir_aba` por passo, e só depois
  trabalhe neles: LER → `ler_abas`, INTERAÇÃO → `paralelo`". Antes dizia "visite um
  por um";
- **dica com UMA aba não lida**, só quando o agente está na página de onde a abriu:
  "A tarefa pede outros itens desta lista? Abra-os AGORA, antes de visitar a B";
- **dica com 2+ abas** ganhou "falta abrir algum? Abra antes" e o JSON do `paralelo`;
- **o resultado do `abrir_aba` e a marca do link já aberto** perderam o "`trocar_aba`
  vai até ela", que convidava a visitar uma por uma, logo no resultado do último
  passo;
- **argumento solto no topo** (`{"action": "paralelo", "tarefas": […]}`, fora do
  `args`): agora é recolhido no `_validate`. Com a dica nova saía assim 6/6 vezes,
  e o passo era recusado.

Testes: `test_varias_abas.py` 36 (o do argumento solto falha no código anterior),
suíte 3759. Script: `scratchpad/f3_paralelo.py` (sessão 97c6202c) — dois braços,
o novo por remendo em memória, sem publicar; `f3_validar.py` repete os pontos contra
o código publicado.

Falta o E2E real: o mesmo pedido do Kingston NV3, para ver os subagentes de fato
(`trace-B.md`… na pasta do job).

## 20. E2E do `paralelo` (2026-11-02, `session-bd44cc03`, job `e239dddd`)

O mesmo pedido das capacidades do Kingston NV3, com o código do §19.1. **O
`paralelo` rodou**:
- passos 1–3: `abrir_aba` em B, C e D, uma por passo, sem visitar nenhuma;
- passo 4: `paralelo ×3`. Os subagentes começaram juntos (22:51:22.2–22.4). B levou
  4 passos e 16,6 s (um `look` de 8 s confirmou que o anúncio só tem 1TB); C levou
  3 passos e 13,0 s; D, 3 passos e 11,4 s. Cada um tem o seu `trace-X.md` e os seus
  prints;
- passo 5: `done`.

Total: **5 passos do principal e 39 s**, contra 21 passos e 71 s no job 9a3223a4,
com os mesmos preços e o mesmo veredito (anúncio 2, 2TB, R$0,79/GB). B, C e D
fecharam no fim, e a mensagem do job abriu com o veredito (a correção do §19).

O que saiu estranho, ainda sem conserto:
1. **"Ainda rodando…" 4 s depois de "Rodando de novo…".** Não foi o verificador
   de promessa (§17), foi o próprio cérebro: passou `then` ao job E emitiu
   `[[HANDOFF:continuar]]`. A continuação rodou na hora, com o job andando.
   **Corrigido em 03/11, 02:17 UTC** (`AIService._continuar_depois_do_job`): se o
   cérebro escreve o bloco num turno que abriu um job que ainda roda, a ordem vira o
   `then` do job, ou entra junto do que já estava combinado, e sai da fila do turno.
   A entrada do job no registro do turno passa a dizer `then`, para o verificador
   logo depois não sintetizar outra continuação para já. Job que já terminou não
   segura nada. Testes: +4 em `tests/test_continuacao_pendurada.py`, suíte 3763.
2. **A resposta da continuação foi gravada duas vezes:** uma pelo turno cognitivo e
   outra pelo dispatcher, ao entregar o job `scheduled_prompt`. Não é das abas:
   4 de 4 entregas de `scheduled_prompt` desde 14/09 (agenda e continuações) estão
   duplicadas no banco. **Corrigido em 03/11, 02:31 UTC:** o dispatcher não grava o
   `texto_literal` quando a sessão já tem essa resposta, criada depois do início do
   job (`_turno_ja_gravou`). Fica a cópia do turno, que leva `tool_trace` e
   `files`, e a mensagem continua indo ao desktop na hora. E o
   `/desktop/session/{id}/history` não devolve mais a mensagem de usuário com
   `source: schedule`: os 6.000 caracteres de "[Continuação automática…]" eram
   desenhados como bolha do Daniel ao reabrir a conversa. Validado contra o job e a
   sessão reais (datas sem fuso dos dois lados, a cópia é achada). Testes: +2 em
   `test_jobs.py`, `test_historico_sem_texto_da_agenda.py`, suíte 3766. As 4
   duplicatas antigas continuam no banco.
3. **Ruído no caderno** (16 linhas):
   - a resposta do `look` da B virou item ("a tela mostrava: …");
   - a C anotou o 500GB duas vezes (a data de entrega mudou depois do clique);
   - o principal reescreveu os 3 anúncios em 4 notas `[aba A]`.
4. **A mensagem do job terminou com "Quer que eu monte uma tabela…?"**, contra a
   persona.
5. **A interação 1 não rodou nada:** o cérebro leu o caderno da sessão do job
   9a3223a4 e respondeu "Já está feito — rodou hoje mais cedo". O Daniel teve de
   pedir "roda de novo".

Dado que a tarefa não pediu, mas muda o veredito: a página do anúncio 3 mostra
"+R$1.453,33 impostos estimados" no 2TB. O preço por GB comparou valores sem o
imposto, e o anúncio 1 é enviado do Brasil.
