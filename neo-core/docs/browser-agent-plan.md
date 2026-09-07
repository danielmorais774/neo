# Plano: Browser Agent — o Neo ganha um browser próprio (browser-use como subagente)

**Data:** 2026-07-15
**Status:** F1 EM PRODUÇÃO (2026-07-15, ver §10) — F2 parcial (handoff note e
GET /sessions saíram na F1; **prints da tela em 2026-08-24, ver §3.7**);
pendentes progresso em `job_logs`, F3 (credenciais) e F4 (hardening)
**Depende de:** sistema de jobs (docs/workbench-background-jobs-plan.md, F1–F3 em prod) e ProactiveDispatcher.

A ideia: uma instância de Chromium rodando no backend (VPS), dirigida pela lib
**browser-use** (Python, ~79k stars, Playwright/CDP, self-hosted), orquestrada
pelo neo-core. O Neo (cognitivo) delega a tarefa em linguagem natural; o
browser-use age como **subagente** com seu próprio LLM e loop de
ver-página→decidir→agir. Requisitos centrais deste plano:

1. **Sessão de auth persistente** — logins/cookies sobrevivem entre tasks e
   entre restarts.
2. **Stage da aba entre interações** — se o usuário manda um segundo prompt
   logo em seguida, o agente continua da página onde parou (mesmas abas,
   mesmo estado).
3. **Credenciais passadas pelo Neo** — sem que NENHUM LLM (nem o do Neo, nem o
   do subagente) veja os valores em claro.

## 1. Estado atual (o que já existe e vamos reusar)

- **agent_job + JobManager + ProactiveDispatcher** (em prod): fila em Postgres,
  workers, aviso proativo no chat ao terminar (`kind="chat_message"`). Task de
  browser demora 30s–minutos ⇒ é exatamente esse trilho.
- **Padrão de runner injetado**: o `deep_research` mostra o molde — tool em
  `JOB_TOOLS` enfileira `agent_job(kind=...)`, e um runner construído em
  `api/main.py` (cf. `build_research_runner`) é injetado no `JobManager`, que
  despacha por `kind` em `_run_job`. O browser segue idêntico:
  `kind="browser"` + `build_browser_runner`.
- **Padrão de container auxiliar** (neo-workbench no docker-compose): serviço
  dedicado com mem_limit, cap_drop, rede própria. O browser ganha o dele —
  mas com política de rede diferente (precisa de internet ampla; ver §6).
- **Skills library**: gating por keyword + `tool_choice=required` (gotcha da
  skill financas) para a ativação confiável.
- **neo-panel**: molde de abas CRUD (Skills, Pesquisas) para a futura aba de
  Credenciais.

## 2. O gap (o que NÃO existe hoje)

- Nenhum browser no backend. O único "browser" no neo-core é o canal de mídia
  (abas do cliente via `media_context`) — atuação no **cliente**, não no
  servidor. `web_search`/`web_fetch` são HTTP puro (sem JS, sem clicar).
- Nenhum lugar seguro para credenciais de sites (e é proibido entrar no PKS —
  mesmo princípio do `ledger_guard`: segredo não é conhecimento semântico).
- Nenhum conceito de "sessão de trabalho" que atravessa turnos do chat.

## 3. Arquitetura proposta

```
┌────────────┐  tool browser_task   ┌──────────────┐  HTTP interno   ┌────────────────────┐
│ cognitivo  │ ───────────────────▶ │ JobManager   │ ──────────────▶ │ neo-browser-agent  │
│ (DeepSeek) │  (enfileira job)     │ kind=browser │  POST /tasks    │ (container)        │
└────────────┘                      │ runner_browser│                │ FastAPI wrapper    │
      ▲                             └──────┬───────┘                 │  SessionManager    │
      │ chat_message proativo              │                         │  browser-use Agent │
      └────────────────────────────────────┘                         │  + LLM próprio     │
                ProactiveDispatcher                                  │  Chromium headless │
                                                                     │  /data/profiles ◀──┼── volume
                                                                     └────────────────────┘
```

Papéis: o **neo-core orquestra** (decide quando delegar, resolve credenciais,
gerencia job e retorno proativo); o **browser-use é o subagente** (recebe a
task em linguagem natural + contexto, executa o loop com LLM próprio, devolve
resultado + screenshots). O cognitivo nunca micro-gerencia cliques.

### 3.1 Container `neo-browser-agent` — o corpo do subagente

