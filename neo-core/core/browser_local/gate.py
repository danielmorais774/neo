"""O portão do navegador local: só age na tela do usuário se ele PEDIU.

Regra do plano (docs/browser-local-plan.md §5.2), em duas partes que precisam
valer JUNTAS — mesma forma da guarda de arquivo (``_file_path_is_grounded``):

* **âncora de posse** — "meu/minha/aqui/nesta máquina/na minha tela/local";
* **afordância de navegador** — "navegador/chrome/brave/aba/janela", ou um verbo
  de acompanhamento ("quero ver/acompanhar") junto de uma dessas palavras.

Só a âncora não basta ("abre meu email" é tarefa comum, não pedido de tela), e
só a afordância também não ("procura no navegador" continua sendo o headless do
servidor). O custo de errar para o lado do local é o Neo tomar conta do Chrome
do usuário sem ele pedir — por isso o default é sempre o servidor.

Duas afordâncias, porém, valem SOZINHAS, e pelo mesmo motivo: elas não existem
no servidor, então não são preferência — são o alvo não estar lá.

* **extensão de navegador** (``_EXTENSION``) — o headless não tem uma;
* **a máquina dele, nomeada** (``_MACHINE_OF_USER``) — "no meu macbook", "nesse
  pc". O Chromium da VPS não é a máquina do usuário: não tem a tela dele nem as
  caixas de som dele.

A segunda nasceu de uma medição, não de um palpite: em 33 decisões no log de
produção o portão rebaixou 10 vezes, herdou 23 e **promoveu zero**. "coloca para
tocar no meu macbook" tinha a âncora e nenhuma palavra de navegador, ia para o
servidor e o pedido virava impossível — ninguém ouve o áudio de um headless.
"""

from __future__ import annotations

import re
import unicodedata

# ── vocabulário ─────────────────────────────────────────────────────────────

# Posse / presença física. "local" entra porque "navegador local" é como o
# usuário nomeia a coisa.
_ANCHORS = (
    "meu",
    "minha",
    "aqui",
    "local",
    "nesta maquina",
    "nessa maquina",
    "nesta tela",
    "na minha frente",
    "do meu lado",
    "comigo",
)

_BROWSER_WORDS = (
    "navegador",
    "browser",
    "chrome",
    "brave",
    "edge",
    "firefox",
    "safari",
    "aba",
    "abas",
    "janela do navegador",
)

# Verbo de acompanhamento: "quero ver ele fazendo", "quero acompanhar".
_WATCH_WORDS = (
    "acompanhar",
    "acompanhando",
    "ver voce",
    "ver vc",
    "te ver",
    "assistir",
    "ao vivo",
    "em tempo real",
    "na minha tela",
    "eu vendo",
)

# Frases que já dizem tudo sozinhas — atalho para não depender da combinação.
_EXPLICIT = (
    "navegador local",
    "browser local",
    # Como o usuário de fato escreve (sessão d01cb5ab: "abra o painel do simples
    # nacional no meu local"). Sozinha "local" é ambígua demais — por isso entra
    # só a forma possessiva colada.
    "no meu local",
    "meu local",
    "aqui no local",
    "no meu navegador",
    "meu navegador",
    "no meu chrome",
    "no meu brave",
    "no meu browser",
    "na minha tela",
    "no navegador daqui",
    "no navegador desta maquina",
)


# Palavras que aceitam erro de digitação. Ficam de fora de propósito:
# "aba"/"abas"/"edge" (3-4 letras: quase tudo fica "parecido") e "brave", que a
# uma edição de distância vira "grave", "bravo", "brava" e "breve" — palavras
# comuns que, com um "meu" na frase, ligariam o navegador do usuário à toa.
_FUZZY_BROWSER_WORDS = (
    "navegador",
    "navegadores",
    "browser",
    "chrome",
    "firefox",
    "safari",
)

