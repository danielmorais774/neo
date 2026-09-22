---
name: ocr-video
description: >
  Extrai por OCR o texto de um vídeo de tela: conversa de chat rolando (remetente, horário, data) ou slides.
  Use para ler/transcrever o vídeo ou tirar as mensagens dele.
keywords: [ocr, gravacao de tela, leitura do video, ler o video, transcrever o video, transcricao do video, mensagens do video, video do chat, filmagem do chat, texto do video]
kind: scripted
register: oficina
enabled: true
version: 2
---

# OCR de vídeo de tela

## Quando usar
- Vídeo com TEXTO na tela para extrair: conversa de chat rolando (WhatsApp,
  Telegram…), apresentação, documento. Não serve para fala/áudio (isso é
  transcrição de áudio).

## Como fazer — um job só, do começo ao fim
1. **Vídeo no sandbox.** Se o arquivo está no disco do usuário, chame
   `request_user_file(mode="path", path=<caminho que ele citou>, stage=true)`.
   Antes, olhe o caderno e o manifesto: se o mesmo vídeo JÁ chegou (file_id e
   `/workspace/files/<nome>`), use-o — não peça o upload de novo.
2. **Rode o pipeline inteiro em background**, com a entrega combinada em `then`:

```
neo_bash_background(
  title="OCR do vídeo <nome curto>",
  command="python3 /skills/ocr-video/scripts/ocr_video.py '/workspace/files/<nome do vídeo>' --out /workspace/ocr/<slug>",
  timeout=3600,
  then="Leia /workspace/ocr/<slug>/resultado.json e o arquivo que ele indicar, e entregue <o pedido do usuário, com o formato exato>. Depois apague /workspace/ocr/<slug> e '/workspace/files/<nome do vídeo>'."
)
```
   - `then` é a ordem para VOCÊ MESMO quando o job terminar: copie o pedido do
     usuário com o formato que ele quer e o nome do arquivo final.
   - Responda em UMA frase que o OCR está rodando e que você entrega quando
     terminar. Encerre a resposta — não fique consultando o job.
3. **Na continuação** (ela chega sozinha com o resultado do job), veja o `modo` do
   resultado:

### `"modo": "chat"` — conversa em balões
O script já separou remetente, horário, data, citação e anexos. Leia
`/workspace/ocr/<slug>/mensagens.txt` com `neo_read_file`:
- `[HORA] REMETENTE: mensagem`, na ordem da conversa; linhas recuadas logo abaixo
  são a continuação da mesma mensagem.
- `Você` é quem gravou a tela (balão à direita). O outro lado vem com o nome do
  cabeçalho da conversa (`contato` no resultado). Se o usuário disse o nome dele,
  use-o no lugar de `Você`.
- `== quarta-feira ==` separa os dias como estavam na tela. Não invente a data do
  calendário; só a escreva se o usuário ou o nome do arquivo disser.
- `[??:??]` = horário ilegível: deixe assim, não deduza. `[aviso]` = mensagem do
  sistema. `(respondendo a "…")` = citação. `[botões: …]` = botões de resposta.
  `[vídeo 1:13]`/`[imagem]` = anexo. `[cortada em "Ler mais"]` = o texto não
  aparecia inteiro na gravação — diga isso, não complete.
- O texto é OCR. Corrija só erro ÓBVIO de letra quando o contexto não deixa dúvida
  ("sunorte" → "suporte"); não reescreva, não resuma e não junte mensagens.
- `descartadas.txt` tem os pedaços que o OCR não conseguiu ler. Se o usuário
  perguntar por algo que não está nas mensagens, olhe ali.

### `"modo": "texto"` — slides, documento, tela sem balões
- Se o RESULT disser `"rolagem_rapida": true`, o vídeo rolou rápido demais para 1
  quadro por segundo: rode o MESMO job de novo com `--fps 2` (mesmo `--out`, mesmo
  `then`) antes de montar.
- `transcricao.txt`: linhas únicas, na ordem, cada uma com `[mm:ss]` do vídeo.
  `interface.txt` lista o que saiu como parte fixa da tela e `descartadas.txt` as
  linhas vistas num quadro só — confira se nada de conteúdo foi parar lá.
- Monte o que foi pedido. Não invente horário nem remetente; linha ilegível do OCR
  fica de fora.

### Nos dois modos
- Grave o resultado com `neo_write_file` e entregue com `file_save`. Não cole a
  transcrição inteira no chat.
- Apague os intermediários com `neo_bash` (`rm -rf` do `--out` e do vídeo em
  `/workspace/files/`): o conteúdo da conversa não fica guardado no servidor.

## Opções do script (só quando precisar)
- `--modo chat` ou `--modo texto`: força um modo (o padrão `auto` procura balões de
  conversa e, sem eles, faz OCR de texto corrido).
- `--crop X,Y,W,H`: lê só essa região, em pixels do vídeo ORIGINAL — use quando
  outra janela na tela atrapalhar.
- `--lang por+eng`: conversa com inglês.
- `--fps 2`: só no modo texto, para rolagem rápida.
- Retomável: se o job cair, rode o MESMO comando (mesmo `--out`); ele continua de
  onde parou.

## Regras
- Nunca rode ffmpeg/tesseract na mão, quadro a quadro, nem na máquina do usuário:
  2 minutos de gravação de tela a 60 fps são ~7.500 quadros para decodificar.
- Nunca rode o script com `neo_bash` em primeiro plano: é trabalho de minutos.
