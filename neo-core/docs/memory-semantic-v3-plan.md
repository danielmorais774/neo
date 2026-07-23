# Memória Semântica v3 — spec de implementação

> Status: PLANO DETALHADO (2026-07-13). Nada implementado.
> Este doc é a especificação completa — cada fase lista os arquivos exatos,
> assinaturas, DDL, prompts e critérios de aceite, no nível de detalhe
> suficiente para implementação direta sem decisões em aberto.
>
> **Decisão de arquitetura** (análise em §2): PKS permanece a verdade canônica.
> Letta NÃO entra como runtime — adotamos os padrões dele (memory blocks,
> archival, sleep-time compute) nativamente. Graphiti/Zep não entra (segundo
> grafo); a ideia de bi-temporalidade é absorvida pelo PKS. Mem0 OSS é plano B
> apenas da Fase 2 (ponto de decisão explícito em §3.F2.7).

---

## §1. Mapa do sistema atual (como a memória flui hoje)

### 1.1 Caminho de LEITURA de um turno (DIRECT_RESPONSE/COGNITIVE)

```
user_message
  │
  ├─ IntentRouter → RoutingDecision {needs_memory, knowledge_keywords}
  │
  ├─ [needs_memory] SessionManager.recover_context()          session_manager.py:365
  │     └─ HybridMemorySearch.search()                        core/memory/hybrid_search.py:55
  │           ├─ embed(query) — MiniLM-L6-v2 384d LOCAL       core/session/embedding_service.py
  │           ├─ db.find_similar_messages (pgvector)   ┐
  │           ├─ db.search_messages_fts (FTS)          ├─ RRF(k=60) + decay(30d) + MMR
  │           └─ db.find_similar_sessions              ┘
  │
  ├─ PKSService.learn_before_response() ×2 (user/system)      ai_service.py:1359,1365
  │     └─ regex síncrono (nome, dispositivo, parente, treino)
  │
  └─ PromptContextSelector.build_prompt()                     context_selector.py:61
        ├─ _fetch_parallel:
        │    ├─ pks.retrieve_system_context()  → NEO_CORE_PINNED (pin)   service.py:177
        │    ├─ prefs.get_preferences_text()   → USER_PREFS (scored)
        │    └─ pks.retrieve_context()         → USER_PKS (scored)       retriever.py:66
        │         ├─ _keywords(): ≤6 termos, stopwords pt                retriever.py:207
        │         ├─ EntityResolver.resolve() por keyword
        │         │    └─ repository.search_entities()                   repository.py:525
        │         │         → nome exato (1.0) | alias (0.95) | TRIGRAM  ← 100% LEXICAL
        │         ├─ type-synonym recall (score 0.6)                     retriever.py:104
        │         └─ globals (health_state, restriction)
        ├─ recovered_context → RETRIEVED (scored, ≤5, 300 chars cada)
        └─ seleção por threshold (0.25) + budget de tokens por camada
```

Camadas do prompt (enum `ContextLayer`, `shared/types.py:332`):
`CORE, RUNTIME, NEO_CORE_PINNED, NEO_REGISTER, NEO_AFFECT, NEO_CORE_CTX,
USER_PREFS, USER_PKS, SESSION, RETRIEVED`.

### 1.2 Caminho de ESCRITA (pós-resposta, background)

```
resposta pronta → ai_service.py:1911 _spawn_bg
  ├─ _bg_prefs()  → PreferenceExtractor
  └─ _bg_pks()    → ai_service.py:1844
        ├─ pks.learn(memory_target="user")    → PKSMemoryPlanner (LLM deepseek)
        │     → PKSOperation[] (confidence ≥ 0.80) → OntologyValidator → repository → audit
        └─ pks.learn(memory_target="system")  → canonicalizer p/ treinamento
```

### 1.3 Inventário de infra reutilizável (já em produção)

| Peça | Onde | Uso no plano |
|---|---|---|
| pgvector + coluna `pks_entity.embedding Vector(384)` **nunca populada** | `db/models/pks.py:85` | F0/F1 |
| `EmbeddingService` (singleton, criado em `api/main.py:288`, com warmup e cache MD5) | `core/session/embedding_service.py` | F0 |
| RRF + decay + MMR prontos | `core/memory/hybrid_search.py` | F1/F2 |
| `agent_job` com `kind` (comentário já prevê `timer|cron`) + `next_run_at` + claim atômico | `db/models/agent_job.py:33`, `async_client.py:1544` | F3 |
| `JobManager` com runner injetável por kind (padrão `research_runner`) | `core/jobs/manager.py:44,155` | F3 |
| `ProactiveDispatcher` (chat_message proativo + notificação) | `core/jobs/dispatcher.py` | F3 (relatório) |
| System workspace com entidades estruturadas `daniel-profile`, `neo-projects`, `neo-world-state`, … | `service.py:177` (`retrieve_system_context`) | F4 — **já são proto-memory-blocks** |
| `pks_entity_relation` **já tem** `valid_from`/`valid_until` | `db/models/pks.py:137` | F5 copia o padrão p/ fields |
| Audit log imutável em toda mutação | `core/pks/audit.py` | F3/F4/F5 |
| Precedente de migração de dimensão de embedding | `alembic/versions/f6g7h8i9j0k1_resize_embeddings_1536_to_384.py` | F0 |
| Flags pydantic-settings (`neo_affect_enabled` → env `NEO_AFFECT_ENABLED`) + padrão shadow (`neo_register_shadow`) | `shared/config.py` | todas |

### 1.4 Gaps (o que faz a memória falhar hoje)

