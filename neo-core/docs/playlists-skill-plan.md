# Plano — Skill `playlists` (playlists + músicas, zero código)

> Status: **IMPLEMENTADO (fases 1–4.1) em 2026-09-08** · Autor: Claude + Daniel
>
> Em produção: migração `w6x7y8z9a0b1` aplicada, skill `playlists` instalada
> (3 cadastros em `neo_data`), `NEO_EVENTS_ENABLED=true`, 24 testes novos
> (`tests/skills/test_events.py`), suíte em 2075 passes.
>
> **Desvios do plano na implementação** (o resto saiu como está escrito abaixo):
> - `event_subscribe` exige a **faixa** concreta, não a aba. A aba entra depois,
>   pelo `observar` da atuação — porque no fluxo real o cérebro sabe o link da
>   linha da fila ANTES de tocar, e só saberia o `tab_id` depois. Trocar a
>   exigência manteve a regra ("nada de inscrição no vazio") e fez o ciclo caber
>   em UM turno.
> - O link da fila é normalizado para o id canônico no subscribe
>   (`https://youtu.be/AAA` → `AAA`). Sem isso a PRIMEIRA faixa seria
>   "inesperada" (a fila guarda URL, a extensão reporta `videoId`) e a inscrição
>   se pausaria sozinha no primeiro evento — o bug que parece "não funciona".
> - Quem arma o watch é a própria atuação de música: `_watch_for_actuation`
>   consulta as inscrições armadas e injeta `observar` nos argumentos. Sem
>   inscrição o campo não existe e o desktop **desarma** — inclusive quando o
>   usuário pede outra música no meio da fila.
> - A vigia mora no `ext_bridge.rs` (não num serviço novo): ele já tem os dois
>   lados (snapshot da extensão e `send_command`), e é lá que o filtro de borda
>   custa zero quando não há fila.
> - Runners implementados: `proxima_da_fila` e `avisa_no_chat`. `marca_linha`
>   não entrou — não apareceu caso de uso que os dois não cubram.
> - A dedup e a detecção de evento ATRASADO usam a mesma chave (`<aba>:<faixa>`):
>   depois de avançar, um `faixa_terminou` da faixa velha chega como repetido em
>   vez de virar "faixa inesperada" e pausar a fila sem motivo.
> - Fase 3 (tocar link salvo) saiu como `parse_direct_link` em
>   `core/skills/music/service.py`, reusado pelo runner — o mesmo parser decide o
>   que é vídeo, playlist, URI do Spotify ou URL solta nos dois caminhos.
>
> Uma pasta, um arquivo: `neo-core/skills/playlists/SKILL.md`. Duas tabelas de
> verdade no Postgres, tela pronta no painel, o Neo salvando e consultando por
> conversa. **Nenhuma linha de Python no neo-core.**
>
> Desacoplamento é literal: apagar a pasta remove a skill do catálogo e nada mais
> no Neo repara. As tabelas ficam (o instalador nunca dropa) — se quiser, some com
> elas pelo painel.
>
> **Fases 1 e 2 são zero código.** A fase 3 (tocar o link salvo) custa ~20 linhas e
> a fase 4 (o Neo tocando a fila sozinho, §7) é um subsistema de inscrições no
> core — vale a pena, mas é outro tamanho de obra.

---

## 1. Por que isso é de graça

Os cadastros generativos já estão em produção (`docs/datasets-crud-plan.md`,
F0–F7, flag `NEO_DATASETS_ENABLED` ligada). Uma skill que declara `datasets:` no
frontmatter ganha, sem código:

- tabela real em `neo_data.<slug>_<code>` (DDL rodado pelo instalador);
- as 6 tools `data_list / data_get / data_create / data_update / data_delete / data_schema`
  destravadas **só** quando o cérebro faz `skill_read` nessa skill;
- tela CRUD em `/dados/<slug>` no neo-panel, desenhada pelo schema;
- leitura analítica de graça: `sql_query_ro` enxerga `neo_data.*`.

O que estou planejando aqui é só **quais campos** e **o texto da skill**.

---

## 2. As tabelas

### 2.1 `playlists`

