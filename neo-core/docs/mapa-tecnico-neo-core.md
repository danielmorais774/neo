# Mapa técnico do Neo Core

Estado em 14/11/2026. Este mapa descreve como um pedido entra no Neo, quem decide o que fazer com ele e por onde a resposta sai. Para o detalhe de cada parte, siga os links para o código e para os planos em `docs/`.

**Em uma frase:** todo pedido passa por um classificador barato que escolhe o trilho. Quase tudo cai no cérebro, um laço de LLM com ferramentas. O que é perigoso, caro ou fácil de errar é decidido por código, não pelo modelo.

---

## 1. O caminho de um pedido

```mermaid
flowchart TD
    subgraph Entrada
        D[Desktop Tauri<br/>gRPC :50051]
        P[neo-panel<br/>NDJSON /desktop/chat/stream]
        W[WhatsApp<br/>webhook /whatsapp]
        A[Agenda<br/>SchedulerTick]
        J[Fim de job<br/>Continuação automática]
    end

    D & P & W & A & J --> S[AIService.process_user_input<br/>autoridade do turno + sessão + contexto]
    S --> R{Intent router<br/>classificador LLM + guardas}

    R -->|DIRECT_RESPONSE<br/>padrão na dúvida| C[Cérebro<br/>laço de rodadas com tools]
    R -->|SERVER_TOOL| ST[Agente de tool do servidor]
    R -->|CLIENT_TOOL| CT[Comando na máquina dele<br/>run_command / save_generated_file]
    R -->|HOME_ASSISTANT<br/>WHATSAPP_MESSAGING| SK[Skill com efeito colateral]
    R -->|needs_plan| PL[PlanManager<br/>passos com dependência]

    C -->|tool rápida| C
    C -->|tool longa| JB[JobManager<br/>agent_job]
    C -->|HANDOFF| SK
    CT -->|resultado| C
    PL --> C & CT

    JB --> DP[ProactiveDispatcher]
    C --> E[Entrega<br/>stream, bolhas, arquivos, voz]
    ST & SK --> E
    DP --> E
```

Passo a passo, em [modules/desktop/ai_service.py](../modules/desktop/ai_service.py) (`process_user_input`):

1. **Autoridade do turno.** Antes de tudo, o código registra quem pediu e o que veio de fora (texto colado, URL que você escreveu). Ver §10.
2. **Entrada por voz.** O backstop determinístico corrige nomes que o STT erra, antes do classificador.
3. **Sessão.** O turno pega ou cria a sessão. No chat ela só muda por ação sua; o modo live expira pelo relógio.
4. **Contexto, em paralelo:** histórico, recuperação semântica, PKS e caderno. Depois vem um segundo passe com o embedding pronto. Ver §3.
5. **Contexto do sistema:** modo live, voz, imagens, identidade, manifesto dos anexos, artefatos, ledger de arquivos e a tarefa viva do navegador.
6. **Classificação.** O roteador decide o trilho. Ver §4.
7. **Despacho.** O `PipelineOrchestrator` roda o passo e os handoffs que ele gerar.
8. **Saída.** Todo trilho passa pelo ponto do afeto e pelo epílogo da birra antes de sair. O diário da sessão grava cada etapa.

---

## 2. Canais de entrada

| Canal | Transporte | Entra em | Particularidade |
|---|---|---|---|
| Desktop (Tauri) | gRPC bidi `ExecuteTask`, porta 50051 | [grpc_server.py](../grpc_server.py) → `AIService` | Único canal que executa tool no Mac (`tool_request`) |
| neo-panel | `POST /api/v1/desktop/chat/stream` (NDJSON) | [api/routes/desktop.py](../api/routes/desktop.py) | Ignora `tool_request`: o navegador não tem SO |
| WhatsApp | serviço Node `neo-whatsapp` → webhook | [api/routes/whatsapp.py](../api/routes/whatsapp.py) | Remetente vira principal `externo` |
| Voz / modo live | STT :5005 e TTS :5006 no PC gamer; desktop fala direto na LAN | `metadata["mode"]="live"` | Resposta final sai falada; sessão expira por inatividade |
| Agenda | `SchedulerTick` → `agent_job` `scheduled_*` | [core/scheduler/](../core/scheduler/) | Principal `agenda`, sem ninguém na tela: CLIENT_TOOL não vale |
| Fim de job | mensagem sintética `[Continuação automática…` | volta ao laço como turno novo | PKS não aprende com ela |
| Aparelhos | `/ws/devices/*` (online, notificações, controle) | [api/routes/devices.py](../api/routes/devices.py) | A extensão do navegador é um aparelho pareado |
| Eventos de mídia | `POST /api/v1/eventos/midia` | [core/events/router.py](../core/events/router.py) | Não acorda o cérebro: a resposta do POST já é o comando |

