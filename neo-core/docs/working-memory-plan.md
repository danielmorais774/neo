# Memória de Trabalho — spec de implementação

> Status: **EM PRODUÇÃO desde 2026-08-30**. `working_note` migrada (head
> `u4v5w6x7y8z9`) e `WORKING_MEMORY_ENABLED=true` no `.env` da raiz
> (backup `.env.bak-working-memory-20260830`). F0–F3 vieram de 2026-07-31;
> o **registro de artefatos** (§15) entrou junto da ativação.
> Ver §14 para o que mudou em relação ao plano original e §15 para o registro.
> Objetivo: dar ao Neo um **caderno da tarefa atual** — onde ele grava as
> conclusões que obteve (números apurados, formato de um arquivo, decisão
> tomada, caminho de um artefato) para que o turno seguinte **não precise reler
> o arquivo nem reprocessar nada**.
>
> **Decisão de arquitetura** (§3): a verdade canônica é uma tabela Postgres
> (`working_note`) cujo `body` **é markdown** — a forma é o `.md` que o Daniel
> pediu, mas o armazenamento não depende do sandbox estar de pé. Disclosure em
> 2 níveis, igual à SkillLibrary: **índice sempre no prompt** (1 linha por nota,
> com timestamp) + **corpo sob demanda** via `wm_read`. Links `[[slug]]` entre
> notas, para arquivos do catálogo (`[[file:<id>]]`) e para artefatos do sandbox
> (`[[path:/workspace/…]]`).

---

## §1. Mapa da memória hoje (o que já existe)

| Tipo | Onde | Horizonte | Quem escreve |
|---|---|---|---|
| **Sessão / conversa** | `conversation_message` + `PersistentSessionManager` | curto (janela + compactação) | automático, todo turno |
| **Recuperada (RETRIEVED)** | `HybridMemorySearch` (pgvector + FTS + RRF/MMR) | longo, por similaridade | leitura |
| **PKS usuário** | `core/pks/` (entities/fields/relations) | permanente, canônico | `PKSMemoryPlanner` (bg) + `pks_remember` |
| **PKS sistema** | mesmo grafo, `memory_target="system"` | permanente | treinamento de comportamento |
| **Preferências** | `user_preference` + `PreferenceExtractor` | permanente | bg |
| **Afeto / Registro** | `core/agent/affect.py`, `register.py` | estado, decai | bg + heurística no turno |
| **Catálogo de arquivos** | `stored_file` (File Store) | temp/perm | `file_save`, uploads |
| **Sandbox** | volume `/workspace` do `neo-workbench` | persistente, **global** | `neo_write_file`, `neo_bash`, jobs |

### 1.1 O proto que já existe — `tool_trace`

Já há **um pedaço** de memória de trabalho em produção, e ele prova que a ideia
funciona:

- `_summarize_tool_call()` (`ai_service.py:482`) resume cada tool call do turno;
- o resumo vai para `conversation_message.extra_metadata["tool_trace"]`
  (`ai_service.py:2745`);
- `_render_tool_trace_note()` (`session_manager.py:38`) reinjeta isso como nota
  técnica no `content` mandado ao LLM — **só** nas 2 últimas mensagens do
  assistente, **teto de 800 chars**.

O comentário no código já nomeia o problema que ele foi criado para resolver:
*"sem isso, um follow-up curto ('cade os arquivos?') não sabe que 3 arquivos já
foram escritos e o modelo refaz tudo do zero com dados inventados diferentes."*

É exatamente o buraco descrito neste plano — resolvido pela metade.

---

## §2. Gaps

| # | Gap | Sintoma concreto |
|---|---|---|
| **G1** | **Conclusão do modelo não persiste em lugar nenhum** | "412 lançamentos, R$ 8.412,10 em débitos, CSV com `;` e decimal por vírgula" existe só como texto de chat; some na compactação (75% de 120k, `_PROTECTED_TAIL_SIZE=6`) ou na rotação de sessão |
| **G2** | `tool_trace` registra **que tool rodou**, não **o que se concluiu** | `neo_bash → exit=0, stdout_preview[300]`. O resultado da análise não cabe e não é o objetivo dele |
| **G3** | Alcance de 2 mensagens / 800 chars | 3 turnos depois a nota técnica já saiu; o trabalho vira inacessível |
| **G4** | Reprocessamento | `file_read`/`file_stage`/`neo_bash` refazem o trabalho porque o resultado anterior não é **endereçável** |
| **G5** | Conclusão ↔ artefato desligados | a nota não aponta pro `file_id` nem pro path no sandbox que a originou |
| **G6** | Único destino durável é o PKS | tentar salvar lixo de tarefa no PKS polui o grafo canônico — o `ledger_guard` (dinheiro fantasma no PKS) já mostrou o preço disso |
| **G7** | Sem tempo legível pelo modelo | só existe a data de hoje no CORE; nada diz *quando* uma conclusão foi obtida |

