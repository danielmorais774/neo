# Escolher o vídeo do YouTube antes de baixar (`neo_video_candidatos`)

Em produção desde 17/11/2026. Código em `core/video/` (`escolha.py` é a nota, `tools.py` a tool), números em `[video]` no `config/neo.toml`.

## Por que existe

Na session-94621cc8, "baixa o vídeo do YouTube 'Já acabou, Jéssica?'" baixou o 1º resultado do `web_search`, uma paródia do Isaac do VINE. A "versão original" veio pelo `yt-dlp "ytsearch1:…"` do classificador, que baixa o 1º da busca do YouTube. Ninguém comparava candidatos. O Daniel decidiu: escolher pelos dados do YouTube e só comparar prints quando os dados não decidirem.

## Os três degraus

1. **Dados (sempre).** O sandbox roda `yt-dlp --flat-playlist ytsearch8:<consulta>` (~2 s). A busca não é barrada pelo IP de datacenter, só o vídeo. Nota de cada empresa:
   - `peso_titulo` × fração dos termos do pedido no título **ou no canal**. Termo de 4+ letras vale dentro de outra palavra: "marco" casa "marcos" e "marcoveioofc".
   - `peso_frase` se a frase inteira está no título.
   - `peso_views` × log das views relativo ao mais visto.
   - `peso_duracao`: meme (`curto`) com até `curto_max_s` vale nota cheia.
   - Menos `peso_penalidade` se título/canal tem termo de cópia (`penalidades`: reação, zoeira, dark, nova versão, como está, reportagem, lyric…) que o pedido não tem. Nesse caso o vídeo também não concorre.
   - Campo faltando (views/canal `NA` na busca) conta neutro, 0,5.
   - Se o 1º elegível tem `margem` sobre o 2º, é ele.
2. **Quadros (empate).** Para até `comparar_max` empatados, o sandbox baixa a capa e `hq1/hq2/hq3.jpg` (quadros automáticos do YouTube a ~25/50/75 %, que também passam pelo bloqueio). Monta com PIL uma grade 2×2 com a letra no canto. O modelo de visão (`browser_local_vision_model`) recebe as grades e os dados e devolve `{escolhido, mesma_cena, certeza, motivo}`. Vale só certeza alta/média. Se mais de um mostra a mesma cena, fica o de nota maior.
3. **Perguntar.** A tool devolve `[[IMAGE:https://i.ytimg.com/vi/<id>/hqdefault.jpg|A · título · canal · 0:20 · 393 mil views]]` e o cérebro pergunta qual.

O resultado sempre traz `proximo_passo`: baixar na máquina do Daniel (`run_command yt-dlp … --print after_move:filepath`, porque o sandbox é barrado) e entregar com `[[FILE:…]]`.

## Trava de download às cegas

- **Loop do cérebro** (`_escolha_de_video_pendente`): `run_command`/`neo_bash` com yt-dlp que baixa do YouTube não roda na 1ª vez quando é `ytsearch`, ou quando o id não foi escolhido pela tool nem está na mensagem do Daniel. Volta `_AVISO_VIDEO_SEM_ESCOLHA`. A 2ª chamada igual passa. Listagem (`--flat-playlist`, `-F`, `--skip-download`) não é travada.
- **Classificador**: CLIENT_TOOL com `ytsearch` volta ao cérebro (`_BUSCA_CEGA_DE_VIDEO`, na guarda de URL inventada).
- Os ids escolhidos (ou mostrados para escolha) ficam liberados por `escolha_validade_s` (memória do processo).

## Medido nas buscas reais de 17/11

- "taca-lê pau nesse carrinho Marco véio" (meme): decide pelos dados no `eeQwPExFNRU`, canal marcoveioofc, 8,7 mi. "Nova versão" e "como está 8 anos após" não concorrem.
- "Já acabou, Jéssica?": 4 empatados (0,99 / 0,98 / 0,90 / 0,87) vão para os quadros. Dark, zueira e a reportagem da BBC não concorrem. A grade do `bOY3qic5_t4` (o que o Daniel aceitou) mostra uma edição "Thug Life" no último quadro. A do `Vdj-jhayGz0` ("VIDEO OFICIAL COMPLETO") é a gravação crua.
- "Yeah boy": meme e música da Kelsea Ballerini empatam (views `NA`). Pergunta, que é o certo para pedido ambíguo.

Testes: `tests/test_video_candidatos.py` e `tests/router/test_net_fetch_wrong_rail.py`.
