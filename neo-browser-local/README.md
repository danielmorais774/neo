# neo-browser-local

O braço do Neo **no navegador que você está olhando**. Fork da extensão do
[open-browser-use](https://github.com/iFurySt/open-browser-use) (MIT) com o
painel do Neo — a linha do tempo do que o agente está fazendo — e o botão PARE.

Plano: [`neo-core/docs/browser-local-plan.md`](../neo-core/docs/browser-local-plan.md).

## Como funciona

```
neo-core (VPS) ──/ws/devices/control──▶ neo-desktop-tauri ──unix socket JSON-RPC──▶ obu (host nativo)
                                                                                        │ native messaging
                                                                                        ▼
                                                                              esta extensão ──CDP──▶ sua aba
```

O neo-core decide o passo; o desktop traduz para CDP; a extensão executa. A
extensão **não** fala com LLM nenhum e não decide nada.

## O painel

Na aba que o Neo reivindica sobe um painel na **lateral direita**, e cada
evento do agente vira um item empilhado:

```
● NEO  agindo nesta aba                      [–]
│ 14:02:11  Vou abrir a busca
│           passo 1/14 · abrindo https://…
│ 14:02:13  (li de «Conteúdo principal»)
│           Um navegador web é um programa…      ← clique abre o resto
│ 14:02:15  não deu: clicando em «Entrar»
│           elemento desabilitado
└──────────────────────────────────────────
              [ PARE ]
```

Era uma faixa de uma linha no topo, trocada a cada passo: o que o agente fez há
dois passos sumia, e o que ele **produziu** (a leitura, a resposta da olhada
com visão, o motivo da falha) não tinha onde aparecer.

- a lista **sobrevive à navegação** (o estado fica no `storage.session` do
  background, por aba) e ao fim da tarefa — a última coisa que você lê é a
  entrega, não uma tela limpa;
- o `–` recolhe para uma pastilha, e a escolha fica guardada;
- detalhe longo nasce cortado em 3 linhas; um clique abre;
- nada disso toca a página: `position:fixed`, shadow DOM fechado,
  `pointer-events:none` fora do painel, e o id é excluído do inventário de
  elementos que o agente enxerga;
- antes de cada print o driver **esconde** o painel (ele comia um quarto da
  largura da foto que o modelo de visão lê).

`python3 scripts/testar-painel.py` prova tudo isso num Chromium de verdade.

## Instalação

```bash
npm i -g open-browser-use     # o host nativo (binário `obu`)
./scripts/setup.sh            # registra o host p/ Brave/Chrome/Edge com o nosso ID
```

Depois, no navegador: `brave://extensions` → Modo do desenvolvedor → **Carregar
sem compactação** → esta pasta → **reinicie o navegador**.

O ID tem de ser `onleolnecjhegkhdkmhchenfljnbijce` (vem da chave em
`keys/manifest-key.txt`). Se aparecer outro, o `key` do `manifest.json` não foi
lido e o host nativo vai recusar a conexão.

### A chave

`keys/neo-browser-local.pem` é a chave privada que fixa o ID da extensão. Ela
**não** é segredo de segurança (a extensão é local e não vai para a Web Store),
mas se for perdida o ID muda e o `scripts/setup.sh` precisa rodar de novo.
Está no `.gitignore` por higiene.

## Conta do Neo (v0.2.0)

Até aqui esta extensão não tinha identidade própria no Neo: ela falava com o host
`obu`, que falava com o `neo-desktop-tauri`, que era o pareado. A "conta" dela era
a do Mac, por transitividade, e não havia onde colocar credencial nenhuma.

Agora ela pode **parear como um dispositivo**, no popup → seção *Conta do Neo*:

1. no painel do Neo, aba **Dispositivos**, gere um **código de pareamento**;
2. digite os 6 dígitos no popup e dê um nome — **ponha a máquina no nome**
   ("Edge do macbook");
3. pronto: ela vira uma linha no registro de dispositivos, com token próprio,
   revogável sozinha.

**Não precisa do `neo-desktop-tauri`.** O código sai do painel, então este é o
caminho de quem não tem desktop na máquina. A contrapartida é que a superfície
nasce **sem vínculo de máquina** (`host_device_id`) — daí a insistência no nome:
é ele que faz "usa o navegador do macbook", dito pelo celular, encontrar este aqui.

### Por que CÓDIGO e não um token colado

O código é de **uso único** e expira em 5 minutos: se vazar, não vale nada. Um
token colado à mão é uma credencial que precisa ser vista, copiada e guardada — e
credencial que se vê acaba em screenshot (foi o que aconteceu com o token do
desktop em 2026-09-27). O token desta extensão é gerado pelo core, guardado pelo
`chrome.storage.local` e **você nunca o vê**.

### O que ela declara saber fazer

Só `tabs` (e mesmo isso **desligado por padrão** — ver abaixo). **Ação de navegação
continua indo pelo `neo-desktop-tauri`**: é ele que tem a biblioteca de payloads, a
marcação numerada do print e o casamento do download com o disco. O core sabe disso
pela declaração e recusa com mensagem própria, em vez de tentar e falhar.
Ver `neo-core/docs/browser-surface-plan.md` §13.3.

### Reportar abas é opt-in

Em *Avançado*. Esta extensão enxerga `<all_urls>`: ligar manda título e URL de
todas as abas para o neo-core. Parear dá **identidade**; reportar é outra decisão,
e é sua — por isso nasce desligado.

### "Failed to fetch" ao parear (resolvido na v0.2.1)

O `connect-src` herdado do upstream era `'self'`, e o pareamento é a **primeira**
coisa nesta extensão que sai para a rede — upstream tudo passa por *native
messaging*, que não tem CSP. Resultado: o `fetch` do `/devices/pair` morria antes
de sair da máquina, com a mesma frase que o navegador usa para "não há ninguém
na porta". O `WebSocket` do canal caía igual, só que mudo (reconexão eterna).

Se voltar a aparecer, as causas agora são distinguíveis:

- a mensagem **nomeia o endereço** e manda olhar o console do service worker
  (`edge://extensions` → *service worker*). `Refused to connect … connect-src` =
  é o manifest de novo (upstream reaplicado por cima? ver `UPSTREAM.md`);
- sem essa linha no console, é o **túnel**: `lsof -nP -iTCP:8000 -sTCP:LISTEN`
  no Mac, e depois `curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:8000/health`;
- endereço fora de `https://` ou de `127.0.0.1`/`localhost` é recusado **antes**
  da rede, com o motivo dito — o campo *Avançado* não alcança o CSP.

## Verificar

- Ícone da extensão com bolinha **verde** = host nativo conectado.
- `obu ping` no terminal responde.
- `obu user-tabs` lista as abas abertas.
