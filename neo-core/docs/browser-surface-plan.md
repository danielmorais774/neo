# Plano: Superfície de Navegador como dispositivo

**Data:** 2026-09-26
**Status:** **F0, F1, F2, F4 e F5 IMPLEMENTADAS**, incluindo o **cliente**: a
`neo-music-ext` v1.4.0 pareia com consentimento nativo e abre o canal do core.
F3 tem o MECANISMO pronto (a escada) e o CONTEÚDO (`DERIVADAS`) vazio de propósito
— a superfície declara só `tabs` e recusa ação de navegação com mensagem própria.
Estado detalhado no §14; **falta validar ponta a ponta** (§14.4).
**Objetivo:** o navegador do usuário deixa de ser um apêndice do desktop e passa a
ser um **dispositivo registrado** — pareado, autenticado, endereçável por nome e
alcançável a partir de qualquer canal (desktop, painel, celular, agenda), sem
exigir que o `neo-desktop-tauri` esteja aberto.
**Depende de:** registro de devices e pareamento (`connected_device`,
`modules/devices/pairing.py`), canal de controle (`/ws/devices/control`),
`core/browser_local/driver.py`, `api-auth-plan.md` (o portão que já existe).
**Documentos irmãos:** `browser-local-plan.md` (o caminho atual, via host `obu`),
`device-control-plan.md` §2 (de onde vem a credencial), `api-auth-plan.md` §11
(a ponte da extensão de música e por que ela tinha que ganhar credencial),
`release-request-plan.md` (título de aba é conteúdo externo).

---

## 0. TL;DR das decisões

| Tema | Decisão |
|---|---|
| O que a extensão é | **Um device**, linha em `connected_device` com `device_type` próprio — não uma tabela nova, não um caso especial |
| Como ela se liga à máquina | Coluna `host_device_id`: "esta superfície roda na máquina daquele desktop". É o vínculo simples que dá contexto |
| Quem tem autoridade para criar o vínculo | **O desktop**, com o token DELE. Ele só pode dizer "uma extensão na MINHA máquina, do MEU usuário" — nada além |
| Credencial da extensão | **Token de pareamento**, o mesmo mecanismo de todo device. Zero esquema novo |
| Canal | `/ws/devices/control/{device_id}` — que já exige token, já confere que o `device_id` é do token, e já carrega `browser.*` |
| A migração é… | **Uma troca de RESOLUÇÃO, não de transporte.** O `act` do driver já é `broadcaster.request(device_id, …)`: muda quem é o `device_id`, e mais nada |
| Endereçamento | Por **nome** ("navegador do macbook"), nunca por id — o modelo inventa id. Ambiguidade **pergunta**, não chuta |
| Herança do canal | Pedido vindo do desktop herda a superfície da própria máquina, sem nome nenhum (é o `current_device_id` que o `resolve_device` já usa) |
| O que isso conserta hoje | Pedido pelo painel/celular **degrada em silêncio** para o navegador do servidor, que loja brasileira bloqueia (`executor.py:687`) |
| Escopo do token | Superfície de navegador **não** recebe `power`/`volume_*`. O core confere `capabilities` antes de rotear |
| A superfície direta substitui o desktop? | **NÃO** (§12). Ela é a via de menor capacidade e maior alcance; `browser.downloads` casado com o disco e o headless são do desktop e não têm como sair de lá |
| `capabilities` é… | **contrato, não enfeite.** É ele que faz "o que o Neo oferece" mudar com a superfície resolvida |
| Quem é o dono da lógica | **O core** (§13). A superfície declara primitivas no `hello`; cada ação tem escada **nativa → derivada → ausente**. Com o desktop na frente, usa as funções dele |
| O modelo pode escrever JS? | **Nunca.** `eval`/`cdp` são primitivas de transporte; o JavaScript vem sempre da biblioteca do core. Teste de varredura impede o vazamento |

---

## 1. Como é hoje, medido

### 1.1 A extensão nunca fala com o neo-core

Os dois caminhos passam pelo desktop, e por **transportes diferentes**:

```
                ws://127.0.0.1:17872 (ponte; token desde 2026-09-26)
neo-music-ext ◄──────────────────────────────────► neo-desktop-tauri
                                                          │ túnel SSH (io.coress)
  SUBIDA                                                  ▼
  • {"type":"tabs"}      → memória da ponte ──┐        neo-core (VPS)
  • {"type":"media_event"} → HTTP na hora ────┼──▶ POST /api/v1/eventos/midia
                                              │
                    ai_agent.rs:284 ──────────┘
                    metadata["media_context"]["browser_tabs"]
                    (SÓ quando há turno)

  DESCIDA
  • {"type":"command"} ◄── client tool `media_command` do turno (ai_agent.rs:1141)
  • {"type":"watch"}   ◄── resposta do /eventos/midia (ext_bridge.rs:669)
```

E o navegador local, onde **quem inicia é o core**:

```
neo-core ──/ws/devices/control/{desktop}──▶ websocket.rs:311 ──▶ browser_local.rs
        ──socket Unix JSON-RPC──▶ obu (host de terceiro, iniciado PELO NAVEGADOR)
        ──native messaging──▶ extensão neo-browser-local ──CDP──▶ a aba
```

