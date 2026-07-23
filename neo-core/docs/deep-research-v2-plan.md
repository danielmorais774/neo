# Plano: Deep Research v2 — de "RAG com citações" para pesquisador de verdade

> **Status:** ✅ **IMPLEMENTADO (F1–F5) em 2026-07-11** — código no repo, 23
> testes novos verdes (`test_research_agent.py`, `test_research_validate.py`,
> `test_research_runner.py`, `test_web_search.py`), suíte completa passando.
> **Falta:** deploy na VPS (migração `k4l5m6n7o8p9` + restart do SearXNG para os
> engines `science`) e calibração dos primeiros relatórios reais.
> Este doc substitui a parte de research do `web-search-deep-research-plan.md`
> (a camada 1 `web_search`/`web_fetch` só mudou na categoria `science`).
>
> **Motivação:** o pipeline v1 (em produção) produz relatórios corretos e
> organizados, mas que parecem "RAG tradicional": concatenação de resumos,
> pouca análise, poucas conexões entre fontes, requisitos do usuário nunca
> verificados. Diagnóstico completo em §1–§2. A v2 ataca isso mantendo o
> esqueleto (protocolos, budgets, jobs, painel) — é evolução, não rewrite.
>
> **Decisões fechadas:**
> - Writer/critic num modelo forte: **`deepseek-v4-pro`** (já mapeado no
>   [router.py:35](../core/llm/router.py#L35), mesmo provider, sem chave nova),
>   config `research_model_writer`. O flash continua em TODO o volume
>   (plan/triage/extração/reflect/organize).
> - Multiagente = **papéis no mesmo pipeline** (prompts+modelos distintos),
>   NÃO processos/agentes separados. Isolamento de contexto já vem do ledger.
> - `core/research/` continua **puro** (só stdlib + protocolos injetados).
> - Sem lib externa de research; sem embeddings nesta versão (near-dup por
>   similaridade de título via difflib).

---

## 1. Diagnóstico do v1 (evidência, verificada no código)

O pipeline atual ([agent.py](../core/research/agent.py)):

```
PLAN (1 flash → lista de queries) → SEARCH ∥ → TRIAGE (1 flash) →
FETCH ∥ (6.000 chars) → NOTES (1 flash por lote de 3, SEQUENCIAL,
fatos em linhas soltas) → [loop QUERIES_FALTANTES] → SYNTH (1 flash,
4.000 tokens, sobre a pilha de notas)
```

Classificação: **T2** numa escada T1 (RAG ingênuo) → T5 (OpenAI/Gemini Deep
Research), com infraestrutura de T4 (jobs, painel, budgets, SSRF, fallback).
A v2 leva o pipeline a T4. O que separa do T5 é o modelo raciocinando sobre
a evidência — por isso o writer forte é decisão de arquitetura, não luxo.

### 1.1 Bugs/limitações encontrados no v1 (todos endereçados neste plano)

| # | Achado | Evidência | Consertado em |
|---|--------|-----------|---------------|
| B1 | **Loop iterativo é dead code no caminho comum**: o round 1 pode consumir `max_fetches` inteiro; round 2 começa com `remaining_budget <= 0` e dá `break`. Os `max_rounds=4` do deep quase nunca executam. | [agent.py:111-113](../core/research/agent.py#L111-L113) | F3 (cap por rodada) |
| B2 | **Sub-perguntas geradas e descartadas**: o prompt do PLAN pede sub-perguntas, o parser só lê `queries`. | [prompts.py:20](../core/research/prompts.py#L20) vs [agent.py:179](../core/research/agent.py#L179) | F1 (PLAN v2) |
| B3 | **Fontes = páginas fetchadas, não citadas**; `published_at` do hit se perde no fetch; síntese mapeia `{url}`→`[n]` sozinha (flash erra citação). | [agent.py:145-148](../core/research/agent.py#L145-L148) | F1 |
| B4 | **Triage com `max_tokens=800`** para selecionar até 24 itens com reason — trunca o JSON e cai no fallback silencioso (ordem da busca). | [agent.py:242](../core/research/agent.py#L242) | F4 (e mitigado na F1) |
| B5 | **NOTES sequencial** (~8 lotes um atrás do outro no deep). | [agent.py:286](../core/research/agent.py#L286) | F1 (gather) |
| B6 | **Resumo do dispatcher = 12 primeiras linhas do markdown** (título + começo, não um sumário). | [runner_research.py:124-133](../core/jobs/runner_research.py#L124) | F1 (Resumo executivo) |
| B7 | **Síntese single-shot de 4.000 tokens sobre notas planas** — causa estrutural do "parece concatenação de resumos". | [agent.py:314-334](../core/research/agent.py#L314) | F1 (writer por seção) |
| B8 | **Nenhuma validação**: "use 30 referências" → 23 passa batido; nada relê o relatório. | (ausência) | F2 |
| B9 | **Lacunas locais**: `QUERIES_FALTANTES` por lote de 3 páginas, sem visão global, teto 3. | [agent.py:299-312](../core/research/agent.py#L299) | F3 (REFLECT) |
| B10 | **Fontes sem ranking/diversidade**: 1 frase no prompt do triage; sem dedup de conteúdo; SearXNG só `general\|news` (a categoria `science` — arXiv/Scholar/Crossref/PubMed — existe de graça e não é usada). | [backends.py:20](../modules/web_search/backends.py#L20), [backends.py:68](../modules/web_search/backends.py#L68) | F4 |

---

## 2. Arquitetura v2 (visão)

```
deep_research(topic, instructions, depth)
   │
   ▼
0. INTAKE (flash, 1 chamada)                                    [F2]
   topic+instructions → RequirementSet (requisitos explícitos:
   nº mín. fontes, seções pedidas, formato, período, idioma;
   + escopo implícito)
   │
   ▼
1. PLAN v2 (flash, 1 chamada)                                   [F1]
   ResearchPlan: sub_questions[] (id+texto) + outline[] (seções
   analíticas, 1ª = "Resumo executivo") + queries[] ligadas a
   sub-perguntas + tipos de fonte alvo
   │
   ▼
2. LOOP DE COLETA (cap de fetches POR RODADA, carry-over)       [F3]
   ├─ a. SEARCH fan-out ∥         (general + news + science)    [F4]
   ├─ b. TRIAGE v2: tier de domínio (determinístico) + LLM p/
   │      diversidade; dedup URL + near-dup por título          [F4]
   ├─ c. FETCH ∥  (tier 1 → 20k chars; resto 6k)                [F4]
   ├─ d. EVIDENCE EXTRACTION ∥ (flash, por lote)                [F3]
   │      → EvidenceLedger: claim + citação textual + url +
   │        data + tipo de fonte + posição do autor + sub_q_id
   └─ e. REFLECT global (código + 1 flash)                      [F3]
          matriz sub-pergunta × evidências → cobertura ok? sai
          : queries dirigidas às lacunas → volta a (a)
   │
   ▼
3. ORGANIZE (flash, 1 chamada)                                  [F3]
   agrupa ledger por seção do outline; marca convergências,
   divergências e conexões causa→efeito entre seções
   │
   ▼
4. WRITE (deepseek-v4-pro, 1 chamada POR SEÇÃO, ∥ até 3)        [F1]
   cada seção: outline global + seu cluster de evidências (com
   [n] resolvidos) + instrução comparativa ("onde as fontes
   divergem, diga quem, por quê, e tome posição fundamentada")
   depois: Resumo executivo + Conclusão sobre os drafts prontos
   │
   ▼
5. VALIDATE                                                     [F2]
   determinístico (código, custo zero): seções do outline
   presentes? fontes citadas ≥ requisito? toda seção com ≥ 2
   citações? sub-perguntas cobertas? todo [n] existe?
   + CRITIC (pro, 1 chamada, rubrica de professor): profundidade,
   coerência, afirmações sem evidência, redundância
   │
   ▼
6. REVISE (pro, até revision_cycles do budget)                  [F2]
   issues → rewrite dirigido por seção; lacuna de EVIDÊNCIA com
   budget sobrando → 1 mini-rodada de coleta; sem budget →
   nota honesta no relatório + flag em quality_json
   │
   ▼
7. DELIVER
   fontes = SÓ as citadas (numeração original, com data);
   resumo do dispatcher = seção "Resumo executivo"
```

**Paralelizável:** buscas da rodada, fetches, extração por lote, escrita das
seções de corpo (semáforo 3), checks determinísticos. **Sequencial por
natureza:** intake→plan, reflect (precisa do ledger todo), organize, critic.

**Custo:** chamadas do pro ≈ 5–10 por pesquisa (seções + resumo/conclusão +
critic + revisões), com contexto comprimido pelo ledger. Todo o resto continua
no flash. Papo normal: zero mudança.

---

## 3. F1 — Escrita v2 (maior impacto; sem migração de DB)

### 3.1 PLAN v2 — `prompts.py::plan_prompts` + `agent.py::_plan`

Novo schema de saída (JSON, com exemplo no prompt — flash lida melhor assim):

```json
{
  "sub_questions": [{"id": "q1", "question": "..."}],
  "outline": [
    {"section": "Resumo executivo", "covers": []},
    {"section": "Contexto", "covers": ["q1"]},
    {"section": "Conclusão", "covers": []}
  ],
  "queries": [{"query": "...", "category": "news|general", "recency": "day|week|month|any", "sub_question": "q1"}]
}
```

Regras do prompt: outline **analítico** (contexto → causas → desenvolvimento →
comparações → impactos → discussão — adaptado ao tópico, NUNCA cronologia pura
salvo pedido); primeira seção sempre `Resumo executivo`, última `Conclusão`;
3–7 seções de corpo (quick: 2–4). Parse defensivo igual ao atual; plano sem
`sub_questions`/`outline` válidos → `ResearchError`. Novo dataclass
`ResearchPlan` em `types.py` (`sub_questions: list[SubQuestion]`,
`outline: list[OutlineSection]`, `queries: list[dict]`).

### 3.2 Writer por seção — novo passo substituindo `_synthesize`

- `ResearchAgent.__init__` ganha `writer_llm: ResearchLLM` (mesmo protocolo,
  adapter fechado sobre `research_model_writer`). O runner monta os dois
  adapters ([runner_research.py:169-175](../core/jobs/runner_research.py#L169)).
- Na F1 (antes do ledger da F3 existir), o insumo do writer são as notas
  atuais **agrupadas por seção**: 1 chamada flash barata (`organize_prompts`
  provisório) distribui as notas pelo outline. Na F3 isso é substituído pelo
  ORGANIZE de verdade sobre o ledger — a interface do writer não muda.
- **1 chamada do pro por seção de corpo** (gather com `Semaphore(3)`), cada
  uma recebendo: outline completo (para não repetir conteúdo de outras
  seções), as notas/evidências da seção JÁ com `[n]` resolvido (o mapa
  url→n é feito em código, nunca pelo LLM — mata o erro de citação B3),
  e a instrução comparativa fixa:
  *"Não resuma fonte por fonte. Cruze as fontes: onde convergem, afirme com
  força e cite as duas; onde divergem, diga QUEM diz o quê, POR QUE divergem
  (método? data? interesse?) e tome posição fundamentada. Conecte com as
  outras seções do outline quando houver relação causa→efeito."*
- Depois dos corpos prontos: **1 chamada do pro** gera `Resumo executivo` +
  `Conclusão` recebendo os drafts (truncados se preciso).
- Montagem em código: título + seções na ordem do outline + `## Fontes`.
- `max_tokens` por seção: novo campo `Budget.max_tokens_section` (default
  2200). `max_tokens_synthesis` passa a ser o teto da chamada de resumo+
  conclusão (mantém 4000).

### 3.3 Fontes citadas + published_at (B3)

- `PageContent` ganha `published_at: str | None = None`; `_fetch_all` copia
  do `SearchHit` ([agent.py:263-276](../core/research/agent.py#L263)).
  `Source` passa a receber `published_at`.
- Ao montar `## Fontes`: varrer o corpo por `\[(\d+)\]` e listar **só os n
  citados**, mantendo a numeração original (buracos são aceitáveis e
  honestos). `sources_json` persiste só as citadas; `stats_json.sources_used`
  = citadas (o painel já lê de lá).

### 3.4 Consertos de carona

- **NOTES em paralelo** (B5): `asyncio.gather` dos lotes com `Semaphore(4)`.
- **Resumo do dispatcher** (B6): `_summary_from_report` extrai a seção
  `## Resumo executivo` (fallback: comportamento atual).
- **Fase nova?** Não — writer roda sob a fase `synthesizing` existente
  (painel inalterado na F1).
- `stats_json` ganha `writer_model`; a coluna `model` continua = flash
  (modelo do volume).
- Timeouts dos budgets sobem (pipeline mais fundo):
  `quick 180→240`, `standard 600→900`, `deep 1500→2400`; atualizar as ETAs
  na descrição da tool ([tools.py:73](../core/jobs/tools.py#L73)).

### 3.5 Config nova (`shared/config.py`, junto de `research_model`)

```python
research_model_writer: str = Field(
    default="deepseek-v4-pro",
    description="Modelo FORTE das chamadas de escrita/crítica do research "
    "(writer por seção, resumo executivo, critic, revisões). O volume "
    "(plan/triage/extração/reflect/organize) continua em research_model.",
)
```

✅ **Aceite F1:** relatório sai estruturado pelo outline (Resumo executivo →
seções analíticas → Conclusão → Fontes), com parágrafos que citam ≥2 fontes
cruzadas quando há divergência; `## Fontes` só lista fontes citadas, com data;
aviso proativo no chat = resumo executivo; testes de `test_research_agent.py`
adaptados (fake do writer separado do fake do flash) verdes.

---

## 4. F2 — Validação + crítica (mata "pediu X, entregou Y")

### 4.1 Migração — 2 colunas novas em `research_task`

Alembic (`add_research_quality_columns`, padrão das migrações existentes):

| Coluna | Tipo | Conteúdo |
|--------|------|----------|
| `requirements_json` | JSON, default `{}` | RequirementSet do INTAKE |
| `quality_json` | JSON, default `{}` | resultado do VALIDATE + rubrica do CRITIC |

(Não dá para enfiar em `stats_json`: o runner o sobrescreve com
`agent.stats.to_dict()` nos estados terminais.)

### 4.2 INTAKE — `prompts.py::intake_prompts` + passo novo no agent

1 chamada flash sobre `topic + instructions` →

```json
{
  "explicit": [{"id": "r1", "text": "pelo menos 30 referências", "check": "min_sources", "value": 30}],
  "implicit_scope": "…", "language": "pt-BR"
}
```

`check` ∈ `min_sources | section_required | period | format | other`
(`other` = só o critic avalia). Sem instructions → `explicit: []` sem chamada
LLM (economia). RequirementSet entra no prompt do PLAN v2 (o plano nasce
moldado pelos requisitos) e persiste em `requirements_json` via
`ResearchProgress` (método novo `meta(key, value)` no protocolo, adapter grava
na task).

### 4.3 VALIDATE — `core/research/validate.py` (novo, puro, sem LLM)

`validate_report(report_md, plan, ledger|notes, requirements) -> ValidationResult`:

- toda seção do outline presente no markdown;
- nº de fontes distintas citadas ≥ `min_sources` (se houver requisito);
- toda seção de corpo com ≥ 2 citações `[n]`;
- todo `[n]` do corpo existe na lista de fontes;
- (com F3) toda sub-pergunta com ≥ 1 evidência usada.

`ValidationResult = {passed: bool, failures: [{check, detail, section?}]}`.

### 4.4 CRITIC + REVISE — chamadas do pro

- **CRITIC** (`critic_prompts`): recebe o relatório + RequirementSet + rubrica
  ("você é um professor universitário avaliando: profundidade, coerência,
  cobertura, afirmações sem evidência, redundância") → JSON
  `{"scores": {"depth": 1-5, "coherence": 1-5, "coverage": 1-5, "evidence": 1-5},
  "issues": [{"section": "...", "issue": "...", "fix": "..."}]}`.
- **REVISE**: agrupa `failures` + `issues` por seção → rewrite dirigido só das
  seções afetadas (pro, mesma instrução comparativa + a lista de correções).
  Até `Budget.revision_cycles` ciclos (quick=0, standard=1, deep=1).
  `min_sources` reprovado **com budget de fetch sobrando** → 1 mini-rodada de
  coleta (reusa o loop; na F2 sem F3, apenas re-search das queries do plano
  ainda não usadas); sem budget → parágrafo honesto no relatório
  ("foram encontradas N fontes das M pedidas") + flag em `quality_json`.
- Fases novas `validating` e `revising`: comentário do model
  ([research_task.py:47-48](../db/models/research_task.py#L47)), e no painel
  `RESEARCH_STATUS_COLORS/LABELS` + `ACTIVE_STATUSES`
  ([ResearchPanel.tsx:21-45](../../neo-panel/src/ResearchPanel.tsx#L21)).
- `quality_json` = `{validation: ValidationResult, rubric: scores, revised: bool}`.

✅ **Aceite F2:** pesquisa com "use pelo menos 30 referências" e só 20
encontradas → relatório declara o gap e `quality_json.validation` registra a
falha; seção fraca apontada pelo critic sai reescrita (diff visível entre
draft e final nos logs); painel mostra fases `Validando`/`Revisando`; get da
API retorna `requirements_json`/`quality_json`.

---

## 5. F3 — Loop de verdade (profundidade que os budgets já prometem)

### 5.1 Cap de fetch por rodada (B1)

Em `agent.run`: `round_cap = max(4, ceil(max_fetches / max_rounds))`; o
não-usado de uma rodada acumula na seguinte
(`remaining = round_cap + carry_over`). O teto global `max_fetches` continua
absoluto. Standard passa a ter 2 rodadas reais de ~6; deep, 4 de ~6.

### 5.2 EvidenceLedger substitui notas em linha

```python
@dataclass
class Evidence:
    id: str                 # "e1", "e2", …
    claim: str              # o fato/argumento, objetivo
    quote: str              # trecho textual curto da página (âncora anti-alucinação)
    url: str
    published_at: str | None
    source_type: str        # academic | official | news | encyclopedia | blog | other
    stance: str | None      # posição do autor, quando houver ("defende que…")
    sub_question_id: str | None
```

`notes_prompts` → `evidence_prompts`: mesma janela (lotes de 3 páginas,
gather ∥ da F1), saída JSON `{"evidence": [Evidence…]}` com exemplo; parse
defensivo + 1 retry; lote inválido 2x = descartado com WARNING (há outros).
A linha `QUERIES_FALTANTES` **morre** — lacuna vira responsabilidade do
REFLECT.

### 5.3 REFLECT global (substitui B9)

Após cada rodada: **em código**, matriz `sub_question × (nº evidências,
melhor source_type)`. Se toda sub-pergunta tem ≥ 2 evidências (≥ 1 na quick)
ou não há budget → sai do loop. Senão, 1 chamada flash
(`reflect_prompts`: matriz + tópico → até 6 queries dirigidas às
sub-perguntas descobertas, podendo criar sub-pergunta nova se o material
revelou ângulo essencial) → próxima rodada.

### 5.4 ORGANIZE (insumo real do writer)

1 chamada flash: ledger completo (compacto: `id | claim | source_type | url`)
+ outline → `{"sections": [{"section": "...", "evidence_ids": [...],
"convergences": ["e1+e4: …"], "divergences": ["e2 vs e7: …"],
"links": ["causa em §Causas → efeito em §Impactos"]}]}`. O writer da F1 passa
a receber isso por seção (com `quote` incluído nas evidências da seção — o
pro escreve com a citação textual à vista). Ledger grande (> ~120 evidências):
truncar por seção priorizando source_type alto — teto natural dado pelos
budgets.

✅ **Aceite F3:** em `standard`, os logs mostram ≥ 2 rodadas com queries da 2ª
dirigidas a lacunas da matriz (não genéricas); `stats_json` ganha
`rounds` e `evidence_count`; teste com fakes: sub-pergunta sem evidência na
rodada 1 → REFLECT gera query para ela → rodada 2 acontece; cap por rodada
respeitado; validador da F2 passa a checar cobertura de sub-perguntas.

---

## 6. F4 — Fontes (matéria-prima)

1. **Categoria `science`**: `VALID_CATEGORY += ("science",)`
   ([backends.py:20](../modules/web_search/backends.py#L20)); SearxngBackend
   mapeia `science → categories=science`
   ([backends.py:68](../modules/web_search/backends.py#L68)); ddgs não tem
   science → trata como `general` (sem hack de query). Expor `science` no
   enum da tool `web_search` ([tools.py](../modules/web_search/tools.py)) e
   no prompt do PLAN ("temas acadêmicos/técnicos → inclua queries science").
   No [settings.yml](../../docker/searxng/settings.yml) do SearXNG, garantir
   engines da categoria science ativos (arxiv, google scholar, semantic
   scholar, crossref, pubmed) — validar com
   `curl 'http://searxng:8080/search?q=teste&categories=science&format=json'`
   de dentro do container (lembrar do gotcha `formats: [html, json]`).
2. **Tier de domínio determinístico**: novo `core/research/source_tiers.py`
   (puro, só dados + `tier_for(url) -> int`):
   - tier 1 — `.edu`, `.gov`, `.gov.br`, arxiv.org, doi.org, nature.com,
     science.org, ieee.org, acm.org, pubmed/nih.gov, scielo.br, ibge.gov.br,
     bcb.gov.br, scholar.google, semanticscholar.org;
   - tier 2 — wikipedia.org, britannica.com, reuters, apnews, bbc,
     folha/uol, estadao, g1/globo, nexojornal;
   - tier 3 — default (inclui os "sites educacionais" que dominavam o v1).
   TRIAGE v2: ordena candidatos por tier antes do LLM; o LLM escolhe
   respeitando diversidade (máx 2 por domínio) e o prompt passa a mostrar o
   tier de cada empresa. `Evidence.source_type` derivado do tier no
   extraction (dica no prompt).
3. **Near-dup**: antes do triage, `difflib.SequenceMatcher` sobre títulos
   normalizados (lower, sem pontuação); ratio > 0.85 → mantém o de tier
   melhor. (Sem embeddings nesta versão.)
4. **Fetch maior para fonte boa**: tier 1 → `max_chars=20_000`; resto mantém
   6.000 (`_FETCH_MAX_CHARS` vira função do tier).
5. **B4**: `max_tokens` do triage 800 → 1500.

✅ **Aceite F4:** pesquisa acadêmica de teste traz ≥ 30% de fontes tier 1-2
(visível em `sources_json`/painel); duas URLs com o mesmo título não são
lidas duas vezes; triage não degrada mais por JSON truncado nos logs.

---

## 7. F5 — Observabilidade de qualidade (painel)

- `GET /api/v1/research` (list): incluir resumo de qualidade
  (`rubric_avg`, `validation_passed`) — campos pequenos, sem report.
- `GET /{task_id}`: já retornará `requirements_json`/`quality_json` (F2).
- [ResearchDetailPanel.tsx](../../neo-panel/src/ResearchDetailPanel.tsx):
  bloco "Qualidade" — scores da rubrica, checks do validador (✓/✗ por
  requisito), matriz de cobertura (sub-pergunta × nº evidências), distribuição
  de tiers das fontes; badge na lista quando `validation_passed=false`.

✅ **Aceite F5:** detalhe de uma pesquisa mostra rubrica + requisitos ✓/✗ +
cobertura; lista sinaliza pesquisas entregues com gap declarado.

---

## 8. Mudanças por arquivo (checklist do implementador)

| # | Arquivo | Fase | Mudança |
|---|---------|------|---------|
| 1 | `core/research/types.py` | F1,F3 | `ResearchPlan`/`SubQuestion`/`OutlineSection`; `PageContent.published_at`; `Evidence`; `Budget.max_tokens_section`+`revision_cycles`; `ResearchProgress.meta()` |
| 2 | `core/research/prompts.py` | F1-F3 | `plan_prompts` v2; `section_prompts`; `summary_conclusion_prompts`; `intake_prompts`; `critic_prompts`; `revise_prompts`; `evidence_prompts` (substitui `notes_prompts`); `reflect_prompts`; `organize_prompts`; triage mostra tier |
| 3 | `core/research/agent.py` | F1-F4 | writer_llm injetado; pipeline v2 (§2); cap por rodada; gather em extração; fontes citadas c/ published_at; near-dup; `_synthesize` removido |
| 4 | `core/research/validate.py` (novo) | F2 | validador determinístico §4.3 |
| 5 | `core/research/source_tiers.py` (novo) | F4 | tiers §6.2 |
| 6 | `core/research/budgets.py` | F1,F2 | timeouts novos; `max_tokens_section`; `revision_cycles` |
| 7 | `core/jobs/runner_research.py` | F1,F2 | 2º adapter LLM (writer); `meta()` no ProgressAdapter; summary = Resumo executivo; persistir requirements/quality |
| 8 | `core/jobs/tools.py` | F1 | ETAs novas na descrição de `deep_research` |
| 9 | `shared/config.py` | F1 | `research_model_writer` (§3.5) |
| 10 | `db/models/research_task.py` + migração alembic | F2 | `requirements_json`, `quality_json`; comentário de fases + `validating`/`revising` |
| 11 | `db/async_client.py` | F2 | update/get incluem as colunas novas |
| 12 | `modules/web_search/backends.py` | F4 | categoria `science` |
| 13 | `modules/web_search/tools.py` | F4 | enum `science` no schema |
| 14 | `docker/searxng/settings.yml` (repo raiz) | F4 | engines science ativos |
| 15 | `api/routes/research.py` | F2,F5 | expor requirements/quality; list c/ resumo de qualidade |
| 16 | `neo-panel/src/ResearchPanel.tsx` | F2,F5 | statuses novos; badge de qualidade |
| 17 | `neo-panel/src/ResearchDetailPanel.tsx` | F5 | bloco Qualidade §7 |
| 18 | `tests/test_research_agent.py` | F1-F4 | ver §9 |
| 19 | `tests/test_research_runner.py` | F1,F2 | writer adapter; persistência quality |

## 9. Testes (fakes dos protocolos, padrão dos testes existentes)

- **PLAN v2**: JSON válido → ResearchPlan completo; sem outline → erro limpo.
- **Writer**: fake flash + fake pro distintos → cada seção do outline gera 1
  chamada do pro com as evidências certas e mapa [n] resolvido; seções em
  paralelo respeitam o semáforo.
- **Fontes**: página fetchada mas não citada não aparece em `## Fontes`;
  published_at flui hit→Source.
- **Cap por rodada (B1)**: standard com fetches todos ok → 2 rodadas
  acontecem; carry-over funciona.
- **REFLECT**: sub-pergunta descoberta → query dirigida na rodada 2; cobertura
  completa → loop sai antes de `max_rounds`.
- **VALIDATE**: relatório sem seção do outline / com 23<30 fontes / com [9]
  inexistente → failures corretos; tudo ok → passed.
- **CRITIC/REVISE**: issue numa seção → só ela é reescrita; `revision_cycles=0`
  (quick) → critic não roda.
- **Tiers/near-dup**: `tier_for` nos domínios âncora; títulos 0.9-similares →
  1 só sobrevive (o de tier melhor).
- **Runner**: quality/requirements persistidos; summary = resumo executivo.

## 10. Riscos / pontos de atenção

- **deepseek-v4-pro**: validar preço/latência reais no provider antes de subir
  o default; se decepcionar, `research_model_writer` é 1 config (candidatos:
  `deepseek-reasoner`, `gpt-4o`).
- **Custo do deep**: até ~10 chamadas do pro + rodadas extras de flash;
  monitorar `stats_json.llm_calls` na primeira semana.
- **JSON do flash nos schemas novos** (plan v2, evidence, reflect, organize):
  manter exemplos no prompt e o parse defensivo + retry já padronizado;
  evidence tem fallback natural (lote descartado).
- **Fases novas × painel antigo**: deploy do neo-core antes do painel deixa
  `validating`/`revising` sem label — o `RESEARCH_STATUS_LABELS` deve ter
  fallback para o status cru (verificar; se não tiver, incluir na F2).
- **SearXNG science**: qualidade dos engines acadêmicos varia; validar na F4
  com 2-3 tópicos reais e desativar engine ruim no settings.yml.
- **Timeouts maiores**: conferir que `JobManager`/`timeout_at` acompanham o
  budget novo (o runner já deriva de `budget.timeout_s` — só conferir).
- **Deploy**: fluxo Mutagen + `--reload` de sempre; a migração alembic roda na
  VPS; mudança no settings.yml do SearXNG exige restart do container
  (gotcha do reload travado ao mexer no compose).

## 11. Ordem de execução

**F1 → F2 → F3 → F4 → F5**, cada fase entregável e testável sozinha
(aceites nos §3-§7). F1 e F2 não dependem de migração de agent_job nem
quebram dados existentes; pesquisas antigas continuam legíveis no painel
(campos novos vazios).

---

## 12. Registro da implementação (2026-07-11)

Tudo o que o plano previa foi implementado; três desvios/descobertas valem
registro para quem for calibrar depois:

1. **Near-dup por título precisou de piso de tamanho.** `SequenceMatcher` sobre
   títulos curtos dá falso positivo ("Fonte 1" × "Fonte 2" = 0,857 de razão, acima
   do corte de 0,85) e descartava fontes legítimas. Só comparamos títulos
   normalizados com ≥ 25 caracteres (`_NEAR_DUP_MIN_TITLE` em `agent.py`).
2. **Passos opcionais degradam, não matam.** TRIAGE, ORGANIZE, REFLECT, EVIDENCE,
   CRITIC e INTAKE usam `_llm_json_soft`: JSON inválido 2x vira WARNING + fallback
   determinístico (triage cai na ordem por tier; organize distribui a evidência
   pelas sub-perguntas que a seção declarou cobrir). Só PLAN mata a pesquisa —
   sem outline não há o que escrever.
3. **O CRITIC roda uma vez só.** Nos ciclos de revisão seguintes quem manda é o
   validador determinístico (custo zero) — pagar o modelo forte para criticar de
   novo não se justificou no desenho.

**Aceites verificados em teste** (não só unitários — o dry-run do pipeline
completo montou o relatório com resumo executivo → seções do outline → conclusão
→ fontes só as citadas, escolhendo arXiv/IBGE sobre um blog):
`test_round_cap_leaves_budget_for_second_round` (o B1, que era dead code, agora
roda a 2ª rodada com query dirigida à lacuna), `test_min_sources_requirement_
failure_is_declared_not_hidden` (o "pediu 30, entregou 23" vira nota no
relatório + falha em `quality_json`), `test_critic_issue_triggers_targeted_
rewrite` (só a seção apontada é reescrita), `test_report_follows_outline_and_
cites_only_used_sources`.

### 12.1 O que o PRIMEIRO relatório em prod ensinou (quick, energia solar no NE)

O pipeline rodou inteiro (205s, 4 buscas, 6 fontes, 13 chamadas LLM) e a
maquinaria de qualidade fez o trabalho dela — o VALIDATE pegou "pediu 8
referências, citou 1" e a sub-pergunta sem evidência, e o relatório **declarou o
gap** em vez de escondê-lo. A prosa veio analítica de verdade (conecta seções,
distingue produção de emprego, é honesta sobre o que a evidência não sustenta).
Três defeitos apareceram, todos consertados:

1. **Seções cortadas no meio da frase.** `_words_for` estimava 1,7 token/palavra;
   em pt-BR é ~2,8. O prompt pedia ~820 palavras com teto de 1.400 tokens → o
   escritor batia no teto. Agora o divisor é 3,2 (folga proposital) e os tetos
   subiram (quick 2.000 / standard 3.000 / deep 3.600). Teste
   `test_word_target_fits_inside_the_token_cap` trava a invariante.
2. **Academia brasileira caindo em tier 3.** `revistaaber.emnuvens.com.br`
   (periódico revisado por pares em OJS) e `repositorio.ufpi.br` viravam
   `source_type: other` — o REFLECT achava que não havia fonte forte e a
   cobertura mentia. `source_tiers.py` ganhou padrões de host
   (`repositorio.`, `periodicos.`, `revistas.`, `emnuvens.com.br`, `seer.`,
   `hdl.handle.net`…), avaliados **depois** da lista de imprensa para não
   promover revista de grande circulação a acadêmica.
3. **`published_at` cru** (`2026-01-01T00:00:00`) na seção Fontes → normalizado
   para o dia (`_norm_date`).

### 12.2 O que o SEGUNDO relatório em prod ensinou (standard, mesmo tópico)

Rodou o pipeline inteiro (527s, 2 rodadas, 12 fontes lidas, 28 chamadas LLM,
1 revisão) e os fixes da §12.1 pegaram: as seções não vieram truncadas e as
fontes acadêmicas brasileiras foram corretamente reconhecidas (`has_strong_
source: true` em todas as sub-perguntas, contra `other`/false antes). Mas o
relatório citou **2 fontes de 12 lidas** — e a caça a essa causa achou dois bugs
sérios e uma limitação de funil:

1. **JSON truncado custava o LOTE INTEIRO (bug grave).** A extração devolve
   claim + citação textual por item; com lote de 3 páginas o JSON estourava o
   teto de tokens no meio da lista, `_llm_json_soft` não parseava e **todas as
   páginas daquele lote iam para o lixo** — páginas já buscadas, baixadas e
   pagas. O mesmo derrubou o CRITIC (rubrica vazia: "critic degradou (JSON
   inválido 2x)"). Fixes: `_salvage_object_list` recupera os objetos completos
   de uma lista truncada (o que veio inteiro é aproveitado; só a cauda cortada
   se perde), lote 3 → 2, tetos de token maiores (evidência 4.000, critic 3.500)
   e citação textual limitada a 25 palavras.
2. **Evidência descartada por URL quase-igual.** O código exigia URL idêntica à
   da página; o modelo às vezes devolve com barra final. Agora casa por
   normalização e **loga** o descarte (era silencioso).
3. **O funil traz o tema VIZINHO (limitação, não bug).** Investigando as páginas
   que renderam zero evidência, o modelo estava **certo**: eram artigos sobre
   energia *eólica* num relatório sobre energia *solar*. A raiz é anterior — o
   PLAN gerava queries academicamente enviesadas (`science` demais) e o TRIAGE
   maximizava tier de domínio. Um paper tier 1 sobre o tema vizinho não sustenta
   relatório nenhum. Prompts recalibrados: o PLAN equilibra `science` com
   `general` (relatórios setoriais, agências, dados oficiais — onde estão os
   números atuais) e é específico no termo central; o TRIAGE agora põe
   **relevância ao tópico ANTES do tier**.

### 12.3 O bug central: resposta VAZIA por orçamento gasto no raciocínio

Depois de consertar truncamento, tiers e funil, uma pesquisa ainda vinha com
seções faltando, rubrica em branco e lotes de evidência perdidos — e os logs
mostravam `evidence degradou`/`triage degradou` de forma **intermitente**, o que
não casa com prompt ruim. Rodando a mesma chamada 4× em paralelo contra o modelo
real, o padrão apareceu:

```
call 0: finish=stop   | chars=2218 | {"evidence": [...
call 1: finish=stop   | chars=1273 | {"evidence":[...
call 2: finish=stop   | chars=1547 | {"evidence": [...
call 3: finish=length | chars=0    | <VAZIO>          ← ~1 em 4
```

Os DeepSeek raciocinam ANTES de responder e cobram esse raciocínio do mesmo
`max_tokens`. Quando o teto acaba durante o raciocínio, a API devolve
`finish_reason=length` com **content vazio**. O pipeline tratava isso como "o
modelo não produziu nada": seção do relatório **sumia**, lote de evidência ia
para o lixo, rubrica ficava `{}`. Silencioso e devastador — e invisível para
qualquer teste com fake, porque um fake nunca devolve vazio.

**Fix (`_complete`)**: resposta vazia não é "sem resposta", é orçamento curto →
retentativa automática com o dobro do teto (limite 8.000). Vale para TODAS as
chamadas (plan, triage, evidência, reflect, organize, escrita, crítica, revisão).
`stats.empty_retries` expõe a frequência: se subir, o teto base está apertado.
Teste `test_empty_response_is_retried_with_more_headroom` trava o comportamento
(inclusive que a retentativa usa teto maior — repetir com o mesmo teto só
repetiria o estouro).

### 12.4 PDF é cidadão de primeira classe

7 das 12 fontes de uma pesquisa acadêmica morriam em `fetch_failed`: eram PDFs
(repositório institucional serve `bitstream/*.pdf`, periódico serve
`article/download`) e o trafilatura não parseia PDF. Metade da fonte primária
brasileira estava sendo descartada. `fetch.py` ganhou extração via **pypdf**
(dep nova), com detecção por *magic bytes* (`%PDF-`) porque repositórios mentem
no `content-type`, teto de 40 páginas/15MB e `js_required` para PDF escaneado
sem camada de texto (sem OCR não há o que extrair — o modelo tenta outra fonte).
Validado contra as URLs reais que falhavam: passaram a entregar 20k caracteres
de texto útil (resumo, método, resultados, conclusão).

### 12.5 Resultado em prod (standard, mesmo tópico e pedido, a cada fix)

| Métrica | 1º relatório | Meio da série | **Final** |
|---|---|---|---|
| Evidências no ledger | 6 | 30 | 20 |
| **Fontes citadas** | 2 | 6 | **8** |
| Fontes com evidência (novo) | — | 10+ | **8** (usou 100%) |
| **Validação** | 6 falhas | 1 falha | **PASSOU (0)** |
| Rubrica (prof/coer/cob/evid) | critic degradou | 3/4/2/2 | **4/4/4/5** |
| `empty_retries` | — | 7 | 8 |

O requisito de "pelo menos 8 referências" foi **cumprido**, e `sources_used ==
sources_with_evidence` mostra que o relatório usou toda a matéria-prima
coletada. As 8 fontes são um mix real: EPE (anuário oficial), O Globo, Valor,
DIEESE, policy brief do LabSOLar, periódico acadêmico, repositório da UFRJ e a
Revista Econômica do BNB — de imprensa a dado primário, todas sobre o recorte
pedido.

A prosa é o que a v2 buscava: números datados com fonte (R$ 5,5 bi de devolução
tarifária [3], 52% dos projetos centralizados [4]), tensões explicitadas (o
"paradoxo das emissões incorporadas na importação de painéis" contra a mitigação
de CO₂), conexões causais entre seções e lacunas declaradas em vez de
preenchidas com achismo.

**Limitações reconhecidas (não são bugs):**
- **Páginas de abstract.** Buscas acadêmicas devolvem landing pages: o texto
  extraído é resumo + lista de referências. Rende 2-3 evidências, não 8.
- **`min_sources` alto ainda pode não ser cumprido.** Com `max_fetches=12` no
  standard e parte das fontes irrelevante/ilegível, um pedido de "30 referências"
  não fecha. O relatório declara o gap (é o comportamento correto), mas quem
  pede muitas referências deve usar `deep` — a descrição da tool já orienta isso.
- **`quick` não repara requisito** (`revision_cycles=0`, sem mini-rodada).
- **Sem OCR**: PDF escaneado (imagem) continua ilegível — por design.

### 12.6 INCIDENTE: trafilatura/lxml em paralelo derrubou o neo-core

**O bug mais grave da série.** Durante uma pesquisa, o log terminou em:

```
DEBUG: htmldate.validators - minimum date setting: 1995-01-01
double free or corruption (!prev)
```

`trafilatura` (e o `htmldate` que ele usa) rodam sobre **lxml, uma extensão C**,
e não são seguros quando várias threads extraem ao mesmo tempo. O pool de fetch
(`fetch_concurrency=4`) chamava `_extract_sync` via `asyncio.to_thread` em
paralelo → **corrupção de heap → o processo Python MORREU**. O sintoma é cruel:
o container fica `Up` (o Docker vê o PID 1 do entrypoint), mas **o uvicorn está
morto e a API inteira fora do ar** — a produção ficou indisponível até o restart.
O job virou zumbi (`running`, sem heartbeat, além do `timeout_at`), porque quem
faria o timeout era justamente o processo que morreu.

**Fix**: `threading.Lock` serializando a extração
([modules/web_search/fetch.py](../modules/web_search/fetch.py)). A extração custa
milissegundos perto do I/O de rede — serializar não pesa na latência e elimina a
classe inteira de bug. `pypdf` é Python puro (verificado: nenhuma extensão C) e
não precisa do lock. Regressão em `test_concurrent_html_extraction_is_serialized`
(12 extrações concorrentes com lxml real) + stress em prod (18 extrações
paralelas, processo vivo).

**Lição:** biblioteca com extensão C chamada de `asyncio.to_thread` concorrente
merece lock por padrão. E o `--reload` do uvicorn **não ressuscita** um processo
que morreu por corrupção de memória: o container fica de pé mentindo que está
saudável. Vale um healthcheck de verdade no compose (`/health`), hoje inexistente.

### 12.7 Fontes coletadas e não usadas

O painel entregou o diagnóstico que faltava: **30 evidências coletadas, 6 fontes
citadas**. Faltar referência por falta de **uso** é diferente de faltar por falta
de fonte — e o conserto é outro. Três mudanças: (a) o writer recebe o requisito
(`citation_goal_note`) e a contagem de fontes com evidência disponível; (b) o
ORGANIZE é instruído a não descartar evidência pertinente (item que não encaixa
perfeito vai para a seção mais próxima em vez de virar trabalho jogado fora);
(c) a revisão por `min_sources` **primeiro incorpora as fontes já lidas e não
citadas** (apontando à seção que as tem em mãos) e só busca mais se não sobrou
nada. Nova stat `sources_with_evidence`: comparada com `sources_used`, mostra
de relance quanta matéria-prima o relatório deixou na mesa.

### 12.8 Estado do deploy (2026-07-11)

**EM PRODUÇÃO e validado end-to-end:**
- Migração `k4l5m6n7o8p9` aplicada (pesquisas antigas preservadas com `{}`).
- neo-core rodando a v2 (`volume=deepseek-v4-flash, writer=deepseek-v4-pro`);
  `pypdf` instalado via restart do container (o entrypoint detecta o hash do
  pyproject — não precisa rebuild da imagem).
- Categoria `science` do SearXNG **funciona com os engines default** (teste em
  prod: 60 resultados DOI). O bloco `engines:` do `settings.yml` é só otimização
  e **não foi aplicado** — ver o gotcha abaixo.

**Gotcha do `docker/searxng/settings.yml`:** na VPS o diretório é do uid 977
(usuário do container), modo 700 → **o Mutagen não consegue sincronizar** e o
`daniel` não tem sudo sem senha. Editar o arquivo no Mac **não muda nada em
prod, silenciosamente**. Para aplicar de verdade: escrever pelo container
(`docker exec -u 977 neo-searxng sh -c 'cat > /etc/searxng/settings.yml'`, mount
é RW) + restart.

**Pendências:**
1. Rebuild/reload do neo-panel no Mac (aba Pesquisas ganhou o bloco Qualidade).
2. Opcional: aplicar o `settings.yml` com os engines science explícitos.
3. Observar `stats.empty_retries` nos próximos relatórios — se ficar alto
   (>10 por pesquisa), os tetos base de token merecem subir.

**Não repetir:** editar arquivo `.py` com uma pesquisa rodando derruba o job (o
`--reload` reinicia o app). O reconcile de órfãos agora fecha a `research_task`
junto do `agent_job` — antes ela ficava presa em `searching` para sempre.
