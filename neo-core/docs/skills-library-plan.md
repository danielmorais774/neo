# Plano — Biblioteca de Skills do Neo (do `.md` puro ao pipeline Python)

> Status: **IMPLEMENTADO (F1–F4) em 2026-07-08** · Autor: Claude + Daniel
>
> Desvios do plano na implementação:
> - `skill_list` fica SEMPRE no read set (schema ~50 tokens; plumbing condicional
>   não valia a complexidade). O catálogo diz quando usá-la.
> - Router: prefixos `skill_`/`sql_` adicionados a `_SKILL_OWNED_TOOL_PREFIXES`
>   (as tools da biblioteca nunca aparecem no palette do classificador).
> - `sql_query_ro`: além do plano, a sessão roda com
>   `default_transaction_read_only=on` (defesa mesmo sem a role `neo_ro`) e o
>   validador bloqueia funções mutantes chamáveis de SELECT (`set_config`,
>   `pg_terminate_backend`, `dblink*`, …). Role dedicada continua recomendada.
> - Factory do SkillExecutor: em vez de dict de factories, o manifest declara
>   `pipeline_factory="modulo:Classe"`; import lazy + kwargs filtrados por
>   assinatura (`inspect.signature`).
> - Skills exemplo embarcadas: `diagnostico-voz` (instructional),
>   `analise-csv` (scripted) e `postgres-neo` (tool_backed, com
>   `references/schema.md`).
> - Aba Skills do neo-panel (F4 item 7) IMPLEMENTADA e ampliada: não é só
>   listar/ligar/desligar — é CRUD dos 3 níveis declarativos. Ver §13.
> - Pendências de deploy (VPS): `docker compose up -d neo-workbench` (mount novo
>   `/skills:ro`); opcional criar role `neo_ro` + `SQLRO_DSN` no `.env`.
>
> Objetivo: um sistema ÚNICO de skills que vai da mais simples (um `SKILL.md` com
> instruções) à mais complexa (pipeline Python com código próprio), com o núcleo
> cognitivo tendo **ciência total do catálogo** sem pagar o corpo das skills no
> prompt — *progressive disclosure*, no mesmo espírito das Agent Skills do Claude
> Code.

---

## 0. Princípios de design

1. **Progressive disclosure (economia de tokens)** — o cérebro sempre sabe *que*
   uma skill existe (1 linha por skill no prompt); só paga o *corpo* quando decide
   usá-la (tool `skill_read`); e só ganha as *tools* de uma skill depois de lê-la
   (tool gating). Nada de colar `.md` inteiro no system prompt.
2. **Fonte única de verdade** — um `SkillLibrary`/registry unificado alimenta:
   catálogo do cognitivo, seção de skills do router, `_HANDOFF_INSTRUCTION`,
   `_SKILL_TO_INTENT` do handoff parser e a factory do `SkillExecutor`. Hoje isso
   está espalhado e hardcoded em 5 lugares.
3. **Fail-safe** — skill inválida é logada e pulada (nunca derruba o boot);
   `skill_read` de nome inexistente retorna erro amigável; catálogo acima do
   budget é prefiltrado por keywords, nunca estoura o prompt.
4. **O cérebro decide** — mantém a filosofia do bias-to-brain: skills `.md` nunca
   são roteadas diretamente pelo classificador barato; elas são recurso do
   cognitivo. Só pipelines (nível 4) continuam roteáveis/handoff-áveis.

---

## 1. Estado atual (mapeado no código)