### 1.2 O achado que justifica o plano

`core/jobs/executor.py:687`:

```python
if not self.device_id:
    # Sem desktop (pedido veio pelo neo-panel, por exemplo) o navegador
    # local não existe — mas DESISTIR aqui era pior que o problema…
    logger.info("[browser-local] sem device — caindo para o navegador do servidor")
```

Ou seja: **pedir pelo celular já não funciona**, e não falha — **degrada**. Cai no
navegador do servidor, que é IP de datacenter francês e que a web brasileira
bloqueia (`neo-browser-servidor-bloqueado-por-origem`). O usuário recebe "o site
recusou a origem" sem saber que o navegador dele estava aberto do outro lado.

Não é uma feature nova que estamos inventando: é um caminho que hoje morre em
silêncio.

### 1.3 A costura que torna isso barato

`core/jobs/runner_browser_local.py:257`:

```python
async def act(action: str, args: dict, timeout: float) -> dict:
    payload = await broadcaster.request(device_id, action, args, timeout=timeout)
```

O driver inteiro (2900 linhas, vocabulário fechado `browser.*`, timeouts por ação,
diário, preenchimento escopado) fala com **um `device_id` no canal de controle**.
Ele não sabe e não precisa saber que do outro lado tem um desktop repassando para
um socket Unix. **Apontar para a extensão é trocar o `device_id` do spec.**

---

## 2. O conceito: superfície de navegador

Uma **superfície** é um navegador concreto, num perfil concreto, com a extensão do
Neo ativa. "Edge do macbook" é uma superfície; "Chrome do macbook" é outra. Cada
superfície:

- é uma **linha em `connected_device`** (não uma tabela nova);
- tem **dono** (`user_id`, vindo do token — não do que o cliente declara);
- tem **hospedeiro** (`host_device_id`): a máquina onde ela roda;
- tem **nome humano** (`display_name`): é por ele que você pede;
- declara o que sabe fazer (`capabilities`).

