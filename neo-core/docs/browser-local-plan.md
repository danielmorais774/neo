# Plano: Browser Local — o Neo dirige o MEU Chrome, na minha frente

**Data:** 2026-08-25
**Status:** F0–F5 IMPLEMENTADAS (2026-08-26) — ver §12. Falta o passo manual
(carregar a extensão no Brave) e ligar `BROWSER_LOCAL_ENABLED`.
**Depende de:** canal de controle por device (`/ws/devices/control`, em prod),
sistema de jobs (`docs/workbench-background-jobs-plan.md`, F1–F3 em prod) e
ProactiveDispatcher.
**Irmão de:** `docs/browser-agent-plan.md` (browser HEADLESS no servidor). Este
plano NÃO substitui aquele — ele acrescenta um segundo corpo, e metade do
trabalho aqui é a **linha divisória** entre os dois.

## 0. O pedido, em uma frase

Quero poder dizer *"faz isso **no meu navegador**"* e ver o Neo agindo na minha
tela, ao vivo, na aba que eu estou olhando. Se eu **não** disser isso, ele
continua usando o Chromium sem tela do servidor, como já usa hoje.

Escolhas já tomadas (2026-08-25):

- **Extensão:** fork da extensão do [open-browser-use](https://github.com/iFurySt/open-browser-use)
  (MIT) num projeto novo, `neo-browser-local/`, mantendo o contrato de native
  messaging com o CLI `obu`.
- **Loop do agente:** quem dirige é o **neo-core**, passo a passo. O Mac só
  executa primitivas e devolve o estado real da página.

## 1. Estado atual (o que já existe e vamos reusar)

| Peça | Onde | Como serve aqui |
|---|---|---|
| Canal permanente core→máquina, **com resposta correlacionada** | `core/notifications/device_control.py` + `api/routes/devices.py:452` + `neo-desktop-tauri/src-tauri/src/websocket.rs:113` | É o cano. `request(device_id, action, args, timeout)` já aceita timeout por chamada e levanta `DeviceOffline`/`DeviceTimeout` — nunca devolve sucesso sem confirmação do device. |
| Resolução de device + capabilities | `modules/devices/control.py` (`resolve_device`, `_supports`) | Decide QUAL máquina é "o meu navegador" e se ela sabe fazer isso. |
| Jobs + aviso proativo | `core/jobs/manager.py`, `ProactiveDispatcher` | Tarefa de browser dura minutos; o trilho de `kind="browser"` já existe e é copiável para `kind="browser_local"`. |
| Runner de browser | `core/jobs/runner_browser.py` | Molde do runner: credenciais decifradas na borda, prints → File Store, `extra_metadata["images"]`, estados terminais. |
| Prints da tela → bolha do chat | `core/jobs/screenshots.py` (`ingest_shots`) | Reusado inteiro: print local vira `file_id`, o dispatcher põe o marcador. |
| Cofre de credenciais | `core/credentials/browser.py` (`build_browser_credentials`) | Mesma regra: nenhum LLM vê valor em claro. |
| Ponte extensão↔desktop | `neo-desktop-tauri/src-tauri/src/ext_bridge.rs` (porta 17872, neo-music-ext) | Molde comprovado de WS loopback + reconexão + keepalive por `chrome.alarms`. |
| Extensão com loop de IA própria | `neo-browser/` | **Não** é o caminho (§11), mas o inventário de 18 tools dela é a melhor lista de primitivas que já validamos na prática. |

## 2. O gap

1. **Nenhum caminho do servidor até o Chrome que eu enxergo.** O `browser_task`
   dirige um Chromium dentro do container, na VPS. O `media_context` sabe quais
   abas de música existem, mas é leitura, não atuação.
2. **Nenhuma noção de "faz na minha tela".** O roteamento e o cognitivo não têm
   como distinguir "pesquisa isso" de "pesquisa isso no meu navegador" — e sem
   essa distinção o portão do §5 não existe.
3. **Nenhuma primitiva de browser no desktop.** O `ext_bridge` atual fala
   *play/pause de vídeo*, não *clique no elemento 7*.

## 3. Arquitetura

```
 VPS (prod)                                          Meu Mac
┌──────────────────────────────┐                  ┌──────────────────────────────────┐
│ cognitivo                    │                  │ neo-desktop-tauri                │
│   tool browser_local_task ───┼── agent_job ──┐  │   websocket.rs (canal já aberto) │
│   ▲ PORTÃO do §5             │  kind=        │  │        │                         │
│   │                          │  browser_local│  │        ▼                         │
│ JobManager                   │               │  │   services/browser_local.rs      │
│   runner_browser_local ──────┼───────────────┼──┼──▶ (novo)                        │
│     BrowserLocalDriver       │  /ws/devices/ │  │        │ stdio/WS loopback       │
│       loop: snapshot→LLM→ação│  control      │  │        ▼                         │
│       (mesmo LLM do          │  request()    │  │   obu CLI (native host)          │
│        browser-use, na VPS)  │  ◀── ok/erro ─┼──┼──┐     │ native messaging        │
│   ProactiveDispatcher ───────┼── "terminei" ─┼──┼─▶│     ▼                         │
└──────────────────────────────┘               │  │  neo-browser-local (extensão)    │
                                               │  │   fork MIT + overlay do Neo      │
                                               │  │        │ chrome.debugger / CDP   │
                                               │  │        ▼                         │
                                               │  │   ABA QUE EU ESTOU VENDO         │
                                               └──┴──────────────────────────────────┘
```

Papéis, sem ambiguidade: **o core pensa, o Mac executa, a extensão toca no DOM.**
O Mac nunca decide o próximo passo; a extensão nunca fala com LLM nenhum.

### 3.1 `neo-browser-local/` — a extensão (fork MIT)

Projeto novo na raiz, irmão de `neo-browser/` e `neo-music-ext/`. Base: a
extensão do open-browser-use, vendorizada com o `LICENSE` MIT preservado e um
`UPSTREAM.md` anotando commit de origem e o que divergiu.

O que o fork ganha por cima do upstream (o motivo de forkar em vez de instalar a
deles):

- **Overlay "o Neo está agindo"**: faixa fixa no topo da aba reivindicada, com o
  passo atual em texto ("clicando em *Entrar*"), e o realce do elemento antes do
  clique. É a razão de ser do modo local — eu quero *ver*.
- **Botão PARE** no overlay: aborta o job em curso. Mesma família do barge-in da
  stop word (`neo-stop-word-barge-in`) — freio que não depende de LLM.
- **Badge de conexão** no ícone (verde/cinza), copiado literalmente do
  `neo-music-ext/background.js`: já provou que resolve "está ligado?".
- **Marca da aba reivindicada**: um `tab_ref` visível, para eu saber qual aba é
  do Neo quando tenho 30 abertas.

O que o fork **não** muda: o contrato de native messaging com o `obu` CLI. É o
que mantém `npm update -g open-browser-use` valendo e o que evita reimplementar
claim de aba, ciclo de vida do MV3 e acesso ao CDP.

Instalação: o fork tem `key` fixa no `manifest.json` (gerada em `keys/`), então
o ID é sempre `onleolnecjhegkhdkmhchenfljnbijce` — isso importa porque o
manifesto do host nativo lista o ID em `allowed_origins`.

**Achado da F0:** não há Chrome nesta máquina — o alvo é o **Brave**. E o
`obu setup` do upstream só conhece Google Chrome / Chrome Beta. Daí
`neo-browser-local/scripts/setup.sh`, que roda `obu install-manifest
--extension-id <o nosso> --output <dir>` para Brave, Chrome, Edge e Chromium.
Já executado nesta máquina (§12).

### 3.2 `services/browser_local.rs` — o braço no desktop

Novo serviço no Tauri, irmão de `services/device_control.rs`. Recebe as ações do
canal de controle (§3.3), traduz para o `obu` e devolve o resultado **cru**.
Zero inteligência: se a ação falhar, ele reporta a falha; não tenta outra coisa.

Como fala com o `obu` — **a F0 achou um caminho melhor que as três opções
previstas** (MCP stdio / CLI one-shot / WS próprio): o host nativo **já publica
um unix socket JSON-RPC**. Ele anuncia o caminho em
`/tmp/open-browser-use/active.json` (`{"socketPath","pid","startedAt"}`), e o
enquadramento é `u32` no endianness nativo + JSON — o mesmo que os SDKs deles
usam. O Rust fala direto nesse socket: sem subprocesso, sem npm, sem
reimplementar native messaging.

Métodos do backend (o que o `obu mcp` chama por baixo): `ping`, `getInfo`,
`createTab`, `getTabs`, `getUserTabs`, `getUserHistory`, `claimUserTab`,
`attach`, `detach`, `executeCdp`, `moveMouse`, `waitForFileChooser`,
`setFileChooserFiles`, `waitForDownload`, `downloadPath`, `readClipboardText`,
`writeClipboardText`, `finalizeTabs`, `nameSession`, `turnEnded`.

**O que o upstream NÃO tem** (e por isso é nosso): não existe `snapshot` de
elementos interativos nem `click`/`fill` por referência — só `page_info` (título,
url, texto) e `executeCdp` cru. Então as primitivas do §3.5 são **composições de
CDP escritas em Rust** (`Runtime.evaluate` com um caminhador de DOM que numera os
elementos e guarda a lista em `window.__neo.refs`). Fica no desktop, e não na
extensão, por dois motivos: mantém o fork pequeno (merge com o upstream continua
trivial) e põe a lógica onde dá para testar.

O desktop declara `capabilities["browser_local"]` (`services/device_control.rs`),
mas **o core não usa isso como trava**, e a razão é boa: `capabilities` só é
gravada no PAREAMENTO (`pair_device` → `POST /devices/pair`), e não há caminho
para atualizá-la depois — um device já pareado nunca declararia a capability
nova, e `_supports` trata chave ausente como `False`. A verdade, então, é o
`browser.health` a cada job: o navegador pode estar fechado mesmo com o desktop
aberto, e essas são falhas diferentes que o usuário precisa distinguir (§5.4).
(`db.set_device_capabilities` existe e continua sem chamador — é por onde um
refresh de capabilities entraria, se um dia valer a pena.)

### 3.3 O cano: `/ws/devices/control`, sem canal novo

Ações viajam no envelope que já existe:

```
core → device   {"id":"c-7f3a","action":"browser.snapshot","args":{"tab_ref":"t1"}}
device → core   {"id":"c-7f3a","ok":true,"result":{"url":"…","elements":[…]}}
```

Por que não um canal novo: este já é autenticado por token, já é full-duplex, já
tem correlação e já está de pé nos dois lados. O `request()` aceita timeout por
chamada — `browser.click` usa 8s, `browser.navigate`/`wait_for` usam 30s.

Duas coisas que **precisam** ser acrescentadas no core:

- **Payload maior que comando de volume.** Um `snapshot` traz dezenas de
  elementos; um `screenshot` traz base64. Definir teto (`snapshot` ≤ 64KB por
  truncagem do número de elementos; print sempre em JPEG q70 ≤ 400KB) e medir na
  F0 — se o WS engasgar, o print sobe por HTTP autenticado e o WS carrega só o
  `file_id`.
- **Cancelamento.** `PARE` no overlay e cancelamento do job precisam abortar o
  passo em voo. Hoje `request()` só tem timeout; entra um `browser.abort` que o
  driver dispara ao ver o job cancelado.

### 3.4 `BrowserLocalDriver` — o loop, no core

Um passo do loop:

1. `browser.snapshot` → url, título e a lista de elementos interativos indexados
   (`ref`, papel, texto, estado). É o "ver a página".
2. Monta o prompt: tarefa + contexto + histórico curto de passos + snapshot.
   LLM na VPS (mesmo provedor do `browser_task`, `disable_thinking` conforme
   `openrouter-reasoning-default-on` / `deepseek-v4-thinking-tool-choice-required`).
3. A saída é **uma** ação do vocabulário do §3.5 — validada por schema antes de
   sair. Ação fora do vocabulário ou `ref` que não está no snapshot = passo
   inválido, não vira comando.
4. `request()` no device. O resultado real volta e entra no histórico.
5. Repete até `done`, `max_steps` (default 25 — teto menor que o headless: cada
   passo é uma ida-e-volta e eu estou olhando), abort ou timeout do job.

**Verdade do resultado vem do device, nunca da narrativa do LLM.** É a lição da
skill HA (`neo-ha-false-success-invented-entity`): "cliquei em Entrar" só é
verdade se o `ok:true` do `browser.click` disser. O `result_summary` do job é
montado a partir do histórico de `ok`, e um passo que falhou aparece no texto.

### 3.5 Vocabulário de ações (o contrato)

| Ação | Args | Volta |
|---|---|---|
| `browser.tabs` | — | abas abertas (id, url, título, ativa) |
| `browser.claim` | `url?`, `tab_id?` | `tab_ref` (reivindica a aba ou abre uma nova) |
| `browser.snapshot` | `tab_ref` | url, título, elementos interativos indexados |
| `browser.navigate` | `tab_ref`, `url` | url final + status |
| `browser.click` | `tab_ref`, `ref` | ok + url resultante |
| `browser.fill` | `tab_ref`, `ref`, `value` \| `secret_label` | ok |
| `browser.select` / `browser.press` / `browser.scroll` | `tab_ref`, … | ok |
| `browser.extract` | `tab_ref`, `ref?` | texto |
| `browser.wait_for` | `tab_ref`, `ref`\|`text`, `timeout` | ok/timeout |
| `browser.screenshot` | `tab_ref` | JPEG base64 (ou `file_id`, §3.3) |
| `browser.release` | `tab_ref` | solta a aba, tira overlay, destaca o debugger |

Mais duas que nasceram na implementação: `browser.health` (o `getInfo`, usado
para saber se dá para prometer antes de prometer) e `browser.banner` (atualiza a
faixa do Neo na página, via `window.postMessage` para o content script).

**Credenciais (`secret_label`) ficaram PENDENTES.** O `browser.fill` recebe
`value` em claro no envelope do canal (que é TLS + token, mas ainda assim). O
gancho do cofre — decifrar na borda como o `runner_browser` faz com
`build_browser_credentials` — é o próximo pedaço; hoje o driver simplesmente
nunca ecoa o `value` no histórico de passos (é o que o teste
`test_valor_digitado_nunca_aparece_no_historico` trava), mas isso protege o
LOG, não o transporte. Enquanto isso: login manual, o Neo continua de onde
você parou (a sessão do navegador é a sua, já logada).

## 4. O que o usuário vê

Um turno completo, do meu lado:

```
eu   › "no meu navegador, entra no portal da NFSe e baixa a última nota"
neo  › "Beleza — vou fazer no seu Chrome, dá pra acompanhar. Te aviso quando terminar."
       [aba ganha a faixa: "Neo: navegando para nfse.exemplo.gov.br"]
       [faixa: "Neo: preenchendo Usuário"] …
neo  › "Baixei a nota 2026/0001 (R$ 100,00). 📸 2 prints da tela em anexo."
```

O `browser_local_task` volta na hora com o `job_id` (não trava a conversa) e o
aviso final vem pelo ProactiveDispatcher — exatamente o UX do `browser_task`.

## 5. O portão: só no local se eu pedir explicitamente

Esta é a parte que mais erra na prática, então são **três camadas** e uma
medição, não uma frase no prompt.

### 5.1 Camada 1 — descrição da tool

`browser_local_task` entra em `JOB_TOOLS` com descrição que começa pela
restrição, não pelo poder:

> "**Só use se o usuário pedir EXPLICITAMENTE o navegador dele/local** ('no meu
> navegador', 'no meu Chrome', 'aqui na minha máquina', 'quero acompanhar').
> Para qualquer outra tarefa de browser use `browser_task`, que roda no
> servidor. Na dúvida, `browser_task`."

E o `browser_task` ganha a contraparte ("se o usuário pediu no navegador DELE,
use `browser_local_task`") — senão o modelo aprende só metade da regra.

### 5.2 Camada 2 — guarda determinística (a que de fato decide)

Antes de enfileirar, no mesmo ponto onde as tools de job são despachadas:

```python
def _local_browser_requested(user_message: str, history: list[dict]) -> bool:
    """O usuário pediu o navegador DELE, com todas as letras, NESTE turno?"""
```

Regra em duas partes, no molde já usado em `_file_path_is_grounded` (afordância
∧ âncora, `neo-guard-heuristics-measure-on-prod-corpus`):

- **âncora de posse**: "meu/minha/aqui/nesta máquina/na minha tela/local";
- **afordância de browser**: "navegador/chrome/browser/aba/janela", ou verbo de
  acompanhamento ("ver/acompanhar/assistir") + tarefa de browser.

Comportamento:

| Modelo escolheu | Guarda diz | Resultado |
|---|---|---|
| `browser_local_task` | pediu local | segue |
| `browser_local_task` | **não** pediu local | **rebaixa** para `browser_task` (servidor), loga `[browser-gate] rebaixado`. Silencioso: o usuário só queria a tarefa feita. |
| `browser_task` | pediu local | **promove** para `browser_local_task`, loga `[browser-gate] promovido` |
| `browser_task` | não pediu | segue |

A promoção existe porque o erro simétrico é igualmente ruim: eu pedi para ver e
o Neo foi fazer escondido no servidor.

**Escopo do "explicitamente": vale por turno, não para sempre.** Continuações
("agora ordena por preço") herdam o modo do job anterior **da mesma sessão** se
foram nos últimos ~10 minutos — sem isso o follow-up cai no servidor e perde a
aba que eu estou olhando.

### 5.3 A medição (antes de ligar)

Rodar a guarda contra o corpus real de mensagens do Postgres de prod, no molde de
`scripts/eval_router.py` → `scripts/eval_browser_gate.py`. Critério de aceite:

- **zero** falsos positivos em pedidos genéricos de browser/pesquisa (o custo de
  um FP é o Neo tomar conta do meu Chrome sem eu pedir);
- ≥ 90% de recall na lista de frases explícitas que eu de fato uso.

Ajuste é na lista de âncoras, com o número medido na mão — não no chute.

**Medido em 2026-08-26** (`docker exec neo-core python scripts/eval_browser_gate.py`):

```
corpus: 1672 mensagens de usuário
  parecem tarefa de navegador/pesquisa: 104
  o portão marcaria como LOCAL: 5 (0.30% do corpus)
recall nas frases explícitas: 10/10 (100%)
falsos positivos na bateria de negativos: 0
```

As 5 marcadas, todas verdadeiras (são mesmo sobre o navegador dele):
"quais abas do youtube esta aberta no meu pc" (×3), "tem uma aba do youtube
com breaking benjamin aberto no meu navegador", "voce vai precisar abrir um aba
no meu navegador". **Zero falso positivo** — critério do plano batido.

### 5.4 Quando não dá

- **Desktop offline / `obu` fora do ar** (`DeviceOffline`, ou capability
  ausente): a tarefa **não** cai calada para o servidor. O Neo diz:
  *"Seu navegador local não está conectado agora. Quer que eu faça no meu
  navegador do servidor?"* — porque eu pedi local por um motivo (ver, ou usar
  um login que só existe ali).
- **Mais de um device pareado**: `resolve_device` já pergunta qual
  (`DeviceResolutionError` com candidatos). O default é o device da conversa.
- **`browser_local_task` pelo roteador, não pelo cognitivo**: a tool fica
  **inline no cognitivo**, como o `browser_task` — e com o teste que faltou em
  `neo-job-tools-dead-on-server-tool-path` ("Todas as ferramentas falharam":
  módulo `jobs` no registry global sem executor).

## 6. Segurança e freio

O modo local é mais perigoso que o headless: é o meu Chrome, com as **minhas**
sessões logadas (banco, email, tudo).

1. **Allowlist de domínio por job.** O driver recusa `navigate` para fora dos
   domínios do job. Default: o domínio do primeiro `claim`/`navigate` + o que a
   tarefa citar. Fora disso, o job para e pergunta.
2. **Ação irreversível pede confirmação.** Comprar, pagar, enviar, excluir,
   publicar: o driver detecta pelo texto do elemento e devolve um turno não-final
   pedindo OK — mesmo trilho de aprovação do `command_request`.
3. **Domínios proibidos**, sempre, sem exceção configurável no LLM: internet
   banking e o painel do provedor. Lista em config.
4. **PARE** no overlay e cancelamento do job → `browser.abort` + `browser.release`.
5. **Uma aba, marcada.** O Neo age só na aba reivindicada. Nada de varrer 30 abas.
6. **`browser.release` no fim, sempre** (inclusive em falha/timeout) — deixar o
   `chrome.debugger` grudado é o jeito de a aba ficar estranha depois.
7. **Credenciais**: §3.5. Nem prompt, nem `spec_json`, nem `job_logs`.

## 7. Fases

### F0 — Spike do open-browser-use (medir, não supor) ✅

Sem escrever nada do Neo. No Mac:

- instalar (`npm i -g open-browser-use`, `open-browser-use setup`), carregar a
  extensão e provar o caminho feliz manualmente;
- **inventariar o contrato real**: `obu mcp` — quais tools, que args, que
  formato de retorno; o que `claim`/CDP/`action plans` fazem de fato;
- responder as três perguntas que travam o resto: (a) existe algo equivalente a
  `snapshot` de elementos interativos, ou tem que ser construído em CDP no fork?
  (b) `fill`/`click` por referência de elemento existem? (c) latência por ação
  no caminho `stdio → native messaging → extensão`?
- medir tamanho de um `snapshot` e de um print (decide o §3.3).

**Aceite:** documento curto com o vocabulário real do `obu` mapeado contra a
tabela do §3.5, marcando o que é upstream e o que o fork precisa acrescentar.
**Sem isso as fases seguintes são chute.**

### F1 — O trilho ponta a ponta, com uma ação boba ✅ (código; falta o E2E manual)

O objetivo é provar VPS→Mac→Chrome, não fazer o agente esperto.

- `neo-browser-local/` criado (fork + `UPSTREAM.md` + `LICENSE`), badge de conexão;
- `services/browser_local.rs` + capability `browser_local`;
- `browser.tabs`, `browser.claim`, `browser.navigate`, `browser.extract` no
  vocabulário do canal;
- tool `browser_local_task` que, nesta fase, só faz: abre a URL e me diz o título.

**Aceite:** *"abre o github no meu navegador e me diz o título da página"* →
a aba abre na minha frente e o Neo responde o título certo. Com o desktop
fechado, a mensagem do §5.4 aparece (não cai calado para o servidor).

### F2 — Portão (o pedido do §0) ✅

- guarda `_local_browser_requested` + rebaixamento/promoção + herança de
  continuação;
- `scripts/eval_browser_gate.py` e a medição do §5.3;
- testes dos quatro quadrantes da tabela + o teste do caminho do roteador.

**Aceite:** os números do §5.3 batidos e registrados aqui na §12.

### F3 — Primitivas de verdade + overlay ✅ (código; falta o E2E manual)

- `snapshot` (o que a F0 disser: upstream ou CDP no fork), `click`, `fill`,
  `select`, `press`, `scroll`, `wait_for`, `screenshot`, `release`;
- overlay do Neo: faixa com o passo atual, realce do elemento, botão PARE;
- `browser.abort`.

**Aceite:** login manualmente guiado num site meu, com print no fim, e o PARE
interrompendo no meio de um passo.

### F4 — O loop (`BrowserLocalDriver`) + job ✅ (código; falta o E2E manual)

- `kind="browser_local"` no `JobManager`, `runner_browser_local` no molde do
  `runner_browser` (prints → File Store, estados terminais, `_with_shots`);
- driver com validação de schema por passo, `max_steps=25`, histórico de passos;
- progresso em `progress_note` e aviso proativo no fim.

**Aceite:** *"no meu navegador, procura X no site Y e me diz o preço"* resolvido
sozinho, com o print anexado — e um caso de falha reportado como falha (nunca
"consegui" sem `ok:true`).

### F5 — Segurança ✅ exceto credenciais por rótulo

Allowlist de domínio, confirmação de ação irreversível, domínios proibidos,
credenciais por `secret_label`, `release` garantido no `finally`.

**Aceite:** tentativa de sair da allowlist para o job; "comprar" pedindo OK.

## 8. Config nova

```
BROWSER_LOCAL_ENABLED=false            # liga a tool browser_local_task
BROWSER_LOCAL_MODEL=deepseek-v4-flash  # quem decide o passo
BROWSER_LOCAL_MAX_STEPS=25
BROWSER_LOCAL_JOB_TIMEOUT=600
BROWSER_LOCAL_BLOCKED_DOMAINS=…        # bancos, painel do provedor
BROWSER_LOCAL_CONFIRM_SENSITIVE=true
```

**Sem `NEO_` na frente** — e isso custou um deploy: o `Settings` não tem
`env_prefix`, então o nome da env var é literalmente o nome do campo
(`browser_local_enabled`), como no irmão `BROWSER_AGENT_ENABLED`. Com
`BROWSER_LOCAL_ENABLED` o pydantic ignora a linha em silêncio e o default
(`False`) vence — o container sobe com a variável no `printenv` e a feature
desligada mesmo assim.

Default **off**, como todo caminho que atua para fora (`device_power` é o
precedente). Lembrete de topologia (`neo-dev-topology-prod-tunnel`): o container
lê o `.env` da **raiz**, e salvar o arquivo já é deploy.

## 9. Testes

- **Guarda** (o coração): quatro quadrantes do §5.2, herança de continuação,
  corpus do §5.3.
- **Driver**: ação fora do vocabulário → recusada; `ref` que não está no
  snapshot → recusada; `ok:false` do device **não** vira sucesso no
  `result_summary` (o teste anti-HA); abort no meio.
- **Canal**: `DeviceOffline` e `DeviceTimeout` viram mensagem do §5.4, não stack
  trace; payload grande truncado sem derrubar o WS.
- **Roteador**: `browser_local_task` acionada pelo cognitivo não morre em "Todas
  as ferramentas falharam".
- **Manual (E2E)**: F1, F3 e F4 têm aceite manual — é o único jeito de verificar
  "eu vi acontecer".

## 10. Riscos e gotchas conhecidos

- **MV3 mata o service worker.** O `neo-music-ext` já resolveu: `chrome.alarms`
  a cada 0.4min + reconexão. Copiar, não redescobrir.
- **`chrome.debugger` mostra a barra "está sendo depurado"** e briga com o
  DevTools aberto. É o preço de não relançar o Chrome com
  `--remote-debugging-port`; documentar, não tentar esconder.
- **ID da extensão precisa ser estável** — o native host do `obu` registra o ID.
  Fork sem chave fixa = quebra a cada recarga.
- **Assinatura/TCC não se aplica aqui**, mas o gotcha irmão sim: nada de usar o
  certificado *Apple Development* da empresa em nada do Neo
  (`neo-notch-mic-tcc-signing`).
- **Timeout do canal**: o default de 5s do `device_control` é curto para
  browser. Passar timeout explícito em toda chamada — esquecer isso vira
  `DeviceTimeout` num `navigate` normal.
- **Latência por passo** é o risco de projeto do modo "core dirige". Se a F0
  medir algo como > 500ms por ação no caminho local, o loop de 25 passos fica
  perceptivelmente lento e vale reavaliar o §11.2.
- **Página muda embaixo do snapshot** (SPA). O `ref` precisa ser revalidado no
  momento do clique — o device devolve `ok:false, error:"stale"`, e o driver
  tira novo snapshot em vez de insistir.

## 11. Decisões e alternativas descartadas

1. **Extensão própria do zero** (molde `neo-music-ext`): controle total, mas
   reimplementa claim de aba, CDP, ciclo do MV3 e o native messaging. O fork dá
   os mesmos ganhos de UI com uma fração do trabalho. *Descartado.*
2. **Agente rodando no Mac** (SDK Python do `obu`): menos latência por passo,
   mas duplica prompt, credenciais e observabilidade que já existem no core, e
   coloca chave de LLM na minha máquina. *Descartado — reavaliar só se a F0
   medir latência proibitiva.*
3. **Reusar o `browser-use` do container via CDP tunelado**: seria zero código de
   agente, mas o CDP do `obu` é por aba (`chrome.debugger`) e o `browser-use`
   espera endpoint de browser inteiro; e expor CDP pela rede é dar controle total
   do meu Chrome a quem alcançar a porta. *Descartado.*
4. **Reusar `neo-browser/`** (a extensão com loop de IA próprio): o loop dela é
   dela, com chave própria e sem cofre, sem job, sem aviso proativo, sem o
   portão. Seria um segundo cérebro. *Descartado — mas o inventário de tools dela
   é insumo para o §3.5.*
5. **Canal WS novo core→extensão** (extensão fala direto com a VPS): teria que
   nascer com auth, pareamento e reconexão próprios. O `/ws/devices/control` já
   tem tudo isso e mantém a extensão em loopback. *Descartado.*
6. **Um só `browser_task` com parâmetro `where="local"`**: tentador, mas junta
   duas descrições contraditórias na mesma tool e o portão do §5 perde o alvo.
   Duas tools, cada uma com sua restrição na primeira linha. *Descartado.*

## 12. Registro de execução

### 2026-08-26 — F0 a F5 implementadas

**Onde ficou o código**

| Peça | Arquivo |
|---|---|
| Extensão (fork MIT) | `neo-browser-local/` — `manifest.json`, apêndice no `background.js`, `content-overlay.js` (novo), `scripts/setup.sh`, `UPSTREAM.md` |
| Braço no desktop | `neo-desktop-tauri/src-tauri/src/services/browser_local.rs` (738 linhas) + dispatch em `websocket.rs` + capability em `device_control.rs` |
| Portão | `neo-core/core/browser_local/gate.py` + `_gate_browser` em `core/jobs/executor.py` |
| Loop | `neo-core/core/browser_local/driver.py` |
| Job | `core/jobs/runner_browser_local.py`, `kind="browser_local"` no `manager.py`, wiring em `api/main.py` |
| Tool | `browser_local_task` em `core/jobs/tools.py` (+ contraparte na descrição do `browser_task`) |
| Config | `browser_local_*` em `shared/config.py`; bloco no `.env` da raiz, **default off** |
| Testes | `tests/browser_local/` — 47 testes, todos passando |
| Medição | `scripts/eval_browser_gate.py` |

**Verificado**

- `cargo check` limpo no desktop.
- 47 testes novos passam; suíte inteira: **1397 passed, 3 failed** — as 3 falhas
  são de `tests/birra/`, causadas por `NEO_BIRRA_ENABLED=true` no `.env` de prod
  (os testes assumem defaults), **não** por este trabalho.
- neo-core recarregou em produção sem erro de import; `browser_local_task`
  aparece no `JOB_TOOLS` e a flag lê `False`.
- Portão medido contra o corpus real (§5.3): 0 falso positivo, 100% de recall.

**Decisão que mudou em relação ao plano**

A ponte com o `obu` não é MCP stdio nem CLI one-shot: é o **unix socket
JSON-RPC** que o host nativo já publica (§3.2). E as primitivas (`snapshot`,
`click`, `fill`…) são composições de CDP no Rust, porque o upstream não as tem
(§3.2) — o que deixou o fork da extensão pequeno: só a faixa, o PARE, o badge e
a chave do ID.

### 2026-08-26 (noite) — ligado e braço validado na página real

Extensão carregada no **Microsoft Edge** (não no Brave — o `setup.sh` já
registrava os dois) e confirmada como a NOSSA: o manifesto do host nativo
autoriza só `onleolnecjhegkhdkmhchenfljnbijce`, e ela conectou.
`BROWSER_LOCAL_ENABLED=true`, container recriado,
`✅ Browser LOCAL runner wired (model=deepseek-v4-flash)`.

Dois defeitos que só apareceram testando de verdade (`obu cdp` rodando o JS
extraído do próprio `browser_local.rs` contra `github.com`):

1. **Env var com prefixo errado** — `NEO_BROWSER_LOCAL_ENABLED` era ignorada em
   silêncio (§8). O container subia com a variável no `printenv` e a feature
   desligada mesmo assim. Custou um deploy inteiro para aparecer.
2. **37% dos elementos sem rótulo** (36 de 98 na home do GitHub): botão de ícone
   e link que embrulha SVG. Elemento sem nome é ruído — o modelo não tem como
   escolher "clica no [4]". `NEO.label` ganhou `aria-labelledby`, `textContent`
   (pega `.sr-only`, que o `innerText` descarta), `svg title`/`img alt` de filho
   e, para link, o caminho da URL. **Depois: 0% sem rótulo**, payload +8%
   (10.2KB → 10.9KB, longe do teto de 64KB).

Validado na página real: snapshot (98 elementos), clique pelo `ref` com rótulo
certo ("Dashboard"), e a **faixa sobrevivendo à navegação** — que é exatamente a
razão de ela ser content script e não `<div>` injetado por CDP (§3.1).

### 2026-08-27 — a falha que o portão NÃO pegava: promessa sem tool

Primeiro teste real (sessão `d01cb5ab`, interações 3 e 4). Pedido: *"abra o
painel do simple nacional no meu local"*. A tool estava disponível (todas as
flags `True`). Resposta:

> "Vou iniciar a tarefa, fica de olho que você vai acompanhar na tela.
> Abrindo o painel do Simples Nacional no seu navegador agora."

**Zero tool calls, 1 iteração.** Cobrado (*"nao aconteceu nada"*), dobrou:
*"a tarefa foi enfileirada — assim que terminar, te aviso"*. Nada rodou nas duas
vezes.

**O portão do §5 é cego para isso por construção**: ele reescreve a tool
ESCOLHIDA (rebaixa/promove), e aqui não houve escolha nenhuma. É a doença da
regra 8 ("execute, não ofereça" — `neo-brain-shell-actuation`), agora no
navegador; e a mais cara de todas, porque o usuário fica OLHANDO a tela.

Três correções:

1. **Guarda anti-promessa de navegador** (`_promises_browser_action`, em
   `ai_service.py`): AFORDÂNCIA (promete abrir / alega ter enfileirado) ∧ ÂNCORA
   (é o navegador DELE). Dispara um round extra com tool obrigatória. Medida
   contra **1738 respostas reais**: 3 disparos, sendo 2 as mensagens exatas
   acima; o terceiro é o fluxo de música, que ATUA de verdade e sai pela
   checagem de tool usada (`_BROWSER_ACTING_TOOLS`).
2. **A anti-promessa genérica não pegava** porque a lista de verbos dela é de
   ENTREGA (zipar/baixar/mandar) e "foi enfileirada" é alegação no PASSADO —
   forma que nenhuma guarda cobria. E `browser_local_task` **faltava** em
   `_LEGITIMATE_DEFERRAL_TOOLS`: sem isso, o "te aviso quando terminar" do
   caminho que FUNCIONA seria punido.
3. **Regra no prompt** (`context_selector.py`, só com a flag ligada): chame
   `browser_local_task` no mesmo turno; nunca diga "vou abrir"/"foi enfileirada"
   sem ter chamado.

E o portão ganhou **"no meu local"** — que é como o usuário escreve de fato
("local" sozinho continua fora: "o restaurante local", "o local do evento").
Re-medido: 8 marcadas em 1684 mensagens, **todas verdadeiras**, recall 12/12.

Suíte: **1414 passed** (as mesmas 3 falhas de `tests/birra/`, do `.env` de prod).

### 2026-08-27 (sessão 91fae2d1) — FUNCIONOU, e a guarda se provou

Primeiro E2E de verdade pelo chat. Dois jobs `browser_local` **completed**:

| Job | Resultado |
|---|---|
| `9c63dca8` Abrir a rede social | "A rede social já está aberta no navegador local (a aba ficou em /feed/)" |
| `43bd3afd` Abrir Kick e listar lives | "Canais ao vivo agora na home da Kick: Locobaltar, Kodhak, giganterichard, SUPERXANDAO, Gaules, Delega, SHEVIII2K, DDG, ovotz, AxT, …" |

O segundo é o teste que importa: o driver navegou, **leu a página e extraiu dado
real** — não só abriu a aba. E ele só existiu porque a **guarda anti-promessa
disparou**: o modelo tinha respondido "Vou abrir a Kick no seu navegador agora"
sem tool; o log registra `Anti-promessa (navegador)` e, no round forçado, o
`browser_local_task` saiu com a tarefa certa.

**Mas a guarda vazou em dois turnos** — "Vou **colocar** a live do Kodhak no seu
navegador" e "vou **forçar** a abertura" não dispararam, porque a v1 enumerava
verbos (`abrir/acessar/entrar/iniciar`). Enumerar verbo de ação em português é
jogo perdido.

**v2 (implementada):** a ÂNCORA é o sinal preciso — só **11 das 1745** respostas
reais contêm "seu navegador"/"navegador local"/"na sua tela". Com um sinal tão
raro, o compromisso pode ser LARGO: futuro em 1ª pessoa, auxiliar + gerúndio/
infinitivo, gerúndio abrindo a frase, ou alegação de já ter feito. O que segura
o falso positivo é a âncora + duas exclusões:

* **negação por FRASE** (não por texto): "não consigo no seu navegador, mas vou
  pesquisar aqui" não é o bug;
* **handoff**: `[[HANDOFF:music]]` é ação real por outro trilho — sem essa
  saída, "YouTube vai abrir aí no seu navegador" (que o fluxo de música ATENDE)
  seria punido e a música quebrava. A guarda roda no texto CRU, onde o marcador
  ainda existe; o que vai para o banco já vem sem ele.

Validada contra as 11 respostas ancoradas reais: **7 disparam** (todas promessas
ou alegações legítimas de bug) e **4 não** (recusa honesta, relato de aba
aberta, resumo do fluxo de música, explicação do Spotify). Testes em
`tests/browser_local/test_promise_guard.py` com as frases reais; suíte:
**1420 passed** (as mesmas 3 falhas de `tests/birra/`, do `.env` de prod).

**Falta (nesta ordem)**

1. ~~Carregar a extensão~~ ✅ (Edge) · ~~ligar a flag~~ ✅ · ~~desktop com o Rust
   novo~~ ✅ (o `tauri dev` rebuildou sozinho).
2. ~~E2E pelo chat~~ ✅ — dois jobs completaram, um deles extraindo dado real da
   página (acima). Falta repetir o caso "coloca o kodhak ai", que era o que
   vazava antes da v2 da guarda.
3. Credenciais por `secret_label` (§3.5) — o único pedaço do plano não feito.
4. Fora do escopo, mas atrapalha o teste: a chave do **OpenRouter estourou o
   limite** (403 "Key limit exceeded", 58× em 6h), então o classificador está em
   fallback e o afeto mudo. Não bloqueia o navegador local (a tool é inline no
   cognitivo), mas degrada tudo em volta.

---

### 2026-08-28 → 2026-08-29 · O driver era cego (o caso da pasta "Print")

**O que aconteceu.** Pedido: baixar a pasta "Print" do Google Drive como ZIP.
Três jobs, nenhum entregou (sessão `5e6a9cbd`, log em `/app/logs/` dentro do
container — não no `neo-core/logs/` sincronizado):

| job | kind | fim |
|---|---|---|
| `ba829946` | browser_local | 25/25 passos, parou em `/drive/home` |
| `1ac2277b` | browser (servidor) | `completed` com "terminou sem resultado textual", `about:blank` |
| `9729bd97` | browser_local | 25/25 passos, parou em `/drive/my-drive` |

Os 8 últimos passos do job 1 no prompt do driver eram literalmente
`click({"ref": 33}) → ok — Mais ações`, oito vezes. O job 2 gastou os 25 em
`click({"ref": 4}) → ok — Pesquisar no Google Drive`.

**Diagnóstico — três defeitos somados, nenhum deles "o modelo é ruim":**

1. **A linha da pasta não tinha `ref`.** O seletor do snapshot pegava só
   `a/button/input/select/textarea` + papéis de controle. Linha do Drive é
   `role=row`/`gridcell` — ficava de fora, e sobrava o botão interno de cada
   linha: **70 elementos chamados "Mais ações"**, indistinguíveis. O nome
   "Print" chegava ao modelo **no dump de texto** (ele estava lendo a pasta) e
   não havia como clicar nela.
2. **Nenhum sinal de que o clique não fez nada.** `browser.click` devolve
   `ok:true` porque o evento foi despachado, não porque a página reagiu — o
   histórico lia oito sucessos seguidos. O único freio era o de passos
   *inválidos* (4 seguidos), que nunca disparava.
3. **Vocabulário sem `dblclick`/`right_click`.** A tarefa pedia botão direito →
   "Baixar". Era impossível por construção.

E, de brinde, o `screenshot` do vocabulário era **cego**: o base64 ia para
`self.shots` (anexo final do job) e nunca voltava ao prompt. Modelo do driver
(`deepseek-v4-flash`) não tem visão, e `Message.content` era `str` — a camada de
providers não sabia carregar imagem.

**O que foi feito** (`browser_local_vision=true`, `gpt-4.1-mini`):

* **Visão por passo.** `Message` ganhou `images`; `OpenAIProvider` monta o corpo
  multimodal só quando há imagem (mandar `[{type:text}]` em todo turno mudaria o
  corpo de todo o Neo sem motivo). O driver tira um print **com o número de cada
  `ref` desenhado em cima** (`browser.screenshot {marks:true, scale:0.75,
  quality:60}`) e manda junto do texto. Falha do print ou do modelo de visão
  desliga a visão pelo resto do job e a tarefa segue no `browser_local_model` —
  cegueira é ruim, tarefa morta é pior.
* **Linhas viram elementos.** SEL ganhou `role=row|gridcell|option|treeitem`;
  célula dentro de linha é pulada (4-5 por item estourariam o teto); o rótulo de
  uma linha é a **primeira linha** do texto dela (`NEO.shortLabel`), e todo botão
  anônimo herda o nome do dono → `[57] button "Mais ações" em "Print"`. Teto de
  elementos 120 → 200.
