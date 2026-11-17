# Plano: Controle de Devices (cross-device)

**Data:** 2026-08-17
**Status:** **F0–F5 EM PRODUÇÃO** (2026-08-17/18), validadas ponta a ponta contra
a VPS. `NEO_DEVICE_AUTH_REQUIRED=true` **ligada** — todo canal de device exige
token de pareamento.
**Caminho escolhido:** **A** (canal de controle dedicado + tools tipadas), não
`run_command` com `target_device`.
**Depende de:** registro de devices (`connected_device`, `DevicesWSRegistry`),
`DeviceNotificationsBroadcaster`, skills/tools do neo-core, neo-desktop-tauri.

Objetivo: "abaixa o volume do PC da sala" dito no celular/desktop A faz o
desktop B abaixar o volume de verdade — com dono verificado, nome humano, e
resultado real (não "acho que mandei").

---

## 0. O achado que muda o plano: não existe credencial de desktop pra reusar

A ideia era vincular o controle "ao auth que o desktop-tauri já usa pra se
credenciar". Esse auth **não existe**:

- `neo-desktop-tauri/src-tauri/src/lib.rs:129` — o `user_id` vem de
  `std::env::var("NEO_USER_ID")` com **fallback hardcoded**
  (`5a998544-c3f3-423f-bebb-1cc42fca6063`). Não há login, token nem sessão.
