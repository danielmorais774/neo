# Plano: Autenticação da API HTTP do neo-core

**Data:** 2026-09-25 (plano) / 2026-09-26 (implementação)
**Status:** **F0, F1, F2, F4 e F5 IMPLEMENTADAS** — `NEO_API_AUTH_MODE=shadow`
por padrão, então **nada barra ainda**. F3 é virar a chave, e é decisão sua:
estado detalhado no §10.
**Objetivo:** nenhuma rota do neo-core responde a quem não provou identidade, e o
`user_id` do turno passa a sair da **credencial**, nunca do que o cliente declara.
**Depende de:** pareamento de device (`connected_device.pairing_token_hash`,
`api/routes/devices.py:241`), tabela `user` (`db/models/user.py`), `api/security.py`.
**Documentos irmãos:** `device-control-plan.md` §0 e §2 (de onde vem o token de
aparelho e por que ele nasceu), `release-request-plan.md` §11 (por que o JWT do
painel **não aprova** nada).

---

## 0. TL;DR das decisões

| Tema | Decisão |
|---|---|
| A pergunta | Deixa de ser "esta rota tem `Depends(require_panel_jwt)`?" e passa a ser **"quem é o dono deste pedido?"** |
| Duas credenciais, um princípio | **Token de aparelho** (desktop/notch/celular, já existe e já é forte) e **login de usuário** (painel, a construir sobre `user.password_hash`). As duas resolvem para o MESMO `Principal(user_id)` |
| Ponto único de decisão | `api/auth/principal.py`. Uma resolução, usada por rota HTTP, WS e gRPC — precedente: `_device_from_token` sendo reusada pelo `release.py` |
| `user_id` do corpo/query | Vira **advisório**. O valor que vale é `principal.user_id`. Declarar outro = 403, não "ignora em silêncio" (ignorar esconde o bug do cliente) |
| JWT do painel | **Legado em extinção.** Estático, sem `exp`, `sub="neo-panel"` — não diz QUEM é. Só sobrevive em telas de administração **somente-leitura**, e morre em F5 |
| Ordem das fases | **sombra → clientes carregarem credencial → enforce.** Mesma disciplina do portão de liberação: medir antes de barrar. Virar a chave antes dos clientes derruba o desktop dele no meio de um turno (local = prod, `--reload`) |
| Autenticar ≠ autorizar | F0–F3 fecham "quem entra". **F4** fecha "o que cada um vê": hoje, autenticado ou não, `/session/{id}/history` entrega a conversa de qualquer sessão |
| Sem dependência nova | JWT já é HMAC à mão (`api/security.py`); senha vai de `hashlib.pbkdf2_hmac` (stdlib). Nada de passlib/PyJWT |

---

## 1. O estado de hoje, medido

`api/main.py:1013-1050` monta 30 routers. Dos grupos que você listou, **nenhum**
tem credencial:

| Rota | Guarda hoje | O que dá pra fazer sem credencial |
|---|---|---|
| `POST /api/v1/desktop/chat`, `/chat/stream`, `/execute` | nenhuma | conversar como você, com o toolset inteiro (terminal, sandbox, casa, e-mail, memória) |
| `POST /api/v1/desktop/chat/command-response` | nenhuma | aprovar comando pendente de **qualquer** sessão (`desktop.py:591` — nem confere dono) |
| `GET /api/v1/desktop/sessions`, `/session/{id}`, `/session/{id}/history` | nenhuma | ler qualquer conversa; o filtro é `service`, não dono (`desktop.py:641`) |
| `DELETE /api/v1/desktop/session/{id}` | nenhuma | apagar sessão |
| `GET/POST /api/v1/desktop/jobs…` (4) | nenhuma | listar, ler log e cancelar tarefas |
| `GET /api/v1/desktop/working-memory/notes(/{slug})` | nenhuma | ler o caderno inteiro |
| `GET /api/v1/desktop/tools` | nenhuma | inventário das ferramentas (mapa pro atacante) |
| `POST /api/v1/context/retrieve`, `/prompt-preview` | nenhuma | despejar o bloco do PKS e o prompt montado |
| `POST /api/v1/files`, `GET /api/v1/files/{id}/download` | nenhuma | subir arquivo no catálogo e baixar qualquer um por id |
| `POST /api/v1/quick-help/ask` | nenhuma | gastar LLM na sua carteira |

