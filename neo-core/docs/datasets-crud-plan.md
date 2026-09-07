# Plano — Skills Instaláveis: Agregador de Tools + Cadastros Generativos

> Status: **SÓ PLANO** · 2026-09-06 (rev. 2) · Autor: Claude + Daniel
>
> Objetivo: uma skill é um **pacote instalável**. Ela se descreve num `.md`; na
> instalação o neo-core **analisa o que ela precisa**, cria as tabelas, liga as
> tools e publica a tela de CRUD — **uma única vez**. Depois disso a skill é
> parte do Neo: some do caminho de instalação e vira capacidade.
>
> Caso motivador (Daniel): "quero atender clientes — cadastro um skill e ele já
> me gera um CRUD de produtos, salva no banco e busca pra mim quando eu precisar".
>
> **Rev. 2** acrescenta as três peças que faltavam: o **agregador de tools** (§2),
> o **ciclo de instalação** (§3) e o **orçamento de tokens** (§8).

---

## 0. O que já existe (e por isso este plano é plumbing, não invenção)

| Peça existente | O que já resolve | Onde |
|---|---|---|
| `SkillLibrary` (SKILL.md) | catálogo no prompt, hot-reload por mtime, CRUD pelo painel, validação na escrita | `core/skills/library.py` |
| **Tool gating** | ler uma skill `tool_backed` DESTRAVA as tools dela só naquele turno | `modules/desktop/ai_service.py:5058` |
| `ToolRegistry` | índice global nome → `ToolSchema`, recusa duplicata | `core/tools/registry.py` |
| `ToolExecutor` | despacho por `module`, com os **dois** contratos de executor | `core/tools/executor.py` |
| `_SKILL_OWNED_TOOL_PREFIXES` | tira tools do palette do classificador | `core/router/intent_router.py:224` |
| **Service Gateway** | precedente de tools genéricas por slug + registry cacheado no banco | `core/gateway/registry.py` |
| `fin_*` | tabelas próprias, centavos inteiros, retorno "CONFERIDO" lido do banco | `modules/finance/` |
| `sql_query_ro` | SELECT arbitrário seguro sobre o banco do Neo | `modules/sqlro/executor.py` |
| File Store + `file_stage` | anexo → sandbox, blobs versionados | `core/files/` |

**A parede** está em `library.py:507`:

```
"Tools não registradas no neo-core: … Uma tool nova exige código (modules/<x>/ + api/main.py)."
```

O agregador (§2) é o que derruba essa parede **sem** virar buraco de segurança.

---

## 1. Princípios de design

1. **Instalar é um evento; usar é o estado.** Toda análise cara (o que a skill
   precisa, qual DDL rodar, qual tela publicar) acontece **uma vez**, na
   instalação. Em runtime não há descoberta: a skill já é parte do Neo.
2. **Um índice, muitas origens.** Tools nascem em `modules/*`, no pacote de uma
   skill ou do gerador de cadastros. O agregador dá **nome único global** e um
   endereço só para todas.
3. **Schema tipado sempre que possível; dispatcher genérico só na cauda.** Este
   repo já pagou caro por tool sem schema (§2.4). O agregador **desbloqueia
   schemas reais** por padrão; `tool_run(nome, args)` é fallback, não default.
4. **Custo zero até ser usado.** Nenhuma skill instalada paga prompt só por
   existir (§8). Hoje paga — em dois lugares.
5. **O `.md` é a fonte da verdade.** Frontmatter declara requisitos; corpo ensina
   o uso. Editar o `.md` (por você ou, no futuro, pelo próprio Neo) muda o plano
   de instalação — com gate por tipo de mudança (§3.4).
6. **Tabela real, não JSONB.** Cadastro de negócio vive de `SUM`/`GROUP BY`; é o
   mesmo motivo de `fin_*` não morar no PKS. Efeito colateral valioso: o
   `sql_query_ro` já responde análise sobre esses dados sem código novo.
7. **Nunca afirmar sem conferir.** Todo write devolve a linha relida do banco
   (lição da skill HA, já codificada no `fin_edit_transaction`).

---

## 2. O agregador de tools (`core/tools/aggregator.py`)

### 2.1 O que é

Um índice de **bindings**, acima do `ToolRegistry` (não no lugar dele — o
registry continua sendo quem o `ToolExecutor` consulta):