| # | Gap | Sintoma concreto |
|---|---|---|
| G1 | PKS 100% lexical | "meu computador de jogos" não acha a entidade "PC Gamer" sem alias literal |
| G2 | MiniLM-L6-v2 é anglo-cêntrico | recall vetorial ruim em pt-BR; paráfrase quase não aproxima vetores |
| G3 | Episódica = chunk cru de mensagem | "o que resolvemos semana passada no TTS?" devolve fragmentos de 300 chars sem data |
| G4 | Nada consolida | duplicatas de entidade, contradições persistem, memória só cresce; canonicalizer dilui regra específica na genérica |
| G5 | Fields sobrescrevem valor | "meu peso era quanto em janeiro?" — histórico perdido (relações têm validade, fields não) |
| G6 | Fusão PKS×episódica só por budget | camadas competem por token, não por relevância unificada |

---

## §2. Decisão: Letta / Mem0 / Graphiti / nativo

| Opção | Veredito | Por quê |
|---|---|---|
| **Letta** (runtime) | ❌ | É um agente completo com loop LLM próprio. Integrar = espelhar cada turno pra um servidor Letta (custo LLM ~2×/mensagem, segundo Postgres, prompts EN) e as decisões de memória aconteceriam DENTRO do loop dele, competindo com router/register/afeto/skills. Em 2026 a própria Letta está migrando sleep-time server-side → subagents client-side (arquitetura em fluxo) |
| **Letta** (padrões) | ✅ | Memory blocks, archival, sleep-time compute — tudo implementável com as peças do §1.3 |
| **Mem0 OSS** | 🟡 plano B da F2 | Encaixa (pgvector, LLM via base_url), mas cria segundo store de fatos fora da ontologia/audit do PKS. Só entra se a F2 nativa emperrar (ver §3.F2.7) |
| **Zep/Graphiti** | ❌ componente / ✅ ideia | Segundo grafo (Neo4j) competindo com o PKS permanente. A bi-temporalidade vira F5 dentro do PKS |
| **Nativo** | ✅ escolhido | ~80% das primitivas já existem em prod (§1.3); o que falta é cola e política |

### Arquitetura alvo

```
┌──────────────────────────────────────────────────────────────────┐
│ MEMORY BLOCKS (F4) — daniel-profile / neo-projects /             │
│ neo-world-state: sempre no prompt (NEO_CORE_PINNED, já existe);  │
│ reescritos SÓ pelo consolidador                                   │
├──────────────────────────────────────────────────────────────────┤
│ PKS (permanente) — entidades/fields/relações + ontologia + audit │
│ F1: busca semântica (embedding) fundida com trigram via RRF      │
│ F5: fields bi-temporais (padrão já existente nas relações)       │
├──────────────────────────────────────────────────────────────────┤
│ EPISÓDICA (F2) — tabela episodic_memory: fatos datados por turno │
│ consolidação ADD/UPDATE/DELETE no write; decay no sleep-time     │
└──────────────────────────────────────────────────────────────────┘
         ▲                                   │
         │ F3: promoção (fato recorrente     │ leitura: braço novo no
         │ → PKSWriter, com audit)           │ HybridMemorySearch (RRF)
         │                                   ▼
   SLEEP-TIME JOB (F3)                PromptContextSelector
   agent_job kind=memory_consolidation      (sem mudança estrutural)
   ~04h via next_run_at, relatório via ProactiveDispatcher
```

---

## §3. Fases

Convenções para todas as fases:
- Flag nova em `shared/config.py` (pydantic → env `NEO_...` maiúsculo), default **off**.
- Fases com escrita automática (F2/F3/F4) têm modo **shadow** (`*_shadow: bool = True`):
  executa tudo e **loga o que faria** sem escrever — mesmo padrão do
  `neo_register_shadow`.
- Deploy: salvar = Mutagen sincroniza + `--reload` na VPS. Migração alembic e
  backfill rodam manualmente na VPS (ver §5).
- Testes: pytest em `tests/`, seguindo `test_hybrid_search.py` (mocks de db) e
  `test_session_manager_lifecycle.py`.

---

### F0 — Fundação de embeddings multilíngue (pré-requisito de tudo)

**Objetivo:** trocar o modelo de embedding para um que entenda pt-BR e passar a
popular o embedding das entidades PKS (coluna já existe, nunca usada).

#### F0.1 Trocar o modelo local

- `shared/config.py`: `embedding_local_model` default `"all-MiniLM-L6-v2"` →
  `"intfloat/multilingual-e5-small"`. **Mesmos 384d** → nenhuma migração de
  coluna (se um dia formos pro `bge-m3`/1024d, o precedente é a migração
  `f6g7h8i9j0k1`).
- Nova setting: `embedding_e5_prefixes: bool = True` — a família e5 exige
  prefixo `"query: "` na busca e `"passage: "` na indexação; sem isso a
  qualidade despenca silenciosamente.

#### F0.2 `EmbeddingService.embed(text, *, kind)`

`core/session/embedding_service.py`:

```python
async def embed(self, text: str, *, kind: Literal["query", "passage"] = "query") -> list[float]
async def embed_batch(self, texts: list[str], *, kind: ... = "passage") -> list[list[float]]
```

- Quando `embedding_e5_prefixes` e provider local: prepend `f"{kind}: "` ANTES
  do cache (o `_cache_key` passa a incluir o prefixo — senão query e passage
  colidem no cache MD5).
- Call sites a ajustar (todos hoje chamam `embed(...)` sem kind):
  - `hybrid_search.py:80` → `kind="query"`
  - `session_manager.py:127` (topic_embedding) e onde embeda mensagens →
    `kind="passage"`

#### F0.3 Backfill das mensagens/sessões

Vetores MiniLM e e5 **não são comparáveis** — busca com modelo novo contra
vetores velhos = lixo silencioso. Novo script `scripts/backfill_embeddings.py`:

1. Itera `conversation_message WHERE embedding IS NOT NULL` em lotes de 256
   (ordenado por `created_at`, keyset pagination), re-encoda com
   `kind="passage"`, `UPDATE ... SET embedding = :vec`.
2. Idem `conversation_session.topic_embedding`.
3. Log de progresso a cada lote + contagem final. Idempotente (re-rodar só
   re-encoda de novo, sem efeito colateral).
4. Rodar DENTRO do container na VPS (mesmo venv):
   `docker exec neo-core python scripts/backfill_embeddings.py`.

Ordem no deploy: (1) deploy do código com modelo novo, (2) rodar backfill
imediatamente. Na janela entre os dois a busca vetorial degrada; o braço FTS do
hybrid search segura o fallback (single-user, janela de minutos — aceitável).

#### F0.4 Popular `pks_entity.embedding`

- `core/pks/schemas.py`: `UpsertEntityInput` ganha campo opcional
  `embedding: list[float] | None = None`.
- `core/pks/repository.py::upsert_entity`: incluir `embedding` no INSERT/UPDATE
  quando presente (UPDATE sempre que `search_text` mudou — mesmo gatilho do
  `build_search_text` já existente em `repository.py:603`).
- **Quem calcula:** `PKSService`. O construtor (`service.py:47`) hoje é
  `(db, llm_router)` → passa a `(db, llm_router, embedding_service=None)`.
  `api/main.py:304` injeta a instância única criada em `api/main.py:288`.
  O service expõe `async def _entity_embedding_text(entity) -> str` e calcula
  o vetor antes de chamar o repository (o repository continua sem dependências
  além do DB).
- Texto embedado (kind="passage"):
  `"{name}. Tipo: {type}. Também conhecido como: {aliases}. {até 5 fields: key: value}"`
  — ex.: `"PC Gamer. Tipo: device. Também conhecido como: pc gamer, computador de jogos. gpu: RTX 5070 Ti. cpu: ... "`.
- Backfill: `scripts/backfill_pks_embeddings.py` — itera `pks_entity WHERE
  deleted_at IS NULL AND embedding IS NULL`, monta o mesmo texto (com fields),
  atualiza. Volume atual é pequeno (single-user) — segundos.
- Índice: migração alembic `add_pks_entity_embedding_index`:
  `CREATE INDEX ix_pks_entity_embedding ON pks_entity USING hnsw (embedding vector_cosine_ops);`
  (pgvector ≥ 0.5; se a versão do container não tiver HNSW, usar ivfflat com
  `lists=100` — verificar com `SELECT extversion FROM pg_extension WHERE extname='vector'`).

#### F0.5 Exemplo de melhoria

| Cenário | Antes (MiniLM) | Depois (e5 multilíngue) |
|---|---|---|
| "lembra da conversa sobre a placa de vídeo passada pra VM?" | vetor pt-BR quase aleatório → hybrid depende do FTS acertar "placa" literal | embedding aproxima "placa de vídeo"≈"GPU"≈"MX150 passthrough" → recupera a conversa certa |
| "o que a gente discutiu sobre deixar o Neo mais sarcástico?" | só acha se a palavra "sarcástico" apareceu literal | aproxima de "deboche/humor ácido/tom" |

#### F0.6 Aceite e testes

- `tests/test_embedding_service.py`: prefixos aplicados por kind; cache não
  colide entre query/passage; batch preserva ordem.
- Verificação manual (VPS): `docker exec neo-postgres psql -U neo -d neodb -c
  "SELECT count(*) FROM pks_entity WHERE embedding IS NOT NULL"` > 0 após backfill.
- Smoke: par de frases pt parafraseadas com cosine > 0.8; par não relacionado < 0.5.

**Esforço: 1–2 dias. Risco: baixo (modelo drop-in, mesma dimensão).**

---

### F1 — Recuperação semântica no PKS (mata G1, prepara G6)

**Objetivo:** o retriever e a tool `search_pks` encontram entidades por
significado, fundidas com o caminho lexical atual por RRF.

#### F1.1 Util compartilhado de RRF

Novo `shared/ranking.py`:

```python
def rrf_fuse(rankings: dict[str, list[str]], weights: dict[str, float], k: int = 60) -> dict[str, float]
```

Extraído da lógica de `hybrid_search.py:190` (que passa a usá-lo — refactor sem
mudança de comportamento, coberto pelo `test_hybrid_search.py` existente).

#### F1.2 `repository.search_entities_semantic()`

```python
async def search_entities_semantic(
    self, session, workspace_id: UUID, query_embedding: list[float],
    entity_types: list[str] | None = None, limit: int = 8,
) -> list[tuple[EntityRecord, float, str]]:   # (entity, cosine_sim, "semantic")
```

```sql
SELECT *, 1 - (embedding <=> CAST(:qvec AS vector)) AS match_score
FROM pks_entity
WHERE workspace_id = :workspace_id AND deleted_at IS NULL
  AND embedding IS NOT NULL {type_filter}
ORDER BY embedding <=> CAST(:qvec AS vector)
LIMIT :limit
```

#### F1.3 Integração no `PKSRetriever.retrieve()` (`retriever.py:66`)

Fluxo novo (flag `pks_semantic_search_enabled`):

1. Lexical por keyword — **inalterado** (barato, preciso p/ nomes exatos).
2. UMA busca semântica com o embedding da `user_message` inteira
   (`kind="query"`) — não por keyword; a frase completa carrega o contexto
   ("meu computador de jogos tá esquentando" ≫ {"computador","jogos"}).