Três achados que mudam a forma do plano:

**1.1 — O `user_id` declarado CRIA usuário.** `db/async_client.py:157` chama
`ensure_user_exists` ao abrir sessão, e `:973` insere a linha se não existir. Ou
seja: mandar um UUID novo no corpo cria um usuário; mandar o SEU
(`5a998544-c3f3-423f-bebb-1cc42fca6063`, que está hardcoded em
`neo-desktop-tauri/src-tauri/src/lib.rs:231` e em `neo-panel/src/lib/chatApi.ts:13`)
faz o pedido escrever e ler a sua memória. É provavelmente a origem dos 5
usuários de produção com 1 real. **Autenticar sem amarrar o `user_id` à
credencial não resolve nada** — por isso a regra do §0.

**1.2 — O JWT do painel é um segredo de código, não uma identidade.**
`shared/config.py:1016` traz `panel_jwt_secret` com **default embutido**, e o
`.env` da raiz **não define `PANEL_JWT_SECRET`** → produção roda com o default.
O token já minerado com esse segredo está commitado em quatro arquivos do painel
(`main.tsx:7`, `SessionsTab.tsx:5`, `WhatsAppModal.tsx:6`,
`WhatsAppStatusPanel.tsx:6`), sem `exp`. Quem lê o repo emite token de admin. E
como `sub="neo-panel"`, ele não diz de quem é — foi exatamente por isso que
`release.py` recusou o painel como aprovador.

**1.3 — O aparelho já tem credencial; ele só não a usa no HTTP.** O desktop
guarda o token em `~/.../neo-desktop/device-token` 0600 (`lib.rs:80`), manda na
metadata do gRPC (`ai_agent.rs:164`) e na query dos WS (`websocket.rs:73`). Mas
as ~8 chamadas HTTP do Rust (`commands/mod.rs:394` upload, `:436` download,
`:1141` history, `:1211` sessions, `:1707` quick-help, `:2214`/`:2239` notas,
`:2313` jobs) vão **sem header nenhum**. F1 é, em boa parte, um helper de
requisição no Rust — não um sistema novo.

### 1.4 O que o gRPC já tem, e o buraco que sobra

`_grpc_device_authorized` (`grpc_server.py:190`) valida `x-device-token` e é
chamada em `:305`. Dois problemas, os dois de amarração:

- A chamada está **dentro de `if device_id is None:`** (`:286`) e só no primeiro
  `user_input` do stream. Sem `device_id` na metadata e sem `session_id`, o
  `device_id` sai falsy e a checagem **não roda** — mesmo com
  `NEO_DEVICE_AUTH_REQUIRED=true` (ligada no `.env` da raiz, linha 90).
- `uid_uuid` vem de `desktop_msg.user_id` (`:276`), do corpo. Um token **válido**
  do aparelho A pode se declarar dono de qualquer `user_id`. Autenticou; não
  amarrou.

---

## 2. O que se reaproveita (e por isso o plano é pequeno)

- `_device_from_token` (`devices.py:241`): resolve `token → (user_id, device_id)`
  pelo `sha256`, sem confiar em nada do cliente. **É o molde.**
- `POST /devices/pair` (`devices.py:182`): código de 6 dígitos de uso único, TTL
  5min, emitido por quem tem credencial; o token cru sai uma vez e o banco só
  guarda o hash.
- `api/security.py`: JWT HS256 escrito à mão com `hmac.compare_digest`. A
  verificação de assinatura serve; o que falta é `exp` e um `sub` que seja
  usuário.
- `release.py`: o precedente de **dois routers no mesmo prefixo** — um autenticado
  por aparelho (decide) e um por JWT do painel (só lê). É esse desenho que se
  repete aqui.
- `spotify.py:47` já assina `state` com HMAC — o molde pronto pra URL de download
  assinada do §7.3.

---

## 3. O `Principal`

Arquivo novo `api/auth/principal.py`:

```python
@dataclass(frozen=True)
class Principal:
    kind: Literal["device", "user", "panel_legacy"]
    user_id: UUID | None      # None só em panel_legacy
    device_id: str | None
    label: str                # "device:PC da sala", "user:daniel", "panel(legado)"
```

