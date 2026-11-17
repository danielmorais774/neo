# Neo Music Bridge (extensão)

Extensão MV3 **enxuta** que liga o navegador ao **Neo Desktop** por um WebSocket
local (`ws://127.0.0.1:17872`). Ela só faz duas coisas:

1. **Reporta abas de música** abertas (YouTube / Spotify) para o Neo — com o
   estado do player, não só a URL (ver *O que a ponte reporta* abaixo).
2. **Executa o "play"** que o Neo manda: foca a aba do YouTube e navega para o
   vídeo (`watch?v=<id>`); se não houver aba, abre uma nova. Numa **fila**, a aba
   é sempre a mesma do começo ao fim (ver *A aba da fila é uma só*).

Sem loop de IA próprio, sem content scripts, sem painel. É só a ponte.

## O que a ponte reporta (por aba)

Chega ao neo-core como `media_context.browser_tabs`. **Nada é obrigatório**: aba
do Spotify, aba ainda carregando e aba onde a injeção falhou vêm só com o
básico, e o lado do Neo trata ausência como "não sei", nunca como "não".

| campo | de onde vem | pra que serve |
|---|---|---|
| `site`, `tabId`, `url`, `title` | `chrome.tabs` | identificar e reusar a aba |
| `playing` | player, com `audible` de piso | "está tocando" de verdade |
| `audible` | `tab.audible` | **o ícone de auto-falante da guia** |
| `muted` / `pageMuted` | `tab.mutedInfo` / `video.muted` | mudo pela guia × pelo player |
| `active`, `windowId`, `discarded`, `status` | `chrome.tabs` | foco e se dá pra sondar |
| `kind` | URL | `watch`, `home`, `feed`, `search`, `playlist`, `shorts`, `channel`, `music`, `embed` |
| `videoId`, `listId` | URL | vídeo e playlist/mix (`RD…` = rádio) |
| `videoUrl` | URL | **o link do vídeo pra citar/compartilhar**, sem `list=`/`t=`/`index=` |
| `hasMedia` | `<video>` com duração | **se existe algo pra controlar** |
| `paused`, `ended` | `<video>` | play × pause de verdade |
| `currentTime`, `duration`, `progress`, `live` | `<video>` | posição, duração, transmissão ao vivo |
| `volume`, `playbackRate` | `<video>` | volume e velocidade |
| `adShowing` | `.ad-showing` no `#movie_player` | o som do momento é comercial |
| `contentType` | ver abaixo | `music` \| `video` \| `unknown` |
| `contentTypeSource` | — | qual sinal decidiu (auditável) |
| `track`, `artist`, `album` | `navigator.mediaSession.metadata` | faixa já normalizada pelo player |
| `channel` | `videoDetails.author` (ou microformat/DOM) | **quem publicou** — não é o mesmo que `artist` |
| `probe`, `probeWorld` | — | `ok` \| `denied` \| `loading` \| `discarded` \| `skipped` \| `error` |

### Música ou vídeo comum?

O YouTube **sabe** — e a extensão lê, em ordem de confiança:

1. host `music.youtube.com`;
2. `ytInitialPlayerResponse.microformat.playerMicroformatRenderer.category` — a
   categoria que o próprio YouTube atribuiu ao vídeo ("Music"/"Música");
3. `<meta itemprop="genre">` (microdata da página);
4. o painel **Música** que aparece sob a descrição de conteúdo licenciado;
5. `list=RD…` — mix/rádio é a fila automática de música.

Os dois primeiros só existem no **mundo MAIN** da página, por isso a sonda é
injetada lá primeiro e cai para o ISOLATED se o navegador recusar — nesse caso
sobram os sinais 3–5 e `contentTypeSource` diz qual valeu.

### Link do vídeo e canal

`videoUrl` é montado a partir da URL da aba, **não** da sonda: uma aba descartada,
ainda carregando ou onde a injeção falhou continua tendo link (o que ela perde é o
canal). E é a URL *canônica* — a da aba carrega `list=`, `index=`, `t=` e, nos
Shorts, o formato `/shorts/<id>`, que não é o que se cola num chat.