**Atenção:** `127.0.0.1:8000` e `:50051` no Mac são um túnel para a VPS. Testar "local" é testar em produção.

---

## 3. Sessão e montagem do contexto

O [SessionManager](../core/session/session_manager.py) cria, recupera e fecha sessões e conta tokens. A rotação por tópico ainda existe, mas não vale nos canais de chat (`neo-desktop`, `neo-panel`): ali a conversa só muda por ação sua.

O prompt do cérebro é montado pelo [PromptContextSelector](../core/agent/context_selector.py), que pontua candidatos e enche um orçamento de tokens:

- **Blocos do PKS**, os mesmos em todo turno: `neo-personality`, `neo-response-rules` e `neo-communication-preferences` (estes dois de alta prioridade), `daniel-profile`, `active-projects`, `neo-goals`, `neo-operating-procedures`, `neo-world-state` e `neo-training-examples`.
- **Recuperação de contexto**: trechos de conversas antigas por busca híbrida, com o rótulo `[memória · outra conversa · dd/mm]`.
- **Índice do caderno**: uma linha por nota ativa, nunca o corpo.
- **Estado emocional**, só quando o límbico sai do repouso.
- **Histórico** com o `tool_trace` dos turnos anteriores, para um "cadê os arquivos?" não refazer o trabalho do zero.

Lição: uma regra enterrada em 27 mil caracteres de sistema perde para skill ativa e para resultado de tool. A mesma frase logo depois da mensagem do usuário deu 8 de 8 (sessão 455646dd). Posição pesa mais que redação.

---

## 4. Intent router

Fica em [core/router/intent_router.py](../core/router/intent_router.py). Roda antes de todo turno, num modelo flash barato: hoje `minimax-m3.1-flash@nebius`, com teto de raciocínio de 300 tokens. Devolve um `RoutingDecision` ([shared/types.py](../shared/types.py)).

### Os trilhos

| Intent | Quando | Quem executa |
|---|---|---|
| `DIRECT_RESPONSE` (= `cognitive`) | Conversa, conhecimento, dado pessoal e tudo que roda no sandbox. **Padrão na dúvida.** | Cérebro |
| `SERVER_TOOL` | Dado em tempo real (clima), configuração do Neo, print do browser do servidor | Agente de tool no neo-core |
| `CLIENT_TOOL` | Só o que exige a máquina dele: `run_command`, `write_file`, `save_generated_file` | Desktop |
| `HOME_ASSISTANT` | Casa inteligente, num imperativo claro | Skill HA |
| `WHATSAPP_MESSAGING` | Mandar mensagem em nome dele | Pipeline do WhatsApp |
| `MUSIC` | Nunca direto: chega por handoff do cérebro, para as regras de gosto do PKS valerem | Skill de música |

### Os campos que mais pesam

- **`onde`** (`sandbox`, `maquina`, `navegador`, `nenhum`): a decisão que mais erra. Quem decide é a **origem** do conteúdo, nunca o destino. Baixar um PDF da internet e salvar no Desktop é `sandbox` + ponte, não `maquina`.
- **`speech_act`** e **`confidence`**: skill com efeito colateral só dispara com imperativo e confiança ≥ 0,75.
- **`needs_plan`** e **`tipo_tarefa`**: levam ao PlanManager (§6).
- **`pipeline_plan`**: uma cascata declarada de 2 ou mais passos.
- **`skill_name`** / **`skill_hint`**, `needs_memory` e `knowledge_keywords`.

