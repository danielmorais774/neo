# Plano: Agenda do Neo — cron de tarefas, prompts e tools disparados sozinhos

> **Status: F1–F4 IMPLEMENTADOS** (2026-09-12). Ver §14 (o que ficou de pé).
> Migration `y8z9a0b1c2d3` aplicada em prod; a feature nasce atrás de
> `NEO_SCHEDULER_ENABLED` (default OFF).
> **Status original:** desenho para revisão, nada implementado.
> **Motivação:** hoje o Neo só age quando alguém fala com ele. Falta o eixo do
> **tempo**: "todo dia 7h me faz o resumo", "dia 5 me lembra da fatura", "a cada
> 15 min confere se o TTS do PC gamer caiu", "amanhã 15h me pergunta se resolvi
> aquilo". O que queremos é uma **agenda** que, na hora marcada, injeta um
> input no próprio neo-core como se o usuário tivesse falado — ou, quando não
> precisa de cérebro, dispara **uma tool direta**, **um alerta** ou **um email**
> sem pagar um turno de LLM.

---

## 1. Estado atual (o que já existe e vamos reusar)

| Peça | Onde | O que dá pra reusar |
|------|------|---------------------|
| `agent_job` com `schedule_expr`, `next_run_at`, `timeout_at`, `notify_policy`, `notified_at` | `db/models/agent_job.py` | As colunas de agendamento **já existem** — foram previstas no plano de jobs (kinds `timer`/`cron`) e nunca usadas |
| Claim atômico por horário | `db/async_client.py:claim_due_agent_jobs` | Já reivindica só jobs com `next_run_at IS NULL OR next_run_at <= now()` — **é o coração de um scheduler e já está pronto e à prova de dupla execução** |
| JobManager: tick loop, semáforo, reconcile no boot, reentrega de terminais não notificados | `core/jobs/manager.py` | O executor de disparos. Não precisamos de um segundo loop: a agenda **materializa** jobs e o JobManager roda |
| ProactiveDispatcher: redige com persona, persiste na sessão, publica `chat_message`, anota no caderno | `core/jobs/dispatcher.py` | Entrega pronta, com replay offline (`user_notification`) e fan-out por device WS |
| Handler de `chat_message` no desktop | `neo-desktop-tauri/src/pages/ChatPage.tsx:248` | Mensagem proativa já aparece na timeline |
| Turno cognitivo completo | `modules/desktop/ai_service.py:2904` (`process_user_input`) | É um **async generator** — dá pra consumir headless até `is_final`. É por aqui que o prompt agendado entra |
| Executor por-request (devices + jobs + pks_search + voice/config com `user_id`) | `ai_service.py:_handle_server_tool` | O stack que faz uma tool server-side funcionar **com usuário**. Precisa virar helper compartilhado |
| Inscrições de evento: dedup, TTL, `max_fires`, disjuntor de 3 erros, lock por inscrição | `core/events/router.py`, `core/events/registry.py` | O **irmão reativo** da agenda. Todas as guardas já foram pensadas e medidas lá — copiamos a semântica, não reinventamos |
| Skills com pipeline declarado | `core/skills`, `modules/skills_library` | Ação `skill` sai de graça |
| Email (IMAP/SMTP com cofre tipado) | `core/mailbox/tools.py` (`email_send`) | Ação `email` é só uma tool |
| Sandbox / research / browser_local como kinds de job | `core/jobs/*` | Agendar um `deep_research` semanal é só criar o job na hora certa |
| Fuso BR já em uso | `modules/finance/money.py` (`America/Fortaleza`), `core/agent/affect.py` (`sleep_tz`) | Fuso do usuário e "hora de dormir" já existem — a agenda respeita os dois |

## 2. O gap

1. **Recorrência.** `agent_job` é um *run*: `queued → running → terminal`, com
   `notified_at` para não duplicar. Não existe nada que **reagende** depois do fim.
2. **Nenhum kind executa prompt.** `_run_job` conhece `sandbox_task`, `research`,
   `browser`, `browser_local`, `download_watch`. Não há caminho de "roda um turno".