| campo | tipo | nota |
|---|---|---|
| `nome` | texto (120), obrigatório | é o rótulo da linha (`label_field`) |
| `servico` | opcao | `spotify · youtube · youtube_music · deezer · apple_music · local · mista` |
| `link` | texto (500) | URL/URI da playlist inteira — é isso que deixa você "anexar" uma playlist do Spotify |
| `observacao` | texto_longo | pra que serve / quando ouvir. Opcional, corte se achar gordura |

`servico = mista` é o caso "playlist minha, montada com música de vários lugares",
que não tem link — o link mora em cada música.

### 2.2 `musicas`

| campo | tipo | nota |
|---|---|---|
| `titulo` | texto (200), obrigatório | rótulo da linha |
| `artista` | texto (120) | |
| `playlist_nome` | texto (120) | **espelho legível** do nome da playlist (ver §4.1) |
| `link` | texto (500) | URL/URI da faixa |
| `servico` | opcao | `spotify · youtube · youtube_music · deezer · apple_music · outro` |
| `ordem` | inteiro | posição dentro da playlist. Vazio = tanto faz |
| `playlist` | relacao → `playlists` | o vínculo de verdade (UUID) |

**A ordem dos campos acima não é estética.** A definição inline não aceita
`list_columns`/`search_fields` — eles caem do default:

- `list_columns` = os 5 primeiros → a tela mostra título, artista, playlist, link, serviço;
- `search_fields` = os 3 primeiros campos de texto → a `busca` cobre título, artista **e nome da playlist**;
- `order_by` = `created_at desc` (mais nova primeiro).

Se um dia quiser mudar isso, é pelo painel (`PUT /api/v1/datasets/{slug}/schema`),
não pelo SKILL.md.

---

## 3. O arquivo

`neo-core/skills/playlists/SKILL.md`:

```yaml
---
name: playlists
description: 'Playlists e músicas salvas do Daniel: guardar playlist (com link do Spotify/YouTube), anexar música numa playlist, listar o que tem salvo. Use quando ele falar em salvar/guardar/anexar música ou playlist, ou perguntar o que tem numa playlist.'
keywords: [playlist, playlists, salvar musica, salva essa musica, guarda essa musica, anexa na playlist, minhas playlists, o que tem na playlist, repertorio]
kind: tool_backed
group: cadastros
discovery: keyword
datasets:
  - slug: playlists
    titulo: Playlists
    descricao: Playlists de música — próprias ou salvas de um serviço
    campos:
      - {name: nome, type: texto, label: Nome, required: true, max: 120}
      - {name: servico, type: opcao, label: Serviço, options: [spotify, youtube, youtube_music, deezer, apple_music, local, mista]}
      - {name: link, type: texto, label: Link, max: 500}
      - {name: observacao, type: texto_longo, label: Observação}
  - slug: musicas
    titulo: Músicas
    descricao: Músicas salvas, cada uma com onde tocar e o link
    campos:
      - {name: titulo, type: texto, label: Título, required: true, max: 200}
      - {name: artista, type: texto, label: Artista, max: 120}
      - {name: playlist_nome, type: texto, label: Playlist, max: 120}
      - {name: link, type: texto, label: Link, max: 500}
      - {name: servico, type: opcao, label: Serviço, options: [spotify, youtube, youtube_music, deezer, apple_music, outro]}
      - {name: ordem, type: inteiro, label: Ordem}
      - {name: playlist, type: relacao, label: Playlist (id), dataset: playlists}
enabled: true
version: 1
---
```

Corpo (o que o cérebro lê quando abre a skill):

- **Salvar playlist**: `data_list(playlists, busca=<nome>)` primeiro; só cria se não achar.
- **Anexar música**: sempre resolver a playlist antes → `data_list(playlists, busca=…)`,
  pegar o `id` e o `nome`, e gravar os DOIS (`playlist=<id>`, `playlist_nome=<nome>`).
- **Listar músicas de uma playlist**: `data_list(musicas, filtro={"playlist_nome": "Churrasco"}, ordem="ordem asc")`.
- **Regras**: só afirmar que salvou se vier `conferido`; nunca inventar link nem
  artista (perguntar, ou salvar sem); `data_delete` sem `confirmar` primeiro;
  dado de cadastro **se consulta, não se memoriza** (nada de `pks_remember` com
  conteúdo de playlist).

