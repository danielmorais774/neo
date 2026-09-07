# Service Gateway + Request DSL — plano de arquitetura

**Status:** **F1 + F2 + F3 EM PRODUÇÃO** (2026-07-16). Flag `NEO_GATEWAY_ENABLED=true`,
cofre Fernet ativo (`NEO_CRED_KEY`), aba **Serviços** no neo-panel, OpenAPI e parser
`.http` funcionando. **F4 é a única fase pendente** (OAuth/mTLS/SSE/rate-limit) —
ver §13. Estado detalhado no §12.
**Data:** 2026-07-15
**Objetivo:** permitir que o Neo execute requisições HTTP arbitrárias contra
qualquer serviço conectado (HA, GitHub, Proxmox, Docker, n8n, APIs próprias…)
**sem que nenhum LLM jamais veja uma credencial**, cobrindo a cauda longa de
endpoints que não têm tool dedicada.

---

## 0. TL;DR das decisões propostas

| Tema | Decisão recomendada |
|---|---|
| Interface p/ o LLM | **Tool JSON estruturada (`service_request`)** — a IR *é* a interface. A DSL textual `.http` vira formato de **autoria/persistência** (exemplos em skills, requests salvos, painel), não o wire format do LLM |
| Host/base_url | **Nunca vem do LLM.** Só o `service` (slug) + `path` relativo. Isso mata SSRF na raiz |
| Componente load-bearing | O **Policy Engine**, não o Credential Broker. Esconder o token não basta: o LLM age *com a autoridade* do token. Confinamento de capacidade > confinamento de segredo |
| Default de política | `GET/HEAD` liberados por conexão habilitada; métodos de escrita exigem regra explícita ou **confirmação no chat** |
| Credenciais | `credential_ref` com schemes: `env://VAR` (F1) e `fernet://<id>` (F2, mesma store do plano browser-agent). `vault://` fica como extensão futura |
| Onde roda | Dentro do neo-core (que já alcança HA/LAN — o adapter HA prova). **Não** passa pelo workbench-proxy (que bloqueia RFC1918 de propósito) |
| Relação com tools atuais | **Dois níveis**: tools curadas (rápidas, tipadas, verificadas — ficam como estão) + gateway (cauda longa). NÃO migrar HA/GitHub/etc. para a DSL |
| Feature flag | `NEO_GATEWAY_ENABLED` default off (padrão da casa: `NEO_AFFECT_ENABLED`, `NEO_REGISTER_ENABLED`) |

---

## 1. Crítica da arquitetura proposta (o que pediu: sem concordância automática)

### 1.1 A DSL textual é a parte mais fraca da proposta — e não é o que dá segurança

A segurança inteira vem de: registry (host fixo), broker (segredo fora do
contexto), policy (o que pode), executor (injeção em runtime). **Nenhum desses
componentes precisa de uma DSL textual** — todos operam sobre uma estrutura
`{service, method, path, headers, body}`.

O que a DSL textual custa:

- **Um parser novo = superfície de ataque nova** (smuggling de header por
  linha dobrada, encoding, request line ambígua, `###` de batch…). O formato
  RFC-2616-like foi desenhado para ser *parseado por servidores*, não para ser
  *canal seguro entre LLM e executor*.
- **Loop de erro de parse**: LLM gera texto → parser recusa → LLM tenta de
  novo. Com tool JSON, a validação de schema já existe no pipeline
  (`core/tools/validator.py`) e o modelo é nativamente bom em JSON args.
- **Dois formatos = duas verdades**: se aceitar DSL *e* JSON, cada validação
  precisa valer nos dois caminhos.

O que a DSL textual ganha (e é real):

- Documentações e issues trazem exemplos `curl`/`.http` que traduzem 1:1.
- Humanos escrevem/leem `.http` com prazer; skills podem versionar exemplos.
- REST Client/HTTPYac é um padrão conhecido.

**Recomendação:** inverter o papel da DSL.

1. O **LLM chama uma tool JSON** `service_request(service, method, path,
   query?, headers?, body?)` — os campos são exatamente a IR. Zero parser no
   caminho quente.
2. A **DSL `.http` é formato de autoria**: exemplos em `references/*.http`
   dentro de skills, requests salvos no painel, e um utilitário
   `parse_http_file()` que converte para a mesma IR (usado por `skill_read`
   +execução, pelo painel e por testes). Dialeto restrito do REST Client
   (ver §6): `@service` obrigatório, **path relativo obrigatório** (request
   line com URL absoluta é recusada — o REST Client original permite URL
   livre, e isso é exatamente o que não podemos herdar).

Assim ganha-se a familiaridade do formato sem colocá-lo no caminho
LLM→execução. Se na prática o modelo se mostrar melhor gerando o bloco
textual, dá para adicionar um campo alternativo `raw` na tool depois — a
arquitetura não muda, só o front do parser.

### 1.2 "O LLM nunca vê a credencial" resolve menos do que parece

Verdades desconfortáveis a registrar no design:

- O LLM **age com a autoridade da credencial**. Um token invisível que pode
  `POST /api/services/shell_command/...` no HA é pior que um token visível
  que só pode `GET /api/states`. O risco nº 1 do sistema não é vazamento de
  segredo, é **confused deputy via prompt injection** (conteúdo de web_fetch,
  e-mail, docs lidos convencem o modelo a fazer uma chamada destrutiva
  autenticada).
- **Respostas podem conter segredos** (endpoints de config que ecoam tokens,
  dumps do n8n com credenciais embutidas). O broker esconde o *nosso* token;
  a resposta entra no contexto do LLM crua se ninguém filtrar → redaction de
  resposta é obrigatória, não opcional.
- Erros de httpx podem embutir headers no `repr` → scrub também no caminho de
  erro e nos logs.

Conclusão: o Policy Engine e a redaction de resposta são tão estruturais
quanto o broker. O documento original trata policy como um item da lista;
aqui ele vira o centro.

### 1.3 Política por método+path não basta para serviços "RPC sobre REST"

`POST /api/services/*` no HA parece inofensivo até lembrar que
`POST /api/services/shell_command/x` executa shell e
`POST /api/services/homeassistant/restart` derruba a casa. Proxmox e Docker
API têm o mesmo padrão (um POST genérico com o "verbo perigoso" no path ou no
body). Política só de método+glob deixa esse buraco aberto.