3. **Nenhum kind executa uma tool avulsa.** Tool hoje só roda dentro de um turno.
4. **Sem CRUD, sem tela, sem tools do cérebro.** Não dá para criar, listar, pausar,
   testar ou ver o histórico de uma tarefa agendada.
5. **Sem semântica de tempo:** cron/intervalo/one-shot, fuso, janelas, feriados,
   o que fazer com disparo perdido (e o neo-core **reinicia a cada save**), o que
   fazer se o disparo anterior ainda está rodando.
6. **Sem condição.** Todo alerta periódico sem condição vira ruído em uma semana.

---

## 3. As cinco decisões duras

### 3.1 Definição ≠ execução — tabela nova `scheduled_task`, run continua sendo `agent_job`

Tentar guardar a recorrência na própria linha do `agent_job` quebra a máquina de
estados (um job terminal é terminal; `notified_at` existe para entregar **uma**
vez) e apaga o histórico a cada disparo. Então:

- `scheduled_task` = **a regra** (quando, o quê, para quem, com que guardas).
- `agent_job` = **cada disparo**, com `extra_metadata.scheduled_task_id`.

De brinde: histórico por tarefa, falha de um disparo não mata a série, "rodar
agora" é só materializar um job fora de hora, e **toda a entrega, o caderno de
trabalho e o replay offline continuam funcionando sem uma linha nova**.

### 3.2 Um tick de agenda, execução no JobManager

Loop próprio (`SchedulerTick`, 20–30s) faz **só** três coisas: achar tarefas
vencidas, avaliar as guardas, materializar o `agent_job` e recalcular
`next_run_at`. Quem executa e entrega continua sendo JobManager + Dispatcher.
Nenhuma concorrência nova, nenhum caminho de entrega novo.

### 3.3 O prompt agendado roda no `process_user_input`, com `source=schedule`

Nada de reimplementar meio turno. O runner consome o generator até `is_final`,
e a resposta final vai para o Dispatcher como qualquer outro job. Mas o turno
**precisa saber que não é o usuário falando** (§6) — senão polui afeto, birra,
rotação de sessão e modo live.

### 3.4 Ação é um **union tipado**, não um campo de texto

O pedido "não precisa ser prompt obrigatório" é o requisito de projeto mais
importante: **a maioria das tarefas úteis não quer LLM nenhum**. Um alerta às 7h
que paga um turno cognitivo é caro, lento, não-determinístico e falha em silêncio.

### 3.5 Tarefa agendada roda sem ninguém na frente → precisa de política de tools

Um prompt noturno que decide usar `run_command` no desktop trava 300s se o
desktop está offline (precedente medido: tools fantasma sem executor). Toda
tarefa carrega uma `tool_policy` e o padrão é **proibir atuação client-side**.

---

## 4. Modelo de dados

```
scheduled_task
  id, user_id, name, description, enabled (bool), created_at, updated_at

  -- QUANDO
  schedule_kind      : once | interval | cron | dataset        (§7)
  schedule_expr      : "2026-09-20T15:00" | "15m" | "0 7 * * 1-5" | slug do cadastro
  timezone           : "America/Fortaleza" (default do usuário)
  window_json        : {dias:[1..7], hora_inicio:"08:00", hora_fim:"22:00",
                        pular_feriado:bool, respeitar_sono:bool}
  jitter_seconds     : int (0 = exato)
  starts_at, ends_at : datetime | null
  max_runs           : int | null          -- teto de disparos (igual max_fires)
  misfire_policy     : skip | run_once | run_all      (§7.3)
  overlap_policy     : skip | queue | cancel_previous

  -- O QUÊ
  action_kind        : prompt | tool | notify | skill | job | pipeline   (§5)
  action_json        : payload por kind
  tool_policy_json   : {allow:[...], deny:[...], client_side:false,
                        max_llm_calls:int, max_seconds:int}
  session_mode       : nova | propria | ativa            (§6.1)
  session_id         : uuid | null   -- fixado quando session_mode=propria

  -- GUARDAS (§8)
  condition_json     : null | {tipo: tool|sql|dataset|device|llm, ...}
  only_if_changed    : bool
  last_fingerprint   : text | null
  requires_approval  : bool

  -- ENTREGA
  notify_policy      : chat | notch | both | silent | email | whatsapp | voz
  notify_escalation  : json  -- ["notch","chat","whatsapp"] com regra de quiet hours
  quiet_hours_json   : {inicio:"23:00", fim:"07:00", acao: adiar|silenciar|forcar}

  -- ESTADO
  next_run_at (index), last_run_at, last_job_id, last_status,
  run_count, consecutive_errors, paused_reason
```