`discovery: keyword` = a skill só entra no catálogo quando a mensagem casa com as
keywords. Custo no prompt fora disso: zero.

---

## 4. Os quatro detalhes chatos (achados no código, não chutados)

### 4.1 `relacao` é UUID cru — em todo lugar
`serialize_value` devolve o UUID como string, e o painel renderiza `relacao` como
**input de texto pedindo o id** (`DadosPanel.tsx:290`). Ou seja: sem espelho, a
lista de músicas mostra `9f3c…` na coluna da playlist, e o cérebro precisa de um
`data_get` extra só pra dizer "essa é da playlist Churrasco".

Por isso os dois campos: `playlist` (verdade, pro futuro — a fila vai apontar pra
id) e `playlist_nome` (legibilidade, busca e filtro). Custo: renomear uma playlist
não propaga. Conserto quando doer: um `data_list` + `data_update` em lote, ou
mudar o nome só no painel e deixar o espelho velho.

> Alternativa mais preguiçosa: jogar fora o `relacao` e ficar só com
> `playlist_nome`. Fica mais simples hoje e mais burro na fase da fila. Recomendo
> manter os dois.

### 4.2 `opcao` vira `CHECK` no banco
Adicionar `tidal` na lista depois exige trocar a constraint (o instalador faz, mas
é edição de schema pelo painel, não `data_define`). Se achar que a lista de
serviços vai mexer muito, troque `servico` por `texto` — perde a garantia de que
o cérebro não escreve "spotfy".

### 4.3 O roteador já tem opinião sobre música
`playlist` é substantivo de mídia no `intent_router` e "toca/coloca/põe" são verbos
de play. A boa notícia: isso **não** dispara a skill de música direto — cai no
cognitivo com `skill_hint="music"` (`intent_router.py:1825`). A má: com esse hint na
mesa, o cérebro tende a fazer handoff pra tocar antes de pensar em ler a skill.

Por isso a fase 1 é só guardar, e as keywords são de guardar
(salvar/guardar/anexar/minhas playlists), não de tocar. "Toca a playlist churrasco"
continua indo pro caminho de hoje (busca no Spotify). É a fase 3.

### 4.4 Instalar é um passo separado de salvar o arquivo
Salvar o SKILL.md coloca a skill no catálogo (hot-reload por mtime), mas quem cria
a tabela é o instalador. Sem instalar, `_requires_ok` tira a skill do catálogo
(cadastro declarado que não existe) — ela some e você acha que quebrou.

```
1. cria a pasta + SKILL.md          (o Mutagen leva pra VPS)
2. painel → Skills → playlists → Instalar      [ou POST /api/v1/skills/playlists/install]
3. verify → confere as colunas no banco        [GET .../install/verify]
4. tela em /dados/playlists e /dados/musicas
```

Desinstalar não dropa tabela. Rollback de verdade = excluir o cadastro no painel.

---

## 5. Fase 2 — a fila (planejada, não construída)

Terceiro dataset na mesma skill, quando a fase 1 estiver rodando:

| campo | tipo | nota |
|---|---|---|
| `musica_titulo` | texto (200) | espelho, mesmo motivo do §4.1 |
| `link` | texto (500) | copiado da música na hora de enfileirar |
| `servico` | opcao | idem |
| `estado` | opcao | `pendente · tocando · tocada · pulada` |
| `posicao` | inteiro | ordem na fila |
| `quando` | data_hora | opcional — "toca isso às 18h" |
| `musica` | relacao → `musicas` | |

Enquanto ninguém consome, a fila é só um caderno: o Neo enfileira, você vê no
painel, e "toca a próxima" ainda é você. Já vale, e o custo é uma tabela.

**Cuidado**: 3 datasets inline já são 3 blocos de schema anexados ao corpo no
`skill_read` (~500-700 tokens). Se incomodar, a fila vira uma segunda skill que
referencia `musicas` só pelo slug.

---

## 6. Fase 3 — tocar (aqui acaba o "zero código")

Duas paredes reais:

1. `open_url` / `launch_uri` / `media_command` **não são selecionáveis pelo
   agente** de propósito (`modules/desktop/tools.py:244`) — senão o LLM inventa
   `spotify:track:…` e a música não existe.
2. A `MusicPipeline` só sabe **buscar faixa por texto**; não aceita link nem URI
   de playlist (`core/skills/music/pipeline.py`).

O conserto barato é uma coisa só, e é boa por si: **a MusicPipeline aceitar um link
concreto na ordem do handoff** — se a ordem contém `http…` ou `spotify:`, pula a
busca e emite `open_url`/`launch_uri` direto. Aí o fluxo fecha:

```
"toca a playlist do churrasco"
  → cognitivo (skill_hint=music) → skill_read(playlists) → data_list(playlists, busca="churrasco")
  → [[HANDOFF:music]] tocar https://open.spotify.com/playlist/...
  → MusicPipeline vê o link → open_url/launch_uri → toca
```

~20 linhas em 1 arquivo do neo-core. É o único acoplamento **da parte de tocar** —
a fase 4 abaixo é outro assunto e custa mais.

---

## 7. Fase 4 — inscrições: o Neo dirigindo a fila sozinho

O pedido: montei uma fila no YouTube, a música acaba, a extensão avisa o neo-core,
o neo-core vê se alguém está inscrito naquele canal e a função da skill despacha a
próxima. E quando eu só peço "toca um AC/DC", **nada disso existe**.

### 7.1 O ponto de partida (o que hoje NÃO existe)

Levantado no código, não suposto:

| peça | estado hoje |
|---|---|
| extensão → desktop | WebSocket em `ws://127.0.0.1:17872` (`ext_bridge.rs:22`). A extensão manda **snapshot de abas**, não evento |
| cadência do snapshot | eventos de aba (`onUpdated/onActivated/…`) + alarme de **24s** (`background.js`, `periodInMinutes: 0.4`) |
| desktop → neo-core | o snapshot só viaja **junto com um turno**, em `metadata["media_context"]`. Fora do turno, ninguém conta nada pra ninguém |
| neo-core → desktop | existe: stream `SubscribeNotifications` (proto) e o HTTP normal (`neo_core_http_url()`, `commands/mod.rs:242`) |
| desktop → extensão | existe: `ExtBridge::send_command(action, target, video_id, tab_id)` |

Ou seja: **as duas pontas existem, falta o gatilho**. Nada precisa ser reinventado
— o que falta é (a) alguém reparar que a faixa acabou, (b) um lugar no neo-core
que saiba a quem isso interessa, (c) o caminho de volta.

### 7.2 A ideia que muda o desenho: o evento certo é "**está acabando**", não "acabou"

Se esperar o fim, você compra dois problemas de graça:

- **silêncio**: com o alarme de 24s, o pior caso é ~24s de nada entre faixas;
- **briga com o autoplay**: numa aba de YouTube (ainda mais em mix `list=RD…`), o
  próprio YouTube emenda a próxima. Você chega atrasado e troca a música 2s depois
  que ela começou — o usuário vê o Neo "roubando" o player.

Despachar quando faltam ~5s (`duration - currentTime < 5`) resolve os dois: a
navegação acontece antes do autoplay decidir, e não tem buraco. `faixa_terminou`
continua existindo como **rede de segurança** (fim súbito, pulo manual), não como
o gatilho principal.

### 7.3 A inscrição é um contrato concreto, não um interesse genérico

Esse é o coração do "não pode vazar pro pedido normal". Uma inscrição **não** é
"me avise quando qualquer música acabar". É:

```
inscricao {
  id
  user_id · device_id                     # em qual máquina
  canal:   "musica.youtube"
  evento:  "faixa_terminando" | "faixa_terminou"
  alvo:    { site: youtube, tab_id: 42 }  # em qual ABA
  esperado: "dQw4w9WgXcQ"                 # a faixa que EU mandei tocar
  faz:     { skill: "playlists", acao: "proxima_da_fila", fila: "fila" }
  estado:  armada | pausada | encerrada
  expira_em: now() + 2h · max_disparos: 200 · disparos: 0 · erros_seguidos: 0
}
```