```python
@dataclass
class ToolBinding:
    name: str              # único global. ^[a-z][a-z0-9_]{2,48}$ (o provider exige [A-Za-z0-9_-])
    summary: str           # UMA linha (≤90 chars) — é isto que o índice paga
    schema: ToolSchema     # assinatura completa; só entra no prompt quando desbloqueada
    origin: Literal["core", "dataset", "skill"]
    source: str            # "modules/finance/tools.py" | "skills/produtos/tools.py" | "datasets"
    module: str            # chave de despacho no ToolExecutor
    owner_skill: str | None
    tags: list[str]
```

Três origens, um endereço:

| origem | de onde vem | quando entra no índice |
|---|---|---|
| `core` | `register_many(...)` no `api/main.py` | boot — o agregador **espelha** o `ToolRegistry` |
| `dataset` | as `data_*` genéricas (§6) | boot |
| `skill` | `skills/<name>/tools.py` de um pacote | **instalação** (§3), import lazy |

### 2.2 Nome único e colisão

Nome global é o próprio `ToolSchema.name` — sem namespace com ponto, porque a API
dos providers só aceita `[A-Za-z0-9_-]`. Colisão na instalação **recusa a
instalação** com a mensagem de qual skill já detém o nome. Não renomeamos em
silêncio: o corpo do `.md` cita a tool pelo nome, e um rename automático quebraria
a instrução que o modelo vai ler. Convenção sugerida (não obrigatória):
`<dominio>_<verbo>` — `produtos_importar`, `nfe_emitir`.

### 2.3 As duas formas de o modelo chegar numa tool

**Nível A — desbloqueio (default).** `skill_read("produtos")` → o agregador
resolve os bindings daquela skill e injeta os **schemas reais** no toolset do
turno. É o gating que já roda hoje em `ai_service.py:5058`, agora alimentado pelo
agregador em vez de pela lista literal do frontmatter. O modelo chama a tool
nativamente, com o provider validando os argumentos.

**Nível B — dispatcher genérico.** Duas tools no índice:

| tool | assinatura | para quê |
|---|---|---|
| `tool_find` | `(busca)` | procura no índice por nome/tag/summary. Devolve `nome — summary`, sem schema. É o que torna achável uma tool "espalhada pelo código" |
| `tool_run` | `(nome, args)` | executa qualquer binding pelo nome único |

`tool_run` é o que o Daniel descreveu: *uma função, passa o nome, funciona*.
Serve para (i) a cauda longa que não justifica schema próprio, (ii) tools de
skill carregadas **depois** do boot, (iii) chamada pontual sem ler a skill inteira.
Ele valida `args` contra o `schema` do binding e, em erro, devolve **a assinatura
completa** — erro que não ensina vira retry infinito.

### 2.4 Por que o Nível B não é o default (análise honesta)

Sem schema tipado o modelo não tem o provider validando nada e **inventa
argumento**. Não é hipótese — é o histórico deste repo: `entity_id` inventado na
skill HA, `file_path`/`file_id` inventados pelo classificador, as 7 client tools
fantasma que travavam 300s sem executor. `tool_run(nome, args)` reintroduz
exatamente essa superfície.

Mitigação, e é o que torna o desenho aceitável: **a assinatura chega antes da
chamada**. O agregador gera um bloco de assinaturas e o anexa ao corpo da skill
lida (§7.2) — o modelo lê a assinatura no mesmo round em que decide usar. Custo
em tokens: praticamente igual ao do schema nativo, com a diferença de que o
nativo ainda ganha a validação do provider. Daí a regra:

> Nível A quando a tool é conhecida no boot. Nível B para o resto.

### 2.5 A linha dura: `tools.py` de skill é instalação HUMANA

Uma skill pode trazer o próprio `tools.py`, importado pelo instalador **dentro do
processo do neo-core** — que tem o banco, a chave do cofre (`NEO_CRED_KEY`) e a
LAN. Isso é execução de código arbitrário com privilégio total.

- Skill com `tools.py` **nunca** é instalável por tool do modelo (`skill_write`
  §7.4 continua proibido de criar `tools.py`).
- Instalação exige `POST /api/v1/skills/{name}/install` com confirmação no painel
  e mostra o diff do arquivo antes.
- Flag própria, default off: `NEO_SKILL_PY_TOOLS_ENABLED`.
- O import é lazy e o erro é isolado: `tools.py` quebrado marca a skill como
  `falhou` e **não derruba o boot** (mesma política fail-safe do loader).