`channel` sai do `videoDetails.author` (a resposta do próprio player, a única que
sobrevive à navegação por SPA), caindo para `ownerChannelName` do microformat e,
no mundo ISOLATED, para o microdata/DOM. É **diferente** de `artist`: em vídeo
comum o `mediaSession` não publica artista nenhum, e em música o artista pode não
ser o canal.

### Home sem vídeo

Uma aba em `youtube.com/` é uma aba do YouTube **sem nada pra tocar**. Ela vem
com `kind: "home"` e `hasMedia: false`, e o Neo para de mandar "pausa" para uma
página que não tem player. As prévias mudas do feed não contam: quem decide é a
**duração** do `<video>`, não a existência do elemento.

### Custo

A sonda só entra em aba do YouTube carregada (é onde há permissão de host), no
máximo 12 por relatório, priorizando as que têm som, estão em foco ou são página
de vídeo. Aba de Spotify vem só com o que `chrome.tabs` dá.

## O que a ponte EXECUTA (controle)

`pause`/`resume`/`mute`/`volume_up`/`volume_down`/`next`/`previous` são aplicados
na aba e **conferidos** antes de responder: a extensão relê o estado e só devolve
sucesso se ele mudou. Sem isso o Neo anunciava "Mutei" com o som intacto (era o
caso quando a ação pegava o primeiro `<video>` do DOM — que numa página do
YouTube pode ser a prévia muda; agora escolhe o mesmo que a sonda, o que tem
duração).

Quando o mundo MAIN está disponível, usa a **API do player** (`#movie_player`):
ela manda no som de verdade, sincroniza o slider da UI e funciona também no
**YouTube Music**, cuja barra de controle não tem os `.ytp-*-button`. Sem a API,
cai no `<video>` e nos botões (inclusive os do YouTube Music).

`mute` **alterna**: a resposta traz `era`/`virou` para o Neo dizer "Mutei" ou
"Desmutei" sem chutar.

## A vigia de fila (quando o Neo está tocando uma FILA)

Fora de uma fila, nada disto existe: um vídeo qualquer do usuário nunca é
pausado nem observado. Quando o Neo despacha uma faixa de uma fila, o desktop
manda `{"type":"watch", tabId, videoId, codigo}` — e aí a extensão instala um
**vigia dentro da aba** (mundo ISOLATED) que faz três coisas:

1. **PAUSA a faixa a ~0,8s do fim.** É a trava de autoplay.
2. Avisa `faixa_iniciou`, `faixa_terminando` (~6s do fim, é o que faz o
   neo-core mandar a próxima) e `faixa_terminou`.
3. Devolve em todo aviso o `codigo` que veio no `watch` — é por ele que o
   neo-core sabe qual linha da fila atualizar.

`{"type":"unwatch"}` encerra a fila — mas **não** entrega o fim da faixa que está
tocando ao YouTube (ver abaixo).

### Por que pausar, e por que só pausar não basta

Se o vídeo chega ao fim, o YouTube emenda outro sozinho. Em vídeo de música ele
monta um mix/rádio (`list=RD…&start_radio=1`), onde o "próximo" vem da fila
DELE e o botão de desligar autoplay nem aparece. Sem `ended`, não há o que
emendar — por isso a trava é pausar.

Só que pausar o `<video>` **não segura o player**. Medido no navegador em
2026-09-08: com o elemento em `paused=true, ended=false` a 0,88s do fim, o
player do YouTube declarou `getPlayerState() === 0` (ENDED) por conta própria e
trocou de vídeo ~6s depois. Então o vigia, logo depois de pausar, **recua o
playhead** para ~4s do fim: o player volta para `2` (PAUSED) e não tem mais fim
nenhum para detectar. Como já estamos pausados, recuar não faz barulho.