**Mitigação em camadas:**
- F1: glob de path com granularidade suficiente
  (`POST /api/services/light/*` allow; `POST /api/services/shell_command/*`
  deny; deny vence allow sempre).
- F2: regra com `body_schema` (JSON Schema aplicado ao body) e/ou
  `body_deny` (JSONPath/campos proibidos) para os casos onde o perigo está
  no payload.
- Sempre: `confirm` como efeito intermediário (aprovação humana no chat)
  para escrita em conexões sensíveis.

### 1.4 Riscos técnicos concretos que o plano original não cobre

| Risco | Mitigação |
|---|---|
| Redirect cross-host (serviço responde 302 p/ host interno/externo) | Não seguir redirects por default; se `follow_redirects` na conexão, revalidar cada hop **contra o mesmo host** (padrão já existente em `modules/web_search/fetch.py`, guard por hop) |
| Path traversal / path absoluto (`../`, `//evil.com`, `http://` no path, `@`, fragmento) | Normalizar (RFC 3986) ANTES do match de política; recusar path que não comece com `/` após normalização, recusar `..` remanescente, recusar `\` e caracteres de controle |
| Bypass por encoding (`%2e%2e`, double-encoding, unicode) | Decodificar percent-encoding uma única vez, renormalizar, **matchear política sobre a forma normalizada**, executar exatamente a forma matcheada |
| Headers reservados vindos do LLM (`Authorization`, `Cookie`, `Host`, `Content-Length`, `Transfer-Encoding`, `Proxy-*`, `X-Forwarded-*`, `X-HTTP-Method-Override`) | Denylist dura no Validator — erro, não silêncio. Headers do LLM são só complementares (`Accept`, `Content-Type`, `X-Request-Id`…), com allowlist configurável por conexão |
| Método "escondido" (`X-HTTP-Method-Override`, `_method` em query) | Coberto pela denylist de headers + strip de query params reservados |
| Resposta gigante estourando contexto | Cap de bytes por conexão (default ~64KB) com truncamento explícito e aviso no resultado; content-types binários viram resumo (`[binário: image/png, 1.2MB]`) |
| Segredo em log/auditoria | Redaction central (`redact.py`): headers de auth nunca serializados; scan de padrões (`Bearer [A-Za-z0-9._-]+`, `ghp_…`, chaves PEM) no corpo de erro e na auditoria |
| Segredo caindo no PKS | Mesmo padrão do `ledger_guard` do PKS (precedente da casa): guard que impede material de credencial e respostas do gateway de virarem fatos permanentes |
| DNS rebinding (base_url com hostname que muda de IP) | Baixo risco aqui (base_url é do admin, não do LLM), registrar como não-objetivo de F1; se preocupar, pin de IP na criação da conexão |
| Timeout/retry amplificando escrita (retry de POST não-idempotente) | Retry só para métodos idempotentes (GET/HEAD/PUT/DELETE conforme config); POST nunca re-tenta automaticamente |

### 1.5 Threat model honesto (evitar teatro de segurança)

Sistema single-user (Daniel), rodando na VPS, alcançando LAN de casa. Os
inimigos reais, em ordem:

1. **Prompt injection indireta** — conteúdo não confiável (web_fetch, docs,
   research) induzindo chamadas autenticadas destrutivas.
2. **Erro do próprio modelo** — precedente registrado: a skill HA inventava
   `entity_id` e reportava sucesso falso (REST do HA dá 200 em id
   inexistente). O gateway devolve status/corpo crus, sem interpretação — o
   contrato é "verdade bruta", nunca "acho que deu certo".
3. **Sprawl de segredo** — token escapando para logs, contexto, PKS,
   `spec_json` de jobs.

O que NÃO é o threat model: multi-tenant, insider, compliance corporativa.
Termos como "Zero Trust" entram como princípio de desenho (nada confia em
nada por posição na rede), não como checklist de produto. Não construir
Vault, não construir RBAC multi-usuário, não construir assinatura de
requests — YAGNI até segunda ordem.

### 1.6 Comparação com plataformas de agentes modernas

| Plataforma | Mecanismo | Lição para o Neo |
|---|---|---|
| **OpenAI GPTs / Actions** | OpenAPI importada + auth guardada na plataforma, injetada na execução; domínio fixo por action | Igualzinho ao Connection Registry + broker. Validação: OpenAPI como fonte de descoberta funciona em produção há anos |
| **MCP (Anthropic)** | Servidor MCP detém a credencial; o modelo só vê tools; o transporte nunca carrega o segredo | O gateway é, na prática, um "MCP server genérico de HTTP" interno. Manter a IR limpa permite, no futuro, expor cada conexão como servidor MCP (ou importar MCP como "provider") quase de graça |
| **Zapier NLA / Composio / Arcade.dev** | "Connections" nomeadas + OAuth broker + escopo por ação + human-in-the-loop para escrita | O modelo `connection + policy + confirm` proposto aqui é o mesmo desses produtos — bom sinal de que a abstração está certa |
| **LangChain RequestsToolkit** | Tool de HTTP com URL livre | O **anti-exemplo** clássico (SSRF + exfiltração à vontade). A decisão "host nunca vem do LLM" é exatamente o que o separa disto |
| **Vault Agent / ESO (K8s)** | Segredo injetado em runtime por referência (`vault://…`), app nunca guarda | Mesmo desenho do Credential Broker; usar a mesma semântica de URI-scheme |
| **Claude Code (permissions)** | Allowlist/denylist por padrão de comando + prompt de permissão | Espelho do Policy Engine com efeito `confirm`; deny > confirm > allow |

---

## 2. Arquitetura

### 2.1 Visão geral