- Imagem: Python 3.12 + `browser-use` + Chromium (o `playwright install
  chromium --with-deps` da própria lib). `mem_limit: 2g`, `cpus: 1.5`,
  `pids_limit: 512` (Chromium é guloso de processos), `init: true`.
- Wrapper FastAPI fino (`app.py`), porta interna 5100, **sem porta
  publicada** — só a rede docker interna alcança (neo-core → wrapper).
- Volume `neo-browser-profiles` montado em `/data/profiles` — os perfis do
  Chromium (cookies, localStorage, senhas salvas) persistem a restart e a
  rebuild.

Endpoints:

| Endpoint | Papel |
|---|---|
| `POST /sessions/{sid}/tasks` | Roda uma task do browser-use na sessão `sid`. Body: `task`, `context` (handoff note, §3.4), `sensitive_data` (§3.5), `allowed_domains?`, `max_steps` (default 40). Responde ao FIM da task (o "background" é papel do JobManager, não do wrapper). |
| `GET /sessions/{sid}` | Estado: `warm`/`cold`, abas abertas (url+título), última task + resumo, screenshot atual (base64). |
| `DELETE /sessions/{sid}` | Fecha o Chromium (RAM), mantém o profile. `?purge=true` apaga o profile (logout de tudo). |
| `GET /health` | Vivacidade + nº de sessões warm. |

### 3.2 SessionManager — auth persistente + stage da aba

Dois níveis de persistência, e é essa separação que resolve o requisito:

1. **Profile em disco (frio, para sempre)** — `Browser(user_data_dir=
   f"/data/profiles/{sid}")`. Cookies e logins sobrevivem a QUALQUER coisa
   (TTL, restart, deploy). Fazer login uma vez ⇒ logado para sempre (até o
   site expirar a sessão).
2. **Browser vivo (quente, entre prompts)** — `Browser(keep_alive=True)`: o
   Chromium NÃO fecha quando o `Agent` termina. A próxima task na mesma
   sessão recebe o **mesmo objeto Browser** ⇒ mesmas abas, mesma página,
   mesmo scroll. É o "stage da aba": o 2º prompt continua de onde o 1º parou.
   - **TTL de idle** (`NEO_BROWSER_SESSION_TTL`, default 900s): sem task
     nova, o SessionManager fecha o Chromium (libera ~400MB) — o estado
     quente morre, o frio (auth) fica. Task nova reabre do profile.
   - Na v2.x da lib, `Browser` **é** `BrowserSession` (alias) — usar `Browser`.

Identidade da sessão: default **`user-default`** — uma sessão durável única
por usuário (não por chat), porque o valor do auth persistente é atravessar
conversas, e a RAM da VPS comporta 1–2 Chromium. O tool aceita `session`
nomeada (ex.: `"trabalho"`) para isolar contextos; máx. `NEO_BROWSER_MAX_WARM`
(default 1) sessões quentes simultâneas, com eviction LRU (fecha a mais ociosa).

Concorrência: **uma task por vez por sessão** (lock + fila FIFO curta, máx. 2
em espera; acima disso o wrapper responde 429 e o runner falha o job com
mensagem clara). Duas tasks simultâneas na mesma aba seria corrupção de estado.

### 3.3 O subagente — browser-use Agent

Por task, o wrapper monta:

```python
agent = Agent(
    task=prompt_montado,          # task do usuário + handoff note (§3.4)
    llm=llm,                      # NEO_BROWSER_LLM via OpenRouter (visão!)
    browser=session.browser,      # o Browser(keep_alive=True) da sessão
    sensitive_data=sensitive,     # §3.5 — placeholders, escopo por domínio
    max_steps=req.max_steps,
    extend_system_message=POLICY, # §6 — nunca pagar/comprar/excluir sem ordem
)
result = await agent.run()
```

- **LLM próprio, com visão** (`NEO_BROWSER_LLM`, recomendação:
  `google/gemini-2.5-flash` via OpenRouter). NÃO usar o DeepSeek do cognitivo:
  (a) sem visão a taxa de sucesso despenca; (b) o gotcha conhecido do content
  vazio (reasoning come o max_tokens) num loop de 20–40 chamadas é fatal.
- Retorno ao runner: `final_result`, histórico resumido de ações, urls
  visitadas, screenshots (paths no volume) e o handoff note novo.

### 3.4 Continuidade entre prompts — handoff note

