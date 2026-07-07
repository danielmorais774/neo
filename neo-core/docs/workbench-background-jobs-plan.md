# Plano: Workbench + Jobs em background — o Neo ganha uma bash própria

> **Status:** análise/desenho para revisão (nada implementado).
> **Motivação:** quando não existe tool específica, o Neo hoje só tem duas saídas:
> responder por texto ou usar o desktop do usuário (`run_command`, client-side,
> síncrono, bloqueia o turno). Queremos o que o Claude Code tem: um **ambiente de
> execução exclusivo do Neo** (bash/filesystem server-side) onde ele cria scripts
> e projetos, executa **em segundo plano sem bloquear o chat**, e quando termina
> **recebe o retorno, trata e manda a resposta pro chat** proativamente. Junto,
> primitivas de primeira classe: timer/cron, notificação, subagentes.

---

## 1. Estado atual (o que já existe e vamos reusar)

| Peça | Onde | O que dá pra reusar |
|------|------|---------------------|
| Registry/Executor de tools (server-side e client-side por módulo) | `core/tools/registry.py`, `core/tools/executor.py` | Registrar as tools do workbench como um novo módulo server-side (`register_module_executor("workbench", ...)`) |
| AgentExecutor (double-loop, loop detection, compactação, timeout) | `core/agent/agent_executor.py` | É o **subagente** pronto: roda LLM→tools→LLM com `tool_subset` + `tool_executor` custom (mesmo seam usado pelas skills) |
| Pipeline + handoff (`[[HANDOFF:skill]]`) | `core/pipeline/orchestrator.py`, `modules/desktop/ai_service.py` | O cérebro cognitivo já sabe delegar; basta um alvo novo `workbench` |
| NotificationBroker (pub/sub em memória) + `user_notification` (DB) + replay de pendentes no connect + Ack | `core/notifications/broker.py`, `grpc_server.py:490` (SubscribeNotifications), `db/models/user_notification.py` | É o **canal proativo servidor→cliente** que já existe, com persistência offline. Vira o transporte da resposta de job concluído |
| Missions do WhatsApp (lifecycle async persistido: status, `timeout_at`, `result_summary`, prompt de aprovação) | `db/models/mission.py`, `RespondToMissionPrompt` | Precedente do padrão de "trabalho de longa duração persistido no DB". O `agent_job` generaliza isso |
| Loop de background com tick | `grpc_server.py:708` (`_offline_checker`) | Precedente do padrão scheduler-por-polling |
| `_spawn_bg` (fire-and-forget rastreado) | `modules/desktop/ai_service.py:455` | Padrão de task assíncrona pós-resposta |
| Docker socket montado no container do neo-core | `docker-compose.yml` (`/var/run/docker.sock`) | **Já dá pra criar containers irmãos** → é a base do sandbox, zero mudança de infra |
| UX de 2 mensagens (ack imediato `is_final` + segunda mensagem depois) | feature de música (handoff client-actuating) | Mesmo padrão de UX para "tô fazendo em background, te aviso" |
| Canal WhatsApp de notificação | `POST /notify` (nginx :8091), `whatsapp_notify_user` | Fallback de entrega quando nenhum device está online |

## 2. O gap (o que NÃO existe hoje)

1. **Sandbox server-side do Neo** — `run_command`/`write_file` são `execution="client-side"`
   (`modules/desktop/tools.py`): rodam **na máquina do usuário**, com aprovação,
   timeout 30s, e seguram o turno do chat. Não existe bash/workspace do próprio Neo.
2. **Sistema de jobs** — nada persiste "trabalho em andamento" genérico. `_spawn_bg`
   é fire-and-forget (morre no restart, sem status, sem logs consultáveis). Mission
   é específico de WhatsApp.
3. **Scheduler/cron** — zero timers. "Me avisa em 5 minutos" não tem para onde ir:
   o classificador escolhe entre responder texto ou rodar comando no desktop.