`agent_job` ganha apenas `kind` novos — `scheduled_prompt`, `scheduled_tool`,
`scheduled_notify` — e `extra_metadata = {scheduled_task_id, fire_at, run_seq}`.
Zero migration destrutiva.

**Opcional (F4): `scheduled_run`** se o histórico do `agent_job` não bastar —
mas comece sem: `list_agent_jobs` filtrado por `scheduled_task_id` já dá a tela.

---

## 5. As ações (`action_kind`)

| kind | payload | Custo | Para quê |
|------|---------|-------|----------|
| `prompt` | `{texto, contexto_extra?, modelo?}` | turno completo | "Resume minha agenda e o que ficou pendente ontem" |
| `tool` | `{tool, arguments}` | 1 chamada, **sem LLM** | `email_send`, `fin_lembrete`, `device_control`, `wm_write`, `data_insert` |
| `notify` | `{titulo, corpo, canal}` | ~0 | "Reunião em 10 min" — texto fixo, determinístico |
| `skill` | `{skill, entrada}` | pipeline da skill | reusa a biblioteca de skills existente |
| `job` | `{kind: research\|browser_local\|sandbox_task, spec}` | job normal | `deep_research` semanal, NFSe todo dia 1º |
| `pipeline` | `[passo, passo, ...]` | composto | tool → condição → prompt → notify (§5.1) |

### 5.1 Composição: tool primeiro, cérebro só se precisar

O padrão que resolve 80% dos casos reais sem pagar LLM à toa:

```
1. tool  → colhe o dado    (email_inbox / fin_saldo / http do gateway / sql)
2. guard → só segue se X   (tem item novo? valor > limite? mudou desde ontem?)
3. prompt→ redige/decide   (opcional; recebe o resultado do passo 1 no contexto)
4. notify→ entrega         (canal conforme hora e device online)
```

Sem o passo 2, todo alerta periódico vira ruído. **Com** ele, o Neo só fala
quando tem o que dizer — e essa é a diferença entre uma agenda que se usa e uma
que se desliga na segunda semana.

---

## 6. O turno agendado — o que muda em relação a um turno de verdade

Runner de `scheduled_prompt`:

```python
metadata = {
    "source": "schedule",            # ← a chave de tudo
    "scheduled_task_id": str(task.id),
    "client": "neo-scheduler",
    "unattended": True,              # ninguém na frente da tela
}
async for resp in ai_service.process_user_input(
    user_message=action["texto"],
    session_id=session_id,           # §6.1
    user_id=task.user_id,
    service="neo-scheduler",
    metadata=metadata,
):
    if resp.is_final:
        final = resp.message
```

Os seis cuidados (cada um é um bug real esperando):

### 6.1 Sessão — três modos, e o padrão **não** é a sessão ativa
- `nova` (padrão): sessão nova por disparo. Não polui a conversa do usuário, não
  dispara rotação de tópico, não envenena o embedding de similaridade.
- `propria`: uma sessão fixa **da tarefa**, reusada em todo disparo — é o que faz
  "compare com o resultado de ontem" funcionar sem tool nenhuma.
- `ativa`: injeta na sessão corrente. Útil para "me pergunta daqui a 1h sobre
  isso", perigoso para o resto.

### 6.2 Afeto e birra não podem tratar isso como fala do usuário
`affect_updater` roda ao fim de todo turno de `process_user_input`. Um cron de 15
em 15 minutos vira um usuário que fala 96 vezes por dia. **Turno com
`source=schedule` não alimenta afeto nem pode disparar birra** (a birra é sobre
pedido do usuário; não existe pedido aqui).