* **Freio de passo inútil.** Fingerprint da tela (url+título+rótulos+texto): se
  não mudou depois de uma ação de atuação, o passo é carimbado com "a tela NÃO
  mudou" no histórico e a **repetição literal é recusada** em `_validate` (não
  toca na página). Só ação de atuação é cobrada — `extract`/`wait_for`/
  `screenshot` não mudam a tela por definição.
* **`dblclick` e `right_click`** no vocabulário e no Rust.
* Texto da página cortado agora avisa que foi cortado (numa lista alfabética o
  item pedido mora logo depois do 2500º caractere).

**Verificação.** 82 testes em `tests/browser_local/` (12 novos em
`test_driver_vision.py`), suíte inteira **1430 passed** (as 3 falhas de
`tests/birra/` continuam sendo do `.env` de prod). Os 11 blocos de JS injetado
passam no `node --check`. `cargo check` limpo. Prod recarregou com
`✅ Browser LOCAL runner wired (model=deepseek-v4-flash, visão=gpt-4.1-mini)`.

**Gotchas achados no caminho**

* `GEMINI_API_KEY` do `.env` da raiz está **inválida** (400 `API_KEY_INVALID`) e
  a chave do **OpenRouter continua zerada** (`limit 3, remaining 0`) — daí o olho
  ser OpenAI. `gpt-4.1-mini` foi escolhido por aceitar `max_tokens`/`temperature`
  clássicos, que é o que esta camada manda; a família `gpt-5.x` quer
  `max_completion_tokens` e quebraria.
* O Rust novo só vale **depois de reiniciar o `tauri dev`**. Com o desktop
  antigo: o print sai (params desconhecidos são ignorados) mas sem marcações,
  `dblclick`/`right_click` voltam "ação de navegador desconhecida", e as linhas
  continuam sem `ref`.
* **Falso sucesso do irmão headless** (job `1ac2277b`): terminou em
  `about:blank` "sem resultado textual", ficou `completed`, e o `_compose` do
  dispatcher escreveu "Baixei a pasta Print… Deu tudo certo" com um anexo
  legendado "tela onde a tarefa parou (com erro)". Mesma família do
  `neo-ha-false-success-invented-entity` — **não consertado aqui**.
* O retry do turno 2 nem foi para o navegador local: o classificador mandou para
  `server_tool` → `agent` → job `browser`. O portão do §5 decide entre as duas
  tools **dentro do cognitivo**; quando o roteador desvia para `server_tool`,
  ninguém aplica essa escolha. Também **não consertado aqui**.

---

### 2026-08-29 · Segundo teste real: acertou, mas baixou duas vezes

Mesmo pedido ("baixa a pasta Print do Drive em zip"), sessão `371975ac`, job
`0baba7c7`. **A visão funcionou**: o modelo achou a pasta, abriu "Mais ações" e
clicou em "Baixar" no passo 4 — o ZIP baixou. Mesmo assim o job terminou
`failed` em 25/25 passos, e o usuário viu o arquivo chegar **duas vezes**.

O rastro dos passos explica tudo:

```
4. click({"ref": 95})  → ok — Baixar          ← o download JÁ aconteceu aqui
6. wait_for({"text": "Compactando"})     → ok
7. wait_for({"text": "Download completo"}) → FALHOU — não apareceu na página
...
17. click({"ref": 171}) → ok — Baixar         ← de novo
20. click({"ref": 174}) → ok — Baixar         ← e de novo
```

**A barra de download é do NAVEGADOR, não do DOM.** Nenhum `snapshot`, nenhum
`wait_for` e nem o print chegam nela — o modelo não tinha como saber que o
arquivo tinha chegado, então repetiu a ação que "não deu retorno". O freio de
repetição não pegou porque a tela mudava de verdade entre os cliques (o menu
abria e fechava).

**Consertos**

* **`downloads`** — ação nova. O desktop lê a pasta de downloads do usuário e
  devolve o que chegou nos últimos 15 min, com `incomplete: true` enquanto a
  extensão for `.crdownload`/`.part`/`.download`/`.tmp` (chamar de "baixado" um
  arquivo a meio caminho seria o falso sucesso outra vez). Regra escrita no
  prompt: `wait_for` NUNCA vai ver "Download completo", e não se clica em
  "Baixar" duas vezes "para garantir".
* **Marcações fora da página.** O usuário relatou a tela **piscando e o site
  bugando** — os retângulos eram injetados no DOM antes de cada foto e apagados
  depois, e o Drive (SPA pesada) reagia à mutação. Agora o JS só **lê** os
  `getBoundingClientRect()` (`JS_RECTS`) e quem desenha os números é o Rust, em
  cima do JPEG (`draw_marks`, fonte de dígitos 3x5 embutida). O navegador do
  usuário deixou de ser tocado; o modelo continua vendo os mesmos números. Custo:
  a dep `image` (já vinha na árvore pelo tauri — entra só o decoder de jpeg).
* **Backstop do roteador** (`_bias_local_browser`): pedido ancorado no navegador
  DELE força `COGNITIVE`, nos dois caminhos do `classify`. É o conserto do
  defeito nº 1 da nota anterior — o portão do §5 só escolhe entre as tools
  DENTRO do cognitivo, então quando o roteador desviava para `server_tool` o
  pedido caía no headless. Seguro porque a âncora é o portão já medido:
  `scripts/eval_browser_gate.py` → 15 marcações em ~1700 mensagens, **zero falso
  positivo**, recall 12/12.
* **Falso sucesso do headless** (defeito nº 2): `success is None` **e**
  `final_result` vazio agora é `failed`, não `completed` — "a lib não carimbou
  veredito" nunca quis dizer "deu certo". E o `_compose` do dispatcher recebe uma
  ordem explícita em português quando o estado não é `completed` ("é PROIBIDO
  dizer que deu certo"), porque `Estado final: failed` sozinho não segurava.

**Verificação.** 1446 na suíte do core (86 em `tests/browser_local/`, +9 no
`test_router_backstop.py`, +3 no `test_browser_screenshots.py`); 25 testes Rust,
4 deles novos cobrindo o desenho das marcações e o base64; os 11 blocos de JS
passam no `node --check`.

---

### 2026-08-29 (2ª rodada) · O menu abria e ele reabria o menu

Dois jobs novos, os dois mortos por **"quatro ações inválidas seguidas"** — e as
recusas eram todas do freio de repetição que eu tinha acabado de instalar.

**Job `706ff8a8` — o menu estava ABERTO com "Baixar" na lista.** O snapshot do
passo 5 tinha 96 elementos e terminava assim:

```
[88] div(menuitem) "Baixar"     ← era só clicar aqui
[89] div(menuitem) "Renomear⌥⌘E"
...
```

E o modelo clicou de novo num `button "Mais ações"`. Motivo: os 8 itens do menu
ficavam **no fim** de uma lista onde ~50 entradas se chamam "Mais ações". O
freio então barrou a repetição três vezes seguidas e o job morreu — a guarda
transformou um loop em um aborto, sem nunca dizer o que fazer no lugar.

**Job `58004ffa` — o `scroll` não rolava nada.**

```
2. scroll({"delta": 600}) → ok — a tela NÃO mudou
4. scroll({"delta": 300}) → ok — a tela NÃO mudou
5. scroll({"delta": 600}) → ok — a tela NÃO mudou
6,7,8. (inválido) — repetição barrada → aborto
```

`scrollBy` na JANELA não move nada quando o conteúdo mora num painel com
`overflow:auto` — que é o caso do Drive (`Rolagem: y=43` em todos os passos). A
pasta "Print" estava abaixo da dobra, numa lista virtualizada, e nunca chegou a
existir no DOM.

**Consertos**

* **Menu/diálogo aberto sobe para o topo do render.** O snapshot marca
  `item.menu` para tudo dentro de `[role=menu|dialog|alertdialog]` /
  `[aria-modal]`, e o `_render` abre com `>>> MENU/DIÁLOGO ABERTO AGORA` antes
  da lista completa. Regra nova no prompt: com o menu aberto, a próxima ação é
  um `ref` de dentro dele; se nenhum serve, `press` Escape.
* **A recusa passou a apontar o caminho.** Quando o freio barra a repetição e há
  menu aberto, o erro devolvido lista os itens: *"o menu já está ABERTO —
  escolha um item DELE: [88] "Baixar", [89] "Renomear"…"*. Recusar sem
  alternativa era o que empurrava o modelo para a mesma escolha na rodada
  seguinte.
* **`scroll` mira o container que realmente rola**: primeiro ancestral rolável a
  partir do `ref` pedido, senão do centro da tela, senão o maior painel
  semântico (`main`/`role=grid|list|listbox|table|tree|feed` — varrer
  `div[class]` no Drive são milhares de `getComputedStyle` e o passo estoura o
  timeout), e só então a janela. **Rolagem que não move volta como ERRO**, não
  como "ok": "ok" numa ação que não fez nada é a mesma mentira do clique que não
  clica. `scroll` aceita `ref` opcional para escolher o painel.
* **`press` ganhou PageDown/PageUp/Home/End** — em grade virtualizada que ignora
  scroll programático, às vezes é o único jeito de descer.
* **O aborto conta a verdade**: "Me embananei: quatro ações inválidas" virou
  "Travei: quatro tentativas seguidas foram recusadas… A última foi: `<motivo>`".
  Sem o motivo não dá para saber se foi ref inventada, ação inexistente ou
  repetição barrada — que é exatamente o que estas duas investigações precisaram.

**Verificação.** 1453 na suíte do core, 25 no Rust, 12 blocos de JS no
`node --check`.

---

### 2026-08-29 · Diário de bordo (§13)

Pedido depois do 3º teste falho: *"vamos fazer logs de tudo que roda nessa
extensão, quais decisão e etc, até as capturas… quero obter o máximo de info"*.
O motivo é concreto — investigar uma tarefa custava meia hora de `grep` no
`app.log` (o payload do LLM é UMA linha de ~300 KB por passo), o banco guardava
uma frase, e **as imagens que o modelo viu não existiam em lugar nenhum**.

**`core/browser_local/trace.py`** — um diretório por job em
`logs/browser_local/<job_id>/` (config `browser_local_trace_dir`; vazio
desliga), com `trace.md` legível + `NN-tela.jpg` de cada passo. O caminho vai
para `agent_job.log_path` e para o rodapé do resumo do job.

Por passo fica registrado:

* **o que viu** — a página renderizada byte a byte como foi ao prompt (com a
  seção de menu aberto), contagem de elementos, se a lista foi cortada;
* **o print** — salvo em disco, com tamanho e **se veio com marcações**. Print
  sem o campo `marks` é o sintoma de desktop desatualizado, e agora isso está
  escrito no diário em vez de virar dúvida;
* **o que decidiu** — modelo usado, latência, `thought`, ação e argumentos; e a
  resposta CRUA quando o JSON não deu para ler (que é justamente quando ela
  importa);
* **o que a página respondeu** — ok/erro, detalhe, duração;
* **se a tela mudou** depois da ação, e há quantos passos está travado;
* **recusas** (ref inexistente, repetição barrada) com o motivo.

E, fora dos passos, o que responde sozinho o "baixou duas vezes": a pasta de
downloads é fotografada **antes e depois** da tarefa, e o delta entra no
`result_summary` do job (`⬇ 2 arquivos na sua pasta de downloads: …`). Device
antigo, sem a ação `downloads`, é registrado como tal e não derruba nada.

O rodapé fica no **driver**, não no runner: é o único ponto por onde todos os
finais passam (inclusive device caindo no meio e exceção não prevista). Diário
sem rodapé é diário que não se sabe se terminou.

Regra de ouro do arquivo: **observabilidade não é caminho crítico** — todo
evento passa por `_log`, que engole qualquer exceção. Tem teste para isso
(`test_diario_quebrado_nunca_derruba_a_tarefa`).

**Bônus da mesma investigação** (job `958e4e06`, o print que o usuário mandou):
o modelo clicou "Fechar" no painel de progresso do Drive, o navegador perguntou
*"Cancelar o download?"* e ele ficou preso nisso até o passo 25. Regra nova no
prompt: não fechar painel de download em andamento, e diante de "cancelar o
download?" a resposta é CONTINUAR.

**Como ler.** `/app/logs` é um volume Docker (`neo_neo-core-logs`), não o
bind-mount do código — então não sincroniza para o Mac:

```
docker exec neo-core ls -t logs/browser_local | head
docker cp neo-core:/app/logs/browser_local/<job_id> ./trace-<job_id>
```

**Verificação.** 1461 na suíte (110 em `tests/browser_local/`, 9 novos em
`test_trace.py`).

---

### 2026-08-29 · O diário funcionou — e a culpa era do meu texto

Primeiro job com diário (`2c8cc496`). Em 30 segundos de leitura, o que antes
levava meia hora de `grep`:

```
 4/25  click {"ref": 88} → ok — Baixar                    ← download começou
 5/25  downloads {}      → ok — nada novo                 ← Drive ainda COMPACTANDO
 6/25  click {"ref": 90} → ok — Cancelar o download       ← matou o próprio download
 9/25  click {"ref": 88} → ok — Baixar
10/25  click {"ref": 90} → ok — Cancelar o download       ← de novo
19/25  click {"ref":171} → ok — Baixar
20/25  click {"ref":173} → ok — Cancelar o download       ← e de novo
```

`downloads (depois da tarefa): nada recente` — zero arquivos. E o print do passo
5 mostra o painel do Drive escrito **"Preparando o download / Compactando…"**.

**A causa raiz é a regra que EU escrevi na rodada anterior.** O painel de
progresso do download é `[role=dialog]`, então entrava na seção nova como
"menu aberto" com três itens — `[88] Minimizar`, `[89] Fechar`,
`[90] Cancelar o download` — sob o cabeçalho *"A próxima ação tem que ser AQUI
DENTRO"*. O modelo obedeceu à ordem e escolheu a única que parecia uma ação.
Guarda que manda agir dentro de um contêiner onde nenhuma opção serve é guarda
que fabrica o erro; é a terceira vez neste plano que uma regra minha vira o bug
seguinte, e a lição é sempre a mesma: **toda regra imperativa precisa de uma
saída explícita para o caso em que ela não se aplica.**

Somou-se a isso o `downloads` sem paciência: baixar uma PASTA faz o Drive
compactar no servidor antes de mandar, então a primeira olhada vem vazia — e o
modelo leu "vazio" como "falhou" e foi cancelar para tentar de novo.

**Consertos**

* **Cancelar o próprio download é recusado.** Quando um clique nosso retorna
  rótulo com "baixar"/"download", o driver liga `_download_started`; a partir
  daí, clique em controle que casa `cancelar (o) download` é barrado com a saída
  escrita no erro: *"o painel de progresso não é um menu de escolha… use
  `downloads` com `wait_s`"*. O rótulo vem do DEVICE, não da intenção do modelo.
  Sem download nosso em andamento, o botão continua clicável (é guarda contra
  destruir o próprio trabalho, não censura ao verbo).
* **A seção de menu ganhou a exceção que faltava**: *"Se for um painel de
  PROGRESSO (download em andamento), não é menu nenhum: ignore, não feche e não
  cancele"* — no cabeçalho e na regra do prompt.
* **`downloads` aceita `wait_s`** (0–60): repete a olhada de 2 em 2 segundos até
  um arquivo NOVO ficar completo. O prompt manda usar 20–40 depois de baixar
  pasta, dizendo por quê (compactação).

**De quebra, o diário respondeu duas perguntas antigas:** todos os prints saíram
`com marcações` (o desktop ESTÁ com o Rust novo — fim da dúvida do `tauri dev`),
e a ação `downloads` respondeu em 170ms em vez de "ação desconhecida".

**Verificação.** 1466 na suíte (119 em `tests/browser_local/`, 5 novos).

---

### 2026-08-29 · Primeiro sucesso — e o resto dos equívocos

Job `0611add0` **completou e o ZIP chegou**: `Print-20260829T133311Z-1-001.zip`,
888 KB, confirmado pelo delta da pasta de downloads. O `wait_s` fez o trabalho
no passo 16. Mas dos 19 passos, **10 foram briga com o painel de progresso**:

```
 4  click 88 → Baixar                    ← download começou
 5  click 89 → Fechar          ┐
 6  click 89 → Fechar          │ fechar o painel abre "Cancelar o download?"
 7  click 92 → Continuar download │
 8  click 92 → Continuar download │
 9  click 89 → Fechar          │
10  click 89 → Fechar          │  ← 10 passos neste ciclo
11  click 89 → Fechar          │
12  click 91 → Fechar          ┘
13  right_click 46 → Mais ações
14  click 88 → Baixar                    ← baixou de novo
16  downloads {"wait_s":30} → Print-...zip (888218 bytes)   ← a evidência
```

**A guarda anterior tapava só metade do buraco.** Ela barrava o rótulo
"Cancelar o download", mas o modelo chegava no mesmo lugar clicando em
**"Fechar"** — e no Drive o X do painel abre o diálogo de cancelamento. E a
razão de ele querer fechar era conceitual: **ele lia "Compactando 1 arquivo…"
como um travamento**, não como a primeira fase do download. Baixar uma PASTA
tem duas fases (o site zipa no servidor, depois manda) e isso não estava escrito
em lugar nenhum.

**Consertos**

* O snapshot passou a dizer **de qual contêiner** cada item de menu veio
  (`item.panel`, o título do `[role=dialog]`), e o render mostra:
  `[89] div(button) "Fechar" [painel: "Preparando o download"]`. "Fechar" num
  menu de escolha e "Fechar" no painel de download deixaram de ser a mesma coisa
  aos olhos do modelo.
* Enquanto um download NOSSO está em curso, clique em controle que DISPENSA
  (fechar/minimizar/cancelar/×) dentro de um painel de progresso é recusado, com
  a explicação inteira no erro. O diálogo "Cancelar o download?" continua
  clicável — "Continuar download" é a resposta certa e precisa passar.
* Prompt: *"compactar/preparar É o download"*, com os nomes que o Drive usa; e
  *"assim que `downloads` mostrar o arquivo COMPLETO, responda `done` — não fique
  arrumando a tela depois disso"* (ele gastou 3 passos depois de já ter o
  arquivo, e ainda relatou "está em andamento" quando já tinha terminado).
* **`stale` agora tem uma segunda chance.** A SPA redesenha nos ~1,5s que o
  modelo leva para decidir, e o `ref` envelhece; o passo morria por um motivo
  que não tem nada a ver com a decisão. Agora o driver reprocura o elemento
  **pelo rótulo** e refaz uma vez. Se o rótulo sumiu de verdade, falha como
  antes — inventar outro elemento seria pior.

### A faixa da extensão estava ocupando espaço na página

Diagnóstico do relato *"o toolbar no bottom da página está ocupando espaço"*:

```js
host.style.cssText = ["position:fixed", "top:0", …, "all:initial"].join(";");
```

**`all:initial` por último apaga tudo que veio antes.** É um atalho que reseta
TODA propriedade, então `position`, `top`, `z-index` e `pointer-events` voltavam
ao default: a faixa virava um bloco `static` anexado depois do `<body>` — no fim
do documento, ocupando altura de verdade e ainda capturando cliques. Explica os
dois sintomas de uma vez: aparecia embaixo E empurrava o conteúdo.

Conserto: `all:initial` primeiro, posicionamento depois (`content-overlay.js`,
extensão v0.1.42 — **precisa recarregar a extensão no navegador**). O HUD do
cursor (`content-cursor.js`) não tem o problema: define as propriedades uma a
uma, sem o atalho.

**Verificação.** 1474 na suíte do core, 25 no Rust, 12 blocos de JS + o
`content-overlay.js` no `node --check`.

---

### 2026-08-29 · Job `6142ed04` — baixou certo, e baixou de novo

Completou e trouxe o arquivo. Não tentou cancelar a compactação nenhuma vez (a
guarda do painel funcionou). Mas baixou **duas vezes**, e a razão está numa
linha de formatação:

```
 6  click 88 → Baixar
 7  downloads {"wait_s":30} → Print-20260829T145255Z-1-001.zip (888218 bytes, 0s atrás)
 8  click 89 → Fechar          ← "limpar a tela"
 9..17  right_click / click … → Baixar          ← baixou de novo
18  downloads → o MESMO arquivo (888218 bytes, 40s atrás)
```

No passo 7 o arquivo já estava completo no disco. O retorno dizia
`nome (888218 bytes, 0s atrás)` — e **em nenhum lugar dizia que terminou**. O
único sinal de conclusão era a AUSÊNCIA do texto "AINDA BAIXANDO", ou seja, uma
inferência por negativa. O modelo não fez essa inferência: leu como "o download
está acontecendo" e voltou para a listagem.

**Consertos**

* O retorno do `downloads` passou a afirmar o fim:
  `✅ DOWNLOAD CONCLUÍDO — <nome> (<n> bytes) já está no disco do usuário. NÃO
  mande baixar de novo: a tarefa acabou, responda `done` com esse nome.`
  Estado positivo escrito, não deduzido pela falta de outro.