| Peça | Onde | O que faz hoje |
|---|---|---|
| Skills "pesadas" | `core/skills/{home_assistant,whatsapp_messaging,music}/` | `SkillPipeline.run()` + `SkillManifest` (registrado em `api/main.py:209-211`) |
| Dispatcher | `core/skills/skill_executor.py` | factory **hardcoded** `if/elif` por nome |
| Handoff | `core/pipeline/handoff_parser.py` | `_SKILL_TO_INTENT` **hardcoded** (3 skills) |
| Instrução de handoff | `modules/desktop/ai_service.py:168` (`_HANDOFF_INSTRUCTION`) | lista de skills **hardcoded** no texto |
| Router | `core/router/intent_router.py` | `_format_skills()` já lê o registry de manifests; prompt tem seções HA/WhatsApp hardcoded |
| Cognitivo | `ai_service._handle_direct_response` + `_run_cognitive_tool_loop` | tool loop bounded com read tools (`core/cognitive/read_tools.py`) + workbench (`neo_*`) + jobs (`job_*`) |
| Workbench | `core/workbench/` + container `neo-workbench` | sandbox Linux do Neo, `/workspace` persistente, internet via proxy (LAN bloqueada) |
| Módulos de tools | `modules/<x>/{tools.py,executor.py}` registrados no `ToolExecutor` global em `api/main.py` | padrão consolidado |

O que falta: skills leves declarativas, catálogo para o cognitivo, leitura sob
demanda, e desduplicar o hardcode acima.

---

## 2. Taxonomia — 4 níveis de skill

| Nível | Tipo | Conteúdo | Quem executa | Exemplo |
|---|---|---|---|---|
| **1** | `instructional` | só `SKILL.md` | o cérebro segue as instruções com as tools que já tem | "formato do relatório semanal de treino", "checklist de diagnóstico de voz" |
| **2** | `scripted` | `SKILL.md` + `scripts/` + `references/` | o cérebro roda os scripts prontos no **sandbox** via `neo_bash` (`/skills/<name>/scripts/...`) | "gerar gráfico de gastos a partir de CSV", "baixar e resumir feed RSS" |
| **3** | `tool_backed` | `SKILL.md` + tools Python server-side **gated** | ler a skill destrava as tools; o código roda no processo neo-core (acesso a recursos internos: Postgres, APIs LAN) | "consultar o banco Postgres do Neo" (`sql_query_ro`) |
| **4** | `pipeline` | pacote Python em `core/skills/<name>/` (como hoje) | `SkillExecutor` via rota do router ou `[[HANDOFF]]` — fluxos com confirmação/missão/estado | `home_assistant`, `whatsapp_messaging`, `music` |

Regra de escolha do nível: *tem fluxo multi-turno com confirmação/estado?* → 4.
*Precisa de recurso interno (DB/LAN) ou lógica Python não-trivial?* → 3.
*Dá pra resolver com script no sandbox (internet/computação pura)?* → 2.
*É só conhecimento/procedimento?* → 1.

> Nota sobre o nível 2: o sandbox **não alcança a LAN nem os containers** (proxy
> recusa RFC1918, by design). Skill que precisa do Postgres/HA/etc. é nível 3,
> não 2. Isso é uma regra documentada no template de skill.

---

## 3. Formato `SKILL.md` (níveis 1–3)

Diretório novo: **`neo-core/skills/`** (top-level; o mount `./neo-core:/app` já o
expõe em `/app/skills` no container — zero mudança de compose para F1).

```
neo-core/skills/
  postgres-neo/
    SKILL.md            # obrigatório
    references/         # opcional — docs extras lidos sob demanda
      schema.md
  grafico-gastos/
    SKILL.md
    scripts/            # opcional (nível 2) — rodam no sandbox em /skills/...
      plot.py
```

`SKILL.md` = frontmatter YAML + corpo markdown:

```markdown
---
name: postgres-neo                # ^[a-z0-9][a-z0-9-]{1,39}$ — único
description: >                    # ≤ 200 chars — é a linha do catálogo; capriche
  Consulta o banco Postgres do Neo (PKS, sessões, missões, mensagens) com SQL
  somente-leitura. Use para perguntas sobre dados internos do Neo.
keywords: [banco, postgres, sql, tabela, consulta, dados, quantas]
kind: tool_backed                 # instructional | scripted | tool_backed
tools: [sql_query_ro]             # só tool_backed: tools destravadas pela leitura
env: []                           # só scripted: envs esperadas no sandbox
requires: []                      # gates de runtime: [workbench] p/ scripted, etc.
enabled: true
version: 1
---

# Consultar o banco do Neo

## Quando usar
...

## Como usar
1. Chame `sql_query_ro` com um SELECT. Sempre use LIMIT (máx 200).
2. Tabelas principais: ver `references/schema.md` (leia com
   `skill_read(name="postgres-neo", file="references/schema.md")`).

## Regras
- NUNCA tente INSERT/UPDATE/DELETE — a tool rejeita.
- Prefira agregações a despejar linhas cruas na resposta.
```

