# PKS — Personal Knowledge Store

Sistema de gestão de conhecimento pessoal que armazena fatos sobre usuários (pessoas, dispositivos, treinos, preferências, etc.) em PostgreSQL. Suporta aprendizado a partir de conversas, recuperação de contexto relevante e manutenção de uma ontologia de tipos de entidades e relações.

---

## Visão Geral da Arquitetura

```
PKSService (fachada)
├── repository      — acesso PostgreSQL de baixo nível
├── writer          — pipeline de aprendizado (planeja via LLM, executa no DB)
├── retriever       — caminho síncrono de recuperação
├── bootstrap       — inicialização de workspace para novos usuários
├── entity_resolver — resolve queries em entidades
├── context_builder — formata resultados para injeção em prompts
├── ontology        — vocabulário controlado + normalização
├── validator       — validação e canonicalização de valores LLM
└── audit           — logging de auditoria de todas as mutações
```

---

## Estrutura de Arquivos

### Core: `core/pks/`

| Arquivo | Responsabilidade |
|---------|-----------------|
| `service.py` | Fachada unificada — ponto de entrada para todas as operações PKS |
| `repository.py` | Camada de acesso PostgreSQL (SQL raw async via SQLAlchemy) |
| `writer.py` | Pipeline de aprendizado: planeja operações via LLM, valida, executa |
| `retriever.py` | Recuperação síncrona para `DIRECT_RESPONSE` |
| `memory_planner.py` | Prompt LLM que propõe operações de escrita |
| `entity_resolver.py` | Resolve queries em linguagem natural para entidades |
| `context_builder.py` | Formata `RetrievalResult` em texto para injeção em prompt |
| `bootstrap.py` | Inicialização de workspace para novos usuários |
| `ontology.py` | Vocabulário controlado (tipos de entidade, relações, aliases) |
| `schemas.py` | Contratos Pydantic para todas as operações PKS |
| `audit.py` | Logging de auditoria para todas as mutações |

### Banco de Dados: `db/models/pks.py` + `alembic/versions/`

8 tabelas PostgreSQL:

| Tabela | Função |
|--------|--------|
| `pks_workspace` | Container por usuário |
| `pks_space` | Pasta organizacional dentro do workspace |
| `pks_collection` | Agrupamento adicional dentro de um space |
| `pks_entity` | Unidade central de conhecimento |
| `pks_entity_field` | Atributos chave-valor de uma entidade |
| `pks_entity_relation` | Arestas dirigidas entre entidades |
| `pks_workspace_link` | Links entre workspaces |
| `pks_audit_log` | Trilha de auditoria imutável de todas as mutações |

---

## Componentes Detalhados

### 1. `schemas.py` — Contratos Tipados

Modelos Pydantic para todo o sistema:

- **`EntityRecord`** — Entidade com id, workspace_id, name, type, aliases, search_text, metadata, confidence, sensitivity, last_used_at
- **`FieldRecord`** — Atributo chave-valor (value pode ser text, number, boolean, date, json)
- **`RelationRecord`** — Ligação dirigida entre duas entidades
- **`EntityCandidate`** — Entidade com score de match e palavra-chave que a encontrou
- **`Neighborhood`** — Entidade agrupada com seus fields e relations
- **`RetrievalResult`** — Resultado final da recuperação com candidatos e neighborhoods
- **`PKSOperation`** — Operação de escrita proposta (upsert_entity, upsert_field, upsert_relation, expire_relation, soft_delete_entity)

### 2. `ontology.py` — Vocabulário Controlado

**`EntityType` (34 tipos):**
`person`, `project`, `company`, `device`, `technology`, `document`, `workout`, `exercise`, `muscle_group`, `meal`, `food`, `supplement`, `health_state`, `restriction`, `routine`, `goal`, `preference`, `vehicle`, `property`, `account`, `pet`, `place`, `software`, `hardware_problem`

**`RelationType` (16 tipos):**
`owns`, `works_on`, `uses_stack`, `developed_on`, `used_for`, `has_exercise`, `targets`, `uses_equipment`, `sibling_of`, `child_of`, `parent_of`, `belongs_to`, `related_to`, `mentions`, `located_in`, `has_member`, `performed`

**`OntologyResolver`** — Mapeia aliases em português/inglês para tipos canônicos:
- `"pc"`, `"notebook"`, `"celular"` → `device`
- `"possui"`, `"tem"` → `owns`
- `"processador"`, `"cpu"` → `cpu`

**`OntologyValidator`** — Canonicaliza valores propostos pelo LLM e rejeita tipos desconhecidos.

### 3. `repository.py` — Camada de Acesso ao Banco

`PKSRepository` opera via SQL raw assíncrono (SQLAlchemy):

**Workspace/Space/Collection:**
- `ensure_workspace()`, `ensure_space()`, `ensure_collection()`