# A MÁQUINA do usuário, nomeada por ele. Vale sozinha (com o possessivo), pela
# mesma lógica de `_EXTENSION`: o browser do servidor não fica na máquina dele,
# então "faz isso no meu macbook" exclui o headless por construção — não é
# preferência, é o alvo não existir lá.
#
# É este o buraco medido: no log de produção o portão decidiu 33 vezes e
# PROMOVEU zero. "coloca para tocar no meu macbook" e "consegue por um musica
# nesse pc" tinham a âncora de posse mas nenhuma palavra de navegador, então
# caíam na regra de duas partes e iam para o servidor — onde o pedido é
# impossível: ninguém ouve o áudio de um Chromium headless numa VPS (job
# 3a0ed590, "Tocar rock no MacBook", falhou no servidor).
#
# O possessivo é obrigatório: "o pc está lento" é conversa, "no meu pc" é lugar.
# Até duas palavrinhas de ligação cobrem as formas reais ("no meu novo macbook",
# "nessa minha maquina").
_MACHINE_OF_USER = re.compile(
    r"\b(?:meu|minha|nesse|neste|nessa|nesta|desse|deste)\b(?:\s+\w{1,8}){0,2}\s+"
    r"\b(?:macbook|mac|notebook|laptop|computador|pc|maquina|desktop)\b"
)

# ...menos quando "máquina" é outro eletrodoméstico. Mesmo cuidado do sentido
# duplo de "extensão": "minha máquina de lavar quebrou" não é pedido de tela.
# Lista fechada, como lá — o que não está aqui continua sendo o computador dele.
_MACHINE_OTHER_SENSE = re.compile(
    r"\bmaquina\s+de\s+"
    r"(?:lavar|secar|costura|costurar|cafe|escrever|gelo|solda|soldar|cortar|"
    r"musculacao|exercicio|passar|moer|fazer)\b"
)

# "extensão" = extensão DO NAVEGADOR. Vale sozinha, sem âncora de posse: não
# existe extensão no browser headless do servidor, então quem pede "usa a
# extensão" só pode estar falando do navegador dele. Medido nas 1658 mensagens
# reais do banco: as 7 ocorrências de "extens*" são todas do navegador, nenhuma
# de extensão de arquivo.
_EXTENSION = re.compile(r"\bextens(?:ao|oes|ion|ions)\b")

# ...menos quando é o OUTRO sentido da mesma palavra ("qual a extensão desse
# arquivo", "salva com extensão .csv"). Não apareceu no corpus, mas é a frase
# natural para pedir formato de arquivo — e aí o local seria o alvo errado.
# Até duas palavrinhas de ligação entre a palavra e o substantivo cobrem as
# formas reais: "extensão do arquivo", "extensão desse arquivo", "extensão
# daquele vídeo". Sem isso "qual a extensão desse arquivo" abria o navegador.
_EXTENSION_FILE_SENSE = re.compile(
    r"\bextens(?:ao|oes|ion|ions)\b(?:\s+\w{1,8}){0,2}\s+"
    r"(?:arquivo|arquivos|ficheiro|imagem|imagens|video|videos|audio|foto|fotos|pasta)\b"
    r"|\bextens(?:ao|oes|ion|ions)\b\s*\.[a-z0-9]{1,5}\b"
)


# ── tocar mídia: o servidor não tem como ────────────────────────────────────
#
# Terceira afordância que vale SOZINHA, pelo mesmo motivo das outras duas
# (`_EXTENSION`, `_MACHINE_OF_USER`): reproduzir áudio/vídeo num Chromium
# headless de VPS não é uma versão pior do pedido — é o pedido não existir lá.
# Ninguém ouve o som e ninguém vê a tela. Então só há dois destinos possíveis: o
# navegador DELE, ou nenhum (o handoff de música).
#
# Medido, não chutado — três jobs de servidor que nasceram mortos:
# 3a0ed590 ("Tocar rock no MacBook"), e o 1998e5fb da sessão 0939a295 ("Tocar
# música do mix do Daniel"), que rodou 5min38 no headless e morreu no "Sign in
# to confirm you're not a bot" do YouTube — enquanto o verificador de música,
# cego para essa atuação, abria uma SEGUNDA execução no desktop.
#
# Diferente das outras duas, esta é lida também na TAREFA que o cérebro
# escreveu, não só na frase do usuário: quem diz "dar play no vídeo" é ele.