Validação no load (fail-safe, loga e pula):
- `name` único e válido; `description` obrigatória ≤ 200 chars; corpo ≤ 64 KB.
- `kind=scripted` sem `requires: [workbench]` → warning e injeta o require.
- `kind=tool_backed` com tool não registrada no `ToolExecutor` global → skill
  entra no catálogo **sem** as tools + warning (ou fica fora se `strict`).

Skills **nível 4** não têm `SKILL.md`: seus `SkillManifest` Python (já existentes)
ganham `kind="pipeline"` e entram no MESMO registry (ver §4.6).

---

## 4. Arquitetura

### 4.1 `SkillLibrary` — loader/registry (novo: `core/skills/library.py`)

```python
class SkillEntry(BaseModel):
    name: str
    description: str
    keywords: list[str] = []
    kind: Literal["instructional", "scripted", "tool_backed", "pipeline"]
    path: Path | None = None          # None p/ pipeline (nível 4)
    tools: list[str] = []             # tool_backed: nomes a destravar
    env: list[str] = []
    requires: list[str] = []
    enabled: bool = True
    version: int = 1
    # pipeline-only (nível 4, vindo do SkillManifest):
    intent_type: IntentType | None = None
    handoff: bool = False             # aparece no _HANDOFF_INSTRUCTION?

class SkillLibrary:
    def __init__(self, root: Path, settings): ...
    def load(self) -> None            # escaneia root/*/SKILL.md; merge c/ manifests nível 4
    def maybe_reload(self) -> None    # stat() no mtime do dir — hot-reload barato
    def get(self, name) -> SkillEntry | None
    def all(self, *, enabled_only=True) -> list[SkillEntry]
    def read(self, name, file: str | None = None) -> dict   # corpo ou reference (§4.3)
    def tools_for(self, name) -> list[ToolSchema]            # tool gating (§4.4)
    def catalog_note(self, user_message: str | None) -> str | None   # §4.2
    def router_note(self) -> str | None                      # §4.6
    def handoff_instruction(self) -> str                     # §4.6
    def skill_to_intent(self) -> dict[str, IntentType]       # §4.6
```

- Parse do frontmatter com `yaml.safe_load` (PyYAML já disponível; se não for dep
  direta, adicionar `pyyaml>=6` ao `pyproject.toml`).
- Singleton criado no lifespan de `api/main.py`, injetado no `AIService` e nos
  executors que precisarem (mesmo padrão do `pks`/`job_manager`).
- `requires` checa capacidades reais no boot: `workbench` → `settings.workbench_enabled`
  e `tool_registry.exists("neo_bash")`; skill não satisfeita fica fora do catálogo.

### 4.2 Catálogo no prompt cognitivo (nível 0 de disclosure)

Em `_handle_direct_response`, junto das notas existentes (`_WORKBENCH_INSTRUCTION`
etc.), quando `settings.skills_library_enabled and settings.cognitive_tools_enabled
and not is_neo_training`:

```python
_skills_note = self.skill_library.catalog_note(user_message)
if _skills_note:
    messages.append(Message(role=MessageRole.SYSTEM, content=_skills_note))
```

Texto gerado (pronto, ver §7.1). Custo: ~60 tokens fixos + ~20/skill.

Budget: se `len(skills) > settings.skills_catalog_max_entries` (default 30),
prefiltra por interseção de `keywords` com a mensagem (accent-stripped, mesmo
helper `_strip_accents` do router) + inclui sempre as N mais usadas/`always`;
acrescenta 1 linha: *"Catálogo parcial — use `skill_list()` para ver todas."*
(a tool `skill_list` só entra no read set quando o catálogo foi cortado).

### 4.3 Tool `skill_read` (nível 1 de disclosure)