**Entidades:**
- `get_entity()`, `find_entity_by_name()`, `search_entities()` — busca trigram + alias + nome exato com scoring
- `upsert_entity()` — merge por nome+tipo, unindo aliases e metadata
- `update_entity_identity()` — atualiza nome e aliases da entidade primária
- `soft_delete_entity()` — deleção lógica (marca `deleted_at`)
- `get_primary_user_entity()` — busca entidade primária do usuário (tipo=person, is_primary_user=true)

**Campos:**
- `upsert_field()`, `get_field()`, `get_entity_fields()`

**Relações:**
- `upsert_relation()`, `find_relation()`, `expire_relation()`, `get_relations()`

**Busca:**
- `search_entities()` — usa similaridade trigram (`%` operator do PostgreSQL), alias e nome exato com score
- `traverse()` — travessia recursiva de grafo via CTE com max_depth, direção e filtros por tipo

### 4. `bootstrap.py` — Inicialização de Workspace

`PKSBootstrap.ensure_workspace()` para novos usuários:

1. Busca `display_name`, `username` ou `email` do usuário
2. Cria `pks_workspace` com o nome derivado
3. Cria 7 spaces padrão: **Perfil**, **Pessoas**, **Projetos**, **Dispositivos**, **Academia**, **Documentos**, **Preferências**
4. Cria entidade primária do usuário (tipo=person, is_primary_user=true, aliases: "eu", "me", "usuário principal")

### 5. `entity_resolver.py` — Resolução de Queries

`EntityResolver.resolve()` — converte queries em linguagem natural em entidades rankeadas:

- Busca via `repository.search_entities()`
- Aplica **recency boost** (+0.05 se `last_used_at` existe)
- Aplica **confidence boost** (até +0.05 baseado na confidence da entidade)
- Ordena por score decrescente

`resolve_one()` — retorna o melhor match acima de `min_score` (default 0.25).

### 6. `context_builder.py` — Formatação para Prompt

`ContextBuilder.format()` formata `RetrievalResult` em texto markdown para injeção em prompt:

```
### João [person]
- full_name: João Silva
- owns -> PC principal
- works_on -> Plano de treino de academia

### PC principal [device]
- cpu: Intel i7-12700K
- memory: 32GB
```

**Regras de sensibilidade:**
- `secret` → nunca mostrado
- `sensitive` → mostrado apenas se há match direto com termos da query
- `private`/`public`/`internal` → sempre mostrados

### 7. `retriever.py` — Pipeline de Recuperação

`PKSRetriever.retrieve()` — caminho síncrono para `DIRECT_RESPONSE`:

1. Garante workspace existe (via bootstrap)
2. Extrai keywords da mensagem (até 6 termos, filtrando stopwords em português)
3. Para cada keyword: resolve entidades via `EntityResolver.resolve()` (limite 3 por keyword)
4. Adiciona entidades "globais" (person, health_state, restriction, routine, preference, goal) — até 12
5. Para cada candidato: busca neighborhood (entity + fields + relations)
6. Atualiza `last_used_at` das entidades
7. Retorna `RetrievalResult` e string de contexto formatada

### 8. `audit.py` — Logging de Auditoria

`AuditLogger.log()` insere em `pks_audit_log`:
- `actor_type`, `actor_id`, `action`, `target_type`, `target_id`
- `before` / `after` (JSON completo)
- `reason`, `confidence`, `source_message_id`, `request_id`

Todas as operações de escrita passam por aqui.

### 9. `memory_planner.py` — Planejador LLM

`PKSMemoryPlanner.plan()` — usa LLM para propor operações de escrita:

- Prompt em português (SYSTEM) com ontologia completa e regras de domínio
- Contexto PKS atual injetado para referência
- Retorna lista de `PKSOperation` em JSON
- Provider: tenta `deepseek` primeiro, fallback para provider padrão
- Parse: extrai JSON da resposta, valida cada operação com Pydantic, descarta inválidas

**Regras do prompt:**
- Salvar apenas fatos pessoais, duráveis e úteis affirmados pelo usuário
- `confidence >= 0.80` apenas para asserções claras
- `sensitivity "private"` por padrão; `"sensitive"` para saúde, finanças, dados íntimos
- Domínio academia: treino semanal = `routine`, dias = `workout`, grupos = `muscle_group`, exercícios = `exercise`

### 10. `writer.py` — Pipeline de Aprendizado

`PKSWriter.learn()` — fluxo padrão pós-resposta:
1. Planeja operações via `PKSMemoryPlanner.plan()`
2. Filtra por `confidence >= 0.80` (`MIN_APPLY_CONFIDENCE`)
3. Para cada operação: valida ontologia, aplica via repository, loga via audit