**Por que reusar `connected_device` e não criar tabela.** O que já existe de graça:
pareamento de uso único, `_device_from_token` (a única definição de "de quem é este
token"), o canal de controle já autenticado, heartbeat/online, `DELETE /devices/{id}`
para revogar, a lista do painel, e o `resolve_device` que já resolve por nome com
recusa em ambiguidade. Uma tabela paralela duplicaria os seis.

---

## 3. Modelo de dados

Uma coluna nova em `connected_device`:

```
host_device_id  varchar(256)  null   -- device_id da MÁQUINA onde esta superfície roda
```

E convenções:

| campo | superfície de navegador |
|---|---|
| `device_type` | `neo-browser-ext` |
| `device_id` | UUID gerado pela extensão, guardado no `chrome.storage.local` (como o desktop guarda em `device-id`) |
| `hardware_id` | **vazio** — o navegador não tem acesso a isso. O vínculo é o `host_device_id`, não o hardware |
| `display_name` | "Edge do macbook", "Chrome do PC da sala" |
| `capabilities` | `{"browser": true, "cdp": true, "engine": "edge", "tabs": true}` |
| `host_device_id` | o `device_id` do desktop que emitiu o código |

**Gotcha do `hardware_id` vazio:** `get_device_by_hardware_id` (`async_client.py:1799`)
casa por igualdade e devolve `.first()`. Com várias superfícies de `hardware_id=''`
ela devolveria uma qualquer. A rota que usa isso é `GET /devices/resolve`, que é do
desktop; ela precisa **ignorar linhas com `hardware_id` vazio** (ou filtrar por
`device_type`), senão um dia o desktop "se reconhece" como uma extensão.

---

## 4. Pareamento vinculado (onde o vínculo nasce)

O problema: a extensão não pode se declarar "do macbook" — se pudesse, o vínculo
não valeria nada. Quem sabe qual máquina é, e pode provar, é o desktop.

**Rota nova:** `POST /api/v1/devices/extension-code`, autenticada pelo **token do
aparelho** (não pelo JWT do painel). Ela devolve um código de uso único, TTL curto,
estampado com `(user_id, host_device_id = device_id de quem chamou)`.

```
desktop (token dele) ──▶ POST /devices/extension-code
                         ◄── {code, expires_in_s}
extensão ──▶ POST /devices/pair {code, device_id, device_type:"neo-browser-ext", …}
             ◄── {device_token, device_id, user_id}   ← já nasce com host_device_id
```

**Por que o desktop e não o painel.** A autoridade fica no mínimo necessário: o
desktop só consegue dizer "uma extensão na minha máquina, do meu usuário". O código
do painel (`/devices/pairing-code`) pode ligar qualquer aparelho a qualquer conta —
poder que não faz falta aqui. O `consume_pairing_code` atual devolve só `user_id`;
passa a devolver `(user_id, host_device_id)`.

**A UX de um clique** (a ideia do OAuth) encaixa exatamente aqui, e agora com
propósito além do conforto: o desktop abre um **diálogo nativo** ("Autorizar o Edge
como navegador do Neo?"), e o que ele emite é um **token de device** — útil nos dois
mundos, hoje e depois. O consentimento tem que ser nativo: um endpoint HTTP local
que emite token é alcançável por qualquer página, e se a aprovação for uma página
web o fluxo fica mais fraco que o paste.

---

## 5. Resolução do alvo (as suas duas histórias)

Uma função, `resolve_browser_surface(db, user_id, hint, turn_device_id)`, com
precedência explícita:

| # | Situação | Resolve para |
|---|---|---|
| 1 | `hint` casa o nome de uma **superfície** ("Edge do macbook") | ela |
| 2 | `hint` casa o nome de um **hospedeiro** ("macbook", "PC da sala") | as superfícies com `host_device_id` daquela máquina |
| 3 | sem `hint`, **turno veio de um desktop** | a superfície hospedada naquele `turn_device_id` ← **história 1** |
| 4 | sem `hint`, existe **exatamente uma** superfície online | ela |
| 5 | qualquer empate | **pergunta**, listando os nomes |

**História 1** ("peço dentro do desktop"): o turno já carrega o `device_id` do
desktop; a regra 3 acha a superfície da mesma máquina sem você dizer nada. É a
"reference da conexão" que você descreveu, e é o mesmo mecanismo que o
`resolve_device` já usa com `current_device_id`.

**História 2** ("peço pelo celular, usando o navegador do macbook"): não há
`turn_device_id`; você diz o nome da máquina e a regra 2 acha a superfície dela. Se
houver Edge **e** Chrome no macbook, a regra 5 pergunta — não chuta. Isso é regra
dura, não detalhe: chutar aqui abre a aba errada na tela dele.

**Superfície offline tem mensagem própria**, nunca "não existe": "o navegador do
macbook não está com o canal aberto — abra o navegador com a extensão do Neo".
Confundir "não pareado" com "fechado agora" custa uma hora de caça ao erro.

---

## 6. O canal, e por que a mudança é pequena

O `broadcaster.request(device_id, …)` não muda. O que muda:

1. `spec["device_id"]` do job passa a ser o **device_id da superfície**, não o do
   desktop. Uma linha no `_browser_local_task`.
2. O portão de `executor.py:687` deixa de ser "tem desktop neste canal?" e passa a
   ser "consigo resolver uma superfície?". É isso que faz a história 2 funcionar e
   **para a degradação silenciosa** para o navegador do servidor.
3. A extensão passa a executar o vocabulário `browser.*` que hoje o `obu` executa.

O caminho antigo (core → desktop → socket Unix → `obu` → extensão) **continua
valendo** enquanto o direto não estiver validado: são dois `device_id` diferentes
para o mesmo driver, e a escolha é da resolução. É essa propriedade que permite
migrar sem parar nada.

---

## 7. O snapshot de abas (um dos trabalhos de verdade — ver §12)

Hoje `browser_tabs` pega carona no turno do desktop (`ai_agent.rs:284`). Sem
desktop, não existe — e é justamente o que a história 2 precisa.

**Proposta:** a superfície empurra o estado pelo próprio canal de controle
(`{"type":"tabs", …}` não solicitado), e o core guarda o último conhecido **por
superfície**, em memória, expirando pelo heartbeat. Então qualquer turno, de
qualquer canal, monta `media_context` resolvendo as superfícies do usuário.

Isso é melhor que hoje em dois sentidos: passa a existir para o painel/celular, e
deixa de depender de haver turno para o core saber o que está aberto.

**E é aqui que entra a procedência:** título e URL de aba são **conteúdo externo**
(`api-auth-plan.md` §11). Chegando direto ao core, eles têm que ser rotulados como
tal para o portão de liberação — hoje entram no prompt como metadata "do cliente",
que é uma promessa que a aba não pode cumprir.

---

## 8. Auth e escopo

O que **não** precisa ser escrito: autenticação de canal (o `/ws/devices/control`
exige token sem fase permissiva e confere que o `device_id` da URL é o do token) e
resolução de dono (`_device_from_token`).

O que precisa:

- **Capability antes de rotear.** O core recusa mandar `power`/`volume_*`/`lock`
  para uma superfície de navegador, e `browser.*` para um desktop sem extensão. Hoje
  o canal entrega o que o core mandar; a checagem é barata e evita o erro futuro.
- **Revogação por superfície.** `DELETE /devices/{id}` já derruba o canal e mata o
  token (`kick()` depois de apagar a linha, nessa ordem). Tirar o Chrome não pode
  tirar o Edge — são linhas diferentes, então isso sai de graça.
- **Honestidade sobre onde o token mora:** `chrome.storage.local` **não é keychain**
  — quem tem o diretório do perfil lê o token. Mitigação real: a superfície só faz
  `browser.*`, é revogável sozinha, e **não** herda nada do desktop (linha
  separada). Não prometer mais do que isso.
- **Sem `Origin` para checar.** A defesa da ponte local (§11 do `api-auth-plan`) era
  `Origin` + token; aqui é WebSocket de saída, autenticado pelo token — o vetor "uma
  página se passa pela extensão" **desaparece junto com o listener local**. É um
  ganho de segurança do plano, não um custo.

---

## 9. Fases

**F0 — Modelo e vínculo.** Coluna `host_device_id`; `device_type` novo;
`POST /devices/extension-code` (token de aparelho); `consume_pairing_code` devolvendo
o vínculo; extensão pareia e **aparece na lista de dispositivos com o nome**.
Nenhuma mudança no caminho de execução — F0 é só identidade.

**F1 — Canal direto + `hello`.** A extensão abre
`/ws/devices/control/{device_id}?token=`, **declara suas primitivas** (§13.2) e
responde o vocabulário `browser.*`. O caminho via desktop/`obu` segue vivo; quem
decide é a resolução.

**F2 — Resolução.** `resolve_browser_surface` com as 5 regras; a tool recebe NOME;
o portão do `executor.py:687` passa a resolver superfície em vez de exigir desktop
no canal. **É aqui que as suas duas histórias passam a funcionar.**

**F3 — Lógica no core + snapshot.** (Ampliada pelo §13.) Biblioteca de payloads
`js_*` no core, `Surface` com a escada nativa→derivada→ausente, e o teste que impede
`eval` de aparecer no catálogo do cérebro. Junto: a superfície empurra `tabs`,
`media_context` passa a ser montado pelo core, o carona no turno do desktop vira
fallback, e os títulos ganham rótulo de procedência.

**F4 — Escopo e revogação.** Checagem de `capabilities` antes de rotear; tela de
dispositivos mostrando superfície aninhada no hospedeiro; um clique para revogar.

**F5 — Declarar as duas vias e escolher a melhor.** (Esta fase mudou depois da
análise de regressão do §12: **não** se pode aposentar a via do desktop.) A
superfície direta é o caminho de MENOR capacidade e MAIOR alcance; a via desktop
continua sendo a completa. A resolução prefere a completa quando as duas estão
disponíveis, e o core só oferece ao cérebro as ações que a superfície resolvida
declara saber fazer.

---

## 10. Gotchas que vão morder

1. **`NEO_DEVICE_AUTH_REQUIRED=true` já está ligado.** A extensão precisa parear
   **antes** de conectar, senão nem registra — a lição do F5 do controle de devices.
2. **Recusa antes do `accept()`.** O canal já faz certo. Se alguém mexer ali,
   aceitar-e-fechar vira 101+close, que o cliente lê como queda de rede e responde
   com retry infinito.
3. **Service worker MV3 é efêmero.** A conexão morre no reciclo (o keepalive de
   0,4 min da `neo-music-ext` existe por isso). O core vai ver a superfície
   piscando entre online/offline — o `is_online` precisa tolerar isso, senão a
   história 2 responde "nenhum navegador aberto" enquanto o worker só reiniciou.
4. **Duas superfícies na mesma máquina é o caso normal**, não a exceção (Edge +
   Chrome). Nome desde o dia 1, e ambiguidade que pergunta.
5. **"Direto para o túnel" troca uma dependência por outra.** A extensão vai falar
   `ws://127.0.0.1:8000/…`, que é o túnel na máquina dele. O desktop pode estar
   fechado; o **túnel**, não. O destravamento real é uma entrada pública
   autenticada (o molde existe: `:8091` com nginx + `X-Notify-Token`), e aí vira
   `wss://`. Enquanto isso, o ganho é "o desktop pode estar fechado" — que já é o
   que você pediu, mas não é "nada local precisa estar rodando".
6. **Não quebrar a música no meio.** O `media_command` de hoje é client tool do
   turno, executado pelo Rust. Migrar a música para `browser.*` na superfície é
   outro passo, depois de F3 — senão a fila para de andar durante a transição.
7. **`hardware_id` vazio** (§3): `GET /devices/resolve` precisa excluir superfícies,
   senão o desktop pode "se achar" numa linha de extensão.

---

## 11. O que este plano NÃO resolve

- **Não torna o core alcançável de fora.** Continua túnel (ou a entrada pública
  autenticada, que é trabalho próprio).
- **Não unifica as duas extensões.** `neo-music-ext` (abas/play) e
  `neo-browser-local` (CDP) continuam duas; o modelo só passa a acomodar as duas
  como superfícies. Unificar é decisão separada.
- **Não remove o host `obu`.** Quem fala CDP com a aba continua sendo ele, dentro da
  `neo-browser-local`. O que sai do caminho é o **desktop** como intermediário.
- **Não é multiusuário.** Uma conta, várias superfícies.

---

## 12. Análise de regressão: o que se perde, e o que não pode se perder

Feita em 2026-09-26, a pedido: *"a ideia aqui é melhorar o que já existe"*. A
conclusão curta é que **o plano, como estava escrito no §9, perderia features** — e
o motivo é um achado que muda o desenho.

### 12.1 O achado: a extensão é um cano, a inteligência está no desktop

`services/browser_local.rs` não é um repasse de CDP. São ~2.500 linhas que fazem o
trabalho **na máquina dele**:

| capacidade | onde mora hoje | sobrevive numa superfície DIRETA? |
|---|---|---|
| `js_snapshot` — a percepção numerada da página (é o que o modelo "vê") | desktop gera o JS | **sim**, movendo as strings para o core |
| `js_click`, `click_text`, `dblclick`, `right_click`, `fill`, `select`, `scroll`, `extract` | desktop gera o JS | **sim**, mesmo jeito |
| `js_banner` + botão PARE | desktop gera; o content script é da extensão | **sim** |
| `navigate`, `press_key`, `wait_for` | desktop orquestra CDP | **sim**, no core |
| `draw_marks` / `crop_jpeg` — os números desenhados no print, que a visão lê | desktop (crate `image` + renderizador de dígitos escrito à mão) | **reescrever** (PIL no core, ou canvas na extensão) |
| `browser.downloads` **casado com o disco** (`dirs::download_dir()`, `.crdownload` = ainda escrevendo) | desktop lê o sistema de arquivos | **NÃO. Impossível** — extensão não lê disco, e o core está na VPS |
| **headless local** (`Command::new(bin).arg("--headless=new")`) | desktop **inicia o navegador** | **NÃO. Impossível** numa extensão |
| continuidade de aba (`tab_ref`) | core, chaveada por `device_id` | **sim**, re-chaveando |

Ou seja: a extensão hoje é um **cano de CDP**. "Apontar a extensão para o túnel" não
move o braço do navegador — move só o cano, e deixa o braço atrás.

### 12.2 Regressões que o plano causaria se fosse implementado como estava

1. **Controle de volume passa a PERGUNTAR onde hoje funciona.** `_load_devices`
   (`modules/devices/control.py:100`) carrega **todos** os devices do usuário, sem
   filtro de tipo. Com uma superfície online ao lado do desktop, a regra "sem nome,
   exatamente um online" deixa de valer e o `resolve_device` responde *"Tem mais de
   um dispositivo disponível — diga qual"*. Regressão silenciosa numa feature que
   funciona hoje. **Correção obrigatória:** filtrar candidatos por capability —
   quem responde `volume`/`power` não é quem responde `browser.*`.
2. **`device_power` passaria a listar navegador como candidato.** Falharia na
   execução, mas o menu de candidatos já estaria errado — e é `suspend`/`shutdown`,
   a ação irreversível.
3. **A retomada da liberação quebraria.** `browser_session` é chaveada por
   `device_id` (`runner_browser_local.py:145`), e ela carrega `pending` — a chave
   que autoriza *aquele* clique depois da sua aprovação (`release-request-plan` F3c).
   Trocar o `device_id` sem re-chavear não perde só a continuidade: perde uma
   **autorização que já foi dada e não pode ser consumida**.
4. **`browser.downloads` e headless não existem na via direta** (§12.1). A F5
   original ("aposentar o indireto") estava errada e foi corrigida.
5. **A marcação numerada do print é o olho do agente.** Sem `draw_marks`, a visão
   por passo perde os números e o modelo volta a chutar coordenada — o modo de falha
   que essa função existe para matar.
6. **A lista de dispositivos passa a mentir.** `DevicesTab.tsx` rotula tudo que não
   é `neo-mobile` como "Neo Desktop". Cosmético, mas é a tela onde você revoga.
7. **`get_device_by_hardware_id` com `hardware_id` vazio** (já registrado no §3) e o
   **piscar do service worker MV3** (§10.3).

### 12.3 O que muda no desenho

O erro era supor que a superfície direta **substitui** o desktop. Ela não
substitui: o desktop **é** o provedor mais capaz, e continua sendo.

- Superfície tem **provedor**: `desktop` (via `obu` → extensão, mais o headless) ou
  `extension` (direta). Duas linhas, duas capabilities.
- `capabilities` deixa de ser enfeite e passa a ser **contrato**: o core só oferece
  ao cérebro as ações que a superfície resolvida sabe fazer, e recusa as outras com
  mensagem própria ("baixar arquivo precisa do Neo Desktop aberto nessa máquina"),
  nunca com falha genérica.
- A resolução **prefere a via completa** quando as duas estão disponíveis. A direta
  é o que faz existir o caminho que hoje não existe (pelo celular, desktop fechado).

### 12.4 O caso que motivou tudo continua coberto

"Peço pelo celular para o Neo abrir uma loja no navegador do macbook" usa
`navigate` + `snapshot` + `click` + `fill` + `screenshot` — todos migráveis (§12.1).
Download e headless, que **não** migram, são justamente os casos em que você está na
máquina, com o desktop aberto. A conta fecha.

E o motivo de existir do navegador local segue intacto: é o IP residencial que a web
brasileira aceita (`neo-browser-servidor-bloqueado-por-origem`). A via direta
preserva isso — o navegador continua sendo o dele.

### 12.5 Trabalho que a análise adicionou às fases

- **F0** ganha: filtro de capability no `_load_devices`/`resolve_device` **antes** de
  a primeira superfície existir. Se entrar fora de ordem, o volume quebra no dia em
  que a extensão parear.
- **F1** ganha: re-chavear `browser_session` pela superfície e garantir que a
  retomada resolve para a **mesma** superfície.
- **F2** ganha: recusa por capability com mensagem que ensina o caminho.
- **F3** ganha: mover a biblioteca de `js_*` para o core (são strings — barato) e
  decidir onde fica `draw_marks` (PIL no core me parece melhor que canvas na
  extensão: é onde a visão já é consumida).
- **F4** ganha: `DevicesTab` mostrando superfície aninhada no hospedeiro, com rótulo
  próprio.

---

## 13. O core como dono da lógica, e a escada de capacidade

Revisão de 2026-09-26, depois do §12: em vez de "a superfície direta é pobre e
pronto", **o core passa a ser o dono da lógica** e negocia com o executor que
existir. Pedindo pelo desktop, usa o que o desktop sabe fazer; sem ele, faz do jeito
que der; e quando não dá de jeito nenhum, **diz o que falta** em vez de falhar torto.

### 13.1 A regra que não pode ser quebrada: o modelo nunca escreve JS

Mover a biblioteca de `js_*` para o core cria uma tentação: expor `browser.eval` e
deixar o cérebro montar o próprio JavaScript. **Não.**

- O vocabulário `browser.*` de alto nível continua sendo **o contrato com o modelo**
  — é ele que a tabela de risco (`core/authority/risk.py`) classifica e que o portão
  de liberação entende. Um `eval` cru seria uma escotilha que passa por fora de toda
  classificação.
- `eval`/`cdp` existem como **primitivas de transporte**, e o JavaScript vem
  **sempre** da biblioteca do core. É a mesma forma do preenchimento escopado
  (`release-request-plan` §7.2): o modelo escreve `«pks:cep»`, quem substitui é o
  driver. O poder cru fica com o executor, nunca com quem está sujeito a injeção.
- **Teste que sustenta:** varredura no catálogo de tools do cérebro que falha se
  `eval` ou `cdp` aparecerem — mesmo truque do `test_risk_cobre_o_registry` e do
  `test_route_coverage`.

### 13.2 Handshake de capacidade

Ao abrir o canal, a superfície declara o que tem:

```json
{"type": "hello",
 "surface": {"provider": "extension",     // ou "desktop"
             "engine": "edge",
             "primitives": {"eval": true, "cdp": true, "screenshot": true,
                            "marks": false, "tabs": true, "banner": true,
                            "downloads_api": true, "downloads_disk": false,
                            "headless": false}}}
```

Guardado em `capabilities` (a coluna já existe) e em memória por conexão. **Declarar
é obrigatório:** superfície que não declara é tratada como a mais pobre, nunca como
a mais capaz — errar para o lado de prometer menos é o único erro barato aqui.

### 13.3 A escada, por ação

Três degraus, sempre na mesma ordem: **nativa → derivada → ausente**.

| ação | nativa | derivada pelo core | ausente → "vira como pode" |
|---|---|---|---|
| `snapshot`, `click`, `click_text`, `fill`, `select`, `scroll`, `extract` | desktop (Rust gera o JS) | `eval` + JS da biblioteca do core | sem `eval` **nem** nativo a superfície não serve para nada — recusa no pareamento, não no meio da tarefa |
| `navigate`, `press_key`, `wait_for` | desktop | `cdp` + composição no core | idem |
| `screenshot` | desktop / extensão | `cdp Page.captureScreenshot` | segue pelo snapshot de TEXTO (que é o caminho principal) |
| **marcação numerada** do print | desktop (crate `image`) ou extensão (OffscreenCanvas) | core com Pillow — **ver 13.5** | **print sem marcação.** O próprio `draw_marks` já devolve `None` em qualquer tropeço, com o comentário *"print sem marcação ainda é útil, print nenhum não é"*, e a visão é opcional (`if not self.vision_model` → "não deu para triar (sem visão)"). A degradação já existe: só passa a ser escolhida por capacidade em vez de por acidente |
| `downloads` | desktop (`chrome.downloads` **casado com o disco**) | — | só `chrome.downloads`: sabe o que o navegador acha, não confirma o byte no disco. Responde o que sabe **e diz o que não sabe** |
| `headless` | desktop (inicia o navegador) | — | desce um degrau na escada que já existe (`neo-escada-de-busca-e-headless-local`): aba visível, ou navegador do servidor |

O que essa tabela compra: a **recusa deixa de ser genérica**. "Baixar arquivo precisa
do Neo Desktop aberto nessa máquina" é acionável; "o navegador recusou a ação" não é.

### 13.4 Onde a lógica mora depois da migração

- `core/browser_local/payloads.py` — os `js_*` como strings Python. É cópia quase
  literal do que está no Rust; o barato aqui é que **já é JavaScript**, só muda a
  linguagem que o guarda.
- `core/browser_local/surface.py` — o `Surface`: guarda as primitivas declaradas e
  resolve cada ação pela escada. O `act` continua sendo
  `broadcaster.request(device_id, …)`; a escada fica **acima** dele, então o driver
  não muda.
- O Rust **mantém** suas implementações nativas. Elas passam a ser o degrau 1, não a
  única via — e continuam sendo o caminho preferido quando o desktop está presente,
  que é exatamente o "se tiver o correto ele usa o correto".

### 13.5 Pillow: a armadilha, e por que não vamos cair nela

O core **não tem Pillow**, nem como dependência transitiva (medido). E
`neo-dev-topology-prod-tunnel` registra o modo de falha: **dependência nova no
`pyproject.toml` derruba produção no reload** — o entrypoint só instala no boot do
container, mas o Mutagen sincroniza o `.py` na hora e o `--reload` reimporta na hora,
então o `import` novo quebra o `api/main.py` antes de qualquer "deploy".

Decisão: **a marcação fica na superfície**, não no core. Rust hoje; `OffscreenCanvas`
na extensão depois (sem tocar o DOM, então sem piscar a tela dele). O core só assume
esse degrau se um dia a conta virar — e aí com a ordem certa
(`docker compose up -d --no-deps neo-core` **antes** de salvar o `.py` que importa).

### 13.6 Ordem dentro das fases

- **F1** ganha o `hello` (a superfície declara ao conectar) — barato e é pré-requisito
  de tudo em 13.3.
- **F3** passa a ser: biblioteca de payloads no core + `Surface` com a escada + o
  teste que impede `eval` de vazar para o cérebro. O snapshot de abas (§7) entra
  junto, porque é a mesma refatoração.
- **F4** ganha a recusa por capacidade **com texto que ensina**, e a oferta da outra
  superfície quando ela existe ("o Edge do macbook não baixa arquivo sem o Neo
  Desktop; o navegador do servidor consegue, mas a loja pode recusar a origem").
- Nada disso mexe no degrau 1: enquanto o desktop estiver na frente, o caminho é o
  mesmo de hoje, com as mesmas funções.

### 13.7 Como isso se prova

Para **cada** ação do vocabulário, dois testes: um com superfície capaz (usa a
nativa) e um com superfície pobre (degrada pela escada, e a mensagem diz o que
falta). Mais dois de fronteira: o que garante que `eval`/`cdp` não estão no catálogo
do cérebro, e o que garante que superfície sem declaração é tratada como pobre.

---

## 14. Estado da implementação (2026-09-26)

### O que entrou

| Fase | Estado | Onde |
|---|---|---|
| **F0** modelo e vínculo | ✅ | coluna `host_device_id` (migração `h1i2j3k4l5m7`, **aplicada em produção**); `BROWSER_SURFACE_TYPE`; `POST /devices/extension-code` (token de aparelho); `consume_pairing_code_full` devolvendo `(user_id, host_device_id)`; `pair_device` gravando o vínculo |
| **F0** (pré-requisito do §12.5) filtro de capacidade | ✅ | `_load_devices(action=…)` filtra candidatos **antes** da resolução; `resolve_device(action=…)`; guarda no `get_device_by_hardware_id` contra `hardware_id` vazio |
| **F1** lado do core | ✅ | `hello` e `tabs` tratados no canal (`/ws/devices/control`), `SurfaceRegistry` em memória, capability mesclada no banco, `forget` na queda |
| **F2** resolução | ✅ | `core/browser_local/surfaces.py`: `resolve_browser_surface` com as 5 regras; `_browser_local_task` resolve superfície em vez de exigir desktop no canal |
| **F3** mecanismo | ✅ | `dispatch()` com a escada nativa→derivada→ausente, `AcaoIndisponivel` com texto que ensina, `DEGRADA_SEM_DISCO`, e a escada **no caminho** do runner |
| **F3** conteúdo (payloads `js_*`) | ⬜ | **deliberadamente vazio** — ver abaixo |
| **F3** snapshot no core | ✅ | `abas_conhecidas()` + `_media_context_completo()`: o `media_context` do turno passa a somar o que as superfícies empurraram |
| **F4** rótulo/revogação | parcial | `DevicesTab` com rótulo próprio por tipo (era "Neo Desktop" para tudo que não fosse mobile — e é a tela onde se revoga). Falta o aninhamento visual no hospedeiro |
| **F5** duas vias | ✅ (por construção) | a resolução prefere a completa; a via desktop nunca sai do caminho |

Testes: `tests/browser_local/test_surface_resolution.py` (18), incluindo as duas
histórias do §5, "empate pergunta", e a escada;
`tests/browser_local/test_vocabulario_nao_vaza.py` (3), a trava do §13.1; e quatro
casos novos em `tests/test_device_control.py` para a regressão do §12.2. Suíte:
**3315 passando**.

### Por que `DERIVADAS` está vazio

O §13.4 prevê a biblioteca de `js_*` migrada do Rust para o core. Ela **não** foi
escrita, e a razão é a mesma que está no código: **nenhuma superfície direta existe
ainda**. Traduzir ~1.000 linhas de JavaScript sem cliente que as exercite produz
código não executado que afirma funcionar — e o §13.7 exige o par de testes (capaz
usa a nativa, pobre degrada) para cada ação, que sem superfície não se pode
escrever de verdade.

O que entrou no lugar é o **mecanismo**: `DERIVADAS` é um dicionário
`acao -> (primitiva, construtor)`, com teste provando que o degrau 2 funciona
(`test_derivada_usa_a_primitiva_declarada`) e que ele não é usado quando a
primitiva falta. Portar passa a ser *acrescentar uma entrada com seu par de
testes*, uma por vez, quando houver uma superfície para provar contra — e
`test_a_escada_nao_nasce_com_derivada_nao_testada` recusa entrada sem prova.

### 14.1 Os clientes (feitos) — `neo-music-ext` v1.4.0 e `neo-browser-local` v0.2.0

**Dois caminhos de pareamento, e a diferença é quem prova a máquina:**

| extensão | código vem de | vínculo | consentimento |
|---|---|---|---|
| `neo-music-ext` | o **desktop**, pela ponte (`pair_request`) | `host_device_id` automático | **diálogo nativo** do desktop |
| `neo-browser-local` | o **painel** (digitado no popup) | órfã — o nome carrega a máquina | o próprio painel, que exige login |

O segundo existe porque `neo-browser-local` **não está na ponte** (fala por native
messaging com o `obu`), e porque é o caminho de quem **não tem desktop** na máquina.
A contrapartida é honesta e está na UI: sem `host_device_id`, quem endereça é o
NOME — daí o campo pedir "Edge do macbook" em vez de só "Edge". A cascata de nomes
casa por trecho, então "macbook" ainda encontra.



A primeira superfície existe, e é de LEITURA: declara `tabs` e nada mais.

- **Pareamento de um clique, consentimento nativo.** O popup manda `pair_request`
  pela ponte; o desktop abre um **diálogo do sistema** (`confirmar_pareamento` em
  `ext_bridge.rs`) e, ao autorizar, chama `POST /devices/extension-code` com o
  token dele — o código já sai estampado com a máquina. A extensão troca o código
  em `/devices/pair` e guarda o token em `chrome.storage.local`.
- **Nenhum listener HTTP novo.** A objeção ao redirect estilo OAuth era justamente
  a superfície de ataque; a ponte já existe e já é autenticada, então o fluxo de
  consentimento não acrescenta porta nenhuma.
- **Canal próprio:** `ws://…/api/v1/ws/devices/control/{device_id}?token=`, com
  `hello` na abertura e o snapshot de abas empurrado no mesmo ciclo que já
  alimentava o desktop (uma varredura, duas pontas). O keepalive de 0,4 min cuida
  dos dois canais, porque o service worker MV3 recicla os dois igual.
- **4401 desapareia local.** Token que o core não reconhece mais (device removido)
  apaga o estado em vez de virar reconexão infinita.
- Ação de navegação chega e é respondida com `ok:false` + motivo. Rede de
  segurança: o core já recusa antes, pela declaração.

### 14.2 O resto do que estava pendente (feito)

- **`browser_session` re-chaveada pelo NAVEGADOR** (`chave_do_navegador`): a chave
  é a máquina (o hospedeiro da superfície, ou o próprio desktop), não o transporte.
  Era o item mais perigoso da lista — o `pending` que autoriza *aquele* clique
  depois da sua aprovação de liberação ficaria órfão na troca de via. Aba do
  headless (ref negativa) continua exigindo o mesmo `device_id`: aquele navegador é
  do desktop e não existe para a extensão.
- **Aninhamento visual** na aba Dispositivos: o navegador aparece dentro da máquina
  dele (`host_device_id` passou a sair na listagem), com remoção própria. É a tela
  onde se revoga, e um navegador solto ao lado do computador convida a revogar a
  linha errada.

### 14.3 O que segue de fora, e por quê

- **`DERIVADAS` vazio** (o porte dos `js_*`): ver a seção acima. Agora existe uma
  superfície para provar contra, mas ela declara `tabs` — para exercitar as
  derivadas seria preciso uma superfície com `eval`/`cdp`, que é a
  `neo-browser-local` (item seguinte).
- ~~**`neo-browser-local` como superfície direta**~~ — **PAREIA desde a v0.2.0**
  (2026-09-27), pelo caminho SEM desktop: código do painel digitado no popup
  (seção *Conta do Neo*). Ela declara só `tabs`, e mesmo isso **opt-in**
  (`<all_urls>` faz de "reportar abas" uma decisão dele, não um default). Ação de
  navegação **continua indo pelo desktop** — declarar `eval`/`cdp` acontece junto
  com portar as derivadas, nunca antes. É esse passo que ainda falta para o
  degrau 2 existir de verdade.
- **Entrada pública autenticada** para o core (hoje o canal é o túnel). Decisão
  dele: o túnel é intencional, no espírito de homelab.

### 14.4 Validação pendente (importante)

Nada do caminho novo foi exercitado contra um navegador de verdade: **não reiniciei
o `tauri dev` nem recarreguei a extensão** (deploy manual, `edge://extensions`), e
o túnel estava fora no fim. O que foi verificado: 3317 testes no core, 81 no Rust,
`tsc` limpo, migração aplicada em produção, e `POST /devices/extension-code`
respondendo 401 a token falso (medido de dentro da VPS).

O primeiro teste ponta a ponta é curto: reiniciar o desktop, recarregar a extensão,
clicar em *Parear este navegador com o Neo*, autorizar no diálogo — e a superfície
tem que aparecer aninhada na aba Dispositivos. Depois disso, "o que está tocando?"
pelo painel deve enxergar as abas sem o desktop no canal.

### Invariante que guiou a implementação

Cada mudança é **aditiva**: nada que funcionava hoje passa por caminho novo. Em
particular, quando a resolução de superfície falha por qualquer motivo e existe um
device no canal, o job vai **para esse device** — o mesmo comportamento de antes.
Cair no navegador do servidor por causa de uma falha de resolução seria trocar o
navegador dele pelo do datacenter, que é exatamente a regressão que este plano
existe para evitar.