Sem isso, "skill traz .py" é uma porta para o Neo se auto-conceder qualquer
capacidade — inclusive ler o próprio cofre.

---

## 3. Ciclo de vida: instalar uma vez, funcionar como um só

### 3.1 O pacote

```
skills/<name>/
  SKILL.md            # frontmatter (requisitos) + corpo (instruções) — a fonte da verdade
  datasets/*.yaml     # opcional: definição dos cadastros (ou inline no frontmatter)
  tools.py            # opcional: tools próprias (instalação humana — §2.5)
  references/*.md     # detalhe lido sob demanda
  scripts/*.py        # executados no sandbox (§9.7)
```

### 3.2 As três etapas

```
  plan ──────────► apply ──────────► verify
 (dry-run,        (idempotente,     (relê do banco:
  legível)         transacional)     bate ou não bate?)
```

**`plan(name) -> InstallPlan`** — lê o `.md`, compara com o estado atual e
descreve **em português** o que falta, sem tocar em nada:

```
Instalar 'produtos':
  + criar cadastro `produtos` → tabela neo_data.produtos_k3f9
      nome (texto, obrigatório) · preco (dinheiro) · estoque (inteiro)
      · categoria (opção: camiseta|caneca|adesivo) · foto (arquivo)
  + ligar tools: data_list, data_get, data_create, data_update, data_delete
  + publicar tela "Produtos" no painel (/dados/produtos)
  ~ nenhuma migração destrutiva
```

**`apply(plan)`** — idempotente por construção (rodar duas vezes = mesmo estado):
DDL no schema `neo_data`, bindings no agregador, registro da tela. Falha no meio
deixa a skill em `falhou` com o motivo, nunca em meio-instalada silenciosa.

**`verify(name) -> InstallState`** — relê do banco: tabela existe? colunas
conferem com o schema? bindings resolvem no agregador? Estados:
`ok` · `degradada` (com motivo) · `falhou`. É o mesmo princípio do "CONFERIDO"
das finanças, aplicado à instalação — sem `verify`, "instalei" é uma afirmação
sem prova, e este repo já pagou por isso.

### 3.3 Estado persistido (`skill_install`, alembic-managed)

| coluna | nota |
|---|---|
| `name` PK | nome da skill |
| `state` | `planejada` · `instalada` · `desatualizada` · `falhou` · `desinstalada` |
| `install_hash` | sha256 do frontmatter normalizado — detecta drift (§3.4) |
| `installed_version` / `installed_at` | |
| `datasets` JSONB | slugs + `table_name` criados por esta skill |
| `tools` JSONB | nomes ligados no agregador |
| `screens` JSONB | telas publicadas |
| `last_error` | motivo quando `falhou`/`degradada` |

O `SkillLibrary` já recarrega por mtime; o instalador **não** roda no reload. O
reload só compara `install_hash` e, se mudou, marca `desatualizada`.

### 3.4 Drift — quando o `.md` muda (inclusive pelo próprio Neo)

Esta é a parte que sustenta o "no futuro o Neo edita o arquivo e melhora a própria
habilidade". Editar o `.md` muda o plano de instalação, então a mudança é
classificada:

| tipo de mudança | exemplo | política |
|---|---|---|
| **cosmética** | corpo, keywords, description | aplica sozinho no reload; nada de DDL |
| **aditiva** | campo novo, tela nova, tool já existente | auto-aplica se `NEO_SKILL_AUTO_INSTALL=true`, senão fica `desatualizada` com o plano pronto |
| **destrutiva** | remover campo, trocar tipo, remover dataset | **nunca** automática — exige apply humano no painel, com preview do que se perde |

### 3.5 Desinstalar

Remove bindings, telas e tira do catálogo. **A tabela nunca é dropada
automaticamente** — vira cadastro *órfão*, visível no painel, e só some com
confirmação explícita. Dado do usuário não desaparece porque uma skill saiu.

---

## 4. Cadastros: onde os dados moram

### 4.1 Nome da tabela (o "código no final")

`neo_data.<slug>_<code>` — `code` = 4 chars de `secrets.token_hex(2)`.

- `slug`: `^[a-z][a-z0-9_]{0,32}$`, derivado do nome do cadastro.
- `code`: evita colisão entre duas skills que querem `produtos`, e evita que
  recriar um cadastro excluído colida com resíduo.
- O nome físico **nunca** vem do cliente/LLM: é montado pelo backend e gravado em
  `neo_data_dataset.table_name`. Nome que não está no registry é recusado antes
  de tocar o banco.