3. Corte: descarta cosine < `pks_semantic_min_sim` (default **0.55**).
4. Fusão: candidatos lexicais e semânticos entram no `rrf_fuse` com pesos
   `{"lexical": 0.6, "semantic": 0.4}`; score final do candidato semântico é
   **capado em 0.85** para nome exato/alias (1.0/0.95) continuarem ganhando.
   `matched_by="semantic"` para o trace.
5. Type-synonym recall e globals — inalterados (rodam depois, como hoje).
6. Shadow mode (`pks_semantic_search_shadow`, default True): roda a busca
   semântica, **não adiciona** candidatos, loga
   `[PKS-SEMANTIC-SHADOW] would_add=[(nome, cosine), ...]` — uma semana de log
   diz o threshold certo antes de ligar.

#### F1.4 Tool `search_pks` (verificação ativa do cognitivo)

`service.search_entities` (`service.py:394`) ganha o mesmo caminho fundido —
a tool (`modules/pks_search/executor.py`) herda de graça, incluindo
`matched_by:"semantic"` no payload (o cognitivo já mostra `matched_by`).

#### F1.5 Exemplo de melhoria

| Pergunta | Antes | Depois |
|---|---|---|
| "meu computador de jogos tá esquentando, o que pode ser?" | keywords {computador, jogos, esquentando}; trigram não bate com "PC Gamer" → PKS entra vazio → resposta genérica | semântico acha "PC Gamer" (cosine ~0.7) → neighborhood com gpu/cpu no prompt → "sua 5070 Ti costuma… verifica as fans que configuramos" |
| "cadê aquela minha máquina de café?" | nada (entidade "Cafeteira Dolce Gusto") | acha por significado |
| cognitivo verifica "salvou meu novo monitor?" via `search_pks("monitor ultrawide")` | só acha com nome exato | acha a entidade mesmo salva como "LG 34WP65" |

#### F1.6 Aceite e testes

- `tests/test_pks_semantic_search.py`: fusão RRF (lexical 1.0 vence semântico),
  cap 0.85, threshold, shadow não adiciona candidato.
- Golden manual: "computador de jogos" → "PC Gamer" no prod (após F0.4).
- Latência: +1 `embed()` por retrieve (~10–30ms local em CPU) — medir no slog
  (`pks_ms` já existe em `ai_service.py:1410`).

**Esforço: 2–3 dias. Depende de F0.**

---

### F2 — Memória episódica de verdade (mata G3)

**Objetivo:** cada turno relevante vira 0–3 *fatos datados* consolidados
(padrão Mem0: ADD/UPDATE/DELETE/NOOP no write), recuperados junto com o
hybrid search.

#### F2.1 Schema

Novo `db/models/episodic.py` + migração `create_episodic_memory_table`:

```python
class EpisodicMemory(SQLModel, table=True):
    __tablename__ = "episodic_memory"
    id: UUID (pk)
    user_id: UUID (fk user.id, index, not null)
    session_id: UUID | None (index)
    kind: str            # 'event' | 'insight' | 'preference_signal'  (String(32))
    fact_text: str       # ex.: "Daniel corrigiu o gap de 6.4s no TTS com rampa por RTF"
    embedding: Vector(384) | None
    importance: Numeric  # 0..1 (do extrator)
    occurred_at: timestamptz     # quando o fato ACONTECEU (≠ created_at)
    last_accessed_at: timestamptz | None
    access_count: int = 0
    source_message_ids: JSONB    # lista de UUIDs
    superseded_by: UUID | None   # setado na promoção (F3) → id da pks_entity
    expires_at: timestamptz | None  # setado no decay (F3)
    created_at / updated_at: timestamptz
```

Índices: hnsw em `embedding`; btree `(user_id, occurred_at DESC)`; parcial
`WHERE expires_at IS NULL AND superseded_by IS NULL` (o "vivo").

#### F2.2 Extrator — `core/memory/episodic_extractor.py`

```python
class EpisodicExtractor:
    def __init__(self, llm_router, embedding_service, db, settings): ...
    async def process_turn(self, *, user_id, session_id, user_message,
                           assistant_response, source_message_id) -> dict:
        """Extrai fatos e consolida. Retorna {added, updated, deleted, noop} p/ log."""
```

Prompt de extração (LLM leve — mesmo padrão do `memory_planner.py`: tenta
`deepseek`, fallback provider default; SYSTEM em pt-BR):

```
Você extrai FATOS EPISÓDICOS de um turno de conversa entre Daniel e o
assistente Neo. Fato episódico = algo que aconteceu, foi decidido, concluído
ou sinalizado NESTE turno, com valor futuro.

Regras:
- Máximo 3 fatos. Nenhum é resposta válida (lista vazia).
- kind: "event" (aconteceu/foi feito), "insight" (conclusão/decisão durável),
  "preference_signal" (sinal de gosto/preferência ainda não confirmado).
- Escreva cada fato em UMA frase, terceira pessoa, autocontida
  (sem "isso", "ele" — nomeie o assunto).
- occurred_at: ISO se o texto disser quando; senão null (o sistema usa agora).
- importance 0..1: 0.9+ decisões/conclusões; 0.5 eventos comuns; 0.2 trivia.
- NÃO extraia: saudações, perguntas sem resposta, fatos já ditos como
  permanentes sobre perfil (isso é papel de outro sistema), conteúdo de
  web_search não relacionado ao usuário.
Responda SÓ com JSON: {"facts":[{"fact","kind","importance","occurred_at"}]}
```

Guarda-corpos (obrigatórios):
- **Gotcha DeepSeek** (memória `deepseek-empty-response-reasoning`): content
  vazio/JSON inválido ⇒ tratar como "0 fatos" e logar `extractor_empty` —
  NUNCA como sinal para deletar/alterar algo existente.