### 6.3 Modo live / voz
Turno agendado **não** é live por padrão (senão vira markdown falado, ou fala
sozinho de madrugada). Voz só quando `notify_policy=voz` **e** o device está
ativo **e** fora do quiet hours.

### 6.4 Tools client-side: proibidas por padrão
Sem device online, a atuação client-side fica pendurada até o timeout. Com
`tool_policy.client_side=false`, o prompt do turno recebe a linha *"você está
rodando sem ninguém na frente; não existe máquina do usuário agora"* e o executor
recusa a tool com um erro legível (que o cérebro consegue contornar). Quando
`client_side=true`, a guarda passa a ser: **device online?** Se não, aplica
`misfire_policy` em vez de tentar.

### 6.5 O executor por-request precisa virar helper
`_handle_server_tool` monta hoje, inline, o stack que dá `user_id` às tools
(`DevicesContextExecutor` → `_with_job_tools` → scoping de `pks_search`,
`voice`, `config`). O runner de `scheduled_tool` **tem** que usar o mesmo stack.
Se montar outro, repete na íntegra o bug de `data_*`/`event_*`/`fin_*` rodando
sem usuário. Extrair `build_request_executor(user_id, session_id, metadata)` e
chamar dos dois lados é pré-requisito da fase F2.

### 6.6 Tetos por disparo
`max_llm_calls`, `max_seconds` (vira `timeout_at` do job) e teto de disparos por
dia por tarefa. Um prompt agendado em loop de tool é a forma mais fácil de
queimar carteira de LLM dormindo.

---

## 7. Quando disparar

### 7.1 Os quatro tipos
- **once** — ISO datetime. "Me lembra amanhã 15h." Ao disparar, `enabled=false`.
- **interval** — `"15m"`, `"2h"`, `"30s"` (piso de 1 min).
- **cron** — 5 campos, `croniter` (dep nova: pure-python, **tem que entrar na
  imagem**, não em runtime) ou parser próprio de ~80 linhas se preferir zero dep.
- **dataset** — §7.5, o tipo que só este projeto tem.

Tudo em `America/Fortaleza` por padrão. Grava-se **UTC naive** no banco (igual ao
resto do core) e converte-se na borda; `timezone` fica na linha para o painel
mostrar e para o cálculo do próximo disparo. Fortaleza não tem horário de verão —
o cálculo é trivial hoje, mas guardar o tz evita reescrever tudo se o usuário
viajar.

### 7.2 Janelas e feriados
`window_json` filtra **depois** do cálculo: dias da semana, faixa de horas,
`pular_feriado` (feriado nacional BR + os do usuário num cadastro) e
`respeitar_sono` (reusa `sleep_tz`/janela de sono do afeto). Disparo que cai fora
da janela é **empurrado para o próximo horário válido**, não perdido.

### 7.3 Disparo perdido (`misfire_policy`) — a guarda mais importante **deste** repo
O neo-core roda com `--reload`: **salvar arquivo é deploy**, e todo save derruba o
processo. Um cron de 5 em 5 minutos vai perder disparos toda semana.
- `skip` (padrão): perdeu, perdeu. Reagenda para o próximo.
- `run_once`: roda **um** disparo atrasado se ainda estiver dentro do
  `grace_seconds` (ex.: 10 min). É o certo para o briefing das 7h.
- `run_all`: roda todos os perdidos. Só para contadores; nunca para notificação
  (é a receita para 14 mensagens ao mesmo tempo depois de um restart).

O boot precisa de um **`catch_up` explícito** no start do scheduler, com trava de
"máximo N disparos atrasados por tarefa por boot".

### 7.4 Sobreposição e disjuntor
- `overlap_policy`: o disparo anterior ainda está `running`? `skip` (padrão),
  `queue` ou `cancel_previous`.
- **Disjuntor**: `consecutive_errors >= 3` → `enabled=false`,
  `paused_reason="3 falhas seguidas"` **e uma mensagem ao usuário**. Mesma
  constante e mesma semântica de `core/events/router.py`. Uma agenda que falha
  em silêncio é pior que não ter agenda.