* Guarda de re-download: com um arquivo novo já completo, clicar de novo em
  "Baixar" é recusado **uma vez**, nomeando o arquivo que já chegou. Aviso
  único, não cerca: se o modelo insistir, passa — tarefa de vários arquivos
  existe, e travar de vez seria pior que baixar repetido.

### Os glitches de resize e piscada que sobraram

Depois do conserto do `all:initial`, o usuário reportou que diminuíram mas
continuavam. Duas causas remanescentes, as duas do nosso lado:

1. **O `clip` do `Page.captureScreenshot`.** Pedir clip faz o Chrome relayoutar
   a página para a captura — 25 vezes por tarefa, na tela que o usuário está
   olhando. A captura agora é crua (viewport, sem clip, sem
   `captureBeyondViewport`) e **a redução acontece por software**, no Rust, em
   cima do JPEG que já era decodificado para desenhar as marcações. Custo zero
   a mais; o navegador deixou de ser tocado.
2. **`scrollIntoView({block:'center'})`** em click/dblclick/right_click: rola a
   página MESMO quando o elemento já está inteiro visível, um pulo por clique.
   Trocado por `'nearest'`, que só mexe se precisar.

**Verificação.** 1478 na suíte do core (127 em `tests/browser_local/`), 27 no
Rust (2 novos cobrindo a redução por software e as marcações sobre a imagem já
reduzida), 11 blocos de JS no `node --check`.

---

### 2026-08-29 · "Baixar" acaba quando o download COMEÇA (§14)

Observação do usuário, e ela reorganiza o problema: *"eu só pedi para ele
baixar; podia já retornar a tarefa como concluída com o download em andamento.
Se fosse para acompanhar mesmo, teria que ser um prompt mais direto — 'baixe e
aguarde a conclusão'. Aí poderia ser um cron em código que o LLM chamaria… se o
status muda ele chama o LLM para dizer qual o status."*

Está certo, e explica por que os últimos jobs gastavam 10 dos 20 passos no fim:
**mandar baixar é ação; esperar o byte chegar é espera.** Cada passo do agente
custa uma chamada de LLM e uma ida-e-volta até a máquina do usuário — é o pior
instrumento possível para ficar olhando um arquivo crescer.

**Dois modos, decididos pelo texto do pedido** (`_ASKS_TO_FOLLOW`):

* **"baixe a pasta Print"** → assim que o arquivo APARECE na pasta de downloads,
  o `downloads` responde *"⬇ DOWNLOAD COMEÇOU … a tarefa está cumprida,
  responda `done`, eu aviso sozinho quando terminar"*, e a espera do laço é
  limitada a 12s. O agente encerra.
* **"baixe e aguarde a conclusão" / "garanta que tudo ocorra bem"** → comportamento
  de antes: espera o arquivo ficar completo antes de encerrar.

**O vigia** (`core/jobs/runner_download_watch.py`, `kind="download_watch"`): o
runner do navegador enfileira este job quando a tarefa acaba com download em
curso. É um laço em código puro, **sem LLM nenhum no meio**, que consulta a
pasta de downloads a cada 10s e produz um dos quatro vereditos — concluído,
travado (tamanho parado há 2 min), sumiu (cancelado no navegador) ou tempo
esgotado. O LLM só volta a ser chamado UMA vez, no fim, e por quem já fazia
isso: o `ProactiveDispatcher`, que redige o aviso no chat a partir do
`result_summary`. Nenhuma máquina nova.

Veredito sempre por evidência do disco: "travou" olha o TAMANHO parado, não o
relógio; "cancelado" é o arquivo ter sumido; e o tempo esgotado não afirma nem
sucesso nem fracasso ("não cancelei nada").

### O clique agora tem eco na tela

Pergunta do usuário: *"a extensão que fizemos o fork não tinha um indicador de
onde o mouse clica?"* Tinha — `content-cursor.js`, o cursor animado do
open-browser-use, com o RPC `moveMouse` no host. Mas ele exige uma **sessão do
host** (`requireSession` pede `session_id` + `turn_id`), e nós não usamos a
camada de sessão do upstream: dirigimos por `executeCdp` direto. Ligar aquilo
significaria entrar na plumbing do fork inteiro.

Feito de outro jeito, mais barato e sob nosso controle: a faixa do Neo já tem um
shadow DOM próprio em toda página, então ganhou **uma camada de efeitos dentro
dele**. Cada `click`/`dblclick`/`right_click` posta `__neo_ping {x, y, tipo}` no
mesmo passo do CDP (zero ida-e-volta a mais) e a extensão desenha um pulso no
ponto — azul para clique esquerdo, âmbar para o direito — que some sozinho em
600ms. Nenhum nó novo na página, `pointer-events:none`, sem reflow. Extensão
**v0.1.43**.

**Verificação.** 1496 na suíte do core (145 em `tests/browser_local/`, 8 deles no
`test_download_watch.py` novo), 27 no Rust, 11 blocos de JS + `content-overlay.js`
no `node --check`.

### 2026-09-05 · O inventário era uma allowlist — e o site que não a segue vira tela cega

Job `9968e68a` (questionário do portal, 10 questões): **43 rodadas, 25 passos,
`failed`, zero questões respondidas**. As duas tarefas de LEITURA da mesma noite
(Amazon `f6b5cb6b`, playlist do YouTube `f374a1d6`) terminaram bem. Não era
navegação nem raciocínio: era percepção de **controle acionável**.

No print com marcações do passo 7, as 5 alternativas estão nítidas no centro da
tela, cada uma com sua bolinha de rádio — e **nenhuma tem número desenhado**. O
snapshot devolveu 56 elementos: o menu lateral inteiro, o cabeçalho, os 10 itens
do índice de questões, `Responder [desabilitado]`, `Finalizar`, prev/next.

**Três defeitos que se somaram.**

1. **A allowlist não alcança markup fora do canônico.** O `SEL` pegava
   `a[href]/button/input/select/textarea`, papéis ARIA e `[onclick]`. Não tinha
   `label`, não tinha `[tabindex]`, e nada que casasse `ng-click` / `@click` /
   `wire:click` / `hx-*`. O portal é AngularJS; a alternativa é ou uma
   `div ng-click`, ou um `<label>` com `<input type=radio>` escondido por CSS —
   e o filtro de visibilidade descartava o input por `opacity<0.05`. Os dois
   padrões caíam no mesmo buraco.
2. **A visão inventou `ref`, e o prompt mandou inventar.** O `look` ACERTOU:
   descreveu as cinco alternativas e escolheu a correta. Mas o `_VISION_SYSTEM`
   dizia "quando houver números desenhados, CITE o número" — e não havia número
   nenhum ali. O modelo respondeu "refs 16 a 20", depois "ref 163". **O ref 18
   era "Meu Papel no Mundo", no menu lateral.** O agente clicou lá cinco vezes,
   depois em "VOLTAR", e perdeu a página.
3. **O orçamento de texto foi comido pela moldura.** `body.innerText.slice(0,
   3000)` em ordem de documento: os 3000 caracteres viraram os 20 links do menu
   + cabeçalho + metadados da prova + os 10 enunciados do índice lateral. O
   bloco "Alternativas" ficou depois do corte. Fechou a última saída — nem
   clicar, nem ler.

**A troca de pergunta.** Allowlist não fecha: sempre existe o próximo framework.
A pergunta deixou de ser "esta TAG é interativa?" e passou a ser **"este pixel
reage?"**, que é o que o inspetor do DevTools responde. O inventário virou três
passadas:

- **declarada** — o `SEL` de antes, mais `label`, `summary`, `[tabindex="0"]`,
  `[ng-click]`, `[data-ng-click]`, `[jsaction]`, `[data-action]` e os papéis de
  campo que faltavam;
- **procurador** — todo `input/select/textarea` INVISÍVEL ganha um rosto
  clicável (o `label` associado, ou o primeiro ancestral visível). Um controle
  de formulário é interativo por definição; só a pintura dele sumiu. O `ref` é o
  procurador, o `checked` continua saindo do input real, e a linha diz
  `[seleciona radio[q1]]`;
- **grade** — varredura geométrica: amostra o viewport de 40 em 40 px com
  `elementFromPoint`, desduplica os nós e sobe de cada um até o primeiro
  ancestral com EVIDÊNCIA (tag, papel de controle, `contenteditable`,
  `tabindex=0`, atributo casando `/^(on\w+|ng-\w*click|v-on:|@|x-on:|wire:|hx-|
  jsaction|data-action)/`, ou `cursor:pointer`). Acha o que ocupa área e reage,
  em qualquer framework.

**`cursor` é HERDADO — e isso quase virou o bug seguinte.** Na primeira versão,
os dois `<span>` de dentro de cada alternativa entraram como candidatos (herdam
`cursor:pointer` do pai), e a alternativa virou `⟪CAIXA com 2 itens⟫` — que é
exatamente o que o `_validate` RECUSA clicar. Dois consertos: quando a evidência
é `cursor`, o alvo é o **mais externo** da faixa contígua com `cursor:pointer`
(que é o que o usuário percebe como "o botão"); e quem tem evidência FORTE
(tag/label/papel de controle/handler) nunca é rotulado caixa. `row`/`gridcell`
continuam fracos de propósito — a `row` do Drive é a lista inteira, e tratá-la
como controle é o loop de 23 passos do job `a0eab88c`.

**Teste de acerto: "este pixel reage a MIM?"** `js_click` despacha o evento
direto no nó, então ele funciona MESMO com um banner por cima — e devolve `ok`
enquanto o site, que escuta na camada de cima, ignora. É o `ok:true ≠ a página
reagiu` entrando por outra porta. Agora o snapshot faz `elementFromPoint` no
elemento e marca `⟪COBERTO por "…"⟫`, e o `_validate` recusa o clique antes de
tocar na página. **A primeira versão disto era ruído puro**: medida na Amazon,
marcou 20 dos 200 elementos como cobertos — vizinhos de layout estáticos, e
links de pulo em `x=-9968` cujo ponto eu espremia para dentro da tela. Ficou
conservador em três camadas: ponto fora da tela não é testado, três pontos
(acertar em um basta, porque sobreposição parcial ainda é clicável), e só conta
quem está numa CAMADA de verdade (`fixed`/`sticky`/`absolute` com z-index, ou
dentro de um modal). Voltou a 0 falso positivo na Amazon, no GitHub e no HN, e
continua pegando o modal de cookie da fixture.

**`click_text` — a saída para o que se lê e não se endereça.** Todo inventário é
uma aposta e aposta erra. `click_text {"texto": "…"}` acha o MENOR elemento
visível cujo texto contém a frase (TreeWalker sobre nós de texto, sem acento e
sem caixa), sobe até o ancestral acionável, testa a cobertura e clica. É o
`getByText` do Playwright. Passa pelas MESMAS guardas de dinheiro e de download
que o `click` — senão bastaria trocar de ação para furar todas.

**A visão parou de poder inventar.** O device agora devolve `marks_refs` (quais
números ele REALMENTE desenhou) e o `_look` cola a lista no prompt: "os únicos
que existem são 0, 1, 45…; qualquer outro elemento você descreve pelo TEXTO
dele". Print sem marcação nenhuma vira uma frase explícita em vez de silêncio.

**Estado positivo AFIRMADO, de novo.** Marcar um rádio não muda URL, nem título,
nem rótulo, nem texto — então o freio de "a tela não mudou" dava o passo por
inútil justamente quando ele tinha funcionado, e recusava a tentativa seguinte.
Agora `checked`/`disabled` entram no fingerprint, e o `js_click` lê o estado do
controle DEPOIS do clique: o histórico recebe `→ SELECIONADO` ou `→ mas NÃO
ficou selecionado (tente click_text)`. O `[desabilitado]` do botão de enviar é o
oráculo grátis da mesma coisa.

**Conteúdo principal ADICIONADO, nunca subtraído.** Recortar "só a área da
tarefa" já jogou a resposta fora uma vez (job `9025d9f8`). O snapshot ganhou
`main` (o maior `main`/`[role=main]`/`article` fora da moldura, ou o melhor
ancestral das empresas não-moldura), renderizado ANTES do texto do corpo, que
passa a ter orçamento menor. As linhas repetidas entre os dois saem do corpo —
dedup, não corte: cada linha continua no prompt, uma vez só.

**A janela entre o deploy do core e o restart do desktop.** O core sobe sozinho
(Mutagen + `--reload`); o Rust só depois de reiniciar o `tauri dev`. Nesse
intervalo o core conhece uma ação que o navegador não implementa. O carimbo de
versão é o campo `main`, que o device novo manda SEMPRE (vale `null`): sem ele,
`click_text` é recusado com a saída escrita, e o diário registra "device
antigo".

**Como isto foi medido** (`neo-desktop-tauri/scripts/snapshot-lab/`): o JS vive
como string dentro do Rust, então `cargo test` não o alcança e `tauri dev` só
prova depois de reiniciar. O laboratório extrai as strings do `.rs` e as roda num
Chromium headless de verdade — layout, `getComputedStyle` e `elementFromPoint`
reais, que jsdom não tem. A fixture reproduz os dois padrões que cegaram o
driver; o teste passa quando as cinco alternativas aparecem, o clique devolve
`checked:true` e o modal marca tudo como coberto. Custo do snapshot novo em
sites reais (1600×1000): Amazon 155–209 ms, GitHub 44–51 ms, HN 26–28 ms — e nos
três a grade não acrescentou nada, porque são sites com markup canônico. Ela
existe para o resto da web.

**Pendente**: rodar o job do portal de novo com o `tauri dev` reiniciado. Nada
disto foi exercitado contra o site real — só contra a reprodução do padrão dele.

### 2026-09-05 (2) · O inspetor visual na extensão — ver o inventário em um clique

Pergunta que motivou: "a função que mapeia os componentes está na extensão?".
Não estava — o mapeamento vive no Rust, injetado por CDP pelo desktop, e a
extensão só tinha a faixa e o pulso de clique. Agora tem um botão.

**O mapeador NÃO foi reimplementado na extensão.** `scripts/gerar-mapa.py`
EXTRAI o `js_snapshot` do `browser_local.rs` e escreve
`neo-browser-local/neo-map.generated.js`, que expõe `__neoMapear(max, chars)`.
Um inspetor que enxerga diferente do driver não testa nada — ele mente com
autoridade. `--check` sai com código 1 quando o gerado está atrasado, e o
`UPSTREAM.md` avisa que mexer no `js_snapshot` exige regerar.

O content script roda no mundo ISOLADO, então o `window.__neo` dele é outro
objeto e não encosta no do mundo principal onde o driver trabalha.

**Três regras de desenho, todas por um bug que já aconteceu:**

1. **Nada é escrito na página.** A v1 das marcações do driver desenhava
   retângulos no DOM e os apagava a cada passo: a tela piscava e o Drive bugava.
   Aqui o único nó é um host `position:fixed` com shadow root fechado.
2. **Um único `<canvas>`, nunca N divs.** Canvas é camada composta própria:
   redesenhar não recalcula estilo nem layout da página. Duzentos divs
   posicionados custariam duzentos layouts a cada rolagem.
3. **`all:initial` PRIMEIRO** — a lição da v0.1.42, em que ele apagou o
   `position:fixed` da faixa e ela virou um bloco estático que ocupava altura.

Rolar NÃO remapeia: as caixas são relidas dos elementos vivos
(`__neo.refs[i].getBoundingClientRect()`) num `requestAnimationFrame`
coalescido, com listeners passivos. O remapeamento (a parte cara) só acontece no
botão.

**Cor por passada**, que é o que transforma o inspetor em diagnóstico: azul = o
site declarou · **verde = rosto de um controle escondido por CSS** · âmbar =
achado pela varredura por pixel · roxo = linha de lista promovida · vermelho
tracejado = coberto. O verde vence a origem de propósito: um `<label>` de
alternativa entra pela primeira passada (o seletor conhece `label`), e pintá-lo
de "declarado" esconderia justamente o que o inspetor existe para mostrar.

**Medido** (`scripts/testar-inspetor.py`, Chromium headless com o
`chrome.runtime` dublado; o teste falha se qualquer medida da página mudar entre
antes e depois de ligar):

- fixture do questionário: 30 elementos em 8–17 ms, **3 procuradores** com
  `→ radio[q1]`;
- fixture com modal: **30 de 31 cobertos**, em vermelho tracejado;
- github.com/anthropics: 142 elementos em 41 ms, e os **3 verdes são os
  dropdowns Type/Language/Sort** — `<details>` com rádios escondidos, um achado
  real de um site real;
- em todos: `scrollHeight`, largura do corpo, caixa do `<main>`, posição de
  rolagem e número de nós no `<body>` **idênticos** antes e depois. Desligar
  remove o host.

Extensão em **0.1.44** — precisa ser recarregada no navegador.

### 2026-09-05 (3) · "Esta aba não aceita o inspetor" — em todo site

Reportado logo depois de publicar o inspetor: a mensagem de fallback aparecia em
qualquer página, google.com incluído. Não era limitação de aba nenhuma; era bug
meu, e só apareceu porque fui reproduzir em vez de adivinhar.

**Carregar a extensão de verdade** (Chrome for Testing com `--load-extension`),
mandar a mensagem do popup a partir do **service worker real** e ler o console
da página deu o diagnóstico em três passos:

1. `chrome.tabs.sendMessage` resolvia com `ok:true` e resposta **`undefined`** —
   ou seja, existia listener na aba, mas nenhum chamava `sendResponse`;
2. o console da página tinha, duas vezes:
   `Uncaught SyntaxError: Identifier 'HOST_ID' has already been declared`;
3. **content scripts do mesmo item do `content_scripts` compartilham UM escopo
   de topo.** `content-overlay.js` já declarava `const HOST_ID`. O
   `content-inspect.js` declarava o dele, o Chrome matava o arquivo INTEIRO, o
   listener nunca era registrado — e quem respondia (com nada) era o listener do
   overlay. O popup lia `undefined` e culpava a página.

Conserto em duas camadas: o inspetor virou uma IIFE com guarda de instalação
(escopo próprio, e injetar duas vezes na mesma aba não redeclara nada), e saiu
do `content_scripts` — o popup o injeta sob demanda com
`chrome.scripting.executeScript`. A injeção sob demanda paga sozinha: são 33 KB
de mapeador que deixam de ser lidos em TODA page load de TODA aba por uma
ferramenta ocasional, e ela ainda resolve a aba que já estava aberta quando a
extensão foi recarregada — caso em que o content script do manifest simplesmente
não está lá. O popup tenta `sendMessage`, e só injeta quando a resposta vem
`undefined`.

**E o teste em site real pegou dois procuradores ruins** que a fixture nunca
mostraria. No google.com, a busca de "rosto" de um controle escondido subiu até
o `<body>` (rosto de um `textarea` de telemetria) e até um contêiner do
formulário (rosto do `submit[btnK]`). Clicar nessas caixas não faz nada — é o
erro da ⟪CAIXA⟫ entrando pela porta dos fundos. O `<label>` continua sem limite
(é o rosto de UM controle por definição); a subida por ancestral ganhou três:
nunca `body/html/form/main/article/section/nav/header/footer`, área ≤ 15% do
viewport, e **no máximo um controle de formulário dentro** — mais de um quer
dizer contêiner de vários, não rosto de um. Depois disso o google.com passou a
não oferecer procurador nenhum (correto) e o GitHub também (os `<details>` de
filtro embrulham vários rádios; os `<summary>` deles já estavam na lista pelo
seletor, então nada de clicável se perdeu).

Junto veio um terceiro achado: `NEO.label` caía no `textContent` quando o
`innerText` era vazio, e o `textContent` traz o **conteúdo de `<style>`** —
elementos vinham rotulados `.A8SBwf{--client-adapter-margin-base:1`. Agora a
queda para `textContent` é pulada quando há `style/script/noscript` dentro. Isso
vale para o driver também, não só para o inspetor: o modelo estava lendo CSS
como nome de elemento.

Extensão em **0.1.45**.

---

### 2026-09-29 — O print entregue não era o da resposta (job `78d998ee`)

Pedido: *"acessa o meu navegador local e vai na amazon e vê se tenho algum
produto pra chegar"*. O agente **respondeu certo** — "Produto
Exemplo, chega 5 de outubro, R$ 49,86" — e anexou o print da página de rastreio de
**outro** pedido (Produto B, entregue 17/set), com o carrinho aberto por
cima.

**Nada falhou.** `self.shots` — a lista que vira anexo — era alimentada em dois
lugares: a triagem de painel (`_triagem_visual`) e a ação `screenshot`. A tarefa
inteira usou `look`, que fotografa, pergunta à visão e **descarta a imagem**. O
agente nunca chamou `screenshot`. Então o único print que chegou ao usuário foi
um acidente: um painel «Subtotal» que abriu sozinho no passo 18, na página
errada. Confirmado por sha256 — o `stored_file` entregue é byte a byte o
`22-tela.jpg` do diário. O print que estava perfeito (`23-tela.jpg`, a lista com
o Produto Exemplo no topo) foi tirado no passo 19 e jogado fora.