- Skip quando: `decision.needs_memory is False`, `is_neo_training`, resposta
  de erro, ou turno de handoff sem conteúdo semântico.

#### F2.3 Consolidação no write (padrão Mem0)

Dentro de `process_turn`, para cada fato extraído:

1. `embed(fact_text, kind="passage")`.
2. Vizinhos: top-5 em `episodic_memory` do user com cosine ≥ **0.85**
   (vivos: `expires_at IS NULL AND superseded_by IS NULL`).
3. Sem vizinho → **ADD** direto (sem 2ª chamada LLM — caminho comum e barato).
4. Com vizinho → 1 chamada LLM com fato novo + vizinhos:
   - `ADD` — fato distinto apesar da similaridade;
   - `UPDATE <id>` — mesmo fato, mais recente/completo → sobrescreve
     `fact_text`, re-embeda, `occurred_at` = mais recente, `access_count`
     preservado, incrementa `importance` em +0.05 (recorrência ≈ relevância);
   - `DELETE <id>` — o fato novo CONTRADIZ o velho (ex.: "voltou a usar X")
     → seta `expires_at=now()` no velho (nunca DELETE físico) e ADD o novo;
   - `NOOP` — já coberto.
   Output vazio/inválido do LLM ⇒ **ADD** (duplicata é limpa no F3; perda de
   fato não tem correção).

#### F2.4 Wire-in da escrita

`ai_service.py` — dentro do bloco existente de background
(`_spawn_bg`, linha ~1911): novo `_bg_episodic()` ao lado de `_bg_pks()`
(não dentro — falha de um não pode matar o outro), guardado por
`settings.episodic_memory_enabled`. Recebe as mesmas variáveis já em escopo
(`user_id`, `decision`, `user_message`, `full_response`,
`source_message_id`). Em shadow (`episodic_memory_shadow`), o extractor roda e
loga os fatos SEM persistir.

#### F2.5 Wire-in da leitura

1. `shared/types.py:305`: `source: Literal["session", "message", "episodic"]`.
2. `db/async_client.py`: novo `find_similar_episodic(query_embedding, user_id,
   limit) -> list[tuple[EpisodicMemory, float]]` (cosine, só vivos).
3. `hybrid_search.py:85`: 4º braço paralelo no `asyncio.gather`; candidatos com
   chave `episodic-{id}`, `content = f"[{occurred_at:%Y-%m-%d}] {fact_text}"`,
   entram no RRF como os demais (o decay temporal existente já favorece o
   recente; fatos episódicos têm `created_at=occurred_at` para o decay).
4. Pós-seleção: `UPDATE episodic_memory SET access_count = access_count + 1,
   last_accessed_at = now() WHERE id = ANY(...)` (batch, fire-and-forget).
5. `context_selector.py:242` — sem mudança estrutural: o prefixo `[memória]`
   já existe; fato episódico chega datado e autocontido (melhor que chunk).

#### F2.6 Exemplo de melhoria

| Pergunta | Antes | Depois |
|---|---|---|
| "que problema a gente resolveu semana passada no TTS?" | chunks crus de 300 chars, sem data, cortados no meio | `[2026-07-05] Daniel eliminou o gap de 6.4s no streaming do chatterbox com rampa por RTF` → resposta precisa e datada |
| "quando montei o Pi-hole?" | depende de FTS achar "pihole" num chunk com data implícita | fato `event` com `occurred_at` → data direta |
| Daniel repete 3× em semanas diferentes "no modo live responde mais curto" | 3 chunks dispersos | 1 `preference_signal` com `access_count=3` e importance crescida → F3 promove a preferência pro PKS |
| "terminei a migração do neo-voice" (dito uma vez, há um mês) | chunk enterrado sob conversas novas | `insight` recuperável por significado ("como tá o neo-voice?") |

#### F2.7 Ponto de decisão Mem0 (plano B)

Se ao final de F2.3 a consolidação nativa se mostrar frágil (medir com F6):
substituir F2.1–F2.3 por Mem0 OSS `Memory` (backend pgvector na MESMA instância
PG, LLM apontado pro router via base_url OpenAI-compatible, `custom_fact_
extraction_prompt` = o prompt do F2.2). F2.4–F2.5 e o F3 **não mudam** (leem a
store do Mem0 no lugar da tabela própria). Custo do plano B: fatos fora do
audit do PKS e schema controlado pelo Mem0.

#### F2.8 Aceite e testes

- `tests/test_episodic_extractor.py`: parse do JSON; vazio ⇒ 0 fatos sem
  efeito colateral; ADD sem vizinho não chama LLM 2ª vez; UPDATE preserva
  access_count; DELETE nunca é físico.
- `tests/test_hybrid_search.py`: braço episódico entra no RRF; source novo.
- E2E manual (padrão smoke da skill financas): conversa → fato aparece em
  `SELECT * FROM episodic_memory ORDER BY created_at DESC LIMIT 5` na VPS →
  pergunta posterior recupera.

**Esforço: ~1 semana. Depende de F0 (embeddings).**

---

### F3 — Sleep-time consolidation, o "hipocampo" (mata G4)

**Objetivo:** job noturno que promove episódico→PKS, funde duplicatas, aplica
decay e mantém os memory blocks (F4). Neo "dorme e organiza a memória".

#### F3.1 Runner e agendamento

- Novo `core/memory/consolidation_runner.py`:
  `async def run_memory_consolidation(job: AgentJob) -> dict` — contrato igual
  ao `research_runner` (`manager.py:44`).
- `JobManager.__init__` ganha `memory_runner: Callable | None = None`;
  `_run_job` (`manager.py:155`) trata `kind == "memory_consolidation"`.