4. **Retorno proativo pro CHAT** — `ExecuteTask` (gRPC bidi) só emite `CoreMessage`
   **dentro** do processamento de uma mensagem que chegou (`grpc_server.py:196-206`).
   Job que termina 5 min depois não tem como virar mensagem de chat; só existe o
   canal de notificação (notch), que hoje não injeta mensagem na timeline.
5. **Roteamento para o fallback** — o prompt do `IntentRouter` não tem o conceito
   "não existe tool → delega pro ambiente de execução do Neo".
6. **Tratamento do resultado** — não existe o passo "job terminou → LLM lê o
   resultado/logs → redige resposta com persona → entrega".

## 3. Arquitetura proposta

```
                                   ┌────────────────────────────────────────┐
 user msg → IntentRouter ──────────► COGNITIVE  ── [[HANDOFF:workbench]] ──┐│
            (ou intent AGENT_TASK)  └───────────────────────────────────────┘│
                                                                             ▼
   ┌───────────────────────────── JobManager (asyncio, in-process) ──────────────┐
   │  agent_job (Postgres): kind = sandbox_task | subagent | timer | cron        │
   │  status: queued → running → completed/failed/timeout/cancelled              │
   │  worker pool (Semaphore N) · heartbeat · reconcile no boot · logs em arquivo│
   └──────┬──────────────────────────────┬───────────────────────────┬──────────┘
          │ kind=subagent                │ kind=sandbox_task         │ kind=timer/cron
          ▼                              ▼                           ▼
   AgentExecutor.execute(              WorkbenchService          tick loop (DB poll
     tool_subset=WORKBENCH_TOOLS,        docker exec no            next_run_at, 5s)
     tool_executor=WorkbenchExec)        container neo-workbench       │
          │ escreve scripts,             (1 por user, volume          fire → evento
          │ roda, lê output              persistente /workspace)       │
          └──────────────┬───────────────┘                             │
                         ▼                                             ▼
   ┌──────────────────── ProactiveDispatcher (job terminou/disparou) ───────────┐
   │ 1. LLM curto: spec do job + result/logs tail → mensagem com persona        │
   │ 2. persiste assistant message em conversation_message (sessão de origem)   │
   │ 3. entrega conforme notify_policy:                                         │
   │    chat  → NotificationBroker kind="chat_message" (payload: session, texto)│
   │    notch → kind="job_update" (título/corpo curto)  [já existe]             │
   │    fallback offline → replay do DB no reconnect + POST /notify (WhatsApp)  │
   └────────────────────────────────────────────────────────────────────────────┘
                         ▼
        neo-desktop-tauri: handler p/ kind="chat_message" → appenda na timeline
        da sessão + TTS se Modo Live (voz vem do _stamp_voice equivalente)
```

### 3.1 Workbench — o sandbox (a "bash do Neo")

- **Imagem** `neo-workbench`: `python:3.12-slim` + node LTS + bash, git, curl, jq,
  ffmpeg(?), uv/pip, npm. Usuário não-root. Sem docker.sock. Nada de secrets do core.
- **Topologia (recomendada): 1 container persistente por usuário**, criado
  on-demand via docker API (socket já montado), `restart: unless-stopped`:
  - volume nomeado `neo-workspace-<user_id>` montado em `/workspace` → projetos e
    scripts do Neo **persistem** (ele acumula ferramentas próprias com o tempo).
  - limites: `--memory 1g --cpus 1 --pids-limit 256`, quota de disco no volume.
  - rede: egress liberado para internet, **bloquear RFC1918/LAN** (iptables na
    network do compose ou network dedicada) — decisão em aberto §7.
  - Alternativa descartada p/ MVP: container por job (isolamento maior, mas perde
    estado entre jobs e paga cold-start toda vez; dá pra evoluir depois).
- **Execução**: `docker exec` assíncrono (aiodocker ou docker-py em thread) com
  timeout por comando; jobs em background = processo dentro do container com
  stdout/stderr redirecionado para `/workspace/.jobs/<job_id>.log` (o JobManager
  acompanha e sincroniza status).