Resolução, nesta ordem (primeira que casar ganha):

1. `X-Device-Token` (header) ou `?token=` → `_device_from_token` →
   `kind="device"`. Query param existe pelo caso do `<a download>` (§7.3) e dos WS.
2. `Authorization: Bearer <jwt>` com `typ="access"` e `sub=<uuid de usuário>` →
   `kind="user"`. Emitido pelo login do §5.
3. `Authorization: Bearer <jwt>` com `sub="neo-panel"` → `kind="panel_legacy"`,
   `user_id=None`. Aceito só enquanto o modo permitir.

Três dependências, e só três:

- `require_principal` — qualquer credencial válida. Para leitura não-pessoal
  (`/desktop/tools`).
- `require_owner` → devolve `Principal` com `user_id` garantido. **Recusa
  `panel_legacy`** (não tem dono). É a dependência de tudo que toca conversa,
  memória, arquivo, job ou LLM.
- `require_admin_read` — telas de administração somente-leitura; aceita
  `panel_legacy` enquanto o modo permitir.

### 3.1 A regra do `user_id`

Um helper, usado por toda rota que hoje recebe `user_id`:

```python
def owner_of(principal: Principal, declarado: UUID | None) -> UUID:
    if declarado and declarado != principal.user_id:
        # sombra: loga; enforce: 403
        ...
    return principal.user_id
```

Sem isso, nada muda de verdade: a credencial diria "é o Daniel" e o corpo
continuaria dizendo "grave no PKS de outro". O 403 (em vez do descarte silencioso)
é de propósito — o `DEFAULT_USER_ID` do painel e o fallback do Rust precisam
**aparecer** pra serem removidos.

---

## 4. O interruptor: `NEO_API_AUTH_MODE`

`shared/config.py`, no mesmo espírito de `neo_device_auth_required` e
`neo_release_gate_mode`:

| Modo | Comportamento |
|---|---|
| `off` | como hoje. Existe pra rollback de 1 linha em incidente |
| `shadow` (primeiro) | resolve o principal, **deixa passar** e loga `[auth] rota=… principal=… declarado=… ip=…`. Mede quem chama o quê sem derrubar nada |
| `enforce` | 401 sem credencial; `panel_legacy` ainda vale nas telas de administração |
| `strict` | `panel_legacy` recusado. Só login real e token de aparelho |

**Por que sombra primeiro, e não "é só ligar":** a máquina dele é produção via
túnel, o core roda `--reload` e salvar arquivo é deploy
([[neo-dev-topology-prod-tunnel]]). O `NEO_DEVICE_AUTH_REQUIRED` ensinou duas
coisas que valem aqui: mudar `.env` exige `docker compose up -d neo-core` (≈1min,
recarrega embeddings — `--reload` **não** pega env), e a recusa tem que vir
**antes** do `accept()` no WS, senão o cliente lê 101+close como queda de rede e
entra em retry infinito.

---

## 5. Credencial de usuário (o que falta existir)

Rotas novas em `api/routes/auth.py`, prefixo `/api/v1/auth`, **sem** guarda:

- `POST /login` `{username, password}` → `{access_token, expires_in, user: {...}}`.
- `POST /refresh` → novo access token (refresh opaco no banco, revogável).
- `GET /me` → quem sou eu (usa `require_owner`; é o teste de fumaça do painel).
- `POST /logout` → revoga o refresh.

Decisões:

- **Hash**: `pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>` via
  `hashlib.pbkdf2_hmac` (stdlib — sem dependência nova). O formato legado
  (`db/seeds.py:13`, `sha256("neo-salt"+senha)`, sem salt por usuário, e a seed
  `daniel`/`123456`) é aceito **uma última vez** e re-hasheado no primeiro login
  bem-sucedido. Trocar a senha da seed faz parte de F2, não é opcional.
- **JWT**: mesma função de assinatura de `api/security.py`, agora com
  `sub=<user_id>`, `typ`, `iat`, `exp` (access curto, 30min) e `jti`.
  `panel_jwt_secret` passa a ser obrigatório do ambiente — o core **recusa
  subir** com o default fora de dev. Hoje ele roda em produção com o default.
