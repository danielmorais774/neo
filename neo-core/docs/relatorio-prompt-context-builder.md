# Relatório Técnico — Sistema de Construção de Prompt/Contexto do Neo

> Análise somente leitura. Nenhum arquivo alterado.
> Data: 2026-05-31

---

## Resumo executivo

O prompt do Neo **não é montado por um `PromptContextBuilder`**. Ele é montado por **concatenação de strings** dentro de um único método gigante: `_handle_direct_response` em `modules/desktop/ai_service.py` (arquivo de 2.990 linhas). Cada bloco é um `system_prompt += "..."` sequencial.

A decisão de "o que entra no prompt" hoje é, na prática:

- **quase tudo entra quase sempre**;
- a única chave liga/desliga real é o `needs_memory` do classificador (e ele só controla a recuperação de conversas antigas, não o resto);
- **não há scoring de relevância** decidindo se uma memória do PKS entra ou não — o PKS injeta entidades casadas por **trigrama/léxico** + um bloco **global incondicional** de 12 entidades;
- **não há orçamento de tokens** sobre o prompt final montado (existe `TokenCounter`/`ContextManager`, mas só no caminho de tools, não no `DIRECT_RESPONSE`).

Vários "problemas conhecidos" se confirmam no código; alguns têm nuance importante (ex.: hardware ≠ treino; data **é** dinâmica).

---

## 1. Fluxo atual de construção do prompt

### Cadeia de chamadas (mensagem → LLM)

```
gRPC ExecuteTask (grpc_server.py:191)
  └─ AIService.process_user_input        (ai_service.py:273)   ← orquestrador
       ├─ sessions.get_or_create_session / should_rotate_session
       ├─ sessions.add_message (+embedding) ‖ sessions.get_history   (em paralelo)
       ├─ _build_system_context(metadata)            (ai_service.py:2769)  ← bloco B
       ├─ _build_recent_missions_context(...)        (ai_service.py:2720)
       ├─ IntentRouter.classify(...)                 (intent_router.py:110) ← decide needs_memory / knowledge_keywords
       └─ dispatch → _handle_direct_response(...)    (ai_service.py:629)    ← MONTA O PROMPT
```

### O montador real: `_handle_direct_response`

Ordem **exata** de composição do `system_prompt` (tudo string concatenada):

| # | Bloco no prompt | Origem | Linha | Tipo | Relevância? |
|---|---|---|---|---|---|
| 1 | Base: identidade, criador, **data**, 9 regras | hardcoded no método | 690 | Fixo | — |
| 2 | `## Contexto do Sistema do Usuário` | `_build_system_context(metadata)` | 714 | Runtime | sempre (se metadata) |
| 3 | `## Memória do Neo (Workspace do Sistema - Alta Prioridade)` | `pks.retrieve_system_context()` | 756 | Neo core | quase sempre |
| 4 | `## Preferências do Usuário` | `preferences.get_preferences_text()` | 769 | User | **tudo, sem filtro** |
| 5 | `## Conhecimento do Usuário` | `pks.retrieve_context()` | 786 | User | trigram + global |
| 6 | `--- Contexto de conversas anteriores ---` | `sessions.recover_context()` | 831 | Sessão | **único com score real** |
| 7 | `## Escrita PKS (Sistema) Confirmada` | flags do prewrite | 838 | Estado | condicional |
| 8 | Histórico (mensagens user/assistant) | `get_history` | 870 | Sessão | budget de tokens (3000) |

### Papel de cada componente

- **`IntentRouter.classify`** ([intent_router.py:110](../core/router/intent_router.py)) — uma chamada LLM que devolve `RoutingDecision`: `intent`, `needs_memory`, `knowledge_keywords` (até 6 substantivos PT), `needs_plan`. **É o único lugar com algo parecido com "detecção de intenção/entidade"**, mas o resultado quase não é usado para filtrar contexto — só `needs_memory` (liga/desliga o bloco 6) e `knowledge_keywords` (vira termos de busca do PKS).