O browser vivo preserva o estado **da página**; o handoff note preserva o
estado **da tarefa**. Ao fim de cada task o wrapper grava na sessão um resumo
curto (última task, resultado em 1–2 frases, URL atual, aba ativa). A próxima
task recebe isso prefixado no prompt:

```
[Sessão em andamento] Última tarefa: "buscar voos FOR→GRU em site X" —
resultado: lista filtrada na tela, aba em site-x.com/resultados.
Nova tarefa: {task do usuário}
```

Assim "agora ordena por preço e pega o mais barato" funciona sem o usuário
repetir contexto. O cognitivo também recebe o handoff note no resultado do
job — ele sabe o que ficou na tela e responde melhor.

### 3.5 Credenciais — o Neo passa sem nunca ver

Regra de ouro: **nenhum LLM vê segredo em claro**. Três camadas:

1. **Armazenamento** — tabela `browser_credential` no Postgres do neo-core:
   `id, label, domain_pattern, fields_json (cifrado), created_at`.
   `fields_json` = `{placeholder: valor}` (ex.: `{"x_user": "...",
   "x_pass": "..."}`), cifrado com Fernet + `NEO_CRED_KEY` (env da VPS, fora
   do banco). **PKS proibido** (princípio do ledger_guard); o `ledger_guard`
   ganha um irmão: `credential_guard` bloqueia padrões de senha/token nos
   canais de escrita do PKS.
2. **Cadastro** — pela **aba Credenciais do neo-panel** (form → API → cifra),
   nunca pelo chat: valor digitado em conversa passaria pelo LLM do Neo e
   ficaria em transcript. Se o usuário tentar mandar senha no chat, o Neo
   orienta a usar o panel (e o `credential_guard` segura vazamento acidental).
3. **Uso** — o tool `browser_task` expõe só `use_credentials: ["label"]`. O
   executor (server-side) resolve labels → decifra → monta o `sensitive_data`
   do browser-use, **escopado por domínio** (formato nativo da lib, com
   wildcard):

   ```python
   sensitive_data = {"https://*.site-x.com": {"x_user": "...", "x_pass": "..."}}
   ```

   A lib garante que o LLM do subagente vê apenas `x_user`/`x_pass`; os
   valores são injetados direto no campo da página, e só em páginas do
   domínio autorizado. O cognitivo (Neo) só conhece os **labels** — para ele,
   listar credenciais disponíveis é um tool read-only (`browser_credentials_list`
   devolve label+domínio, nunca valores).

### 3.6 Lado neo-core — tool, runner, dispatch

- **`core/jobs/tools.py`** — nova tool em `JOB_TOOLS`:

  ```
  browser_task(task, session?, use_credentials?, allowed_domains?, title)
  ```

  Descrição no molde do deep_research ("demora minutos, você é avisado…"),
  módulo `jobs`, server-side. `use_credentials` documenta que só aceita labels.
- **`core/jobs/executor.py`** — `_browser_task()`: valida flag
  (`settings.browser_agent_enabled`), resolve labels de credenciais, cria
  `agent_job(kind="browser", spec_json={task, session, sensitive_ref,
  allowed_domains, ...})` e devolve `job_id` na hora. Segredos decifrados NÃO
  vão no `spec_json` (que é jsonb legível) — vai só a lista de labels; quem
  decifra é o runner na hora do POST.
- **`core/jobs/runner_browser.py`** (novo) — `build_browser_runner(db,
  http_base, cred_store)`: carrega o job, decifra credenciais, `POST
  /sessions/{sid}/tasks` (timeout do job, default 600s), escreve progresso no
  log do job (o wrapper manda linhas de ação → `job_logs` funciona), retorna
  `{status, result_summary, artifacts}`.
- **`core/jobs/manager.py`** — branch `elif job.kind == "browser" and
  self._browser_runner is not None` em `_run_job` (espelho do research).
- **`api/main.py`** — wiring: `build_browser_runner` quando
  `settings.browser_agent_enabled`, injeta no `JobManager`.
- **`shared/config.py`** — `NEO_BROWSER_AGENT_ENABLED` (default false),
  `NEO_BROWSER_URL`, `NEO_BROWSER_LLM`, `NEO_BROWSER_SESSION_TTL`,
  `NEO_BROWSER_MAX_WARM`, `NEO_CRED_KEY`.
- **Roteamento/ativação**: `browser_task` entra como tool inline do cognitivo
  (como deep_research). Atenção ao gotcha do roteador (memória
  neo-router-skill-bypass): frases como "entra no site X" não podem cair no
  pipeline HA/music — revisar o classifier com exemplos de browser.