Novo módulo no padrão da casa: `modules/skills_library/{__init__.py,tools.py,executor.py}`.

```python
SKILLS_LIBRARY_TOOLS = [
    ToolSchema(
        name="skill_read",
        description=("Lê as instruções de uma skill do catálogo (ou um arquivo de "
                     "references/ dela). Chame UMA vez, siga o que está escrito."),
        module="skills_library", execution="server-side",
        parameters={
            "name": ToolParameter(type="string", required=True),
            "file": ToolParameter(type="string", required=False,
                description="arquivo relativo dentro da skill, ex: references/schema.md"),
        },
    ),
    # skill_list: só oferecida quando o catálogo foi truncado (§4.2)
]
```

Executor (`SkillsLibraryExecutor.execute`) → `library.read(name, file)`:
- resolve `file` DENTRO da pasta da skill (`Path.resolve()` + checagem de prefixo;
  rejeita `..`, absolutos e symlinks para fora) — anti path-traversal;
- retorno capado em `settings.skill_read_max_bytes` (default 16 KB) com sufixo
  `"... [truncado — peça um arquivo de references/ específico]"`;
- para `kind=scripted`, anexa ao retorno a lista de `scripts/` com o caminho
  sandbox (`/skills/<name>/scripts/x.py`) para o modelo não adivinhar;
- para `kind=tool_backed`, anexa: `"Tools destravadas neste turno: sql_query_ro"`.

Wire-up:
- `api/main.py`: `_tool_executor.register_module_executor("skills_library", ...)`.
- `core/cognitive/read_tools.py`: `skill_read` entra no `COGNITIVE_READ_TOOLS`
  (não é user-scoped). É leitura pura — zero risco de ação.
- O dedupe `seen` do `_run_cognitive_tool_loop` já impede reler no mesmo turno.

### 4.4 Tool gating (nível 3 — o mecanismo-chave p/ "skill de banco de dados")

As tools de uma skill `tool_backed` **não aparecem** no toolset do cognitivo até
a skill ser lida. Mudança pequena e local em `_run_cognitive_tool_loop`
(`modules/desktop/ai_service.py`):

```python
tools = list(tools)  # cópia mutável no início do loop
...
tr = await tool_executor.execute_tool_call(tc)
if tc.name == "skill_read" and not tr.error:
    unlocked = self.skill_library.tools_for(str(tc.arguments.get("name", "")))
    have = {t.name for t in tools}
    fresh = [t for t in unlocked if t.name not in have]
    if fresh:
        tools += fresh   # próximos rounds já oferecem as tools da skill
        if slog: slog.log_event("Skill tools destravadas", ", ".join(t.name for t in fresh))
```

Dispatch: o `CognitiveReadExecutor` já delega nomes desconhecidos ao
`ToolExecutor` global — basta o módulo da tool estar registrado no boot. Ou seja,
uma skill nível 3 = pasta `skills/<name>/SKILL.md` + módulo `modules/<x>/` com a
tool, registrado em `api/main.py`. O cérebro "pode usar quando bem entender, sem
saber como funciona por dentro" — que é exatamente o requisito.

Exemplo de referência a implementar junto (F3): **`modules/sqlro/`** com
`sql_query_ro(query, limit=200)`:
- conexão própria com role Postgres **read-only** (`NEO_DB_DSN_RO`, criar role
  `neo_ro` com `GRANT SELECT`), `statement_timeout=5s`;
- valida `^\s*(select|with)\b`, um único statement, aplica `LIMIT` se ausente;
- resultado como tabela texto compacta, capado em N linhas/bytes.

### 4.5 Scripts no sandbox (nível 2)

- `docker-compose.yml`, serviço `neo-workbench`:

  ```yaml
  volumes:
    - ./neo-core/skills:/skills:ro
  ```

  Read-only: o sandbox pode executar/ler, nunca alterar a skill. O
  `resolve_path` do `WorkbenchSandbox` continua prendendo **escritas** a
  `/workspace`; comandos `neo_bash` podem referenciar `/skills/...` livremente
  (ex.: `python3 /skills/grafico-gastos/scripts/plot.py entrada.csv`).