```
LLM (cognitivo)
  │  tool call JSON: service_request / service_list / service_docs
  ▼
ToolExecutor (existente, core/tools/executor.py)
  │  module executor "gateway"
  ▼
┌─────────────────────── core/gateway/ ───────────────────────┐
│  ir.py         ServiceRequestIR (pydantic, já validado)     │
│  validator.py  estrutura: headers reservados, path, tamanho │
│  registry.py   ConnectionRegistry (db + cache)              │
│  policy.py     PolicyEngine → allow | deny | confirm        │
│  broker.py     CredentialBroker (env:// | fernet://)        │
│  executor.py   HTTPExecutor (httpx) + redirects + retry     │
│  redact.py     scrub de segredos (erro, resposta, log)      │
│  audit.py      service_request_log                          │
│  parser.py     .http → IR (autoria: skills, painel) [F3]    │
│  openapi.py    ingestão/indexação de specs [F3]             │
└──────────────────────────────────────────────────────────────┘
  ▼
Serviço alvo (HA, GitHub, Proxmox, …)
```

Ordem do pipeline interno (imutável): **validate → resolve connection →
policy → (confirm?) → broker → execute → redact → audit → respond**. O broker
só é consultado DEPOIS do allow — segredo nunca é materializado para uma
request que seria negada.

### 2.2 Componentes e responsabilidades

**ConnectionRegistry (`registry.py`)** — fonte de verdade das conexões,
persistida no Postgres (tabela `service_connection`), cache em memória com
invalidação por update (mesmo padrão do config/skills). Cadastro pelo
neo-panel (aba Conexões) e seeds de bootstrap a partir das envs já existentes
(ex.: `home_assistant_url`/`home_assistant_token` viram a conexão
`homeassistant` com `credential_ref=env://HOME_ASSISTANT_TOKEN` — sem quebrar
nada do que existe).

**CredentialBroker (`broker.py`)** — resolve `credential_ref` → material de
segredo, **em memória, no momento da execução, e só para o executor**.
Schemes:

- `env://VAR_NAME` — F1; lê da `Settings`/ambiente da VPS (onde os tokens já
  vivem hoje).
- `fernet://<credential_id>` — F2; mesma store cifrada
  (`core/credentials/`, `NEO_CRED_KEY`) já planejada no browser-agent-plan
  §3.5 — **um único cofre para os dois sistemas**, cadastro panel-only.
- `vault://…` — interface prevista, implementação futura (se um dia houver
  Vault/OpenBao no homelab).

O broker devolve um objeto `SecretMaterial` que sabe **se aplicar** a uma
request (ver AuthScheme abaixo) e cujo `__repr__`/serialização é sempre
`"***"`. Nunca aparece em ToolResult, log, exception ou job spec.

**AuthScheme (parte do broker)** — estratégia plugável por conexão:

- `bearer` → `Authorization: Bearer <secret>`
- `basic` → `Authorization: Basic base64(user:pass)`
- `header` → header custom (`X-Api-Key: <secret>`, template configurável)
- `query_param` → `?token=<secret>` (necessário p/ APIs legadas; marcado como
  desaconselhado — vaza em logs do servidor alvo)
- `client_cert` → mTLS (par cert/key via ref) [F4]
- `oauth2_client_credentials` → broker mantém token cache + refresh [F4]
- `oauth2_device_flow` → fluxo interativo via chat/painel [F4]
- `none` → serviços sem auth (Ollama local)

**Validator (`validator.py`)** — estrutural, sem rede, sem segredo:
método ∈ {GET, HEAD, POST, PUT, PATCH, DELETE}; path normalizado e relativo;
headers do LLM fora da denylist reservada; body ≤ `max_body_bytes` da
conexão; content-type coerente; query sem params reservados.

**PolicyEngine (`policy.py`)** — regras ordenadas por conexão, avaliação
*deny > confirm > allow*, default **deny para escrita, allow para
GET/HEAD** (configurável por conexão via `default_read`/`default_write`):

```yaml
# exemplo: conexão homeassistant
rules:
  - effect: deny      # deny sempre vence
    methods: [POST]
    path: "/api/services/shell_command/*"
  - effect: deny
    methods: ["*"]
    path: "/api/services/hassio/*"
  - effect: allow
    methods: [GET]
    path: "/api/*"
  - effect: allow
    methods: [POST]
    path: "/api/services/light/*"
  - effect: confirm   # escrita fora do allow explícito → aprovação no chat
    methods: [POST, PUT, PATCH, DELETE]
    path: "/*"
```

Efeito `confirm` → o pipeline devolve ao cognitivo um resultado
`pending_confirmation` com um resumo legível (método, serviço, path, body
truncado) + `confirmation_id`; o Neo pergunta ao usuário no chat; a resposta
afirmativa executa via `service_confirm(confirmation_id)` (tool separada,
TTL curto, single-use). Isso reutiliza o padrão de handoff já existente
(música/client-actuating) sem inventar UI nova.

**HTTPExecutor (`executor.py`)** — único ponto do sistema que monta URL
completa e toca rede com credencial. httpx.AsyncClient por conexão (pool),
timeout da conexão (override por request ≤ teto), retry exponencial só para
idempotentes/5xx (espelho do adapter HA atual), redirects off por default,
TLS verify on (off só por conexão explícita — Proxmox self-signed), tracing
(`X-Request-Id` propagado), medição de duração/tamanho.

**Redact (`redact.py`)** — três pontos de aplicação: (1) resposta antes de
entrar no ToolResult (padrões de token/chave conhecidos + headers de auth
ecoados); (2) mensagens de exceção; (3) registros de auditoria. Cap de
tamanho da resposta aplicado aqui também.

**Audit (`audit.py`)** — grava `service_request_log` SEMPRE (allow, deny,
confirm, erro), inclusive a decisão de política tomada. Nunca grava header de
auth nem body completo (body: hash + primeiros N bytes redacted). Exposto no
painel e por tool read-only (`service_log_tail`?) — decisão em aberto, ver §8.

### 2.3 O que explicitamente NÃO muda

- **Tools curadas existentes** (HA, música, WhatsApp, finanças…) continuam
  como estão: são o caminho rápido, tipado, com verify. O gateway é a
  válvula de escape para a cauda longa. Migrar adapters existentes para a
  DSL seria trocar código verificado por política genérica — não fazer.
- `web_fetch` continua sendo o caminho para conteúdo público não autenticado
  (com o guard anti-IP-privado dele). A separação vira doutrina:
  **web_fetch = internet pública, sem auth, IP privado bloqueado;
  service_request = serviços cadastrados, com auth, host fixo pelo admin.**