---

## §3. Decisões de arquitetura

### 3.1 Onde mora: Postgres, com corpo em markdown

Considerado e **recusado**: `/workspace/memory/*.md` puro no sandbox.

| Critério | `/workspace/*.md` | `working_note` (Postgres) |
|---|---|---|
| Índice no prompt **sem tool call** | exige `docker exec` a cada turno | 1 SELECT, ~ms |
| Sandbox parado (`WorkbenchUnavailableError`) | memória inteira some | intacta |
| Escopo por usuário | `/workspace` é global, sem namespace | `user_id` na linha |
| GC / TTL | shell script | mesmo padrão de `stored_file` (`expires_at`) |
| Painel (aba futura) | leitura por exec | rota REST direta |
| `grep`/`sed` pelo `neo_bash` | nativo | via espelho best-effort (F4) |

O argumento decisivo é o primeiro: **o índice tem que chegar no prompt sem o
modelo precisar lembrar de olhar**. A lição já está escrita em
`read_tools.py:71` — *"tool disponível não é tool usada"*. Uma memória de
trabalho que depende do modelo lembrar de abrir o caderno não fecha G1.

O `body` **é** markdown; F4 espelha em `/workspace/memory/<slug>.md`
(best-effort, não bloqueante) para o `neo_bash` poder processar o caderno.

### 3.2 Disclosure em 2 níveis (igual SkillLibrary)

```
prompt (sempre)   →  índice: 1 linha por nota ativa + timestamp + [[links]]
sob demanda       →  wm_read(slug) devolve o corpo inteiro
```

Mesmo padrão de `SkillLibrary.catalog_note()` + `skill_read` — que já está em
produção e funciona.

### 3.3 Escopo e ciclo de vida

- Nota pertence ao **usuário**; `session_id` é só a **origem**.
  Motivo: `should_rotate_session()` pode rotacionar no meio da tarefa — nota
  presa à sessão morreria junto.
- `status`: `active` (entra no índice) | `done` (sai do índice, continua legível
  por `wm_read`).
- Ordenação do índice: nota da sessão atual primeiro → `pinned` → `updated_at`
  desc. Teto de `working_memory_max_notes_in_index` (8) e de budget de tokens.
- TTL: `expires_at = updated_at + working_memory_ttl_days` (30d), renovado a
  cada escrita; `pinned=true` nunca expira. GC no padrão de `core/files/gc.py`.

### 3.4 Fronteira com o PKS — **não negociável**

| | Memória de Trabalho | PKS |
|---|---|---|
| O que | rascunho da tarefa: números apurados, formato de arquivo, decisão, path | fato canônico sobre o usuário/mundo |
| Vida | dias, expira | permanente |
| Validação | nenhuma | ontologia + confidence ≥ 0.80 + audit |
| Escrita | `wm_write` (direta, barata) | `pks_remember` → `PKSMemoryPlanner` |

Regras: **nada promove automaticamente** WM → PKS (uma proposta no sleep-time é
F5, opcional). E o risco inverso, que é o mais caro, está em §13-R1: o modelo
começar a chamar `wm_write` quando o usuário diz *"anota isso"* — isso
**regrediria** o `pks-write-on-demand`, que custou caro pra funcionar. Daí o
prefixo `wm_` (que ninguém fala em voz alta) em vez de `note_*`/`anotar`, mais o
texto explícito nas descriptions e no CORE.

### 3.5 Escrita garantida, não confiada (backstop determinístico)

O repo já aprendeu duas vezes que o modelo não faz o que o prompt pede em ~metade
dos turnos a 0.9 de temperatura (`force_tool_first`, guarda anti-mentira,
backstop do `intent_router` no `pks_remember`). Então:

- **caminho 1 (bom)**: o modelo chama `wm_write` com a conclusão redigida;
- **caminho 2 (piso)**: turno que rodou tool pesada (`neo_bash`, `file_read`,
  `file_stage`, `deep_research`, `web_fetch`, `run_command`) e **não** chamou
  `wm_write` ganha, em background e **sem LLM**, um append na nota automática da
  sessão com o `tool_trace` timestampado.

O piso é burro mas nunca falha, e transforma "o Neo lembra do trabalho" de
promessa de prompt em invariante.

---

## §4. Modelo de dados

`db/models/working_note.py` (padrão de `db/models/stored_file.py`):

```python
class WorkingNote(SQLModel, table=True):
    __tablename__ = "working_note"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: Optional[uuid.UUID]      # index
    session_id: Optional[uuid.UUID]   # sessão de ORIGEM; não limita leitura
    slug: str                         # String(64) — o alvo de [[slug]]
    title: str                        # String(120)
    summary: str                      # String(240) — a linha do índice
    body: str                         # TEXT, markdown
    status: str                       # String(10): active|done   index
    pinned: bool                      # default False
    links: list[str]                  # JSONB: ["slug", "file:<uuid>", "path:/workspace/…"]
    entry_count: int
    origin: str                       # String(12): model|auto
    created_at: datetime
    updated_at: datetime              # index (ordena o índice)
    expires_at: Optional[datetime]    # index (GC); NULL quando pinned
```

```sql
CREATE UNIQUE INDEX ux_working_note_user_slug ON working_note (user_id, slug);
CREATE INDEX ix_working_note_active ON working_note (user_id, status, updated_at DESC);
```

Migração: `alembic/versions/q0r1s2t3u4v5_create_working_note_table.py`,
`down_revision = "p9q0r1s2t3u4"` (head atual).

> ⚠️ **2026-08-02:** este plano ficou para depois do `modo-birra-plan.md`, que
> assumiu o id `q0r1s2t3u4v5`. Quando a memória de trabalho entrar, rebaseie para
> o head da época (provavelmente `r1s2t3u4v5w6`, sobre `q0r1s2t3u4v5`).

### 4.1 Formato do corpo

```markdown
# Extrato Nubank — março
> criado 31/07 14:20 · atualizado 31/07 14:41 · fonte [[file:6f2c9a1e]]

## 31/07 14:20
412 lançamentos, período 01/03–31/03. Débitos R$ 8.412,10, créditos R$ 11.900,00.
Top categorias: mercado 2.310,44 · transporte 1.180,00 · assinaturas 412,90.
CSV separado por `;`, decimal com vírgula, colunas: data;descricao;valor;categoria.

## 31/07 14:41
Excluídos os estornos (descrição começa com "Estorno"): 14 linhas, −R$ 622,10.
Total ajustado: R$ 7.790,00. Script em [[path:/workspace/extrato/parse.py]].
```

Cada append vira um `## <timestamp>` — o **quando** que o Daniel pediu fica no
conteúdo, não só na coluna. Timestamps sempre em Brasília (UTC-3), pelo mesmo
`_BRAZIL_TZ` de `context_selector.py:43`.

---

## §5. Tools (`modules/working_memory/`)

Duas tools. `wm_list` não existe de propósito: o índice **já está no prompt**;
uma terceira tool só pagaria schema à toa.

### `wm_write`

```
description:
  "Grava no SEU caderno de trabalho da tarefa atual uma conclusão que você
   obteve e que vai fazer falta depois: números já apurados, o formato/estrutura
   de um arquivo que você leu, uma decisão tomada, o caminho de um artefato que
   você gerou. Chame no MESMO turno em que concluiu — no próximo turno o
   conteúdo bruto já não está mais no seu contexto e você teria que reler o
   arquivo ou reprocessar tudo. Escreva a conclusão por extenso e autocontida
   (quem lê depois não vê esta conversa).
   NÃO é a memória permanente do usuário: 'lembra que eu prefiro X', 'anota que
   meu aniversário é Y' vão para `pks_remember`, nunca para cá."
parameters:
  note     (string, obrigatório)  slug curto e estável, ex.: 'extrato-nubank-marco'
  content  (string, obrigatório)  a conclusão, em markdown, por extenso
  title    (string, opcional)     título humano (só na criação)
  summary  (string, opcional)     UMA linha, é o que aparece no seu índice todo turno
  mode     (enum, opcional)       append (default) | replace
  status   (enum, opcional)       active (default) | done — 'done' tira do índice
  links    (array, opcional)      ['outra-nota', 'file:<id>', 'path:/workspace/…']
```