# Verbos de reprodução. Só palavras SOLTAS aqui: `_has_word` casa expressão de
# várias palavras por substring, e "dar play" nessa lista aceitava "o link **da
# play**list" como ordem de tocar. As formas com "play" moram em
# `_EXPLICIT_PLAY`, que exige fronteira de palavra de verdade.
#
# "tocar" é o único ambíguo (tocar = encostar), e o sentido de encostar vem
# sempre com preposição — "não toque NO botão", "sem tocar EM nada" —, que é o
# que `_TOUCH_SENSE` remove.
_PLAYBACK_VERBS = (
    "tocar",
    "toca",
    "toque",
    "toquem",
    "tocando",
    "reproduzir",
    "reproduza",
    "reproduz",
    "reproduzindo",
)

# ...e o que se encosta é um ELEMENTO de página, lista fechada. Casar só pela
# preposição comia o sentido certo: "coloca para tocar NO meu macbook" é play,
# não é encostar em nada.
_TOUCH_SENSE = re.compile(
    r"\bto(?:car|ca|cam|cando|que|quem)\s+(?:em|n?[oa]s?|num|numa)\s+(?:\w{1,6}\s+)?"
    r"(?:botao|botoes|link|links|campo|campos|menu|menus|nada|item|itens|"
    r"checkbox|opcao|opcoes|celula|celulas|icone|icones|caixa|aba|abas|"
    r"formulario|carrinho)\b"
)

# Play escrito por extenso sobrevive à frase que TAMBÉM manda não encostar em
# nada ("dar play no vídeo e não toque no botão de anúncio") — tarefa de browser
# é comprida e cabem as duas ordens. Precisa de fronteira de palavra de verdade:
# `_has_word` casa expressão por substring, e "da **play**list" acionava esta
# válvula sozinha.
_EXPLICIT_PLAY = re.compile(
    r"\b(?:dar|da|de|deu|bota|poe|coloca|coloque)\s+play\b|\bplay\s+n[oa]s?\b"
)

# "a música QUE TOCA no comercial" descreve uma faixa, não manda tocar nada — é
# oração relativa, não ordem. Sai do texto ANTES da procura pelo verbo, para não
# valer como o único sinal de reprodução da frase; se sobrar outro verbo ("toca
# a musica que toca no filme"), o pedido continua sendo play.
_RELATIVA_TOCA = re.compile(r"\bque\s+to(?:ca|cam|cava|cavam|cou|caram)\b")

# O verbo sozinho não basta: "toca" precisa de algo tocável. Lista fechada de
# mídia — o que não está aqui continua indo para o servidor.
_MEDIA_WORDS = (
    "musica",
    "musicas",
    "som",
    "playlist",
    "playlists",
    "mix",
    "album",
    "albuns",
    "faixa",
    "faixas",
    "cancao",
    "video",
    "videos",
    "clipe",
    "audio",
    "podcast",
    "radio",
    "rock",
    "mp3",
    "youtube",
    "spotify",
    "deezer",
    "soundcloud",
    "tidal",
)


def _normalize(text: str) -> str:
    """Minúsculas, sem acento, espaços colapsados — o usuário escreve torto."""
    lowered = (text or "").lower()
    stripped = unicodedata.normalize("NFKD", lowered)
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip()


def _has_word(haystack: str, needles: tuple[str, ...]) -> bool:
    """Casamento por PALAVRA (não substring): 'abas' não pode casar em 'sabas'."""
    for needle in needles:
        if " " in needle:
            if needle in haystack:
                return True
            continue
        if re.search(rf"\b{re.escape(needle)}\b", haystack):
            return True
    return False