- **Antienumeração e força bruta**: resposta e tempo iguais para usuário
  inexistente e senha errada; contador por `username`+IP em memória com
  janela — o mesmo lugar onde já vive o TTL do código de pareamento
  (`modules/devices/pairing.py`).
- **Não é multiusuário.** Continua um dono. O login existe pra que "só eu, com o
  meu usuário" seja **verificável**, e pra o painel poder aprovar liberação
  (§11 do `release-request-plan`, que hoje recusa o painel justamente por isso).

---

## 6. Fases

### F0 — Sombra (1 sessão, nada quebra)
`api/auth/principal.py` + `NEO_API_AUTH_MODE=shadow`, aplicado como dependência
de router em `desktop`, `context`, `quick_help`, `files`. Log estruturado por
chamada. **Entregável real:** um dia de log responde qual cliente chama qual rota
sem credencial — inclusive o que eu não achei lendo código.

### F1 — Clientes passam a carregar a credencial
- **Rust**: um `core_http(...)` que injeta `X-Device-Token` de
  `crate::device_token()`; trocar as ~8 chamadas de `commands/mod.rs`. Sem
  token → erro claro "aparelho não pareado", não 401 cru.
- **Webview do desktop**: `ChatPage.tsx:458,465` chama `/api/v1/missions/*` com
  `fetch` puro e sem header — essas rotas **já** estão atrás do JWT do painel,
  então isso **já falha hoje** (401 silencioso no aprovar/ack de card de missão).
  Passa por comando Tauri. É bug existente que F1 conserta de graça.
- **Painel**: tela de login → token em memória (`sessionStorage`), wrapper de
  `window.fetch` de `main.tsx` mandando o token do usuário. Sai a constante
  `PANEL_JWT` dos 4 arquivos e o `DEFAULT_USER_ID` do `chatApi.ts`.
- **`scripts/pks_first_message_test.sh:167`** chama `/desktop/chat` — precisa de
  token de teste, senão o smoke test quebra no enforce.

### F2 — Login real
`api/routes/auth.py` + hash novo + `exp` no JWT + `PANEL_JWT_SECRET` no `.env` da
raiz (e a senha da seed trocada). Aqui `panel_legacy` já não é necessário pro
painel funcionar; ele só continua aceito pelo modo.

### F3 — `enforce`
Virar a chave **depois** de F1+F2 e de o log da sombra vir limpo por alguns dias.
Ordem copiada da lição do portão de liberação: infra → cliente → medir → barrar.

### F4 — Autorização (o dono, não só a porta)
Autenticação sozinha deixa buracos que já estão no código:
- `GET /desktop/sessions` (`desktop.py:641`) filtra por `service` e **não por
  dono**; `/session/{id}/history` (`:712`) não confere dono nenhum;
  `DELETE /session/{id}` também não. `conversation_session.user_id` existe mas é
  **nullable** → precisa de backfill antes do filtro, senão as conversas antigas
  desaparecem da lista (e aí o seletor de sessões some, [[neo-desktop-session-picker]]).
- `POST /chat/command-response` (`:591`) aprova pendência de qualquer sessão:
  amarrar ao principal + conferir que a sessão é dele.
- Jobs e notas já têm dono (`_owned_job_or_404`, `:1050`), mas o dono sai de
  `_resolve_working_memory_user(db, user_id)` (`:853`), que aceita o `user_id`
  **do cliente** e, quando ele falta, chuta "o único usuário do banco". Troca por
  `principal.user_id`.
- `ensure_user_exists`: para de criar usuário a partir de valor declarado. Criar
  usuário passa a ser ato explícito (seed/CLI).
- gRPC: mover a checagem para fora do `if device_id is None`, recusar sem
  `device_id` quando `required`, e **derivar `uid_uuid` do token**, ignorando
  `desktop_msg.user_id`.

### F5 — `strict` e faxina
- `panel_legacy` sai de vez.
- **`api/routes/letta.py` deleta.** É um proxy catch-all
  (`/api/v1/letta/{path:path}`, todos os métodos) para `http://neo-letta:8283`,
  **sem auth**, repassando headers e corpo. Letta foi descartado
  ([[neo-memory-semantic-v3-plan]]); um proxy aberto pra dentro da rede Docker é
  puro passivo.
- `desktop.py:773` tem um **segundo** `GET /sessions` que o de `:641` sombreia —
  código morto, some.