`esperado` é o detalhe que faz tudo funcionar: a inscrição fica grudada **na faixa
que a própria fila despachou**, e é re-armada a cada despacho. Se acabar outra
coisa, o evento não é meu.

### 7.4 As três camadas que protegem o "toca um AC/DC pra mim"

1. **Nada armado → o evento nem nasce.** O desktop só chama o neo-core se tiver um
   *watch* para aquela aba. Pedido normal não cria inscrição → zero requisição,
   zero token, zero linha no banco. O caminho de hoje continua idêntico, byte por
   byte.
2. **O servidor confere de novo**: inscrição existe? é deste device? está armada?
   não expirou? o `esperado` bate com o que acabou? Qualquer "não" → 204, fim.
3. **`esperado` que não bate = usuário assumiu o controle.** Você pediu AC/DC no
   meio da fila: o vídeo que termina não é o que despachei → a inscrição **se
   pausa** (não avança, não fecha aba, não briga). Volta com uma frase sua
   ("continua a fila") ou morre no TTL.

O mesmo raciocínio do guard de birra e do `_SKILL_OWNED_TOOL_PREFIXES`: o gatilho
tem que ser cego pro que não é dele, por construção — não por bom senso do LLM.

### 7.5 O caminho do evento (sem canal novo, sem proto novo)

```
extensão: "faltam 5s no vídeo dQw…"   [só na aba observada]
   → desktop (ext_bridge): tem watch armado pra essa aba? senão, descarta
   → POST /api/v1/eventos/midia   { device_id, tab_id, video_id, evento, event_id }
   → EventRouter: acha a inscrição → roda o handler (SEM LLM)
   → handler: próxima linha da fila → marca "tocando" → devolve o link
   ← 200 { acao: "tocar", video_id: "xyz", tab_id: 42, rearmar: "xyz" }
   → desktop: ext_bridge.send_command("play", …, "xyz", Some(42)) + re-arma o watch
```

**A resposta do POST é o comando.** Um round trip, nenhum canal de push novo,
nenhuma mudança no `.proto`, nenhuma autenticação nova (o device já tem token).
Quando não há nada a fazer: `204`, e o desktop desarma.

E o primeiro *arm* pega carona: a atuação que **inicia** a fila
(`media_command`/`open_url`) leva junto `observar: {inscricao_id, video_id}`. O
desktop guarda e pronto — não precisa de push do core pra saber o que vigiar. Se o
desktop reiniciar, ele pergunta uma vez (`GET /api/v1/eventos/inscricoes?device_id=…`)
e re-arma.

### 7.6 Quem escreve a "função da skill" — três níveis, e eu recomendo o primeiro

| nível | o que é | custo por faixa | quando usar |
|---|---|---|---|
| **N1 declarativo** ✅ | o SKILL.md nomeia um *runner* pronto do core (`proxima_da_fila`, `avisa_no_chat`, `marca_linha`) e diz em qual dataset ele mexe | ~10ms, 0 token | **sempre que der** |
| N2 Python da skill | `handlers.py` na pasta da skill | idem | é a F8 do plano de datasets (`NEO_SKILL_PY_TOOLS_ENABLED`, off por padrão, instalação com humano na frente). Código arbitrário dentro do neo-core |
| N3 cognitivo ("modo DJ") | o evento vira turno do cérebro | 1 chamada de LLM + latência | **só na borda**: fila esvaziou → "acabou, quero que eu siga no clima?" |

N1 no SKILL.md fica assim — continua sendo um arquivo só:

```yaml
eventos:
  - quando: faixa_terminando
    canal: musica
    faz: proxima_da_fila
    fila: fila                      # o dataset
    ordem: posicao asc
    filtro: {estado: pendente}
    campo_link: link
    marca: {despachada: estado=tocando, anterior: estado=tocada}
    encerra_quando: fila_vazia
```

Determinístico, sem LLM no caminho quente, testável sem navegador. Se o cérebro
tivesse que decidir cada faixa, você pagaria um turno a cada 3 minutos e a fila
viraria improviso.

### 7.7 As tools que a skill ganha