### Camadas em volta do LLM, na ordem

1. **Guardas determinísticas antes do LLM.** Pegam treino do Neo, diretiva de memória ("lembra que…"), pergunta sobre aparelhos conhecidos, pergunta referencial ("de onde veio esse 22?"), ação condicional, música, status de mídia e nome de outro assistente ("alexa, …"). Todas caem no cérebro. Regra de ouro do arquivo: não criar padrão por frase para consertar rota errada.
2. **Classificador LLM**, com saída JSON nativa e reparo de rede.
3. **Vieses depois do LLM:**
   - `_bias_to_brain`: devolve ao cérebro o que não for imperativo claro;
   - `_bias_local_browser` e `_bias_claude_code`;
   - viés de afeto;
   - heurísticas de memória.
4. **Overrides no `ai_service`:** `force_intent` no metadata, tarefa do Claude Code esperando resposta (`[INTENT-OVERRIDE]`), turno agendado sem CLIENT_TOOL e tarefa viva no navegador (vai ao cérebro).

Para medir mudança no prompt: `scripts/eval_router.py` com o golden set em `tests/router/golden_intents.yaml`.

---

## 5. Cérebro (laço cognitivo)

Fica em `_run_cognitive_tool_loop`, no [ai_service.py](../modules/desktop/ai_service.py). O modelo de produção é `deepseek-v4.1-flash` pelo OpenRouter, com o provider `open-inference` ignorado.

**O laço:** a cada rodada, o modelo recebe o contexto e as tools, chama zero ou mais tools, os resultados voltam como mensagem, e o ciclo segue. Termina quando ele responde sem chamar tool, ou no teto.

| Regra | Valor | Onde |
|---|---|---|
| Teto de rodadas | 32 | `cognitive_max_tool_rounds` (shared/config.py) |
| Temperatura da 1ª rodada (persona) | 0,9 | `[cerebro] temperatura` |
| Temperatura depois da 1ª tool (código, argumentos) | 0,3 | `temperatura_com_ferramentas` |
| Rodadas seguidas só com repetição antes de cortar as tools | 3 | `repeticoes_seguidas_max` |
| Trecho do resultado anterior no aviso de repetição | 800 chars | `repeticao_trecho_max` |

**Guardas que viraram código** (cada uma nasceu de uma sessão real):

- **Fim das rodadas:** a última rodada vai sem tools **e** com `_AVISO_FIM_DAS_RODADAS`. Sem o aviso, o DeepSeek devolvia `"\n\n"` (sessão 3229310a).
- **Chamada repetida:** não roda de novo. O modelo recebe `NÃO RODOU — chamada REPETIDA`, com o começo e o fim dos argumentos e o resultado anterior.
- **Lote de chamadas:** contaminação que chega dentro do lote de uma mesma resposta só vale quando o lote fecha (`lote_de_chamadas`, em core/authority/turn.py).
- **Anti-mentira de skill:** se a resposta afirma "registrei" e nenhuma tool da skill rodou, o turno ganha uma rodada extra com a tool obrigatória.
- **Anti-HANDOFF-de-tool:** `[[HANDOFF:save_generated_file]]` escrito em vez de chamado vira a chamada de verdade.
- **Auto-continuação:** `[[HANDOFF:continuar]]` abre outro passe. Um verificador pega a resposta que promete trabalho sem o bloco.
- **`annotate_result`:** o código cola avisos no resultado da tool, como "este arquivo é de OUTRA conversa" e "URL tirada da memória falhou, busque".

**Handoffs:** o cérebro aciona uma skill escrevendo `[[HANDOFF:home_assistant]]<ordem>[[/HANDOFF]]`. O [handoff_parser](../core/pipeline/handoff_parser.py) extrai o bloco e o [orchestrator](../core/pipeline/orchestrator.py) enfileira o passo. É assim que música e HA passam pelas regras do PKS.