- Tudo em `neo_data.*`, nunca no `public` — protege as tabelas do Neo e mantém o
  `alembic autogenerate` honesto (§9.4).

### 4.2 Tabela de definição (`neo_data_dataset`, alembic-managed, no `public`)

`id` UUID PK · `user_id` UUID idx · `slug` VARCHAR(40) · `code` VARCHAR(8) ·
`table_name` VARCHAR(64) · `title` VARCHAR(80) · `description` TEXT ·
`schema_json` JSONB (§5) · `skill_name` VARCHAR(40) NULL · `enabled` BOOL ·
`schema_version` INT · `created_at`/`updated_at` TIMESTAMPTZ.

Mais `neo_data_audit` (quem/quando/o quê em delete e em ALTER destrutivo).

### 4.3 Espinha fixa de toda tabela de cadastro

```sql
id          UUID PRIMARY KEY DEFAULT gen_random_uuid()
user_id     UUID NOT NULL
created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
deleted_at  TIMESTAMPTZ NULL          -- soft delete, igual PKS e fin_*
-- + as colunas do schema
```

Soft delete por padrão: o cérebro apagando linha é o risco mais alto do
subsistema (§9.2); `deleted_at` deixa desfazer.

---

## 5. O schema (contrato único backend ↔ painel ↔ cérebro)

```jsonc
{
  "version": 1,
  "fields": [
    { "name": "nome",      "type": "texto",   "label": "Nome",  "required": true, "max": 120 },
    { "name": "preco",     "type": "dinheiro","label": "Preço", "required": true },
    { "name": "estoque",   "type": "inteiro", "label": "Estoque", "default": 0 },
    { "name": "categoria", "type": "opcao",   "label": "Categoria",
      "options": ["camiseta", "caneca", "adesivo"] },
    { "name": "ativo",     "type": "booleano","label": "Ativo", "default": true },
    { "name": "foto",      "type": "arquivo", "label": "Foto" },
    { "name": "fornecedor","type": "relacao", "label": "Fornecedor", "dataset": "fornecedores" }
  ],
  "list_columns": ["nome", "preco", "estoque", "ativo"],
  "search_fields": ["nome", "categoria"],
  "order_by": "nome asc",
  "label_field": "nome"        // como uma linha é citada em texto e em relações
}
```

O mesmo JSON alimenta DDL, validação de write, o bloco entregue ao cérebro e a
renderização do painel. Campo novo aparece nos quatro de uma vez.

### 5.1 Tipos (conjunto FECHADO — é o que torna o DDL seguro)

| type | Postgres | painel | validação |
|---|---|---|---|
| `texto` | `VARCHAR(n)` (n≤500, default 200) | input | tamanho |
| `texto_longo` | `TEXT` | textarea | teto 32 KB |
| `inteiro` | `BIGINT` | number | |
| `numero` | `NUMERIC(18,4)` | number | |
| `dinheiro` | `BIGINT` (centavos) | input BR | **centavos inteiros, nunca float** — regra de `fin_*` |
| `booleano` | `BOOLEAN` | switch | |
| `data` | `DATE` | datepicker | |
| `data_hora` | `TIMESTAMPTZ` | datetime | |
| `opcao` | `VARCHAR(60)` + `CHECK IN (…)` | select | pertence ao enum |
| `multi_opcao` | `TEXT[]` | multiselect | cada item no enum |
| `relacao` | `UUID` + FK p/ outro dataset | autocomplete | dataset existe |
| `arquivo` | `UUID` → `stored_file.id` | upload | reusa o File Store |
| `json` | `JSONB` | editor cru | JSON válido |

Tipo desconhecido: backend **recusa** na instalação; painel **degrada para input
de texto** (nunca quebra a tela quando o backend evolui primeiro).

---

## 6. Tools de dados (`module="datasets"`, server-side)

Prefixo `data_` entra em `_SKILL_OWNED_TOOL_PREFIXES` — o classificador nunca as
vê nem roteia pra elas.

