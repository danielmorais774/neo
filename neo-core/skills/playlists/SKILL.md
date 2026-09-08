---
name: playlists
description: 'Playlists, músicas salvas e fila de reprodução: guardar, anexar música numa playlist, enfileirar e consultar. Use quando falarem em salvar, guardar, anexar ou enfileirar música/playlist.'
keywords: [playlist, playlists, salvar musica, salva essa musica, guarda essa musica, anexa na playlist, minhas playlists, o que tem na playlist, fila de musica, enfileira, repertorio]
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
  - slug: fila
    titulo: Fila de reprodução
    descricao: O que tocar em seguida, na ordem — consumida pelo gatilho de fim de faixa
    campos:
      - {name: musica_titulo, type: texto, label: Música, required: true, max: 200}
      - {name: link, type: texto, label: Link, max: 500}
      - {name: servico, type: opcao, label: Serviço, options: [spotify, youtube, youtube_music, deezer, apple_music, outro]}
      - {name: estado, type: opcao, label: Estado, options: [pendente, despachada, tocando, tocada, pulada]}
      - {name: posicao, type: inteiro, label: Posição}
      - {name: quando, type: data_hora, label: Tocar a partir de}
      - {name: musica, type: relacao, label: Música (id), dataset: musicas}
      - {name: codigo, type: texto, label: Código, max: 16}
eventos:
  - nome: fila_musica
    quando: [faixa_iniciou, faixa_terminando, faixa_terminou]
    canal: musica
    faz: proxima_da_fila
    fila: fila
    ordem: posicao asc
    filtro: {estado: pendente}
    campo_link: link
    campo_titulo: musica_titulo
    campo_estado: estado
    campo_codigo: codigo
    marca: {despachada: despachada, tocando: tocando, anterior: tocada, pulada: pulada}
    encerra_quando: fila_vazia
enabled: true
version: 1
---

# Playlists, músicas e fila

Três cadastros: `playlists` (a coleção), `musicas` (as faixas salvas) e `fila`
(o que tocar em seguida). Os campos exatos estão nos blocos de cadastro acima.

## Salvar uma playlist
1. `data_list(dataset="playlists", busca="<nome>")` — **sempre antes de criar**.
   Sem isso viram três "Churrasco" com grafias diferentes.
2. Se não existir: `data_create(dataset="playlists", campos={...})`.
   `link` é a URL da playlist inteira (Spotify/YouTube); `servico="mista"` é
   playlist montada por você, sem link próprio.

## Anexar uma música a uma playlist
1. Resolva a playlist primeiro: `data_list(dataset="playlists", busca="...")`.
2. Grave os DOIS vínculos: `playlist=<id da playlist>` e
   `playlist_nome=<nome da playlist>`. O `id` é a verdade; o nome é o que deixa
   a lista legível e a busca funcionar.
3. `data_create(dataset="musicas", campos={"titulo": ..., "artista": ...,
   "link": ..., "servico": ..., "playlist": ..., "playlist_nome": ...})`.

## Consultar
- Músicas de uma playlist:
  `data_list(dataset="musicas", filtro={"playlist_nome": "Churrasco"}, ordem="ordem asc")`.
- Busca solta (título, artista, playlist): `data_list(dataset="musicas", busca="...")`.
- Análise (quantas músicas por playlist, o que está sem link): `sql_query_ro`
  nas tabelas citadas nos blocos de cadastro.

## Montar e tocar uma fila
1. Enfileire na ordem: `data_create(dataset="fila", campos={"musica_titulo": ...,
   "link": ..., "servico": ..., "estado": "pendente", "posicao": 1})`, 2, 3…
   Copie o `link` da música — a fila é autossuficiente de propósito.
   **Nunca preencha `codigo`**: quem gera é o sistema, na hora de despachar.
2. Para o Neo tocar sozinho, **na mesma resposta**:
   a. pegue a primeira linha pendente (`data_list(dataset="fila",
      filtro={"estado": "pendente"}, ordem="posicao asc", limite=1)`);
   b. `event_subscribe(gatilho="playlists.fila_musica", faixa="<link dessa
      linha>")` — a inscrição fica grudada NESSA faixa;
   c. mande tocar essa faixa (handoff de música com o link).
   Quando ela estiver acabando, a próxima linha `pendente` é despachada
   sozinha, sem passar por você.

   O estado anda sozinho e quer dizer coisas diferentes:
   `pendente` → `despachada` (mandei tocar) → `tocando` (o player confirmou que
   começou) → `tocada`. Quem confirma o `tocando` e o fim é a extensão, pelo
   `codigo` daquele despacho.
3. Parar: `event_unsubscribe()` ou "para a fila". Ver o que está armado:
   `event_list()`.
4. Se a pessoa pedir outra música no meio da fila, **não** re-inscreva: a fila
   pausa sozinha e ela retoma pedindo ("continua a fila").

## Regras
- Só afirme que salvou/alterou se o retorno trouxer `conferido`.
- Nunca invente link, artista ou título que a pessoa não disse — pergunte, ou
  salve sem o campo.
- `data_delete` SEM `confirmar` primeiro (vem preview); só confirme depois de
  ela confirmar em palavras.
- **Dado de cadastro se consulta, não se memoriza**: nada de `pks_remember` com
  conteúdo de playlist/fila. O cadastro é a fonte.
- `event_subscribe` só faz sentido logo depois de você mandar tocar algo
  concreto, com a aba e a faixa em mãos. Nunca "de olho em música" no vazio —
  a tool recusa, e com razão: um pedido solto ("toca um AC/DC") não pode
  disparar fila nenhuma.