### 3.7 Artefatos e observabilidade

**Prints da tela (implementado em 2026-08-24).** O texto do subagente é a única
coisa que o usuário via de uma task; quando dava erro, "não consegui" não
explicava nada. Agora o print vira a evidência — e chega sozinho no chat:

| Camada | Arquivo | Papel |
|---|---|---|
| captura | `docker/neo-browser-agent/app/shots.py` | print AO VIVO (CDP, JPEG q75) + os por passo que a lib guardou (PNG); orçamento de nº/tamanho |
| política | `session_manager.run_task(screenshots=…)` | `auto` (default: só em erro/timeout/`success=False`) · `always` · `never` |
| transporte | `POST /sessions/{sid}/tasks` → campo `screenshots` (base64) | o browser está em OUTRO container; o File Store é a única superfície comum |
| ingestão | `core/jobs/screenshots.py` → `extra_metadata["images"]` | vira arquivo `origin="job"`, retenção temp (TTL de 7 dias do store) |
| exibição | `ProactiveDispatcher._append_images` | `[[IMAGE:file:<id>\|legenda]]` colado DEPOIS do texto redigido |
| sob demanda | `POST /sessions/{sid}/screenshot` + tool `browser_screenshot` | "me mostra o que tá aí agora", sem rodar task nova — funciona até com task em andamento |

Três decisões que valem a pena lembrar:

1. **O print do erro é tirado ANTES do `_kill_browser`.** Nos dois caminhos de
   falha (timeout e crash) o Chromium é reciclado; depois disso não existe mais
   tela para fotografar. Por isso `_capture_failure` roda primeiro.
2. **Mais de um print, de propósito**: o ao vivo mostra ONDE parou, os do
   histórico mostram COMO chegou lá (`browser_max_screenshots`, default 3).
3. **O marcador entra por fora do LLM** que redige o aviso proativo. Dentro do
   `result_summary` ele seria reescrito/comido pelo "escreve no seu tom"; por
   fora, chega íntegro — e como fica salvo no `content` da mensagem, reabrir a
   sessão redesenha os prints.

Print é evidência EXTRA: base64 podre, File Store desligado ou quota estourada
NUNCA derrubam a task (`ingest_shots` engole tudo e loga). O oposto — perder o
resultado por causa de uma foto — seria trocar o certo pelo enfeite.

- GIF da task (a lib gera) continua fora: um MP4/GIF por task é caro no volume
  e o par "passos + tela final" já responde "o que aconteceu?".
- Log do agente (ação por passo) vai para o log do job ⇒ `job_logs` já serve
  de olho em tempo real. Evitar o gap de observabilidade da skill HA (verdade
  só no app.log): o resultado do job guarda o histórico de ações.
- Futuro (F4): modo headful + VNC para assistir o agente ao vivo.

## 4. Fluxos de exemplo

**A. Continuidade (o caso do plano):**
1. "Neo, entra no Mercado Livre e busca teclado mecânico até R$300."
   → `browser_task` → job → subagente navega, filtra → chat_message: "achei
   12; os 3 melhores são…" (browser fica quente, aba nos resultados).
2. (2 min depois) "ordena por avaliação e abre o primeiro"
   → mesma sessão `user-default`, browser quente, handoff note dá o contexto
   → agente só clica em ordenar + abre o item. Sem re-navegar, sem re-login.

**B. Auth persistente + credencial:**
1. Daniel cadastra login do site no neo-panel (label `mercadolivre`).
2. "Neo, vê se tem atualização nos meus pedidos" → `browser_task(task=...,
   use_credentials=["mercadolivre"])` → site pede login → agente preenche com
   placeholders (valores injetados fora do LLM) → cookies ficam no profile.
3. Semana seguinte, mesmo pedido: profile ainda logado, nem precisa da
   credencial.

## 5. Mudanças por repo/diretório

| Onde | O quê |
|---|---|
| `docker/neo-browser-agent/` (novo) | Dockerfile, `app.py` (FastAPI), `session_manager.py`, `policy.py` |
| `docker-compose.yml` | serviço `neo-browser-agent`, volume `neo-browser-profiles`, rede `browser-net` |
| `neo-core/core/jobs/` | `tools.py` (+browser_task), `executor.py` (+_browser_task), `runner_browser.py` (novo), `manager.py` (kind) |
| `neo-core/core/credentials/` (novo) | store Fernet + `credential_guard` (PKS) |
| `neo-core/db/models/` + migração | `browser_credential` |
| `neo-core/shared/config.py` | flags `NEO_BROWSER_*`, `NEO_CRED_KEY` |
| `neo-core/api/main.py` | wiring do runner |
| `neo-panel` | aba Credenciais (F3) |