`event_subscribe` · `event_unsubscribe` · `event_list`, gated pelo `skill_read`
igual às `data_*`, e prefixo `event_` em `_SKILL_OWNED_TOOL_PREFIXES`
(`intent_router.py:342`) — o classificador nunca roteia pra elas.

**Regra dura**: `event_subscribe` **recusa** sem alvo concreto. O cérebro só pode
inscrever como parte de "comecei a tocar a fila X nesta aba" — nunca "vou ficar de
olho em música". Inscrição no vazio é exatamente o bug que você não quer.

As inscrições moram numa tabela do core (`event_subscription`, alembic, `public`),
não num dataset: é encanamento com índice por `(device_id, tab_id)` e TTL, não
cadastro de negócio. A **fila** continua sendo dataset da skill.

### 7.8 Melhorias que eu proponho (o que vai te morder se faltar)

1. **Idempotência por `event_id`** (`device+tab+video+bucket de 10s`). Sem isso, um
   reconnect da extensão reenvia o snapshot e você pula 3 músicas de uma vez.
2. **Lock por inscrição** (advisory lock / `FOR UPDATE`): alarme e listener podem
   chegar juntos. Mesmo motivo do item 1, causa diferente.
3. **Circuit breaker**: 3 erros seguidos ou 3 comandos sem confirmação → encerra a
   inscrição e avisa **uma vez** no chat. É o antídoto do retry infinito que já
   travou mic + wake word uma vez (`neo-live-tts-stall-lockout`).
4. **Teto duro**: `expira_em` (2h) e `max_disparos`. Inscrição imortal é a coisa
   que mais vai te assombrar num sistema que você esquece que ligou.
5. **Kill switch óbvio**: "para a fila" em palavras, botão no painel, e morte
   automática quando a aba fecha ou o device some. Mais `NEO_EVENTS_ENABLED`
   (default **false**).
6. **`event_delivery` (log de entrega)**: evento, inscrição, ação, sucesso, motivo.
   Sem isso o modo de falha é "a fila parou e ninguém sabe por quê" — a mesma dor
   da skill HA dizendo "desliguei" sem ter desligado.
7. **O link NUNCA vem do payload do evento.** O que a extensão manda é só
   *identificação* (tab, video, estado); o link despachado sai **sempre** de uma
   linha do dataset. Isso fecha a porta de "extensão/página comprometida faz o Neo
   abrir qualquer URL".
8. **O POST não pode acordar o cognitivo.** Nenhum caminho do handler N1 toca
   sessão, prompt ou LLM. Se um dia tocar, o custo da fila deixa de ser zero.
9. **Link limpo**: despachar sempre o `videoUrl` canônico (sem `list=`/`index=`/`t=`),
   que a extensão já sabe montar — senão você cai de novo no mix e o YouTube
   volta a mandar na fila.
10. **Genérico de graça**: a tabela e o roteador falam `canal`/`evento`, não
    "música". Amanhã "chegou email", "cron bateu", "porta abriu" usam o mesmo
    trilho sem reescrever nada. Custo hoje: zero — é só não hardcodar "youtube".
11. **Um `event_list` no painel** (que inscrições estão armadas, desde quando, o
    que dispararam). 20 linhas de tela que economizam uma tarde de debug.

### 7.9 Código por item, status real e autoplay (ajuste de 2026-09-08)

O desenho original marcava a linha como "tocando" no momento do **despacho** e
inferia o fim pelo snapshot. Duas coisas estavam erradas nisso: "mandei tocar"
não é "está tocando", e quem enxerga o `<video>` é a extensão, não o snapshot.

**Código único por despacho.** Cada item despachado recebe um `codigo` de 8 hex
gravado na linha da fila (`campo_codigo`). Ele viaja para o aparelho, e volta em
todos os eventos. É ele — e não "quem estiver como tocando" — que diz qual linha
atualizar. A mesma música enfileirada duas vezes tem dois códigos: o código
identifica a **reprodução**, não a faixa. Ele também aparece no popup da
extensão, então dá para casar "o que estou ouvindo" com "o que o Neo acha que
está tocando" sem abrir o banco.

**Quatro estados, e cada um quer dizer uma coisa:**

```
pendente → despachada → tocando → tocada
             (mandei)   (o player   (a extensão
                        confirmou)   viu acabar)
```