| tool | assinatura | nota |
|---|---|---|
| `data_schema` | `(dataset)` | campos, tipos, opções |
| `data_list` | `(dataset, busca?, filtro?, ordem?, limite?, pagina?)` | `filtro` é objeto `{campo: valor}` / `{campo: {op, valor}}` — **nunca SQL cru** |
| `data_get` | `(dataset, id)` | |
| `data_create` | `(dataset, campos)` | devolve a linha **relida do banco** |
| `data_update` | `(dataset, id, campos)` | idem; só campos enviados |
| `data_delete` | `(dataset, id, confirmar)` | soft delete; sem `confirmar` devolve *preview* |
| `data_define` | `(slug, titulo, descricao, campos[], modo)` | meta-tool; destravada só pela meta-skill `criar-cadastro` (§7.3) |

Seis tools cobrem 1 ou 100 cadastros — é o precedente do
`service_request(service=…)`. **Alternativa rejeitada**: registrar
`produtos_create`, `produtos_list`, … em runtime. Cada cadastro custaria ~5
schemas, `ToolRegistry.register` recusa duplicata (`ValueError`), e o registry é
global no processo — com `--reload` ligado (que no setup do Daniel é *deploy*)
isso vira bug de estado.

**Regras de retorno:**

- Todo write devolve `{"ok": true, "id": …, "conferido": {<linha relida>}}`.
  Sem `conferido`, o `.md` manda **não afirmar** que salvou.
- `dataset` desconhecido → erro que **lista os cadastros existentes** (o modelo
  diz `produto` quando o slug é `produtos`; resolvemos exato e, no singular/plural,
  por aproximação — lição de `neo-file-id-name-resolution`).
- Campo desconhecido → erro que **lista os campos válidos**.

---

## 7. Integração com a biblioteca de skills

### 7.1 Frontmatter: os requisitos declarados

```yaml
---
name: produtos
description: >
  Catálogo de produtos da loja: cadastrar, consultar preço/estoque, atualizar.
  Use quando o Daniel falar de produto, preço, estoque ou pedido de cliente.
keywords: [produto, produtos, preço, estoque, catálogo, sku]
kind: tool_backed
discovery: keyword          # ← NOVO (§8): keyword | always | oculta
group: cadastros            # ← NOVO (§8): colapsa N skills numa linha de catálogo
datasets: [produtos]        # ← NOVO: cria/liga cadastro na instalação
screens:                    # ← NOVO: tela publicada no painel
  - { dataset: produtos, titulo: Produtos, icone: box }
tools: []                   # tools extras do agregador, por nome único
enabled: true
version: 1
---
```

Pontos de código em `core/skills/library.py`:

1. `SkillEntry` ganha `datasets`, `screens`, `discovery`, `group` + parse.
2. `_requires_ok`: skill `instalada` mas com dataset ausente → fora do catálogo
   (mesma mecânica do `requires: workbench`). Skill nunca instalada também fica
   fora — aparece no painel como "pendente de instalação", não no prompt.
3. `tools_for(name)` passa a perguntar ao **agregador** (§2), somando as `data_*`
   quando há `datasets:`. É isto que faz `write_skill` deixar de recusar: a skill
   ganha tools sem código novo, sem abrir porta para tool arbitrária.

### 7.2 O que o `skill_read` devolve depois disso

Corpo do `.md` **+ blocos gerados pelo agregador** (não escritos à mão):

```
## Cadastro `produtos` (neo_data.produtos_k3f9)
nome (texto, obrigatório) · preco (dinheiro, centavos) · estoque (inteiro)
· categoria (opção: camiseta|caneca|adesivo) · ativo (booleano) · foto (arquivo)

## Tools desta skill
data_list(dataset, busca?, filtro?, ordem?, limite?, pagina?)
data_create(dataset, campos)  → devolve `conferido` com a linha do banco
produtos_importar(arquivo_id, mapa)   [via tool_run]
```

~150–250 tokens por cadastro, pagos **só** no `skill_read`. Teto: 3 datasets
inline; do 4º em diante vira uma linha e o modelo chama `data_schema`.

### 7.3 Meta-skill `criar-cadastro`

Skill embarcada, `tools: [data_define]`, `discovery: keyword`. Ensina o cérebro a:
(a) transformar a descrição em português em campos tipados, (b) **perguntar** o
ambíguo (preço por unidade? guarda foto?), (c) chamar `data_define`, (d) chamar
`skill_write` (§7.4), (e) disparar `install`, (f) avisar que a tela está no painel.

```
Daniel: "cria um cadastro de produtos: nome, preço, estoque, categoria e foto"
  → skill_read("criar-cadastro")
  → data_define(slug="produtos", campos=[…])
  → skill_write(name="produtos", datasets=["produtos"], screens=[…])
  → install("produtos")  → plan → apply → verify=ok
  → catálogo/painel já têm "produtos" na PRÓXIMA mensagem
Daniel, 5 min depois: "quanto tá a caneca preta?"
  → keyword "produto/preço" → skill_read("produtos") → data_list(busca="caneca preta")
```