- **Jitter**: `jitter_seconds` aleatório evita cinco tarefas colidindo às 7h em
  cima do mesmo modelo/carteira.

### 7.5 `dataset` — agenda derivada de cadastro (a ideia grande)
Os cadastros generativos já existem (`/dados`, tools `data_*`). Então uma tarefa
pode ser: *"para cada linha do cadastro `contas`, dispare 3 dias antes de
`vencimento`"*.

```
schedule_kind = dataset
schedule_expr = "contas"
action_json   = {"campo_data":"vencimento","offset":"-3d",
                 "acao":{"kind":"notify","titulo":"{descricao} vence em 3 dias",
                         "corpo":"R$ {valor} — {credor}"}}
```

Isso cobre de uma vez: contas a pagar, aniversários, consultas, vistorias,
renovação de assinatura, validade de certificado. **Uma regra, N disparos, e o
usuário mantém a lista num CRUD que ele já tem** — em vez de criar 40 crons.

---

## 8. A parte "inteligente" — guardas antes de gastar turno

### 8.1 `condition_json` — só dispara se
| tipo | exemplo | custo |
|------|---------|-------|
| `tool` | `email_inbox(filtro=...)` retornou ≥1 item | 1 chamada |
| `sql` | `SELECT count(*) FROM ... > 0` (módulo `sqlro` já existe, read-only) | ~0 |
| `dataset` | alguma linha de `contas` com `status=aberto` | ~0 |
| `device` | o desktop está online / estou em casa | ~0 |
| `http` | endpoint do gateway devolve 200 e `json.preco < 3000` | 1 request |
| `llm` | modelo barato responde "isso merece interromper agora?" | 1 chamada curta, **off por padrão** |

Condição falsa = **não conta como erro, não conta como run, não notifica**. Só
reagenda.

### 8.2 `only_if_changed` — o antídoto da fadiga de alerta
Hash do resultado normalizado em `last_fingerprint`. Igual ao anterior → silêncio.
Transforma "a cada 15 min me diz o preço" (ruído) em "me avisa quando o preço
mudar" (valor). Vale também para status de engine, saldo, caixa de email, saída
de script.

### 8.3 Entrega escalonada + quiet hours
`notch → chat → whatsapp → email`, decidido na hora do disparo por: device
online? dentro do quiet hours? urgência da tarefa? Em quiet hours: `adiar`
(entrega às 7h), `silenciar` (só grava, sem push) ou `forcar` (alarme mesmo).

### 8.4 Snooze e ack na notificação
Botão "adiar 1h" / "já fiz" no notch e no chat. "Já fiz" fecha o disparo do dia
(e, num lembrete de cadastro, marca a linha). Sem isso, lembrete recorrente vira
o alarme que a gente aprende a ignorar.

### 8.5 Promessa vira agenda
O ciclo de auto-continuação (`[[HANDOFF:continuar]]`) resolve a promessa **dentro
do turno**. A promessa que atravessa o tempo — *"te aviso quando o preço cair"*,
*"amanhã eu confiro"* — não tem onde morar hoje. Com a agenda, o cérebro ganha
`schedule_create` e o backstop de promessa passa a ter um destino real: promessa
com prazo → tarefa agendada com condição. **Isso fecha um buraco que já foi
medido no self-continuation.**

---

## 9. Tools do cérebro (criar agenda conversando)

| tool | assinatura |
|------|------------|
| `schedule_create` | `(nome, quando, acao, canal?, condicao?, sessao?)` — `quando` em linguagem natural pt-BR resolvida no servidor |
| `schedule_list` | `(ativas_apenas?)` |
| `schedule_update` | `(id, campos)` |
| `schedule_pause` / `schedule_resume` | `(id)` |
| `schedule_cancel` | `(id)` |
| `schedule_run_now` | `(id)` — materializa um disparo fora de hora |