Somava um segundo erro, que sozinho já bastaria: a janela guardava os
**primeiros** `max_screenshots`. Numa tarefa de navegador a evidência se
acumula — a tela que responde a pergunta é a do fim, não a primeira que
apareceu.

**O conserto, em três peças:**

1. **`look` alimenta a entrega.** A imagem que o modelo *escolheu* olhar é a
   melhor candidata que existe no meio da tarefa: é uma tela sobre a qual
   alguém teve uma pergunta.
2. **A janela guarda os ÚLTIMOS** (`_guardar_print`). A triagem de painel
   perdeu a prioridade e entrou na mesma fila — era ela que, sendo um dos dois
   únicos caminhos, transformava um painel qualquer na única imagem do job.
3. **Print no fim, na frente e sem marcações** (`_shot(para_usuario=True)` +
   `_fechar_prints`). É a tela de onde saiu a resposta, e é a única que se sabe
   relevante: se o teto só deixar passar uma, que seja essa. Sai limpa porque
   os números desenhados são andaime do modelo de visão e, para quem lê o chat,
   só sujam a foto. Vale nos **seis** finais, não só no `done` — é no fim ruim
   (desistiu, acabaram os passos, parou para confirmar) que a foto de onde
   parou vale mais.

Duas consequências que não são óbvias:

- **O print do fim não depende da visão estar ligada.** Quem lê aquele print é
  uma pessoa, não o modelo — e job cego que entrega texto e nenhuma tela foi
  exatamente o que `browser_local_vision_model` existe para não repetir. Para
  distinguir "sem visão" de "o device não fotografa", entrou `_shot_broken`.
- **`DriverResult.screenshot_captions`**: a primeira imagem é a do fim e diz
  isso ("a tela quando terminei"); as outras são "no caminho". Sem legenda as
  três chegavam com o mesmo rótulo e o usuário tinha que adivinhar qual era a
  conclusão.

Testes: `tests/browser_local/test_prints_da_entrega.py` (as três promessas + o
fim ruim). `test_sem_visao_nao_tira_print_nem_manda_imagem` virou
`test_sem_visao_o_modelo_fica_cego_mas_o_usuario_nao` — a promessa mudou de
propósito.

**O que este job mostrou e NÃO foi consertado** (fica registrado porque é o
próximo gargalo, não um detalhe):

- **162s, e a resposta estava pronta aos 50s.** O `note` com o Produto Exemplo é do
  passo 9 (01:10:57). Os 112s seguintes foram conferir se havia outros: 8
  rolagens a mais, um clique no "Rastrear pacote" do pedido errado e 3 olhadas.
  Percorreu a mesma página três vezes (y: 0→2800→0→2400→1200→2000→0→1200).
- **77% do tempo é LLM**: 25 decisões × 3,9s de média (pico 7,1s) = 98,7s, mais
  25,7s de visão. O navegador em si custou 5,8s — as 14 rolagens somaram 2,9s.
- **O `max_steps` do classificador é descartado.** Ele montou
  `max_steps: 15` + `start_url`, e o modelo COGNITIVE reemitiu a tool com só
  `task`/`title`/`visible` — o teto caiu no default 25
  (`executor.py:813`). Com os 15 pedidos, o job teria parado em 01:11:35 (88s
  em vez de 162s) com o Produto Exemplo já no caderno.

---

### 2026-09-30 — A mesma leitura cinco vezes (job `6633351e`)

Mesmo pedido de sempre ("tenho algum produto pra chegar?"). A resposta certa
entrou no caderno **no passo 2, aos 5 segundos**. O job morreu no teto de 25
passos, **115s** depois, com estado `failed` — e o que chegou ao usuário trazia
**cinco cópias do menu de atalhos de teclado da Amazon**.

Repetições literais em 29 decisões: `scroll {"delta": 700}` ×6, `extract {}` ×5,
`wait_for {"text":"Pedidos"}` ×2, `press Escape` ×2. Duas recusas dispararam, e
nenhuma pegou a que custou o job.

**O motor era o `extract`, e o defeito é um que este plano já corrigiu uma vez.**
`extract` sem `ref` mirava `document.body`, que sai em ordem de DOCUMENTO — os
1500 caracteres viravam os skip-links e o painel de atalhos, em qualquer página
da Amazon. É literalmente o lixo que o comentário de `principal` no `js_snapshot`
cita pelo nome desde o job 9968e68a: *"o vencedor foi um `<nav>` de skip-links
('Ir para / Conteúdo principal / Atalhos do teclado')"*. O snapshot ganhou o
conteúdo principal; o `extract` ficou para trás.

Daí o ciclo se fechava sozinho:

1. o texto tinha >200 chars → **ia sozinho para o caderno** (auto-caderno);
2. o modelo não achava o que queria e tentava de novo;
3. a trava de releitura (`_reads_on_screen`) é chaveada pela **TELA**, e entre um
   `extract` e outro houve clique e rolagem → tela nova, trava limpa;
4. cinco vezes, e o caderno (que é o que vai para o usuário) virou lixo.

**Conserto em três camadas, uma por ponta:**

- **O snapshot PUBLICA o que escolheu** (`NEO.principal = principal ? principal.el
  : null`, uma linha) e o `js_extract` sem `ref` lê dali. Não é reimplementação:
  é a MESMA escolha que acabou de ir no prompt. Reimplementar criaria uma segunda
  verdade — o erro que a extensão já cometeu uma vez (`neo-map.generated.js`), e
  aqui seria pior, porque o modelo leria uma região e veria outra descrita.
  `isConnected` cobre a página ter trocado desde o snapshot; sem ela, cai no
  corpo, que ainda é melhor que um elemento morto.
- **`js_extract` devolve `label`** e o driver prefixa o resultado com
  `(li de «...»)`. Sem isso, leitura certa e leitura errada chegam idênticas ao
  modelo — era o que o deixava repetir sem ter como perceber.
- **Trava por LEITURA, não por tela** (`_leituras` / `_leituras_repetidas`): texto
  que já voltou é dito ("MESMO TEXTO que você já leu antes"), **não é anotado de
  novo**, e a terceira tentativa com a mesma assinatura é recusada com a saída
  junto. A trava velha (`_reads_on_screen`) fica: ela pega o caso barato, antes
  de gastar a ida ao device.

O auto-caderno continua valendo para leitura NOVA — ele existe porque leitura
substancial que morre no histórico é tarefa perdida (job 6785cf70). O que mudou é
que o critério era só o TAMANHO.

**Medido no laboratório, não no job.** `scripts/snapshot-lab` extrai o JS do `.rs`
e roda num Chromium de verdade — foi lá que o defeito e o conserto foram provados,
em `amazon.com.br/s?k=teclado`:

```
antes:  EXTRACT sem ref → "Ir para ⏎ Conteúdo principal ⏎ Atalhos do teclado ⏎ …"
depois: EXTRACT sem ref → label: 'Filtros'  ·  mesma região do snapshot.main: SIM
```

As duas fixtures (`questionario.html`, `questionario-com-modal.html`) seguem
passando iguais, `cargo test` 85/85, e o `neo-map.generated.js` foi regerado.

**Deploy:** a parte do Rust **só vale depois de reiniciar o `tauri dev`**; sem
isso o `extract` continua no `document.body` (e a trava nova do core apenas o
barra mais cedo). O `neo-map.generated.js` regerado só afeta o inspetor da
extensão — precisa de reload em `edge://extensions` para o inspetor voltar a
enxergar igual ao driver.

**Não foi feito, de propósito:** usar VISÃO para escolher onde o `extract` mira.
A imagem não faltava — o modelo já tem `look` para pergunta visual, e o alvo
agora é determinístico (a região que ele acabou de ver descrita). Pagar uma
chamada de visão em toda leitura para adivinhar o que o snapshot já sabe seria
trocar um bug por um custo.

**O que continua de pé:** 6 `scroll {"delta": 700}` idênticos. A guarda de
repetição só recusa ação que não mudou a tela, e rolar muda — então rolar em
círculos é "progresso" pela regra atual. E o teto de passos segue sendo o que
termina o job em `failed` com a resposta certa já no caderno desde o passo 2:
falta um sinal de "já tenho o que foi pedido, pare de conferir".

---

### 2026-09-30 — O orçamento deixou de ser um número e virou uma régua

O teto fixo de 25 passos tratava igual duas coisas opostas, e errou dos dois
lados nos dois traces que existem:

* **`6633351e`** anotou a resposta na 2ª decisão e gastou **23 passos e 110s**
  depois dela, terminando `failed` no teto — com a resposta certa no caderno;
* **`78d998ee`** chegou ao `done` no passo 20 de 25. Terminou certo, mas quase
  não coube: uma coleta de vinte itens (um clique e uma anotação cada) não cabe.

**A régua é o CADERNO, não o movimento.** Ele é literalmente o que o job tem
para entregar. Rolar oito vezes para percorrer uma lista longa é trabalho
legítimo — o `78d998ee` fez exatamente isso e terminou certo. Voltar e revoltar
sem nada entrar no caderno é giro. Um contador de "passos sem novidade na tela"
não separa os dois: **rolar fabrica tela nova**, e foi por isso que a primeira
versão desta régua cortava o job que tinha dado certo no passo 14.

Três peças:

- **`_sem_ganho`** — passos de atuação desde a última vez que o caderno cresceu.
  Vale ZERO enquanto o caderno está vazio: antes de existir algo para entregar,
  procurar É o trabalho, e cortar aí seria cortar a tarefa.
- **`_teto_de_passos()`** — o orçamento é elástico **para cima e só por
  produção**: cada crescimento do caderno devolve um passo, até o dobro do
  orçamento base, que continua sendo o limite duro (é o navegador dele, e ele
  está olhando).
- **`_motivo_para_parar()`** — encerra por falta de retorno, no topo da rodada,
  **antes de gastar a decisão**: parar cedo custa um snapshot, não mais uma
  chamada ao modelo. Dois desfechos, dois motivos escritos para o usuário:
  *"12 passos seguidos sem acrescentar nada ao que eu já tinha"* e *"voltei à
  mesma tela N vezes e não consegui nada para te entregar"*. O segundo só vale
  com o caderno VAZIO — revisitar uma tela com coisa anotada é navegação normal
  (abrir um detalhe e voltar).

**A cutucada é o mecanismo; a parada é a rede.** Quatro passos antes de o limite
bater, o prompt ganha: *"se você JÁ TEM o que a tarefa pediu, responda `done`
agora — conferir de novo não melhora o que você vai entregar"*, com a contagem
regressiva. Nos dois traces reais o aviso cai com folga para o modelo fechar
sozinho, que é sempre melhor que ser cortado.

**Os limiares foram MEDIDOS, não escolhidos.** `_SEM_GANHO_MAX = 12` é o número
que faz os dois traces acabarem certo; com 10 o job bom é amputado dois passos
antes de fechar sozinho. Replay das duas sequências reais de decisões contra o
driver:

```
6633351e (girou)       13 passos (real: 27)  failed  → "Parei: 12 passos seguidos sem…"
78d998ee (deu certo)   17 passos (real: 20)  completed → "achei o que você pediu"
```

Ou seja: **metade do desperdício, zero do trabalho.** Os dois roteiros estão
fixados em `tests/browser_local/test_orcamento_por_producao.py` — mudar o
limiar tem que passar por eles.

**A contabilidade fica num lugar só** (`_contabilizar_producao`, no topo da
rodada, medindo o diff de `len(self.notes)`). É o que faz valer para as TRÊS
fontes de nota — o `note` do modelo, o auto-caderno do `extract` e a transcrição
de painel — sem espalhar contabilidade por elas.

`browser_local_max_steps` passou a ser o orçamento BASE; a descrição no
`shared/config.py` e o cabeçalho do diário dizem isso.

**O que isto NÃO resolve:** as 6 rolagens idênticas seguem passando, porque
rolar muda a tela e a guarda de repetição só recusa ação que não mudou nada. A
diferença é que agora elas custam caro — cada uma é um passo sem ganho, e doze
delas encerram o job.

---

### 2026-10-01 — Três jobs mortos em 73s, nenhum por burrice do modelo

Sessão `277a4afd`: o usuário pediu a mesma coisa **quatro vezes** e recebeu
quatro "já mandei, te aviso quando terminar". Nenhum job respondeu. Os três
morreram em guardas NOSSAS que não deixavam saída.