- `/desktop/tools` (`:612`) devolve o inventário de ferramentas: mínimo
  `require_principal`.

---

## 7. Gotchas que vão morder

**7.1 — `panel_jwt_subject` recusa JWT de usuário.** `api/security.py:50` exige
`sub == settings.panel_jwt_subject`. Emitir token com `sub=<uuid>` **falha** na
função atual. A verificação de assinatura tem que ser separada da de identidade.

**7.2 — `X-Panel-Token` além do `Authorization`.** `require_panel_jwt` aceita os
dois. O `Principal` precisa aceitar os dois também, ou alguma tela do painel
morre sem aviso.

**7.3 — `<a href download>` não manda header.** `ChatMessageBubble.tsx:264` baixa
arquivo por âncora; o wrapper de `window.fetch` não alcança navegação. Fechar
`/files/{id}/download` só com header **quebra o download no painel**. Saída:
URL assinada de vida curta (`?exp=&sig=`, HMAC igual ao `state` do
`spotify.py:47`), emitida por rota autenticada. O Rust, que usa `reqwest`
(`commands/mod.rs:436`), pode usar header e não precisa disso.

**7.4 — Streaming e o 401 tardio.** `/chat/stream` e `/command-response` devolvem
`StreamingResponse`. A dependência resolve **antes** do primeiro byte, então o 401
sai como status — mas quem consome NDJSON tem que tratar status ≠ 200 antes de
ler linha, ou aparece como "stream vazia" ([[neo-pipeline-forced-close-kills-roundtrip]]).

**7.5 — `--reload` no meio do turno.** Mexer em `api/` recarrega o core e derruba
o stream gRPC em andamento ([[neo-grpc-transport-error-reload-storm]]). Editar com
o desktop parado, ou aceitar um turno perdido.

**7.6 — CORS.** `Authorization` e `X-Device-Token` precisam estar em
`allow_headers` do middleware (`api/main.py`), senão o preflight do painel
bloqueia antes de chegar na rota.

**7.7 — Credencial nunca perto do prompt.** O gRPC já resolveu isso: token vai na
metadata da chamada porque o `metadata` do `user_input` **alimenta o contexto do
prompt** (`ai_agent.rs:160-163`). No HTTP, credencial só em header/query — nunca
em corpo que vire metadata de sessão.

---

## 8. Testes

O molde existe: `tests/files/test_routes.py:126`
(`test_catalog_management_requires_panel_jwt`) monta `FastAPI` de verdade com
`TestClient` e db falso. O que cobrir:

- tabela de rota × modo × credencial: `off`/`shadow` passam, `enforce` dá 401 sem
  credencial, `strict` recusa `panel_legacy`;
- `user_id` declarado ≠ dono → 403 em enforce, log em sombra;
- `/chat/command-response` de outra sessão → 403;
- login: senha errada, usuário inexistente (mesma resposta e tempo), hash legado
  migrando, token expirado;
- um teste que **varre `app.routes`** e falha se uma rota nova não tiver nenhuma
  das três dependências. É a única forma de "nada passa sem auth" continuar
  verdade daqui a três features — mesmo truque do
  `tests/authority/test_risk_cobre_o_registry.py`, que falha quando existe tool
  fora da tabela de risco.

---

## 9. O que este plano NÃO resolve

- **Injection continua sendo outro problema.** Auth diz quem pediu; não impede que
  uma página lida no meio do turno faça o modelo agir. É o
  `release-request-plan.md`, e os dois se completam: o portão de liberação precisa
  de um principal confiável, e é este plano que produz um.
- **Não é multiusuário** (sem papéis, sem convite, sem organização).
- **Não fecha a `:8000`** — isso já foi feito na infra em 2026-09-24 (portas em
  `127.0.0.1`, acesso por túnel). Este plano é a segunda camada: se a porta
  reabrir por engano, ou se o sandbox achar um caminho de volta, a rota ainda pede
  credencial.
- **Não cobre a ponte da extensão de música** — medido em 2026-09-26, ver §11.

---

## 11. A ponte da extensão: o buraco que sobra (medido em 2026-09-26)