## 6. Segurança

- **Rede**: container sem porta pública; ingress só do neo-core pela rede
  docker. Egress: internet ampla (é um browser) — mas ISOLADO da LAN/serviços
  internos: não entra nas redes do postgres/mongo/workbench.
- **Prompt injection** (página maliciosa instruindo o agente): (a)
  `sensitive_data` escopado por domínio — página fora do domínio nunca recebe
  o segredo; (b) `allowed_domains` opcional por task para fluxos sensíveis;
  (c) `extend_system_message` com política fixa: nunca confirmar pagamento,
  compra, exclusão ou envio de mensagem sem que a task peça explicitamente;
  ao detectar pedido da página divergente da task, parar e reportar.
- **Segredos**: Fernet com chave só em env; jamais em spec_json, logs, PKS ou
  transcript. `job_logs` do runner mascara os valores por precaução
  (substring match antes de gravar).
- **Falso sucesso** (lição da skill HA): o resultado inclui a URL final e o
  histórico de ações; o runner marca `verified` a partir do próprio histórico
  da lib (`is_done` + `success`), não da narrativa do LLM.
- **RAM**: mem_limit 2g + TTL + max_warm=1 → pior caso conhecido e limitado.

## 7. Decisões em aberto (com recomendação)

1. **Escopo da sessão default** — por usuário (`user-default`) vs por chat.
   *Recomendo por usuário*: auth interessa entre conversas; RAM limita.
   Sessões nomeadas cobrem isolamento quando precisar.
2. **Modelo do subagente** — *Recomendo `google/gemini-2.5-flash` via
   OpenRouter* (visão, barato, rápido). Medir custo/task na F1; se caro,
   testar modo DOM-only (`use_vision=False`) em tasks simples.
3. **Cadastro de credenciais** — panel vs chat. *Recomendo panel-only na F3*;
   chat no máximo orienta.
4. **Sessão ocupada** — fila FIFO (2) vs rejeitar. *Recomendo fila*: o caso
   real é o usuário emendar pedidos.

## 8. Fases

- **F1 — andar (sem segredos):** container + wrapper com sessão única
  `user-default` (keep_alive + user_data_dir) + tool `browser_task` +
  runner + dispatch proativo. Sem credenciais. **Critério de aceite:** fluxo
  A do §4 — dois prompts encadeados continuam na mesma aba; e restart do
  container preserva um login feito manualmente na sessão.
- **F2 — continuidade rica:** handoff note, `GET /sessions` (estado + abas +
  screenshot), TTL/eviction, fila por sessão, **prints da tela (§3.7, feito em
  2026-08-24)**, progresso em `job_logs` (pendente).
- **F3 — credenciais:** tabela cifrada + store + aba Credenciais no neo-panel
  + `use_credentials`→`sensitive_data` + `credential_guard` no PKS.
- **F4 — hardening/extras:** `allowed_domains` por skill, TOTP/2FA, múltiplas
  sessões nomeadas com LRU, headful+VNC, cache de fluxos repetidos (gravar
  como script Playwright determinístico e só cair no agente quando quebrar).

## 9. Deploy (runbook) — VPS Contabo

Mesmo rito do workbench (Mutagen já espelha o código; salvar = deploy do
neo-core via --reload, mas container novo e migração são passos explícitos):

```bash
# 1. build + up do container do browser (não-disruptivo)
docker compose build neo-browser-agent && docker compose up -d neo-browser-agent
# 2. migração (browser_credential) — aditiva (só na F3)
# 3. flag NEO_BROWSER_AGENT_ENABLED=true + NEO_CRED_KEY no env → restart neo-core
# 4. smoke: POST /sessions/user-default/tasks {"task": "abra example.com e leia o título"}
#    e depois um browser_task pelo chat com 2 prompts encadeados
```

Atenção ao gotcha conhecido do reload travado no deploy (memória
neo-music-feature) ao mexer no env do neo-core.

## 10. Implementação F1 (2026-07-15)

Implementada e VALIDADA em produção na VPS, conforme §3 com estas decisões
fechadas (as recomendações do §7): sessão default por usuário (`user-default`),
LLM `google/gemini-2.5-flash` via OpenRouter, credenciais panel-only (F3),
fila FIFO (2) por sessão.