- **`_build_system_context`** ([ai_service.py:2769](../modules/desktop/ai_service.py)) — runtime do desktop (OS, paths, shell). Puro template sobre `metadata`.

- **`retrieve_system_context`** ([service.py:147](../core/pks/service.py)) — lê o **workspace de sistema** do Neo e injeta entidades estruturadas fixas (personality, response-rules, procedures, comm-prefs, goals, profile, projects, world-state) + exemplos de correção + lições aprendidas.

- **`retrieve_context`** → **`PKSRetriever.retrieve`** → **`ContextBuilder.format`** — recupera memória **do usuário**.

- **`recover_context`** → **`HybridMemorySearch.search`** — busca híbrida real (vetor + FTS + decaimento temporal via RRF + MMR). **Única camada com seleção por relevância.**

> **Nota:** existe um `core/agent/prompt_builder.py` (`PromptBuilder`) e um `core/agent/context_manager.py`, mas eles servem o **AgentExecutor** (caminho `SERVER_TOOL`), **não** o caminho conversacional principal. O prompt que aparece no chat vem do `_handle_direct_response`, que **não usa nenhum dos dois**.

---

## 2. Camadas existentes classificadas

### A. Base prompt fixo

- **Onde:** string literal em `_handle_direct_response` (linha 690).
- **Fixo?** Sim (exceto a data, computada por turno).
- **Entra sempre?** Sim. **Deveria?** Sim.
- **Risco:** as regras core estão **soltas no código**, não no PKS — duplicam parcialmente o `neo-response-rules` do bloco C, gerando duas fontes de verdade para comportamento.

### B. Runtime environment

- **Onde:** `_build_system_context` (linha 2769), a partir de `metadata` do gRPC.
- **Dinâmico**, entra sempre que há `os_platform`. **Deveria** entrar sempre (barato e útil para paths).
- **Risco:** shell é derivado **só** por igualdade de string de `os_platform` (`'bash/zsh' if platform in ('linux','darwin') else 'PowerShell/cmd'`). Qualquer valor inesperado/ausente cai em **PowerShell/cmd** mesmo em macOS — é exatamente a fonte da inconsistência "macos aarch64 + PowerShell" que você notou.

### C. Neo core memory

- **Onde:** `retrieve_system_context` (service.py:147), workspace de sistema.
- **Dinâmico** (lê do DB), mas **na prática quase sempre cheio**: itens com `priority="high"` entram sempre; os demais entram por casamento fraco de palavra (`_should_render_training_item`).
- **Deveria entrar sempre?** Parcialmente — identidade/persona/regras core sim; exemplos de treino e lições, não.
- **Risco:** mistura "regra essencial" (sempre) com "exemplo/lição" (deveria ser contextual) no mesmo bloco de alta prioridade.

### D. User relevant context

- **Onde:** bloco 4 (preferências) + bloco 5 (PKS).
- **Preferências:** `get_preferences_text` retorna **todas** as preferências do usuário, sem relevância. **Injeta tudo.**
- **PKS:** casa entidades por trigram/nome/alias **+** injeta incondicionalmente até **12 entidades globais** de `GLOBAL_TYPES = (person, health_state, restriction, routine, preference, goal)`.
- **Risco:** **alto.** `routine` é tipo global → **a ficha de treino entra sempre**, mesmo quando o input não é sobre treino. `device` não está em `GLOBAL_TYPES`; entra só por keyword — ou seja, treino é um problema maior que hardware.

### E. Current session context

- **Onde:** histórico via `get_history` + missões recentes + continuidade.
- **Dinâmico**, com **orçamento de tokens (3000)** e re-rank semântico só para sessões > 30 msgs.
- **Risco:** baixo — é a camada mais bem comportada.

### F. Retrieved documents / logs / chunks

- **Onde:** bloco 6, `recover_context` → `HybridMemorySearch`.
- **Dinâmico, com score real** (vetor+FTS+temporal+MMR). Só entra se `needs_memory=true`; usa `limit=5` e o prompt corta em **top 3**.
- **Risco:** baixo/médio — é o único lugar com seleção por relevância de verdade; o limite hardcoded de 3 é arbitrário mas conservador.