- Convenção: scripts autossuficientes (stdlib ou `pip install` no próprio
  sandbox, que tem internet via proxy); saída em `/workspace`.
- `env:` do frontmatter (MVP): documentacional + injetadas no serviço
  `neo-workbench` via `.env` do compose. Refinamento posterior: injeção por-exec
  na Docker Exec API quando o comando referencia `/skills/<name>` (allowlist do
  frontmatter) — só se surgir necessidade real.
- Acrescentar 1 frase ao `_WORKBENCH_INSTRUCTION`: *"Skills com scripts prontos
  ficam montadas em /skills (read-only) — use os caminhos que o skill_read te
  der, não reescreva o script."*

### 4.6 Unificação — registry como fonte única (mata o hardcode)

1. **Manifests nível 4 → registry**: `SkillManifest` ganha `kind="pipeline"`,
   `handoff: bool = True` e `handoff_line: str` (linha curta para a instrução).
   `SkillLibrary.load()` faz merge dos `.md` com `SkillManifest.all()`.
2. **`handoff_parser._SKILL_TO_INTENT`** → construído do registry:
   `{e.name: e.intent_type for e in lib.all() if e.kind == "pipeline" and e.handoff}`.
   (Mantém fallback estático se a library estiver off — flag-safe.)
3. **`_HANDOFF_INSTRUCTION`** → `lib.handoff_instruction()` gera a lista de
   skills do texto a partir das `handoff_line`; o esqueleto do texto atual fica
   como template. Comportamento idêntico com as 3 atuais (teste de snapshot).
4. **Router**: `_format_skills()` passa a listar só `kind=pipeline` (roteáveis).
   NOVO: seção compacta gerada por `lib.router_note()` (ver §7.2) dizendo ao
   classificador que assuntos cobertos por skills `.md` são **DIRECT_RESPONSE**
   — evita que "consulta o banco X" caia em `run_command`/CLIENT_TOOL.
5. **`SkillExecutor._create_pipeline`** → registro de factories em vez de
   `if/elif`: cada pacote nível 4 registra
   `SKILL_PIPELINE_FACTORIES["home_assistant"] = lambda deps: HomeAssistantPipeline(...)`
   no seu `__init__.py`; o executor só consulta o dict. Zero mudança de
   comportamento.
6. **(Opcional, F5 — fora do escopo deste plano)**: `IntentType.SKILL` genérico
   com `skill_name`, para pipelines novos não exigirem enum novo + handler novo
   no `ai_service`. Não bloqueia nada acima.

### 4.7 Fluxo completo (exemplo: "quantas sessões de conversa eu tive esse mês?")

1. Router: sem guard determinístico; classificador vê a seção §7.2 → `DIRECT_RESPONSE`.
2. Cognitivo monta prompt: catálogo (nível 0) tem `postgres-neo — consulta o
   banco...`; modelo decide usar.
3. Round 1 do tool loop: `skill_read(name="postgres-neo")` → corpo + "tools
   destravadas: sql_query_ro".
4. Round 2: `sql_query_ro(query="SELECT count(*) ... WHERE created_at >= ...")`.
5. Round 3: resposta final em linguagem natural. Custo extra do turno: catálogo
   (~200 tokens) + corpo da skill (~800) + 2 tool rounds — só porque USOU.

---

## 5. Economia de tokens — regras e números

| Item | Custo | Quando paga |
|---|---|---|
| Catálogo (nível 0) | ~60 + ~20/skill (10 skills ≈ 260 tok) | todo turno cognitivo |
| Schema `skill_read` | ~80 tok | todo turno cognitivo (junto do read set) |
| Corpo `SKILL.md` | tamanho do corpo (alvo < 1.500 tok; cap 16 KB) | só quando o modelo lê |
| `references/` | idem | só quando pede o arquivo |
| Tools da skill (nível 3) | ~100–200 tok/tool | só nos rounds após `skill_read` |

Regras de escrita de skill (documentar no template `skills/_TEMPLATE/SKILL.md`):
- `description` é o que o modelo vê SEMPRE — precisa bastar para decidir "é isso".
- Corpo curto e imperativo; detalhe grande (schemas, tabelas, exemplos longos)
  vai para `references/` e é citado pelo caminho.