- `api/security.py` — o `require_panel_jwt` valida um JWT **estático e
  compartilhado**, com `sub == "neo-panel"`. Autentica *o painel*, não um
  usuário. O segredo está default no `shared/config.py:642` ("Panel auth
  (temporary without login flow)" — o próprio comentário admite).
- `db/models/user.py` tem `password_hash`, mas o único hash que existe é
  SHA-256 com salt fixo `"neo-salt"` em `db/seeds.py:13`, e **não há rota de
  login** em `api/routes/`.
- `api/main.py:664` monta o `ws_router` **sem JWT**, e o `device_id` é query
  param. Hoje qualquer um que alcance a `:8000` se registra como qualquer
  device de qualquer usuário.

Consequência prática: hoje o pior caso é "aparecer online numa lista". Com
`suspend`/`shutdown` no cardápio, o pior caso vira *desligar a máquina do
usuário*. **A autenticação deixa de ser higiene e vira pré-requisito de F1.**

Então "vincular ao auth do desktop" = **criar esse auth**. A proposta é o
mínimo que entrega dono verificável sem construir um sistema de login inteiro:
**token de pareamento por device** (§2), forward-compatible com um login real
depois (o token vira derivado da sessão do usuário em vez de emitido no
pareamento).

---

## 1. Modelo de dados

Colunas novas em `connected_device` (o `user_id` já existe e passa a ser o
**dono de verdade**, porque só é gravado por quem provou posse do token):

```
display_name         varchar(64)  null   -- "PC da sala", "notebook do trabalho"
pairing_token_hash   varchar(128) null   -- sha256 do token; NUNCA o token cru
paired_at            timestamp    null
capabilities         jsonb        null   -- {"volume":true,"power":true,"media":"os+ext"}
```

Duas decisões embutidas:

- **`display_name` é do usuário, não do sistema.** O default pode ser
  `"{model} ({os_platform})"`, mas quem renomeia é o dono — via painel ou
  falando com o Neo ("chama esse aqui de PC da sala").
- **`capabilities` é descoberto, não chutado.** O desktop informa no
  pareamento o que sabe fazer. Sem isso o cérebro vai oferecer `shutdown` num
  device que não implementa, e a tool falha *depois* de prometer — o modo de
  falha de [[neo-ha-false-success-invented-entity]].

Migração: nova revisão alembic + **`make migrate`** (não esquecer; é o passo
que ficou pendente em outros planos).

---

## 2. Autenticação e dono

### 2.1 Pareamento (uma vez por device)

```
POST /api/v1/devices/pair          (autorizado — ver 2.2)
  { hardware_id, device_type, model, os_platform, os_version,
    client_version, capabilities, display_name? }
→ { device_id, device_token }      device_token: 32 bytes urlsafe, mostrado UMA vez
```

O core grava `sha256(device_token)` em `pairing_token_hash` junto com o
`user_id` do autorizador. **Esse é o vínculo de dono.**

O desktop guarda o token ao lado do `device-id` que já persiste em
`dirs::config_dir()/neo-desktop/` (`lib.rs:99`) — mesmo diretório, arquivo
`device-token`, permissão 0600.

### 2.2 Quem autoriza o primeiro pareamento — **a decisão em aberto**

Três caminhos, do mais seguro ao mais cômodo:

1. **Código de pareamento no painel** (recomendado): o painel gera um código de
   6 dígitos válido por 5 min; o desktop pede o código na primeira execução.
   O código carrega o `user_id`. Custa uma telinha no desktop.
2. **Aprovação no painel**: o desktop registra como `pending`, o painel lista e
   você aprova. Sem digitar nada, mas exige olhar o painel.
3. **TOFU restrito à rede confiável**: primeiro registro vindo da LAN/WireGuard
   é aceito automaticamente; da internet, nunca. Zero fricção, e o modelo de
   ameaça vira "quem está na minha VPN". Só é aceitável se a `:8000` **não**
   estiver aberta no firewall da VPS — o `docker-compose.yml:26` publica
   `8000:8000` em `0.0.0.0`, então isso precisa ser **verificado**, não assumido.

Recomendo **(1)**, com **(3)** como atalho opcional pra sua própria LAN.

### 2.3 Uso do token

Todo canal do device passa a carregar o token:

| Canal | Como |
|---|---|
| `/ws/devices/online` | `?device_id=&token=` |
| `/ws/devices/notifications/{id}` | `?token=` |
| `/ws/devices/control/{id}` (novo) | `?token=` — **obrigatório, sem exceção** |
| gRPC `ExecuteTask` / `RegisterDevice` | metadata `x-device-token` |

O core resolve `token → (user_id, device_id)` e **ignora o `user_id` que o
cliente mandar**. É isso que fecha a brecha atual de "digo que sou de quem eu
quiser".

### 2.4 Transição sem derrubar a prod — **concluída**

O neo-core roda com `--reload` e salvar = deploy ([[neo-dev-topology-prod-tunnel]]).
Se a checagem entrar dura de uma vez, o desktop em execução perde os dois WS na
hora do save. Então:

- **Fase permissiva:** token ausente → aceita, loga `WARN [pair] device sem
  token`, e o device entra como `unverified`.
- **Canal de controle NUNCA é permissivo** — nasce exigindo token. Como ele é
  novo, não há cliente antigo pra quebrar.
- ✅ Chave virada em 2026-08-18, depois de confirmar que o único device vivo
  (o MacBook) estava pareado e mandando token nos três canais. Os outros 25
  registros eram duplicatas offline do mesmo Mac.

**Uma sutileza que só o teste contra a VPS pegou:** a checagem tem que rodar
**antes** de `websocket.accept()`. Aceitar e só então fechar faz o cliente ver
um 101 bem-sucedido seguido de um close — indistinguível de queda de rede, e
portanto candidato a retry infinito. Recusando antes, ele recebe 403 e sabe que
precisa parear. O teste inicial não pegava isso porque nunca lia do socket.

---

## 3. O canal de controle

Novo endpoint, **não** reuso do WS de notificações — reusar aquele significaria
um popup nativo a cada ajuste de volume (`websocket.rs` mostra notificação pra
toda mensagem que chega, filtrando só por foco de janela) e contaminar o parser
de `NeoNotification`.

```
/ws/devices/control/{device_id}?token=...      full-duplex

core → device   {"id":"c-7f3a","action":"volume_set","args":{"level":30}}
device → core   {"id":"c-7f3a","ok":true,"result":{"level":30,"muted":false}}
                {"id":"c-7f3a","ok":false,"error":"volume não suportado"}
```

`DeviceControlBroadcaster` (`core/notifications/device_control.py`), espelhando
o `DeviceNotificationsBroadcaster` que já existe:

- `dict[device_id, WebSocket]` + `dict[correlation_id, asyncio.Future]`
- `request(device_id, action, args, timeout=5.0)` → resolve a Future ou levanta
  `DeviceTimeout`
- device desconectou no meio → Future resolve com erro, nunca pendura

**Ações fire-and-forget:** `suspend`, `shutdown` e `restart` derrubam o WS antes
de qualquer resposta. Pra elas o contrato é outro: `ok` = a mensagem saiu **e** o
device desconectou dentro de N segundos. Esperar `result` nessas é garantir
timeout falso.

---

## 4. Superfície de tools

Duas, não uma nem oito. Uma tool por ação enche o catálogo e o classificador
dilui regra específica na genérica ([[neo-router-skill-bypass-persona]]); tudo
numa só coloca "desligar o PC" no mesmo nível de risco que "abaixar o volume".
A separação espelha `FILE_ACTUATION_TOOL_NAMES` vs `SHELL_ACTUATION_TOOL_NAMES`.

### `device_control` (server-side, `modules/devices/`)
```
device   string  nome do device ("PC da sala"). Omitido = device da conversa.
action   enum    volume_set | volume_up | volume_down | mute | unmute |
                 play | pause | play_pause | next | previous | lock |
                 get_status
value    integer só para volume_set (0-100) / volume_up|down (passo)
```

### `device_power` (server-side, gated)
```
device   string  obrigatório e explícito — nunca implícito
action   enum    suspend | shutdown | restart
```
- flag `neo_device_power_enabled` (default **off**)
- **aprovação no device alvo**, não no de origem: o core manda
  `confirm_required`, o desktop alvo mostra o diálogo nativo, e só então executa.
  Quem está na frente da máquina que vai desligar é quem decide.

`list_connected_devices` continua sendo a descoberta, mas passa a devolver
`display_name` e `capabilities`.

---

## 5. Resolução de nome (onde isso costuma quebrar)

O `device_id` é um UUID opaco. **O modelo vai inventar um** — é literalmente o
modo de falha de [[neo-file-id-name-resolution]] (o classificador punha o *nome*
no campo `file_id`) e de [[neo-ha-false-success-invented-entity]] (entity_id
inventado → "desliguei" sem desligar). Então:

1. A tool recebe **nome**, nunca id. O id sai do catálogo, do lado do core.
2. Casamento: `display_name` exato (case/acento-insensitive) → prefixo →
   `model`/`device_type`. Sempre escopado ao `user_id` do turno (o seam do
   `DevicesContextExecutor` já carrega isso — reusar, não reinventar).
3. **Ambíguo → pergunta**, listando os candidatos. Não escolhe o primeiro.
4. **Offline → falha dura**, com a lista do que está online. Nunca reportar
   sucesso de algo que não teve confirmação (`ok:true` do canal, não "mandei").
5. Sem `device` na chamada → o device da própria conversa.

---

## 6. Execução no desktop (o que cada ação custa)

| Ação | macOS | Windows | Linux |
|---|---|---|---|
| volume/mute | CoreAudio `VirtualMasterVolume` (FFI **já existe** em `services/audio_route.rs`) | `IAudioEndpointVolume` (COM) | `pactl set-sink-volume` |
| play/pause/next | tecla de mídia via IOKit `NX_KEYTYPE_*` | `keybd_event VK_MEDIA_*` | `playerctl` (MPRIS) |
| lock | `pmset displaysleepnow` | `LockWorkStation` | `loginctl lock-session` |
| suspend | `pmset sleepnow` | `SetSuspendState` | `systemctl suspend` |
| shutdown | System Events | `shutdown /s /t 0` | `systemctl poweroff` |

Dois pontos que decidem qualidade:

- **Volume: usar o CoreAudio que já está lá**, não `osascript set volume`. O
  FFI de `audio_route.rs` já enumera devices e resolve rota; `set_volume` é o
  irmão natural de `resolve()`, e o `osascript` não sabe em qual saída mexer.
- **Play/pause tem dois mundos.** Player do SO ≠ aba do navegador. A aba já tem
  trilho pronto — `ExtBridge` + `media_command` (`ai_agent.rs:368`) — mas o
  `run_media_command` atual só passa `action/target/videoId`, montado pra
  *tocar*. Decisão: **`play_pause` é sempre media key do SO** (que a aba
  também obedece, via MPRIS/AVRoute), e o `ExtBridge` fica só pro que exige
  alvo específico. Um roteador "quem está tocando?" é complexidade sem retorno
  na F1.

---

## 7. Fases

| Fase | Entrega | Status |
|---|---|---|
| **F0 — Esqueleto** | `/ws/devices/control/{id}` + `DeviceControlBroadcaster` | ✅ round-trip validado contra a VPS |
| **F1 — Dono e nome** | migração + `POST /devices/pair` + código no painel + token no desktop | ✅ pareamento real; replay do código dá 403, token falso dá 401 |
| **F2 — device_control** | tool + resolução por nome + volume/mute/get_status | ✅ **"abaixa o volume do PC da Sala pra 20" → o aparelho recebeu `volume_set {level:20}`** |
| **F3 — Mídia + lock** | play/pause/next/previous, lock | ✅ implementado (macOS via player, Linux via playerctl) |
| **F4 — device_power** | suspend/shutdown/restart + flag + confirmação no alvo | ✅ implementado; flag `neo_device_power_enabled` **OFF** por padrão |
| **F5 — Auth dura** | `neo_device_auth_required` nos canais antigos + gRPC | ✅ **LIGADA em prod**: sem token e token errado dão 403 nos 3 canais; o MacBook reconectou com token |
| **Remover device** | `DELETE /devices/{id}` (painel) e `/devices/mine/{id}` (token) + botão nas duas UIs | ✅ apagar invalida o token e derruba o canal aberto |

### O que foi construído, por arquivo

**neo-core**
- `db/models/connected_device.py` — `display_name`, `pairing_token_hash`, `paired_at`, `capabilities`
- `alembic/versions/t3u4v5w6x7y8_add_device_pairing_columns.py` — aplicada em prod
- `modules/devices/pairing.py` — códigos de 6 dígitos (TTL 5 min, uso único) e tokens
- `core/notifications/device_control.py` — `DeviceControlBroadcaster` (correlação, timeout, confirmação-por-desconexão)
- `modules/devices/control.py` — resolução por nome, validação, despacho
- `modules/devices/tools.py` — `device_control` e `device_power`
- `api/routes/devices.py` — `/ws/devices/control`, `/devices/pair`, `/devices/pairing-code`, `/devices/mine`, `DELETE /devices/{id}`, `_device_ws_authorized`
- `grpc_server.py` — `_grpc_device_authorized` (token na metadata `x-device-token` da CHAMADA, nunca no corpo: o `metadata` do user_input alimenta o prompt)
- `tests/test_device_control.py` — 28 testes; suíte total 1416 passando

**neo-desktop-tauri**
- `src-tauri/src/services/device_control.rs` — atuadores (CoreAudio no macOS, pactl no Linux)
- `src-tauri/src/websocket.rs` — cliente do canal + diálogo de confirmação de energia
- `src-tauri/src/lib.rs` — `device-token` em disco (0600)
- `src/components/DevicesTab.tsx` — cartão de pareamento + renomear no lugar + botão Remover
- token enviado também nos canais antigos e na metadata do gRPC
- `list_connected_devices` **deixou de ser stub** (devolvia `vec![]`; a aba nunca mostrou nada)

**neo-panel**
- `src/ConnectedDevicesPanel.tsx` — gerador de código com contagem regressiva + botão Remover

---

## 8. Riscos e decisões em aberto

1. ~~Como autorizar o primeiro pareamento~~ — **resolvido**: código de 6 dígitos
   gerado no painel, TTL 5 min, uso único, e gerar outro invalida o anterior.
2. **A `:8000` está aberta na internet?** `docker-compose.yml:26` publica em
   `0.0.0.0`. **Ainda não verificado** — e é o que decide se dá para ligar
   `neo_device_power_enabled`. Enquanto não conferir o firewall, `device_power`
   fica OFF: os outros canais seguem sem auth, então uma :8000 aberta somada a
   shutdown ligado seria "qualquer um desliga a máquina".
3. **`NEO_USER_ID` com fallback hardcoded** vira dívida no dia em que houver um
   segundo usuário. A F1 não resolve isso — só torna o `user_id` *provado* em
   vez de *declarado*. Login real fica pra depois.
4. **Windows ficou pra trás, e isso está declarado.** `capabilities()` no
   desktop devolve `volume: false` e `media: false` no Windows — a COM
   `IAudioEndpointVolume` não foi ligada. O cérebro não oferece o que o aparelho
   não sabe fazer, em vez de prometer e falhar depois.
5. ~~`get_status` é o que faz o resto ser verificável~~ — **feito**: toda ação de
   volume devolve o estado medido depois de agir, então o Neo reporta o valor
   real (se o hardware arredondar 30 → 31, é 31 que ele diz).
6. **Suspend mata o canal.** Contrato de fire-and-forget explícito (§3), senão
   toda suspensão bem-sucedida vira timeout reportado como falha.

---

## 10. Como usar

1. **neo-panel → Dispositivos** → "Gerar código" (o `user_id` já vem preenchido
   com o dono da maioria dos aparelhos; confira antes).
2. **neo-desktop → aba Dispositivos** → digite o código e, se quiser, o nome
   ("PC da sala"). O canal de controle sobe na hora, sem reiniciar o app.
3. Clique no nome de qualquer aparelho da lista para renomear — é o nome pelo
   qual você vai pedir as coisas ao Neo.
4. Peça: *"abaixa o volume do PC da sala pra 20"*, *"muta o notebook"*,
   *"pausa a música"*.

Para ligar suspend/shutdown: `NEO_DEVICE_POWER_ENABLED=true` no `.env` da RAIZ
(o container lê aquele, não o de `neo-core/`) — **depois** de conferir o
firewall da `:8000`.

---

## 11. Remover um device

`DELETE /api/v1/devices/{id}` (painel, JWT) ou `DELETE /api/v1/devices/mine/{id}?token=`
(o próprio device, mesma conta). Botão "Remover" nas duas UIs, com confirmação
em dois toques.

Apagar a linha **invalida o token junto** — o banco só guardava o hash — e o
canal de controle aberto é derrubado na hora (`DeviceControlBroadcaster.kick`).
Sem esse kick, um device apagado seguiria aceitando comandos pelo socket que
abriu enquanto ainda existia.

A ordem importa: **apaga primeiro, derruba depois**. O contrário deixaria uma
janela em que o device reconecta (o token ainda valeria) antes do DELETE.

Remover o próprio aparelho equivale a **desparear**: o desktop apaga o
`device-token` do disco e a aba volta a mostrar o cartão de pareamento.