`faixa_iniciou` é um evento de **status**: não faz a fila andar, não passa pela
dedup e não re-arma nada. Isso importa mais do que parece — ele e o
`faixa_terminando` da mesma faixa dividem a chave de dedup (`<aba>:<faixa>`), e
se o status carimbasse a chave, o "está acabando" chegaria como repetido e a
fila **travaria na primeira música**.

**Quem controla o fim é a extensão.** Ela mantém uma máquina de estados por
código (armada → tocando → terminando → terminou), sonda a aba observada a cada
2s e emite um evento por transição, uma vez só. O caminho antigo (inferir pelo
snapshot no desktop) continua como reserva e se desliga sozinho no primeiro
evento explícito (`ext_driven`) — os dois nunca disputam o mesmo fim.

**Autoplay desligado no fluxo.** Enquanto o Neo dirige, a extensão clica no
`.ytp-autonav-toggle-button` da aba observada. Sem isso o YouTube escolhe a
próxima música e briga com a fila — o usuário vê a faixa trocar para algo que
ele não pediu. Fora deste fluxo nada é tocado: a preferência do usuário no resto
do navegador continua como está.

**A janela de chegada (bug real, medido em produção).** No primeiro teste de
verdade a fila pausou sozinha em 16 segundos: a vigia nasce no mesmo instante em
que a aba **ainda mostra a música anterior**, o lado do Neo leu "vídeo diferente
do esperado" e concluiu "o usuário assumiu o player". A correção é uma distinção
que faltava: enquanto a nossa faixa não aparecer **nenhuma vez** naquela aba,
vídeo diferente ali significa "a navegação ainda não chegou", não "trocaram".
Passados 30s sem ela aparecer, a vigia se desliga calada — inventar um fim que
não houve é pior do que parar.

**A extensão diz que está sob controle.** Badge roxo com `▶` e um bloco no popup
("Controlado pelo neo-core" + fase + código). A diferença entre "a ponte está
conectada" e "o Neo está no comando do player agora" precisava ser visível.

---

### 7.10 O que o primeiro uso real quebrou (2026-09-08, sessão e8170c41)

Três defeitos encadeados, todos visíveis no log da sessão. Vale registrar porque
nenhum deles aparece em teste de unidade — eles moram nas juntas.

**1. Inscrição pausada é inscrição morta, e ninguém a ressuscitava.** No pedido
"coloca a fila de novo para tocar começo da primeira de novo", o cérebro chamou
`event_list` (viu `pausada`), chamou `data_list` e foi direto para o handoff —
**sem `event_subscribe`**. Como o roteador só considera `state='armada'`, nenhum
evento casaria: a fila nunca andaria, com tudo o mais funcionando.

Duas correções, e a ordem importa:
- **sistema** (a que vale): mandar tocar a faixa que a inscrição pausada esperava
  JÁ É dizer que a fila voltou → `_watch_for_actuation` **retoma** a inscrição
  sozinha. E o inverso também virou regra: tocar uma faixa que NÃO é a esperada
  **pausa** a fila, em vez de deixá-la seguir por cima do que o usuário pediu.
- **prompt** (reforço): o SKILL.md ganhou o passo "retomar/recomeçar". Sozinho
  ele não bastaria — "garantir que ele lembre" nunca é prompt.

**2. `open_url` abre uma ABA NOVA e o alvo apontava para a velha.** A atuação
não reusou a aba (a pipeline só reusa aba que o Neo colocou música), então a
música foi para uma aba nova enquanto `target.tab_id` continuava na antiga —
e o casamento por aba recusaria todos os eventos. Agora o evento carrega o
`inscricao_id` que nós mesmos entregamos: quando ele vem, ele manda, e o alvo é
**atualizado** para a aba nova. A tolerância vale só para esse id — evento solto
de outra aba continua recusado. Do lado da extensão, a vigia acha a aba pelo
`videoId` quando o `tabId` não serve.