- Nada de duplicar o que o cérebro já sabe (persona, regras gerais).

Mitigação de re-leitura entre turnos: o corpo NÃO persiste no histórico da sessão
(o tool loop é local ao turno). Aceitável no início (só paga quem usa); se doer,
F4 tem o *session pinning* (marcar `active_skill` no metadata da sessão e injetar
um resumo curto do corpo nos turnos seguintes).

---

## 6. Fases de implementação (arquivo a arquivo)

### F1 — Núcleo: library + catálogo + `skill_read` (1 dia)

| # | Arquivo | Mudança |
|---|---|---|
| 1 | `neo-core/skills/` | criar pasta + `_TEMPLATE/SKILL.md` + 1 skill instrucional real (ex. `diagnostico-voz/SKILL.md`: passos p/ diagnosticar STT/TTS usando `voice_list`/`config_get`) |
| 2 | `core/skills/library.py` | `SkillEntry` + `SkillLibrary` (load, validação fail-safe, `read()`, `catalog_note()`, `maybe_reload()` por mtime) |
| 3 | `modules/skills_library/` | `tools.py` (`skill_read`, `skill_list`) + `executor.py` |
| 4 | `api/main.py` | instanciar `SkillLibrary` no lifespan; `register_module_executor("skills_library", ...)`; passar ao `AIService` |
| 5 | `core/cognitive/read_tools.py` | adicionar `skill_read` ao `COGNITIVE_READ_TOOLS` (condicional à flag) |
| 6 | `modules/desktop/ai_service.py` | injetar `catalog_note` em `_handle_direct_response` (junto do `_WORKBENCH_INSTRUCTION`) |
| 7 | `shared/config.py` | novas settings (§8) |
| 8 | `tests/skills/` | `test_library_loader.py` (frontmatter ok/quebrado/duplicado/oversize), `test_skill_read.py` (corpo, reference, traversal, cap), `test_catalog_note.py` (budget/prefiltro) |

Critério de aceite: com a flag on, um turno "como eu diagnostico a voz?" gera
`skill_read` no slog e resposta baseada no corpo; com a flag off, zero diferença.

### F2 — Nível 2 (scripts no sandbox) (0,5 dia)

| # | Arquivo | Mudança |
|---|---|---|
| 1 | `docker-compose.yml` | `neo-workbench` ganha `./neo-core/skills:/skills:ro` |
| 2 | `modules/desktop/ai_service.py` | +1 frase no `_WORKBENCH_INSTRUCTION` (§4.5) |
| 3 | `core/skills/library.py` | `read()` anexa listagem de `scripts/` com caminhos `/skills/...` |
| 4 | skill exemplo | `skills/grafico-gastos/` ou similar (script matplotlib no sandbox) |
| 5 | testes | e2e manual via `/verify`: skill_read → neo_bash rodando script montado |

### F3 — Nível 3 (tool gating + skill de banco) (1 dia)

| # | Arquivo | Mudança |
|---|---|---|
| 1 | `modules/desktop/ai_service.py` | tool gating no `_run_cognitive_tool_loop` (§4.4) |
| 2 | `modules/sqlro/` | tool `sql_query_ro` (validação SELECT-only, limit, timeout) |
| 3 | Postgres | role `neo_ro` (migration/script: `CREATE ROLE neo_ro ... GRANT SELECT`) + `NEO_DB_DSN_RO` no `.env` |
| 4 | `api/main.py` | registrar `sqlro`; a tool NÃO entra em `COGNITIVE_READ_TOOLS` (só via gating) |
| 5 | `skills/postgres-neo/` | `SKILL.md` (`kind: tool_backed`, `tools: [sql_query_ro]`) + `references/schema.md` (tabelas principais) |
| 6 | testes | gating (tools aparecem só após skill_read), sql_query_ro rejeita DML/multi-statement |

### F4 — Unificação + QoL (1 dia)