### 7.4 `skill_write` — a tool que fecha o ciclo

Wrapper fino sobre `SkillLibrary.write_skill`, com a **mesma** validação.
Restrições inegociáveis: só `instructional` ou `tool_backed`-com-`datasets`;
**não** pode criar `tools.py` (§2.5); **não** pode declarar `tools:` que não
estejam já no agregador; nunca sobrescreve sem `overwrite=true` + confirmação;
teto de N skills por sessão (loop de skill criando skill é real).

> Decisão mais delicada do plano: dar ao Neo caneta no catálogo que molda o
> prompt dele. Mitigações em §9.6.

---

## 8. Orçamento de tokens: parar de indexar skill à toa

### 8.1 Onde vaza hoje (medido no código, não estimado)

| vazamento | custo | onde |
|---|---|---|
| `catalog_note` lista **todas** as skills, 1 linha ≈ 20 tokens, em **toda** mensagem cognitiva. Só prefiltra acima de 30 | 30 skills ≈ **600–700 tokens/mensagem** | `library.py:678` |
| `router_note` repete até **12 skills + keywords** em **toda** mensagem do classificador | ≈ **150–250 tokens/mensagem** | `library.py:877` |

São **dois** caminhos, e o segundo passa despercebido. Com 20 cadastros de
cliente instalados isso vira ~1k tokens por mensagem — pagos mesmo quando a
conversa é sobre outra coisa.

### 8.2 As quatro camadas

**Camada 0 — `discovery` no frontmatter.** `keyword` (default para skill
instalada) · `always` (as poucas que precisam estar sempre visíveis) · `oculta`
(só por nome exato / `tool_find`).

`catalog_note` passa a montar: as `always` **+** as `keyword` que casaram *nesta*
mensagem. Conversa fora do assunto = catálogo praticamente vazio. A diferença
para o prefiltro de hoje é que o prefiltro só liga acima de 30 skills; aqui é o
comportamento padrão desde a primeira.

**Camada 1 — `group`.** N skills do mesmo grupo colapsam em uma linha:

```
- cadastros — produtos, clientes, pedidos. Leia `skill_read('<nome>')` para usar.
```

20 cadastros passam de ~400 tokens para ~25.

**Camada 2 — autoload (já existe).** Keyword forte injeta corpo + tools já no
round 1 (`autoload_for`), economizando o round-trip inteiro do `skill_read`.

**Camada 3 — índice semântico (quando passar de ~50 skills).** Keyword não
escala: ninguém acerta as keywords de 50 cadastros. O pgvector já está no banco
(embeddings de mensagem, 384d) — ranquear as 3 skills mais próximas da mensagem e
só essas entram no catálogo. Fica para a F6; keyword + grupo resolvem até lá.

**Teto por tokens, não por entradas.** Trocar `skills_catalog_max_entries` (30)
por `skills_catalog_max_tokens` — contar o que se paga, não quantas linhas são.

### 8.3 O mesmo tratamento no `router_note`

O classificador não precisa saber de cadastro nenhum: pedido sobre produto é
`DIRECT_RESPONSE` de qualquer jeito. Skills com `group` entram no `router_note`
como **uma** entrada de grupo, e `discovery: oculta` não entra.

---

## 9. Impacto no Neo — análise honesta

### 9.1 Prompt e roteamento (**baixo, com §8; alto sem**)
Sem a §8 este plano **piora** o Neo: cada cadastro novo é imposto ao prompt para
sempre, nos dois caminhos. Com a §8, o prompt de uma conversa que não toca em
cadastro fica **menor** que o de hoje (o catálogo deixa de listar tudo).

Risco que sobra: a `description` gerada por LLM é a única coisa que o cérebro vê
sempre — descrição ruim = skill que nunca dispara ou dispara errado. A meta-skill
tem de exigir o formato "O QUE + QUANDO", e o painel deixa corrigir (já deixa).

`data_` **tem** que entrar em `_SKILL_OWNED_TOOL_PREFIXES`; sem isso o
classificador roteia direto para `data_create` e inventa dataset e campos — o
mesmo bypass que fazia a skill HA alucinar `entity_id`.