**3. O autoplay não foi desligado — e o YouTube tinha virado RÁDIO.** A aba
estava em `watch?v=…&list=RD…&start_radio=1`. Duas causas somadas: (a) a vigia
nunca foi armada (defeitos 1 e 2), então a extensão nunca recebeu o `watch` que
dispara o `disableAutoplay`; (b) mesmo armada, o toggle de autonav **não impede
um rádio** — com `list=RD…` ativo o "próximo" vem da fila do YouTube. Agora, ao
confirmar a faixa, a extensão também **volta para a URL canônica** (sem `list`/
`start_radio`), matando o rádio. Só na confirmação, quando a faixa está em ~0s:
navegar no meio da música reiniciaria ela.

---

### 7.9 Fases dessa parte

| Fase | Entrega | Como se prova |
|---|---|---|
| **F4.0** | tabela + registry + `POST /api/v1/eventos/midia` + runner `proxima_da_fila` + tools `event_*` + watch no Rust, **usando o snapshot que já existe** | fila de 3 músicas toca sozinha (com até ~24s de buraco entre elas) |
| **F4.1** | extensão: na aba observada, avisa em `ended` e em "faltam 5s" | buraco vai a zero e o autoplay para de brigar |
| **F4.2** | `avisa_no_chat` + fila vazia → 1 mensagem proativa (reusa `chat_message`, `jobs/dispatcher.py:164`) | "acabou a fila do churrasco" aparece no chat sem você perguntar |
| **F4.3** | N2/N3 (handler Python, modo DJ) | só se F4.0–F4.2 não derem conta |

**Esforço**: F4.0 ≈ 1 dia (migração + registry + endpoint + runner + 3 tools + o
watch no `ext_bridge.rs`). F4.1 ≈ 2h de extensão. F4.2 ≈ 2h.

E o teste que importa, o seu: **"toca um AC/DC"** com `NEO_EVENTS_ENABLED=true` e
nenhuma fila rodando → nenhuma linha em `event_subscription`, nenhum POST, nenhum
log de entrega. Se aparecer qualquer coisa, o desenho está errado.

---

## 8. O que NÃO fazer

- ❌ Criar módulo Python, tabela via alembic ou tool nova **na fase 1**. Nada disso
  é preciso pra guardar playlist e música.
- ❌ Guardar a lista de músicas como JSON dentro da playlist — mata `sql_query_ro`,
  filtro e a tela.
- ❌ Deixar o cérebro gravar playlist no PKS (`pks_remember`). Cadastro se consulta.
- ❌ Deixar o cérebro criar playlist sem `data_list` antes: em uma semana você tem
  "Churrasco", "churrasco" e "Churras".
- ❌ Mexer no roteador na fase 1.
- ❌ **Mandar todo evento de mídia pro neo-core e filtrar lá.** O filtro tem que
  estar na borda (§7.4), senão cada faixa de cada música vira requisição.
- ❌ **Inscrição sem alvo concreto** ("de olho em música"). Só com aba + faixa.
- ❌ **LLM no caminho quente do evento.** N1 declarativo resolve; o cérebro só
  aparece na borda (fila vazia).
- ❌ Aceitar link vindo do payload do evento (§7.8.7).

---

## 9. Decisões pendentes (Daniel)

1. **`relacao` + espelho, ou só o nome da playlist em texto?** (§4.1) — recomendo
   os dois.
2. **`observacao` na playlist e `ordem` na música ficam ou saem?** São os dois
   campos que você não pediu; entrei com eles porque são baratos.
3. **A fila entra já na fase 1** (terceira tabela, sem consumidor) **ou espera?**
   Se a fase 4 é o destino, ela entra logo — o consumidor da fase 4 é ela.
4. **Fase 4 fica no N1 declarativo** (runner do core, zero código na skill) **ou
   você quer o `handlers.py` da skill (N2) desde já?** Recomendo N1: o N2 é código
   arbitrário rodando dentro do neo-core, e a fila não precisa disso.
5. **A extensão pode mudar (F4.1)?** Sem mexer nela dá pra fazer tudo, com ~24s de
   silêncio entre faixas. Com ~30 linhas nela, o buraco some.

**Esforço**: fase 1 ≈ 30 min · fase 2 ≈ 15 min · fase 3 ≈ meia tarde ·
fase 4 ≈ 1 dia + 4h. Só as fases 3 e 4 tocam o neo-core.