- Agendamento SEM infra nova de cron — usa `next_run_at` que o
  `claim_due_agent_jobs` (`async_client.py:1544`) já respeita:
  1. No lifespan de `api/main.py` (após `JobManager.start()`): garantir que
     existe UM job `kind="memory_consolidation"` com `status='queued'`
     (idempotente; se não existe, insere com `next_run_at` = próximas 04:00
     America/Fortaleza).
  2. Último passo do runner: enfileirar o próximo (mesmo spec, +24h).
     Crash no meio ⇒ o job fica `failed` e o lifespan do próximo boot recria —
     auto-cicatrizante.
- `spec_json`: `{"steps": ["promote","merge","decay","guard","blocks"],
  "user_id": "<uuid do Daniel>"}` — permite rodar passos isolados manualmente
  (`POST /jobs` já existente).

#### F3.2 Passos do runner (cada um com try/except próprio; relatório soma tudo)

**(a) Promoção episódico → PKS**

- Candidatos: vivos, `kind IN ('insight','preference_signal')`,
  `created_at < now() - interval '24 hours'` (quarentena anti-ruído) e
  (`access_count >= 2` OR `importance >= 0.8`).
- Prompt dedicado (reusa contratos existentes — `PKSOperation` de
  `schemas.py`, validação `OntologyValidator`, aplicação `writer._apply_*`,
  audit com `actor_type="consolidator"`): "converta estes fatos episódicos em
  operações PKS; se já coberto pelo contexto PKS atual (injetado), devolva
  lista vazia". `MIN_APPLY_CONFIDENCE = 0.80` continua valendo.
- Fato promovido: `superseded_by = entity_id` (sai da busca viva; a verdade
  agora é o PKS). Fato rejeitado pelo planner: `importance -= 0.1` (três
  rejeições ⇒ decay natural o arquiva).

**(b) Merge de entidades PKS duplicadas**

- Pares candidatos:
  ```sql
  SELECT a.id, b.id, 1 - (a.embedding <=> b.embedding) AS sim
  FROM pks_entity a JOIN pks_entity b
    ON a.workspace_id = b.workspace_id AND a.type = b.type AND a.id < b.id
  WHERE a.deleted_at IS NULL AND b.deleted_at IS NULL
    AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
    AND (a.embedding <=> b.embedding) < 0.08   -- sim > 0.92
  ```
- LLM confirma par a par (nome+fields+relations dos dois): `MERGE` ou `KEEP`.
- Merge (novo `repository.merge_entities(session, keep_id, absorb_id)`):
  aliases = união; fields ausentes copiam, conflitos ficam com o `keep`
  (mais recente vence, o perdedor vira histórico no audit `before`);
  relations repontadas com dedup; absorvida = `soft_delete` + audit
  `action="merge"`.

**(c) Decay episódico**

- Alvo: vivos, `kind='event'`, `occurred_at < now() - interval '90 days'`,
  `access_count = 0`, `importance < 0.5`.
- Por mês-calendário: 1 chamada LLM gera um digest de ≤3 frases → INSERT como
  `kind='insight'` (`fact_text="Resumo de {mês}: ..."`,
  `source_message_ids=[]`, `importance=0.4`) → originais ganham
  `expires_at=now()`.
- Memória não some: linhas expiradas ficam na tabela (auditáveis), só saem da
  busca.

**(d) Guard de regressão do treinamento** (fix da diluição — memória
`neo-router-skill-bypass-persona`)

- A CORREÇÃO em si é no canonicalizer (já parcialmente feita via `registers:`
  escopado — `memory_canonicalizer.py` linha ~55); o job adiciona o teste
  contínuo: para cada regra canônica em `neo-response-rules` com trigger,
  chama `retrieve_system_context(user_message=<frase-gatilho da regra>)` e
  verifica que a regra aparece no texto. Falhou ⇒ entra no relatório como
  `guard_failures` (é O sinal de que alguma escrita a diluiu).

**(e) Blocks refresh** — ver F4.3 (roda como último passo).

#### F3.3 Relatório e observabilidade

- Retorno do runner: `{"promoted": n, "merged": n, "decayed": n,
  "guard_failures": [...], "blocks_updated": [...], "errors": [...],
  "shadow": bool}` → vira `result_summary` do job → o **ProactiveDispatcher
  existente** entrega como chat_message proativo: *"Consolidei a memória:
  promovi 3 fatos, fundi 'PC Gamer'+'pc-gamer', arquivei 12 eventos de abril."*
- Shadow (`memory_consolidation_shadow=True` por ≥1 semana): tudo roda,
  NENHUMA escrita (nem PKS, nem episódica, nem blocks) — o relatório diz o que
  faria. Ligar escrita só depois de revisar os relatórios.

#### F3.4 Exemplo de melhoria

| Situação | Antes | Depois |
|---|---|---|
| "no modo live responde curto" dito 3× | 3 sinais soltos na episódica | promovido a `preference` no PKS → entra via USER_PREFS/USER_PKS em TODO turno relevante, para sempre |
| Entidades "PC Gamer" e "pc-gamer" (criadas por planners diferentes) | contexto duplicado/conflitante no prompt | 1 entidade com aliases unidos |
| 300 eventos "tocou música X" de abril | poluem a busca episódica | 1 digest: "Em abril, ouviu principalmente synthwave à noite" |
| Regra treinada "quando eu falar 'alexia' não use o pipeline HA" some após novo treino | ninguém percebe até falhar na conversa | `guard_failures` no relatório da madrugada seguinte |

#### F3.5 Aceite e testes

- `tests/test_consolidation_runner.py`: promoção respeita quarentena e
  confidence; merge não roda sem confirmação LLM; decay nunca deleta
  fisicamente; shadow não escreve NADA (assert em mocks); crash num passo não
  aborta os demais.