---

## 3. Existe cálculo de relevância?

**Respostas diretas:**

1. **Existe seleção por relevância?**
   Só **parcialmente**, e **só em uma camada** (recuperação de conversas antigas). Para PKS-usuário, PKS-sistema e preferências, **não há scoring que decida entrada no prompt**.

2. **Onde acontece?**
   Apenas em `HybridMemorySearch` (bloco 6): RRF de vetor+FTS, decaimento temporal exponencial, MMR para diversidade (`final_score = w_vec·rrf_vec + w_fts·rrf_fts + w_time·temporal`).

3. **Quais critérios?**
   - Conversa antiga (bloco 6): semântico + lexical + recência + diversidade. Pesos em config: `vector_weight=0.5`, `fts_weight=0.3`, `temporal_weight=0.2`, `mmr_lambda=0.7`.
   - PKS-usuário (bloco 5): **trigram (`pg_trgm`) + match exato de nome/alias**, sem embeddings. + boost de recência/confiança no `EntityResolver`. Mas o `ContextBuilder` **não usa o score** para incluir/excluir — só filtra por `sensitivity`.
   - PKS-sistema (bloco 3): `priority=="high"` sempre; senão keyword overlap com termos ≥ 4 chars.

4. **Diferença entre memória do Neo e do usuário?**
   Sim, em mecanismo (Neo = lista estruturada fixa; usuário = trigram+global), mas **ambas sem ranking real de corte**.

5. **Diferença entre memória fixa e contextual?**
   **Não há essa distinção formal.** Tudo é "injeta se existir" com filtros fracos.

6. **Injeta todo o conhecimento do usuário ou só o relevante?**
   **Quase tudo:** todas as preferências + todos os campos não-secretos das entidades casadas + 12 entidades globais incondicionais.

7. **Injeta todo o workspace do Neo ou só regras críticas?**
   **Quase tudo:** 8 entidades estruturadas + até 6 correction_examples + 6 learned_lessons, com filtro por-item fraco.

8. **Existe orçamento de tokens?**
   **Não no prompt montado.** Só no histórico (`max_tokens_budget=3000`) e no caminho de tools (`ContextManager`, `max_context_tokens=120k`, `compaction_threshold=0.75`) — que o `DIRECT_RESPONSE` **não invoca**.

9. **Existe debug/log de entrada/exclusão de memória?**
   Existe log de **inclusão** (`slog.log_event("Contexto PKS Injetado", ...)`, `log_context_recovery`). Mas **não há log de exclusão/score por memória** — você vê o que entrou, não por que algo ficou de fora.

---

## 4. Problemas confirmados no prompt atual