### 9.2 Escrita autônoma no banco (**alto — o risco principal**)
- **Soft delete sempre**; `data_delete` sem `confirmar` devolve preview (padrão
  `fin_edit_transaction(preview=true)`).
- **Read-back obrigatório** no retorno — sem isso volta o "salvei!" sem ter salvo.
- **Audit** em delete e em ALTER destrutivo.
- **`confirmar` não é do modelo**: só depois de a tool recusar E o usuário
  confirmar em palavras. Reaproveitar a redação do SKILL.md de finanças.

### 9.3 DDL em runtime no banco de produção (**médio, mitigável**)
Mesmo banco das conversas, do PKS e do ledger. Camadas:
1. `slug`/nome de campo por regex fechada + blocklist (reservadas do PG e nomes
   da espinha §4.3).
2. Tipos **só** do mapa fechado (§5.1).
3. DDL restrito a `neo_data` — prefixo fixo no código, não parametrizado.
4. `table_name` só existe se veio do `neo_data_dataset`.
5. ALTER destrutivo nunca é oferecido ao modelo; só `add_field` é (§3.4).

### 9.4 Alembic (**1 mudança obrigatória, senão quebra feio**)
`alembic/env.py` hoje é `target_metadata = SQLModel.metadata` **sem
`include_object`**. Um `autogenerate` depois disto geraria `DROP TABLE` para toda
tabela de cadastro:

```python
def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and getattr(obj, "schema", None) == "neo_data":
        return False
    return True
```

Mais `include_schemas` restrito ao `public` no `context.configure`.

### 9.5 Fronteira com PKS e ledger (**baixo, mas precisa estar escrito**)
Cadastro com campo `dinheiro` **não** viola o `ledger_guard` (ele protege o ledger
*pessoal* de virar memória semântica). O que não pode é resultado de `data_*` ser
resumido para dentro do `pks_remember` — "o produto X custa 49,90" vira saldo
fantasma da família do `balance=980`. Regra no `.md`: **dado de cadastro se
consulta, não se memoriza.**

### 9.6 Neo escrevendo o próprio catálogo (**médio**)
- Só cria skill com `datasets:`; nunca `tools.py`; nunca `tools:` fora do agregador.
- Skill criada por tool nasce com `origem: agente` no frontmatter e destacada no
  painel — revisão humana **depois**, não antes (senão o ciclo perde a graça).
- Mudança destrutiva no `.md` nunca auto-aplica (§3.4).

### 9.7 Scripts `.py` no sandbox (**restrição dura, precisa decisão**)
O `neo-workbench` roda em rede `internal` com saída só pelo proxy, que **recusa
destino privado** (`docker-compose.yml:270`). Ou seja:

> **script no sandbox NÃO alcança o Postgres do Neo.** Nunca alcançou — é o
> motivo de a skill `postgres-neo` ser `tool_backed` e não `scripted`.

Isto **não** vale para o `tools.py` da §2.5: aquele roda dentro do neo-core, com
acesso total — são coisas diferentes e a confusão entre as duas é perigosa.

- **(A) recomendada** — `data_export(dataset, filtro) → File Store → file_stage →
  /workspace/…csv`. O script trabalha sobre o arquivo (relatório, gráfico,
  importação em massa via `data_import`). Reusa maquinário existente, zero furo.
- (B) abrir caminho HTTP autenticado do `workbench-proxy` para a API de datasets.
  Mais poderoso, fura o isolamento que hoje separa código gerado por LLM da LAN.

### 9.8 neo-panel (**positivo a médio prazo**)
`DataGrid` + `DataForm` dirigidos pelo schema substituem N telas à mão: ~700–900
linhas uma vez contra ~400 por cadastro. Mais uma tela de **Instalação** (plano,
apply, estado `ok/degradada/falhou`, órfãos). Tipo desconhecido tem que degradar,
não quebrar.

### 9.9 O multiplicador que ninguém pediu
Com tabelas reais, `sql_query_ro` + skill `postgres-neo` já respondem "qual
produto mais vendeu", "quanto de estoque parado" — **sem escrever nada**. Só
garantir `USAGE` no schema `neo_data` para a role do `SQLRO_DSN`.

---

## 10. Fases