- Job manual na VPS: `POST /jobs {"kind":"memory_consolidation",
  "spec_json":{"steps":["merge"]}}` → relatório coerente.

**Esforço: ~1 semana. Depende de F2 (e F0/F1 para merge por embedding).**

---

### F4 — Memory blocks vivos (padrão Letta core memory)

**Objetivo:** os blocos `daniel-profile`, `neo-projects` e `neo-world-state`
— que **já existem** no system workspace e **já são injetados pinned** via
`retrieve_system_context` → `NEO_CORE_PINNED` — passam a ser mantidos
automaticamente pelo consolidador. Hoje só mudam via treinamento explícito;
por isso envelhecem (o `neo-projects` de hoje não sabe o que foi entregue
este mês).

#### F4.1 O que NÃO muda

Zero mudança no caminho de leitura: `service.py:177` já renderiza esses blocos
e o `context_selector.py:128` já os fixa no prompt. Não criar camada nova.

#### F4.2 Contrato dos blocos

- Cada bloco mantém `properties` (JSONB no field `properties` — formato atual)
  + nova property `_meta: {"last_consolidated": iso, "changelog": [últimas 5
  mudanças "YYYY-MM-DD: resumo"]}`.
- Budget por bloco (enforçado no runner, contado com o `TokenCounter` de
  `core/agent/token_counter.py`): `daniel-profile ≤ 400 tokens`,
  `neo-projects ≤ 500`, `neo-world-state ≤ 300`. Estourou ⇒ o próprio prompt
  de rewrite pede compressão (o mais antigo/menos relevante sai).

#### F4.3 Passo (e) do runner — rewrite

1. Evidência: fatos episódicos dos últimos 7 dias (vivos + recém-promovidos) +
   diffs do audit PKS da semana (`pks_audit_log WHERE created_at > now()-'7 days'`).
2. Para cada bloco: 1 chamada LLM — `properties` atual + evidência →
   `properties` novo (JSON, mesmo shape) + lista `changes`. Regras do prompt:
   "remova o que deixou de ser verdade; não invente; datas absolutas;
   máx N tokens".
3. Diff vazio ⇒ não escreve. Diff ⇒ `repository.upsert_field(...,
   actor="consolidator")` + audit (o `before/after` do audit é o versionamento
   — sem tabela nova).
4. Proteção: o rewrite automático NUNCA toca `neo-personality`,
   `neo-response-rules`, `neo-operating-procedures` (esses só mudam por
   treinamento explícito do Daniel — são comportamento, não estado).

#### F4.4 Exemplo de melhoria

| Situação | Antes | Depois |
|---|---|---|
| 1ª mensagem do dia: "bom dia, como estamos?" | Neo não tem noção de estado corrente sem retrieval com sorte | `neo-world-state` pinned: "migração neo-voice→neo-host concluída; register em shadow; skill finanças em prod" → resposta situada de primeira |
| "no que eu tava trabalhando mesmo?" | busca híbrida pode trazer projeto de 3 meses atrás | `neo-projects` atualizado semana a semana responde direto, sem retrieval |
| Perfil: Daniel muda rotina de treino p/ manhã | `daniel-profile` continua dizendo "treina à noite" até alguém treinar de novo | evidência episódica de 7d atualiza o bloco; changelog registra a mudança |

#### F4.5 Aceite e testes

- `tests/test_blocks_refresh.py`: budget respeitado; blocos proibidos nunca
  reescritos (assert); diff vazio ⇒ zero writes; changelog rotaciona em 5.

**Esforço: 2–3 dias (a maior parte é prompt + testes). Depende de F3.**

---

### F5 — Bi-temporalidade nos fields do PKS (mata G5)

**Objetivo:** mudança de valor de field cria linha nova e fecha a antiga —
exatamente o padrão que `pks_entity_relation` **já tem**
(`valid_from`/`valid_until`, `db/models/pks.py:137`). Fica por último: mexe no
core do PKS.

#### F5.1 Migração `add_field_temporal_validity`

```sql
ALTER TABLE pks_entity_field
  ADD COLUMN valid_from timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN valid_until timestamptz NULL;
UPDATE pks_entity_field SET valid_from = updated_at;
CREATE UNIQUE INDEX uq_entity_field_current
  ON pks_entity_field (entity_id, key) WHERE valid_until IS NULL AND deleted_at IS NULL;
```

(Verificar antes se existe unique antigo em `(entity_id, key)` no DB prod —
o model não declara constraint; conferir com `\d pks_entity_field` e dropar
na migração se houver.)

#### F5.2 `repository.upsert_field()` — nova semântica

- Valor **igual** (comparação pelo value_* tipado): comportamento atual
  (atualiza confidence/metadata in place). Sem churn de linhas.
- Valor **diferente**: `UPDATE ... SET valid_until = now() WHERE id = :old`
  + INSERT da linha nova (`valid_from = now()`). Audit inalterado
  (before/after já capturam).
- `get_entity_fields` / `get_field`: filtro default
  `valid_until IS NULL` (comportamento atual preservado byte a byte para
  chamadores). Novo `get_field_history(session, entity_id, key, limit=10)`.

#### F5.3 Exposição mínima (leitura)

- `context_builder.format()`: para fields voláteis (number/date), sufixo
  `"(desde YYYY-MM-DD)"` quando `valid_from` < 30 dias — sinaliza recência ao
  LLM sem custo.
- Tool `search_pks`: argumento opcional `include_history: bool = False` →
  quando true, executor anexa `history: [{value, valid_from, valid_until}]`
  (≤5) por field. É o suficiente para o cognitivo responder pergunta
  histórica; um `pks_history` dedicado só se o uso provar demanda.