---

## 6. Planos, skills e pipeline

**PlanManager** ([core/planning/plan_manager.py](../core/planning/plan_manager.py)):
- guarda os planos em memória, por sessão, com passos ordenados e sub-planos;
- depois de cada `tool_result`, o plano avança e se refina (`advance_and_refine`);
- o plano guarda o **pedido literal** (`ExecutionPlan.pedido`), não só a paráfrase. Sem isso, o zip de 4,5 GB foi parar em `~/` com a árvore `Users/daniel/...` (sessão 7b61bc5e).

**Skills** (`neo-core/skills/<nome>/SKILL.md`, carregadas por [modules/skills_library](../modules/skills_library/)):
- hoje: `analise-csv`, `clientes`, `compras`, `criar-cadastro`, `diagnostico-voz`, `docx`, `financas`, `imitacao-vozes`, `ocr-video`, `playlists`, `postgres-neo`, `produtos`;
- **tool gating:** a skill `tool_backed` destrava as tools dela quando o cérebro faz `skill_read`;
- **autoload por palavra-chave:** a skill entra sozinha e força `tool_choice=required` na 1ª rodada.

**Cadastros** (`datasets:` no SKILL.md): viram tabela mais as tools `data_*`, sem código novo.

---

## 7. Ferramentas e jobs

### Três lugares físicos, sem filesystem compartilhado

```mermaid
flowchart LR
    SB["Sandbox do cérebro<br/>/workspace na VPS<br/>tools neo_*"] -->|file_save| FS[("File Store<br/>catálogo do neo-core")]
    FS -->|save_generated_file<br/>CLIENT_TOOL| MAC["Máquina dele<br/>disco, apps, shell"]
    MAC -->|request_user_file<br/>anexo| FS
    FS -->|file_*| SB
```

O [FileLedger](../core/files/ledger.py) registra onde está cada cópia, em dois anéis: arquivos desta conversa e de outras. Nada cruza por caminho escrito à mão: um `/workspace/...` num `run_command` sempre falha.

### Famílias de tool

| Família | Exemplos | Onde roda |
|---|---|---|
| Leitura do cérebro | `search_pks`, `wm_read`, `conversation_recall`, `get_weather` | neo-core |
| Web | `web_search` (só o Google CSE está vivo), `web_fetch` | neo-core |
| Sandbox | `neo_bash`, `neo_read_file`, `file_save`, `file_olhar` | container workbench |
| Documentos | `deep_read`, `docx_*` | job / sandbox |
| Cliente | `run_command`, `write_file`, `save_generated_file`, `read_user_file`, `edit_user_file` | Mac, via `tool_request` |
| Jobs | `neo_bash_background`, `job_*`, `job_falar`, `browser_local_task`, `claude_task`, `claude_reply` | JobManager |
| Domínio | `fin_*`, `data_*`, `ha_*`, música, agenda | neo-core |

O caminho CLIENT_TOOL: o core manda o `tool_request`, o desktop executa e devolve, e `process_tool_result` reentra no cérebro (`_reenter_cognitive`). Um comando que passa de 90 s vira tarefa local (`comando_local`). Todo resultado é cortado em 1 MB.

### JobManager

O [JobManager](../core/jobs/manager.py) roda um tick no mesmo processo do neo-core. Ele reivindica linhas de `agent_job` com claim atômico e dispara um worker por job, limitado por semáforo.

- **Runners:** `browser` (Chromium headless do servidor), `browser_local`, `claude`, `claude_sessao`, `comando_local`, `deep_read`, `download_watch`, `research` e `scheduled_*`.
- **Restart-safe:** no boot, job preso em `running` vira `failed`, exceto os de `REATTACHABLE_JOB_KINDS`, que voltam para a fila.
- **`neo_bash` acima de 120 s** vira job sozinho. O parâmetro `then` diz o que fazer quando ele terminar.
- **Entrega:** o [ProactiveDispatcher](../core/jobs/dispatcher.py) redige com a persona, salva na sessão de origem e publica `chat_message` no broker, com fan-out por aparelho e replay offline. Um texto literal pula o redator.