O que **não** fazemos é clicar no `.ytp-autonav-toggle-button`: ele não existe
no caso que quebrava (mix/rádio) e é uma preferência do usuário — desligar por
baixo dos panos deixaria rastro depois do `unwatch`.

### Por que o vigia mora na página, e não no service worker

O service worker do MV3 é descartável, e leva junto qualquer `setInterval`; o
`chrome.alarms` tem piso de ~30s, que não serve para precisão de segundos. O
timer da página sobrevive à morte do worker, e o `chrome.runtime.sendMessage`
do vigia **acorda** o worker de volta (é também o que mantém o socket vivo — o
desktop desarma a vigia dele quando a conexão cai). O worker só instala,
encaminha e solta.

O relógio do vigia é o `timeupdate` do próprio `<video>` (~4x/s): ele vem do
player, não da fila de timers, e por isso não é estrangulado quando a aba está em
segundo plano — que é exatamente onde a música fica. O `setInterval` de 1s ao
lado é manutenção (URL, batida, TTL).

### O `unwatch` não larga a faixa no ar

O `faixa_terminando` sai **6s antes do fim**. Quando a faixa é a última da fila, o
neo-core responde `desarmar` na mesma hora e o desktop manda `unwatch` — com a
música ainda tocando. Soltar a trava ali devolvia o fim da última faixa de **toda
fila** para o autoplay do YouTube, que emendava um vídeo aleatório (medido em
produção 2026-09-09, "Erase My Scars": watch armado 02:55:45, desarmado 02:56:27,
autoplay em seguida).

Por isso o `unwatch` **solta** em vez de destruir: o vigia para de reportar e de
cutucar o worker, mas segura a trava até **esta** faixa acabar — e aí morre
sozinho. Vale também para "para a fila": quem começou a música foi o Neo, então
quem termina não é o autoplay. Outro vídeo qualquer não é afetado — o vigia é
preso ao `videoId` dele e some assim que a aba troca.

### Quando ele se solta sozinho

- `unwatch` — solto (acima): sai depois de travar o fim desta faixa;
- o worker dizendo que a fila já está em outro despacho;
- a aba mudou para outro vídeo — aí ele reporta `faixa_terminou` com o vídeo
  que **está** tocando, e o neo-core pausa a fila em vez de brigar pelo player;
- a extensão foi recarregada (`chrome.runtime.id` some) — obrigatório, senão o
  vídeo do usuário passaria a pausar sozinho no fim para sempre;
- 30 minutos, como teto.

Durante um **comercial** o vigia não faz nada: o `<video>` é o do anúncio, com
outra duração; pausar ali seria pausar o comercial e pedir a próxima antes de a
música tocar um segundo.

## Instalar no Microsoft Edge

1. Abra `edge://extensions`.
2. Ative o **Modo de desenvolvedor** (canto inferior esquerdo).
3. Clique em **Carregar sem pacote** (Load unpacked) e selecione esta pasta
   (`neo-music-ext`).
4. O **Neo Desktop precisa estar aberto** (ele hospeda o servidor WebSocket). A
   extensão reconecta sozinha quando o app sobe.
5. **Cole a credencial da ponte** (a partir da v1.3.0 ela é obrigatória): no Neo
   Desktop, aba **Dispositivos** → cartão **"Extensão de música"** → **Copiar**.
   No popup da extensão, seção **Credencial da ponte** → cole → **Salvar**. Uma
   vez por perfil de navegador; fica em `chrome.storage.local`.

> Também funciona no Brave/Chrome (mesma extensão, "Carregar sem pacote").

## Status da conexão (UI)

Clique no **ícone da extensão** na barra do Edge para abrir o popup, que mostra:

- ponto **verde** = conectado ao Neo Desktop / **amarelo** = conectando / **vermelho** = desconectado;
- as **abas de música** abertas (YouTube/Spotify) que estão sendo reportadas, com marca de "tocando";
- botão **Reconectar**.