`ws://127.0.0.1:17872` (`neo-desktop-tauri/src-tauri/src/ext_bridge.rs:701`) é o
único listener local do Neo no Mac, e **não tem autenticação nenhuma**: nem token,
nem checagem de `Origin` — `accept_async` aceita qualquer handshake. Os outros
listeners locais são os vite de dev (`[::1]:5173`, `[::1]:5176`) e o túnel
(`io.coress`); a `neo-browser-local` usa **native messaging** (`connectNative`,
host `com.ifuryst.open_browser_use.extension`, `allowed_origins` travado num id de
extensão) — essa está certa, e é o modelo a copiar.

**Por que importa mais do que parece.** Não é só "alguém mexe na música":

1. `set_connection` guarda **uma** conexão. Quem conectar depois **desloca a
   extensão de verdade** — não coexiste, substitui.
2. O que entra por `type:"tabs"` vira `ext_bridge.snapshot()` →
   `metadata["media_context"]["browser_tabs"]` (`services/ai_agent.rs:284`) → chega
   ao turno e é lido pela skill de música (`core/skills/music/service.py:293`). Os
   campos `title`/`url` são string livre: é um canal de **injeção** para dentro do
   prompt, vindo de fora do modelo de ameaça do `release-request-plan`.
3. `type:"media_event"` faz o desktop chamar `POST /api/v1/eventos/midia`
   **com o token do aparelho**. Ou seja: a credencial que acabei de dar ao cliente
   não protege nada se o gatilho local é aberto — o atacante não precisa da
   credencial, ele pede para quem tem.
4. Uma **página web** manda `Origin` no handshake de WebSocket, e o servidor não
   olha. As restrições de rede local do Chrome (Private Network Access) podem
   barrar ou pedir permissão dependendo da versão — mas isso é mitigação do
   navegador, não defesa da ponte.

### ✅ Corrigido em 2026-09-26 — credencial **e** `Origin`

Os dois primeiros degraus foram feitos juntos (a opção 1 sozinha não fecharia
"qualquer processo local", e a 2 sozinha não diria *quem* é o cliente):

- **Token obrigatório no handshake.** `crate::ext_bridge_token()` gera na primeira
  execução e guarda em `~/Library/Application Support/neo-desktop/ext-token`
  (0600), mesmo padrão do `device-token`. A ponte **não sobe** se não conseguir a
  credencial — subir sem ela seria subir sem autenticação, que é o que isto fecha.
  Comparação em tempo constante (`tokens_iguais`): 64 hex adivinhados byte a byte
  é viável; de uma vez, não.
- **`Origin` recusado quando é web.** Aceita `chrome-extension://`,
  `moz-extension://`, `safari-web-extension://`, `null` **ou ausência**. A regra é
  contraintuitiva de propósito: um service worker de extensão pode não mandar
  `Origin`, uma página **sempre** manda a dela — inverter deixaria a ponte
  dependente da versão do navegador.
- **Recusa ANTES do upgrade** (`accept_hdr_async` devolvendo `ErrorResponse` →
  HTTP 401/403). Aceitar-e-fechar viraria 101+close, que o cliente lê como queda
  de rede e responde com reconexão infinita — a mesma lição dos canais WS do core.
- **Onde a credencial aparece:** aba Dispositivos do desktop, cartão "Extensão de
  música" (mascarada, com Mostrar/Copiar). Mostrar esta credencial é o ponto dela
  — existe para ser copiada para outro programa; o `device-token` continua sem
  aparecer em lugar nenhum.
- **Extensão v1.3.0:** guarda o token em `chrome.storage.local` (o worker MV3 é
  efêmero), apresenta em `?token=`, e o popup distingue as três causas de
  "desconectado" — sem token, recusado, ou o Neo fechado. O WebSocket do navegador
  não expõe o status HTTP de um handshake recusado (vira close 1006), então isso é
  inferência honesta, não leitura do servidor.

Testes: `handshake_auth_tests` em `ext_bridge.rs` (8 casos — inclusive
"página web não passa" e "prefixo certo não basta"). 81 testes Rust no total.

**Ainda por fazer (opção 3):** trocar a porta por **native messaging**, como a
`neo-browser-local` já faz — sem porta nenhuma, e o Chrome garante o id da
extensão. É o certo a longo prazo; o token fecha o buraco hoje.