| Suspeita | Veredito | Evidência |
|---|---|---|
| `neo-response-rules` marcado como `[preference]` | ✅ Confirmado | `SYSTEM_RESPONSE_RULES_TYPE = "preference"` (system_bootstrap.py:28); `retrieve_system_context` renderiza `### nome [{type}]` → sai "[preference]". |
| `neo-communication-preferences` com `owner: daniel` sendo regra do Neo | ✅ Confirmado | `DEFAULT_COMMUNICATION_PREFS_PROPERTIES` tem `"owner": "daniel"`, `"owner_user_name": "Daniel Morais"` (system_bootstrap.py:98-100). Regra de comportamento etiquetada como propriedade do usuário. |
| `rule_key` gerado de texto bruto (hash) | ⚠️ Confirmado no fallback | `_rule_key` = slug das 5 primeiras palavras + `sha1` (writer.py:1021). Só roda quando o `MemoryCanonicalizer` (determinístico, com `semantic_rule_key`) não produz ops. O caminho v2 é correto; o legado/fallback ainda gera chave por hash. |
| Exemplos de resposta salvos como `rules` | ⚠️ Parcial | O fallback `_training_item` entra em `training_rules`/response-rules com `priority="high"` e carrega `examples_good/bad` no mesmo item (writer.py:1007). O canonicalizer separa em `neo-training-examples`, mas o fallback mistura. |
| Comportamento do Neo tratado como preferência pessoal | ✅ Confirmado | Combinação: tipos `preference` + `owner: daniel` para regras de comportamento. |
| Conhecimento do usuário inserido inteiro | ✅ Confirmado | `get_preferences_text` = todas; `ContextBuilder` inclui todos campos não-secretos; 12 globais incondicionais. |
| Ficha de treino quando input não é sobre treino | ✅ Confirmado | `routine ∈ GLOBAL_TYPES` → injeção incondicional (retriever.py:18,65-68). |
| Hardware quando input não é sobre hardware | ⚠️ Menos grave | `device` **não** está em `GLOBAL_TYPES`; entra só por keyword. Menos problemático que treino. |
| `macos aarch64` com shell `PowerShell/cmd` | ✅ Confirmado | shell derivado por igualdade exata de `os_platform` (ai_service.py:2796); valor ausente/divergente → PowerShell/cmd. |
| Data fixa renderizada no prompt | ❌ Refutado | `formatted_date` vem de `date.today()` (linhas 661-688) — é **dinâmica**. Risco real é só **timezone** (usa hora local do servidor, sem TZ explícita). |
| Prompt muito longo | ✅ Confirmado | 8 blocos always-on sem orçamento de tokens no `DIRECT_RESPONSE`. |
| Contexto irrelevante competindo com instruções | ✅ Confirmado | Treino/projetos/perfil globais + todas as prefs disputam atenção com as regras core. |

### Problemas estruturais adicionais identificados

- **Dupla fonte de verdade de comportamento:** regras core hardcoded no `system_prompt` (bloco 1) **e** `neo-response-rules` no PKS (bloco 3) podem divergir/duplicar.
- **PKS-usuário é puramente lexical (trigram).** Apesar de existir índice HNSW (migration `b2c3d4e5f6g7`) e `HybridMemorySearch`, a recuperação de entidades do PKS **não usa embeddings** — sinônimos e paráfrases não casam.
- **Bloco global ignora o score:** `_global_entities` injeta com `score=0.5` fixo e o `ContextBuilder` nem olha o score — só `sensitivity`.
- **Sem token budget no `_handle_direct_response`:** o `ContextManager`/`TokenCounter` que existem ficam ociosos nesse caminho.

---

## 5. Arquitetura proposta — `PromptContextSelector`

### Pipeline alvo

```
User Message
  → Intent Router        (já existe; estender saída)
  → Entity Detector      (novo; ou reaproveitar knowledge_keywords + NER leve)
  → Retrieval (paralelo)
       ├─ PKS Retrieval        (adicionar embeddings ao lado do trigram)
       ├─ System/Neo Retrieval (workspace do Neo)
       ├─ Preferences Retrieval
       ├─ Conversation Recall  (HybridMemorySearch — já bom)
       └─ Letta Retrieval      (futuro; mesma interface de candidato)
  → Context Scoring      (score unificado por candidato)
  → Context Selector     (corte por relevância + orçamento de tokens, por camada)
  → Prompt Renderer      (templates versionados, ordem estável)
  → LLM
```

### Conceito central: `ContextCandidate`

```python
from enum import Enum
from dataclasses import dataclass

class Layer(str, Enum):
    CORE     = "core"      # identidade, idioma, regras core → sempre pin=True
    RUNTIME  = "runtime"   # OS, paths, shell → sempre pin=True
    NEO_CORE = "neo_core"  # personality, procedures — pinned ou scored
    USER     = "user"      # prefs, PKS-usuário — scored
    SESSION  = "session"   # histórico recente — orçamento de tokens
    RETRIEVED = "retrieved" # conversas antigas, chunks — scored

@dataclass
class ContextCandidate:
    layer: Layer
    source: str            # "pks", "letta", "prefs", "hybrid_search", ...
    key: str               # rule_key / entity_id / mensagem_id
    pin: bool              # always-on (sem score necessário)
    text: str
    est_tokens: int
    relevance: float       # 0..1 (semântico/lexical/entidade/intent)
    priority: float        # peso da camada/fonte
    recency: float
    final_score: float     # combinação ponderada
```