- Workbench/sandbox: o gateway roda no processo do neo-core, não no sandbox
  (o sandbox é `internal:true` + proxy que nega RFC1918 de propósito).
  Scripts do workbench que precisarem de um serviço interno continuam
  proibidos de acessá-lo diretamente — se fizer sentido no futuro, expor um
  endpoint interno `neo-core → gateway` para o sandbox é uma extensão (F4+),
  com a mesma política.

---

## 3. Modelos de dados

### 3.1 IR (pydantic, `core/gateway/ir.py`)

```python
class ServiceRequestIR(BaseModel):
    service: str                      # slug da conexão
    method: Literal["GET","HEAD","POST","PUT","PATCH","DELETE"]
    path: str                         # relativo, começa com "/", já normalizado
    query: dict[str, str | list[str]] = {}
    headers: dict[str, str] = {}      # só complementares; reservados = erro
    body: JsonValue | str | None = None
    timeout_s: float | None = None    # override ≤ teto da conexão

class ServiceResponse(BaseModel):
    status: int
    headers: dict[str, str]           # subset seguro (content-type, etc.)
    body: str                         # redacted + truncado (limite da conexão)
    truncated: bool
    duration_ms: int
    request_id: str                   # correlaciona com a auditoria
```

### 3.2 Tabelas (SQLAlchemy em `db/models/`, migração Alembic)

```
service_connection
  id            text PK (slug: ^[a-z0-9][a-z0-9-]{1,39}$, ex. "github-personal")
  name          text
  provider      text            -- "homeassistant" | "github" | "generic" | ...
  base_url      text            -- https obrigatório exceto allow_http=true
  auth_type     text            -- none|bearer|basic|header|query_param|...
  auth_config   jsonb           -- template do header/param (SEM segredo)
  credential_ref text NULL      -- "env://..." | "fernet://..."
  default_headers jsonb
  timeout_s     numeric default 15
  retry         jsonb           -- {max: 3, backoff: "exp", idempotent_only: true}
  verify_tls    bool default true
  ca_cert_ref   text NULL       -- [F4]
  client_cert_ref text NULL     -- [F4]
  max_body_bytes   int default 262144
  max_response_bytes int default 65536
  default_read  text default 'allow'   -- allow|deny
  default_write text default 'confirm' -- allow|confirm|deny
  policies      jsonb           -- lista ordenada de regras (ver §2.2)
  docs_url      text NULL
  openapi_ref   text NULL       -- [F3] caminho/urn da spec ingerida
  capabilities  jsonb           -- descritivo p/ service_list (ex. tags)
  enabled       bool default true
  created_at / updated_at

credential            -- [F2] compartilhada com o browser-agent (plano §3.5)
  id uuid PK, label text unique, kind text, value_encrypted bytea, created_at
  fields_json jsonb     -- [F5] campos NÃO secretos do tipo (username, address,
                        --      imap_host, domain). Em claro de propósito.

service_request_log
  id uuid PK, ts, user_id, session_id, source text  -- chat|job|skill|panel
  connection_id text, method text, path text        -- path redacted (query sem valores sensíveis)
  policy_decision text  -- allow|deny|confirm_pending|confirmed|error
  status int NULL, duration_ms int NULL
  req_bytes int, resp_bytes int, error text NULL    -- redacted
  request_id text
```

### 3.3 Tools expostas ao cognitivo (`modules/services/tools.py`)

| Tool | Tipo | Descrição |
|---|---|---|
| `service_list()` | read, sempre disponível | Conexões habilitadas: slug, nome, provider, capabilities, resumo da política ("leitura livre, escrita com confirmação"), docs_url. É o que ancora o modelo — sem inventar serviço |
| `service_docs(service, query?)` | read | Progressive disclosure da documentação: F1 devolve `docs_url` + notas da conexão; F3 busca na OpenAPI indexada e devolve operações relevantes (method, path, params, schema resumido) |
| `service_request(...)` | write-gated pela política | Campos = IR. Resultado = `ServiceResponse` cru (status real, sem interpretação) |
| `service_confirm(confirmation_id)` | write | Executa uma request aprovada pelo usuário (TTL 5min, single-use) |

Registro: `service_list`/`service_docs` entram em `COGNITIVE_READ_TOOLS`
(`core/cognitive/read_tools.py`); `service_request`/`service_confirm` entram
sempre que `NEO_GATEWAY_ENABLED` (o gate fino é a política, não a presença da
tool). Lição da skill finanças aplicada: descrição da tool precisa dizer
QUANDO usar ("endpoint sem tool dedicada; consulte service_list antes"), e o
system prompt do cognitivo ganha 2 linhas sobre a existência do gateway.

---

## 4. Fluxo de execução completo

Cenário: *"lê a doc do HA e liga a luz da sala por chamada manual"*.

```
 1. Cognitivo: service_list() → vê "homeassistant (leitura livre, escrita
    light/* liberada)"
 2. Cognitivo: service_docs("homeassistant", "turn on light")
    → F1: docs_url + notas; F3: operação POST /api/services/light/turn_on
    (da OpenAPI indexada)
 3. Cognitivo: service_request(service="homeassistant", method="POST",
    path="/api/services/light/turn_on", body={"entity_id":"light.sala"})
 4. ToolExecutor → module executor "gateway"
 5. ir.py: parse/normalização (path RFC3986, lower-case do método…)
 6. validator.py: headers ok, body ≤ cap, path relativo ✓
 7. registry.py: conexão "homeassistant" carregada (cache)
 8. policy.py: match "POST /api/services/light/*" → allow
    (se caísse no confirm: retorna pending_confirmation → Neo pergunta no
    chat → usuário aprova → service_confirm(id) reentra no passo 9)
 9. broker.py: resolve env://HOME_ASSISTANT_TOKEN → SecretMaterial (memória)
10. executor.py: monta URL, aplica AuthScheme, timeout 15s, sem redirect,
    executa; mede duração/bytes
11. redact.py: scrub + truncamento da resposta
12. audit.py: grava service_request_log (decision=allow, status=200, 340ms)
13. ToolResult: {status: 200, body: "[{...estado da luz...}]", truncated:
    false, request_id: "..."} → cognitivo relata COM base no status real
    (nunca "desliguei" sem 2xx — precedente do falso-sucesso do HA)
```