---

## 8. Navegador local

O navegador local é o Edge dele, logado, dirigido pela extensão e pelo desktop. Quem decide é o core; o aparelho só executa.

```mermaid
sequenceDiagram
    participant C as Cérebro
    participant G as Portão (gate.py)
    participant J as Job browser_local
    participant Dr as Driver (core)
    participant Dev as Desktop + extensão
    participant U as Daniel (painel da aba / chat)

    C->>G: browser_local_task(tarefa)
    G-->>C: só passa com âncora de posse E afordância
    C->>J: enfileira o job
    loop cada passo
        Dr->>Dev: browser.snapshot
        Dev-->>Dr: elementos numerados + texto
        Dr->>Dr: LLM escolhe UMA ação, validada contra o snapshot
        Dr->>Dev: executa (CDP na aba)
        Dev-->>Dr: ok:true / erro
        U-->>Dr: recado no painel ou job_falar
        Dr-->>U: avisar / perguntar (espera viva)
    end
    Dr->>Dr: conferência antes de Enviar
    Dr->>Dev: solta a aba (sempre)
    J-->>C: resultado REAL, não a narrativa
```

**Peças** (em [core/browser_local/](../core/browser_local/)):

- **[gate.py](../core/browser_local/gate.py):** só age na tela dele se ele pediu. A regra exige uma âncora de posse ("meu", "aqui") **e** uma afordância ("navegador", "aba").
- **[surfaces.py](../core/browser_local/surfaces.py):** quem pode dirigir um navegador. O desktop é o provedor completo (visível e headless); a extensão é um aparelho pareado.
- **[driver.py](../core/browser_local/driver.py)**, o laço ver → decidir → agir, com quatro princípios:
  1. a verdade vem do aparelho;
  2. `ok:true` não quer dizer que a página reagiu: tela que não mudou carimba o passo como inútil;
  3. ação ou `ref` inventados são recusados antes de sair;
  4. a aba é solta no fim, sempre.

  A percepção é texto por padrão. A imagem entra pela ação `look`, e quem responde é um modelo de visão, que não decide o passo.
- **[conversa.py](../core/browser_local/conversa.py):** caixa de entrada em memória por job vivo, com `avisar`, `perguntar`, o painel da aba e `job_falar`. A pergunta com botões só nasce pelo canal privilegiado, porque a página consegue imitar `postMessage`.
- **[respondedor.py](../core/browser_local/respondedor.py):** a ação `consultar` responde formulário pela memória, pergunta por pergunta, com aterramento. No driver, `_check_revisao` para antes de Enviar quando algum campo veio da memória.
- **[juiz.py](../core/browser_local/juiz.py):** julga grade de avaliação (`julgar`, `marcar_grade`) com modelo próprio. Linha sobre o Daniel volta `PESSOAL`.
- **[cofre.py](../core/browser_local/cofre.py):** a senha nova é gerada pelo código como `«senha:nova»` e vai para o cofre. Nunca passa pelo prompt, pelo caderno ou pela leitura da página.
- **Escopo de domínio** (`_allowed_hosts`): a mensagem dele amplia o escopo; a página nunca amplia.

Constantes em `[navegador.*]` do neo.toml. O diário do job fica em `/app/logs/browser_local/<job>/trace.md`. A extensão precisa de reload manual em `edge://extensions`.

---

## 9. Memória

| Camada | O que guarda | Como entra no turno | Código |
|---|---|---|---|
| PKS | Entidades, campos e relações sobre ele e o Neo (ontologia) | Blocos fixos + busca ranqueada por orçamento de tokens | [core/pks/](../core/pks/) |
| Recuperação passiva | Trechos de conversas antigas | Todo turno, orçamento apertado, por similaridade | `SessionManager.recover_context` |
| `conversation_recall` | Arquivo de conversas | Só a pedido: FTS → trigram → vetor, em cascata | [core/memory/conversation_recall.py](../core/memory/conversation_recall.py) |
| Caderno (working memory) | Notas da tarefa atual e `artefatos-recentes` | Índice em todo turno; corpo só com `wm_read` | [core/working_memory/](../core/working_memory/) |
| Ledger de arquivos | Onde está cada cópia, versões | Bloco "ARQUIVOS DESTA CONVERSA" + aviso no resultado | [core/files/ledger.py](../core/files/ledger.py) |
| Afeto (límbico v2) | 5 emoções e postura | Bloco condicional; teto de tom | `NEO_AFFECT_ENABLED` |