O **ícone também ganha um badge** (`●` verde quando conectado, `○` cinza quando não) — dá pra ver o status sem abrir o popup.

## Verificar

Sem navegador (roda o `background.js` de verdade num VM):

```sh
node scripts/testar-sinais.mjs   # sonda, tipo de página, snapshot, controle
node scripts/testar-vigia.mjs    # a vigia de fila: pausa, eventos, código, limpeza
node scripts/testar-aba-da-fila.mjs  # em QUE ABA a fila toca (com várias abertas)
```

No navegador:

- O badge no ícone deve ficar verde (`●`) com o Neo Desktop aberto.
- Em `edge://extensions`, clique em **service worker** (no card da extensão) para
  abrir o console. Você deve ver `"[neo-music] connected to Neo Desktop"`.
- Nos logs do Neo Desktop: `"[ExtBridge] extension connected"` e, ao abrir/fechar
  abas do YouTube, o snapshot de abas chega ao app.
- Com uma aba do YouTube aberta, peça "toca <música> no youtube" — o Neo navega a
  aba existente para o vídeo certo (em vez de abrir uma nova).

Para exercitar a **vigia** sem o Neo, no console do service worker (com uma aba
do YouTube tocando):

```js
startWatch(<tabId>, "<videoId>", "teste123")   // arma
// pule para os últimos 15s do vídeo e observe:
//   [neo-music] vigia instalada na aba …
//   [neo-music] media_event faixa_iniciou / faixa_terminando …
// o vídeo tem que PAUSAR ~0,8s antes do fim e o playhead recuar para ~4s do fim
stopWatch()                                    // solta: a aba volta ao normal
```

Dois testes que valem a pena repetir à mão:

- **encerre o service worker** (link "encerrar" no card da extensão) com a vigia
  armada: a pausa tem que acontecer do mesmo jeito (ela é da página), e o próprio
  vigia acorda o worker para entregar o aviso;
- **sem `watch` armado**, deixe um vídeo qualquer terminar: nada pode pausar, e
  nenhum `media_event` pode sair.

### A aba da fila é uma só

Enquanto uma fila toca, **toda faixa vai para a mesma guia** — a que o usuário viu
a fila começar. Quem manda nisso é esta extensão (`abaDaFila`), não o servidor: o
alvo que vem no `watch` é um chute do `event_subscribe`, tirado do snapshot de
ANTES de a fila começar, e no primeiro despacho ele vem vazio.

Foi assim que a fila trocava de guia no meio (log do desktop, 12/09):

```
15:39:38  watch armado: aba None       faixa YdQ5NKjbd2U   ← ninguém sabia a aba
15:39:58  evento faixa_terminando → tocar
15:39:58  watch armado: aba Some(1976602762) …             ← aba do snapshot velho
```

A faixa 2 ia para a aba `1976602762` e a faixa 1 ficava pausada na guia em que o
Daniel estava ouvindo. Agora a aba é **aprendida** por quem a vê — o `result` do
play, o `tabId` do `media_event` (o vigia mora na página) e o snapshot — e sobe
até a inscrição no neo-core. Se o `watch` chegar apontando outra guia, a extensão
avisa no console e **fica onde está**:

```
[neo-music] fila continua na aba 77 — o desktop mandou 10
```

A fila só solta a aba no `unwatch`/fim de fila; a próxima fila escolhe de novo.

## Protocolo (WS, JSON)

Extensão → Desktop:
- `{"type":"tabs","tabs":[{"site":"youtube","tabId":123,"url":"…","videoUrl":"https://www.youtube.com/watch?v=…","title":"…","channel":"…","playing":true}]}`
- `{"type":"result","id":"<cmd id>","success":true,"error":"","tabId":123}`
  — o `tabId` é **a aba em que o comando caiu de verdade** (pode ser uma que a
  extensão acabou de abrir). Só este lado sabe disso; é por ele que o desktop
  corrige o alvo da vigia e, no evento seguinte, o alvo da inscrição.