"Todo dia útil às 8h me manda o resumo dos emails não lidos" precisa virar
`cron 0 8 * * 1-5` + `action=prompt` + `condition=tool(email_inbox)` **numa
chamada só**. O parser de tempo pt-BR ("toda segunda", "dia 5", "daqui a 2h",
"amanhã de manhã", "todo fim de mês") é determinístico e vive no servidor — o
modelo passa o texto, o servidor resolve e **devolve o horário calculado para o
Neo confirmar em voz alta**: *"marquei para segunda, 15/09, às 08:00"*. Confirmar
o horário resolvido é o que impede o cron fantasma às 3h da manhã.

**Backstop no `intent_router`** (mesmo padrão do `pks_remember`): "me lembra",
"todo dia", "toda semana", "às 7h" são frases que não podem depender de o
classificador estar inspirado.

---

## 10. API + painel

`api/routes/schedules.py` (JWT do painel), prefixo `/api/v1/agenda`:

```
GET    /tarefas                 lista (com next_run_at e último status)
POST   /tarefas                 cria
GET    /tarefas/{id}            detalhe + próximos 5 disparos calculados
PATCH  /tarefas/{id}            edita / liga / desliga
DELETE /tarefas/{id}
POST   /tarefas/{id}/rodar      dispara agora
POST   /tarefas/{id}/simular    dry-run: mostra o que rodaria, sem entregar
GET    /tarefas/{id}/disparos   histórico (agent_job filtrado)
POST   /pausar-tudo             kill switch global
```

### Tela `/agenda` (grupo **Operação**, ao lado de Pesquisas)
- **Lista**: nome, próxima execução ("hoje 19:00"), última ("ontem ✅"), canal,
  toggle de ligar/desligar, badge de pausada-por-erro.
- **Form** com o tipo de ação em abas (Prompt / Tool / Alerta / Skill / Job) —
  o form do prompt é uma textarea grande com preview do que será injetado; o de
  tool é um select do registry com os campos gerados do schema (já existe
  precedente nos cadastros).
- **Construtor de horário** com **preview humano obrigatório**:
  *"toda segunda-feira às 07:00 · próximos: 15/09 07:00, 22/09 07:00, 29/09 07:00"*.
  Ninguém deve precisar saber cron para usar isso.
- **Simular**: roda com entrega desligada e mostra o resultado. É o que evita
  descobrir que o prompt estava ruim depois de três dias de mensagem errada.
- **Histórico por tarefa**: disparo, duração, status, mensagem entregue, link do
  log. Reusa o visual de Pesquisas/Missões.
- **Kill switch** visível no topo.

---

## 11. Fases

| Fase | Entrega | Por que nesta ordem |
|------|---------|---------------------|
| **F1** | `scheduled_task` + migration + `SchedulerTick` + kinds `once`/`interval`/`cron` + ações `notify` e `tool` + CRUD + tela básica | Já é útil no primeiro dia e **não depende de LLM nenhum** — dá para validar o relógio, o claim e a entrega sem variável aleatória |
| **F2** | `build_request_executor` extraído + ação `prompt` (`source=schedule`, modos de sessão, guardas de afeto/live/client-side) | O pedido central, sobre trilhos já testados |
| **F3** | Condições, `only_if_changed`, janelas, misfire/overlap, disjuntor, jitter | O que separa agenda usável de gerador de ruído |
| **F4** | Tools `schedule_*` + parser pt-BR + backstop no router + confirmação do horário resolvido | Criar agenda conversando |
| **F5** | `dataset` schedules, escalonamento de entrega, quiet hours, snooze/ack | Lembretes de verdade |
| **F6** | Ações `skill`/`job`/`pipeline`, aprovação prévia, relatório semanal do que a agenda fez | Composição |

---

## 12. Riscos e gotchas específicos deste repo

1. **`--reload` derruba o processo a cada save.** O scheduler precisa ser
   restart-safe por construção: estado só no DB, catch-up no boot com teto, e
   `skip` como misfire padrão. (Ver `neo-grpc-transport-error-reload-storm`.)
2. **Dependência nova vai na imagem.** `croniter` no `pyproject` + rebuild; não
   dá para `pip install` em runtime (já queimou no toolset de documentos).
3. **Datetime naive vs aware.** O core grava naive-UTC. Todo cálculo de próximo
   disparo converte para tz do usuário, calcula, e volta para naive-UTC. Um
   `astimezone` esquecido = tarefa 3h fora do horário.