Servidor: cria ou atualiza por `(user_id, slug)`; prefixa o append com
`## <timestamp>`; deriva `summary` da primeira linha se não vier; extrai
`[[…]]` do `content` para `links`; corta em `working_memory_note_max_bytes`
(32 KB) pelo **começo** (o mais antigo cai primeiro).

### `wm_read`

```
description:
  "Lê uma anotação do seu caderno de trabalho pelo slug (os slugs disponíveis
   estão na seção 'Memória de Trabalho' do seu contexto, entre [[ ]]). Chame
   ANTES de reler um arquivo, refazer uma análise ou recalcular qualquer coisa
   sobre um trabalho que você já fez — a conclusão provavelmente já está aqui."
parameters:
  note   (string, obrigatório)
  full   (bool, opcional)   false (default) = últimas entradas; true = corpo inteiro
```

Registro: `read_tools.py` (`COGNITIVE_READ_TOOLS`), user-scoped (entram no
`_USER_SCOPED`), gated por `working_memory_enabled`; executor de módulo
`working_memory` registrado em `api/main.py` no padrão de `files`.

---

## §6. Injeção no prompt

1. `shared/types.py:332` — nova camada, entre `RUNTIME` e `NEO_CORE_CTX`:
   ```python
   WORKING_MEM = "working_mem"   # caderno da tarefa atual — scored/budgeted
   ```
2. `PromptContextSelector.build_prompt()` ganha `session_id: UUID | None` e mais
   uma coroutine no `_fetch_parallel` (`_safe_fetch_working_notes`).
   Chamador: `ai_service.py:2225`.
3. Render (nunca o corpo, só o índice):

```
## Memória de Trabalho (seu caderno desta tarefa)
Anotações que VOCÊ fez. Antes de reler arquivo, reprocessar dados ou refazer
qualquer trabalho, veja se a conclusão já está aqui — abra com `wm_read`.
- [[extrato-nubank-marco]] · hoje 14:41 · Extrato de março: 412 lançamentos, R$ 7.790,00
  em débitos após excluir estornos; CSV com `;` e decimal por vírgula.
- [[analise-gastos-q1]] · ontem 19:02 · Comparativo jan–mar, gráfico salvo. Liga
  [[extrato-nubank-marco]].
Concluiu algo que vale para o próximo turno (número apurado, formato de arquivo,
decisão, caminho de artefato)? Grave com `wm_write` no MESMO turno.
```

Budget `context_budget_working_mem` (700 tokens default), threshold do seletor
não se aplica (`pin=False` mas `relevance=1.0` para a sessão atual; notas de
outras sessões entram com `0.5 + overlap`).

> ⚠️ Gotcha conhecido: `datetime.utcnow()` naive já quebrou o "há X" no seletor
> de sessões do desktop (memória `neo-desktop-session-picker`). O render de
> "hoje/ontem/dd/mm" converte explicitamente para `_BRAZIL_TZ` antes de comparar.

### 6.1 Regra 12 no CORE

`context_selector._build_core_prompt()` ganha:

> **12.** Você tem um caderno de trabalho (`wm_write`/`wm_read`) para a tarefa em
> andamento. Sempre que apurar um resultado, descobrir a estrutura de um
> arquivo, tomar uma decisão ou gerar um artefato, GRAVE isso com `wm_write` no
> mesmo turno — no próximo turno o conteúdo bruto não estará mais no seu
> contexto. Antes de reler um arquivo ou refazer uma conta que você já fez,
> consulte o caderno com `wm_read`. O caderno é rascunho de tarefa: fato
> permanente sobre o usuário continua indo para `pks_remember`.

---

## §7. Backstop automático (§3.5)

`ai_service.py`, ao lado de `_bg_pks`/`_bg_prefs` (`_spawn_bg`, ~linha 2808):

```python
async def _bg_working_note() -> None:
    if not settings.working_memory_autonote_enabled: return
    if "wm_write" in used_tools: return                    # o modelo já anotou
    heavy = [e for e in tool_trace if e.get("tool") in _WM_HEAVY_TOOLS and e.get("ok")]
    if not heavy: return
    await wm_service.append_auto(
        user_id=user_id, session_id=session_id,
        slug=f"sessao-{str(session_id)[:8]}",
        title="Trabalho desta sessão",
        entry=_render_tool_trace_note({"tool_trace": heavy, "files": produced_files}),
    )
```