### O que foi construído

- `docker/neo-browser-agent/`: Dockerfile (python:3.12-slim + Chromium do apt +
  browser-use **0.13.4** pinado), `app/app.py` (wrapper FastAPI :5100),
  `app/session_manager.py` (2 níveis de persistência, TTL 900s, max_warm=1,
  fila 2, eviction LRU), `app/policy.py` (extend_system_message anti-injection
  + pt-BR). Código do wrapper é bind-mount: editar + `docker restart
  neo-browser-agent`, sem rebuild.
- compose: serviço `neo-browser-agent` (mem 1.5g, shm 1g, pids 512, sem porta
  pública), volume `neo-browser-profiles`, rede `browser-net` (bridge normal,
  só neo-core + browser — o browser NÃO alcança postgres/mongo/workbench);
  neo-core ganhou `networks: [default, browser-net]`.
- neo-core: flags `browser_*` no config; tool `browser_task` em JOB_TOOLS
  (params task/title/context/session/max_steps); `_browser_task` no
  JobToolExecutor (kind="browser", sessão saneada, sem segredos no spec_json);
  `runner_browser.py` (`build_browser_runner`; verdade do resultado vem de
  `success`=is_successful() da lib, não da narrativa); branch no `_run_job`;
  wiring no main.py; filtro no palette do cognitivo quando a flag está off
  (espelho do deep_research). `kind` é String(32) livre — **sem migração**.
- env: `NEO_BROWSER_OPENROUTER_KEY` no `.env` da RAIZ (interpolação do compose;
  o browser NÃO recebe o env_file inteiro do neo-core) e
  `BROWSER_AGENT_ENABLED=true` no `neo-core/.env`.

### Validação (2026-07-15, em prod)

1. Wrapper direto: task simples (example.com) → ok, 2 passos, 14s.
2. **Continuidade quente**: 2ª task "clique no link da página onde parou" →
   foi direto de `click` (sem re-navegar), 2 passos, 9s.
3. Restart do container → warm morre, `profile_exists=true` (frio persiste).
4. **E2E pelo trilho de jobs** (JobToolExecutor→fila→JobManager vivo→runner→
   wrapper): 2 jobs encadeados — HN top 3 e depois "clica na primeira notícia"
   → o job 2 continuou da aba do job 1 e resumiu o artigo. `completed` nos dois.

### Gotchas aprendidos

- browser-use 0.13 usa `cdp-use` (SEM Playwright): Chromium vem do apt e entra
  por `BrowserProfile(executable_path=...)`; `--no-sandbox` + `shm_size: 1g`.
- O pacote não tem `__version__` (usar `importlib.metadata.version`); exports
  são lazy via `__getattr__`.
- `AgentHistoryList` muda entre versões → wrapper lê tudo por `_history_get`
  defensivo (`final_result`/`is_successful`/`urls`/`action_names`/`errors`).
- Subagente respondia em INGLÊS com Gemini → regra 6 na POLICY (pt-BR sempre).
- Smoke via SSH: `docker exec` precisa de `-i` para receber heredoc.
- `POST /desktop/chat` (rota `/api/v1`) exige `user_id` — com None quebra na
  criação da conversation_session (o e2e usou o trilho de jobs direto, que
  aceita user/session nulos).

### Limitações conhecidas (= backlog F2/F4)

- Sem progresso em `job_logs` durante a task (F2-resto) — hoje só o
  `progress_note` genérico. Prints já saíram (§3.7); GIF segue fora.
- **Marcador cru fora do desktop**: a tool `browser_screenshot` só entrega o
  `[[IMAGE:file:…]]` a cliente que renderiza (`can_show_images`), mas o AVISO
  PROATIVO de um job não sabe de qual cliente a task nasceu — num canal sem
  render (WhatsApp, neo-panel) o marcador apareceria como texto. Mesmo gap já
  conhecido das imagens inline; fechar quando incomodar (guardar o `client` no
  `spec_json` da task e checar no dispatcher).
- Print de página logada é dado sensível: fica no File Store com retenção
  `temp` (TTL de 7 dias, GC do store) e sai pela rota autenticada de download.
- `GET /sessions/{sid}` perde last_task/current_url no restart do container
  (estado warm é só RAM; o que persiste é o perfil).
- Roteador ainda não foi auditado para frases de browser (gotcha
  neo-router-skill-bypass) — se "entra no site X" cair no pipeline errado,
  revisar o classifier com exemplos de browser.