**O caminho que apaga este buraco em vez de blindá-lo:** `browser-surface-plan.md`
— a extensão vira um device pareado e fala com o core pelo canal de controle, de
SAÍDA. Sem listener local, o vetor "uma página se passa pela extensão" deixa de
existir.

**Deploy (as duas pontas, senão a música para):** reiniciar o `tauri dev` (o
binário em execução ainda é o antigo) e recarregar a extensão em
`edge://extensions` — o deploy dela é manual —, depois colar o token no popup.

---

## 10. Estado da implementação (2026-09-26)

### O que entrou

| Fase | Estado | Onde |
|---|---|---|
| F0 sombra | ✅ | `api/auth/` (`principal.py`, `jwt.py`, `passwords.py`, `throttle.py`, `signed_url.py`); `NEO_API_AUTH_MODE` em `shared/config.py` |
| F1 clientes | ✅ | Rust: `with_device_token` em `lib.rs` + 10 chamadas em `commands/mod.rs`/`ext_bridge.rs`; card de missão virou comando Tauri (`mission_command`); painel com login |
| F2 login | ✅ | `api/routes/auth.py` (`/login`, `/refresh`, `/logout`, `/logout-all`, `/me`, `/mode`), tabela `auth_refresh_token`, PBKDF2 com migração do hash legado |
| F3 enforce | ⬜ | **decisão sua** — ver "Como subir os degraus" |
| F4 dono | ✅ | escopo por dono em sessões/jobs/notas/arquivos; `command-response` amarrado à sessão; `ensure_user_exists` fecha em enforce; gRPC deriva o dono do token |
| F5 faxina | ✅ | `api/routes/letta.py` deletado (+ `LettaPanel` do painel), `GET /sessions` duplicado removido |

Testes: `tests/auth/` (67 casos) — resolução de identidade, degraus, amarração do
`user_id`, login/hash/rotação, **varredura de `app.routes`** e um teste de
integração que bate nas 20 rotas do §1 contra o `api.main:app` de verdade. Suíte
completa: 3258 passando.

### Decisões que mudaram em relação ao plano

- **`require_panel_jwt` passou a aceitar access token de usuário** (além do
  estático). Sem isso, o painel com login perderia ~20 telas de administração de
  uma vez, e cada router precisaria de edição. Assim o `strict` mata o token
  estático sem tocar em nenhuma delas.
- **`missions.router` trocou panel JWT por `require_principal`.** Não é só tela de
  administração: o desktop decide card de missão por ali — e fazia isso com
  `fetch` sem header, ou seja, tomando 401 em silêncio desde que o panel JWT
  entrou nesse router. O card do chat agora passa pelo Rust.
- **`GET /devices/resolve` mudou de router** (painel → `ws_router`) e passou a
  exigir token de aparelho, derivando o `user_id` dele. Estava atrás de uma
  credencial que o único cliente dela não tem: nunca respondeu. E o `user_id`
  vinha como query param, então bastava declarar o UUID de alguém.
- **Rotas que a varredura encontrou e o plano não citava:** `GET /datasets/tipos`
  (a própria docstring do módulo dizia "painel JWT" e a dependência faltava),
  `POST /eventos/midia` e `GET /eventos/inscricoes` (ganharam `require_principal`;
  a primeira dispara ação de mídia).
- **Sessão/arquivo com dono NULL continua visível.** A coluna é anulável e há
  conversa anterior à auth; filtrar faria o histórico desaparecer do seletor de
  sessões. `scripts/backfill_session_owner.py` tira essa exceção do caminho.
- **`GET /auth/mode` é público.** O painel precisa saber se login é obrigatório
  *antes* de ter credencial. Modo é política, não segredo.
- **A tela de login do painel aparece por AUSÊNCIA DE TOKEN, não pelo modo.** A
  primeira versão só a mostrava em `enforce`/`strict` — errado: com o token
  estático fora do fonte, "entrar direto" em `shadow` significava entrar **sem
  credencial nenhuma**, e as telas de administração passam por
  `require_panel_jwt`, que recusa a ausência de token em *qualquer* modo. O painel
  abria e cada tela morria em 401 sem dizer por quê. O `/auth/mode` continua sendo
  consultado, mas só para EXPLICAR a situação na tela. Dispensa única:
  `VITE_PANEL_TOKEN`.

### Como subir os degraus