- `{"type":"media_event","event":"faixa_iniciou|faixa_terminando|faixa_terminou","videoId":"<id>","codigo":"<código do despacho>","tabId":123}`

Desktop → Extensão:
- `{"type":"command","id":"<uuid>","action":"play","target":"youtube","videoId":"<id>","tabId":123,"novaAba":false}`
  — `tabId` é **pin, não ordem**: aba fechada cai no fallback. `novaAba:true` diz
  que o neo-core não achou aba reusável (ele se recusa a tomar vídeo do usuário):
  abra a sua em vez de escolher uma.
- `{"type":"watch","tabId":123,"videoId":"<id>","codigo":"<código do despacho>"}`
- `{"type":"unwatch"}`

Porta fixa `17872`, bind apenas em `127.0.0.1` (sem exposição na rede).

### Superfície do neo-core (v1.4.0)

Além de ser a ponte do Neo Desktop, a extensão pode ser um **dispositivo pareado
do neo-core** — um segundo canal, direto, que continua de pé com o **Neo Desktop
fechado**. Pareie no popup, seção *Navegador do Neo*.

**Por que existe.** Pedido que não vem do desktop (painel, celular) caía no
navegador do SERVIDOR — IP de datacenter, que loja brasileira bloqueia. Com este
canal o core sabe quais abas estão abertas aqui e pode endereçar este navegador de
qualquer lugar ("usa o navegador do macbook", dito pelo celular).

**Pareamento de um clique, com consentimento NATIVO.** O popup pede pela ponte
(`pair_request`), **o Neo Desktop abre um diálogo do sistema**, e ao autorizar ele
devolve um código de pareamento estampado com a própria máquina. A extensão troca
o código por um token de device e guarda em `chrome.storage.local`.

Não existe endpoint HTTP local que emita credencial: se existisse, qualquer aba
alcançaria. E a extensão não pode se declarar "do macbook" — quem sabe qual
máquina é, e pode provar, é o desktop.

**O que esta superfície declara saber fazer:** `tabs` (ler abas). Ação de
navegação (`click`, `fill`, `snapshot`…) segue sendo executada pelo Neo Desktop; o
core sabe disso pela declaração e recusa com mensagem própria em vez de tentar e
falhar. Ver `neo-core/docs/browser-surface-plan.md` §13.3.

**Duas credenciais, donos diferentes:** a *credencial da ponte* autentica a
extensão no Neo Desktop; a *superfície* autentica este navegador no neo-core. A
segunda é a que vale com o desktop fechado. Desparear uma não afeta a outra.

### Autenticação da ponte (v1.3.0)

O handshake exige `?token=<credencial>` e **recusa `Origin` de página web**
(aceita só `chrome-extension://`, `moz-extension://`, `safari-web-extension://`
ou ausência de `Origin`). A recusa é HTTP 401/403 **antes** do upgrade — nunca
101-seguido-de-close, que o cliente leria como queda de rede e viraria reconexão
infinita.

**Por que, se é loopback.** Loopback não é fronteira de confiança: qualquer
processo da máquina alcança a porta, e uma **página web** consegue abrir
WebSocket para `127.0.0.1` (o navegador manda `Origin`, e quem não olha não se
defende). Como a ponte guarda **uma** conexão por vez, a conexão hostil
*deslocava* esta extensão — passando a receber os comandos do Neo e a mandar
títulos de aba, que entram no prompt dele (`media_context.browser_tabs`). E
`media_event` faz o Neo Desktop chamar `/api/v1/eventos/midia` **com o token do
aparelho**: um gatilho local aberto empresta a credencial de quem a tem.

O token é gerado pelo Neo Desktop na primeira execução e vive em
`~/Library/Application Support/neo-desktop/ext-token` (0600). Trocar = apagar o
arquivo e colar o novo no popup. Ver `neo-core/docs/api-auth-plan.md` §11.