#### F5.4 Exemplo de melhoria

| Situação | Antes | Depois |
|---|---|---|
| Peso 82kg (jan) → 79kg (jul) | field sobrescrito; trajetória perdida | "você registrou 82kg em janeiro e 79kg agora — 3kg a menos" (via include_history) |
| "quando mudei a carga do supino?" | irrecuperável | `valid_from` da linha atual responde |
| Field atualizado errado pelo planner | valor antigo só no audit (ilegível p/ o LLM) | linha anterior consultável; rollback = fechar a nova e reabrir a antiga |

#### F5.5 Aceite e testes

- `tests/test_field_temporal.py`: valor igual não cria linha; diferente fecha
  e abre; unique parcial impede duas "atuais"; `get_entity_fields` idêntico ao
  comportamento pré-migração para quem não pede histórico.

**Esforço: 3–4 dias. Independente de F2–F4 (precisa só de F0 não — nem isso);
deixar por último mesmo assim por risco.**

---

### F6 — Benchmark de recall (contínuo, começa junto com F1)

**Objetivo:** número objetivo para "a memória melhorou?" e detector de
regressão das fases.

- `tests/memory_benchmark/golden.yaml` (~30 casos, crescendo):

```yaml
- id: pks-device-semantic
  question: "meu computador de jogos aguenta rodar LLM local?"
  expect_in_context: ["PC Gamer", "5070"]     # substring no prompt montado
  expect_layer: user_pks
- id: episodic-tts-fix
  question: "que problema resolvemos no TTS em julho?"
  expect_in_context: ["rampa", "chatterbox"]
  expect_layer: retrieved
```

- `scripts/memory_benchmark.py`: para cada caso, roda SÓ o caminho de leitura
  (`PromptContextSelector.build_prompt` + `recover_context`) contra o DB real
  e verifica as substrings no prompt final. **Sem chamada de LLM de resposta**
  → barato, determinístico, roda em <1min. Output: recall geral + por camada,
  JSON em `logs/memory_benchmark/{data}.json` + diff vs run anterior.
- Agendamento: passo opcional `"benchmark"` do job F3 (semanal, ou após cada
  fase ligada); queda >10% no recall ⇒ entra no relatório do dispatcher.
- É este número que decide objetivamente: threshold do F1, prompts do F2,
  e o ponto de decisão Mem0 (F2.7).

**Esforço: 1–2 dias. Começa junto com F1 e nunca termina.**

---

## §4. Flags novas (todas em `shared/config.py`, default seguro)

| Setting (env) | Default | Fase |
|---|---|---|
| `embedding_e5_prefixes` (`EMBEDDING_E5_PREFIXES`) | `True` | F0 |
| `pks_semantic_search_enabled` | `False` | F1 |
| `pks_semantic_search_shadow` | `True` | F1 |
| `pks_semantic_min_sim` | `0.55` | F1 |
| `episodic_memory_enabled` | `False` | F2 |
| `episodic_memory_shadow` | `True` | F2 |
| `memory_consolidation_enabled` | `False` | F3 |
| `memory_consolidation_shadow` | `True` | F3 |
| `memory_blocks_refresh_enabled` | `False` | F4 (sub-flag do F3) |
| `pks_field_temporal_enabled` | `False`* | F5 |

\* F5: a migração é estrutural; a flag controla só a semântica nova do
`upsert_field` (off = sobrescreve como hoje, mas já com as colunas).

## §5. Deploy, migrações e rollback (topologia real)

1. **Código**: salvar local ⇒ Mutagen ⇒ VPS com `--reload` (memória
   `neo-dev-topology-prod-tunnel`). Mudanças grandes: conferir que o reload
   não travou (gotcha da memória `neo-music-feature`).
2. **Migrações** (F0.4, F2.1, F5.1): na VPS,
   `docker exec neo-core alembic upgrade head`. Nunca via Mutagen automático.
3. **Backfills** (F0.3, F0.4): `docker exec neo-core python scripts/...` logo
   após o deploy do F0. Janela de degradação vetorial de minutos, FTS segura.
4. **Rollback por fase**: desligar a flag (nenhuma fase remove dado).
   Exceção F0: voltar o modelo exige re-rodar o backfill com o modelo antigo.
5. **Verificação pós-deploy**: `docker exec neo-postgres psql -U neo -d neodb`
   com os SELECTs de aceite de cada fase; benchmark F6 antes/depois.

## §6. Ordem, dependências e esforço

```
F0 (1–2d) ─► F1 (2–3d) ─► F2 (1sem) ─► F3 (1sem) ─► F4 (2–3d)
                │                                        
                └─► F6 (1–2d, contínuo)      F5 (3–4d, por último, independente)
```

~4 semanas corridas, cada fase entregável e ligável separadamente
(shadow ⇒ revisar logs/relatórios ⇒ ligar).

## §7. O que explicitamente NÃO fazer

- **Não** rodar Letta server ao lado do neo-core (2º loop LLM, split-brain).
- **Não** adotar Neo4j/Graphiti (2º grafo compete com o PKS permanente).
- **Não** deixar o turno escrever memory blocks — só o consolidador; e o
  consolidador NUNCA toca personality/response-rules/procedures.
- **Não** interpretar resposta vazia do DeepSeek como instrução de
  apagar/alterar memória (sempre ⇒ "sem fatos"/ADD).
- **Não** trocar modelo de embedding sem backfill completo no mesmo deploy.
- **Não** deletar fisicamente memória episódica ou fields históricos —
  `expires_at`/`valid_until`/soft-delete sempre.
- **Não** criar camada nova no `ContextLayer` para blocks (NEO_CORE_PINNED já
  cobre) nem duplicar RRF (extrair util, F1.1).