**Como o PKS aprende** ([writer.py](../core/pks/writer.py)): o LLM propõe, o backend valida contra a ontologia e o repositório executa. São dois momentos: `learn_before_response` e `learn`, depois da resposta. O aprendizado automático é pulado em mensagem sintética do Neo e recusado em turno contaminado (texto colado, página lida). `pks_remember`, quando ele manda guardar, sempre vale.

O **ranking** ([ranking.py](../core/pks/ranking.py)) roda em 5 estágios (consulta, candidatos, nota, agrupamento, empacotamento) e é puro: o mesmo código serve ao turno, à `search_pks` e ao benchmark.

Defeitos conhecidos e ainda abertos:
- o extrator ainda transforma "sou X" em nome;
- a recuperação semântica traz recusas antigas como precedente (score 0,99).

---

## 10. Autoridade e segurança

Fica em [core/authority/](../core/authority/). O princípio: **o código escreve, o modelo não**. Quem decide lê um objeto que nenhum LLM consegue alterar.

O `TurnAuthority` ([turn.py](../core/authority/turn.py)) guarda três coisas:
1. **Principal**: quem originou o turno (`admin` = ele, `externo` = remetente de WhatsApp ou e-mail, `agenda`).
2. **Contaminação**: se conteúdo de terceiro entrou no contexto. Contaminado não é proibido; a saída passa pelo portão. Texto colado de 240 caracteres ou mais (`pasted_chars`) já conta.
3. **Procedência de URL**: as URLs vistas em resultado de tool. É isso que transforma a regra do prompt "só use URL que você viu" numa verificação.

O **portão** ([gate.py](../core/authority/gate.py)) pergunta "quem pediu isto?" e consulta a tabela de risco. Ele é chamado pelos dois executores de tool, o global e o cognitivo. Modo `NEO_RELEASE_GATE_MODE`: `shadow` (padrão, só loga `[authority][sombra]`) ou `enforce`.

A **liberação** ([core/release/service.py](../core/release/service.py)) é feita com um card no aparelho:
- `create` é chamado por quem precisa do dado;
- `decide`, só pelas rotas de aprovação, com a identidade do token;
- `consume` é de uso único, com hash do conteúdo aprovado.

Nenhuma tool aprova.

**Credenciais:** ficam num cofre tipado, cifrado com Fernet (`NEO_CRED_KEY`). O service gateway faz HTTP sem o LLM ver a credencial.

**Rotas HTTP:** o `Principal` está em sombra (`NEO_API_AUTH_MODE=shadow`). Falta o segredo e virar enforce.

Achado aberto: o `web_fetch` bloqueado vira `curl` no `neo_bash`, e esse caminho está só em sombra. Uma nota no prompt não impede injeção.

---

## 11. Entrega

O `AIService` emite um fluxo de `AIServiceResponse`. O gRPC converte em `CoreMessage` e o painel em NDJSON. Os eventos:

| Evento | O que é | Quem mostra |
|---|---|---|
| `thinking` | Progresso; `[[CYCLE]]` marca um passe de auto-continuação | Desktop, painel |
| `plan_update` | Estado dos passos do plano | Desktop, painel |
| `command_request` | Card de aprovação de comando | Desktop executa; painel só recusa |
| `tool_request` | Tool que o Mac executa | Só o desktop |
| `assistant_message` | Bolha intermediária persistida (passes, entregas parciais) | Todos |
| `complete` | Resposta final, com `files` (anexos clicáveis do `file_save`) | Todos |