- **Tools novas (módulo `workbench`, server-side)** — nomes deixando explícito que
  é a máquina DO NEO, para o router nunca confundir com o desktop do usuário:
  - `neo_bash(command, cwd?, timeout?)` — síncrono curto (cap ~60s).
  - `neo_bash_background(command, cwd?, title)` — cria `agent_job` kind=sandbox_task
    e retorna `job_id` na hora (não bloqueia).
  - `neo_write_file(path, content)` / `neo_read_file(path)` / `neo_list_files(path)`
    — escopados a `/workspace` (validação de path).
  - `job_status(job_id)` / `job_list()` / `job_logs(job_id, tail?)` / `job_cancel(job_id)`.

### 3.2 Sistema de jobs — `agent_job` + JobManager

Nova tabela `agent_job` (generaliza o padrão `mission`):

```
id, user_id, session_id, kind (sandbox_task|subagent|timer|cron),
title, spec_json (goal/command/cron_expr/message), status
(queued|running|completed|failed|timeout|cancelled), progress_note,
result_summary, log_path, notify_policy (chat|notch|both|silent),
schedule_expr, next_run_at, timeout_at, heartbeat_at,
created_at, started_at, finished_at
```

`core/jobs/manager.py` (in-process, mesmo processo do gRPC/API):
- worker pool com `asyncio.Semaphore` (ex.: 3 jobs simultâneos) consumindo a fila
  do DB (status=queued, `next_run_at <= now`), tick de ~5s — mesmo padrão do
  `_offline_checker`.
- **Restart-safe**: no boot, jobs `running` com heartbeat velho → `failed`
  ("interrompido por restart") e o ProactiveDispatcher avisa; timers/cron são
  recalculados do DB (nada em memória é fonte de verdade).
- Enforcement de `timeout_at` (default por kind; sandbox 30min, subagent =
  `agent_global_timeout`).
- Logs: arquivo por job (volume `neo-core-logs` já existe) + eventos no Mongo
  (observability) reutilizando `session_file_logger`.

### 3.3 Subagente — AgentExecutor com toolset do workbench

`kind=subagent` = a versão "cria um projeto e resolve": roda
`AgentExecutor.execute(task=goal, tool_subset=WORKBENCH_TOOLS + read tools,
tool_executor=WorkbenchExecutor(user_id), system_prompt_override=WORKBENCH_PROMPT)`
**dentro de um job**. É exatamente o seam que as skills já usam
(`agent_executor.py:73-96`) — zero mudança no AgentExecutor. O prompt do subagente
instrui: explorar `/workspace`, reaproveitar scripts que ele já criou antes,
escrever → executar → validar → resumir. Steps viram log do job.

### 3.4 Scheduler — timer e cron como tools de primeira classe

"Me avisa em 5 minutos" **não** deve virar script com sleep — esse é o fallback
genérico; o caminho profissional é primitiva nativa:

- `schedule_timer(delay_seconds | at_iso, message, notify_policy?)` → `agent_job`
  kind=timer, `next_run_at = now + delay`. Ao disparar: ProactiveDispatcher entrega
  a mensagem (sem LLM se for aviso literal; com LLM se tiver `goal`).
- `schedule_recurring(cron_expr, goal)` → kind=cron (croniter para calcular o
  próximo `next_run_at` após cada disparo).
- `schedule_list()` / `schedule_cancel(job_id)`.
- Implementação: **o próprio tick do JobManager** (poll no DB por `next_run_at`),
  não APScheduler — menos dependência, restart-safe de graça, e a instância é
  única (sem necessidade de lock distribuído).
- O sleep-script no sandbox continua existindo como plano B natural (o modelo pode
  usar se quiser encadear lógica), mas o router/persona apontam para a primitiva.

### 3.5 Retorno pro chat — ProactiveDispatcher + kind="chat_message"