Sem LLM, sem custo, `origin="auto"`. A nota automática entra no índice com
`summary` = "registro automático do que foi executado nesta sessão".

---

## §8. Settings (`shared/config.py`)

| Flag | Default | Nota |
|---|---|---|
| `working_memory_enabled` | `False` | liga tools + injeção (padrão `neo_files_enabled`) |
| `working_memory_autonote_enabled` | `True` | backstop §7 (só vale com a de cima ligada) |
| `working_memory_max_notes_in_index` | `8` | |
| `context_budget_working_mem` | `700` | camada nova no `_select_candidates` |
| `working_memory_note_max_bytes` | `32768` | teto por nota |
| `working_memory_ttl_days` | `30` | renovado a cada escrita; `pinned` isenta |
| `working_memory_mirror_sandbox` | `False` | F4, espelho `.md` best-effort |

---

## §9. Fases

| Fase | Entrega | Arquivos |
|---|---|---|
| **F0** | modelo + migração + repositório | `db/models/working_note.py`, `alembic/versions/q0r1s2t3u4v5_…`, métodos em `db/async_client.py` |
| **F1** | serviço + 2 tools + executor + registro | `core/working_memory/{service.py,tools.py,executor.py}`, `modules/working_memory/`, `core/cognitive/read_tools.py`, `api/main.py`, `shared/config.py` |
| **F2** | injeção do índice + regra 12 | `shared/types.py`, `core/agent/context_selector.py`, `modules/desktop/ai_service.py:2225` |
| **F3** | backstop automático | `modules/desktop/ai_service.py` (`_bg_working_note`) |
| **F4** | GC + espelho no sandbox (opcional) | `core/working_memory/gc.py` (padrão `core/files/gc.py`), `api/main.py` |
| **F5** | aba no neo-panel + consolidação WM→PKS proposta (opcional) | `api/routes/working_memory.py`, `neo-panel` |

F0–F3 é o mínimo que fecha G1–G5 e G7. F1 sozinha já é usável manualmente.

---

## §10. Testes

- `tests/working_memory/test_service.py`: cria/append/replace; slug duplicado
  atualiza (não duplica); truncagem pelo começo; extração de `[[links]]`;
  `status="done"` sai do índice; timestamp em UTC-3.
- `tests/working_memory/test_index_render.py`: budget estoura → notas mais
  antigas caem, nunca a da sessão atual; corpo NUNCA aparece no índice.
- `tests/agent/test_context_selector.py`: nova camada renderiza na ordem certa;
  `working_memory_enabled=False` ⇒ prompt byte-a-byte idêntico ao de hoje.
- `tests/test_ai_service_working_note.py`: turno com `neo_bash` e sem `wm_write`
  gera nota automática; turno com `wm_write` **não** gera; turno leve não gera.
- **Regressão obrigatória** (R1): "anota que meu aniversário é 12 de março"
  continua indo para `pks_remember`, não para `wm_write`.
- E2E na VPS: mandar um CSV → pedir análise → **nova mensagem** pedindo um
  recorte → confirmar no `app.log` que houve `wm_read` e **nenhum** `file_read`
  do arquivo inteiro.

---

## §11. Riscos

| # | Risco | Mitigação |
|---|---|---|
| **R1** | `wm_write` canibaliza `pks_remember` ("anota isso" cai no caderno e o fato permanente se perde em 30 dias) | prefixo `wm_` (não-verbal), negativa explícita na description e na regra 12, teste de regressão dedicado, backstop do `intent_router` intocado |
| **R2** | O modelo não chama `wm_write` (0.9 de temperatura) | backstop §7 garante o piso sem LLM |
| **R3** | Índice vira lixo acumulado e come budget | teto de 8 notas + budget + `status=done` + TTL 30d |
| **R4** | Nota vira dump de arquivo (o oposto de economia de contexto) | teto de 32 KB, truncagem pelo começo, description manda gravar **conclusão**, não conteúdo; conteúdo bruto continua sendo `file_*` |
| **R5** | Contradição entre caderno e realidade (arquivo mudou depois da nota) | a entrada é datada; `wm_read` mostra o timestamp; regra: número apurado com mais de 1 dia deve ser reconferido antes de afirmar |
| **R6** | Multi-usuário | `(user_id, slug)` único; `user_id=None` não indexa nada |

