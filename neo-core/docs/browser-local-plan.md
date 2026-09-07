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