| Fase | Entrega | Como se prova |
|---|---|---|
| **F0** | Migração: schema `neo_data` + `neo_data_dataset` + `skill_install` + `include_object` no alembic | `make migrate` limpo; `autogenerate` seguinte não propõe DROP |
| **F1** | **Agregador** (`ToolBinding`, espelho do `ToolRegistry`, `tool_find`) + `tools_for` passa a consultá-lo — **sem** mudar comportamento visível | catálogo de tools por origem via endpoint; suíte atual verde |
| **F2** | `DatasetRegistry` + `DatasetSchema` + DDL builder + `data_define` (só create), endpoints `/api/v1/datasets` — sem tools no cérebro | testes de injeção de identificador; criar cadastro por curl |
| **F3** | **Instalador**: `plan`/`apply`/`verify`/`uninstall` + estado + tela de instalação no painel | instalar/desinstalar 2× seguidas → mesmo estado (idempotência) |
| **F4** | `DATA_TOOLS` + executor + prefixo no router + `datasets:`/`screens:` no frontmatter + blocos do agregador no `skill_read` | E2E: skill à mão → "cadastra o produto X" → linha no banco, com `conferido` |
| **F5** | **Orçamento de tokens** (§8): `discovery`, `group`, teto por tokens, `router_note` enxuto | medir prompt antes/depois com 20 cadastros instalados |
| **F6** | Tela genérica `/dados` no painel (lista, busca, paginação, form, delete com confirmação) | criar/editar/excluir pela tela; conferir no psql |
| **F7** | Meta-skill `criar-cadastro` + `skill_write` + `add_field` | "cria um cadastro de produtos…" → CRUD funcionando na mesma sessão |
| **F8** | `tool_run` + `tools.py` de skill (flag off, instalação humana) + `relacao` + `arquivo` + `data_export`/`data_import` + índice semântico (§8.2 camada 3) | skill com tool própria instalada pelo painel |

Flag mestre: `NEO_DATASETS_ENABLED` (default **false**).
Flags separadas: `NEO_SKILL_AUTO_INSTALL`, `NEO_SKILL_PY_TOOLS_ENABLED` (default off).

**Ordem importa**: o agregador (F1) vem antes das tools de dados (F4) porque é
ele que dá endereço a elas; e o orçamento (F5) vem **antes** da tela (F6) porque
sem ele cada cadastro instalado piora o prompt de todo mundo.

**Esforço**: F0–F2 ≈ 2–3 dias · F3 ≈ 1–2 dias · F4 ≈ 1–2 dias · F5 ≈ 1 dia ·
F6 ≈ 1–2 dias · F7 ≈ 1 dia · F8 aberto. O caminho crítico é F3+F4 — é onde as
decisões de segurança viram código.

---

## 11. O que NÃO fazer (decisões já tomadas)

- ❌ Registrar tools por cadastro no `ToolRegistry` em runtime (§6).
- ❌ Fazer de `tool_run` o caminho padrão — schema tipado primeiro (§2.4).
- ❌ Renomear tool em colisão de nome; a instalação recusa e diz quem já tem (§2.2).
- ❌ Deixar o modelo instalar skill com `tools.py` (§2.5).
- ❌ Rodar DDL destrutivo por drift do `.md` (§3.4).
- ❌ Dropar tabela ao desinstalar skill (§3.5).
- ❌ Guardar linhas em JSONB genérico — mata `sql_query_ro` e agregação.
- ❌ Usar o PKS como armazenamento — entidade-campo não agrega (decisão do `fin_*`).
- ❌ Deixar o classificador rotear `data_*` (§9.1).
- ❌ Aceitar SQL cru no `filtro` do `data_list`.
- ❌ Abrir rede do sandbox para o Postgres (§9.7).

---

## 12. Pendências de decisão (Daniel)

1. **Multi-tenant de verdade?** Hoje toda linha carrega `user_id` e o Neo é
   mono-usuário na prática. "Atender clientes" pode ser (a) *você* atende, dados
   no seu Neo — `user_id` basta; (b) cada cliente tem a própria instância — nada
   muda; (c) um Neo com N clientes isolados — precisa `tenant_id` desde a F0.
   Barato agora, caro depois.
2. **`skill_write` entra (F7) ou o ciclo para na tabela?** Sem ele: o Neo cria o
   cadastro, você escreve a skill no painel (2 min). Com ele: uma frase.
3. **`tools.py` de skill vale o risco (§2.5)?** É o que permite skill trazer
   capacidade de verdade, e é a única parte do plano com execução de código
   arbitrário dentro do neo-core.
4. **(A) ou (B) no §9.7** para os `.py` do sandbox.