### Regras de seleção

1. **`pin=True` entram sempre** (sem score): identidade, idioma, comportamento core, runtime/paths, regras core do Neo. → camadas CORE, RUNTIME e subconjunto **crítico** de NEO_CORE.

2. **Demais entram por `final_score ≥ threshold`** e dentro de um **orçamento de tokens por camada**:
   - `NEO_CORE` contextual ≤ 400 tok
   - `USER` (prefs + PKS) ≤ 800 tok
   - `RETRIEVED` ≤ 1200 tok
   - `SESSION` (histórico) ≤ 3000 tok (já existe)

3. **`Context Selector` produz um trace de decisão**: para cada candidato, `{key, score, incluído, motivo}` — resolve a lacuna atual de "não há log de exclusão".

### Mudanças concretas no código atual

| O que mudar | Onde | Impacto |
|---|---|---|
| Separar Neo core memory em `pinned` vs `contextual` | `system_bootstrap.py` + `retrieve_system_context` | Bloco 3 deixa de injetar exemplos/lições incondicionalmente |
| Corrigir tipos: `neo-response-rules` → `behavior_rule`, remover `owner: daniel` das entidades de comportamento | `system_bootstrap.py` | Elimina semântica incorreta no prompt |
| Remover injeção incondicional de `GLOBAL_TYPES` | `retriever.py:65` | `routine`/`goal` passam pelo scoring; treino só entra quando relevante |
| Adicionar recuperação semântica (HNSW) ao PKS-usuário | `retriever.py` + `entity_resolver.py` | Sinônimos e paráfrases passam a casar |
| Filtrar preferências por relevância | `preference_extractor.py:200` | Elimina dump completo de prefs |
| Aplicar `TokenCounter`/orçamento no `DIRECT_RESPONSE` | `ai_service.py:_handle_direct_response` | Controle real de tamanho do prompt |
| Corrigir derivação de shell | `ai_service.py:2796` | Elimina "macOS + PowerShell" |
| Letta como `Retriever` de candidatos | `api/routes/letta.py` → `core/pks/retriever` | Integração no fluxo, não como proxy isolado |

---

## Apêndice — arquivos-chave

| Arquivo | Papel |
|---|---|
| `modules/desktop/ai_service.py` | Orquestrador + **montador real do prompt** (`_handle_direct_response`, `_build_system_context`) |
| `core/router/intent_router.py` | Classificação de intenção, `needs_memory`, `knowledge_keywords` |
| `core/pks/service.py` | Fachada PKS; `retrieve_system_context` (Neo core memory) |
| `core/pks/retriever.py` | Recuperação PKS-usuário + **bloco global incondicional** |
| `core/pks/context_builder.py` | Formata PKS p/ prompt (filtra só por `sensitivity`) |
| `core/pks/repository.py:525` | `search_entities` — busca **trigram/exata**, sem embeddings |
| `core/pks/system_bootstrap.py` | Tipos/defaults do workspace do Neo (`response-rules` = `preference`, `owner: daniel`) |
| `core/pks/writer.py:1021` | `_rule_key` por hash (fallback) vs canonicalizer |
| `core/pks/memory_canonicalizer.py` | Caminho v2: `semantic_rule_key` determinístico |
| `core/memory/hybrid_search.py` | **Única** busca com score real (vetor+FTS+temporal+MMR) |
| `core/session/session_manager.py` | `get_history` (budget), `recover_context`, rotação de tópico |
| `core/agent/prompt_builder.py` / `context_manager.py` | `PromptBuilder`/`ContextManager` — **só no caminho de tools**, ociosos no chat |
| `api/routes/letta.py` | Letta hoje = só proxy HTTP, fora do fluxo de prompt |
