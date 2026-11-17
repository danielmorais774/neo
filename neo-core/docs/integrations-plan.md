# Integrações de contas (OAuth) + Google Drive — plano de arquitetura

**Status:** PLANO. Nada implementado.
**Data:** 2026-09-19
**Objetivo:** dar ao Neo acesso REAL ao Google Drive do usuário (achar, ler,
baixar, enviar, organizar), com **múltiplas contas Google**, sem que nenhum LLM
veja token — e deixar isso numa **área de integrações** que absorva o próximo
provedor (OneDrive, Dropbox, Notion, Gmail) sem virar um hardcode por serviço.

Relacionados: `service-gateway-dsl-plan.md` (a base que isto estende — §13 "O que
resta: F4"), `file-store-plan.md` (para onde os bytes vão), `file-links-plan.md`
(onde fica registrado que foram), `browser-local-plan.md` (o caminho concorrente).

---

## 0. TL;DR das decisões propostas

| Tema | Decisão recomendada |
|---|---|
| Skill ou integração? | **Integração de fato.** Skill não guarda token nem renova credencial. Mas entra uma skill fina por cima, como **gatilho de ativação** (é o único mecanismo de ativação medido da casa) |
| Onde mora a área | **A área já existe**: `/servicos` + Service Gateway. Vira **Integrações**, com duas abas: *Conexões HTTP* (o que já roda) e *Contas conectadas* (novo) |
| Precisa de gateway novo? | **Não.** Registry, cofre Fernet, política, confirmação e auditoria são reaproveitados inteiros. O que falta é um **AuthScheme OAuth2 com refresh** e uma **tabela de contas** |
| Drive via `service_request`? | **Não.** Quatro lacunas medidas (§1.3). O Drive ganha tools tipadas; o `service_request` continua sendo a cauda longa |
| Nome das tools | **`cloud_*`** (não `drive_*`), com `account` identificando provedor+conta. Renomear tool depois é proibido na prática — o corpo de uma SKILL.md cita o nome (ver `ToolAggregator.ToolNameConflict`) |
| Multi-conta | Tabela `integration_account` por usuário. 1 conta → implícita; 2+ → **leitura usa a default, escrita PERGUNTA**. Chutar conta é a doença do `entity_id` inventado |
| Token | Cofre Fernet existente, `kind: "oauth"` novo. **Nunca** em `user_preference` em claro (é o que o Spotify faz hoje — é o débito que este plano fecha) |
| Bytes | **Nunca pelo `HTTPExecutor`** (ele é text-only e corta em 64 KB). Download → `FileStore.ingest_stream` → evento no `file_link` |
| Privacidade | Download cai com `retention="temp"` por default. Conteúdo dele não fica de bobeira na VPS sem ele pedir |
| Navegador local | Continua existindo e ganha fronteira escrita: API é o default; navegador quando ele pede a tela ou a API não alcança |
| Feature flag | `NEO_INTEGRATIONS_ENABLED` default off (padrão da casa) |

---

## 1. Crítica honesta das três saídas possíveis

### 1.1 "Faz uma skill de Drive"

Uma `SKILL.md` é markdown + uma lista de tools que **já existem**
(`skills/financas/SKILL.md` é o modelo: `kind: tool_backed`, `tools: [fin_*]`).
Ela não guarda segredo, não renova token, não passa por política nem auditoria.
Skill é **manual de uso**, não implementação.

Se a skill fosse o caminho, as tools por baixo dela teriam que ser
`service_request` cru — e aí caímos no §1.3.

**Veredito:** a skill entra, mas na camada certa (§7.3), não como resposta.

### 1.2 "Copia o que o Spotify faz"

O Spotify já tem OAuth authorization_code funcionando:
`api/routes/spotify.py` (authorize/callback com `state` assinado por HMAC) +
`core/skills/music/adapter_spotify.py` (refresh) +
`modules/music_config/store.py` (tokens numa linha de `user_preference`).

Funciona. E é **exatamente o que não pode ser repetido**:

- o refresh token fica **em claro** numa linha de `user_preference`, enquanto o
  cofre Fernet existe a três diretórios dali;
- a rota, a store e o refresh são **de um provedor só**. Drive seria o segundo.
  OneDrive, o terceiro. Nenhum deles aparece no painel de Integrações, nem na
  auditoria, nem no cofre;
- uma conta por usuário, sem lugar para a segunda.

**Veredito:** o Spotify é o **precedente do problema**, não da solução. A F1
deste plano deveria, no fim, poder absorvê-lo (§11, questão 4).

### 1.3 "Cadastra o Drive como conexão do gateway e usa `service_request`"

É a saída mais barata e a mais tentadora — e falha em quatro pontos, todos
verificáveis no código de hoje:

**(a) OAuth2 com refresh não existe.** `AuthType` declara
`OAUTH2_CLIENT_CREDENTIALS` e `OAUTH2_DEVICE_FLOW` (`core/gateway/ir.py:45-46`),
mas o broker termina em `unsupported_auth ... (F4)`
(`core/gateway/broker.py:170`). E nenhum dos dois serve: Drive de conta pessoal
exige **authorization_code com refresh offline**. Nem service account resolve —
service account só alcança Drive de Workspace, com domain-wide delegation.

**(b) O executor é text-only.** `HTTPExecutor._build_response` faz
`response.text`, redacta e corta em `max_response_bytes`
(`core/gateway/executor.py:160-162`, default 64 KB). Um PDF de 8 MB volta como
lixo truncado. Mover bytes **precisa** de outro caminho.

**(c) Uma conexão = uma credencial.** `ConnectionConfig.credential_ref` é
escalar (`core/gateway/ir.py:106`). Multi-conta só sairia duplicando a conexão
inteira — política, spec OpenAPI e tudo.

**(d) O modelo teria que montar a API do Drive na mão.** A Drive API v3 tem
linguagem de query própria (`q="name contains 'nfse' and trashed = false"`),
`fields` obrigatório na prática, paginação por `pageToken`,
`supportsAllDrives`, e Google Docs que só saem por `/export`. O repo tem duas
cicatrizes exatamente dessa classe de erro: o `entity_id` inventado da skill HA
(`neo-ha-false-success-invented-entity`) e o `file_path` inventado pelo
classificador. **Não repetir.**

**Veredito:** o gateway é a **fundação certa** (registry, cofre, política,
auditoria, confirmação) e a **interface errada** para o Drive. Este plano
estende a fundação e põe tools tipadas por cima.

### 1.4 O que este plano NÃO promete

- Não é sincronização. Não existe "pasta espelhada". Todo acesso é sob demanda.
- Não é busca semântica no Drive. `cloud_search` é a busca do provedor, não um
  índice nosso.
- Não substitui o navegador local. §7 desenha a fronteira, não a remoção.
- Não resolve Google Docs nativos na F2 (só exportando — §11, questão 3).

---

## 2. Arquitetura

### 2.1 Visão geral

```
┌─ Painel /integracoes ────────────────────────────────────────┐
│  Aba "Conexões HTTP"      → o Service Gateway de hoje         │
│  Aba "Contas conectadas"  → NOVO                              │
│     google-drive · daniel@gmail.com     ativa    [reautorizar]│
│     google-drive · daniel@empresa.com   ativa    [desconectar]│
└───────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┴──────────────────────┐
        ▼                                            ▼
┌─ core/integrations/ ──────────┐      ┌─ core/gateway/ (existe) ─┐
│ providers/google_drive.py     │      │ registry · policy        │
│   catálogo: URLs, scopes,     │      │ confirm  · audit         │
│   base_url, como identificar  │      │ broker ← + OAuth2Scheme  │
│   a conta                     │      └──────────────────────────┘
│ oauth.py   authorize/refresh  │
│ accounts.py  integration_account          ┌─ core/credentials/ ─┐
│ audit → mesma tabela do gateway           │ cofre Fernet        │
└───────────────┬───────────────┘           │ + kind "oauth"      │
                ▼                           └─────────────────────┘
┌─ modules/cloud/ ─────────────────────────────────────────────┐
│ tools.py    cloud_list_accounts · cloud_search · cloud_get    │
│             cloud_download · cloud_upload · cloud_mkdir       │
│             cloud_move · cloud_delete · cloud_link            │
│ executor.py resolve conta → adapter do provedor → HTTP        │
└──────────────┬────────────────────────────────────────────────┘
               ▼ bytes
    FileStore.ingest_stream → file_link (evento "pulled_cloud")
```

### 2.2 Componentes e responsabilidades

| Componente | Responsabilidade | Nunca faz |
|---|---|---|
| `providers/<nome>.py` | Catálogo estático: authorize URL, token URL, scopes, base_url, como ler o e-mail da conta, tradução de query | Tocar rede fora do fluxo OAuth |
| `oauth.py` | authorization_code, refresh com lock, revogação, marcação `needs_reauth` | Guardar segredo fora do cofre |
| `accounts.py` | CRUD de `integration_account`, resolução de conta (§5) | Escolher conta sozinho numa escrita |
| `OAuth2AuthScheme` (broker) | Devolver `Authorization: Bearer <access>` já renovado | Aparecer em log, trace ou ToolResult |
| `modules/cloud/executor.py` | Traduzir tool → chamada do provedor, streamar bytes, registrar auditoria e ledger | Devolver conteúdo binário ao contexto |
| `modules/cloud/tools.py` | Schemas tipados vistos pelo cognitivo | Aceitar URL absoluta ou token |

### 2.3 O que explicitamente NÃO muda

- `service_request` e as conexões HTTP atuais: intocadas. Drive **não** vira
  conexão do gateway.
- `browser_local_task` / `browser_task`: intocados. Só ganham fronteira escrita
  no prompt (§7).
- `file_*` (catálogo) e `neo_*` (sandbox): intocados. `cloud_*` é o **terceiro**
  namespace de arquivo e a distinção precisa estar escrita em cada description —
  é o que `file-store-plan.md §14.5` já manda fazer, e o motivo é medido.

---

## 3. Modelos de dados

### 3.1 Catálogo de provedores (código, não tabela)

Provedor é código porque carrega tradução de query e parsing de resposta —
não cabe em JSON de painel. Fica em `core/integrations/providers/`.

```python
@dataclass(frozen=True)
class ProviderSpec:
    slug: str                      # "google-drive"
    label: str                     # "Google Drive"
    authorize_url: str
    token_url: str
    revoke_url: str | None
    base_url: str                  # https://www.googleapis.com/drive/v3
    scopes: tuple[str, ...]        # ver §11 questão 2
    extra_authorize_params: dict   # access_type=offline, prompt=consent
    identity: Callable             # como descobrir o e-mail da conta autorizada
    adapter: type[CloudAdapter]    # search/get/download/upload/…
```

`access_type=offline` + `prompt=consent` não são detalhe: sem os dois o Google
devolve refresh token **só na primeira autorização da vida**, e a segunda conta
(ou uma reautorização) chega sem refresh — falha silenciosa clássica.

### 3.2 Tabela `integration_account`

```sql
CREATE TABLE integration_account (
    id              UUID PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    provider        VARCHAR(40)  NOT NULL,      -- "google-drive"
    label           VARCHAR(80)  NOT NULL,      -- "pessoal" | "trabalho"
    account_ref     VARCHAR(200) NOT NULL,      -- e-mail devolvido pelo provedor
    credential_id   UUID NOT NULL,              -- → credential (cofre Fernet)
    scopes          JSONB NOT NULL DEFAULT '[]',
    is_default      BOOLEAN NOT NULL DEFAULT false,
    status          VARCHAR(20)  NOT NULL DEFAULT 'active',
                    -- active | needs_reauth | revoked
    status_detail   TEXT,
    last_used_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, provider, account_ref)
);
CREATE UNIQUE INDEX ON integration_account (user_id, provider)
    WHERE is_default;
```

Diferença importante em relação a `service_connection`: **conexão é global,
conta é do usuário**. Uma conexão HTTP é infra da casa; uma conta OAuth é a
identidade de uma pessoa. Por isso tabela separada e não uma coluna nova.

`UNIQUE (user_id, provider, account_ref)` impede a mesma conta Google entrar
duas vezes com rótulos diferentes — que é como se chega a dois refresh tokens
vivos para a mesma conta, um deles invalidado sem aviso.

### 3.3 Credencial `kind: "oauth"`

Novo `KindSpec` em `core/credentials/kinds.py`, seguindo a regra de ouro do
arquivo (segredo cifrado / campos em claro):

- **segredo**: JSON com `{refresh_token, access_token, expires_at}` — cifrado
  inteiro, write-only, jamais volta ao painel nem a LLM nenhum;
- **fields** (em claro): `account` (e-mail), `provider`, `scopes`.

Os campos em claro são o que deixa o painel mostrar "daniel@empresa.com" sem
decifrar nada — exatamente o motivo pelo qual `fields` existe.

O `client_id`/`client_secret` do app Google **não** são credencial de usuário:
vão para a env (`GOOGLE_OAUTH_CLIENT_ID` / `_SECRET`), como o Spotify já faz.

### 3.4 Auditoria: reaproveita `service_request_log`

Toda chamada `cloud_*` grava na tabela que já existe, com
`connection_id = "google-drive:pessoal"`. Assim a aba Integrações tem **uma**
visão de auditoria, não duas. É a lição de observabilidade da skill HA: a
verdade tem que ser consultável num lugar só.

---

## 4. Fluxo OAuth

### 4.1 Conectar (painel)

```
1. Painel  → GET /api/v1/integrations/google-drive/authorize?label=trabalho
2. Core    → 302 para o consent do Google
              state = HMAC(user_id|provider|label, panel_jwt_secret)
              access_type=offline & prompt=consent
3. Google  → GET /api/v1/integrations/callback?code=...&state=...
4. Core    → troca code por {access, refresh, expires_in}
           → descobre o e-mail (provider.identity)
           → grava segredo no cofre (kind=oauth)
           → cria/atualiza integration_account
           → HTML "pode fechar esta aba"
```

O `state` assinado é o mesmo padrão de `api/routes/spotify.py` — rota **fora**
do `require_panel_jwt`, porque o callback é redirect de browser e não carrega o
JWT. Reaproveitar o helper, não reescrever.

### 4.2 Refresh (caminho quente)

Dentro do `OAuth2AuthScheme`, na hora de montar a request:

```
se expires_at - now > 120s  → usa o access token
senão → adquire o LOCK DA CONTA
        recheca expires_at (outra corrotina pode ter renovado)
        POST token_url grant_type=refresh_token
        regrava o segredo no cofre
        solta o lock
```

O lock por conta não é zelo: duas tools `cloud_*` no mesmo turno renovando em
paralelo produzem dois access tokens e, em provedor que rotaciona refresh, um
refresh token morto. `asyncio.Lock` por `account_id` no processo resolve o caso
real (um neo-core só).

**Falha de refresh** (`invalid_grant`): conta vai para `needs_reauth`, dispara
notificação pelo `core/notifications` e **a tool devolve erro explícito**. Nunca
"não encontrei nada" — é a mentira do falso-sucesso da skill HA com outra
roupa.

### 4.3 Desconectar

Revoga no provedor (quando há `revoke_url`), apaga a credencial do cofre, marca
a conta `revoked`. Atenção a uma assimetria que já existe: quem recusa apagar
uma credencial em uso é a **rota** (`api/routes/connections.py`, varrendo as
conexões), não o `CredentialStore`. Essa varredura precisa passar a olhar
`integration_account` também — ou a credencial de uma conta conectada some pelo
painel sem ninguém reclamar.

---

## 5. Multi-conta: como a conta é escolhida

Regra, em ordem:

1. **Nenhuma conta conectada** → as tools `cloud_*` **não entram no toolset**.
   Mesma doutrina de `browser_local_task` com a flag off: oferecer a tool seria
   pura tentação, e o modelo prometeria o que não acontece.
2. **Uma conta** → implícita. O parâmetro `account` é opcional e ignorado.
3. **Duas ou mais**:
   - o pedido nomeia ("na do trabalho", "na minha conta da empresa", o e-mail) →
     resolve por `label` ou `account_ref`;
   - **leitura** (`cloud_search`, `cloud_get`, `cloud_download`) sem nomear →
     usa a `is_default`, e **diz na resposta qual conta usou**;
   - **escrita** (`cloud_upload`, `cloud_mkdir`, `cloud_move`, `cloud_delete`)
     sem nomear → **pergunta**. Não há default para escrita.

A assimetria leitura/escrita é a mesma do gateway (`default_read=allow`,
`default_write=confirm`) e pelo mesmo motivo: ler na conta errada custa uma
resposta; escrever na conta errada custa um arquivo no lugar errado, visível
para outras pessoas.

`cloud_list_accounts` existe para o modelo poder resolver ambiguidade **sem
chutar** — e a description diz isso com todas as letras.

---

## 6. Bytes: download, upload e o ledger

### 6.1 Download

`cloud_download(file_id)` **não** devolve conteúdo. Ele:

1. streama do provedor direto para `FileStore.ingest_stream(origin="cloud")`;
2. grava evento `pulled_cloud` no `file_link` (§6.3);
3. devolve `{file_id_catalogo, nome, tamanho, mime, retention}`.

A partir daí o Neo usa o que já existe: `file_info`, `file_read` em fatias,
`file_stage` para o sandbox. **O conteúdo nunca passa pelo contexto** — que é o
princípio do `file-store-plan.md §5`.

`retention="temp"` por default. Isso é deliberado: baixar o Drive dele para a
VPS e deixar lá é exatamente o que ele já pediu para não acontecer com material
de conversa. `perm` só quando ele pedir.

### 6.2 Upload

`cloud_upload(file_id=... | sandbox_path=...)` aceita as duas origens que
existem hoje (catálogo e `/workspace`) e usa upload resumable acima de ~5 MB.

Isso encaixa no gancho que o `Stager` já previu em `core/files/stagers.py`
("browser-agent e gateway entram como novos Stagers sem mexer na tool nem no
store") — aqui é o caminho inverso, um *uploader*, mas a simetria vale.

### 6.3 Ledger

`core/files/ledger.py` hoje conhece três lugares (host, catálogo, sandbox). A
nuvem é o quarto. Concretamente:

- novas ações: `pulled_cloud`, `pushed_cloud`;
- `pulled_cloud` entra em `_CONTENT_ACTIONS` (a cópia baixada passa a ser a
  versão corrente);
- `pushed_cloud` **não** entra (enviar não muda o conteúdo);
- `build_views` ganha a coluna "nuvem: google-drive · pessoal · /Documentos/2026".

Sem isso, "salva isso no meu Drive" e depois "cadê aquele arquivo?" repete a
sessão f33632b4 — o Neo sem saber qual cópia é qual.

---

## 7. Ativação: quando API, quando navegador

É o ponto mais delicado do plano, porque **hoje o Drive já funciona pelo
navegador local** e a mudança é de comportamento, não só de código.

### 7.1 A fronteira

| Pedido | Caminho | Por quê |
|---|---|---|
| "procura a nfse de abril no meu drive" | `cloud_search` | rápido, exato, não ocupa a tela dele |
| "baixa o app-release.apk do drive" | `cloud_download` | vai direto para o catálogo |
| "abre meu drive **pelo meu navegador**" | `browser_local_task` | posse ∧ afordância — o portão já decide isso |
| "quero **ver** você mexendo lá" | `browser_local_task` | o resultado só existe se ele vir |
| compartilhamento, permissão, Google Docs nativo | `browser_local_task` | a API não alcança (ou não na F2) |
| conta **não** conectada | `browser_local_task` | a sessão do browser dele já está logada |
| qualquer coisa do Drive dele | **nunca** `browser_task` | o headless do servidor não tem a sessão dele |

Um ganho colateral que vale registrar: hoje, um pedido **sem** a palavra
"navegador" ("vê se tem nfse de abril no meu drive") não tem caminho bom —
`core/browser_local/gate.py` exige âncora **e** afordância, então o pedido não
vira navegador local, e o `browser_task` do servidor não tem sessão no Drive
dele. A integração fecha esse buraco.

### 7.2 Onde cada regra mora (quatro pontos, nenhum opcional)

**(a) Regra no CORE prompt** — `core/agent/context_selector.py`, no bloco
condicional que já existe (a numeração dinâmica termina em
`nxt = 14 if working_memory_enabled ...`, linha ~744). Texto proposto:

> N. Você tem acesso REAL ao armazenamento em nuvem do usuário pelas tools
> `cloud_*` (quando disponíveis): procurar, ler, baixar, enviar e organizar
> arquivos do Google Drive dele. Quando ele falar do Drive/nuvem dele ("no meu
> drive", "na nuvem", "aquela pasta do drive"), CHAME a tool AGORA, no mesmo
> turno — não ofereça, não peça para ele procurar, e não diga que não tem
> acesso. `cloud_download` NÃO traz o conteúdo para você: ele põe o arquivo no
> catálogo e devolve um `file_id` — leia depois com `file_info`/`file_read`.
> Duas exceções mandam no navegador dele em vez da API: (1) ele pediu a TELA
> dele ("pelo meu navegador", "quero ver"), (2) o que ele quer não existe na
> API — compartilhamento, permissão, ou uma conta que não está conectada. Nesses
> casos use `browser_local_task`. Com DUAS OU MAIS contas conectadas e um pedido
> de ESCRITA sem dizer qual, PERGUNTE antes — nunca escolha a conta no chute.

**(b) Prefixo no router** — `cloud_` entra em `_SKILL_OWNED_TOOL_PREFIXES`
(`core/router/intent_router.py:387`), pelo motivo já escrito lá para `service_`,
`data_` e `file_`: o classificador roteando direto inventaria `file_id`, query e
conta. Quem descobre é o cognitivo.

**(c) Skill `nuvem`** — `kind: tool_backed`, com o autoload por keyword
(`core/skills/library.py:972`), que é o único mecanismo de ativação **medido**
da casa (foi ele que impediu a `financas` de dizer "registrei" sem registrar).
Keywords estreitas, pelo aviso que a skill `compras` carrega no próprio arquivo:
`[drive, google drive, meu drive, nuvem, na nuvem, gdrive, pasta compartilhada]`.
O corpo da skill é o manual: qual tool para quê, como escolher conta, quando
cair para o navegador, e a proibição de afirmar que enviou sem ter enviado.

**(d) Ajuste no gate do navegador** — `core/browser_local/gate.py` não muda de
regra, mas ganha nota: "drive"/"nuvem" **não** são afordância de navegador. Sem
isso, "abre meu drive" poderia ser lido como pedido de tela e a API nunca seria
exercitada.

### 7.3 A skill `compras` precisa de um ajuste

Ela hoje diz, no próprio SKILL.md: *"NÃO use para procurar ARQUIVO (disco,
Drive, anexo) — isso é `run_command` ou `browser_local_task`"*. Vira
`cloud_search` na F3.

---

## 8. Defesas

| Risco | Defesa |
|---|---|
| LLM ver o token | Segredo só materializa dentro do `AuthScheme`, no `SecretMaterial` que serializa `***`. Mesmo mecanismo do gateway, sem exceção |
| Injeção via conteúdo de arquivo do Drive | Conteúdo de arquivo é **informação, nunca instrução** — mesma regra já escrita para conteúdo web (regra 11 do CORE) |
| Exclusão acidental | `cloud_delete` manda para a LIXEIRA, nunca `permanently delete`. Política = `confirm` |
| Sobrescrever arquivo dele | `cloud_upload` com nome existente → cria versão nova ou renomeia; **nunca** sobrescreve sem ele ver (é a lição do `overwrite` de `edit_user_file`) |
| Escopo largo demais | §11 questão 2 |
| Vazamento entre contas | `account_id` resolvido no executor, a partir do `user_id` do turno. A tool nunca recebe `credential_id` |
| Conta de outro usuário | Toda query em `integration_account` filtra por `user_id` — é o buraco que `data_*` teve (`neo-job-tools-dead-on-server-tool-path`) |
| Cópia do material dele na VPS | `retention="temp"` + GC do File Store |

---

## 9. Estrutura de diretórios

```
core/integrations/
    __init__.py
    providers/
        __init__.py         registry de ProviderSpec
        google_drive.py
    oauth.py                authorize/exchange/refresh/revoke
    accounts.py             AccountStore + resolução de conta
    errors.py
core/gateway/
    broker.py               + OAuth2AuthScheme
core/credentials/
    kinds.py                + KindSpec "oauth"
modules/cloud/
    __init__.py
    tools.py                schemas cloud_*
    executor.py             module executor
api/routes/
    integrations.py         authorize/callback + CRUD de contas
db/models/
    integration_account.py
alembic/versions/
    xxxx_integration_account.py
skills/nuvem/SKILL.md
tests/integrations/
    test_oauth_refresh.py       (lock, invalid_grant, expiry)
    test_account_resolution.py  (1 conta, N contas, escrita sem nome)
    test_cloud_tools.py         (download → store → ledger)
neo-panel/src/
    IntegrationsPanel.tsx   (aba nova dentro de /servicos → /integracoes)
```

---

## 10. Fases

### F0 — a área (meio dia)
`/servicos` vira `/integracoes`, com duas abas. Nada de OAuth ainda.
**Aceite:** as conexões HTTP de hoje continuam funcionando na aba 1, sem regressão.

### F1 — OAuth2 no gateway (a fase que vale por si só)
`ProviderSpec`, `integration_account`, `oauth.py`, `OAuth2AuthScheme`,
`kind: "oauth"` no cofre, rotas authorize/callback, aba "Contas conectadas".
**Aceite:** conectar duas contas Google, ver as duas no painel, e um
`service_request` de teste contra `drive/v3/about?fields=user` responder 200 nas
duas — **com o access token expirado de propósito**, provando o refresh.

### F2 — tools `cloud_*`
`cloud_list_accounts`, `cloud_search`, `cloud_get`, `cloud_download`,
`cloud_upload`, `cloud_mkdir`, `cloud_move`, `cloud_delete`, `cloud_link`.
Download no File Store + evento no ledger.
**Aceite E2E:** "procura a nfse de abril no meu drive e baixa" → arquivo no
catálogo, `file_info` responde, ledger mostra a cópia da nuvem.

### F3 — ativação
Regra no CORE prompt, `cloud_` no router, skill `nuvem`, nota no gate,
ajuste da skill `compras`.
**Aceite:** as frases reais dele, medidas contra o log de produção (é o método
de `neo-guard-heuristics-measure-on-prod-corpus`) — "acessa o meu google drive
pelo meu navegador local e vê se tem nfse de abril" continua indo para o
navegador; "vê se tem nfse de abril no meu drive" passa a ir para `cloud_search`.

### F4 — segunda conta e segundo provedor
O teste real da abstração: se OneDrive ou Dropbox sair como **catálogo +
scopes + adapter**, sem tocar em `oauth.py` nem nas tools, o desenho está certo.
Se exigir mexer no núcleo, a generalização estava errada e é hora de consertar —
com dois casos na mão, não com um.

---

## 11. Questões em aberto (decidir antes/durante a F1)

**1. O app OAuth em "Testing" invalida o refresh token em 7 dias.** É o gotcha
que mais dói: com o app em modo de teste no Google Cloud Console, o refresh
token morre em uma semana e ele reautoriza toda segunda-feira. A saída é
publicar o app (fica "não verificado", com tela de aviso na primeira
autorização, mas o refresh estabiliza). **Confirmar na console antes da F1** —
muda a experiência de uso, não o código.

**2. Qual escopo.** `drive.file` só enxerga o que o próprio app criou — não
serve para "procura a nfse de abril". O mínimo real é `drive.readonly`, e
`drive` completo para enviar/organizar. Escopo restrito puxa o item 1 junto.
Decisão dele; a arquitetura suporta pedir escopo incremental (só leitura agora,
escrita quando precisar).

**3. Google Docs nativos.** Não têm bytes: só saem por `/export` escolhendo
formato (docx/pdf/md). Proposta: `cloud_download` exporta por default para o
formato mais fiel e **diz na resposta que exportou**. Ficar em aberto na F2 é
aceitável; ficar em silêncio, não.

**4. Absorver o Spotify.** Depois da F1 existir, o OAuth dele pode migrar para
`integration_account` e o token sair de `user_preference` para o cofre. Não é
pré-requisito de nada, mas é o débito que justifica ter feito genérico.

**5. Notificação de `needs_reauth`.** Basta o cartão no painel, ou o Neo avisa
no chat quando tropeçar? Proposta: as duas — o painel mostra sempre, o chat
avisa na primeira vez que uma tool falhar por isso.

**6. Cota e tamanho.** Teto de download por arquivo (herdar
`NEO_FILES_MAX_FILE_MB`) e o que fazer quando ele mandar baixar uma pasta com
400 arquivos. Proposta: `cloud_download` é um arquivo por chamada; pasta inteira
vira `agent_job`.

---

## 12. Por que não fazer nada disso (o argumento contrário, honesto)

O navegador local **já resolve** Drive hoje, com a sessão dele já logada, sem
app OAuth, sem verificação do Google, sem escopo, sem refresh token. O custo
desta integração inteira é pago para ganhar velocidade, exatidão e a capacidade
de o Drive participar de um pipeline (baixar → analisar no sandbox → devolver)
sem depender de uma tela aberta.

Se o uso real for "de vez em quando eu peço para ele olhar uma nota fiscal", a
F1 sozinha não se paga e o navegador continua sendo a resposta certa. O sinal de
que vale: pedidos de Drive **dentro** de tarefas maiores, e a frustração de
esperar o navegador pilotar uma UI para achar um arquivo que uma query acharia
em 300 ms.