O coração do "termina → trata → responde no chat":

1. Job muda para estado terminal (ou timer dispara) → JobManager chama
   `ProactiveDispatcher.dispatch(job)`.
2. **Tratamento**: uma chamada LLM curta (provider default) com persona + spec do
   job + `result_summary`/tail dos logs → redige a mensagem pro usuário ("terminei
   a conversão, o arquivo tá em /workspace/out.csv, 12.4k linhas"). Timer literal
   pula o LLM.
3. **Persistência**: grava como assistant message na `conversation_message` da
   sessão de origem → mesmo offline, a resposta está na história quando reabrir.
4. **Entrega**: publica no `NotificationBroker` com um **kind novo
   `chat_message`** (payload: `session_id`, `text`, `job_id`, voz) — reusa 100% do
   transporte existente (`SubscribeNotifications` + replay offline do DB + Ack).
   Nada de RPC novo nem mudança de proto (payload_json já é livre).
5. **Cliente** (neo-desktop-tauri): handler para `kind=chat_message` → appenda a
   mensagem na timeline da sessão correspondente + notch curto + TTS se Modo Live.
   É a única mudança fora do neo-core (além da imagem docker).
6. **Fallback**: nenhum subscriber ativo → já fica pendente no DB (replay no
   reconnect); opcionalmente espelha no WhatsApp via `POST /notify` se
   `notify_policy` pedir.

### 3.6 Roteamento — "não tenho tool → workbench"

- **Cérebro cognitivo**: novo alvo de handoff `[[HANDOFF:workbench]]` (mesmo
  parser de sentinela de hoje) — é ele que percebe "não tenho tool pra isso, mas
  consigo resolver com código".
- **IntentRouter**: regra nova no prompt: pedido de cronometragem/lembrete →
  SERVER_TOOL `schedule_timer`; computação/processamento/geração que nenhuma tool
  cobre → COGNITIVE (que decide handoff), nunca inventar CLIENT_TOOL. Atenção à
  distinção que já existe hoje: "roda NA MINHA máquina" continua CLIENT_TOOL.
- **UX de 2 mensagens** (padrão da feature de música): turno 1 responde na hora
  "tô rodando em background (job #n), te aviso" com `is_final=true` → chat
  desbloqueado; turno 2 é o `chat_message` proativo. Cuidado com o gotcha do
  `is_final` já documentado no handoff client-actuating.

## 4. Fluxos de exemplo

**"Me avisa quando passar 5 minutos"**
router → SERVER_TOOL `schedule_timer(delay_seconds=300, message="⏰ passaram os 5
minutos")` → resposta imediata "marcado, te aviso às 14:32" → tick do JobManager
dispara → Dispatcher persiste + publica `chat_message` (+ notch) → desktop appenda
no chat e fala (Live).

**"Baixa esse CSV do link e me diz quantas linhas têm valor > 100"**
router → COGNITIVE → sem tool específica → `[[HANDOFF:workbench]]` → ack imediato
→ job kind=subagent → AgentExecutor no sandbox: `neo_write_file(analyze.py)`,
`neo_bash("python analyze.py")`, lê stdout → `result_summary` → Dispatcher redige
e entrega no chat.

**"Todo dia às 8h me manda o clima"**
`schedule_recurring("0 8 * * *", goal="buscar clima e avisar")` → cada disparo
cria um mini-run cognitivo (com a tool `get_weather`) → `chat_message` diário.

## 5. Mudanças por repo/arquivo

| Onde | O quê |
|------|-------|
| `docker/neo-workbench/Dockerfile` (novo) + compose | imagem do sandbox; network policy |
| `core/workbench/` (novo) | `WorkbenchService` (lifecycle do container, docker API), `WorkbenchExecutor` (module executor), `tools.py` (schemas `neo_*`) |
| `core/jobs/` (novo) | `manager.py` (pool + tick + reconcile), `dispatcher.py` (ProactiveDispatcher), `tools.py` (`job_*`, `schedule_*`) |
| `db/models/agent_job.py` (novo) + alembic | tabela `agent_job` |
| `api/main.py` lifespan | subir JobManager/tick; registrar módulo `workbench` e tools no registry |
| `modules/desktop/ai_service.py` | alvo de handoff `workbench`; ack de 2 mensagens; nudge no prompt do cognitivo |
| `core/router/intent_router.py` | regras de roteamento (timer → `schedule_timer`; gap → COGNITIVE/handoff) |
| `core/agent/persona.md` / context_selector | regra: "sem tool? você tem seu próprio ambiente; delegue e avise que roda em background" |
| `observability/` | logger de jobs (reusar session_file_logger) + rota `api/routes/jobs.py` p/ painel |
| **neo-desktop-tauri** | handler `kind=chat_message` → appenda na timeline + TTS Live; card de job (status/logs) opcional |

## 6. Segurança

- Sandbox: não-root, sem docker.sock, sem env/secrets do core, `/workspace` como
  única escrita, limites de CPU/mem/pids, quota de disco, timeout por comando e
  por job, egress sem LAN (proteger Proxmox/HA/portas internas da VPS).
- Auditoria: todo `neo_bash*` gravado (comando, exit code, duração) no log do job
  + Mongo. `job_logs` expõe pro próprio Neo e pro painel.
- Quotas: máx N jobs simultâneos por usuário, máx M jobs/dia, tamanho máx de log.
- Ações para FORA do sandbox (mandar WhatsApp, mexer na casa) continuam passando
  pelo gating existente das skills — o subagente do workbench só recebe toolset
  `neo_*` + leitura; nunca `ha_call_service`/`whatsapp_send_message`.

## 7. Decisões em aberto (com recomendação)

1. **Topologia do sandbox** — recomendo container persistente por usuário +
   `docker exec`; por-job fica como evolução.
2. **Egress do sandbox** — recomendo internet liberada, LAN bloqueada (senão o
   subagente não baixa nada e perde 80% da utilidade).
3. **Canal de entrega** — recomendo reusar `SubscribeNotifications` com
   `kind=chat_message` (replay offline grátis, zero proto novo). Alternativa: RPC
   dedicado `SubscribeSessionEvents` se quisermos streaming de progresso no futuro.
4. **Scheduler** — recomendo tick no DB (sem APScheduler).
5. **Progresso intermediário no chat** (job longo mandando updates) — fora do MVP;
   o card de job com `job_status`/`job_logs` cobre.

## 8. Fases

1. **F1 — Workbench síncrono**: imagem + WorkbenchService + tools `neo_*` (fore-
   ground, cap 60s) registradas como server-side; handoff `workbench` no cognitivo.
   *Valor: Neo já escreve e executa código próprio, mesmo que ainda segurando o turno.*
2. **F2 — Jobs**: `agent_job` + JobManager + `neo_bash_background` + `job_*` tools
   + ack de 2 mensagens. *Chat deixa de bloquear.*
3. **F3 — Retorno proativo**: ProactiveDispatcher + `kind=chat_message` + persist
   em `conversation_message` + handler no neo-desktop-tauri (+ TTS Live).
   *Fecha o ciclo terminou→tratou→respondeu.*
4. **F4 — Scheduler**: kinds timer/cron + tools `schedule_*` + regras no router.
   *O caso "me avisa em 5 min" vira nativo.*
5. **F5 — Subagente pleno**: kind=subagent com AgentExecutor + prompt do workbench
   + reaproveitamento de scripts do /workspace.
6. **F6 — Hardening**: reconcile pós-restart, quotas, painel de jobs (neo-panel),
   fallback WhatsApp, network policy final.

F1–F3 entregam o fluxo do exemplo do usuário fim-a-fim; F4 profissionaliza a
cronometragem; F5 é o "cria projetos como o Claude"; F6 endurece.