**`7fd73dca` (11s)** — a Amazon redirecionou `/gp/css/order-history` para o
signin. **Não existia tratamento nenhum para "caí numa tela de login"**: o
modelo tentou `fill «pks:email»`, falhou, e repetiu a MESMA ação três vezes até
a regra das quatro recusas matar o job. A recusa que ele recebia ("escolha outro
elemento, dblclick, rolar, navegar direto") não resolve nada quando o que falta
é a credencial.

**`14578a69` (54s)** — o pior, e a cadeia é toda nossa:

1. o carrinho «Subtotal» abriu sozinho na página de detalhes do pedido;
2. foi para a triagem visual e **a visão respondeu VAZIO** (7,8s, nada);
3. a triagem caiu no default `conteudo` ("o erro que custa caro é fechar a
   resposta");
4. e o prompt então DECLAROU: *"O PAINEL «Subtotal» ABRIU E É CONTEÚDO… NÃO
   feche, NÃO aperte Escape… a resposta está AQUI DENTRO"*. **O driver disse ao
   modelo que o carrinho de compras era a resposta e proibiu fechá-lo.** O
   `extract` passou a ler o carrinho (`(li de «Subtotal»): Subtotal R$ 75,77…`);
5. e aí as duas recusas de leitura **apontaram uma para a outra**: a do `look`
   mandava usar `extract`, a do `extract` mandava ler o snapshot ou "escolher
   outro caminho". `extract → look → extract → look → extract`, quatro recusas,
   job morto — com a página certa aberta na tela.

**`23cde731` (8s)** — abriu **aba nova** apesar de a tarefa dizer "na aba que já
está aberta"; sem a aba, o modelo chutou `amazon.com` (EUA) em vez de `.com.br`,
caiu no signin, emitiu `done` sem `answer` e morreu num `Debugger unattached`.

#### Os quatro consertos

**1. O print apagava a sessão de aba (regressão).** `_gravar_sessao` grava o
`browser_session`; o bloco dos prints montava o `extra_metadata` dele a partir
da cópia **em memória** do job, de antes dessa gravação, e sobrescrevia. Medido
nos 16 `browser_local` mais recentes: **todo** job com `images` tem
`browser_session` nulo; todo job com `browser_session` não tem `images`. O bug é
antigo — mas era intermitente, porque só jobs que por acaso tiravam print
perdiam a continuidade. **Virou 100% quando o print do fim passou a existir
sempre** (2026-09-30). Agora `_gravar_sessao` devolve o metadata vivo e quem
escreve depois parte dele.

**2. As recusas de leitura paravam de se apontar** (`_recusa_de_leitura`). A
partir da SEGUNDA recusa de leitura na mesma tela, a mensagem deixa de sugerir
outro jeito de ler e manda AGIR, nomeando alvos pelo número
(`_saidas_da_tela`) — e oferecendo encerrar com `done` e o que já se tem. É a
lição de 2026-08-29 em forma nova: lá as recusas não ofereciam saída; aqui cada
uma oferecia, e a saída de uma era a outra.

**3. Triagem que falhou parou de declarar o painel como resposta.** O default
cauteloso (`conteudo`) continua certo para tudo que vem depois — fechar a
resposta é o erro caro. O que estava errado era o TEXTO: com `fonte` em
(`default`, `erro`) o render agora diz *"ABRIU e eu NÃO consegui classificar…
se for estorvo, pode fechar e seguir"*. **A própria guarda de fechamento já
discordava do texto antigo** — ela só recusa com `fonte` de evidência
(`causa`/`visao`/`dom`), ou seja, fechar sempre foi permitido ali; só o prompt
dizia o contrário. Resposta vazia virou um caso distinto no diário: não é "olhei
e não entendi", é "a triagem não aconteceu" — 16 vezes nos últimos 20 MB de log.

**4. Parede de login** (`_PAREDE_DE_LOGIN`): uma rodada numa tela de login é
acidente de redirect e o modelo ainda pode sair sozinho; a segunda, com o
caderno vazio, encerra com *"a página pediu LOGIN e eu não tenho como entrar na
sua conta"*. Antes disso o prompt já avisa, com a saída (`done` honesto). O
casamento é por SEGMENTO do caminho, não por pedaço de palavra: sem a borda,
`/help/login-faq` virava parede — e falso positivo aqui encerra tarefa legítima.
Dez URLs reais sob teste.

Testes: `tests/browser_local/test_saidas_sem_saida.py` (4) e dois novos em
`test_sessao_de_aba.py`. 3341 no total.

#### Achado que NÃO foi consertado, e por quê

**`self._last_seen_url` nunca é atribuído depois do `__init__`.** É a URL que
escopa a substituição de `«pks:campo»` no `fill` (release-request §7.2) — com
ela sempre vazia, **toda** substituição falha com *"não sei em que página estou
— não substituo dado sem domínio"*. O preenchimento escopado está morto neste
driver desde que existe, e foi nessa mensagem que o job `7fd73dca` bateu quatro
vezes.

Consertar é uma linha. Não fiz porque o efeito não é "voltar a funcionar": é
**passar a injetar dado pessoal dele em formulário de site**, quando houver
liberação aprovada para o domínio. A autorização por domínio continua valendo e
é ela que decide — mas ligar isso é decisão dele, não minha.

### 2026-10-30 — O conteúdo principal era o menu do site (job `29c739d4`)

Pedido: *"usando o meu navegador local, veja se tem alguma compra para chegar na
Amazon"*. Resposta certa: **nenhuma** — os 5 pedidos dos últimos 3 meses estavam
todos entregues. O job gastou 20 ações em 75,6s, terminou `failed`, e a mensagem
que chegou ao usuário trazia, como *"o que consegui coletar"*, o menu de atalhos
de teclado da Amazon.

As 20 ações: `1 navigate · 5 extract · 6 scroll · 1 press Home · 3 click · 4
look` — **4 recusadas** pelos freios antes de tocar na página. A visão respondeu
a tarefa duas vezes (*"todos os pedidos estão Entregue, nenhum A caminho"*) e as
duas foram descartadas.

#### A medição

O texto da página tem **13.249 caracteres** e o primeiro pedido começa no
caractere **3.263**. As duas janelas de texto do snapshot — `main[0:2200]` e
`corpo[0:1500]` — começavam **as duas no caractere 0 da MESMA string**: 3.700
caracteres de orçamento rendendo ~2.400 únicos, e **zero** úteis, porque o
prefixo comum era a moldura inteira do site. O corte efetivo caiu 659 caracteres
antes do primeiro pedido.

A eleição do conteúdo principal escolheu o wrapper da página (rótulo `"Ir para"`,
os skip-links) em **10 dos 15 snapshots**. A página de pedidos não tem `<main>`
nem `role="main"` — zero ocorrências — então o caminho rápido não achava nada e
o fallback decidia tudo, com o critério `t.length > principal.texto.length`:
*o maior texto ganha*. Como `closest(CHROME)` só rejeita quem **está dentro** da
moldura, nunca um `<div>` que a **embrulha**, esse critério converge sempre para a
raiz. O defeito já estava escrito no comentário do próprio `js_snapshot` desde
2026-09-05 — *"na Amazon o vencedor foi um `<nav>` de skip-links"* — anotado ao
consertar o modal, e o picker nunca foi mexido.

#### Cinco camadas, cada uma suficiente para salvar o job

1. **Eleição por texto LÍQUIDO** (`js_snapshot`, Rust). A pontuação desconta o
   texto das subárvores de moldura, e o desempate — dentro de 10%, que é o caso
   NORMAL, porque o container da lista e o wrapper que o embrulha têm quase o
   mesmo conteúdo — é pelo bloco **mais profundo**. `textContent` no desconto, e
   não `innerText`: innerText força layout e esta varredura não pode virar
   O(página); como é desconto, superestimar penaliza o wrapper, que é o lado
   seguro do erro. Estar dentro de um `<aside>`/`[role=search]` também
   desqualifica da ELEIÇÃO — o que não é o mesmo que carimbar `item.chrome`, que
   esconde, e foi assim que o painel do Drive virou invisível no job `9025d9f8`.
   Medido na captura real: a moldura soma 2.384 caracteres, e descontá-la move o
   primeiro pedido do caractere 3.263 para o **879**.
2. **Último recurso para página sem elemento interativo no conteúdo.** O
   fallback subia a partir dos CANDIDATOS, que são elementos interativos — um
   artigo puro, uma fatura, um texto legal nunca era considerado e caía no mesmo
   desfecho. Descoberto pela fixture `com-aside.html`, não pelo job.
3. **As duas janelas pararam de se sobrepor** (`js_snapshot` + `_render`). Os
   literais 2200/1500 viraram derivados de `text_chars` — que o core já mandava e
   que era **ignorado** sempre que havia principal eleito, então não existia
   alavanca nenhuma. E o piso de 200 caracteres da deduplicação saiu: o que ele
   fazia na prática era RESTAURAR o duplicado, porque o corpo chegava pré-cortado
   em 1500 e o que sobrava era subconjunto do principal. Com principal eleito o
   corpo é fallback e ganha teto curto (`_CORPO_COM_MAIN`).
4. **As duas mensagens que mentiam** (`_render`, `_onde_esta_o_texto`). A recusa
   de releitura afirmava, SEMPRE, que *"o conteúdo dos painéis já vem inteiro no
   snapshot (inclusive a parte abaixo da dobra)"*. Não havia painel nenhum e o
   texto estava cortado: cada vez que o modelo concluía, **corretamente**, que o
   dado não estava no texto, a recusa respondia com autoridade que estava — cinco
   vezes. E o aviso de corte mandava **rolar**, quando o texto sai em ordem de
   documento e rolar não muda o prefixo; o modelo obedeceu sete vezes (6 `scroll`
   e um `Home`). Conselho errado, repetido com autoridade, é o que fabrica giro.
   Agora a recusa só afirma o que o snapshot daquele passo sustenta, e tem um
   quarto regime: **device antigo**. O core sobe sozinho e o Rust só depois do
   restart — sem a chave `text_cut`, "não houve corte" é *desconhecido*, não
   *ausência de corte*.
5. **A visão vai para o caderno** (`_anotar_visao`). O auto-caderno nasceu para o
   `extract` (job `6785cf70`) e nunca cobriu o `look`. Piso baixo de propósito —
   resposta de visão é resposta a uma PERGUNTA, e a deste job tinha ~150
   caracteres, que o piso de 200 do `extract` descartaria. Duas sutilezas que a
   suíte pegou: *"não consegui ver"* **não é observação** (os quatro caminhos de
   falha da visão viraram a constante `_VISAO_SEM_RESPOSTA`), e nota de visão
   **não devolve passo ao orçamento** — olhar não muda a página, e o freio de
   giro existe para detectar exatamente "nada está mudando", então deixar `look`
   render passo transformaria o freio em acelerador (o replay do job `6633351e`
   virou `completed` até isso ser corrigido).

Em `extract`: a leitura passa a dizer quando saiu da **moldura** — o rótulo
sozinho não bastava, o modelo recebeu três vezes 1500 caracteres de menu
rotulados `"Ir para"` sem como distinguir leitura certa de errada — e moldura
**não entra no caderno nem na primeira vez**. A deduplicação já impedia as cópias
2 a 5 (job `6633351e`), mas a primeira passava, e neste job ela era a *única*
nota: foi exatamente ela que o usuário recebeu.

#### O efeito, medido na mesma página

| | antes | depois |
|---|---|---|
| bloco eleito | `"Ir para"` (skip-links) | `"Sua conta › Seus pedidos"` |
| moldura no bloco eleito | 100% | **0** linhas |
| status de entrega no prompt | 0, em 15 snapshots | **8**, no primeiro |
| nº de pedido e data | nenhum | 4 dos 5 pedidos |
| "Texto da página" | 2.500 chars de menu | 1.385 chars (o que sobrou) |

A tarefa passou a ser respondível em **um** passo.

#### Testes

`tests/browser_local/test_moldura_e_visao.py` (15), uma por camada, mais o
`--check` do `neo-map.generated.js` — que existia desde sempre e não tinha onde
rodar, porque não há CI neste repositório; mexer na eleição sem regerar o mapa é
o erro que passa desapercebido. 3470 no total, suíte inteira verde.

A camada 1 é do device e `cargo test` não alcança string JS: ela é provada em
`neo-desktop-tauri/scripts/snapshot-lab/conteudo-principal.py`, que roda um
**corpus** de cinco formas de página num Chromium headless — a captura real da
Amazon entre elas, como uma amostra. Afinar contra uma página só é como se chega
num atalho por site disfarçado de heurística.

#### Deploy

As camadas 3(core), 4 e 5 são Python: Mutagen + `--reload`, já no ar. As camadas
1, 2, 3(device) e o `moldura` do `extract` são Rust — **exigem reiniciar o
`tauri dev`** (nunca `tauri build`) e, depois, `python3
neo-browser-local/scripts/gerar-mapa.py` com recarga **manual** da extensão.

### 2026-10-30 (2) — O passo final morreu transcrevendo o que já tinha (job `5ab98119`)

Primeiro job depois do conserto da eleição, e ele **deu certo**: 4 pendrives com
preço real, parcelamento, frete, vendedor, nota e URL, todos anotados. O caderno
foi entregue ao usuário (`_com_caderno`) e as duas respostas de visão viraram
nota — as camadas 5 e 6 da entrada anterior funcionando. O device novo estava no
ar: o marcador `"este bloco CONTINUA"` aparece 3 vezes neste diário e 0 no do
job `29c739d4`.

Mas o job terminou `failed`, 20 passos, 144,1s. **O modelo decidiu `done` quatro
vezes, com a resposta completa escrita, e o JSON foi cortado no meio da string
nas quatro.** `max_tokens` da decisão era 700 — dimensionado para "pensamento +
ação + argumentos curtos" — e o `done` carrega **o resultado inteiro** dentro do
campo `answer`: ~1.650 caracteres, da ordem de 600 tokens, mais o pensamento e o
andaime do JSON. Morreu na regra das quatro recusas com tudo já no caderno.

**O sinal do defeito estava invertido: quanto MELHOR a tarefa tinha ido, mais
certo era o passo final falhar.** Uma tarefa de 1 item nunca bate no teto; uma de
5, sempre.

E a recusa não era acionável: *"não entendi a resposta do modelo: Unterminated
string starting at: line 1 column 245 (char 244)"* — mensagem de parser de
Python. O modelo reemitiu a MESMA resposta longa quatro vezes. É a mesma classe
da recusa que mentia no job `29c739d4`: freio que não nomeia a saída vira giro.

- `_DECISAO_MAX_TOKENS = 2000` (teto é limite, não gasto: só custa no passo que
  de fato escreve muito).
- Corte passou a ser um caso PRÓPRIO: resposta completa e inválida termina em
  `}`, resposta cortada termina no meio. A mensagem diz que reenviar vai cortar
  igual, e aponta o caderno — que já viaja em todo passo e já é a resposta.
- A linha do `done` no prompt passou a dizer que o `answer` é curto e que
  recopiar os itens anotados é o que estoura o teto. A regra do caderno já
  existia ("`done` só precisa fechar o que já está anotado") e não bastou: sem
  consequência visível, instrução de prompt não se sustenta.

#### Dois defeitos da ELEIÇÃO que este job expôs

Medidos na página ao vivo pelo `snapshot-lab`, não deduzidos do diário.

**1. `role="dialog"` não é promessa de sobreposição.** A barra de filtros da
busca da Amazon é um `<div role="dialog">` de **262×7957px, em fluxo**, à
esquerda dos resultados — passa por `visivel` e está de fato na tela. A regra
"modal aberto É o conteúdo" a elegia por cima do `#search`, que pontuava **18.711
caracteres líquidos contra os 1.305 dela**. Em 4 snapshots o modelo leu marcas e
faixas de preço como se fossem a lista de resultados. Agora o modal precisa
SOBREPOR: `aria-modal="true"`, ou `position: fixed` em até 5 ancestrais (o
desenho comum é fundo `fixed` com a caixa `absolute` dentro). Fixture nova
`com-modal-aria.html` protege o modal legítimo, que é por causa de quem a regra
existe.

**2. `NEO.visible` não tem teste horizontal nenhum.** O carrinho lateral
(`#nav-flyout-ewc`, `role=dialog`) vive no DOM em **`left: 1819` numa janela de
1600px** — fora da tela — e sequestrou o conteúdo principal em **10 dos 17
snapshots**, inclusive nos passos em que a visão respondeu *"não há painel
lateral aberto"*. O modelo gastou um `look` de 11s procurando o X de um painel
que não existia, e o 4º produto teve de sair por visão porque o bloco de
conteúdo era um resumo de carrinho. A generosidade do `visivel` (até 2000px
abaixo da dobra, sem limite lateral) é deliberada — elemento fora da dobra ainda
precisa virar `ref` — então o teste de interseção entrou **só no ramo do modal**,
onde "está aberto" é a premissa.

**3. O teto de 6 níveis da subida favorecia região RASA.** Numa grade de
resultados o container fica ~10 níveis acima dos links dos produtos, fora de
alcance; o container de uma barra de filtros, raso, entrava. O teto existia
porque, com a pontuação por `t.length`, subir mais sempre ganhava. Com o desconto
de moldura a raiz pontua ZERO e se autoexclui (`#a-page` na busca: 21.426
caracteres de `innerText` contra 48.893 de moldura por `textContent`, que inclui
menu escondido) — então o teto virou só o obstáculo. Saiu; o limite de custo
continua sendo o `olhados`, agora checado também dentro da subida.

#### O efeito

Na busca ao vivo, o bloco eleito passou de «Filtros» (1.305 caracteres de marcas
e faixas de preço) para «Exibindo resultados para pen drive 64gb», com **41
preços dentro do orçamento padrão de 3.000** — incluindo os R$ 65,76 e R$ 72,10
que o job levou 144s e quatro visitas a páginas de produto para colher um por um.

Testes: 3 novos em `test_moldura_e_visao.py` (18 no arquivo), 3474 no total;
corpus da eleição em 6 casos.

### 2026-10-30 (3) — A resposta final tinha DOIS donos

Continuação direta do item anterior. Subir `_DECISAO_MAX_TOKENS` para 2.000
resolvia a tarefa de 4 produtos e não resolveria a de 15: teto é remendo quando
o problema é *quem escreve a resposta*.

#### O que a varredura dos outros seis laços mostrou

Em `deep_read`, `deep_research`, roteador, entrega de job, PKS e planejador, **a
última chamada ao modelo não produz o resultado**. Três mecanismos, usados
juntos:

1. **acumular fora do turno** — o `deep_read` lê parte por parte, 3.000 tokens
   cada, e nenhuma janela nunca vê o documento inteiro;
2. **montar por código** — o relatório da pesquisa é concatenação (uma chamada
   por seção); o `deep_read` escreve `notas/<doc>.md` e `sintese.md` no disco;
3. **a última chamada só fecha**, e vê o material já cortado pelo código
   (`[:4000]` por rascunho na síntese, `[:3500]` no redator da entrega).

E quando estoura mesmo assim, quatro defesas — cada uma com o incidente de prod
anotado no comentário: **salvar o que veio inteiro** (`_salvage_object_list`,
`salvage_findings`, `_salvage_operations`), **retentar com mais espaço** (o
roteador, que distingue corte de prosa pelo `finish_reason`), **ter resposta que
não depende do modelo** (`return text or fallback`) e **degradar em vez de
morrer** (consolidação que falha 2× ainda entrega as evidências conferidas).

O navegador local era o único laço sem nenhuma das quatro — e o único em que o
modelo recopiava o resultado.

#### A contradição que matava o job

O prompt dizia uma coisa: *"o caderno viaja em todo passo e é ELE que vira sua
resposta no fim"*. A recusa do `_validate` dizia o contrário: *"o `answer` É a
resposta que o usuário vai ler"*. Essa frase foi escrita em 2026-09-02, quando o
problema era o oposto (o modelo emitia `done` vazio e não entregava nada).
Consertou aquele incidente e plantou este — e o modelo acredita na recusa,
porque é o que ele lê no momento em que está errando.

Pior: a prosa do `answer` **é reescrita de qualquer jeito** uma camada acima.
`browser_local` nasce com `notify_policy="chat"`, então o `result_summary` passa
pelo `_compose` do dispatcher, que tem orçamento de prosa, `disable_thinking`, a
persona e `_fallback_text` sem modelo atrás. O trabalho que matou o job era
trabalho descartável.

#### O conserto, em cinco camadas

1. **`answer` só é obrigatório com o caderno VAZIO** (`_validate`). Uma
   condicional resolve as duas falhas de sinal oposto: sem caderno e sem texto
   não há o que entregar (02/09, recusa certa); com 4 produtos anotados, a
   recusa matava um job que tinha dado certo (30/10).
2. **O `done` é pescado de dentro do JSON cortado** (`_parse`). O `answer` é o
   último campo e o único que cresce com a tarefa, então o corte cai nele e a
   intenção de terminar chega inteira. O **fragmento é descartado**: meia frase
   ("…- R$ 5") como linha de fecho entrega um preço truncado ao usuário. É o que
   torna esta pescaria mais segura que as outras três da casa — nada do conteúdo
   vem do texto salvo. Só o `done` é pescado: um `click` cortado pode ter
   perdido o `ref`.
3. **A frase que ensinava o errado saiu.** A recusa do quadrante vazio diz o que
   fazer (`note` primeiro, ou `extract`, ou `give_up`) sem dizer que o `answer`
   é a resposta.
4. **Tamanho em número, não em adjetivo**: `_ANSWER_MAX_CHARS = 300`, no prompt.
   Mesma ideia do `_words_for` da pesquisa — o alvo tem de caber folgado no teto.
   Estourar o dobro só deixa marca no diário (`"answer longo"`), que é a medida
   de se o pedido está pegando.
5. **`finish_reason == "length"`** (`_foi_cortado`) no lugar de adivinhar pela
   cauda ("termina com `}`?"), que erra nos dois sentidos. Ausência é "não sei",
   nunca "não foi cortado" — mesmo princípio do `text_cut` do device antigo.

De lambuja: `_com_caderno` passou a receber o `status`. Com o `done` fechando em
uma linha, colar o caderno virou o caminho **normal do sucesso**, e o rótulo
"O que eu TINHA coletado até parar" mentiria sobre o desfecho.

Testes: 10 novos em `test_moldura_e_visao.py`, 3484 no total.

### 2026-10-30 (4) — A entrega em duas metades (o `[:3500]` do `_compose`)

O mesmo defeito uma camada acima, e a medição mudou o diagnóstico. Em 227 jobs
de prod, o corte em 3.500 disparou **uma vez**; 27 jobs passaram do `[:1200]` do
fallback. **O problema nunca foi o corte — era a compressão:** o `_compose` lia
até 3.500 caracteres e escrevia no máximo ~1.470 (700 tokens de prosa em pt-BR),
então todo resultado entre os dois números era reescrito por um modelo sem
registro do que caiu. Para lista de fatos — preço, nome de arquivo, caminho,
uuid de retomada — parafrasear é a operação errada.

Pior que o óbvio: o `continue_job="<uuid>"` que permite retomar uma tarefa
pausada **só chega ao cérebro pela mensagem entregue**, e até aqui chegava lá
depois de passar pelo redator.

#### O princípio já era da casa, duas vezes no mesmo arquivo

O `texto_literal` da agenda entrega sem passar pelo redator, e o
`_append_images` pendura as imagens **depois** dele — *"um LLM redigindo no seu
tom de sempre reescreve, resume ou come um token estranho no meio do texto"*.
**O que tem de chegar íntegro passa por fora do redator.** As imagens já
passavam; os fatos não. Isto é a mesma regra em grão mais fino: **o modelo
escreve, o código entrega.**

#### O desenho

`extra_metadata["entrega"] = {"fecho": str, "itens": [str]}` — coluna `JSON` que
já existe, **zero migração**. O `DriverResult` ganhou `fecho` (o `summary` antes
de o `_com_caderno` colar), e o runner declara, pedaço por pedaço, o que é prosa
e o que é fato:

| | |
|---|---|
| **fecho** (redigível) | linha do `done`, aviso do `_honest`, aba onde parou, "📸 N prints em anexo", "⏸ Parei e preciso da sua autorização" |
| **itens** (íntegro) | o caderno, a instrução de retomada com o uuid, os nomes dos arquivos baixados, o que ainda está vindo, o caminho do diário |

No dispatcher: `_compose` vê **só o fecho** mais a contagem de linhas do bloco —
não pode reescrever o que nunca recebeu. `_com_itens` cola o bloco por fora,
agrupando achados (em lista) e rodapés (com glifo) em parágrafos separados. A
ordem do `dispatch` é a regra em código: `_compose` → `_com_itens` →
`_append_images`, as duas últimas por fora do modelo.

O `result_summary` **não mudou** — é contrato com o cérebro e com o caderno do
servidor. O raio de alcance é só o caminho da entrega.

#### Os três cortes deixaram de ser mudos

`_COMPOSE_ENTRADA_MAX` (3500), `_FALLBACK_ENTRADA_MAX` (1200) e
`_COMPOSE_SAIDA_ALERTA` (1400) agora logam quando disparam. O terceiro é o que
importa: prosa redigida perto do teto quer dizer que o redator está
**transportando** conteúdo, e é a medida que diz qual `kind` ainda precisa
declarar `entrega`. Era a medição que faltava — e nenhum dos três registrava
nada.

No caminho do fallback o corte deixou de existir em vez de ser afrouxado: com
`entrega` declarada, cabeça fixa + fecho inteiro, e os itens colados uma vez só
pelo `_com_itens` (nos dois caminhos, para não duplicar).

Testes: 8 novos (7 em `tests/test_jobs.py`, 1 no driver), 3492 no total. Guarda
de regressão explícita para os 8 outros `kind`, que seguem no caminho de hoje
até declararem `entrega`.

---

### 2026-10-30 (5) — A faixa virou painel: a linha do tempo do que está acontecendo

A faixa no topo da aba tinha **uma linha**, e cada passo apagava o anterior
(`js_banner` → `window.postMessage({__neo_overlay:{text}})` → o content script
trocava o `textContent`). Duas consequências:

1. **o histórico não existia na tela.** Quem estava olhando a própria aba mexer
   sozinha via o presente e nada mais. "O que ele fez até agora?" só tinha
   resposta no diário do job, depois;
2. **o que o agente PRODUZIU não tinha onde caber.** A leitura do `extract`, a
   resposta da olhada com visão, o rótulo do que foi clicado de verdade, o
   motivo da falha — tudo isso já era calculado e virava `detail` do `Step`,
   mas a faixa só recebia `thought[:90]`. O retorno do passo nunca chegava aos
   olhos do usuário.

Agora cada evento **empilha** um item num painel na lateral direita, com tipo:
`fase` (preparação), `passo` (o raciocínio como manchete, a ação concreta como
legenda — `passo 3/14 · clicando em «Entrar»`), `ok` (a primeira linha do
retorno como manchete, o resto como detalhe), `erro`, `parado`, `fim`.

| camada | mudança |
|---|---|
| `driver.py` | `_banner(text, *, kind, detail)`; chamadas novas no retorno de cada passo (sucesso **e** falha), no `done`, no `give_up`, na recusa e na parada por improdutividade. `_acao_em_palavras()` traduz `click {'ref': 42}` para "clicando em «Entrar»" — e **nunca** o `value` do `fill`, mesma regra do `Step.render` |
| `browser_local.rs` | `js_banner` ganhou `kind` e `detail`; `browser.banner` repassa os dois |
| `content-overlay.js` | reescrito: painel lateral, lista rolável com teto de 60 itens, repetição vira contador, detalhe cortado em 3 linhas com clique para abrir, recolher para pastilha |
| `background.js` | o estado por aba passou a carregar a lista inteira (com o mesmo teto, porque é o que vai para o `storage.session`) |

**O que o painel não pode fazer, e como fica provado.** Ele é maior que a faixa,
então os riscos cresceram junto:

- **roubar clique / empurrar a página**: `position:fixed`, shadow fechado,
  `pointer-events:none` fora do painel, e o host segue sendo o último filho do
  `<html>` — a mesma lição de §"A faixa da extensão estava ocupando espaço na
  página" (o `all:initial` continua declarado PRIMEIRO);
- **aparecer no inventário**: o id (`neo-browser-local-overlay`) já é excluído
  pelo snapshot e pelo `JS_RECTS`. Mudar o id faz o agente enxergar o próprio
  painel como conteúdo;
- **comer a foto**: uma faixa de 44px cabia no print; uma lateral de ~310px
  come um quarto da largura útil e cobre conteúdo que o modelo de visão precisa
  ler. `browser.screenshot` agora **esconde** o painel pelo id (`visibility`,
  síncrono pelo mundo principal) antes do `Page.captureScreenshot` e devolve
  depois — inclusive quando a captura falha.

`scripts/testar-painel.py` (na extensão) roda os três num Chromium de verdade:
mede a página antes e depois, compara os pixels das fotos com e sem o painel
(a caixa de diferença tem que ficar na metade direita), e clica no PARE **por
coordenada, achando o botão pelo vermelho** — o shadow é fechado, então o teste
chega nele pelo mesmo caminho que o olho do usuário. Foi esse teste que pegou a
primeira regressão: ao fazer o painel encolher para a altura do conteúdo, o
PARE saiu do pé da janela.

Do lado do core, `tests/browser_local/test_painel_linha_do_tempo.py` (4 testes):
os quatro momentos são emitidos, a falha vai com o motivo, o detalhe respeita o
teto de 1200 caracteres — e o que foi digitado num `fill` não aparece em evento
nenhum. Este último não é higiene: o painel fica **na tela**, e portanto dentro
de todo print que o agente tirar depois.

---

### 2026-10-30 (6) — A vistoria de tela entregue como se fosse achado (job `4a1e6748`)

Busca por "monitor OLED 27 polegadas" na Amazon. Cinco olhadas: três
perguntaram preço e acertaram; duas perguntaram se havia um painel de carrinho
na frente. Essas duas foram as únicas notas ruins da entrega — e uma delas
entregou isto ao usuário:

> NÃO. Não há painel lateral de carrinho aberto. O botão X para fechá-lo não
> está visível, pois o carrinho não está expandido. A página do produto (preço,
> frete) está totalmente visível atrás — **preço R$ 43,90**, frete grátis, e
> todos os detalhes técnicos estão legíveis. Nenhum overlay ou modal cobre o
> conteúdo principal.

**R$ 43,90 numa busca por monitores de R$ 3.199,90.** Ninguém perguntou preço
ali: os olhos preencheram a frase. É a regra que faltava — *conteúdo que
ninguém pediu é conteúdo que ninguém conferiu* — e o caderno é o que vai para o
usuário (`_com_caderno` entrega mesmo quando o desfecho é `failed`).

**O conserto está no CADERNO, não no `look`.** `_anotar_visao` passou a receber
a PERGUNTA junto da resposta: se a pergunta é uma vistoria de obstrução, não
vira nota — nem que a resposta venha cheia de números. O `look` continua indo
para os olhos.

**A primeira tentativa estava errada nas duas pontas, e o log provou.** Ela
interceptava o `look` antes dos olhos e respondia pelo `_panels_now`:

1. **não teria disparado.** `_e_vistoria_de_obstrucao` olhava a frase inteira,
   via o "Onde está o botão X" da SEGUNDA cláusula e classificava como pergunta
   aberta. As duas vistorias reais do job passavam direto — 0 de 5 nas
   perguntas verdadeiras. Agora julga só a PRIMEIRA cláusula (é o que o modelo
   quer saber; o resto é guarnição): 5 de 5;
2. **se tivesse disparado, teria piorado.** A vistoria real vinha GRUDADA numa
   pergunta de conteúdo ("…a página do produto (preço, frete) está visível
   atrás?"). Responder pelo snapshot jogaria fora a metade que o modelo foi
   buscar — e `_panels()` não é detector completo de obstrução (é por isso que
   `_triage_panel` existe e usa visão): responder "não há painel" quando há
   trava o job com uma mentira barata.

Duas limpezas menores no mesmo caminho, pelo mesmo motivo (o caderno é texto
para o USUÁRIO, não para o modelo):

- `_sem_vistoria()` tira da resposta boa o veredito de brinde ("Nenhum overlay
  ou modal cobre o conteúdo principal") e o "SIM."/"NÃO." solto da frente;
- `_SEM_MARCACAO` tira `(elemento 124)` — o número da marcação do print é
  contrato entre o driver e os olhos, e saía inteiro em três das cinco notas.

E uma linha a mais na lista "NÃO use `look` para" do prompt: perguntar se tem
painel cobrindo a página, que o snapshot já responde a cada passo.

**Medido no job real** (as 5 notas, replayadas pelo caminho novo): 5 notas de
visão → 3. Saíram `R$ 43,90`, `(elemento …)`, `overlay` e `painel lateral`;
ficaram os três preços (R$ 3.199,90, R$ 3.899,00, R$ 4.100,00).

`tests/browser_local/test_vistoria_de_obstrucao.py`: 10 testes com as perguntas
e respostas do job palavra por palavra, incluindo a guarda da primeira
tentativa (o "Onde" da segunda cláusula não pode salvar a vistoria) e as duas
de regressão — "não há pedidos a caminho" (job 29c739d4, em que a visão ERA a
resposta) e o painel que É o conteúdo pedido.

---

### 2026-10-31 — A guarda de painel virou a prisão (job `b22acf4b`)

Tarefa: 3 a 5 anúncios de TV 55" na Amazon, com preço e frete. **Morreu no
passo 9 de 40, em 68 segundos, com UMA nota no caderno**, dizendo "Travei:
quatro tentativas seguidas foram recusadas antes de chegar na página".

O que aconteceu, na ordem:

1. o carrinho da Amazon («Subtotal») abriu — sozinho, com um protetor de surto
   de R$ 54,90 dentro, nada a ver com TV;
2. a triagem visual foi chamada e **voltou VAZIA**. O default conservador é
   `tipo: conteudo` (o erro caro é fechar a resposta), com `fonte: erro` para
   marcar que não houve evidência;
3. o prompt, corretamente, disse ao modelo: *"Pode ser o resultado do que você
   pediu, e pode ser estorvo (carrinho, promoção, cadastro). (…) **se for
   estorvo, pode fechar e seguir**"*;
4. o modelo mandou `Escape` — e a guarda de vaivém respondeu *"você já abriu e
   fechou o painel «Subtotal» 2 vezes (…) **A informação está DENTRO dele**"*;
5. quatro vezes. Teto de recusas seguidas. Fim da tarefa.

**O prompt autorizava e a guarda proibia, sobre um painel que ninguém
conseguiu classificar.** Guarda que contradiz o prompt não corrige o modelo —
trava ele.

Três consertos, todos no mesmo princípio que já estava escrito no arquivo e só
valia para metade dos casos:

| | antes | agora |
|---|---|---|
| **evidência** | a guarda de vaivém não olhava o veredito | só recusa com `fonte` em `causa`/`visao`/`dom`. Triagem que falhou (`erro`) ou nem rodou (`default`) não dá base para dizer "está DENTRO dele" |
| **teto** | recusa ilimitada (vaivém sempre; conteúdo sempre, exceto `causa`) | **UMA recusa por painel**, contador compartilhado. Depois do aviso, a ação passa |
| **memória** | `_panel_verdict` era limpo quando o painel sumia da tela | `_panel_dismissed` guarda o que o modelo já julgou estorvo e sobrevive ao fecha/abre |

O teto é o ponto central: **depois do aviso a ação vira ação de verdade, e laço
de ação tem detector** (`_note_change`, `_stuck`, a guarda de repetição). Laço
de RECUSA não tem — a ação nunca roda, `_pending_signature` nunca é armado,
nada sobe, e a única coisa que acontece é o teto de quatro que mata a tarefa
inteira. Trocar uma prisão por um detector é estritamente melhor.

A memória é a outra metade, e é o que o usuário pediu com todas as letras:
*"apenas ignorasse nos próximos passos se já julgou irrelevante pelo passo
anterior"*. Na Amazon o carrinho reabre a cada navegação; sem ela, cada
reabertura recomeçava do zero — nova triagem (mais uma olhada paga), novo
">>> pode ser o resultado do que você pediu", nova recusa. Agora o veredito
vira `intruso` com `fonte: "modelo"` — que **não** está na lista de evidências
que autoriza recusa, de propósito: ele só muda o que o prompt diz, nunca vira
bloqueio.

Quatro testes novos em `test_painel_triagem.py`, incluindo o job inteiro em
miniatura (triagem que volta vazia + quatro `Escape`). Os quatro falham contra
o código de ontem. A guarda central continua de pé: fechar o painel que É a
resposta ainda é recusado — uma vez, com a saída junto.

---

### 2026-10-31 (2) — O modelo lia o DOM; o usuário lia a tela (job `45e85695`)

Os dois consertos anteriores (a vistoria fora do caderno, a guarda com teto) tratavam
sintoma. A pergunta do usuário foi a certa: *"veja se a forma como o neo lê a página
não está cortando as partes relevantes, e por isso ele tenta fechar"*. Estava.

Dois jobs na mesma tarde, no mesmo site, são o experimento natural:

| | `2d416462` | `45e85695` |
|---|---|---|
| navegador | headless, 1280×819 | o real, ~1912px de largura |
| carrinho | fora da tela (`left: 1819`) | visível, preso na borda |
| painel do Neo | não existe no headless | por cima da página |
| resultado | 12 itens em 100s | 3 itens em 196s, `failed` |

**A causa, em quatro camadas que se reforçavam:**

1. **O carrinho virou "conteúdo principal".** A regra era "`role=dialog` + `fixed`
   visível na tela É o conteúdo". Nas janelas de menos de ~1830px o carrinho fica
   fora da tela e o filtro `naTela` o barrava; numa janela larga ele entra. O print
   é conclusivo: é uma faixa de 150px, com a página inteira visível ao lado, sem X,
   que não fecha com nada. Nos 19 snapshots de página de produto em que ele foi
   eleito, 15 não tinham o preço do produto e 4 só o traziam enterrado numa linha de
   variação. Para quem recebia aquele prompt, fechar o painel era a decisão racional.
2. **O painel que eu fiz fabricou o obstáculo.** `elementFromPoint` devolve o host do
   overlay, um `div` fixo de shadow fechado, e todo elemento embaixo dele saía
   `⟪COBERTO por "div"⟫`, cuja regra é "dispense o intruso primeiro". `COBERTO por
   "div"`: 0 nos três jobs anteriores ao painel, 432 no primeiro depois, 540 neste.
3. **A saída oferecida não existia.** O aviso de corte mandava "`extract` no `ref` do
   bloco", mas texto puro não tem `ref`: o modelo chutou o 0 (um skip-link) e o 110
   (uma imagem, que devolveu vazio).
4. **A triagem nunca aconteceu.** `_look` manda `disable_thinking`, `_triage_vision`
   não mandava: 10 de 10 triagens vazias em 10,1s, contra 0 de 15 olhadas em 4,2s. O
   qwen raciocina por default e isso consumia a resposta. Cada vazia virava
   ">>> ABRIU e eu NÃO consegui classificar… pode fechar e seguir".

**O conserto de fundo foi mudar a pergunta.** Todos os remendos anteriores decidiam
*qual pedaço do DOM vai na frente*, e bastava a eleição errar para o modelo ficar
cego. `NEO.ler` pergunta **o que está na janela agora**: anda pelos nós de texto em
ordem de documento e fica com o que está visível. Não depende de landmark, de
framework nem de eleição, e rolar passa a trazer texto novo. Na mesma página do
notebook: 1.385 caracteres em 11 ms, com preço, parcelas, frete, estoque, vendedor e
nota. É a seção `>>> NA TELA AGORA`, a primeira do prompt.

| camada | mudança |
|---|---|
| leitura | `tela` e `resto` no snapshot. `main` e o corpo passam a ser só "o que a tela ainda não mostrou", por continência, e o dump em ordem de documento some quando há `tela` |
| painéis | `quadro()` classifica pela GEOMETRIA: `fora`, `fluxo`, `lateral`, `bloqueia`, `solto`. Só `bloqueia` toma o lugar do principal. `lateral` nem é painel: sem triagem, sem veredito, com seção própria dizendo para ignorar |
| hit-test | `NEO.topo` pula os overlays do Neo e atravessa shadow DOM aberto. Usado nos 5 pontos que testavam a tela |
| teto de elementos | coleta o triplo, o que está na janela primeiro, e o teto escolhe por relevância, com cota para a moldura. Antes cortava em ordem de documento: 0 de 6 botões visíveis na fixture saturada, agora 6 de 6 |
| alcance | `extract {}` lê a página sem menus, `{"de": N}` continua, `{"busca": "termo"}` é o Ctrl+F e diz quando não tem. Nada mais fica atrás de um corte sem saída |
| outros sites | shadow DOM aberto em elementos e texto; iframe do mesmo domínio é lido, o de outro domínio é declarado com o endereço |
| velocidade | `anotar` na mesma resposta da ação (12 das 28 rodadas do `2d416462` eram só `note`); `assentar` espera a página no próprio device depois do clique (o snapshot saía com 59 elementos, todos de cabeçalho); a trava de olhada só solta depois de uma ação, porque página viva muda o fingerprint sozinha |
| prompt | moldura numa linha, link repetido fora (1 em cada 4 linhas era o mesmo link), rastro de URL fora, histórico podado depois de 3 passos |
| caderno | nota de 400 para 1000 caracteres: a URL pedida saía cortada em `…/dp/B0F` |

**Medido na página real, com carrinho e painel do Neo injetados na geometria do job:**

| | código de antes | agora |
|---|---|---|
| conteúdo principal | `Subtotal` | o bloco do produto |
| parcelas e estoque do produto | ausentes | no topo do prompt |
| "pode fechar e seguir" | sim | não |
| `⟪COBERTO⟫` | 7 | 0 |
| tamanho do prompt | 33.971 | 25.701 |

**O que NÃO está provado.** A cena acima foi montada: a barra foi injetada para
reproduzir a geometria do print, e a página é a de um visitante anônimo. Falta um job
real na janela larga. Nele, procurar no diário por `BARRA LATERAL FIXA`: se ela não
aparecer e voltar `>>> O PAINEL «Subtotal»`, a medida real da barra escapou dos
limites de `quadro()`, e mesmo assim `NA TELA AGORA` continua trazendo o preço.

**Cuidado que ficou para quem medir de novo:** procurar o preço por substring engana.
Na primeira versão da tabela acima, "3.399" e "Entrega GRÁTIS" deram verdadeiro no
código antigo: eram de outro notebook e de um mouse, nas recomendações.

Provas: `snapshot-lab/leitura.py` (39 verificações em Chromium real, com o
`content-overlay.js` de verdade), `tests/browser_local/test_leitura_da_tela.py` (24).

---

### 2026-10-31 (3) — 32 decisões atrás de uma foto que nenhuma ferramenta devolvia (job `aa4c89f8`)

O conserto da leitura passou no teste de verdade: janela larga, carrinho na tela,
`BARRA LATERAL FIXA` em 37 snapshots, zero `COBERTO`, nenhum Escape, nenhum `look`, e
5 notas numa decisão só. Os 4 produtos estavam completos em 69s. Só que o job levou
128s, e 32 das 39 decisões foram a mesma coisa: procurar a URL da foto.

**Um pedido impossível.** A tarefa exigia "a URL da FOTO principal" (o cérebro precisa
dela para mostrar a imagem no chat) e dizia para não inventar. Nenhuma ferramenta
devolvia endereço de imagem: o snapshot não traz `src`, `extract` no `img` voltava
vazio com `ok`, e `busca` lê texto. O modelo entendeu isso sozinho ("extract de texto
não pega atributos") e, sem alternativa, continuou tentando.

**Três coisas que eu introduzi na rodada anterior transformaram isso em giro:**

1. a mensagem do `busca` terminava com "se não for assunto que aparece escrito com
   outra palavra, não está aqui". Era um convite, e ele aceitou: 26 buscas, 15 termos;
2. `anotar` não tinha nenhum filtro. O modelo passou a usar o caderno como rascunho
   ("PRODUTO 4 foto: tentar extract 'AC_'") e a reescrever o mesmo produto ("completo",
   "confirmado", "FINAL", "relido"): 35 notas para 5 produtos. Foi isso que você
   recebeu no chat, e por isso teve de pedir "resume essa info";
3. toda nota nova conta como produção e devolve um passo. O orçamento foi de 35 para
   66 e o freio de improdutividade (12 rodadas sem ganho) nunca disparou: havia
   "ganho" em quase toda rodada.

**O conserto:**

| | antes | agora |
|---|---|---|
| imagens | nenhum endereço em lugar nenhum | `>>> IMAGENS NA TELA` com as 8 maiores; `→ url` no elemento `img`; `extract` no `img` devolve o endereço |
| `busca` | só texto | também endereço e nome de links e imagens: "não achei" passa a ser verdade |
| busca vazia | convite a tentar outra palavra | "registre não informado e siga"; mesmo termo na mesma página é recusado, e 3 vazias seguidas encerram a procura |
| trava de repetição | por tela (solta sozinha em página viva) | por endereço |
| caderno | toda nota entra | rascunho é recusado; o mesmo item reescrito atualiza a nota (`_mesma_nota`) |
| produção | caderno cresceu | continua assim, mas agora só dado novo o faz crescer |
| `role=dialog` em fluxo | "O PAINEL «Filtros» ABRIU E É CONTEÚDO", 9.408 caracteres de 29.851 | parte da página (`quadro` = `pagina`) |

As 35 notas reais, replayadas pelo caminho novo: **9** (5 produtos e 4 "foto não
informado"), e o teto de passos pararia em 44 em vez de 66. Na página real do produto
4, a mesma em que o job passou 21 rodadas, o endereço da foto chega no primeiro
snapshot.

Um teste pegou um erro meu antes de ir ao ar: "PRODUTO 2 foto: https://…" tinha
código e texto suficientes em comum com a nota do produto para ser fundida nela, e a
URL ia embora. Nota que traz um endereço que a outra não tem é dado novo, sempre.

#### O que NÃO foi mexido, de propósito

**O portão do `web_fetch`.** Fez o que devia: em turno com conteúdo de terceiro, só
passa URL que veio de um resultado de tool, e das 3 barradas uma tinha um código de
produto que nunca apareceu em resultado nenhum (`B0D2L5Q4SS`: inventado). As outras
duas eram a forma curta (`/dp/CODIGO`) de URLs reais, então a recusa é falso positivo
ali. A mensagem manda chamar `solicitar_dado`, que não se aplica a um GET público, e
o modelo traduziu isso para "Eu libero o acesso", uma opção que não existia. Afrouxar
é decisão de segurança, não ajuste de leitura.

**A primeira mensagem da sessão.** O pedido era "no meu navegador local", o
classificador acertou, e a regra 14 manda chamar `browser_local_task` no mesmo turno.
Mas o cérebro leu o caderno da sessão anterior, viu o job que falhou e decidiu não
usar o navegador ("não repetir a mesma tentativa"), respondendo com o resultado
velho como se fosse novo. Pedido explícito perdeu para memória de fracasso.

---

### 2026-10-31 (4) — Pedido explícito perdia para a memória do fracasso

Os dois itens que a seção anterior deixou de fora. O método foi reenviar ao modelo o
request REAL da sessão `455646dd`, tirado do `RAW payload` do log, com e sem a mudança.
O log guarda só o nome das tools, então as que importam foram remontadas do código, e
as outras seguiram só pelo nome nos dois braços.

**Abrir o navegador pedido.** O classificador tinha acertado, mas a guarda de catálogo
do CLIENT_TOOL só conhece tool client-side, tratou `browser_local_task` como
"desconhecida" e zerou o `tool_name`. O sinal morria antes de chegar ao cérebro.

| 8 chamadas cada | abriu o navegador |
|---|---|
| como estava | 2 |
| instrução escrita na regra 14 | 3 |
| tirar `web_search`/`web_fetch` da mesa | 7 |
| **aviso logo depois da mensagem dele** | **8** |

A regra escrita não mediu efeito: está a 27 mil caracteres da decisão, e a skill
`compras`, ativa no turno, manda buscar na web. Por isso ela foi revertida em vez de
ficar engordando o prompt de todo turno. O que resolve é a POSIÇÃO: `navegador_do_usuario`
preserva o sinal e `_aviso_navegador_pedido` põe o aviso logo depois da mensagem. Ele
continua valendo com o resultado do caderno entrando depois dele (8 de 8). Só existe em
turno limpo: texto colado, mensagem de fora e agenda não recebem o empurrão.

**A forma curta no portão.** `url_is_short_form` aceita `/dp/CODIGO` quando isso é um
trecho CONTÍGUO do caminho de UMA URL vista, mesmo esquema e host, sem query, em base de
até 12 segmentos. A versão óbvia, subsequência, abriria um canal: um link com o
alfabeto em segmentos escreveria qualquer texto. O teto fecha a variante De Bruijn.
Vale só para o `web_fetch`: a procedência de imagem continua exata. Verificado por
mutação: enfraquecer cada condição derruba um teste, e o único mutante vivo (aceitar
query) é equivalente, porque um segmento com `?` nunca bate com um segmento visto.

**A recusa do portão.** Mandava chamar `solicitar_dado`, que não tem o que aprovar num
GET público. Reenviado o mesmo momento, com 8 chamadas: com a mensagem antiga, 6
encerraram o turno em texto e 2 ofereceram "liberar"; com a nova, as 8 foram atrás do
endereço certo por `web_search`, e nenhuma ofereceu nada.

Testes: `tests/test_navegador_pedido_explicito.py` (10) e
`tests/authority/test_forma_curta_de_url.py` (19, a maioria tentativas de abuso).

### 2026-10-31 — O clique chega no controle (job `8bc25dd1`)

"Coloque no carrinho" gastou 16 cliques com `ok:true` em "Adicionar ao carrinho" e
terminou com o carrinho vazio. A Amazon não bloqueou nada. O botão real é um `<input
class="a-button-input">` com `opacity:0.01`, esticado sobre um desenho e FILHO do
`span` que o inventário ofereceu: o `visivel` corta abaixo de 0.05, o passo do
procurador oferece o pai, e o clique no pai não desce. No mesmo job o `Excluir`, um
input visível, funcionou. Prova em Chromium real: `.click()` no input abre a gaveta,
no `span` não abre nada.

**A correção troca a pergunta**, de "qual elemento?" para "quem recebe este pixel?".

1. **Mira** (`NEO.mirar`, `NEO.preparar`). Hit test no alvo. `el` sem ser controle com
   um controle no pixel: vai para o controle. `el` já controle com outro controle no
   pixel (link no meio do label): procura outro ponto. Por baixo do painel do Neo,
   aba em segundo plano e botão direito não usam mouse real. O direito abriria o menu
   nativo do sistema por cima da página.
2. **Mouse real com sonda** (`clicar()`). `Input.dispatchMouseEvent`, como o
   `press_key` já fazia com tecla. A sonda escuta `pointerdown`/`mousedown`/`click`, e
   o evento fabricado só entra se ela provar que nada chegou: sem isso seria item em
   dobro. `window.__neo` sumido quer dizer que a página navegou, logo o clique chegou.
   `desvio` avisa se caiu fora do alvo.
3. **Varredura da DOM** (`NEO.candidatos`, ação `find`). Documento inteiro, shadow DOM
   e iframe de mesma origem, por NOME ACESSÍVEL: texto, `value`, `aria-label`,
   `aria-labelledby`, `title`, `alt`, `placeholder`. Texto sem controle em volta sobe
   ao dono, inclusive pelo caminho de volta do `aria-labelledby` e pela geometria. O
   `click_text` passou a usar a mesma lista, e `n` escolhe o candidato. Custo medido:
   5–36 ms. Iframe de outra origem não se alcança: `frames_fechados` diz quantos.
4. **Inventário honesto**. O transparente que recebe o pixel entra como ele mesmo
   (`input(button) "Adicionar ao carrinho"`), com o nome do desenho quando não tem
   nome próprio. `fill` e `select` seguem o rosto até o controle (`NEO.controles`): o
   `js_select` recusava todo `[seleciona select[...]]`.
5. **Socorro** (`_socorrer_clique_morto`). Clique que não muda a tela roda a varredura
   sozinho, uma vez por rótulo, e o resultado entra no histórico sem custo de LLM.

**Guardas que o clique vivo exigiu.** Botão morto também é botão que não compra.
A `trava` manda `_SENSITIVE` ao device, conferida no alvo REAL na hora do clique: antes
`click_text "agora"` chegava em "Comprar agora" sem passar por guarda nenhuma. "Fechar
pedido" entrou na lista. O portão do PARE (`ATUA`, `parado`) barra toda ação que muda
algo: em 8bc25dd1 ele foi apertado às 19:49:54 e um clique saiu às 19:50:11, porque o
sinal só era lido no snapshot. `SENSIVEL:` e `PARE:` são conferidos com `startswith`,
senão texto da página citado num erro acionaria a parada.

Medido: corpus `snapshot-lab/clique.py`, 15 falhas → 0 em 36 tentativas, contando
exatamente 1 por cena. Na Amazon real, sem login: `Quantidade: 2`, gaveta de garantia,
`Não, obrigado`, contador em 2, por `ref` e por nome. Rust real em Chromium real em
`o_clique_chega_no_controle_transparente_uma_vez_so`. **Não exercitado: Edge logado,
pela extensão.** O caminho do CDP é o mesmo `executeCdp` do `press_key`, e a reserva
cobre o mouse que não chegar.


### 2026-11-05 — O enunciado morava num `<header>` (jobs `2611e76d` e `7b73f755`)

Quiz 12axes no modo curto. Os dois jobs leram "01" e as cinco opções, sem a pergunta e sem
o "Question 1 of 36". O print mostrava as duas coisas. O `2611e76d` morreu em 11 s pelo
freio de giro. O `7b73f755` começou na aba que o anterior deixou, já na pergunta 1 de 36,
e reiniciou o quiz três vezes até o PARE.

**Causa.** `NEO.CHROME` pegava qualquer `header` e `footer`, e o `NEO.foraDaLeitura` tira da
leitura o que está dentro da moldura. O site monta `article.question-card > header > h2`
(o enunciado) e o progresso é outro `<header>`, dentro de `section.quiz-layout`. O sinal
estava no snapshot: "gender identity", um termo de DENTRO do enunciado, saía com
`[moldura do site]`. Pela regra do HTML, que é a do leitor de tela, `header`/`footer` só são
banner e rodapé do site fora de article, aside, main, nav e section (ou dos papéis
equivalentes). O mesmo defeito apagava título e autor de toda notícia em
`<article><header>`.

**Efeito somado.** Sem o contador do site, o modelo usou o único que tinha, o
`Passo N de no máximo M` do fim do prompt. Como o teto cresce a cada nota (50 → 57), nunca
deu 36: "Passo 8 de no máximo 53 — ainda estou no modo longo".

**Consertos.**
1. `NEO.CHROME` segue a regra ARIA: `header:not(:is(<bloco>) header)`, idem `footer`, com
   `NEO.BLOCO` = article, aside, main, nav, section e os papéis. `nav`, `banner` e
   `contentinfo` explícitos continuam moldura em qualquer lugar.
2. A linha do contador diz de quem ele é: "Seu orçamento nesta tarefa (contador SEU, não
   da página): ação N de no máximo M. O progresso do site (pergunta, etapa, X de Y) é o
   que a PÁGINA mostra."
3. `_pede_acompanhar_download`: as frases de espera ("até o fim", "aguarde", "garanta
   que") só ligam o acompanhamento quando o pedido fala de baixar alguma coisa. O
   `2611e76d` tinha ligado por "avance até o fim".

**Medido.** Fixtures `cartao-com-header.html` e `artigo-com-header.html` no
`snapshot-lab` (o código velho reproduz o job letra por letra; o novo passa, com o banner e
o rodapé do site ainda fora da leitura). Corpus inteiro do laboratório sem regressão
(leitura, eleição do principal, clique, inspetor) e `cargo test browser_local
--include-ignored`: 17/17. Em 12 sites reais, mesmo snapshot com o JS velho e o novo: o
12axes passou a ler a pergunta e o progresso, a Wikipédia e o GitHub ganharam o título
(`<header>` dentro de `<main>`), e nenhum menu vazou para a leitura. O portão do download
sobre os 164 pedidos `browser_local` do banco: o velho ligava em 32, e 16 deles não tinham
download nenhum (questionário, rastreio no AliExpress, Forms, rede social); os 16 que
continuam ligando são todos downloads do Drive. **Não exercitado: um job real no Edge.**

### 2026-11-05 (2) — A etapa nova lida como o quiz reiniciando (job `f9370b43`)

Com a leitura consertada, o agente respondeu as 36 perguntas do 12axes, uma por passo. A
36ª abriu uma etapa extra, "Identifying your archetype 1 of 6", com seis perguntas de A a F.
Três defeitos se somaram ali, e o agente clicou em "Back to start" com as 36 respostas
anotadas.

1. **O clique mentiu.** O site desmonta o cartão da última pergunta na hora, antes de o
   `aria-checked` virar `true`. O `fimDoClique` lia o nó que já tinha saído da página e
   o driver disse "NÃO ficou selecionado (o clique não pegou)". Agora, controle fora da
   página devolve `saiu: true` e o driver diz que a página reagiu.
2. **A decisão certa foi recusada.** O passo seguinte ia responder a etapa nova ("C Live
   and let live"). O `thought` citava `click({"ref": 5})` com aspas sem escape, e o
   parser parou no caractere 346. Na nova rodada o modelo leu "1 of 6" como reinício.
   `_consertar_aspas_do_thought` escapa as aspas SÓ do `thought` (que nunca vira ação);
   `action` e `args` saem do parser como vieram, e a resposta cortada continua sendo corte.
3. **Nada parou o recomeço.** `_check_desfaz_progresso`: com 5 anotações ou mais, clique
   em rótulo de recomeço (restart, back to start, recomeçar, reiniciar, limpar formulário…)
   é recusado UMA vez, com a primeira linha da tela e a explicação de que numeração que
   volta a 1 com outro título é etapa nova. Insistir passa, e o aviso rearma quando o
   caderno cresce mais 5.

A tarefa que o cérebro escreveu já trazia a receita: "se o contador não bater, clique em
Restart quiz ou Back to start". Ele a montou com as notas do `7b73f755`, que registravam o
contador do PRÓPRIO agente como se fosse o do site. O contador já foi renomeado; a guarda
cobre a receita que sobrar.

**Medido.** Cena `some` no `snapshot-lab/clique.py` (opção que desmonta o próprio cartão):
o `.rs` velho conta 1 clique e devolve `checked=False`; o novo devolve `saiu=True`, sem
falha no resto do corpus. Guarda reencenada nos 86 cliques dos 20 diários da VPS: 7
disparos, todos recomeços errados (6 no `7b73f755`, que reiniciava o quiz já no modo curto,
e o do `f9370b43`), nenhum alarme falso. `cargo test browser_local --include-ignored` 17/17,
neo-core 3799 testes. **Não exercitado: a etapa extra num job real.**

### 2026-11-05 (3) — O aviso de finalizar que nunca chegou (sessão `cb41834a`, Portal)

Questionário de 10 questões, site lento. O job `21c563c1` respondeu as dez. Depois do
Responder da 10ª o site voltou para a questão 1, habilitou o Finalizar e mostrou por cima da
página "Finalizar questionário · Você respondeu todas as questões, lembre-se de Finalizar o
questionário". O modelo não leu o aviso, concluiu que a 10 não tinha pegado e clicou nela por
oito passos, chegando a marcar a alternativa 1 (sem Responder, a salva continuou a 3), até o
PARE. O pedido seguinte ("monta um arquivo com as questões") morreu em 0,5 s com Broken pipe.

1. **A leitura estourava em todo passo.** Do passo 8 ao fim, `tela` = 5000 (o teto) em todos.
   A lista lateral mostra duas linhas de cada enunciado e esconde o resto com `overflow`,
   mas a caixa estar na janela bastava para o enunciado INTEIRO contar como tela: 11 mil
   caracteres. O que vinha depois no DOM caía fora: o enunciado da questão atual, as
   alternativas e os avisos, que só apareciam dentro das marcas `⟪COBERTO por "Bom
   trabalho! Questão 8 respondida."⟫`. Conserto: `NEO.ler(…, {recorte: true})`, só no
   snapshot, mede as linhas de cada nó (`Range.getClientRects`) contra a caixa que recorta, eixo
   por eixo. O escondido de caixa com rolagem vai para `resto`; o de `hidden`/line-clamp sai do
   snapshot. `extract` e busca leem tudo, como antes. Texto curto (< 40) parcialmente à vista é
   lido inteiro.
2. **Aviso sem lugar.** `avisos` no snapshot: região viva (`role=alert|status`, `aria-live`) ou
   camada `fixed` presa ao `<body>` com pouco texto e no máximo 3 controles (a barra do topo é
   feita de links e fica fora). Sai do texto corrido e entra como `>>> AVISO NA TELA`, antes da
   tela, marcado "APARECEU AGORA" quando o par elemento+texto não estava no snapshot anterior.
3. **A visão errou o estado e venceu o DOM.** O `look` disse "nenhuma alternativa marcada" e
   "Responder habilitado (azul clara)"; o DOM dizia marcada e `[desabilitado]`.
   `_com_estado_do_dom` acrescenta à resposta da visão o estado (marcado, desabilitado) dos
   números que ela cita, quando ela fala de estado.
4. **O navegador errado.** `headless = not local_browser_requested(mensagem atual)` mandou 3
   de 5 tarefas para o navegador sem janela, que não tem o login dele: "continuar sem
   recarregar" caiu no login, "monta um arquivo" tentou reaproveitar a aba do Edge. Agora
   também valem `_asked_local_recently` (que já existia e só servia ao portão do servidor) e
   `_trabalho_no_navegador_dele`: no mesmo site e na mesma conversa, a última tarefa foi no
   navegador dele há até 15 min, ou a do headless parou no login há até 12 h. `_resolver_aba`
   não reaproveita aba de um navegador num job do outro.
5. **O headless morto não subia de novo.** `ensure_running` olhava se havia um registro, não se
   o processo vivia. O Chrome sem janela morreu nas 3 h paradas e duas tarefas escreveram no
   socket morto. `vivo()` (`try_wait`) e, se a conexão cair ao abrir aba, reinicia e tenta uma vez.

**Medido.** Fixture `snapshot-lab/lista-recortada-e-aviso.html` (lista recortada, linha com
reticências, caixa com rolagem, aviso com e sem ARIA, aviso novo e velho): o `.rs` velho falha
em 10 dos 16 casos, o novo passa todos, e o corpus inteiro do laboratório segue verde. Em 12
sites reais, palavras da tela antiga que sumiram da nova: 0 na Wikipédia, MDN, HN, gov.br e The
Verge, nenhum preço em lugar nenhum; Amazon e KaBuM perderam o fim dos títulos que o site
esconde com line-clamp; o aviso de cookies da The Verge virou AVISO. Mediana do snapshot igual
(g1 +38 ms). Regra do navegador reencenada nos 5 jobs reais da sessão: os 3 que foram para o
headless teriam ido para o navegador dele. `cargo test` 17/17 nos dois módulos, com dois testes
novos de Chromium real (processo morto com `kill -9`, conexão caída com o processo vivo);
neo-core 3813. **Não exercitado: o questionário num job real; o código velho do headless não
rodou contra os testes novos.**

### 2026-11-05 (4) — A nota menor que o item (job `d73fa72a`, Portal)

"Extrair as questões e alternativas para o .txt". Com a leitura e o navegador já consertados
(o aviso apareceu como `>>> AVISO NA TELA`, o job foi para o navegador dele), o agente
percorreu as 10 questões, anotando cada uma no mesmo passo em que clicava na próxima, e depois
recomeçou a volta, e a terceira já começava quando veio o PARE.

**Causa.** `_NOTA_MAX = 1000`, cortado em silêncio. As dez notas de questão têm EXATAMENTE
1000 caracteres no diário: o enunciado tem ~1000 e as cinco alternativas ficavam de fora ("…
ALTERNATIVA 1: O consumo de m"). O modelo via o caderno truncado ("as notas atuais estão
truncadas"), voltava à questão para completar, a nota nova também batia no teto com o mesmo
começo, `_mesma_nota` a tomava por repetição, e nada crescia. A volta inteira se repetia.

**Conserto.** Teto de 4000, corte DITO na própria nota (`_NOTA_CORTADA`, "anote o resto numa
nota separada") e, no prompt, nota acima de 600 caracteres aparece compacta (`_nota_no_prompt`):
começo, fim (a última alternativa, que prova que o item terminou) e "guardada INTEIRA no
caderno: N caracteres — não precisa anotar de novo". O caderno do prompt de 10 questões fica
em ~6 mil caracteres, menos que os 10 mil truncados de antes.

**E o arquivo.** O agente do navegador não grava arquivo; quem grava é a continuação, que lia o
resultado em `[:6000]` e teria de REDIGITAR 15–20 mil caracteres no `neo_write_file`, mais do
que cabe numa resposta. `JobManager._itens_em_arquivo`: quando o pedido (`pedido`/`then`/`task`)
fala em arquivo, os itens íntegros da entrega (sem os rodapés) vão para
`/workspace/files/<titulo>-<job8>.txt`, e a continuação manda entregar ESTE com `file_save`.
Sem pedido de arquivo nada é gravado, para não deixar no sandbox cópia do que ele navega.

**Medido.** Notas do diário: 10 de 10 com 1000 caracteres. Testes com dez questões de ~1.400
caracteres: cabem inteiras, a segunda volta não cria nem encurta nota, o arquivo sai idêntico à
junção dos itens. neo-core 3820. **Não exercitado: o pedido num job real.**

### 2026-11-05 (5) — O quiz parado na 15ª por "não produzir" (sessão `b230a40d`)

"Usando meu navegador responda esse quiz de 36 perguntas" (12axes, versão curta). O job
`379df104` respondeu uma pergunta por passo e parou na 15ª com "Parei: 12 passos seguidos sem
acrescentar nada". O "ainda tá lá, dá pra continuar" do turno seguinte nem chegou a rodar.
Não foi contexto: a chamada que quebrou tinha 18 mil tokens de prompt.

1. **A régua só via nota NOVA.** O modelo guardava as respostas numa nota só ("Q1: Discordo.
   Q2: …") e a reescrevia maior a cada clique; `_mesma_nota` a tomava pela mesma nota
   (`atualizei a nota 2`) e `_contabilizar_producao` não contava. Progresso real virou giro. E
   o orçamento (25, que só cresce com produção) não cabia 36 perguntas de qualquer jeito.
   Conserto: nota atualizada também é produção, mas só com três provas juntas — ganhou
   `_NOTA_CRESCEU_MIN` (3) palavras, o passo foi uma interação que deu certo (`_INTERAGE`:
   clique, seleção, digitação, tecla; não `scroll`, `wait_for` nem `navigate`) e a tela
   (`snapshot["tela"]`) trouxe `_TELA_INEDITA_MIN` (4) palavras nunca vistas no job, sem contar
   número.
2. **O "continuar" foi para o servidor.** O classificador escolheu `browser_task`. O portão só
   promovia para o local quando a FRASE pedia o navegador dele; o job local de dois minutos
   antes, na mesma conversa, não contava. Agora `_inherits_local()` também promove (com
   device no canal); dali o `_browser_local_task` decide a janela e a aba como sempre. A
   promoção por MENSAGEM continua proibida (`test_historico_so_mantem_nunca_promove`).
3. **JSON quebrado virou a resposta.** O agente do SERVER_TOOL rodava no modelo do desktop
   (nemotron-3.5-lightning, servido pela Io Net): 5.319 tokens, `finish_reason` nulo e os
   argumentos do `browser_task` com uma string aberta. O `json.loads` do `get_tool_calls`
   levantou e o `except` genérico do `_inner_loop` entregou "Erro na execução: Unterminated
   string…". Agora a chamada quebrada não roda: o modelo é avisado e tenta de novo até
   `_MAX_TOOL_CALL_QUEBRADA` (2) vezes, com a tool ainda forçada até a primeira rodar; depois
   disso sai uma frase, não a exceção. O loop cognitivo já tratava isso
   (`_MAX_TRUNCATED_TOOL_ROUNDS`). O JSON não é consertado: argumento cortado executado seria
   tarefa pela metade.

**Medido.** `scripts/replay_sem_ganho.py` reencena a régua sobre os diários e acerta a parada
real do `379df104` (passo 18). Nos 20 diários da VPS, a régua nova só muda o desfecho dele.
Duas réguas mais simples foram rejeitadas porque soltavam o `b8dbde34`, um giro real de 12
`wait_for` em "Carregando Questionário…": "nota atualizada que cresceu" (o modelo
narrava "ainda carregando" na nota, +6 palavras) e essa mesma régua com "tela de hash inédito"
(a página mudava o hash a cada passo). Com as três provas, o `b8dbde34` para no mesmo passo e o
loop da questão 10 do `21c563c1` não ganha orçamento. Portão: em 60 dias, 0 dos 21 jobs de
servidor nasceram até 10 min depois de um job local da mesma conversa. Testes: quiz de 20
perguntas numa nota só vai ao `done` com orçamento de 12; "carregando" com relógio, a mesma
pergunta de novo e rolagem atualizando a nota param como antes; desligar cada prova derruba o
teste dela. neo-core 3831. **Não exercitado: o quiz e o "continuar" num job real; a
recuperação do JSON quebrado contra o modelo real.**

### 2026-11-05 (6) — O mesmo quiz, respondido sem anotar (sessão `f6f96081`)

Uma hora depois do conserto acima, o job `90b58ca3` retomou o quiz na pergunta 16 e parou de
novo no passo 18 com "12 passos sem acrescentar nada", na 31ª pergunta. Desta vez o modelo
(o mesmo deepseek-v4.1-flash; o cérebro era o nemotron-3.5-lightning, que só escreveu a tarefa)
fez duas notas no começo e depois respondeu as perguntas 20 a 31 sem anotar nada. A régua da
entrada anterior exige que uma nota cresça, então não viu avanço.

**O que separa os quizzes que terminaram.** Os dois jobs do 12axes que chegaram ao resultado
(`5bd1a1cc` e `d3aba1ca`, cérebro deepseek) anotaram cada resposta como nota NOVA: 46 e 49
notas. Os dois que pararam fizeram 2 notas novas cada um. Anotar ou não é hábito do modelo, e a
régua dependia dele.

**Conserto.** Sem nota, também conta como produção o clique que o device confirmou MARCADO
(`checked`, o "→ SELECIONADO") seguido de tela com `_TELA_INEDITA_MIN` palavras inéditas.
Clique que só leva a outra página ("SAIU da tela") e `select` (que o device não confere) não
entram.

**Medido.** No replay dos 20 diários da VPS (`scripts/replay_sem_ganho.py`, régua D), só os dois
quizzes mudam de desfecho; nos outros 18 a parada e o orçamento são os mesmos da régua velha.
Testes: o quiz de 20 perguntas sem anotar vai ao `done`; marcar de novo a mesma pergunta e
clicar em links com telas novas param como antes; desligar a regra derruba só o teste dela.
neo-core 3834. **Não exercitado: o quiz num job real.**