## §12. Não-objetivos

- Não substitui o histórico de sessão, o PKS nem a episódica planejada na
  `memory-semantic-v3-plan.md` (episódica = fato datado sobre o mundo; caderno =
  rascunho de tarefa). São camadas diferentes e coexistem.
- Não versiona (sem histórico de edição da nota).
- Não é compartilhável entre usuários.
- Não substitui `file_save`: artefato é arquivo, o caderno só aponta pra ele.

---

## §14. O que a implementação mudou (2026-07-31)

Sete desvios do plano, todos com motivo:

| # | Plano dizia | Ficou | Por quê |
|---|---|---|---|
| 1 | migração `q0r1s2t3u4v5` | `s2t3u4v5w6x7` ← `r1s2t3u4v5w6` | o id do plano já era do `birra_act`; head real era outro |
| 2 | `links` como `array` | `string` CSV | **nenhuma** tool do repo usa `array`, e `_format_tools` emite `{"type":"array"}` sem `items` — JSON Schema inválido, risco de 400 no provider |
| 3 | "regra 12 no CORE" | **regra 13** | a 12 já é do `conversation_recall` |
| 4 | camada scored genérica | poda por NOTA antes de virar candidato (`_fit_working_index`) | o índice é UM bloco: estourando o budget, `_select_candidates` descartaria o caderno INTEIRO e o modelo perderia até a existência das notas |
| 5 | GC "opcional (F4)" | entrou junto (`core/working_memory/gc.py`) | 30 linhas no molde do GC do File Store; sem ele o índice cresce para sempre |
| 6 | decisão do backstop dentro da closure | função de módulo `_autonote_entry()` | a closure vive dentro de um gerador de 600 linhas — a decisão (filtro de tool pesada + dedup do `wm_write`) é onde mora o bug e precisava de teste |
| 7 | `wm_*` no registry global | só no `CognitiveReadExecutor` | mesmo caminho de `pks_remember`/`conversation_recall`; o executor global **nunca recebe `set_context`**, então uma instância global gravaria com `user_id=None` |

### Arquivos

**Novos:** `db/models/working_note.py` · `alembic/versions/s2t3u4v5w6x7_…` ·
`core/working_memory/{__init__,store,render,gc}.py` ·
`modules/working_memory/{__init__,tools,executor}.py` ·
`tests/working_memory/{test_working_note_store,test_render,test_prompt_layer,test_autonote,test_executor}.py`

**Tocados:** `db/models/__init__.py` · `shared/config.py` (8 settings) ·
`shared/types.py` (`ContextLayer.WORKING_MEM`) ·
`core/agent/context_selector.py` (fetch paralelo, camada, budget, poda, regra 13) ·
`core/cognitive/read_tools.py` (toolset, gating, `_USER_SCOPED`, dispatch) ·
`api/main.py` (store + GC + injeção no seletor + shutdown) ·
`modules/desktop/ai_service.py` (`session_id` no `build_prompt`, `_WM_HEAVY_TOOLS`,
`_autonote_entry`, `_bg_working_note`).

### Como ativar (feito em 2026-08-30)

1. `make migrate` (cria `working_note`) — **já aplicada**.
2. `WORKING_MEMORY_ENABLED=true` no `.env` da **raiz** (o container só lê esse)
   \+ `docker compose up -d --no-deps neo-core`. O boot imprime
   `✅ Memória de trabalho ativa (tools wm_*, índice no prompt)`.
3. Smoke: mandar um CSV → pedir análise → **mensagem nova** pedindo um recorte →
   conferir no `app.log` que houve `wm_read` e nenhum `file_read` do arquivo inteiro.

Com a flag off o prompt e o toolset voltam a ser byte-a-byte os de antes — há
teste para as duas coisas (`test_prompt_layer.py`), e é assim que se reverte.

---

## §15. Registro de artefatos (2026-08-30)

O buraco que a sessão 081cd1c3 expôs: o Neo baixou `nfse_marco.pdf` pelo
navegador local, disse **em prosa** "está na sua pasta de downloads", e no turno
seguinte o classificador teve que remontar o caminho de cabeça. Três detalhes
tornavam isso inevitável:

1. o `dir` real vinha do device em toda resposta de `browser.downloads` e era
   descartado em `driver.py` (`return listing.get("files")`);
2. o download acontece **em um job**, fora de qualquer turno — nem o `wm_write`
   nem o backstop `_bg_working_note` (que vive dentro de `_handle_direct_response`)
   passam por ali;
3. o índice do caderno só chega ao cognitivo. Quem monta o comando é o
   **classificador**, que lê outro canal (`system_context`).

`core/working_memory/artifacts.py` fecha os três com uma nota rolante por usuário
(slug `artefatos-recentes`) — não uma tabela nova, porque a nota já é por usuário
(sobrevive à rotação de sessão), já entra no prompt todo turno e já é podada pelo
começo em 32 KB, que é a política certa para um registro rolante.

**A garantia de leitura é em camadas, e nenhuma delas é "peça pro modelo ler":**

| Camada | Onde | O que garante |
|---|---|---|
| O caminho vai no `summary` | `render_summary` | o índice já carrega a resposta — `wm_read` vira exceção, não o caminho feliz |
| Bloco no `system_context` | `_recent_artifacts` (ai_service) | o **classificador** enxerga (mesma costura do manifesto de anexos) |
| Reescrita determinística | `_resolve_artifact_paths` (intent_router) | caminho montado de cabeça é trocado pelo REAL antes do dispatch |
| Âncora de fundamentação | idem | arquivo que o Neo baixou conta como fundamentado — não cai no `needs_plan` |

**Gravação em dois tempos, de propósito:** o runner do navegador registra quando o
download **começa** (via `on_download` do driver — o arquivo já está no disco dele
mesmo que ainda enchendo) e confirma no fim; o vigia (`runner_download_watch`)
grava o veredito final. As três gravações colapsam em UMA linha porque
`normalize()` tira o sufixo temporário (`.crdownload`) do nome **e do caminho**, e
`parse_artifacts` deduplica por nome mantendo a última. Tamanho parcial nunca é
registrado — número que mente é pior que número ausente.

**Deixado de fora de propósito:** subir o `_bg_working_note` para o epílogo comum
do turno. Num turno `CLIENT_TOOL` o trabalho não aconteceu ainda (o resultado
volta em `process_tool_result`), e o caminho do shell do cérebro já reentra no
cognitivo — então a nota automática já cobre esse. Anotar o `tool_trace` de todo
comando one-shot só encheria o índice, que é o recurso mais caro da feature (R3).

## 16. Caderno passo a passo (2026-09-13)

O "deixado de fora de propósito" acima partia de que o caminho do shell do cérebro
reentra no cognitivo e a nota automática o cobre. Medido nas sessões c4f3f7de e
45bd5ad0, não cobre: dois turnos terminaram na guarda de atuação e o terceiro foi
cancelado — um vídeo de 392 MB subido, 127 quadros gerados, e o caderno sem uma
linha. O turno seguinte pediu o mesmo upload de novo.

Agora o passo é anotado quando acontece, na nota da sessão (`sessao-<8>`, a mesma
do piso — o índice continua com UMA linha por nota; só o `summary` passa a dizer o
último passo, em vez do texto fixo):
- laço cognitivo: `on_step` depois de cada tool em `_WM_STEP_TOOLS`;
- atuação no cliente: `_resume_after_command` (comando + saída),
  `_resume_after_user_file` (nome, file_id, tamanho e o caminho no `/workspace`) e
  `_confirm_file_delivery`;
- comando que virou job: entrada de job aberto; o resultado continua vindo do
  `ProactiveDispatcher._note_result`.

A entrada anotada ganha `noted` e o piso do fim do turno a pula; a chave sai do
trace junto com `note_detail`. Render em `render_step_entry` /
`render_step_summary` (`core/working_memory/render.py`), com o texto por entrada
compartilhado com o piso (`_trace_entry_lines`) — o mesmo passo não é descrito de
dois jeitos conforme o momento em que foi anotado.

Na mesma mudança, o teto `_MAX_ACTUATION_DEPTH` subiu de 3 para 10 e virou rede de
segurança: quem para o laço é a repetição (`_repeated_actuation`, mesmo comando ou
mesmo arquivo já pedido neste pedido), e a parada lista o que já rodou em vez de
"me diga em uma frase o que você quer".