| # | Arquivo | Mudança |
|---|---|---|
| 1 | `core/skills/manifest.py` | `kind`, `handoff`, `handoff_line` no `SkillManifest`; merge no `SkillLibrary` |
| 2 | `core/pipeline/handoff_parser.py` | `_SKILL_TO_INTENT` do registry (fallback estático) |
| 3 | `modules/desktop/ai_service.py` | `_HANDOFF_INSTRUCTION` gerado do registry (snapshot-test antes/depois) |
| 4 | `core/router/intent_router.py` | `_format_skills()` só pipelines + seção §7.2 gerada do registry |
| 5 | `core/skills/skill_executor.py` | factory por registro (dict) em vez de `if/elif` |
| 6 | hot-reload | `maybe_reload()` no início de `catalog_note()` (mtime do dir — de graça em dev e na VPS, que monta o código) |
| 7 | neo-panel | aba "Skills" — virou CRUD completo, ver §13 |

---

## 7. Textos de prompt (prontos)

### 7.1 Catálogo do cognitivo (`catalog_note`)

```
## Suas skills (procedimentos prontos)
Você tem uma biblioteca de skills — instruções e ferramentas prontas para tarefas
específicas. Catálogo (nome — quando usar):
- postgres-neo — Consulta o banco Postgres do Neo (PKS, sessões, missões) com SQL somente-leitura.
- grafico-gastos — Gera gráfico de gastos a partir de um CSV usando script pronto no sandbox.
- diagnostico-voz — Passo a passo para diagnosticar problemas de STT/TTS do Neo.

Quando o pedido casa com uma skill: chame `skill_read(name)` UMA vez, leia e SIGA
as instruções (elas dizem quais tools/scripts usar). Não chame skill_read para
assuntos sem skill correspondente e não invente nomes fora do catálogo.
```

### 7.2 Seção do router (gerada; só quando existem skills .md)

```
## Assuntos com skill interna do cérebro (→ DIRECT_RESPONSE)
O núcleo cognitivo tem skills próprias para: banco de dados do Neo (postgres-neo:
banco, sql, consulta, dados) · gráficos de gastos (grafico-gastos: gasto, csv,
gráfico) · diagnóstico de voz (diagnostico-voz: voz, tts, stt, microfone).
Pedidos sobre esses assuntos NÃO são CLIENT_TOOL nem SERVER_TOOL → DIRECT_RESPONSE
(o cérebro lê a skill e resolve sozinho).
```

(Cap: ~10 skills/nomes; acima disso, só os nomes + keywords mais fortes.)

---

## 8. Novas settings (`shared/config.py`)

```python
skills_library_enabled: bool = True      # master switch (fail-safe: off = hoje)
skills_library_path: str = "/app/skills"
skills_catalog_max_entries: int = 30     # acima disso, prefiltro por keywords
skill_read_max_bytes: int = 16384
skills_router_note_enabled: bool = True  # F4
```

---

## 9. Segurança

- `skill_read` é leitura pura de conteúdo versionado no repo — sem side effects.
- Path traversal bloqueado no `read(file=...)` (resolve + prefix check, nega
  symlink para fora da pasta da skill).
- Nível 2 herda o isolamento do workbench (sem LAN, mount `ro`, mem/pids caps).
- Nível 3: cada tool define seus próprios limites — `sql_query_ro` usa role
  read-only + SELECT-only + timeout + LIMIT. Tools de skill passam pelo mesmo
  `ToolExecutor` global (timeout/observabilidade de sempre).
- Nível 4 intocado: bias-to-brain e confirmações (WhatsApp) continuam valendo.
- Nomes/descrições de skill entram em prompt — o load valida charset do nome e
  tamanho da descrição para não abrir canal de injeção bizarro via arquivo.

## 10. Observabilidade

- `slog.log_event` já cobre tool calls do loop; adicionar eventos "Skill lida"
  e "Skill tools destravadas" (F1/F3) — aparecem no session log e no trace.
- Contagem de uso por skill: `logger.info("[SkillLibrary] read name=%s ...")`
  basta no MVP (métricas de verdade só se o painel F4 sair).

## 11. Deploy (VPS)