`PKSWriter.learn_before_response()` — escritas diretas pré-resposta:
- Sem LLM — usa regex patterns para extrair fatos diretos
- **Declarações de nome**: "meu nome é X", "eu me chamo X", "eu sou X"
- **Declarações de dispositivo**: "meu pc tem cpu ..."
- **Declarações de parentesco**: "minha irmã se chama X"
- **Fatos de treino**: parsing de treinos com exercícios, séries, reps, peso

**Domínio academia (regras de extração):**
- `_extract_weekly_split()` — parseia "na segunda faço peito e tríceps, na quarta faço perna"
- `_extract_muscle_groups()` — mapeia tokens português para nomes canônicos
- `_extract_workout_exercises()` — regex para supino com peso/séries/reps, crucifixo, tríceps corda

### 11. `service.py` — Fachada

`PKSService` é o ponto de entrada único:

```python
async def retrieve_context(user_id, keywords, user_message) -> (dict, str)
async def ensure_user_pks(user_id) -> UUID
async def learn_before_response(user_id, user_message, source_message_id) -> dict
async def learn(user_id, user_message, assistant_response, ...) -> list[dict]
async def search_entities(user_id, query) -> list[dict]
```

---

## Fluxos de Dados

### Recuperação (leitura)

```
User message → PKSRetriever.retrieve()
  → _keywords() — extrai termos da mensagem
  → EntityResolver.resolve() — busca entidades via repository.search_entities()
  → build Neighborhoods (fields + relations)
  → ContextBuilder.format()
→ context string para injeção em prompt
```

### Aprendizado (escrita)

```
User message + assistant response → PKSWriter.learn()
  → PKSMemoryPlanner.plan() [LLM]
  → para cada PKSOperation (confidence >= 0.80):
      → OntologyValidator.canonicalization
      → _apply_upsert_entity / _apply_upsert_field / _apply_upsert_relation / _apply_soft_delete_entity
      → AuditLogger.log()
```

### Aprendizado pré-resposta (escrita direta sem LLM)

```
User message → PKSWriter.learn_before_response()
  → _extract_name_statement() — regex "meu nome é X"
  → _extract_device_statement() — regex "meu pc tem..."
  → _extract_relative_statement() — regex "minha irmã se chama X"
  → _apply_workout_direct_writes() — parsing de treino semanal
  → Repository upserts diretos
```

### Bootstrap (primeiro acesso)

```
user_id → PKSBootstrap.ensure_workspace()
  → cria pks_workspace
  → cria 7 spaces padrão
  → cria entidade primária do usuário (type=person)
```

---

## Índice de Ontologia

### Aliases de Tipo de Entidade

| Alias | Tipo Canônico |
|-------|--------------|
| pessoa, contato, amigo, familia | person |
| projeto | project |
| empresa, trabalho | company |
| pc, notebook, computador, desktop, celular, telefone, dispositivo | device |
| stack, framework, orm, runtime, linguagem, database, banco de dados | technology |
| documento | document |
| treino, academia | workout |
| exercicio | exercise |
| musculo, grupo muscular | muscle_group |
| refeição | meal |
| comida, alimento | food |
| suplemento | supplement |
| saúde | health_state |
| restrição | restriction |
| rotina | routine |
| objetivo, meta | goal |
| preferência | preference |
| veículo, carro | vehicle |
| imóvel, casa | property |
| conta | account |
| pet, animal | pet |
| lugar, local | place |
| software, app, aplicativo | software |
| problema, defeito, bug, erro | hardware_problem |

### Aliases de Tipo de Relação

| Alias | Tipo Canônico |
|-------|--------------|
| possui, tem, dono | owns |
| trabalha em, participa | works_on |
| usa stack, stack | uses_stack |
| desenvolvido em | developed_on |
| usado para | used_for |
| contém, tem exercício | has_exercise |
| atinge, trabalha | targets |
| usa equipamento | uses_equipment |
| irmão | sibling_of |
| filho | child_of |
| pai | parent_of |
| pertence | belongs_to |
| relacionado | related_to |
| menciona | mentions |
| localizado | located_in |
| membro | has_member |
| realizou | performed |

### Espaços Padrão

| Nome | Tipo |
|------|------|
| Perfil | profile |
| Pessoas | people |
| Projetos | projects |
| Dispositivos | devices |
| Academia | fitness |
| Documentos | documents |
| Preferências | preferences |

---

## Constantes Importantes

```python
MIN_APPLY_CONFIDENCE = 0.80   # confidence mínima para aplicar operação
MAX_PLANNER_OPERATIONS = 40  # limite de operações por planejamento
GLOBAL_TYPES = ("person", "health_state", "restriction", "routine", "preference", "goal")
SENSITIVITY_LEVELS = {"public", "internal", "private", "sensitive", "secret"}
VALUE_TYPES = {"text", "number", "boolean", "date", "json"}
```