Outras saídas:
- **Arquivo no disco dele:** `save_generated_file`, com caminho literal e criação de pasta.
- **Imagem no chat:** o marcador `[[IMAGE:caminho]]` vira data URL no cliente; os bytes não passam pelo prompt.
- **Voz:** no modo live, a resposta final vai ao TTS. `[[VOICE:nome]]` divide em segmentos com outra voz.
- **Depois do turno:** resultado de job e disparo de agenda chegam pelo ProactiveDispatcher, como bolha nova, notificação no aparelho e replay quando ele voltar.
- **WhatsApp:** saída pelo `neo-whatsapp`; o `/notify` passa pelo nginx :8091 com `X-Notify-Token`.

---

## 12. Configuração, observabilidade e onde mexer

**Configuração**
- `config/neo.toml`: números de comportamento, nas seções `[cerebro]`, `[roteador]`, `[navegador.*]`, `[trabalho]`, `[web]`, `[faxina]`, `[comando_no_computador]`, `[grpc]`, `[openrouter]` e `[teto_adaptativo]`. Regra da casa: número novo vai para cá.
- `.env` **da raiz** do monorepo, que é o único que o container lê. Mudou o `.env`: `docker compose up -d --no-deps neo-core`.
- Catálogo de LLM, slots, chaves e configuração de intent: Postgres, pelo neo-panel.

**Onde olhar quando algo dá errado**

| Pergunta | Fonte |
|---|---|
| O que aconteceu num turno | Diário da sessão `.md` no container: etapas 1–8, RAW payload de cada chamada |
| Corpo inteiro do classificador | `docker logs neo-core \| grep 'Request options'` |
| Passo a passo de um job do navegador | `/app/logs/browser_local/<job>/trace.md` |
| Tokens e finish por rodada | `GET /api/v1/generation?id=` do OpenRouter, pelos `x-generation-id` do app.log |
| Folga da chave | `GET https://openrouter.ai/api/v1/key` |
| Desktop | Log do Tauri (5 MB × 5) |

**Testes:** cerca de 4.400 no Python e 125 no Rust. Rode com `neo-core/.venv/bin/pytest`. Evals em `scripts/eval_*.py`: roteador, portão do navegador, recall, tom e GAIA.

**Mapa de pastas do neo-core**

| Pasta | O que mora lá |
|---|---|
| `api/` | Rotas FastAPI, auth, schemas |
| `grpc_server.py` | Transporte do desktop |
| `modules/desktop/ai_service.py` | O orquestrador do turno (14 mil linhas): roteamento, cérebro, CLIENT_TOOL, entrega |
| `core/router/` | Classificador e guardas |
| `core/pipeline/`, `core/planning/` | Cascata, handoff e planos |
| `core/agent/` | Seleção de contexto do prompt |
| `core/pks/`, `core/memory/`, `core/working_memory/`, `core/session/` | Memória |
| `core/authority/`, `core/release/`, `core/credentials/`, `core/gateway/` | Segurança |
| `core/jobs/`, `core/scheduler/`, `core/events/`, `core/notifications/` | Trabalho em segundo plano e entrega |
| `core/browser_local/` | Navegador local |
| `core/files/`, `core/workbench/`, `core/documents/` | Arquivos, sandbox, leitura delegada |
| `skills/` | Skills em SKILL.md |
| `config/neo.toml` | Constantes de comportamento |

**Pontos frágeis do sistema**
- **Salvar é deploy.** Mutagen mais `--reload`: um `.py` salvo no Mac vai para produção na hora. Dependência nova no `pyproject.toml` derruba o processo no reload. `scripts/`, `tests/` e `docs/` ficam fora do reload.
- **Muita coisa sem commit.** Vários consertos de produção estão só no disco.
- **Portões em sombra.** Autoridade, auth das rotas e ranking v2 do PKS medem, mas não barram.
- **Teste de ponta a ponta pendente** em umas 17 features: formulário, docx, cofre, Claude Code, URL de memória e outras.
- **O defeito mais caro é dado errado dito com confiança**, não travar. É por isso que o eval do GAIA separa "errou com confiança" de "não sei".