1. F1/F3 não mudam compose → só sync (Mutagen) + `docker compose restart neo-core`
   (cuidado com o gotcha do reload travado — usar o fluxo da skill
   `restart-neo-core-fast`).
2. F2 muda o compose (`neo-workbench` volume) → `docker compose up -d neo-workbench`.
3. F3 exige a role `neo_ro` no Postgres prod (`docker exec neo-postgres psql ...`)
   e `NEO_DB_DSN_RO` no `.env`.
4. Rollback: `skills_library_enabled=false` restaura o comportamento atual.

## 12. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Modelo chama `skill_read` para tudo (curiosidade) | instrução do catálogo ("não chame para assuntos sem skill"); dedupe `seen` já limita a 1×/turno; monitorar no slog |
| Catálogo cresce e infla o prompt | budget + prefiltro por keywords + `skill_list` fallback (§4.2) |
| Skill mal escrita degrada respostas | template + regras de escrita (§5); validação de tamanho; review no repo (skills são código) |
| Corpo relido a cada turno de uma tarefa longa | aceito no MVP; session pinning como follow-up (F4+) |
| Classificador manda assunto de skill p/ CLIENT_TOOL | seção §7.2 no router prompt (F4); até lá, o default-to-brain do router já cobre a maioria |
| Tool gating confunde o modelo (tool "aparece" no meio) | o retorno do skill_read anuncia explicitamente as tools destravadas |

---

## 13. Aba Skills do neo-panel (F4 item 7)

O painel virou o caminho normal de autoria: dos 4 níveis, os 3 declarativos
nascem de um `SKILL.md` e portanto podem ser criados pela web. Nível 4 aparece
read-only (é pacote Python).

| Nível | No painel |
|---|---|
| `instructional` | criar, editar, ligar/desligar, apagar |
| `scripted` | idem + editor de `scripts/*.py` (o require `workbench` é derivado, não digitado) |
| `tool_backed` | idem, com as tools escolhidas num seletor do `ToolRegistry` — tool nova continua exigindo código |
| `pipeline` | listado, read-only |

### Peças

- `core/skills/library.py` — `write_skill`, `set_enabled`, `delete_skill`,
  `read_source`, `list_files`, `read_file`, `write_file`, `delete_file`,
  `errors()`, `is_available()`. **A validação de escrita é a mesma do loader**:
  o painel não consegue gravar uma skill que o boot recusaria (teste
  `test_failed_write_leaves_no_partial_skill` e amigos).
- `api/routes/skills.py` — CRUD atrás do `require_panel_jwt`, traduzindo
  `ValueError` → HTTP 400. `GET /skills/catalog` devolve a nota que o cognitivo
  injeta + estimativa de tokens (hoje ~340 com 3 skills).
- `neo-panel/src/SkillsPanel.tsx` — lista, editor de frontmatter + corpo,
  editor de arquivos, banner das skills puladas no load.

### Decisões que valem lembrar

- **Fonte da verdade continua o arquivo.** `enabled` é o `enabled:` do
  frontmatter, não um override em DB: uma fonte só, e o hot-reload por mtime já
  publica a mudança no turno seguinte, sem restart.
- **Escrita atômica** (`tmp` + `os.replace`): o `maybe_reload()` nunca lê meio
  arquivo.
- **`requires` é derivado do `kind`**, nunca vem do cliente (`scripted` →
  `workbench`).
- **Allowlist de escrita**: só `references/<f>` e `scripts/<f>`, um nível, com
  extensão em `_EDITABLE_SUFFIXES`. `SKILL.md` só via `write_skill` (validado).
  `_resolve_writable` é mais estrito que o `_resolve_inside` (que só lê).
- **Skill quebrada continua editável**: `read_source` devolve corpo +
  `parse_error` mesmo com YAML inválido — senão um `SKILL.md` mal editado só
  seria consertável por SSH. Em compensação, `set_enabled` se recusa a
  reescrever um frontmatter que não parseou (apagaria o que o operador precisa
  ver).
- Chaves de frontmatter escritas à mão que o painel não conhece são
  **preservadas** na edição.