def _edit_distance(a: str, b: str, *, cap: int = 3) -> int:
    """Damerau-Levenshtein (com transposição), abortando acima de ``cap``.

    Transposição conta como UMA edição porque é justamente o erro de quem digita
    rápido: "nevagador" por "navegador", "chorme" por "chrome". Tratá-la como
    duas substituições (que é o que a distância clássica faz) reprovava
    exatamente os casos que esta guarda precisa pegar.
    """
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    prev2: list[int] = []
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[lb]


def _has_browser_word(haystack: str) -> bool:
    """Afordância de navegador, tolerante a erro de digitação.

    O casamento exato custou uma sessão inteira (d3be77e1): o usuário escreveu
    "pelo meu **nevagador** local ... via extensao", o portão não viu navegador
    nenhum, rebaixou para o headless do servidor e o Neo foi mexer num browser
    que o usuário não podia ver — bem no pedido em que ele foi mais explícito.
    Quem digita rápido troca letra; a guarda não pode depender de ortografia.

    A tolerância vale só para as palavras LONGAS (``_FUZZY_BROWSER_WORDS``) e
    exige a âncora de posse do mesmo jeito — nada aqui afrouxa a regra de duas
    partes, só o soletrar.
    """
    if _has_word(haystack, _BROWSER_WORDS):
        return True
    for token in re.findall(r"[a-z]{5,}", haystack):
        for word in _FUZZY_BROWSER_WORDS:
            # Palavra curta erra menos antes de virar outra coisa; palavra longa
            # aguenta duas edições sem ambiguidade. "chorme"→"chrome" é uma
            # transposição adjacente (distância 1); "nevagador"→"navegador" NÃO
            # é — o 'a' e o 'e' trocados estão separados pelo 'v', o que dá duas
            # substituições. É por isso que 9 letras precisam de orçamento 2.
            budget = 1 if len(word) <= 7 else 2
            if abs(len(token) - len(word)) <= budget and _edit_distance(token, word) <= budget:
                return True
    return False


def local_browser_requested(user_message: str) -> bool:
    """O usuário pediu o navegador DELE, com todas as letras, neste turno?"""
    text = _normalize(user_message)
    if not text:
        return False
    if any(phrase in text for phrase in _EXPLICIT):
        return True
    # Extensão de navegador vale sozinha: o headless do servidor não tem uma.
    if _EXTENSION.search(text) and not _EXTENSION_FILE_SENSE.search(text):
        return True
    # A máquina DELE, nomeada: idem — o servidor não é ela.
    if _MACHINE_OF_USER.search(text) and not _MACHINE_OTHER_SENSE.search(text):
        return True
    has_browser = _has_browser_word(text)
    if has_browser and _has_word(text, _ANCHORS):
        return True
    # "quero acompanhar no chrome" / "quero ver ao vivo a aba"
    if has_browser and _has_word(text, _WATCH_WORDS):
        return True
    return False


def media_playback_requested(text: str) -> bool:
    """O texto pede para REPRODUZIR áudio/vídeo (e não só abrir uma página)?

    Vale sozinha, sem âncora de posse: play no headless do servidor é sempre
    inútil (ver o bloco de vocabulário acima). Exige verbo de reprodução E uma
    palavra de mídia, porque o custo de errar para o lado do local continua
    sendo o Neo tomar conta do navegador do usuário sem ele pedir.
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    sem_relativa = _RELATIVA_TOCA.sub(" ", normalized)
    if not (
        _has_word(sem_relativa, _PLAYBACK_VERBS) or _EXPLICIT_PLAY.search(sem_relativa)
    ):
        return False
    # "não toque no botão de comprar" é instrução de navegação, não de play.
    if _TOUCH_SENSE.search(normalized) and not _EXPLICIT_PLAY.search(normalized):
        return False
    return _has_word(normalized, _MEDIA_WORDS)


def explain(user_message: str) -> str:
    """Motivo legível da decisão — vai para o log, não para o usuário."""
    return "pediu local" if local_browser_requested(user_message) else "não pediu local"