Caminho de negação: passo 8 devolve
`{denied: true, rule: "deny POST /api/services/shell_command/*", hint:
"essa operação é bloqueada por política da conexão"}` — o modelo recebe o
motivo (sem stack, sem segredo) e consegue explicar ao usuário ou propor
alternativa. Auditoria registra o deny.

---

## 5. Defesas contra prompt injection nesta camada

A superfície nova é: *conteúdo não confiável induz o modelo a chamar
`service_request` destrutivo*. Camadas, da mais forte à mais fraca:

1. **Capacidade estrutural**: host nunca vem do LLM; conexão inexistente =
   erro; método/path fora da política = deny. Injection não cria capacidade
   nova — no pior caso usa as que existem.
2. **Escrita exige allow explícito ou humano**: `default_write=confirm` faz
   o pior caso do injection ser "o Neo *pede permissão* para fazer a coisa
   maligna" — visível e negável no chat.
3. **Deny imutável pelo modelo**: políticas só mudam por painel/config, nunca
   por tool. Não existe `service_policy_update` exposta ao cognitivo. (Igual
   à decisão de credenciais panel-only do browser-plan.)
4. **Sem exfiltração fácil**: o corpo/headers nunca contêm segredos do
   broker; `query_param` auth é desaconselhado; respostas são redacted antes
   do contexto — mesmo que um serviço eco-e um token, o modelo vê `***`.
5. **Isolamento entre conexões**: uma request usa exatamente UMA conexão;
   não há template/variável que puxe dado de outra conexão ou do cofre.
6. **Rate limit por conexão** (token bucket simples em memória): limita o
   estrago de um loop induzido; combinado com o `loop_detector` já existente
   no agente.
7. **Taint opcional (F4, ideia a validar)**: se a sessão consumiu conteúdo
   não confiável (web_fetch/research) nos últimos N turnos, escalar
   `allow`→`confirm` para escrita. Custo de UX real; medir necessidade antes.
8. **Auditoria sempre-grava**: até os denies ficam registrados — é o sensor
   para perceber tentativa de abuso induzido.

O que NÃO prometer: detecção semântica de injection no conteúdo (frágil) e
"o modelo vai se comportar" (não é controle). As garantias vêm dos itens 1–3.

---

## 6. DSL textual `.http` (formato de autoria — F3)

Dialeto restrito do REST Client, convertido para IR por `parser.py`:

```http
### liga a luz da sala
@service homeassistant

POST /api/services/light/turn_on
Content-Type: application/json

{
  "entity_id": "light.sala"
}
```

Regras do dialeto (diferenças deliberadas vs. REST Client/HTTPYac):

- `@service <slug>` obrigatório antes da request line.
- Request line aceita **apenas path relativo** — URL absoluta = erro de parse
  (no REST Client original a URL é livre; é a herança que recusamos).
- Sem `{{variáveis}}` em F3 (nem interpolação de env — é onde ferramentas
  desse tipo vazam segredo). Se um dia entrar, só variáveis não-secretas
  declaradas na conexão.
- Sem scripts embutidos (HTTPYac permite JS — recusado).
- `###` separa requests salvos num mesmo arquivo; execução é sempre 1 por vez.
- Headers reservados no arquivo = mesmo erro do validator.

Usos: `references/*.http` em skills (exemplos executáveis por
`skill_read` → parse → mesma IR/pipeline), requests salvos no painel
("biblioteca de chamadas"), fixtures de teste do gateway.

---

## 7. Estrutura de diretórios

```
neo-core/
  core/gateway/
    __init__.py
    ir.py            # ServiceRequestIR, ServiceResponse, tipos
    validator.py     # validações estruturais
    registry.py      # ConnectionRegistry + cache
    policy.py        # PolicyEngine (match, deny>confirm>allow)
    broker.py        # CredentialBroker + AuthSchemes
    executor.py      # HTTPExecutor (httpx)
    redact.py        # scrub central de segredos
    audit.py         # service_request_log writer
    confirm.py       # store de confirmações pendentes (TTL, single-use)
    parser.py        # [F3] .http → IR
    openapi.py       # [F3] ingestão + índice de operações
  modules/services/
    __init__.py
    tools.py         # ToolSchemas: service_list/docs/request/confirm
    executor.py      # module executor → core/gateway
  core/credentials/  # [F2] store Fernet (COMPARTILHADO com browser-agent F3)
    store.py
    guard.py         # bloqueia material de credencial no PKS/logs
  db/models/gateway.py       # service_connection, service_request_log
  api/routes/connections.py  # CRUD p/ o painel + "testar conexão"
  tests/gateway/             # unit (policy, validator, redact) + e2e mockado
neo-panel/src/
  ConnectionsPanel.tsx       # aba Conexões: CRUD, editor de política,
                             # botão test, viewer de auditoria
```

Nome `core/gateway/` (alternativas consideradas: `core/connect/`,
`core/services_gateway/` — "gateway" comunica exatamente o papel: único
ponto de passagem HTTP autenticado).

---

## 8. Integração com o restante do Neo

- **Skills library**: skill `tool_backed` pode declarar exemplos `.http` em
  `references/` e instruir o uso de `service_request` no corpo. Extensão
  futura interessante: frontmatter `connections: [homeassistant]` que, ao
  ler a skill, **eleva temporariamente a política** (ex. libera um path de
  escrita enquanto a skill está ativa) — capability elevation auditável.
  Fica registrado como ideia F4, não requisito.