4. **Tool sem `user_id`.** Só usar o executor por-request (§6.5).
5. **`fin_*` e o ledger.** Tarefa agendada que mexe em dinheiro passa pelo
   `ledger_guard` como qualquer outra; nada de saldo indo parar no PKS.
6. **Uma instância só.** O claim atômico cobre dois processos, mas o lock por
   tarefa é em memória (igual às inscrições). Se um dia houver dois workers,
   advisory lock do Postgres.
7. **Custo.** Prompt agendado é a única feature do Neo que gasta dinheiro
   **sozinha**. Teto por tarefa, teto global diário e o gasto visível na tela.

---

## 13. Catálogo de ideias (o que dá para fazer no dia seguinte)

**Rotina pessoal**
1. Briefing das 7h: agenda do dia + emails que importam + clima + o que ficou pendente ontem (1 prompt, sessão `propria` → compara com ontem sozinho).
2. Fechamento das 22h: o que o Neo fez hoje (lê `agent_job` + caderno) e o que ficou aberto.
3. "Amanhã me pergunta se resolvi X" — follow-up de conversa, o Neo puxando assunto.
4. Lembrete de fatura/conta a partir do cadastro `contas` (§7.5), 3 dias antes.
5. Aniversários e datas do cadastro de pessoas, com sugestão de mensagem.
6. Revisão semanal de finanças: gasto por categoria vs. semana passada, só se variou >20%.

**Vigia de infra (tudo com `only_if_changed`)**
7. Engines do PC gamer (TTS/STT/F5/CosyVoice) a cada 5 min → alerta só na mudança de estado.
8. WhatsApp: sessão caiu / QR expirou.
9. Disco, memória e containers da VPS todo dia 6h.
10. Backup do Postgres às 3h + verificação de que o dump não está vazio (**verificar o backup é metade do backup**).
11. Certificados e domínios vencendo em 30 dias.
12. Pi-hole/WireGuard/LXCs: ping semanal do inventário de máquinas.

**Informação**
13. `deep_research` semanal sobre um tema fixo, entregue segunda 8h.
14. Monitor de preço (Amazon é a única que abre preço) com série no cadastro → avisa só na queda.
15. Triagem de email a cada 30 min: só interrompe se casar com critério.
16. Resumo diário de um site/feed via `web_fetch`.
17. NFSe / obrigação mensal via `browser_local` todo dia 1º.

**Cérebro do Neo (sleep-time compute)**
18. Consolidação noturna do PKS: dedupe, promoção de fato episódico → permanente.
19. Resumo/arquivamento de sessões antigas; poda do caderno de trabalho.
20. Reindexação de embeddings e limpeza de artefatos órfãos do File Store.
21. Auto-avaliação semanal: rodar o corpus de mensagens reais contra as guardas do router e reportar regressão (já existe o corpus de ~1450 mensagens).
22. Decaimento do afeto e "humor do dia" calculado de madrugada em vez de no turno.

**Casa e aparelhos**
23. Rotina de sair/chegar: HA + volume + luzes em horário fixo, com condição de presença.
24. Despertador de verdade: TTS na voz escolhida, com escalonamento se não houver ack.
25. Sirene ONVIF: teste mensal automático.

**Meta**
26. Relatório semanal da própria agenda: o que disparou, o que foi ignorado, o que falhou — e sugestão de desligar as que ninguém lê.
27. Tarefa que cria tarefa: o Neo propõe um cron quando percebe repetição ("você me pede isso toda segunda; quer que eu passe a fazer sozinho?") — com aprovação.
28. Modo férias: pausa tudo que não for crítico até uma data.


---

## 14. O que ficou de pé (2026-09-12)

Implementado em uma leva, com a suíte inteira verde (2220 testes existentes + 58
novos, `tests/scheduler/`).

### Arquivos novos