1. ~~**Migração** (a tabela do refresh token)~~ — **FEITA em 2026-09-26**:
   produção estava em `f1a2b3c4d5e6` (o pai exato) e subiu para `g1h2i3j4k5l6`.
   Head único; a história de três heads foi resolvida pelo merge `i2j3k4l5m6n7`.
2. **Segredo próprio** no `.env` da RAIZ: `PANEL_JWT_SECRET=$(openssl rand -base64 48)`.
   Isso invalida o token estático que esteve commitado — e é o que torna o
   `.env.example` verdade. Exige `docker compose up -d neo-core` (≈1min; `--reload`
   **não** pega env).
3. ~~**Senha do dono**~~ — **FEITA em 2026-09-26** (ver acima). O caminho de
   migração do hash legado segue no código, mas não foi exercido em produção:
   não havia hash nenhum para migrar.
4. **Ler a sombra** por alguns dias: `grep '\[auth:sombra\]' logs/app.log`. Ela
   diz qual cliente chama qual rota sem credencial — inclusive o que não aparece
   lendo código.
5. **`NEO_API_AUTH_MODE=enforce`** quando o log estiver limpo (+ `up -d`).
6. **`strict`** depois de confirmar que o painel entra por login. Aí o token
   estático morre.

Rollback em qualquer ponto: `NEO_API_AUTH_MODE=off`.

### ✅ A identidade do dono, resolvida em produção (2026-09-26)

**Não existia usuário nenhum.** A seed do `db/seeds.py` nunca rodou neste banco:
as 5 linhas de `user` foram todas criadas pelo `ensure_user_exists` a partir de
`user_id` declarado por cliente (duas delas são literalmente UUID de teste,
`11111111…` e `00000000…0001`), **todas sem `username`, sem `email` e sem
`password_hash`**. Ou seja: "recuperar a senha do painel" não era possível porque
credencial de usuário nunca chegou a existir — só o JWT estático.

Qual linha é o dono, medido e inequívoco:

| id | devices | sessões | notas |
|---|---|---|---|
| **5a998544-c3f3-423f-bebb-1cc42fca6063** | **4** | **694** | **77** |
| 00000000-…-0001 | 2 | 2 | 0 |
| 11111111-…-1111 | 0 | 1 | 0 |
| f5e4a783-… / 62e46f16-… | 0 | 0 | 0 |

Aplicado: `username='daniel'`, `display_name='Daniel Morais'` e senha PBKDF2
(600k iterações) **nessa linha**. O hash foi calculado no Mac e só ele viajou até
a VPS — a senha em claro nunca passou por lá. Trocar depois:
`scripts/set_password.py daniel` (revoga os refresh tokens de tabela).

Validado contra o core de produção: `/auth/login` 200 com
`user.id = 5a998544…`; `/auth/me` devolve `principal.kind = "user"`; `/desktop/sessions`,
`/desktop/working-memory/notes` (77 notas, `user_id` certo), `/files` e `/app-logs`
respondem 200 **com o token de usuário** — inclusive as telas que antes eram só
panel JWT. Senha errada e usuário inexistente dão 401 iguais.

**Defeito achado por essa validação e corrigido:** `/auth/me` respondia **200 com
`user: null`** para um token inventado, porque em `shadow` o portão deixa passar.
Numa rota cujo payload *é* a identidade, isso transforma "quem sou eu" num "sim".
Agora ela dá 401 sem dono em qualquer modo — nenhum cliente antigo a chama, ela
nasceu nesta mudança. Coberto por
`tests/auth/test_login.py::test_me_recusa_token_invalido_mesmo_em_sombra`.

### Pendências conscientes

- **Backfill do dono das sessões** não foi rodado (precisa do banco de produção):
  `scripts/backfill_session_owner.py --dry-run` primeiro.
- **Mobile e notch** não foram tocados: o notch fala por IPC com o Rust (herda a
  credencial dele) e o `neo-mobile` ainda não chama estas rotas (só TODOs).
- **`scripts/pks_first_message_test.sh`** chama `/desktop/chat` direto; no
  `enforce` ele precisa de um token de teste.
- **WebSocket de notificações e `/ws/devices/online`** seguem no esquema antigo
  (`NEO_DEVICE_AUTH_REQUIRED`, já ligado) — não passaram pelo `Principal`.