- **PKS/memória**: conexões são config, não memória — nada disso entra no
  PKS. O `guard.py` segue o precedente do `ledger_guard` (dinheiro nunca
  entra no PKS → segredo/resposta de gateway nunca entra no PKS). O que PODE
  virar conhecimento: fatos aprendidos *a partir* de respostas (ex. "a
  entity da luz da sala é light.sala"), pelo fluxo normal do cognitivo.
- **JobManager/workbench**: chamadas HTTP são curtas — rodam inline no turno.
  Jobs longos (polling, SSE, webhook listener) NÃO entram nesta arquitetura
  agora; quando entrarem, o runner de job chama o mesmo `core/gateway`
  (mesma política/auditoria), nunca httpx direto.
- **Painel**: aba Conexões espelha a aba Skills (CRUD + editor). Botão
  "testar" chama um endpoint de healthcheck da conexão (GET configurável).
  Auditoria consultável por conexão/período.
- **Config/flags**: `NEO_GATEWAY_ENABLED` (master), `NEO_CRED_KEY` (F2,
  compartilhada com browser-agent). Seeds de conexão a partir das envs
  existentes para não duplicar segredo.
- **Observabilidade**: logs estruturados no app.log com `request_id`
  (lição do gap de observabilidade da skill HA: a verdade precisa estar num
  lugar consultável — aqui é a tabela de auditoria, não um .md).

---

## 9. Integrações futuras (como a arquitetura absorve sem mudar a DSL/IR)

| Futuro | Onde encaixa |
|---|---|
| OpenAPI/Swagger | `openapi.py` ingere spec (upload/URL no painel), indexa operações; `service_docs` vira busca semântica/keyword sobre o índice; opcional: policy sugerida a partir da spec |
| MCP | Duas direções: (a) importar servidor MCP como "provider" de conexão; (b) expor cada conexão como MCP server p/ outros clientes. A IR já é o denominador comum |
| GraphQL | Nova `kind: graphql` na conexão; IR ganha campo `operation` opcional; política sobre operation name + profundidade (path-glob não serve — registrado em §1.4) |
| Webhooks (entrada) | Fora do gateway (é ingress, não egress) — casa com o JobManager/chat proativo existente |
| SSE / WebSocket | `executor.py` streaming + entrega via job com progresso (`job_logs`); política idêntica |
| OAuth (CC / Device Flow) | Novos AuthSchemes no broker; token cache + refresh internos; device flow interage via chat/painel, código nunca passa pelo LLM em claro (é código de pareamento público, ok mostrar) |
| mTLS / CA custom | `client_cert_ref`/`ca_cert_ref` já reservados na tabela |
| Basic/API-Key | Já em F1/F2 via AuthScheme |

---

## 10. Fases de implementação

**F1 — MVP leitura + escrita confirmada (o núcleo de valor)**
`core/gateway/` (ir, validator, registry db+seed de envs, policy com
default_read=allow/default_write=confirm, broker env://, executor, redact,
audit, confirm), tools `service_list`/`service_request`/`service_confirm`,
migração Alembic, flag off→on.
*Aceite:* pelo chat, "faz um GET em /api/states no homeassistant" funciona;
um POST fora de allow pede confirmação; token não aparece em nenhum log/
resposta (teste automatizado de redaction); deny de shell_command registrado
na auditoria.

**F2 — Painel + cofre Fernet**
Aba Conexões (CRUD, editor de política, testar), `core/credentials/`
(compartilhado com browser-agent F3), `fernet://` no broker, viewer de
auditoria.
*Aceite:* cadastrar "github-personal" 100% pelo painel (sem env nova) e
listar issues pelo chat.

**F3 — Descoberta: OpenAPI + DSL de autoria**
`openapi.py` (ingestão + índice), `service_docs` com busca de operações,
`parser.py` do dialeto `.http`, exemplos `.http` em skills.
*Aceite:* "estuda a OpenAPI do meu serviço X e chama o endpoint Y que não
tem tool" resolve sozinho via service_docs → service_request.

**F4 — Auth avançada + endurecimento**
OAuth2 CC/device flow, mTLS, rate limit refinado, taint-based confirm (se
os dados de auditoria justificarem), SSE via jobs, capability elevation por
skill.

---

## 11. Questões em aberto (decidir antes/durante F1)

1. **Granularidade default do HA**: liberar `POST /api/services/*` com
   denylist de domínios perigosos (shell_command, hassio, homeassistant/
   restart…) ou allowlist de domínios (light, switch, scene, script…)? A
   allowlist é mais segura; a denylist dá menos atrito. Proposta: allowlist +
   confirm para o resto.
2. **`service_request` sempre visível vs. gated por skill/keyword**: proposta
   é sempre visível (a política é o gate), mas o precedente da skill finanças
   (ativação só veio com autoload por keyword) sugere medir se o modelo
   *encontra* a tool na hora certa. Se não encontrar, criar skill
   "integracoes-http" com keywords como gatilho de contexto.
3. **Expor auditoria como tool read-only** (`service_log_tail`)? Útil para o
   próprio Neo se depurar ("a chamada falhou ontem?"), mas amplia contexto —
   decidir em F2.
4. **Postgres via gateway?** Não — Postgres não é HTTP; já existe `sqlro`/
   skill postgres-neo. Registrar para não escopo-crescer.
5. **Compressão/streaming de resposta em F1**: começar sem (httpx já
   descomprime gzip por default); streaming só com SSE em F4.

---

## 12. Estado da implementação (F1 — 2026-07-16)

**Entregue e testado** (53 testes em `tests/gateway/`, 835 da suíte coletam sem
regressão). Tudo atrás de `NEO_GATEWAY_ENABLED` (default off).

Pacote `core/gateway/`:
- `ir.py` — `ServiceRequestIR`, `ServiceResponse`, `ConnectionConfig`,
  `PolicyRule`/`PolicyDecision`/`PolicyEffect`, `AuthType`, `GatewayError`.
- `validator.py` — `normalize_path` (traversal, `//`, URL absoluta, `?`/`#`,
  controle/`\`, `..` inclusive por single/double-encoding) + headers reservados
  (Authorization/Cookie/Host/Proxy-*/X-Forwarded-*/method-override), query
  reservada, teto de body. Política casa **sobre a forma normalizada**.
- `policy.py` — precedência **deny > confirm > allow** (não ordem de lista) +
  defaults por natureza (`default_read`/`default_write`).
- `broker.py` — `CredentialResolver` (`env://` implementado; `fernet://` levanta
  erro claro até a F2; `vault://` reservado) + `SecretMaterial` que serializa
  como `***` + AuthSchemes bearer/basic/header/query_param/none.
- `executor.py` — `HTTPExecutor` (httpx): URL montada por nós (host fixo),
  auth por último, retry só idempotente, redirects off (revalida host por hop se
  ligados), redação+truncamento da resposta. `transport` injetável só p/ teste.
- `redact.py` — scrub por padrão (Bearer/Basic/JWT/PEM/gh_/xox/sk-/AKIA/headers
  de auth ecoados) em resposta, erro e auditoria + `cap_body`.
- `confirm.py` — `ConfirmStore` in-memory, TTL 5min, uso único (só a IR, sem
  segredo).
- `registry.py` — `ConnectionRegistry` (db + seed de env; HA seed com allowlist
  de domínios + denies de shell_command/hassio/restart + `default_write=confirm`).
- `audit.py` — `AuditWriter` best-effort (nunca derruba o request).
- `service.py` — orquestrador `GatewayService` (`handle_request`/`handle_confirm`
  /`list_services`/`describe_service`), pipeline na ordem imutável.
- `__init__.py` — `build_gateway_service(db, settings, fernet_store)`.

Fora do pacote:
- `db/models/gateway.py` (`ServiceConnection`, `ServiceRequestLog`) + registro no
  `db/models/__init__.py` + migração `m6n7o8p9q0r1` (head único).
- `modules/services/` — `SERVICE_TOOLS` (service_list/docs/request/confirm) +
  `ServicesExecutor`.
- Wiring: `shared/config.py` (`neo_gateway_enabled`), `api/main.py` (registra
  tools + module executor quando a flag está on; `app.state.gateway_service`),
  `core/cognitive/read_tools.py` (4 tools no read set, gated pela flag).

**Diferenças em relação ao plano, deliberadas:**
- A tool `service_request` recebe `query`/`headers`/`body` como `object` no
  schema do LLM (o `ToolParameter.type` vira JSON-schema direto). Body string/
  bytes é aceito pelo executor (caminho interno/`.http` da F3), mas o schema
  exposto ao modelo é objeto JSON — cobre HA/GitHub/maioria. Rever se surgir API
  de body texto puro.
- Auditoria com `user_id`/`session_id` é best-effort na F1: o executor global
  registrado no boot não thread-a contexto por request (default `source=chat`,
  `user_id=None`). `ServicesExecutor.set_context` já existe para o dia em que o
  cognitivo criar executor fresco por request (como devices/finance).

**Não incluído (fica para as fases seguintes):** aba Conexões no neo-panel e
`api/routes/connections.py` (F2), cofre Fernet (`core/credentials/`, F2),
`openapi.py`/`parser.py`/`.http` (F3), OAuth/mTLS/SSE/rate-limit refinado (F4).
Enquanto a flag estiver off, nada muda no comportamento atual.

### Deploy em produção (2026-07-16)

Aplicado na VPS Contabo (o "local" do Neo já é prod: Mutagen sincroniza `.py` e o
neo-core roda `--reload`). Passos executados:

1. Código do gateway sincronizado automaticamente (Mutagen).
2. `docker exec -w /app neo-core alembic upgrade head` → cria `service_connection`
   e `service_request_log` (alembic_version = `m6n7o8p9q0r1`).
3. `NEO_GATEWAY_ENABLED=true` no `.env` (backup `.env.bak-gateway-20260716`) e
   `docker compose up -d --no-deps neo-core` (reload não relê env) → container
   healthy, log `✅ Service Gateway wired (tools service_*)`.
4. **Fix no deploy:** o seed do HA passou a derivar `allow_http` do scheme da URL
   — o HA em prod é `http://172.18.0.1` (rede interna) e o executor recusa `http://`
   sem `allow_http`. Corrigido em `registry.py::_env_seeds`; deny de `shell_command`
   validado ao vivo (sem tocar credencial).

Pendências de validação: um smoke test **autenticado** ao vivo contra o HA
(`GET /api/`) não foi rodado — precisa de autorização explícita nomeando o passo
(o gateway faz chamada autenticada a serviço de produção). Rodar quando for
exercitar a feature pelo chat.

---

## 13. F2 + F3 — implementação e deploy (2026-07-16)

**107 testes** em `tests/gateway/` (suíte inteira: 937, zero regressão).

### F2 — cofre + painel (EM PRODUÇÃO)

| Peça | Arquivo |
|---|---|
| Cofre Fernet | `core/credentials/store.py` (`CredentialStore`), `db/models/credential.py`, alembic `o8p9q0r1s2t3` |
| Guard do PKS | `core/credentials/guard.py` + enforcement em `core/pks/writer.py` |
| `fernet://` real | `core/gateway/broker.py` |
| CRUD de conexões | `core/gateway/registry.py` (`upsert`/`delete`/`is_seed`) |
| API do painel | `api/routes/connections.py` (conexões, credenciais, testar, auditoria) |
| Aba **Serviços** | `neo-panel/src/ServicesPanel.tsx` + rota `/servicos` |

**Decisões que valem registrar:**
- **`build_auth` virou async.** O cofre lê do banco; o hack alternativo (cache
  síncrono) tinha bug de estado compartilhado. Um contrato async > dois.
- **Segredo é write-only.** Entra no cadastro e nunca volta: a API devolve só
  rótulo/tipo/`hint` (últimos 4 chars). Para trocar, cadastra por cima. Não
  existe endpoint que leia o valor — nem para o painel.
- **`core/credentials/__init__.py` é deliberadamente leve** (só o guard). O
  `pks/writer.py` importa o guard e é carregado SEMPRE; se o `__init__`
  puxasse o store (→ `cryptography`), o import do PKS quebraria e, com
  Mutagen + `--reload`, derrubaria a produção antes de qualquer deploy — foi
  exatamente o que o `python-multipart` fez no File Store. O store fica atrás
  de `build_credential_store()`, chamado só quando há `NEO_CRED_KEY`.
- **Seeds do `.env` são read-only no painel** (400 no DELETE): quem manda nelas
  é a env. Credencial em uso não some (409) — diz qual conexão a usa.

### F5 — cofre TIPADO, visível ao cérebro (EM PRODUÇÃO 2026-08-25)

O que motivou: *"o type está irrelevante na hora de cadastrar, parece sempre
aceitar um tipo só; se eu quiser registrar um usuário e senha não aparecem os
campos; e a description não é usada pelo neo-core no julgamento e escolha do
item certo no cofre"*. Os três diagnósticos estavam certos:

- `kind` e `description` eram **inertes** — nada no core lia os campos, só
  ecoava no `to_dict()`;
- **usuário+senha não cabia**: a credencial guarda UM segredo e o username
  morava do outro lado, no `auth_config` da *conexão* — logo, um login de site
  sem conexão nenhuma não tinha onde existir;
- **não havia "escolha"**: o único consumidor era o broker resolvendo o
  `credential_ref` que a conexão já apontava. Sem alguém escolhendo, a
  descrição não tinha para quem falar.

| Peça | Arquivo |
|---|---|
| Spec de campos por tipo | `core/credentials/kinds.py` (`CREDENTIAL_KIND_SPECS`) |
| Coluna `fields_json` | `db/models/credential.py`, alembic `u4v5w6x7y8z9` |
| Validação (whitelist + obrigatórios) | `CredentialStore._fields` / `normalize_fields` |
| Spec para o painel | `GET /api/v1/gateway/credential-kinds` |
| Formulário dirigido pela spec | `neo-panel/src/ServicesPanel.tsx` (`VaultTab`) |
| Catálogo para o cérebro | `core/credentials/tools.py` (`vault_list`) |
| Cofre → browser | `core/credentials/browser.py` (`sensitive_data` por domínio) |
| Caixa de email do Neo | `core/mailbox/` (IMAP, `email_inbox`/`email_message`) |
| Identidade no prompt | `AIService._neo_identity_block` |

**Decisões que valem registrar:**
- **Dois destinos, uma regra**: o SEGREDO é único e cifrado; os CAMPOS
  (`username`, `address`, `imap_host`, `domain`) ficam **em claro**. É essa
  separação que deixa o painel mostrar "usuário: daniel" e o cérebro escolher
  sem decifrar nada. Como os nomes vêm da spec (whitelist), não existe "campo
  livre" onde um segundo segredo entre sem querer — chave desconhecida é 400.
- **Senha sem domínio é RECUSADA para o browser.** O `sensitive_data` da
  browser-use só revela o placeholder quando a aba está num domínio que casa
  com o padrão; sem domínio o valor viraria global e qualquer página poderia
  colhê-lo por prompt injection. Recusar com "preencha o Domínio de uso" é
  melhor que degradar em silêncio.
- **O endereço de email vai em claro, a senha da caixa não vai ao browser.**
  Endereço é público (é o que se digita num cadastro); ler a caixa é papel do
  IMAP, não do Chromium.
- **A identidade ("você tem email, o endereço é X") vai no PROMPT, não atrás de
  tool.** Se dependesse de descobrir, o Neo pediria o email do USUÁRIO em vez
  de usar o dele. Cache de 5 min; o endereço é público, a senha nunca aparece.
- **Tool no registry global ≠ tool que o cérebro tem.** O toolset inline sai de
  `cognitive_tools_for` (`core/cognitive/read_tools.py`), não do registry —
  registrar só no registry deixaria a instrução de identidade impossível de
  cumprir (mesma família do gotcha das job tools).
- **`db/models/` NÃO importa de `core`.** `core/__init__.py` puxa a engine
  inteira; o alembic importa os models direto e morre em circular import com
  `db.async_client`. A lista de kinds é duplicada no model de propósito, com um
  teste que trava a divergência.

### F3 — descoberta (EM PRODUÇÃO)

- `core/gateway/openapi.py` — indexa OpenAPI 3.x e Swagger 2.0 (`basePath` vira
  prefixo). Busca por keyword com score (path pesa mais). **Sem embeddings de
  propósito:** o vocabulário é literal (`turn_on`, `issues`), onde casar token
  bate semântica sem pagar latência.
- `service_docs(service, query)` devolve as ~8 operações relevantes + hint de
  body (`{title*, body, labels}`) — **nunca a spec inteira**. Índice cacheado
  por conteúdo.
- `core/gateway/parser.py` — dialeto `.http` restrito → a MESMA IR da tool.
  Recusa: URL absoluta, `//host`, `@service` ausente, `{{vars}}`, scripts,
  headers reservados. Coluna `service_connection.openapi_spec` (alembic
  `p9q0r1s2t3u4`); campo para colar a spec no painel.

### Deploy + aceite ao vivo

Migrações `o8p9q0r1s2t3` e `p9q0r1s2t3u4` aplicadas; `NEO_CRED_KEY` **gerada na
própria VPS** (o valor nunca transitou por logs/contexto) e gravada no `.env`
(backup `.env.bak-credkey-20260716`); `docker compose up -d --no-deps neo-core`
(instalou `cryptography==49.0.0`). Boot: `✅ Service Gateway wired (tools
service_*; cofre: ON)`.

Exercitado em prod com dado descartável (limpo depois):

| Verificação | Resultado |
|---|---|
| Cadastro de credencial pela API do painel | ✅ devolve id/ref/hint |
| Segredo não volta na listagem | ✅ 0 ocorrências |
| Cifrado em repouso | ✅ `gAAAAABq…` (Fernet) no banco; 0 linhas com o valor em claro |
| Conexão nova com `fernet://` + spec | ✅ criada pelo PUT do painel |
| `service_docs("criar issue")` | ✅ retornou **1 de 3** operações: `POST /issues` |
| Apagar credencial em uso | ✅ 409 dizendo qual conexão a usa |
| Apagar seed do `.env` | ✅ 400 (read-only) |

**Prod sobreviveu ao sync do código sem `cryptography`** — a prova de que o
import preguiçoso funciona (o `unhealthy` visto no meio foi transitório: rajada
de reloads durante a edição, o healthcheck tem timeout de 5s; normalizou sozinho).

### O que resta: F4

`oauth2_client_credentials` / `oauth2_device_flow` / `client_cert` (mTLS) já
existem como valores de `AuthType` e são recusados com "não implementada (F4)".
Faltam também: rate limit por conexão, taint-based confirm, SSE via jobs e
capability elevation por skill. Nenhum é exercível hoje — não há serviço
cadastrado que use OAuth ou mTLS. Fazer quando houver um caso real.