| Arquivo | Papel |
|---------|-------|
| `db/models/scheduled_task.py` + `alembic/versions/y8z9a0b1c2d3_*` | A regra. Migration aditiva, já aplicada |
| `core/scheduler/timing.py` | Cron de 5 campos **escrito à mão** (com `L`), intervalo, uma vez, fuso, janela, quiet hours, descrição em português |
| `core/scheduler/nlp.py` | "todo dia útil às 8h" → `0 8 * * 1,2,3,4,5`, com o horário resolvido de volta |
| `core/scheduler/conditions.py` | Guardas (`tool`/`device`/`http`) + fingerprint do "só se mudou" |
| `core/scheduler/tick.py` | O relógio: claim, misfire, sobreposição, condição, materialização, disjuntor |
| `core/scheduler/runners.py` | Os quatro disparos (`notify`/`tool`/`job`/`prompt`) + contadores da tarefa |
| `core/scheduler/service.py` | CRUD e validação compartilhados por painel e tools |
| `core/scheduler/tools.py` + `executor.py` | As 7 tools `schedule_*`, por-request, com usuário |
| `api/routes/schedules.py` | `/api/v1/agenda` — CRUD, preview, simular, disparos, kill switch |
| `neo-panel/src/AgendaPanel.tsx` | A tela `/agenda`, com preview de horário ao vivo |

### Decisões que mudaram em relação ao plano

1. **Sem `croniter`.** Dependência nova derruba prod no reload (memória
   `neo-dev-topology-prod-tunnel`), e o cron que a agenda precisa coube em 150
   linhas testadas. Zero dependência nova, zero rebuild.
2. **`build_request_executor` extraído de verdade** (`ai_service.py:5121`), e o
   `_handle_server_tool` passou a chamá-lo. Era pré-requisito de §6.5 e agora é
   um ponto só — a agenda dispara tool com exatamente o mesmo escopo de usuário
   que um turno normal.
3. **`texto_literal`.** O `ProactiveDispatcher` reescrevia tudo com um LLM. Um
   alerta de texto fixo virava paráfrase e a resposta de um turno agendado (que
   já sai com a persona certa) era reescrita por um modelo menor. Quem sabe o
   texto final agora manda em `extra_metadata.texto_literal` e o dispatcher
   entrega como está.
4. **Ação `job` reusa a tool** (`deep_research`, `browser_local_task`…) em vez
   de montar o `agent_job` na mão — do contrário uma pesquisa agendada nasceria
   sem a `research_task` e o job rodaria sem ter o que executar.
5. **Guardas do turno agendado** ficaram nos três pontos que o plano previu:
   `source=schedule` tira o turno do afeto e da birra (`ai_service.py`, dois
   sítios), `unattended` tira as tools client-side do toolset e **devolve um
   CLIENT_TOOL ao cognitivo** em vez de deixá-lo pendurado até o timeout.
6. **A cópia no claim.** `claim_due_scheduled_tasks` grava a sentinela mas
   devolve o objeto com o `next_run_at` **original** — é dele que sai a decisão
   de misfire. O primeiro teste do tick pegou exatamente isso (o fake devolvia a
   sentinela e todo disparo parecia adiantado).

### O que NÃO entrou (e por quê)

- **F5 `dataset` schedules** (§7.5) — a ideia grande, mas depende de decidir
  como a linha do cadastro vira disparo idempotente (não pode avisar duas vezes
  da mesma conta). Merece desenho próprio.
- **Escalonamento de entrega e snooze/ack** (§8.3/§8.4) — o `quiet_hours` com
  `adiar`/`silenciar`/`forcar` está de pé; a escada notch→chat→WhatsApp→email e
  os botões de adiar/já-fiz pedem mexer no cliente.
- **`requires_approval`** (§F6) — a coluna não existe ainda; hoje a proteção é a
  `tool_policy` (client-side proibido por padrão) e o teto de tempo.
- **Ação `pipeline`** — `tool → guard → prompt → notify` numa tarefa só. Hoje se
  faz com duas tarefas ou com uma condição + prompt.

### Como ligar

```bash
# .env da RAIZ (o container não lê o neo-core/.env)
NEO_SCHEDULER_ENABLED=true
docker compose up -d --no-deps neo-core
```

Com a flag off: sem tabela consultada, sem laço, sem tools no toolset, e a API
responde 503. Rollback é desligar a flag.
