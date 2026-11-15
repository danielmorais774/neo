"""Intent Router — classifies user messages into execution paths.

Routes to one of:
  - DIRECT_RESPONSE: conversational / general knowledge (no tools)
  - SERVER_TOOL:     needs a tool running on the neo-core host (e.g. weather API)
  - CLIENT_TOOL:     needs a tool running on the user's desktop (shell, filesystem)
"""

import json
import logging
import re
import unicodedata
from typing import Any

from core.llm.router import LLMRouter
from core.memory.recall_triggers import is_conversation_recall_request
from core.pks.training_envelope import TrainingEnvelopeParser
from core.tools.registry import ToolRegistry
from shared import (
    IntentType,
    Message,
    MessageRole,
    PipelineStepSpec,
    RoutingDecision,
)

logger = logging.getLogger(__name__)

# Shared parser so bare instances (object.__new__) and the fallback path both work.
_TRAINING_PARSER = TrainingEnvelopeParser()

def _written_text(command: str) -> str:
    """O texto que um comando de shell grava (corpo do heredoc ou string de echo/printf)."""
    heredoc = re.search(r"<<-?\s*['\"]?([A-Za-z_]+)['\"]?\n(.*?)\n\1\b", command or "", re.DOTALL)
    if heredoc:
        return heredoc.group(2)
    literal = re.search(r"\b(?:echo|printf)\s+(['\"])(.*?)\1", command or "", re.DOTALL)
    return literal.group(2) if literal else command or ""


def _text_is_dictated(content: str, user_message: str) -> bool:
    """O texto a gravar está LITERAL na mensagem do usuário?

    Espaços colapsados e caixa ignorada — ditar "grava 'oi mundo' em notas.txt"
    passa; um arquivo inteiro que o classificador redigiu a partir de "atualiza o
    .md" não (ver ``_reject_classifier_file_authoring``).
    """
    alvo = " ".join(str(content or "").split()).casefold()
    if not alvo:
        return True
    return alvo in " ".join(str(user_message or "").split()).casefold()


# Conciliação de arquivos (docs/file-links-plan.md). Três furos do caminho
# CLIENT_TOOL medidos na sessão f33632b4:
#  - "adiciona ao arquivo final e coloque em <pasta>" virou UMA entrega direta, com
#    o file_id do PDF que tinha subido por último — o "adiciona" sumiu e o PDF foi
#    gravado como .md. Mudar conteúdo + entregar é trabalho do cérebro;
#  - "baixe ele para mim e coloque lá" virou um heredoc python que ESCREVIA no Mac um
#    markdown com texto inventado pelo classificador (só não gravou porque citava
#    /workspace). Shell que grava texto não ditado é a mesma falha do write_file;
#  - "me mostra o botão de download" virou um plano de 3 run_command.
_CONTENT_CHANGE_RE = re.compile(
    r"\b(adicion[ae]|acrescent[ae]|inclu[ai]|atualiz[ae]|edit[ae]|corrij[ae]|corrig[ae]|"
    r"alter[ae]|mud[ae]|reescrev[ae]|insir[ae]|inser[ae]|complement[ae]|junt[ae])\b",
    re.IGNORECASE,
)
_DOWNLOAD_BUTTON_RE = re.compile(
    r"\bbot[aã]o\b[^.?!\n]{0,40}\b(download|baixar)\b|\b(link|bot[aã]o)\s+(de|pra|para)\s+(download|baixar)\b",
    re.IGNORECASE,
)
_SHELL_WRITES_FILE_RE = re.compile(
    r"<<-?\s*['\"]?[A-Za-z_]+|open\([^)]*,\s*['\"][wa]|\btee\b|(?<![0-9&])>>?\s*['\"]?[~/]"
)


# Deterministic guard: explicit "remember/register in memory" directives must
# always be a DIRECT_RESPONSE so the PKS write pipeline captures them. Without
# this, a weak classifier maps "registra/lembra/salva" → write_file (CLIENT_TOOL)
# and the request hangs waiting on the desktop. Patterns run on accent-stripped,
# lowercased text and are scoped to avoid catching plain file operations.
_MEMORY_DIRECTIVE_PATTERNS = [
    re.compile(p)
    for p in (
        r"\bpks\b",
        r"\bmemoriz",                                   # memoriza / memorize / memorização
        r"\bna (minha )?memoria\b",                     # salva/guarda ... na memoria
        r"\blembr\w*\s+(que|de que|do que|disso|disto)",  # lembra que / lembre disso
        r"\bregistr\w*\s+(no pks|na memoria|que )",     # registra no pks / registra que
        r"\banot[ae]\w*\s+(que|isso|ai|aqui)",          # anota que / anote isso
        r"\bnao esquec",                                # nao esqueca / não esqueça
        r"\b(remember|memorize|take note|keep in mind)\b",
        # Referências INEQUÍVOCAS de memória/anotações (não casam save de arquivo,
        # que não fala em "anotações"/"ideia"/"lembrar"). Fecham o gap da Fase 2:
        # "salva essa ideia", "coloca isso nas nossas anotações", "pra eu lembrar".
        r"\bnas?\s+(minhas?\s+|nossas?\s+|suas?\s+)?(anotac\w+|notas)\b",  # nas (nossas) anotações
        r"\b(salv|guard|anot|registr)\w*\s+(a |o |essa |esta |esse |este |uma |um )?(ideia|dica|receita|informacao|info|observacao)\b",  # salva essa ideia/dica
        r"\b(pra|para)\s+(eu\s+)?lembrar\b",            # pra lembrar / para eu lembrar
        # "grava 01310-100 como meu CEP", "salva isso como minha senha do wifi" —
        # verbo de gravação + âncora POSSESSIVA. Faltava: `grava` era o verbo mais
        # natural em pt-BR para a diretiva e não casava com NADA aqui; a frase só
        # entrava quando trazia a palavra "pks", que casava por acidente — e era a
        # mesma palavra que autocarregava a skill `postgres-neo` (SELECT) e calava a
        # escrita. Tirado o acidente (session-be7659d2), o buraco apareceu: 4 pedidos,
        # `confirmed_save=False`, planner devolvendo `{"operations": []}`.
        # Medido nas 2387 mensagens de usuário em prod: `\bgrav` solto pega 9, sendo 7
        # falsos ("gravação de tela", "mais grave", "ele gravou"); `grava` + âncora de
        # conteúdo (que/isso/aqui) pega ZERO, porque a frase-alvo não tem nenhuma. O
        # que qualifica é o "como meu/minha" — a afirmação de que o dado é DELE, que é
        # exatamente o que vira fato de PKS: 2 casos, 0 falso positivo.
        # A guarda de extensão separa a entrega de arquivo ("salva o arquivo como meu
        # relatorio.pdf"), que é file_save e não memória.
        r"\b(salv|grav|anot|registr|guard)\w*\b"
        r"(?![^.!?]*\.(pdf|docx?|xlsx?|pptx?|png|jpe?g|gif|svg|md|txt|csv|tsv|json|zip|mp[34]|mov)\b)"
        r".{0,40}\bcomo\s+(o\s+|a\s+)?(meu|minha|meus|minhas)\b",
    )
]


# Device questions are ambiguous: "dispositivos conectados agora" is a live
# websocket query (SERVER_TOOL/list_connected_devices), but "dispositivos que
# eu tenho/cadastrados/que você conhece" is a PKS-knowledge query that must go
# to DIRECT_RESPONSE so the PKS context (which already loads device entities by
# type) is injected. The classifier conflated the two (session e245da98), so we
# disambiguate deterministically, mirroring the memory-directive guard above.
#
# Explicit device-listing nouns only — broad category words (computador/celular)
# are left out to avoid hijacking action requests ("instala no meu notebook").
_DEVICE_NOUNS = (
    "dispositivo", "dispositivos", "device", "devices",
    "aparelho", "aparelhos", "equipamento", "equipamentos",
)
# Live-connection qualifiers → keep on the SERVER_TOOL/list_connected_devices path.
_DEVICE_LIVE_TERMS = (
    "conectado", "conectados", "conectada", "conectadas",
    "conectar", "conectando", "conexao", "online", "on line",
    "tempo real", "agora", "neste momento", "ligado", "ligados",
)
# Knowledge qualifiers → the user wants what Neo *knows* (PKS), not live links.
_DEVICE_KNOWLEDGE_TERMS = (
    "conhecimento", "conhece", "conheco", "conhecid",
    "cadastr", "registr", "sabe sobre", "que voce sabe",
)
# Back-reference pronouns for the correction turn ("quero os que você conhece").
_BACKREF_PATTERN = re.compile(r"\b(os que|as que|aqueles|aquelas|esses|essas)\b")

# Caminho de arquivo ESPECÍFICO (com extensão) dentro de um comando de shell — usado
# pelo grounding guard de CLIENT_TOOL (ver _ungrounded_path_literals). Exige extensão
# de propósito: comandos exploratórios (`ls ~/Desktop`, `df -h`, `find . -iname
# '.env*'`) não citam um arquivo específico e não devem disparar o guard.
_PATH_WITH_EXT_RE = re.compile(r"(?:~|/)[^\s'\"|;&<>]*\.[A-Za-z0-9]{1,6}(?=[\s'\"|;&<>]|$)")

# Referential questions point back to an earlier turn via a demonstrative+
# preposition contraction ("como tu chegou NESSA conta de 22?", "de onde veio
# ISSO?"). The history may be full of HA/tool commands, but the user is *asking
# about* a past action, not issuing a new one — so this must stay DIRECT_RESPONSE.
_REFERENTIAL_DEMONSTRATIVES = (
    "nessa", "nesse", "nesses", "nessas",
    "desse", "dessa", "desses", "dessas", "disso", "disto",
    "neste", "nesta", "nisto", "nisso",
    "naquele", "naquela", "naquilo",
    "porque disso", "por que disso", "pra que isso",
    "de onde veio", "como voce chegou", "como tu chegou", "como chegou",
)
_INTERROGATIVE_STARTS = (
    "como", "porque", "por que", "pq", "de onde", "quando",
    "onde", "qual", "quanto", "quantos", "quanta", "quantas", "o que", "oq",
)

# Foreign wake-name addressing: when the user addresses Neo by ANOTHER assistant's
# name ("alexa, liga a luz", "ok google...", "chatgpt, faz X"), the turn MUST go to
# the cognitive core — never fast-path to a skill. Neo's persona/behavior rules
# (e.g. the trained "get offended when called Alexa, refuse and mock") live in the
# PKS and are applied ONLY on the DIRECT_RESPONSE path (retrieve_system_context).
# Without this, "alexia liga a luz" reads as a textbook imperative HA command
# (confidence 1.0) → routed straight to the home_assistant skill, which never sees
# the persona, so the trained reaction can never fire (session 2173af2b). Anchored
# at the START of the message (a vocative), accent-stripped/lowercased. Fails safe:
# sending an unrelated turn to the brain is always harmless (it's DIRECT_RESPONSE).
_FOREIGN_ASSISTANT_NAMES = (
    "amazon alexa", "alexa", "alexia",
    "ok google", "hey google", "google assistente", "assistente do google",
    "hey siri", "e ai siri", "oi siri", "siri",
    "cortana", "bixby",
    "chatgpt", "chat gpt", "gpt",
    "gemini", "copilot", "claude",
)
_FOREIGN_ASSISTANT_ADDRESS = re.compile(
    r"^(?:" + "|".join(re.escape(n) for n in _FOREIGN_ASSISTANT_NAMES) + r")\b"
)

# Skill requests that are CONDITIONAL on the user's own habits/preferences — or
# that ASK about them — must go to the cognitive core (DIRECT_RESPONSE) so it can
# read the PKS and decide whether to hand off to a skill, instead of the cheap
# router firing the skill blindly. Without this, "se eu costumo, pode ligar" was
# misrouted straight to HOME_ASSISTANT, skipping the brain + PKS (session 6bb1d340).
# Patterns run on accent-stripped, lowercased, whitespace-collapsed text.
_HABIT_SELF_REF = (
    "eu costumo", "costumo", "eu uso", "eu gosto", "gosto de", "eu prefiro",
    "prefiro", "eu sempre", "eu geralmente", "geralmente eu", "normalmente eu",
    "eu costumava", "costumava",
)
_CONDITIONAL_MARKERS = (
    "se eu", "se voce achar", "se voce acha", "se fizer sentido", "se valer",
    "caso eu", "se ja", "se for o caso",
)
# Question explicitly about the user's own preference/habit ("como eu gosto…",
# "qual eu costumo…"). Excludes bland how-to/state questions ("qual a temperatura").
_PREFERENCE_QUESTION = re.compile(
    r"\b(como|o que|qual|quando|sera que)\b.*"
    r"\b(gosto|costumo|prefiro|gostaria|costumava)\b"
)

# Music requests must go to the COGNITIVE core, never fast-path to a skill: the
# brain reads the PKS (taste + behavioral rules like "refuse sertanejo and tease")
# and decides whether to hand off to the `music` skill. The cheap router has no PKS
# access, so a confident imperative like "toca um sertanejo" would otherwise fire
# the skill blindly. This guard sets skill_hint="music" so the cognitive core knows
# to offer the strict music handoff. See neo-music-feature.
_MUSIC_PLAY_VERBS = (
    "toca", "tocar", "toque", "coloca", "colocar", "bota", "botar",
    "poe", "poem", "reproduz", "reproduzir", "ouvir", "escutar",
)
# Media-control verbs: pause/resume/next/previous/volume/mute. Routed to the music
# skill (same handoff path as play) so "pausa a música" never falls to run_command
# and improvises a terminal command. See neo-music-feature.
_MUSIC_CONTROL_VERBS = (
    "pausa", "pausar", "pause", "para", "parar", "pare",
    "retoma", "retomar", "continua", "continuar", "continue", "despausa", "despausar",
    "play",  # "dá play", "play a música" — resume/start; English verb common in PT-BR.
    "proxima", "proximo", "pula", "pular", "avanca", "avancar", "passa", "passar",
    "anterior", "volta", "voltar", "retrocede",
    "aumenta", "aumentar", "abaixa", "abaixar", "diminui", "diminuir", "baixa", "baixar",
    "muta", "mutar", "mute", "desmuta", "desmutar", "silencia", "silenciar",
)
# Unambiguous media-control verbs. These refer to the media ALREADY playing, so
# they route to the music skill even WITHOUT a media noun — anaphoric follow-ups
# like "pausa aí de novo", "dá um play nela", "pula essa" otherwise fall to
# run_command and improvise an AppleScript (session-…, osascript key code 179).
# Music routes to the COGNITIVE core (not the skill directly), so a rare false
# positive (e.g. "pula essa pergunta" in a quiz) is safe: the brain decides and
# can emit [[NO_MUSIC]]. Ambiguous verbs (para/passa/volta/continua/aumenta/…)
# stay in _MUSIC_CONTROL_VERBS and still require a noun. See neo-music-feature.
_MUSIC_STRONG_CONTROL_VERBS = (
    "pausa", "pausar", "despausa", "despausar",
    "retoma", "retomar", "pula", "pular", "play",
    # Mudo: em pt-BR "muta/desmuta" só existe para som de mídia, então não
    # precisa de substantivo. Sem isto, "consegue mutar essa aba do super
    # xandão" caía no classificador, que improvisou `osascript` para o Google
    # Chrome (o usuário usa Edge) e errou a sintaxe 5 vezes seguidas —
    # enquanto a extensão reportava a aba, com tabId, no mesmo turno.
    "muta", "mutar", "mute", "desmuta", "desmutar",
)
# Unambiguous "resume what's playing" phrases. These route to the music skill even
# WITHOUT a media noun (so "dá play de novo" / "volta a tocar" don't fall to
# run_command and improvise an AppleScript). Matched as substrings on accent-
# stripped, whitespace-collapsed text. See neo-music-feature.
_MUSIC_RESUME_PHRASES = (
    "da play", "dar play", "de play", "da o play", "de o play", "play de novo",
    "volta a tocar", "voltar a tocar", "volta a musica", "volta a tocar a musica",
    "continua a musica", "continua tocando", "continuar tocando",
    "poe pra tocar de novo", "bota pra tocar de novo", "manda ver de novo",
)
_MUSIC_NOUNS = (
    "musica", "musicas", "som", "playlist", "album", "albuns", "banda",
    "cantor", "cantora", "sertanejo", "pagode", "rock", "funk", "mpb", "rap",
    "samba", "forro", "reggae", "jazz", "blues", "trap", "lofi", "gospel",
    "eletronica", "axe", "piseiro", "arrocha", "bossa", "hip hop",
    "volume", "video",
    # A aba/guia É o objeto da mídia quando ele fala do que está tocando
    # ("abaixa o volume dessa aba", "silencia essa guia"). Já eram substantivos
    # do guard de STATUS (_MEDIA_TAB_NOUNS); faltava valerem para o CONTROLE.
    "aba", "abas", "guia", "guias",
)
_MUSIC_SERVICES = ("spotify", "youtube", "deezer", "soundcloud", "yt music")

# ── verbo e objeto de mídia na MESMA frase (_music_object_near_verb) ─────────
_MUSIC_SENTENCE = re.compile(r"[.!?;:\n]+")
_MUSIC_WORD = re.compile(r"[a-z0-9]+")
_MUSIC_ANY_VERB = frozenset(_MUSIC_PLAY_VERBS) | frozenset(_MUSIC_CONTROL_VERBS)
# Quantas palavras antes/depois do verbo o objeto pode estar: "abaixa um pouco o
# volume", "volta pro comeco do video", "a musica, para ela".
_MUSIC_NOUN_AFTER = 6
_MUSIC_NOUN_BEFORE = 3
# "para avaliar", "para ver": preposição antes de infinitivo, não o verbo parar.
_MUSIC_INFINITIVE = re.compile(r"^(ir|[a-z]+(ar|er|ir|or))$")
_MUSIC_TAB_NOUNS = ("aba", "abas", "guia", "guias")
_MUSIC_OBJECT_NOUNS = tuple(n for n in _MUSIC_NOUNS if n not in _MUSIC_TAB_NOUNS)
# Verbos que só fazem sentido com SOM — os únicos que tornam a aba objeto de mídia.
_MUSIC_SOUND_VERBS = frozenset({
    "abaixa", "abaixar", "aumenta", "aumentar", "diminui", "diminuir", "baixa",
    "baixar", "silencia", "silenciar", "muta", "mutar", "mute", "desmuta", "desmutar",
})

# ── "toca <faixa/artista>" — a ordem de play que não diz a palavra "música" ──
#
# O guard acima exige verbo + (substantivo de música OU serviço). A frase mais
# comum do Daniel não tem nenhum dos dois: "toca duality do slipknot agora"
# nomeia a FAIXA. Sem casar aqui, ela caía no classificador LLM, que devolvia
# `browser_local_task` — o cérebro abria um job e dirigia o Edge passo a passo
# (~26s) em vez do bloco [[HANDOFF:music]], que é instantâneo e usa a extensão.
#
# Duas camadas, para não transformar todo "coloca X" em música:
#   1. AFORDÂNCIA — verbos que, abrindo uma ordem, só significam reproduzir
#      mídia. "coloca/bota/põe" NÃO entram: no corpus real eles colocam salário,
#      coluna de tabela, cor de lâmpada e piada.
#   2. ÂNCORA — para os polissêmicos, exige pista explícita de play ("pra
#      tocar", "tocando"), e qualquer objeto de outro domínio mata o palpite.
#
# MEDIDO contra as 1865 mensagens de usuário do banco: 12 novas frases entram,
# 12/12 são pedido de música de verdade, 0 falso positivo. As que ficam de fora
# são declarativas ("Ela não botou pra tocar, não") — não são ordem.
_MUSIC_PLAY_IMPERATIVES = ("toca", "toque", "reproduz", "reproduzir")
_MUSIC_PLAY_AMBIGUOUS = (
    "coloca", "colocar", "coloque", "bota", "botar", "bote",
    "poe", "poem", "ponha", "tocar",
)
_MUSIC_PLAY_CUES = (
    "pra tocar", "para tocar", "pra toca", "para toca", "tocando", "toca ai", "de som",
)
# Objeto de OUTRO domínio na frase → não é música, por mais que o verbo pareça.
_MUSIC_FOREIGN_OBJECTS = (
    "arquivo", "arquivos", "pasta", "codigo", "script", "texto", "documento",
    "planilha", "pdf", "foto", "imagem", "print", "luz", "lampada", "alarme",
    "despertador", "lembrete", "agenda", "email", "whatsapp", "mensagem", "grupo",
    "terminal", "comando", "tabela", "coluna", "colunas", "linha", "senha", "cofre",
    "nota", "notas", "anotacoes", "caderno", "roupa", "assunto", "salario", "piada",
    "alternativa", "celular",
)
# "toca no assunto", "toca na porta": o verbo forte pede objeto DIRETO; com
# preposição ele deixa de ser ordem de tocar mídia.
_MUSIC_PLAY_PREPOSITION = re.compile(r"^\w+\s+(no|na|nos|nas|em)\b")

# A RECLAMAÇÃO de que o controle não pegou ("nao mutou ela", "não tocou"). É o
# pior momento para errar o roteamento: o usuário já está irritado porque a ação
# anterior falhou, e a frase não tem verbo no imperativo nem substantivo de
# música — na sessão 7687b737 ela foi parar em `device_control` e virou agente de
# navegador clicando na tela. Exige negação + verbo de controle no passado +
# frase curta, para não pegar narrativa ("o container não parou desde ontem").
# REDE DE SEGURANÇA sobre a saída do classificador. Três sessões seguidas
# (ace000b8, 7687b737, 0565907e) produziram a MESMA improvisação para controlar
# o som do navegador:
#     osascript -e 'tell application "Google Chrome" to set volume of tab 1 …'
# Nunca funciona — o Chrome não expõe volume por AppleScript (morre em syntax
# error) e a máquina usa Edge. E é a pergunta errada: quem mexe na aba é a
# extensão, pelo bloco [[HANDOFF:music]]. Enumerar as frases que levam a isso é
# perder: a rede olha o COMANDO que saiu, não a frase que entrou.
_BROWSER_AUDIO_COMMAND = re.compile(
    r"(chrome|edge|safari|firefox|brave|chromium|navegador)[\s\S]{0,120}"
    r"(volume|mute|muted|mudo|silenc|som\b|audio|pause|play\b)",
    re.I,
)
# Irmão do anterior: dirigir o navegador por AppleScript para ABRIR mídia
#     tell application "Google Chrome" to set URL of active tab … youtube.com/watch
# (sessão 0565907e, interação 6 — a resposta ao pedido "usar a extensão de
# bridge", ironicamente). Não tem palavra de volume, então a rede de áudio não
# pega; e continua sendo o app errado na máquina errada. Aqui NÃO forçamos
# música: "abre o youtube pra eu ver os comentários" é pedido legítimo de
# navegador. Só matamos o comando improvisado e devolvemos o turno ao cognitivo,
# que tem `browser_local_task` (o navegador DELE) e o bloco de música.
_BROWSER_MEDIA_URL_COMMAND = re.compile(
    r"(chrome|edge|safari|firefox|brave|chromium)[\s\S]{0,200}"
    r"(youtube\.com|youtu\.be|music\.youtube|open\.spotify)",
    re.I,
)
# Baixar da internet PELO SHELL DELE quase sempre é o trilho errado.
#
# A máquina do usuário não sabe procurar: ela só executa a URL que o modelo
# escreveu. E o modelo não tem como saber o deep link de um PDF que ele nunca
# viu — então ele inventa um plausível. Sessão d9f9417b, três turnos seguidos:
#     curl -sL "https://relatorios.exemplo.com.br/$candidato.pdf"
#     curl -sL "https://raw.githubusercontent.com/neo-core/government-plans/…"
#     curl -L -o relatorio_charlie.pdf 'https://www.planalto.gov.br/ccivil_03/…'
# Nenhum desses endereços existe. Quem SABE procurar é o cérebro (`web_search` +
# sandbox com internet), e ele tem a ponte pro disco dele
# (`file_save` → `save_generated_file`, que aceita pasta literal e a cria).
#
# A guarda é estreita: só dispara quando a URL NÃO foi dita na conversa. URL que
# o usuário deu ("baixa https://x/y.pdf pro meu desktop") continua CLIENT_TOOL —
# ali o shell dele funciona e é o caminho mais curto. O que morre é o palpite.
_NET_FETCH_COMMAND = re.compile(r"(?:^|[\s;&|(`$])(?:curl|wget)(?:$|[\s])", re.I)
_URL_IN_COMMAND = re.compile(r"https?://[^\s'\"`)>|;]+", re.I)
# Endereço que não exige conhecimento do mundo: não é palpite, é convenção.
_URL_HOSTS_LOCAIS = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|"
    r"192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+)(?::\d+)?(?:/|$)",
    re.I,
)


def _urls_nao_fundamentadas(command: str, haystack_lower: str) -> list[str]:
    """URLs do comando que não aparecem LITERALMENTE na conversa.

    A fundamentação é pela URL inteira, não pelo host: "pega no site do portal de dados" não
    torna `https://www.exemplo.gov.br/arquivos/plano_2026.pdf` real — o host é
    plausível e o caminho continua sendo chute, que é justamente o que falha
    (404, ou pior, um HTML de erro salvo como .pdf). Só a busca produz deep link.
    """
    fora: list[str] = []
    for url in _URL_IN_COMMAND.findall(command or ""):
        limpa = url.rstrip(".,'\"")
        if _URL_HOSTS_LOCAIS.match(limpa):
            continue
        if limpa.lower() in haystack_lower:
            continue
        fora.append(limpa)
    return fora


#: Marca de URL que o próprio classificador sabe ser de mentira: "XXXXXX", um
#: `<link>` por preencher, o domínio de exemplo.
_URL_PLACEHOLDER = re.compile(r"[Xx]{6,}|<[^>]*>|\bexample\.(?:com|org)\b|\bseu[-_]link\b", re.I)
#: Segmento de caminho que é IDENTIFICADOR (id de formulário, documento, arquivo):
#: longo e misturando letra com dígito. Não se adivinha — ou alguém citou, ou é
#: inventado.
_URL_ID_SEGMENTO = re.compile(r"/(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{16,}")


def _url_inventada(url: str) -> bool:
    """Esta URL (já sabida NÃO citada na conversa) só pode ter sido inventada?

    Estreita de propósito. ``open 'https://www.youtube.com/results?search_query=AC/DC'``
    também não é citação literal e é legítimo — o classificador MONTA a busca a
    partir da fala (8 dos 9 `open` com URL nos logs de produção, 01/11). O que
    não se monta é placeholder e id: na session-8f2febfa "faz isso" virou
    ``open -a "Google Chrome" ".../forms/d/e/1FAIpQLSfXXXXXXXX…/viewform"``, com a
    própria explicação dizendo "usarei um placeholder genérico".
    """
    caminho = url.split("?", 1)[0].split("#", 1)[0]
    return bool(_URL_PLACEHOLDER.search(url) or _URL_ID_SEGMENTO.search(caminho.split("://", 1)[-1]))


# Print do navegador DO SERVIDOR não responde pergunta sobre a mídia DELE.
_SERVER_BROWSER_TOOLS = frozenset({"browser_screenshot", "browser_task"})
_USER_MEDIA_ANCHORS = (
    "youtube", "you tube", "spotify", "musica", "tocando", "player",
    "aba", "abas", "tab", "tabs", "guia", "guias",
)

#: Trabalho com ARQUIVO de áudio: baixar, juntar, converter, cortar. "baixa" é
#: verbo de controle ("baixa o som" = abaixar o volume), então "baixa o som de
#: connect USB do Windows, junta num mp3" virava pedido de música, pulava o
#: classificador e tirava as tools da mesa (session-367b0103, 14/11). Medido nas
#: 2747 mensagens reais de usuário: das 108 que o atalho pegava, só 2 tinham estas
#: palavras, e as 2 eram falsos positivos.
_MUSIC_FILE_WORK = re.compile(
    r"\b(mp3|mp4|wav|ogg|m4a|flac|aac|arquivos?|download|"
    r"junta|juntar|concatena|concatenar|converte|converter|"
    r"corta|cortar|recorta|recortar|edita|editar)\b"
)
_MUSIC_COMPLAINT = re.compile(
    r"\bn[ãa]o\s+(mutou|pausou|pulou|tocou|parou|retomou|abaixou|aumentou)\b"
)

# Media-STATUS questions ("quais abas do YouTube estão abertas?", "o que tá
# tocando?", "como tá o YouTube?") must go to the cognitive core, which gets a
# real-time media_context note (the open browser tabs). The cheap classifier
# otherwise sends "quais abas abertas" to run_command (osascript inspecting
# Chrome) — the exact misroute seen in the screenshot. Patterns run on accent-
# stripped, whitespace-collapsed text. See neo-music-feature.
# "tab"/"tabs" em inglês: o Daniel escreve os dois ("quais tab aberta com
# youtube no momento?"). Sem eles a pergunta não casava o guard, ia para o
# classificador, que respondeu com `browser_screenshot` do navegador DO
# SERVIDOR e concluiu "não tem navegador rodando, então não tem aba do
# YouTube" — com a aba tocando no `media_context` daquele mesmo turno.
_MEDIA_TAB_NOUNS = ("aba", "abas", "guia", "guias", "tab", "tabs")
_MEDIA_STATUS_CTX = (
    "youtube", "you tube", "spotify", "navegador", "chrome", "edge",
    "musica", "video", "tocando", "aberta", "abertas", "aberto", "abertos", "player",
)
_MEDIA_PLAYING_QUERY = re.compile(r"\b(o que|que|qual|quais)\b.*\b(tocando|toca|rolando)\b")
_MEDIA_QUESTION_STARTS = ("tem ", "tem alguma", "existe ", "ta aberta", "tao aberta")
# "ta vendo o video aberto no YouTube?", "digo vc enchega a aba aberta?" — o
# usuário pergunta se o Neo ENXERGA, não "quais abas". Sem isto a pergunta caiu
# no classificador, que respondeu com `browser_screenshot` do navegador DO
# SERVIDOR: "Tô vendo nada aqui do meu lado não — seu navegador não transmite
# tela pra mim" (sessão 0565907e), com as abas dele no media_context do turno.
# As grafias erradas estão de propósito: é assim que ele digita.
_MEDIA_SEEING = re.compile(r"\b(ve|ver|vendo|veja|enxerg\w*|ench[ae]rg?\w*)\b")
# Contexto ESTREITO para a pergunta de percepção — só o que é mídia mesmo.
# "navegador" ficou de fora de propósito: "acessa o meu google drive pelo menu
# navegador local e VEJA se tem nfse de abril" é ordem de tarefa no navegador,
# não pergunta sobre aba de mídia, e virar status quebraria o download.
_MEDIA_SEEING_CTX = (
    "youtube", "you tube", "spotify", "musica", "tocando", "player",
    "aba", "abas", "tab", "tabs", "guia", "guias", "video",
)
# Verbo de AÇÃO no navegador → é tarefa, não pergunta de percepção.
_BROWSER_ACTION_VERBS = re.compile(
    r"\b(acessa|acessar|abre|abrir|entra|entrar|navega|navegar|baixa|baixar|"
    r"clica|clicar|busca|buscar|procura|procurar|pesquisa|pesquisar|preenche|preencher)\b"
)

# Tools owned by a skill — excluded from the router's tool palette (Workstream A).
# The router routes to the SKILL via skill_name; the skill picks its own tools.
# ``neo_``/``job_``/``schedule_`` are the Neo's OWN agent tools (workbench sandbox,
# background jobs, scheduler): they are reachable ONLY through the cognitive core's
# inline tool loop, never by direct classification — so a "compute this" / "run a
# script" / "remind me in 5 min" turn routes to COGNITIVE, which then decides to
# use them. See docs/workbench-background-jobs-plan.md.
_SKILL_OWNED_TOOL_PREFIXES = (
    "ha_", "whatsapp_", "spotify_", "youtube_", "neo_", "job_", "schedule_",
    # Skills library (docs/skills-library-plan.md): skill_read/skill_list são do
    # tool loop do cognitivo, e as tools "gated" (sql_*, fin_*) só existem DEPOIS
    # de o cérebro ler a skill — o classificador nunca as vê nem roteia para elas.
    "skill_", "sql_", "fin_",
    # Busca web + deep research (docs/web-search-deep-research-plan.md): decisão
    # do cérebro, inline — nunca roteadas direto (evita o bypass do cognitivo).
    "web_", "deep_",
    # Service Gateway (docs/service-gateway-dsl-plan.md): o cognitivo é quem
    # descobre a conexão (service_list/service_docs) e só então monta a chamada.
    # Rotear direto para service_request deixaria o classificador inventar
    # service/path/body sem passar pela descoberta — o mesmo bypass que fazia a
    # skill HA alucinar entity_id. A política ainda barraria a escrita, mas o
    # caminho certo é o cérebro.
    "service_",
    # File Store (docs/file-store-plan.md §4): tools do catálogo são do tool loop
    # do cognitivo — quem ativa é o manifesto do anexo, não o classificador.
    "file_",
    # Cadastros generativos (docs/datasets-crud-plan.md §9.1): as data_* só
    # existem depois de o cérebro ler a skill do cadastro. Deixar o classificador
    # ver `data_create` seria o mesmo bypass que fazia a skill HA alucinar
    # entity_id — ele rotearia direto e inventaria dataset e campos, sem passar
    # pela descoberta que ensina os campos reais. `tool_` cobre o agregador
    # (tool_find/tool_run), pelo mesmo motivo.
    "data_", "tool_",
    # Inscrições de evento (docs/playlists-skill-plan.md §7.7): armar um gatilho
    # exige saber a aba e a faixa que ACABARAM de tocar — contexto que só o
    # cognitivo tem. O classificador roteando para event_subscribe inventaria
    # alvo, que é justamente o que não pode existir: inscrição no vazio.
    "event_",
)

# request_user_file (plano "arquivos sob iniciativa do cérebro") é client-side mas
# PURAMENTE do cérebro: ele decide puxar um arquivo do usuário no meio do turno
# cognitivo (via ClientActuationRequested). Fica fora da visão do classificador
# pra não roubar do DIRECT_RESPONSE os pedidos de arquivo (start conservador). O
# save_generated_file, esse sim, segue roteável ("salva aí" → CLIENT_TOOL).
# Mesmo conjunto, com nome público: o trilho SERVER_TOOL usa isto para NÃO
# oferecer ao agente uma tool gated que ele não sabe executar (ver
# `ai_service._server_tool_toolset`). Esconder do classificador nunca bastou —
# quem chamou a `data_list` sem usuário foi o agente, não o classificador.
SKILL_OWNED_TOOL_PREFIXES = _SKILL_OWNED_TOOL_PREFIXES


def server_tool_can_run(tool_name: str, *, execution: str | None = None) -> bool:
    """O trilho SERVER_TOOL sabe EXECUTAR esta tool?

    Uma definição só, usada nos dois lugares que dependem dela: o toolset que o
    agente enxerga (`ai_service._server_tool_toolset`) e a decisão de devolver o
    turno ao cognitivo (`_brain_skill_for`). Separadas, elas divergiriam no
    primeiro prefixo novo — e a divergência é silenciosa: ou o agente vê uma tool
    que não roda, ou o turno é desviado de um caminho que funcionava.

    Gated (`data_`, `fin_`, `sql_`, `skill_`, …) = destravada pelo `skill_read` do
    cognitivo, com o usuário do turno. As job tools são a exceção: casam o prefixo,
    mas este trilho tem executor por request para elas (`_with_job_tools`).

    Tool de CLIENTE (``execution="client-side"``: ``run_command``, ``write_file``,
    ``request_user_file``…) também não roda aqui: ela precisa da ida e volta com o
    aparelho, que só o trilho do cliente e o cérebro têm, e o executor global não
    tem módulo ``desktop``. Oferecida ao agente, virou "Executor para módulo
    'desktop' não registrado" DEPOIS de ele já ter enfileirado o job do browser
    (session-30b1e484) — o usuário leu fracasso e pediu de novo. Quem não passa
    ``execution`` (o desvio por skill, que só tem o nome) fica na regra de prefixo.
    """
    if not tool_name:
        return False
    try:
        from core.jobs import JOB_TOOL_NAMES
    except Exception:  # noqa: BLE001 — sem jobs, sobra o filtro por prefixo
        JOB_TOOL_NAMES = frozenset()
    if tool_name in JOB_TOOL_NAMES:
        return True
    if execution == "client-side":
        return False
    return not tool_name.startswith(SKILL_OWNED_TOOL_PREFIXES)

_BRAIN_ONLY_CLIENT_TOOLS = frozenset(
    {"request_user_file", "read_user_file", "edit_user_file", "delegate_file_edit"}
)

# Skills whose actions have side effects: they may ONLY be fired on a clear,
# confident imperative. Anything conditional/definitional/uncertain about them is
# routed to the cognitive core instead (Workstream B — bias to brain).
_SIDE_EFFECT_SKILL_INTENTS = frozenset({
    IntentType.HOME_ASSISTANT,
    IntentType.WHATSAPP_MESSAGING,
})

_SPEECH_ACTS = frozenset({
    "imperative", "question", "conditional", "definition", "chitchat",
})

# ── Orçamento de saída da classificação ─────────────────────────────────────
# `max_tokens` é TETO, não reserva: cobra-se o que o modelo gera, e ele para
# sozinho ao fechar o JSON. Ou seja, margem larga é grátis no caminho normal e
# só aparece na conta quando evita um turno perdido — a assimetria toda está do
# lado de dar folga.
#
# A resposta típica (16 campos, explicação em PT) mede ~150 tokens; o pior caso
# realista — `pipeline_plan` com vários steps e explicação longa — fica em ~400.
# O valor antigo (300) foi escrito em fev/2026, quando o contrato tinha 8 campos
# e ~60 tokens: era 5× de margem. O contrato dobrou em quatro features
# (`knowledge_keywords`, `speech_act`, `pipeline_plan`, `gravity`/
# `register_hint`) e o classificador virou um modelo que RACIOCINA antes de
# responder — aí o raciocínio consumia o teto antes da primeira chave e o
# conteúdo voltava truncado, ou vazio, caindo no fallback por palavra-chave
# (medido: 13 de 46 casos com deepseek-v4-flash).
#
# 2048 dá ~14× a resposta típica e ~5× o pior caso, e ainda absorve um bom
# tanto de raciocínio se algum provider ignorar `disable_thinking`. 4096 na
# retentativa é o dobro disso E o teto de saída mais baixo entre os providers
# que o painel deixa escolher (modelos Claude antigos param em 4096) — pedir
# mais que isso arriscaria um 400 em vez de uma classificação.
_CLASSIFIER_TYPICAL_OUTPUT_TOKENS = 150
_CLASSIFIER_MAX_TOKENS = 2048
_CLASSIFIER_MAX_TOKENS_RETRY = 4096

#: Frases NOSSAS que jamais deveriam sair como conteúdo de uma ação.
#:
#: A rede embaixo da correção do reparo (ver ``classify``): o classificador é um
#: modelo pequeno, e um modelo pequeno confunde instrução com dado de vez em
#: quando. Quando isso acontece num intent que FALA COM O MUNDO — mandar
#: WhatsApp, rodar comando, enviar email — o estrago não é uma resposta ruim, é
#: uma mensagem de verdade para uma pessoa de verdade com o prompt do Neo dentro.
#:
#: São fragmentos curtos e específicos do nosso próprio texto: não aparecem numa
#: frase que o usuário escreveria, e casar por substring é barato o bastante para
#: rodar em todo turno.
_OWN_PROMPT_MARKERS: tuple[str, ...] = (
    "você é um classificador",
    "você é classificador",
    "intent classifier for neo",
    "objeto json do formato de saída",
    "json do formato de saída",
    "nada antes, nada depois",
    "esta instrução é de sistema",
    "não responda ao usuário",
)


#: Palavras que não identificam ninguém — saem antes de procurar o destinatário.
_RECIPIENT_STOPWORDS = frozenset(
    "o a os as um uma meu minha meus minhas seu sua do da dos das pro pra para "
    "no na sr sra senhor senhora contato numero número".split()
)


def _normalize_for_match(text: str) -> str:
    """Minúsculas, sem acento e sem pontuação — "mãe" tem que casar com "mae"."""
    stripped = unicodedata.normalize("NFD", (text or "").lower())
    stripped = "".join(c for c in stripped if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9@. ]+", " ", stripped)


def _recipient_is_grounded(recipient: Any, haystack: str) -> bool:
    """O destinatário proposto aparece no que o USUÁRIO realmente escreveu?

    A âncora do par afordância∧âncora (mesmo desenho da guarda de download, ver
    ``tests/test_download_claim_guard.py``): não basta a frase parecer uma ordem
    — a pessoa a quem se vai falar tem que estar em algum lugar da conversa.

    Sem destinatário proposto devolve True de propósito: aí a skill pergunta a
    quem enviar, que é seguro. O que não pode é ela receber um nome que o
    usuário nunca disse, resolvê-lo na agenda e montar o rascunho — foi assim
    que um "minha mãe" saído do nada virou um contato real (sessão 289b6c29).

    Medido contra as 1887 mensagens reais de usuário do Postgres em 2026-09-07:
    os 7 formatos de envio legítimo do corpus (e dos exemplos deste prompt)
    passam; um "minha mãe" inventado só ancoraria em 0,3% delas — e essas cinco
    são justamente mensagens de verdade para a mãe.
    """
    if not isinstance(recipient, str) or not recipient.strip():
        return True
    tokens = [
        t for t in _normalize_for_match(recipient).split()
        if t not in _RECIPIENT_STOPWORDS and len(t) >= 3
    ]
    if not tokens:
        return True  # só palavra vazia ("a", "o") — nada de concreto para ancorar
    field = _normalize_for_match(haystack)
    return any(t in field for t in tokens)


def _echoes_own_prompt(value: Any) -> bool:
    """O modelo devolveu o NOSSO texto como se fosse conteúdo do usuário?

    Percorre estruturas (``tool_arguments`` é dict, e o texto pode estar em
    qualquer chave) porque o campo exato varia por intent — o que importa é que
    nenhuma parte do payload de uma ação carregue instrução nossa.
    """
    if isinstance(value, str):
        low = value.lower()
        return any(marker in low for marker in _OWN_PROMPT_MARKERS)
    if isinstance(value, dict):
        return any(_echoes_own_prompt(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_echoes_own_prompt(v) for v in value)
    return False


def _strip_accents(text: str) -> str:
    """Lowercase and remove PT accents for robust keyword matching."""
    out = (text or "").lower()
    for src, dst in {
        "á": "a", "à": "a", "â": "a", "ã": "a",
        "é": "e", "ê": "e", "í": "i",
        "ó": "o", "ô": "o", "õ": "o",
        "ú": "u", "ç": "c",
    }.items():
        out = out.replace(src, dst)
    return out

ROUTER_SYSTEM_PROMPT = """You are an intent classifier for Neo, a desktop AI assistant. Classify the LAST user message into ONE execution path. Você é CLASSIFICADOR, não o assistente: não responda ao usuário — devolva só o JSON do formato de saída.

## Regra mestra: em QUE MÁQUINA a tarefa precisa rodar
Três lugares FÍSICOS distintos, sem filesystem compartilhado entre eles:
1. **Sandbox do cérebro** — `/workspace`, container Linux na VPS, com internet. É onde o DIRECT_RESPONSE cria e processa arquivo (tools `neo_*` do cérebro).
2. **Máquina do usuário** — o disco, o sistema e os apps DELE. Só o CLIENT_TOOL (`run_command`) executa lá de verdade.
3. **Catálogo de arquivos** (File Store do neo-core) — a ÚNICA ponte entre os dois: o cérebro publica um arquivo do sandbox com `file_save` e ele aparece no histórico como "entregue ao usuário: 'nome' (file_id=...)".

Esta é a decisão que mais erra — resolva-a ANTES de escolher tool:
- Calcular, gerar (uuid/hash/senha/QR code), converter formato, processar dados, rodar/testar código, baixar de uma URL e analisar → **DIRECT_RESPONSE**: o cérebro faz isso no PRÓPRIO sandbox, sem pedir permissão e sem depender da máquina do usuário estar ligada.
- Arquivo que já existe no DISCO dele, info do SISTEMA/hardware dele, instalar/ABRIR um programa dele, abrir uma URL/universal link no computador dele → **CLIENT_TOOL**.
- Na dúvida entre os dois: se a mensagem não fala do disco/sistema/app DELE, é DIRECT_RESPONSE.
- Registre no campo `onde`: `sandbox`, `maquina` (disco/sistema/app dele), `navegador` (navegador logado dele) ou `nenhum` (só conversa).

**Quem decide o trilho é a ORIGEM do conteúdo, nunca o destino.** Citar a pasta de destino ("põe na área de trabalho", "salva na pasta X") NÃO torna a tarefa CLIENT_TOOL — destino no disco dele é problema da ponte, não do trilho. Conteúdo que vem da INTERNET (baixar PDF/planilha/imagem, com ou sem link em mãos) é **DIRECT_RESPONSE** mesmo quando ele pede o arquivo no disco: o cérebro procura o endereço real com `web_search`, baixa no sandbox, publica com `file_save` e entrega com `save_generated_file` (que aceita caminho literal de pasta e cria a pasta). Só é CLIENT_TOOL quando o conteúdo JÁ ESTÁ na máquina dele. Você NUNCA escreve um `curl`/`wget` com URL que o usuário não citou — você não sabe o endereço de um arquivo que nunca viu, e um endereço plausível baixa um 404 salvo com extensão .pdf.

Nada cruza de um lado pro outro por caminho ou URL escrita à mão: um `run_command` citando `/workspace/...` sempre falha ("No such file or directory" — esse caminho só existe na VPS), e um `curl` pra baixar o arquivo do neo-core nunca funciona (você não sabe, e não pode adivinhar, o host/porta real — só o cliente sabe). Levar um arquivo GERADO PELO CÉREBRO pro lado do usuário ("salva aí", "manda pra mim", "põe no Desktop/Downloads/naquela pasta") tem um caminho só:
- Já existe `file_id` no histórico → **CLIENT_TOOL** com `tool_name="save_generated_file"` e `tool_arguments={{"file_id": "<o file_id exato, copiado literalmente do histórico>", "file_name": "<nome do arquivo>", "destination": "<destino>"}}`. `destination` é opcional (padrão "desktop"): "downloads"/"documents"/"home" pros atalhos conhecidos, ou um caminho literal (ex: "~/Projetos/financas") se o usuário citou a pasta.
- Ainda NÃO existe `file_id` → **DIRECT_RESPONSE** primeiro (o cérebro gera e chama `file_save`); só no turno seguinte "coloca no meu disco" vira CLIENT_TOOL.
Nunca invente um `file_id` nem um caminho que o usuário não mencionou.

## Paths
- **DIRECT_RESPONSE** (o núcleo cognitivo / "cérebro"): conversa, conhecimento geral, saudação, jogo, tradução, dados pessoais (treino, dieta, suplemento, rotina — injetados automaticamente) — qualquer coisa respondível em texto — MAIS tudo que roda no sandbox (regra mestra acima). O cérebro também tem `web_search` e as tools `file_*` do catálogo. **Default na dúvida.**
- **SERVER_TOOL**: dado externo em tempo real (clima via `get_weather`) OU ler/mudar uma CONFIGURAÇÃO do Neo (voz/engine de TTS, etc.). Para MUDAR use `tool_name="config_set"` com `tool_arguments={{"setting": "...", "value": "..."}}`: voz → `{{"setting":"tts.voice","value":"<nome>"}}`; engine/motor → `{{"setting":"tts.engine","value":"<qwen|coqui|dia>"}}`. "quais vozes você tem?" → `voice_list`; "o que dá pra configurar?" → `config_list`.
- **CLIENT_TOOL**: só o que exige a máquina do usuário (regra mestra acima). Existem TRÊS tools client-side e nenhuma outra — `read_file`, `list_directory`, `search_files`, `get_system_info` NÃO existem, nunca invente uma quarta:
  - `save_generated_file`: entrega no disco dele um arquivo que o cérebro já publicou (exige um `file_id` real do histórico). Pergunta de verificação ("salvou mesmo?", "atualizou o arquivo?") é DIRECT_RESPONSE — nunca refaça a gravação pra responder. Se o contexto tem o bloco "ARQUIVOS DESTA CONVERSA", o `file_id` sai de LÁ (a versão atual do documento pedido) — nunca o id de outro arquivo só por ser o mais recente. Pedido que muda o conteúdo antes de entregar ("adiciona", "atualiza", "corrige"), arquivo "corrompido/errado", apagar uma cópia ou "botão de download" é DIRECT_RESPONSE.
  - `write_file`: grava TEXTO que você já tem num arquivo do disco dele, em uma chamada — `tool_arguments={{"path": "<absoluto ou ~/...>", "content": "<o texto>"}}` (opcional `mode: "append"`). Prefira esta a um `run_command` com `cat <<EOF`/`echo >`: pelo shell, aspas/crase/`$` no conteúdo corrompem o arquivo calado. Só quando o usuário DITOU o texto literal NESTA mensagem. EDITAR/ATUALIZAR/CORRIGIR/MELHORAR um arquivo que já existe no disco dele (mesmo com o caminho exato), ou gravar um texto que ainda precisa ser escrito/revisado → **DIRECT_RESPONSE**: o cérebro lê a versão atual, aplica a mudança e grava. Você nunca redige o conteúdo de um arquivo e nunca passa `overwrite`.
  - `run_command`: todo o resto (ler/listar/escrever arquivo, espaço em disco, processos, instalar/abrir programa), via `raw_command` de shell. O comando só pode citar nome/caminho que o USUÁRIO disse nesta mensagem ou no histórico recente — NUNCA invente um nome plausível que ele não mencionou (pediram "meu relatório de finanças" sem dizer o nome → não existe `relatorio.csv` pra chutar; isso é DIRECT_RESPONSE ou plano de sondagem).
- **WHATSAPP_MESSAGING**: enviar QUALQUER mensagem de WhatsApp em nome do usuário — pessoal ("avisa minha mãe que vou chegar tarde"), comercial (pizza, delivery, farmácia, mercado, encomenda) ou pra telefone/JID direto. Extraia sempre em `tool_arguments` os campos `text` (o texto) e `recipient` (JID, telefone OU apelido cru como "minha mãe", "pai", "Ana" — NÃO interprete o apelido, o pipeline resolve na agenda). `contact_source` ("whatsapp" = agenda do WhatsApp | "saved" = contatos salvos do Neo) só quando o usuário disser a fonte EXPLICITAMENTE; senão omita.
- **HOME_ASSISTANT**: a casa inteligente do usuário — luz, tomada/interruptor, clima/termostato, persiana, cena, script, automação ("acende a luz da sala", "ativa o modo cinema", "qual a temperatura do termostato?"). É uma SKILL: use `intent=HOME_ASSISTANT`, `skill_name="home_assistant"` e PARE — NÃO escolha tools `ha_*`, NÃO preencha `entity_id`/`tool_arguments` (a skill resolve o dispositivo sozinha) e nunca invente entity_id. NÃO confundir com `list_connected_devices`, que é dos dispositivos do PRÓPRIO Neo (desktop/celular).

## Ato de fala (decida ANTES do intent)
Classifique o **ato de fala** e preencha `speech_act`:
- `imperative` — ordem para fazer AGORA ("acende a luz", "manda um oi pra mãe").
- `question` — pergunta ("qual o clima?", "que dispositivos eu tenho?").
- `conditional` — ação condicionada a algo do usuário ("se eu costumo, pode ligar").
- `definition` — definir/cadastrar uma regra/brincadeira/comportamento ("cadastra uma brincadeira: se eu te provocar, você pode desligar a luz").
- `chitchat` — conversa, sem ação.

REGRA DURA: só use HOME_ASSISTANT ou WHATSAPP_MESSAGING quando `speech_act=imperative` E você tem ALTA confiança. `conditional`, `definition`, `question` sobre a ação, ação que DEPENDE de um hábito/preferência do usuário ("se eu costumo…", "como eu gosto de…"), ou qualquer dúvida → **DIRECT_RESPONSE** (o núcleo cognitivo lê o PKS e decide; pode acionar a skill depois). Mencionar "luz/ar" dentro de uma hipótese ou regra NÃO é um comando.

## Gravidade e registro (sinais de TOM — não mudam o roteamento)
- `gravity`: seriedade da SITUAÇÃO relatada, não a complexidade da tarefa. "alta" = problema sério/urgente acontecendo AGORA (sistema quebrado, produção fora do ar, perda de dados, cliente afetado); "critica" = catastrófico; senão "normal". Pergunta técnica comum ou tarefa difícil SEM urgência = "normal".
- `register_hint`: "protocolo" quando a RESPOSTA será vista por terceiros ou o contexto é formal (reunião, chefe, cliente, apresentação, relatório); "confidente" quando o usuário está desabafando ou trazendo assunto pessoal/emocional; senão null. Na dúvida, null.

## Server Tools
{server_tools}

## Client Tools
{client_tools}

## Skills
{skills}

## Key Rules
- **Arquivo ANEXADO na conversa**: se o contexto tem a seção "ARQUIVOS ANEXADOS NESTA MENSAGEM" ou "ARQUIVOS DESTA CONVERSA", esses arquivos estão no catálogo do PRÓPRIO Neo (não no disco do usuário) — QUALQUER pergunta sobre eles ("o que tem nesse arquivo?", "de qual instituição é?", "resume", "quantas linhas tem") é **DIRECT_RESPONSE**: o cérebro lê sozinho com as tools `file_*`. NUNCA escolha `run_command` para eles e **NUNCA invente um caminho** (ex.: `/Users/.../Downloads/<nome>`) a partir do nome do arquivo. Só é CLIENT_TOOL quando o USUÁRIO cita explicitamente um caminho/pasta da máquina dele nesta mensagem.
- **Mostrar/exibir IMAGEM** ("carrega essa imagem aqui no chat", "mostra a foto X", "quero ver o print"): quem exibe é o CÉREBRO — ele escreve `[[IMAGE:/caminho/da/imagem.ext]]` na resposta e o cliente desenha. Se o caminho JÁ apareceu nesta conversa (o usuário citou, ou saiu de um comando anterior) → **DIRECT_RESPONSE**. Só é CLIENT_TOOL quando ainda falta DESCOBRIR onde a imagem está (`ls`/`find`). NUNCA roteie para um comando que "converte a imagem" (`base64`, `xxd`, `cat` de .jpg/.png): isso não mostra nada e despeja um blob gigante na conversa. Exceção: print da tela do BROWSER do servidor ("mostra o que apareceu no browser", "tira um print daí") → **SERVER_TOOL** `browser_screenshot` — essa imagem ainda não existe, precisa ser tirada.
- **Evento atual / notícia / esporte / cotação** ("qual o jogo hoje?", "quem ganhou X?", "cotação do dólar", "saiu a versão nova?") e questionamento de resposta anterior ("tem certeza?"): **DIRECT_RESPONSE** — o cérebro busca na web sozinho antes de responder. NÃO escolha tool aqui.
- **Agir é o padrão; só o TEXTO do comando é a exceção**: quando o pedido depende da máquina do usuário, a resposta certa é RODAR (CLIENT_TOOL), não explicar como rodar — inclusive em follow-up curto logo depois de um comando ("e quais?", "lista os nomes", "então roda", "e agora?", "mostra tudo"), que é a MESMA tarefa com mais detalhe. A ÚNICA exceção é ele pedir explicitamente só o texto ("qual o comando pra...", "me diz o comando", "como eu faço pelo terminal?", "não roda, só me mostra") → **DIRECT_RESPONSE**.
- **Continuidade e back-reference**: jogo/quiz/roleplay/tarefa criativa em andamento no histórico, ou referência ao que já foi feito ("está errado", "refaz", "melhora isso", "de onde veio isso?") → DIRECT_RESPONSE.
- **Diretiva de memória** ("lembra que...", "registra no PKS", "memoriza isso", "anota que...", "não esqueça que...", "salva essa ideia"): DIRECT_RESPONSE — o cérebro grava no próprio turno com a tool `pks_remember`. NUNCA um client tool.
- **Dispositivos**: "conectados/online/agora/em tempo real" → SERVER_TOOL `list_connected_devices` (WebSocket ao vivo, APENAS conexões ativas no momento). "que eu tenho/meus/cadastrados/que você conhece" → DIRECT_RESPONSE com `needs_memory=true` e `knowledge_keywords:["dispositivo"]` (vêm do PKS).
- **Caminhos explícitos**: use paths absolutos como estão; para pastas conhecidas (Documents, Downloads, Desktop) use as dicas do "Contexto do Sistema".
- `requires_approval=true` para operação destrutiva (delete, kill, format); `false` para leitura.
- `needs_memory=false` para saudação/ack ("oi", "ok", "valeu"); `true` para assunto substantivo.
- `knowledge_keywords`: até 6 substantivos PT no singular tirados da mensagem (dispositivo, saúde, comida, finança...); `[]` em saudação.
- `needs_plan=true` para CLIENT_TOOL com 2+ passos dependentes, E TAMBÉM quando o alvo é um arquivo/recurso que o usuário só DESCREVEU (sem nome exato) — aí o primeiro passo do plano é sempre uma sondagem read-only (`ls`/`find`), nunca um comando que já assume o nome. Sempre `false` fora de CLIENT_TOOL.
- **Cascata (`pipeline_plan`)**: SOMENTE quando a mensagem tem 2+ ações CLARAMENTE INDEPENDENTES ligadas por "e"/"depois". Preencha a lista ordenada de steps `{{intent, input, skill_name}}`, cada `input` uma ordem imperativa, limpa e auto-contida. Uma ação só, ou ação condicional/baseada em hábito → `null`. Na dúvida, `null`.

## Output — valid JSON only, no markdown:
{{
  "speech_act": "imperative"|"question"|"conditional"|"definition"|"chitchat",
  "intent": "DIRECT_RESPONSE"|"SERVER_TOOL"|"CLIENT_TOOL"|"WHATSAPP_MESSAGING"|"HOME_ASSISTANT",
  "confidence": 0.0-1.0,
  "tool_name": "tool name or null (null para HOME_ASSISTANT)",
  "tool_arguments": {{}},
  "raw_command": "shell cmd or null",
  "explanation": "brief in Portuguese",
  "requires_approval": false,
  "needs_memory": true,
  "knowledge_keywords": [],
  "needs_plan": false,
  "tipo_tarefa": "simples|multi_etapas|analitica|criacao",
  "onde": "sandbox"|"maquina"|"navegador"|"nenhum",
  "skill_name": "whatsapp_messaging, home_assistant or null",
  "pipeline_plan": null,
  "gravity": "normal"|"alta"|"critica",
  "register_hint": "protocolo"|"confidente"|null
}}

## Exemplos
# Sandbox do Neo (DIRECT_RESPONSE) × máquina do usuário (CLIENT_TOOL)
User: "quantos números primos existem até 1 milhão?" → DIRECT_RESPONSE (o cérebro calcula no sandbox)
User: "baixa esse csv https://exemplo.com/a.csv e me diz quantas linhas tem" → DIRECT_RESPONSE (baixa e analisa no sandbox)
User: "quantos GB livres tem no meu disco?" → CLIENT_TOOL, tool_name "run_command", raw_command "df -h"
User: "lê o arquivo ~/Downloads/relatorio.pdf" → CLIENT_TOOL, "run_command", "cat ~/Downloads/relatorio.pdf" (caminho EXPLÍCITO na mensagem)
User: "acha os arquivos .env aqui no meu projeto" → CLIENT_TOOL, "run_command", "find . -iname '.env*'"
User: "abre o VSCode" / "instala o ffmpeg na minha máquina" → CLIENT_TOOL, "run_command"
User: "qual o comando pra ver quanto espaço tem no disco?" → DIRECT_RESPONSE (quer o TEXTO do comando, não a execução)
User: "relatorio de financas em csv na minha area de trabalho" → DIRECT_RESPONSE (nenhum arquivo existente foi citado — o cérebro GERA e entrega; nunca chute "relatorio.csv")
User: "manda pra mim aquele pdf da nota fiscal que tá no downloads" → CLIENT_TOOL, "run_command", needs_plan: true (o arquivo existe mas só foi descrito: o plano faz `ls`/`find` antes de decidir o nome real)

# Ato de fala, skills e cascata
User: "acende a luz da sala" → speech_act "imperative", HOME_ASSISTANT, skill_name "home_assistant", pipeline_plan null
User: "quais dispositivos estão conectados agora?" → "question", SERVER_TOOL, tool_name "list_connected_devices" (NÃO é HOME_ASSISTANT)
User: "cadastra uma brincadeira: se eu te provocar, você pode desligar a luz" → "definition", DIRECT_RESPONSE (definir uma regra ≠ executar agora; não invente entity_id)
User: "se eu costumo usar o ar nas quintas, pode ligar" → "conditional", DIRECT_RESPONSE (depende do hábito → o cérebro lê o PKS)
User: "apaga todas as luzes da casa e manda um whatsapp pro porteiro avisando que vou dormir" → HOME_ASSISTANT (a primeira ação), e
  "pipeline_plan": [
    {{"intent": "HOME_ASSISTANT", "input": "apagar todas as luzes da casa", "skill_name": "home_assistant"}},
    {{"intent": "WHATSAPP_MESSAGING", "input": "avisar o porteiro que vou dormir", "skill_name": "whatsapp_messaging"}}
  ]

# WHATSAPP_MESSAGING — extração
User: "manda uma mensagem para minha mãe que vou chegar mais tarde" → tool_arguments {{"text": "vou chegar mais tarde", "recipient": "minha mãe"}}
User: "manda whatsapp pro 5511988217222: oi tudo bem?" → {{"text": "oi tudo bem?", "recipient": "5511988217222"}}
User: "pede uma pizza no Domino's" → {{"text": "Olá, gostaria de fazer um pedido", "recipient": "Domino's"}}
User: "procura o João na lista do WhatsApp e manda um oi" → {{"text": "oi", "recipient": "João", "contact_source": "whatsapp"}}

# CONFIG (voz/engine de TTS)
User: "usa a voz do Jarvis" → SERVER_TOOL, "config_set", {{"setting": "tts.voice", "value": "jarvis"}}
User: "troca o engine pra qwen" → SERVER_TOOL, "config_set", {{"setting": "tts.engine", "value": "qwen"}}
User: "quais vozes você tem?" → SERVER_TOOL, "voice_list"
"""


_ONDE = frozenset({"sandbox", "maquina", "navegador", "nenhum"})


def _onde(valor: Any) -> str | None:
    """O campo ``onde`` do classificador, normalizado (``máquina`` → ``maquina``)."""
    texto = str(valor or "").strip().lower().replace("á", "a")
    return texto if texto in _ONDE else None


def _teto_raciocinio() -> int | None:
    """``[roteador] teto_raciocinio`` do neo.toml (0 = sem teto)."""
    try:
        from shared.ajustes import ajustes

        return ajustes().roteador.teto_raciocinio or None
    except Exception:  # noqa: BLE001 — ajuste ilegível não derruba a classificação
        return None


class IntentRouter:
    """Classifies user messages and routes to the correct execution path."""

    def __init__(
        self,
        llm_router: LLMRouter,
        tool_registry: ToolRegistry,
        db: Any | None = None,
        skill_library: Any | None = None,
    ) -> None:
        self.llm_router = llm_router
        self.tool_registry = tool_registry
        # Optional DB handle used to resolve the configurable classifier
        # provider/model (modules.intent_config). None → static default.
        self.db = db
        # SkillLibrary (docs/skills-library-plan.md): fonte da seção "assuntos com
        # skill do cérebro → DIRECT_RESPONSE" no prompt do classificador.
        self.skill_library = skill_library

    async def _resolve_classifier(self) -> tuple[str, str]:
        """Resolve the (provider, model) used to classify intent.

        The selection is configurable from the neo-panel and persisted via
        ``modules.intent_config`` (read here, memoized in-process). Falls back to
        the DeepSeek V4 Flash default when nothing is set, the DB is absent, or the
        chosen provider isn't actually available (e.g. its API key is missing), so
        classification never breaks because of a bad/incomplete config.
        """
        from modules.intent_config import DEFAULT_CLASSIFIER, get_classifier_config

        db = getattr(self, "db", None)
        config = await get_classifier_config(db)
        provider_name = config.get("provider") or DEFAULT_CLASSIFIER["provider"]
        model_name = config.get("model") or DEFAULT_CLASSIFIER["model"]

        try:
            available = self.llm_router.list_available_providers()
        except Exception:  # pragma: no cover - defensive
            available = []
        if provider_name not in available:
            logger.warning(
                "Intent classifier provider '%s' unavailable; falling back to '%s'",
                provider_name, DEFAULT_CLASSIFIER["provider"],
            )
            return DEFAULT_CLASSIFIER["provider"], DEFAULT_CLASSIFIER["model"]
        return provider_name, model_name

    async def classify(
        self,
        user_message: str,
        conversation_history: list[dict[str, str]] | None = None,
        system_context: str | None = None,
        affect_hint: str | None = None,
        affect_force_brain: bool = False,
        artifact_paths: dict[str, str] | None = None,
    ) -> RoutingDecision:
        """Classify user intent and return a routing decision.

        Args:
            user_message: The user's message text.
            conversation_history: Recent messages as [{role, content}] for context.
            system_context: Optional OS/environment context string to append to the prompt.
            affect_hint: Bloco de estado emocional do Neo para o prompt do
                classificador (docs/modo-birra-plan.md §3.7). ``None`` quando ele
                está no repouso — aí o prompt é byte-a-byte o de sempre. Números
                crus são seguros aqui: o roteador CLASSIFICA e devolve JSON, nunca
                fala com o usuário.
            affect_force_brain: Desvio determinístico para o núcleo cognitivo. O
                hint sozinho é sugestão a um LLM; isto é a garantia. Quem calcula
                é o AIService — o roteador não conhece PKS nem afeto.
            artifact_paths: ``{nome-do-arquivo: caminho real}`` dos arquivos que o
                Neo já pôs no disco do usuário (registro de artefatos do caderno
                de trabalho). Serve a DUAS coisas, e nenhuma delas é prompt: o
                nome conhecido passa a contar como fundamentado, e o caminho que
                o classificador montou de cabeça é substituído pelo real. É o que
                torna "vai direto no arquivo" determinístico em vez de torcida.

        Returns:
            RoutingDecision with intent, tool info, etc.
        """
        # ── Deterministic guards (fast-path only) ─────────────────────────────
        # GOLDEN RULE: do NOT add new per-phrase patterns here to fix a misroute
        # toward the cognitive core — that is the job of the bias-to-brain step
        # (_bias_to_brain, Workstream B). These guards exist only for (a) high-
        # precision deterministic fast-paths and (b) safety bypasses; all of them
        # fail safe to DIRECT_RESPONSE. See docs/intent-routing-redesign.md.

        # Neo training messages are always a direct response (Neo learns + acks).
        # Guard before the LLM so a long training prompt can never be misrouted
        # into a tool/terminal command.
        if self._is_training_message(user_message):
            logger.info("Intent: Neo training message → DIRECT_RESPONSE (router bypass)")
            return self._training_decision(user_message)

        # Addressed by another assistant's name ("alexa, liga a luz") → cognitive
        # core, so Neo's persona/behavior rules (pride/offense) apply instead of the
        # message fast-pathing to a skill (which is persona-blind). See session 2173af2b.
        if self._is_foreign_assistant_address(user_message):
            logger.info("Intent: foreign-assistant address → DIRECT_RESPONSE (persona, router bypass)")
            return self._foreign_assistant_decision(user_message)

        # Explicit memory directives ("lembra que...", "registra no PKS") are saved
        # by the PKS pipeline on the DIRECT_RESPONSE path — never a tool/terminal call.
        if self._is_memory_directive(user_message):
            logger.info("Intent: memory directive → DIRECT_RESPONSE (router bypass)")
            return self._memory_decision(user_message)

        # "Devices I have / known / registered" is a PKS-knowledge question, not
        # a live-connection query. Route it to DIRECT_RESPONSE so the PKS context
        # is injected, instead of firing list_connected_devices (session e245da98).
        if self._is_known_devices_query(user_message, conversation_history):
            logger.info("Intent: known-devices query → DIRECT_RESPONSE (PKS, router bypass)")
            return self._known_devices_decision(user_message)

        # Referential questions ("como tu chegou nessa conta de 22?") are asking
        # ABOUT a past action, not requesting a new tool/HA action. Keep them on
        # DIRECT_RESPONSE so context recovery runs, even when recent history is
        # full of HA commands (session 9d61bfba misrouted this to HOME_ASSISTANT).
        if self._is_referential_question(user_message):
            logger.info("Intent: referential question → DIRECT_RESPONSE (router bypass)")
            return self._referential_question_decision(user_message)

        # Conditional/habit-based actions and preference questions go to the
        # cognitive core so it can read the PKS and decide whether to hand off to
        # a skill — instead of the cheap router firing the skill blindly.
        if self._is_brain_reasoning_request(user_message):
            logger.info("Intent: conditional/preference request → DIRECT_RESPONSE (cognitive core, router bypass)")
            return self._brain_reasoning_decision(user_message)

        # Music requests go to the cognitive core (which reads the PKS) and never
        # fast-path to the skill — so behavioral rules like "refuse sertanejo" win.
        from shared.config import get_settings as _get_settings
        if _get_settings().music_enabled:
            # Status questions ("quais abas do YouTube estão abertas?", "o que tá
            # tocando?") are answered from the live media_context note — never a
            # terminal command (classifier otherwise emits osascript). Checked
            # before the play/control guard since these carry no action verb.
            if self._is_media_status_query(user_message):
                logger.info("Intent: media-status query → DIRECT_RESPONSE (media_context, router bypass)")
                return self._media_status_decision(user_message)
            if self._is_music_request(user_message):
                logger.info("Intent: music request → DIRECT_RESPONSE (cognitive core handoff, router bypass)")
                return self._music_decision(user_message)

        # Build tool descriptions for the prompt
        server_tools_desc = self._format_tools("server-side")
        client_tools_desc = self._format_tools("client-side")
        skills_desc = self._format_skills()

        base_prompt = ROUTER_SYSTEM_PROMPT.format(
            server_tools=server_tools_desc or "(none registered yet)",
            client_tools=client_tools_desc or "(none registered yet)",
            skills=skills_desc or "(none registered yet)",
        )

        # Assuntos cobertos por skills .md do cérebro → DIRECT_RESPONSE (evita o
        # classificador mandar "consulta o banco" para run_command/CLIENT_TOOL).
        brain_note = self._skills_brain_note()
        if brain_note:
            base_prompt += f"\n\n{brain_note}"

        # Estado emocional do Neo (§3.7 do modo-birra-plan). Bloco CONDICIONAL: só
        # existe quando ele está fora do repouso, então o caminho quente de todo
        # dia continua com o prompt enxuto que o corte de 37% deixou.
        if affect_hint:
            base_prompt += f"\n\n{affect_hint}"

        # Inject system context (OS, paths) when provided by the client
        if system_context:
            base_prompt += f"\n\n## Contexto do Sistema do Usuário\n{system_context}"

        system_prompt = base_prompt

        # Build messages
        messages: list[Message] = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
        ]

        # Add last few messages of history for context
        if conversation_history:
            for msg in conversation_history[-6:]:
                role = MessageRole(msg["role"]) if msg["role"] in ("user", "assistant") else MessageRole.USER
                messages.append(Message(role=role, content=msg["content"]))

        # Only append current message if not already the last in history
        last_hist = conversation_history[-1] if conversation_history else None
        if not last_hist or last_hist.get("content") != user_message or last_hist.get("role") != "user":
            messages.append(Message(role=MessageRole.USER, content=user_message))

        try:
            # Classifier provider/model are configurable (neo-panel → intent
            # config), persisted via modules.intent_config. Defaults to DeepSeek
            # V4 Flash (fast, low-cost) when unset/unavailable.
            provider_name, model_name = await self._resolve_classifier()
            provider = self.llm_router.get_provider(provider_name)
            response = await provider.complete(
                messages=messages,
                tools=None,  # no function calling for classification
                model=model_name,
                temperature=0.1,
                max_tokens=_CLASSIFIER_MAX_TOKENS,
                disable_thinking=True,
                # Modelo que não deixa desligar o raciocínio recebe-o LIMITADO
                # (o provider traduz). Sem isto o GLM-5.3-flash raciocinava até
                # estourar o orçamento e a classificação chegava a 44 s.
                teto_raciocinio=_teto_raciocinio(),
                # Saída JSON nativa. O reparo abaixo existe porque o
                # classificador escorrega para o modo conversa quando o
                # histórico vem em turnos user/assistant — e responder ao
                # USUÁRIO em vez de classificar custa uma segunda chamada
                # inteira (medido: a classificação vai de ~2,5s para 8,4s, e
                # ele já chegou a inventar destinatário de WhatsApp a partir do
                # próprio texto do reparo). Com o formato imposto pela API, a
                # prosa deixa de ser uma saída possível. O provider degrada
                # sozinho no endpoint que não implementa o parâmetro, então o
                # reparo continua de pé como rede.
                response_format={"type": "json_object"},
            )

            raw_text = response.get_content() or ""

            # Duas falhas DIFERENTES levam ao mesmo lugar (JSON ausente → fallback
            # por palavra-chave, que é cego ao turno anterior), e cada uma pede um
            # remédio próprio — tratar as duas como "prosa" foi o que fez o reparo
            # não reparar nada:
            #
            #  (a) TRUNCAGEM: o JSON começa certo e é cortado no meio, ou volta
            #      vazio, porque o orçamento acabou (em modelo que raciocina, o
            #      raciocínio come tudo antes de escrever a primeira chave). A
            #      instrução de reparo não resolve — o que falta é ESPAÇO.
            #  (b) PROSA: com o histórico em turnos user/assistant, um follow-up
            #      curto ("Quais? lista os nomes") puxa o modelo pro modo conversa
            #      e ele responde ao usuário (sessão 69feab87: chegou a inventar
            #      uma lista de chaves SSH). Aí sim o remédio é mandar parar.
            if not self._carries_json(raw_text):
                # `{}` também é "vazio": o MiniMax m3.1-flash devolveu o objeto
                # vazio em 1 de 30 pedidos reais no Nebius (4 de 30 no
                # DigitalOcean), com 2 tokens de saída. Não é prosa — o reparo
                # "PARE, você é classificador" seria o remédio errado.
                if (
                    self._was_truncated(response)
                    or not raw_text.strip()
                    or self._is_empty_object(raw_text)
                ):
                    logger.warning(
                        "Router: resposta do classificador truncada/vazia (finish_reason=length "
                        "ou conteúdo vazio) — retentando com orçamento de %d tokens",
                        _CLASSIFIER_MAX_TOKENS_RETRY,
                    )
                    roomier = await provider.complete(
                        messages=messages,
                        tools=None,
                        model=model_name,
                        temperature=0.0,
                        max_tokens=_CLASSIFIER_MAX_TOKENS_RETRY,
                        disable_thinking=True,
                        teto_raciocinio=_teto_raciocinio(),
                    )
                    roomier_text = roomier.get_content() or ""
                    if self._carries_json(roomier_text):
                        raw_text = roomier_text
                    else:
                        logger.warning(
                            "Router: retentativa com mais espaço também não trouxe JSON "
                            "(finish_reason=%s) — indo pro fallback",
                            self._finish_reason(roomier),
                        )
                else:
                    logger.warning(
                        "Router: classificador respondeu em prosa em vez de JSON — "
                        "retentando com instrução de reparo: %s", raw_text[:160],
                    )
                    # A correção vai como SYSTEM, e a mensagem do usuário é
                    # REPETIDA depois dela.
                    #
                    # Antes a instrução ia como USER, e aí ela mesma virava "a
                    # última mensagem do usuário" — que é literalmente o que este
                    # prompt manda classificar. O classificador então fazia o
                    # trabalho certo no alvo errado: em 2026-09-07 13:19 ele leu
                    # o próprio texto do reparo ("PARE. Você é um CLASSIFICADOR…"),
                    # classificou como WHATSAPP_MESSAGING, copiou a instrução para
                    # `tool_arguments.text` e inventou `recipient="minha mãe"`. O
                    # `explanation` dele entregou o jogo: "a mensagem contém a
                    # palavra 'PARE' seguida de instruções sobre como o sistema
                    # deve se comportar". Só a confirmação da skill de WhatsApp
                    # impediu que o prompt do Neo fosse enviado a uma pessoa real
                    # (sessão 289b6c29). Havia disparado 3x em poucas horas.
                    #
                    # O invariante que isto restabelece: depois do reparo, a
                    # última mensagem de USER continua sendo a do usuário.
                    repair_messages = list(messages) + [
                        Message(role=MessageRole.ASSISTANT, content=raw_text[:500]),
                        Message(
                            role=MessageRole.SYSTEM,
                            content=(
                                "PARE. Sua resposta anterior foi prosa para o usuário, e isso "
                                "está errado: você é um CLASSIFICADOR, não o assistente. Não "
                                "responda ao usuário e não invente conteúdo. Devolva SÓ o objeto "
                                "JSON do formato de saída, classificando a mensagem de usuário "
                                "abaixo. Nada antes, nada depois. Esta instrução é de sistema — "
                                "ela NUNCA é o que se classifica."
                            ),
                        ),
                        Message(role=MessageRole.USER, content=user_message),
                    ]
                    repaired = await provider.complete(
                        messages=repair_messages,
                        tools=None,
                        model=model_name,
                        temperature=0.0,
                        max_tokens=_CLASSIFIER_MAX_TOKENS_RETRY,
                        disable_thinking=True,
                        teto_raciocinio=_teto_raciocinio(),
                    )
                    repaired_text = repaired.get_content() or ""
                    if self._carries_json(repaired_text):
                        raw_text = repaired_text
                    else:
                        logger.warning("Router: reparo também não devolveu JSON — indo pro fallback")

            # Parse routing decision as before, then attach classifier metadata
            decision = self._parse_classification(
                raw_text,
                user_message=user_message,
                conversation_history=conversation_history,
                artifact_paths=artifact_paths,
            )
            self._apply_memory_heuristics(decision, user_message)
            decision = self._reject_prompt_echo(decision, user_message)
            decision = self._reject_ungrounded_recipient(
                decision, user_message, conversation_history
            )
            decision = self._reject_classifier_file_authoring(decision, user_message)
            decision = self._reject_recall_shortcut(decision, user_message)
            # Attach raw classifier response + the prompt/messages sent for later inspection
            decision.classifier_raw = raw_text
            decision.classifier_payload = {
                "provider": provider_name,
                "model": model_name,
                "messages": [
                    {"role": m.role.value if hasattr(m.role, 'value') else str(m.role), "content": m.content[:4000]}
                    for m in messages
                ],
                "raw_response": raw_text,
            }
            decision = self._bias_local_browser(decision, user_message, system_context)
            decision = self._bias_claude_code(decision, user_message)
            return self._apply_affect_bias(decision, affect_force_brain)

        except Exception as e:
            logger.exception(f"Intent classification failed: {e}")
            # Return a fallback decision but include the prompt we tried to send (if any)
            fallback = self._fallback_classify(user_message)
            self._apply_memory_heuristics(fallback, user_message)
            fallback.classifier_payload = {"messages": [
                {"role": m.role.value if hasattr(m.role, 'value') else str(m.role), "content": m.content[:4000]}
                for m in messages
            ], "error": str(e)}
            fallback = self._bias_local_browser(fallback, user_message, system_context)
            fallback = self._bias_claude_code(fallback, user_message)
            return self._apply_affect_bias(fallback, affect_force_brain)

    def _resolve_artifact_paths(
        self, command: str, artifact_paths: dict[str, str]
    ) -> tuple[str, list[str]]:
        """Troca caminho MONTADO por caminho REAL de artefato já registrado.

        O classificador compõe ``<pasta do Contexto do Sistema> + <nome>`` e
        acerta na maioria das vezes — mas é um palpite: o navegador do usuário
        pode salvar em outra pasta, e o nome pode ter mudado ao completar o
        download. Quando o nome está no registro de artefatos, o caminho real é
        conhecido e não há motivo para adivinhar.

        Devolve ``(comando, nomes resolvidos)``. Os nomes servem para o guard de
        fundamentação: arquivo que EU mesmo pus lá está fundamentado por
        construção, mesmo que o usuário nunca tenha escrito o nome.
        """
        if not command or not artifact_paths:
            return command, []
        resolved: list[str] = []
        out = command
        for match in _PATH_WITH_EXT_RE.finditer(command):
            literal = match.group(0)
            basename = literal.rsplit("/", 1)[-1]
            real = artifact_paths.get(basename.lower())
            if not real or real == literal:
                continue
            out = out.replace(literal, real)
            resolved.append(basename)
        if resolved:
            logger.info(
                "Router: caminho resolvido pelo registro de artefatos %r — %r → %r",
                resolved, command, out,
            )
        return out, resolved

    def _ungrounded_path_literals(self, command: str, haystack: str) -> list[str]:
        """Caminhos com extensão citados em ``command`` que não aparecem (nem pelo
        nome do arquivo) em ``haystack`` (mensagem atual + histórico recente).

        Fundamentação é por NOME DE ARQUIVO (basename), não pelo caminho completo:
        o classificador tem permissão de montar `<pasta do Contexto do Sistema> +
        <nome que o usuário disse>` (ex.: "relatorio.csv" dito pelo usuário + "no
        desktop" → `/Users/.../Desktop/relatorio.csv`) — isso é composição legítima,
        não invenção. O que este guard pega é o CONTRÁRIO: um basename que não foi
        dito em lugar nenhum (ex.: ninguém falou "relatorio.csv").
        """
        if not command:
            return []
        haystack_lower = haystack.lower()
        ungrounded = []
        for match in _PATH_WITH_EXT_RE.finditer(command):
            literal = match.group(0)
            basename = literal.rsplit("/", 1)[-1]
            # Ponto INICIAL é dotfile/dot-dir, não extensão: `~/.ssh`, `~/.zshrc`,
            # `~/.config`, `~/.gitconfig`. O regex casa os dois do mesmo jeito, então
            # o guard tratava um caminho de sistema universal como "nome que o
            # classificador inventou" — e como ele responde ZERANDO o comando e
            # forçando needs_plan, um `ls -la ~/.ssh` perfeito virava turno travado
            # (sessão 9f7abeec). Nome sem ponto DEPOIS do inicial = não é arquivo
            # com extensão; não há o que fundamentar.
            stem = basename[1:] if basename.startswith(".") else basename
            if "." not in stem:
                continue
            # Fundamentado pelo nome com OU sem o ponto inicial: o usuário escreve
            # "meu zshrc", não "~/.zshrc".
            if basename.lower() in haystack_lower or stem.lower() in haystack_lower:
                continue
            # Fundamentado pelo nome SEM a extensão, quando esse nome aparece na
            # conversa como ARQUIVO (entre crases/aspas ou dentro de um caminho) —
            # não como palavra solta em prosa. É composição legítima: na sessão
            # e8e5664b o Neo tinha acabado de listar "`pc_gamer`" e o classificador
            # montou `pc_gamer.pub`, que o guard rejeitou como "inventado" e o turno
            # travou. A exigência do delimitador é o que separa esse caso do bug
            # original (bd7a304d): lá "relatorio" era substantivo comum na frase
            # "relatorio de financas em csv", e `relatorio.csv` era chute puro.
            root = basename.rsplit(".", 1)[0]
            if len(root) >= 3 and self._cited_as_filename(root, haystack_lower):
                continue
            ungrounded.append(literal)
        return ungrounded

    @staticmethod
    def _cited_as_filename(name: str, haystack_lower: str) -> bool:
        """True quando ``name`` aparece no histórico marcado COMO nome de arquivo.

        Delimitador antes (crase, aspas ou barra de caminho) e fim de token depois.
        Saída de comando e listagem do próprio Neo vêm sempre assim; prosa, não.
        """
        pattern = r"[`'\"/]" + re.escape(name.lower()) + r"(?=[`'\"\s,.;:)\]]|$)"
        return re.search(pattern, haystack_lower) is not None

    def _valid_client_tool_names(self) -> set[str]:
        """Nomes de tools client-side realmente registradas no ToolRegistry.

        Usado para recusar despachar um ``tool_name`` alucinado/desatualizado pro
        desktop (ele trava esperando um handler que não existe — ver guard em
        ``_parse_classification``).
        """
        return {t.name for t in self.tool_registry.get_all() if t.execution == "client-side"}

    def _format_tools(self, execution_type: str) -> str:
        """Format tool list for the system prompt.

        Skill-owned tools (``ha_*``, ``whatsapp_*``) are excluded: the router routes
        to the SKILL (``skill_name``), not to the skill's internal tools. Listing
        them biased the classifier toward HA and invited ``entity_id`` hallucination
        (Workstream A — see docs/intent-routing-redesign.md).
        """
        from core.claude_code import chave

        tools = [
            t
            for t in self.tool_registry.get_all()
            if t.execution == execution_type
            and not t.name.startswith(_SKILL_OWNED_TOOL_PREFIXES)
            and t.name not in _BRAIN_ONLY_CLIENT_TOOLS
            # chave do Claude desligada no painel: o classificador nem as vê
            and (t.name not in chave.TOOLS or chave.ligado())
        ]
        if not tools:
            return ""
        lines = []
        for t in tools:
            params_desc = ", ".join(
                f"{name}: {p.type}" for name, p in t.parameters.items()
            )
            # ``router_summary`` quando existir: a description completa ensina o
            # cérebro a USAR a tool (vai como schema da função), enquanto aqui
            # basta RECONHECER — e este texto é pago em toda mensagem.
            summary = t.router_summary or t.description
            lines.append(f"- {t.name}: {summary} ({params_desc})")
        return "\n".join(lines)

    def _skills_brain_note(self) -> str | None:
        """Nota "assuntos com skill do cérebro" gerada pela SkillLibrary.

        None quando a library está ausente/vazia ou a flag está off — o prompt
        fica idêntico ao anterior (comportamento fail-safe).
        """
        library = getattr(self, "skill_library", None)
        if library is None:
            return None
        from shared.config import get_settings

        if not get_settings().skills_router_note_enabled:
            return None
        try:
            return library.router_note()
        except Exception as e:  # noqa: BLE001 — classificação nunca quebra por isso
            logger.warning("[SkillLibrary] router_note falhou: %s", e)
            return None

    def _format_skills(self) -> str:
        """Format registered skills for the system prompt."""
        from core.skills.manifest import SkillManifest
        skills = SkillManifest.all()
        if not skills:
            return ""
        lines = []
        for name, manifest in skills.items():
            lines.append(f"- {name}: {manifest.description} (keywords: {', '.join(manifest.keywords)})")
        return "\n".join(lines)

    def _parse_classification(
        self,
        raw_text: str,
        user_message: str = "",
        conversation_history: list[dict[str, str]] | None = None,
        artifact_paths: dict[str, str] | None = None,
    ) -> RoutingDecision:
        """Parse LLM JSON response into RoutingDecision."""
        text = raw_text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # LLM returned prose instead of JSON — try to extract an embedded JSON object.
            json_text = self._extract_embedded_json(text)
            if json_text:
                try:
                    data = json.loads(json_text)
                    logger.warning("Router: extracted JSON from prose response")
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse router response (bad embedded JSON): {text[:200]}")
                    fallback = self._fallback_classify(user_message or raw_text)
                    self._apply_memory_heuristics(fallback, user_message or raw_text)
                    return fallback
            else:
                logger.warning(f"Failed to parse router response (no JSON found): {text[:200]}")
                fallback = self._fallback_classify(user_message or raw_text)
                self._apply_memory_heuristics(fallback, user_message or raw_text)
                return fallback

        intent_str = data.get("intent", "DIRECT_RESPONSE").upper()
        intent_map = {
            "DIRECT_RESPONSE": IntentType.DIRECT_RESPONSE,
            "COGNITIVE": IntentType.COGNITIVE,
            "SERVER_TOOL": IntentType.SERVER_TOOL,
            "CLIENT_TOOL": IntentType.CLIENT_TOOL,
            "WHATSAPP_MESSAGING": IntentType.WHATSAPP_MESSAGING,
            "WHATSAPP_ORDER": IntentType.WHATSAPP_MESSAGING,
            "HOME_ASSISTANT": IntentType.HOME_ASSISTANT,
            "MUSIC": IntentType.MUSIC,
        }
        intent = intent_map.get(intent_str, IntentType.DIRECT_RESPONSE)

        raw_keywords = data.get("knowledge_keywords")
        knowledge_keywords: list[str] | None = None
        if isinstance(raw_keywords, list):
            knowledge_keywords = [str(k).lower().strip() for k in raw_keywords if k] or None

        pipeline_plan = self._parse_pipeline_plan(data.get("pipeline_plan"), intent_map)

        speech_act = str(data.get("speech_act", "imperative")).lower().strip()
        if speech_act not in _SPEECH_ACTS:
            speech_act = "imperative"

        # Situational-register signals (weak, tone-only — never affect routing).
        gravity = str(data.get("gravity") or "normal").lower().strip()
        if gravity not in ("normal", "alta", "critica"):
            gravity = "normal"
        register_hint = str(data.get("register_hint") or "").lower().strip() or None
        if register_hint not in ("protocolo", "confidente"):
            register_hint = None

        # Workstream A: HA routing carries no execution details — the skill resolves
        # its own entity. Discard anything the model emitted to kill hallucinations.
        tool_name = data.get("tool_name")
        # Lido aqui porque as guardas abaixo zeram `tool_name`.
        pediu_navegador = tool_name == "browser_local_task"
        tool_arguments = data.get("tool_arguments")
        raw_command = data.get("raw_command")
        if intent == IntentType.HOME_ASSISTANT:
            tool_name = None
            tool_arguments = None
            raw_command = None

        # `browser_local_task` só existe no cognitivo, então quem atende é ele,
        # com ou sem plano. A guarda de catálogo logo abaixo não roda quando vem
        # `needs_plan=true`, e aí o planner refazia o pedido do zero a partir da
        # mensagem crua, sem saber da tool. Na sessão d9fae447, "eu limpei o
        # formulario para vc preencher do zero" virou `ls`, `find`,
        # `cat ~/Documents/form.html` e um `python3 -c "...selenium..."` no Mac
        # dele, e a tool certa, com os argumentos certos, foi jogada fora.
        # O sinal `navegador_do_usuario` continua ligado: o cognitivo recebe o
        # aviso de abrir o navegador nesta resposta.
        needs_plan = bool(data.get("needs_plan", False))
        if pediu_navegador and intent in (IntentType.CLIENT_TOOL, IntentType.SERVER_TOOL):
            logger.warning(
                "Router: %s/browser_local_task (needs_plan=%s) — devolvendo ao "
                "cognitivo, que é onde a tool existe",
                intent.value, needs_plan,
            )
            intent = IntentType.DIRECT_RESPONSE
            tool_name = None
            tool_arguments = None
            raw_command = None
            needs_plan = False

        # Falha fechada: o prompt e o catálogo real de client tools podem divergir
        # (uma tool foi removida do catálogo mas o classificador ainda "lembra" dela
        # de exemplos/treino antigos). Despachar um tool_name desconhecido pro desktop
        # trava o turno pra sempre — o client não tem handler e nunca devolve resultado
        # (sessão bd7a304d: read_file inexistente ficou ~2min30s até a conexão cair).
        # Um tool_name fora do catálogo real vira DIRECT_RESPONSE em vez de ser
        # despachado — o núcleo cognitivo assume (tem PKS + NEED_CLARIFICATION).
        if (
            intent == IntentType.CLIENT_TOOL
            and not data.get("needs_plan")
            and tool_name
            and getattr(self, "tool_registry", None) is not None
            and tool_name not in self._valid_client_tool_names()
        ):
            logger.warning(
                "Router: CLIENT_TOOL com tool_name desconhecido %r (fora do catálogo) — "
                "rebaixando para DIRECT_RESPONSE",
                tool_name,
            )
            intent = IntentType.DIRECT_RESPONSE
            tool_name = None
            tool_arguments = None
            raw_command = None

        # A skill de música é HANDOFF-ONLY (core/skills/music/__init__.py): o
        # roteador nunca despacha direto para ela. O classificador, porém, às
        # vezes resolve fazer isso sozinho — "Sim, inicia a minha fila para
        # tocar" saiu como SERVER_TOOL/tool_name="music" (sessão e8170c41,
        # 2026-09-08) e o turno morreu em "Todas as ferramentas falharam":
        # naquele trilho as tools rodam pelo executor GLOBAL, que não tem
        # usuário, então `data_list`/`event_list` recusam ("precisa de um
        # usuário identificado"). Pior: fila e cadastros só existem no
        # cognitivo, que é quem lê a skill.
        if (tool_name or "").strip().lower() == "music" or (
            str(data.get("skill_name") or "").strip().lower() == "music"
        ):
            logger.warning(
                "Router: classificador despachou direto para a skill de música "
                "(intent=%s) — devolvendo ao cognitivo, que é quem lê a fila",
                getattr(intent, "value", intent),
            )
            return self._music_decision(user_message)

        # Assunto de skill do cérebro despachado como SERVER_TOOL: devolve ao
        # cognitivo. O `router_note()` já MANDA o classificador fazer isso ("Pedidos
        # sobre esses assuntos NÃO são CLIENT_TOOL nem SERVER_TOOL"), mas ele lista
        # só uma AMOSTRA de keywords por grupo — em 2026-09-09 o grupo `cadastros`
        # apareceu como "(anexa na playlist, camiseta, caneca, catalogo)", sem
        # nenhuma palavra de fila/música, e "tem uma fila de musica criada né, me
        # mostra o que tem nela" saiu como SERVER_TOOL/`list_connected_devices`
        # ("preciso identificar quais dispositivos estão conectados para verificar a
        # fila", disse o classificador). O agente do SERVER_TOOL então viu `data_list`
        # no registry, chamou, e o turno morreu em "A ferramenta falhou — data_list
        # precisa de um usuário identificado": naquele trilho as executoras
        # user-scoped rodam pelo executor GLOBAL, que nasce sem usuário.
        #
        # Aqui a política vira CÓDIGO em vez de pedido no prompt: se a mensagem casa
        # a keyword de uma skill `tool_backed`, o dono do turno é o cognitivo — que é
        # quem carrega a skill (autoload) e tem o usuário do turno. Sem skill_hint de
        # propósito: o autoload já acha a skill sozinho, e o hint só mexeria no
        # registro de tom.
        if intent == IntentType.SERVER_TOOL:
            casada = self._brain_skill_for(user_message)
            if casada:
                logger.warning(
                    "Router: SERVER_TOOL(%s) para assunto da skill '%s' — devolvendo "
                    "ao cognitivo (é quem carrega a skill e tem o usuário)",
                    tool_name or "agent",
                    casada,
                )
                intent = IntentType.DIRECT_RESPONSE
                tool_name = None
                tool_arguments = None
                raw_command = None

        # Rede do áudio do navegador (ver _BROWSER_AUDIO_COMMAND): qualquer
        # comando improvisado para mexer no som/player de um navegador vira
        # pedido de música — o cognitivo decide e o bloco atua pela extensão.
        _cmd = raw_command or (
            tool_arguments.get("command") if isinstance(tool_arguments, dict) else None
        )
        if (
            intent in (IntentType.CLIENT_TOOL, IntentType.SERVER_TOOL)
            and _cmd
            and _BROWSER_AUDIO_COMMAND.search(str(_cmd))
        ):
            logger.warning(
                "Router: comando improvisado para o áudio do navegador (%r) — "
                "roteando para música (a extensão é quem controla a aba)",
                str(_cmd)[:120],
            )
            return self._music_decision(user_message)

        if (
            intent in (IntentType.CLIENT_TOOL, IntentType.SERVER_TOOL)
            and _cmd
            and _BROWSER_MEDIA_URL_COMMAND.search(str(_cmd))
        ):
            logger.warning(
                "Router: comando improvisado para abrir mídia no navegador (%r) — "
                "devolvendo ao cognitivo (browser_local_task / bloco de música)",
                str(_cmd)[:120],
            )
            intent = IntentType.DIRECT_RESPONSE
            tool_name = None
            tool_arguments = None
            raw_command = None

        # Pergunta sobre a mídia DELE não se responde com print do navegador do
        # SERVIDOR: "ta vendo o video aberto no YouTube?" virou browser_screenshot
        # e a resposta foi "não tem navegador rodando" (sessão 0565907e), com as
        # abas dele no media_context do mesmo turno.
        if (
            intent == IntentType.SERVER_TOOL
            and tool_name in _SERVER_BROWSER_TOOLS
            and any(
                re.search(rf"\b{a}\b", _strip_accents(user_message.lower()))
                for a in _USER_MEDIA_ANCHORS
            )
        ):
            logger.warning(
                "Router: %s para pergunta sobre a mídia do USUÁRIO — "
                "roteando para o media_context (cognitivo)",
                tool_name,
            )
            return self._media_status_decision(user_message)

        # Rede do download inventado (ver _NET_FETCH_COMMAND): baixar da internet
        # pelo shell DELE com uma URL que ninguém disse é palpite, não tarefa de
        # máquina do usuário. Devolve ao cognitivo, que sabe PROCURAR o endereço
        # real (`web_search`) e tem a ponte pro disco dele (`file_save` →
        # `save_generated_file`). De quebra devolve a capacidade de PERGUNTAR:
        # o loop de clarificação só existe no trilho cognitivo, então em
        # CLIENT_TOOL "internet ou disco?" nem era uma pergunta possível.
        if intent == IntentType.CLIENT_TOOL:
            _cmd_net = raw_command or (
                tool_arguments.get("command") if isinstance(tool_arguments, dict) else None
            )
            if _cmd_net and _NET_FETCH_COMMAND.search(str(_cmd_net)):
                _hay = (user_message + "\n" + "\n".join(
                    str(m.get("content", "")) for m in (conversation_history or [])[-8:]
                )).lower()
                _urls_chutadas = _urls_nao_fundamentadas(str(_cmd_net), _hay)
                if _urls_chutadas:
                    logger.warning(
                        "Router: CLIENT_TOOL baixando da internet com URL que ninguém "
                        "citou %r (comando: %r) — devolvendo ao cognitivo, que sabe "
                        "procurar o endereço real e entregar pelo catálogo",
                        _urls_chutadas, str(_cmd_net)[:160],
                    )
                    intent = IntentType.DIRECT_RESPONSE
                    tool_name = None
                    tool_arguments = None
                    raw_command = None
            elif _cmd_net and _URL_IN_COMMAND.search(str(_cmd_net)):
                # Fora de download (`open`, navegador): só barra a URL que não
                # pode ter vindo da conversa — placeholder ou id inventado. A
                # busca montada a partir da fala passa (ver `_url_inventada`).
                _hay = (user_message + "\n" + "\n".join(
                    str(m.get("content", "")) for m in (conversation_history or [])[-8:]
                )).lower()
                _inventadas = [
                    u for u in _urls_nao_fundamentadas(str(_cmd_net), _hay) if _url_inventada(u)
                ]
                if _inventadas:
                    logger.warning(
                        "Router: CLIENT_TOOL abrindo URL inventada %r (comando: %r) — "
                        "devolvendo ao cognitivo, que tem o endereço real no histórico "
                        "ou pergunta",
                        _inventadas, str(_cmd_net)[:160],
                    )
                    intent = IntentType.DIRECT_RESPONSE
                    tool_name = None
                    tool_arguments = None
                    raw_command = None

        # Grounding guard: um comando de shell que cita um caminho de arquivo
        # ESPECÍFICO (com extensão) que não apareceu em lugar nenhum da conversa é
        # quase sempre o classificador inventando um nome plausível em vez de admitir
        # que não sabe (sessão bd7a304d: "relatorio de financas em csv" virou
        # `read_file("/Users/daniel/Desktop/relatorio.csv")` — arquivo que não existia
        # e nunca foi citado — o desktop travou ~2min30s tentando ler algo que não
        # existe). Comandos exploratórios (ls, find, df) não citam extensão → passam
        # direto. Path fundamentado (usuário disse o nome, ou apareceu no histórico,
        # inclusive combinado com uma pasta como "desktop"/"~") → passa direto.
        #
        # Em vez de só desistir pro DIRECT_RESPONSE (que não tem NENHUMA tool pra
        # tocar o disco do usuário — só pode perguntar), força needs_plan=true:
        # o PlanManager (generate_plan) sabe fazer "lista primeiro, decide depois"
        # (depends_on_previous + refine_step/advance_and_refine, docs/... e
        # core/planning/plan_manager.py) — Neo verifica o que existe de verdade em
        # vez de inventar OU de só perguntar. Continua CLIENT_TOOL/run_command;
        # só o argumento chutado é descartado — o plano recomeça do zero a partir
        # da mensagem real do usuário.
        # ⚠️ NÃO reintroduzir `and not data.get("needs_plan")` aqui. A resposta do
        # guard é forçar needs_plan, então parecia redundante rodá-lo quando o
        # classificador já pediu plano — mas o guard faz DUAS coisas, e a outra é a
        # que importa: ele DESCARTA o argumento chutado. Com `needs_plan=true` o
        # comando inventado não é ignorado, ele vira o passo 1 da cascata
        # (`pipeline_plan`) e é despachado literalmente. Sessão d9f9417b: os três
        # turnos vieram com `needs_plan=true`, o guard nunca rodou, e
        # `curl -sL "https://relatorios.exemplo.com.br/$candidato.pdf"` — domínio que não
        # existe — foi parar no Mac do usuário como passo 1. O regex pega URL tão bem
        # quanto caminho de disco (`//relatorios.exemplo.com.br/$candidato.pdf`), só nunca
        # teve a chance.
        needs_plan_forced = False
        if intent == IntentType.CLIENT_TOOL:
            cmd_to_check = raw_command or (
                tool_arguments.get("command") if isinstance(tool_arguments, dict) else None
            )
            if cmd_to_check:
                # Passo 1: caminho de artefato conhecido vira o caminho REAL.
                cmd_to_check, resolved = self._resolve_artifact_paths(
                    cmd_to_check, artifact_paths or {}
                )
                if resolved:
                    if raw_command:
                        raw_command = cmd_to_check
                    elif isinstance(tool_arguments, dict):
                        tool_arguments = {**tool_arguments, "command": cmd_to_check}
                haystack = user_message + "\n" + "\n".join(
                    str(m.get("content", "")) for m in (conversation_history or [])[-8:]
                )
                # Passo 2: o registro é âncora de fundamentação. Arquivo que o
                # próprio Neo baixou não precisa ter sido citado pelo usuário
                # para ser um alvo legítimo — ele é evidência de disco, não chute.
                if artifact_paths:
                    haystack += "\n" + "\n".join(artifact_paths.values())
                ungrounded = self._ungrounded_path_literals(cmd_to_check, haystack)
                if ungrounded:
                    logger.warning(
                        "Router: CLIENT_TOOL com caminho/URL não fundamentado na "
                        "conversa %r (comando: %r) — descartando o argumento e "
                        "forçando plano de verificação (needs_plan)",
                        ungrounded,
                        cmd_to_check,
                    )
                    tool_name = "run_command"
                    tool_arguments = None
                    raw_command = None
                    needs_plan_forced = True

        decision = RoutingDecision(
            intent=intent,
            confidence=float(data.get("confidence", 0.8)),
            tool_name=tool_name,
            navegador_do_usuario=pediu_navegador,
            tool_arguments=tool_arguments,
            explanation=data.get("explanation"),
            requires_approval=data.get("requires_approval", intent == IntentType.CLIENT_TOOL),
            raw_command=raw_command,
            needs_memory=data.get("needs_memory", True),
            knowledge_keywords=knowledge_keywords,
            needs_plan=needs_plan or needs_plan_forced,
            tipo_tarefa=data.get("tipo_tarefa", "simples"),
            onde=_onde(data.get("onde")),
            skill_name=data.get("skill_name"),
            pipeline_plan=pipeline_plan,
            speech_act=speech_act,
            gravity=gravity,
            register_hint=register_hint,
        )
        return self._bias_to_brain(decision)

    def _apply_affect_bias(
        self, decision: RoutingDecision, force_brain: bool
    ) -> RoutingDecision:
        """Desvio por estado emocional — aplicado nos DOIS caminhos do classify.

        Deliberadamente aqui, e não dentro de ``_parse_classification``: aquele
        ponto só é atingido quando o LLM devolveu JSON. Quando o provider cai e a
        decisão vem do ``_fallback_classify`` por palavra-chave (já foram 13 de 46
        turnos num dia ruim), o viés precisa valer igual — senão um problema de
        infra desliga a personalidade em silêncio.

        A skill de casa não conhece o estado emocional do Neo (monta só
        ``neo_persona("slim") + prompt.md``), então com ele irritado quem tem que
        atender o turno é o núcleo cognitivo — que decide se atende, recusa ou
        responde atravessado, e ainda pode acionar a skill via handoff.
        """
        if not force_brain or decision.intent not in _SIDE_EFFECT_SKILL_INTENTS:
            return decision
        logger.info(
            "Intent: %s → DIRECT_RESPONSE (affect-bias; conf=%.2f speech_act=%s)",
            decision.intent.value, decision.confidence, decision.speech_act,
        )
        decision.intent = IntentType.DIRECT_RESPONSE
        decision.skill_name = None
        decision.tool_name = None
        decision.tool_arguments = None
        decision.raw_command = None
        decision.needs_memory = True
        decision.explanation = (
            "Roteado ao núcleo cognitivo: o estado emocional do Neo com este usuário "
            "muda qual é a resposta certa, e a skill não conhece esse estado "
            "(o cérebro decide e pode acionar a skill via handoff)."
        )
        return decision

    def _bias_local_browser(
        self, decision: RoutingDecision, user_message: str, system_context: str | None = None
    ) -> RoutingDecision:
        """Pediu o navegador DELE? Então quem atende é o cognitivo, e ponto.

        A tool ``browser_local_task`` só existe no núcleo cognitivo. Quando o
        roteador manda a mensagem para outro trilho, o portão do §5 nunca roda e
        o pedido cai no navegador HEADLESS do servidor — o usuário fica olhando
        uma tela onde nada acontece. Foi o que houve em 2026-08-28: o
        classificador caiu (OpenRouter 403), o fallback por palavra-chave viu
        "busca" em "deveria ter buscado na barra de busca" e devolveu
        SERVER_TOOL; o turno virou um job no Chromium da VPS que terminou em
        ``about:blank``.

        Vale nos DOIS caminhos do classify pelo mesmo motivo do viés de afeto:
        quando o provider cai, o fallback é justamente onde o erro acontece.
        A âncora é o portão já medido contra o corpus real
        (``scripts/eval_browser_gate.py``: 15 marcações em ~1700 mensagens, zero
        falso positivo), então promover essas poucas ao cérebro é barato.
        """
        from shared.config import get_settings

        if not getattr(get_settings(), "browser_local_enabled", False):
            return decision
        from core.browser_local.conversa import MARCA_DO_BLOCO
        from core.browser_local.gate import local_browser_requested

        tarefa_viva = bool(system_context) and MARCA_DO_BLOCO in system_context
        pediu_local = local_browser_requested(user_message)
        # Pediu com todas as letras: o cognitivo recebe o aviso de abrir o
        # navegador nesta resposta, mesmo quando o classificador não escolheu a
        # tool. Na sessão d9fae447 o pedido dizia "no meu navegador local", o
        # classificador devolveu sem tool e o cérebro leu no caderno a auditoria
        # de OUTRA conversa: respondeu "corrijo as divergências?" em vez de abrir
        # o form. Com tarefa viva não, porque aí o caminho é `job_falar`. E só
        # com ordem: o portão também casa pergunta ("por que meu navegador local
        # travou?"), e o aviso manda abrir o navegador agora.
        if pediu_local and not tarefa_viva and getattr(decision, "speech_act", None) == "imperative":
            decision.navegador_do_usuario = True
        if decision.intent in (IntentType.COGNITIVE, IntentType.DIRECT_RESPONSE):
            return decision

        # Tarefa VIVA no navegador desta conversa e o classificador quer abrir
        # OUTRO navegador: quem fala com a tarefa que está rodando (`job_falar`)
        # é o cognitivo. Sem isto, "abre o AppDemo também" virava um job novo no
        # servidor enquanto a tarefa dele esperava (docs/browser-conversa-plan.md).
        outro_navegador = tarefa_viva and str(
            decision.tool_name or ""
        ) in ("browser_task", "browser_local_task")
        if not (pediu_local or outro_navegador):
            return decision

        logger.info(
            "Intent: %s → COGNITIVE (%s; conf=%.2f)",
            decision.intent.value,
            "tarefa viva no navegador dele" if outro_navegador else "pediu o navegador dele",
            decision.confidence,
        )
        decision.intent = IntentType.COGNITIVE
        decision.skill_name = None
        decision.tool_name = None
        decision.tool_arguments = None
        decision.raw_command = None
        decision.needs_memory = True
        decision.explanation = (
            "Roteado ao núcleo cognitivo: o pedido é para o navegador do próprio "
            "usuário, e a tool que abre a tela dele (browser_local_task) só existe lá."
        )
        return decision

    def _bias_claude_code(
        self, decision: RoutingDecision, user_message: str
    ) -> RoutingDecision:
        """Falou do Claude? Então quem atende é o cognitivo.

        ``claude_task`` mora no cérebro (docs/claude-code-delegation-plan.md §7).
        O desvio que este viés fecha é o do CLIENT_TOOL: "roda o Claude no meu Mac"
        tem cara de `run_command` e viraria um `claude -p` cru no terminal dele,
        com card de aprovação e sem orquestração nenhuma. A âncora é larga de
        propósito — promover ao cérebro uma frase que só CITA o Claude é
        inofensivo, porque o cérebro é o caminho geral.
        """
        if decision.intent in (IntentType.COGNITIVE, IntentType.DIRECT_RESPONSE):
            return decision
        from core.claude_code import chave

        if not chave.ligado():
            return decision
        if not re.search(r"\bclaude\b", _strip_accents((user_message or "").lower())):
            return decision
        logger.info(
            "Intent: %s → COGNITIVE (falou do Claude; conf=%.2f)",
            decision.intent.value, decision.confidence,
        )
        decision.intent = IntentType.COGNITIVE
        decision.skill_name = None
        decision.tool_name = None
        decision.tool_arguments = None
        decision.raw_command = None
        decision.needs_memory = True
        decision.explanation = (
            "Roteado ao núcleo cognitivo: o pedido cita o Claude, e a tool que delega a "
            "ele (claude_task) só existe lá."
        )
        return decision

    def _bias_to_brain(self, decision: RoutingDecision) -> RoutingDecision:
        """Fire a side-effecting skill only on a confident imperative (Workstream B).

        Any conditional/definitional/uncertain request about HA/WhatsApp is routed
        to the cognitive core (DIRECT_RESPONSE) instead — it has the PKS + the strong
        model and can still trigger the skill via [[HANDOFF]]. This turns the open
        set ("catch what should be cognitive") into a closed one ("only let clear
        imperatives through") that fails safe. Read-only paths (SERVER_TOOL/
        CLIENT_TOOL) are untouched — for them a question is the normal trigger.
        """
        from shared.config import get_settings

        settings = get_settings()
        if not getattr(settings, "router_bias_to_brain_enabled", True):
            return decision
        if decision.intent not in _SIDE_EFFECT_SKILL_INTENTS:
            return decision

        threshold = getattr(settings, "router_skill_confidence_threshold", 0.75)
        if decision.speech_act == "imperative" and decision.confidence >= threshold:
            return decision

        logger.info(
            "Intent: %s '%s' (conf=%.2f) → DIRECT_RESPONSE (bias-to-brain)",
            decision.intent.value, decision.speech_act, decision.confidence,
        )
        decision.intent = IntentType.DIRECT_RESPONSE
        decision.skill_name = None
        decision.tool_name = None
        decision.tool_arguments = None
        decision.raw_command = None
        decision.needs_memory = True
        decision.explanation = (
            f"Roteado ao núcleo cognitivo: ato de fala '{decision.speech_act}' não é "
            "imperativo claro para uma skill com efeito colateral (o cérebro decide e "
            "pode acionar via handoff)."
        )
        return decision

    @staticmethod
    def _parse_pipeline_plan(
        raw_plan: Any, intent_map: dict[str, IntentType]
    ) -> list[PipelineStepSpec] | None:
        """Validate a declared cascade (Phase 4). Conservative: needs 2+ valid steps.

        Each entry must map to a known intent and carry a non-empty input. Anything
        malformed is dropped; if fewer than 2 valid steps remain, the plan is
        ignored entirely (falls back to the single classified intent).
        """
        if not isinstance(raw_plan, list) or len(raw_plan) < 2:
            return None
        specs: list[PipelineStepSpec] = []
        for item in raw_plan:
            if not isinstance(item, dict):
                continue
            step_intent = intent_map.get(str(item.get("intent", "")).upper())
            input_text = str(item.get("input") or item.get("input_text") or "").strip()
            if step_intent is None or not input_text:
                continue
            skill_name = item.get("skill_name") or None
            specs.append(
                PipelineStepSpec(
                    intent=step_intent,
                    input_text=input_text,
                    skill_name=skill_name,
                )
            )
        if len(specs) < 2:
            return None
        logger.info("Router: declared cascade with %d steps", len(specs))
        return specs

    @staticmethod
    def _extract_embedded_json(text: str) -> str | None:
        start = text.find("{")
        while start >= 0:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(text)):
                char = text[index]
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:index + 1]
                        if '"intent"' in candidate:
                            return candidate
                        break
            start = text.find("{", start + 1)
        return None

    @staticmethod
    def _finish_reason(response: Any) -> str | None:
        """``finish_reason`` da resposta, quando o provider expõe um.

        Cada provider embrulha a resposta na própria classe; nem todos oferecem
        o campo, e em alguns ele é atributo em vez de método. Defensivo de
        propósito: não saber o motivo nunca pode derrubar a classificação.
        """
        attr = getattr(response, "finish_reason", None)
        try:
            reason = attr() if callable(attr) else attr
        except Exception:  # noqa: BLE001 — provider exótico não quebra o turno
            return None
        return str(reason) if reason else None

    @classmethod
    def _was_truncated(cls, response: Any) -> bool:
        """True quando o modelo parou por falta de orçamento, não por ter acabado."""
        return (cls._finish_reason(response) or "").lower() == "length"

    @classmethod
    def _carries_json(cls, text: str) -> bool:
        """True quando ``text`` traz um objeto JSON de classificação utilizável.

        Mesmo critério que ``_parse_classification`` aplica (JSON direto, cercado por
        markdown, ou embutido em prosa) — usado ANTES do parse para decidir se vale a
        retentativa de reparo em vez de degradar direto pro fallback.
        """
        obj = cls._json_object(text)
        # Um objeto sem `intent` não classifica nada: o parse cairia no padrão
        # DIRECT_RESPONSE calado. É o `{}` vazio de alguns upstreams.
        return obj is not None and "intent" in obj

    @classmethod
    def _is_empty_object(cls, text: str) -> bool:
        """O classificador devolveu um objeto JSON VAZIO (``{}``)?"""
        obj = cls._json_object(text)
        return obj is not None and not obj

    @classmethod
    def _json_object(cls, text: str) -> dict | None:
        """O objeto JSON de ``text`` (direto, cercado por markdown ou no meio de prosa)."""
        stripped = (text or "").strip()
        if not stripped:
            return None
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            embutido = cls._extract_embedded_json(stripped)
            if embutido is None:
                return None
            try:
                obj = json.loads(embutido)
            except json.JSONDecodeError:
                return None
        return obj if isinstance(obj, dict) else None

    def _is_training_message(self, message: str) -> bool:
        """True when the message is an explicit Neo training message."""
        try:
            return _TRAINING_PARSER.parse(message).is_training
        except Exception:  # pragma: no cover - parser is pure, defensive only
            return False

    def _training_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision for a Neo training message (no tools)."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.95,
            explanation="Mensagem de treino do Neo — resposta direta (sem ferramenta)",
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @staticmethod
    def _is_memory_directive(message: str) -> bool:
        """True for explicit 'remember/register in memory' directives."""
        normalized = _strip_accents(message)
        return any(pattern.search(normalized) for pattern in _MEMORY_DIRECTIVE_PATTERNS)

    @staticmethod
    def _history_mentions_device(conversation_history: list[dict[str, str]] | None) -> bool:
        """True when a recent turn was about the user's devices."""
        if not conversation_history:
            return False
        for msg in conversation_history[-4:]:
            normalized = _strip_accents(msg.get("content", ""))
            if any(noun in normalized for noun in _DEVICE_NOUNS):
                return True
        return False

    @classmethod
    def _is_known_devices_query(
        cls,
        message: str,
        conversation_history: list[dict[str, str]] | None = None,
    ) -> bool:
        """True when the user asks about devices Neo *knows* (PKS), not live links.

        Two cases:
          (a) explicit device noun without a live qualifier
              — "quais dispositivos eu tenho", "meus aparelhos";
          (b) a knowledge/correction turn referencing devices, possibly only via
              history — "não, quero os que você tem conhecimento".
        "conectados/online/agora" keeps the message on the SERVER_TOOL path.
        """
        normalized = _strip_accents(message)
        has_device = any(noun in normalized for noun in _DEVICE_NOUNS)
        has_live = any(term in normalized for term in _DEVICE_LIVE_TERMS)
        has_knowledge = any(term in normalized for term in _DEVICE_KNOWLEDGE_TERMS)

        # (a) ownership/listing question, no real-time qualifier.
        if has_device and not has_live:
            return True

        # (b) knowledge or back-reference correction pointing at devices.
        if has_knowledge:
            is_backref = bool(_BACKREF_PATTERN.search(normalized))
            if has_device or (is_backref and cls._history_mentions_device(conversation_history)):
                return True

        return False

    def _known_devices_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision for a known/registered-devices question.

        PKS already loads device entities by type, so DIRECT_RESPONSE with
        needs_memory=True surfaces them without any live-connection tool.
        """
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation="Pergunta sobre dispositivos conhecidos — resposta direta com contexto do PKS",
            needs_memory=True,
            knowledge_keywords=["dispositivo"],
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @staticmethod
    def _is_referential_question(message: str) -> bool:
        """True for an interrogative that points back to an earlier turn."""
        normalized = _strip_accents(message)
        compact = " ".join(normalized.split())
        if not compact or len(compact) > 180:
            return False
        is_question = "?" in compact or compact.startswith(_INTERROGATIVE_STARTS)
        if not is_question:
            return False
        return any(marker in compact for marker in _REFERENTIAL_DEMONSTRATIVES)

    def _referential_question_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision for a referential follow-up question."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.85,
            explanation="Pergunta referencial sobre algo anterior — resposta direta com contexto",
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @staticmethod
    def _is_foreign_assistant_address(message: str) -> bool:
        """True when the user addresses Neo by ANOTHER assistant's name at the start.

        The message is accent-stripped/lowercased and whitespace-collapsed, then
        matched against a vocative anchored at the START ("alexa, ...", "ok google
        ...", "chatgpt ..."). A word boundary keeps "alexandre"/"sirineu" from
        matching. This keeps the turn on the cognitive core so Neo's persona rules
        (pride/offense) fire, instead of a persona-blind skill grabbing the command.
        """
        compact = " ".join(_strip_accents(message).split())
        if not compact:
            return False
        return bool(_FOREIGN_ASSISTANT_ADDRESS.match(compact))

    def _foreign_assistant_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision so the cognitive core applies Neo's persona.

        The behavior rules ("get offended when called Alexa, refuse and mock") come
        from the PKS system context (retrieve_system_context) on this path.
        """
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation=(
                "Usuário endereçou o Neo por nome de outro assistente → núcleo "
                "cognitivo (aplica a persona/regra de comportamento; não fast-path pra skill)"
            ),
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @classmethod
    def _is_brain_reasoning_request(cls, message: str) -> bool:
        """True when a request must go to the cognitive core (DIRECT_RESPONSE).

        Two cases the cheap router otherwise mis-fires as a direct skill call:
          (a) an action CONDITIONED on the user's own habit/preference
              — "se eu costumo, pode ligar", "se eu uso, liga o ar";
          (b) a QUESTION about the user's own preference/habit
              — "como eu gosto de deixar a temperatura à noite?".
        The brain then reads the PKS and decides whether to hand off to a skill.
        """
        normalized = _strip_accents(message)
        compact = " ".join(normalized.split())
        if not compact:
            return False
        # (a) conditional that depends on the user's own habit/preference.
        has_conditional = any(m in compact for m in _CONDITIONAL_MARKERS)
        has_self_habit = any(h in compact for h in _HABIT_SELF_REF)
        if has_conditional and has_self_habit:
            return True
        # (b) explicit question about the user's own preference/habit.
        if "?" in message and _PREFERENCE_QUESTION.search(compact):
            return True
        return False

    def _brain_reasoning_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision so the cognitive core reasons over the PKS.

        It may then emit a [[HANDOFF:skill]] block to run the actual action.
        """
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation=(
                "Ação condicional/baseada em hábito ou pergunta sobre preferência "
                "→ núcleo cognitivo (lê o PKS e decide o handoff)"
            ),
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @classmethod
    def _is_music_request(cls, message: str) -> bool:
        """True when the user is asking to PLAY or CONTROL music (Spotify/YouTube).

        Precision-oriented: requires a play OR control verb AND (a music noun OR a
        service), so "toca no assunto" / "coloca uma roupa" / "para de falar" don't
        match. Misses (e.g. "toca aquela do Queen", bare "pausa") fall through to
        normal classification → still COGNITIVE, which can hand off via the generic
        block. Control verbs (pausa/próxima/...) route here so "pausa a música" never
        improvises a terminal command. See neo-music-feature.
        """
        normalized = _strip_accents(message)
        compact = " ".join(normalized.split())
        if not compact:
            return False
        # Mexer num ARQUIVO de áudio não é tocar nem controlar: o classificador decide.
        if _MUSIC_FILE_WORK.search(compact.lower()):
            return False
        # Unambiguous resume phrases ("dá play de novo", "volta a tocar") → music
        # even without a media noun, so they reach the cognitive music handoff
        # instead of run_command (session-e54f, "Da play de novo" → osascript).
        if any(p in compact for p in _MUSIC_RESUME_PHRASES):
            return True
        # "nao mutou ela" — reclamação de que o controle não pegou.
        if len(compact.split()) <= 8 and _MUSIC_COMPLAINT.search(compact):
            return True
        # Unambiguous control verbs (pausa/pula/play/retoma) refer to the media
        # already playing — anaphoric follow-ups ("pausa aí de novo", "dá um play
        # nela") carry no noun, so route them to music here. See neo-music-feature.
        if cls._token_hits(compact, list(_MUSIC_STRONG_CONTROL_VERBS)) >= 1:
            return True
        has_verb = (
            cls._token_hits(compact, list(_MUSIC_PLAY_VERBS)) >= 1
            or cls._token_hits(compact, list(_MUSIC_CONTROL_VERBS)) >= 1
        )
        if not has_verb:
            return False
        if cls._music_object_near_verb(normalized):
            return True
        # Última camada: a ordem de play que nomeia a FAIXA, não a palavra
        # "música" ("toca duality do slipknot"). Ver _is_music_play_order.
        return cls._is_music_play_order(compact)

    @classmethod
    def _music_object_near_verb(cls, normalized: str) -> bool:
        """O verbo de mídia tem o objeto de mídia PERTO, na mesma frase?

        Antes bastava o verbo e o substantivo aparecerem em qualquer lugar da
        mensagem, e um pedido longo juntava as duas pontas por acaso. Sessões
        41a690ba, f6a0be50 e 294e4ec5 (12/11): "…espera com a ABA aberta. Os
        requisitos… PARA avaliar de verdade…" virou turno de música, e o turno de
        música tira o `browser_local_task` da mesa — o pedido de preencher um
        formulário no navegador dele morreu três vezes em "a extensão está
        offline". Três travas, todas só ESTREITANDO (nada que era não-música vira
        música):

        * a frase: verbo e objeto na mesma frase, o substantivo a poucas palavras;
        * "para" seguido de infinitivo ("para avaliar") é preposição, não "parar";
        * aba/guia só é objeto de mídia com verbo de SOM ("silencia essa guia",
          "abaixa… dessa aba"): "volta para a aba do formulário" é navegação.
        """
        for frase in _MUSIC_SENTENCE.split(normalized):
            palavras = _MUSIC_WORD.findall(frase)
            if not palavras:
                continue
            tem_servico = any(svc in frase for svc in _MUSIC_SERVICES)
            for i, palavra in enumerate(palavras):
                if palavra not in _MUSIC_ANY_VERB:
                    continue
                seguinte = palavras[i + 1] if i + 1 < len(palavras) else ""
                if palavra == "para" and _MUSIC_INFINITIVE.match(seguinte):
                    continue
                if tem_servico:
                    return True
                janela = " ".join(
                    palavras[max(0, i - _MUSIC_NOUN_BEFORE) : i + 1 + _MUSIC_NOUN_AFTER]
                )
                if cls._token_hits(janela, list(_MUSIC_OBJECT_NOUNS)) >= 1:
                    return True
                if palavra in _MUSIC_SOUND_VERBS and cls._token_hits(
                    janela, list(_MUSIC_TAB_NOUNS)
                ) >= 1:
                    return True
        return False

    @classmethod
    def _is_music_play_order(cls, compact: str) -> bool:
        """True para "toca <faixa/artista>" — ordem de play sem palavra de música.

        Recebe o texto já sem acento e com espaços colapsados. Precisão medida
        no corpus real do banco (ver o bloco de constantes acima): 12/12.
        """
        palavras = compact.split()
        if not palavras or len(palavras) > 12:
            return False
        if any(
            re.search(rf"\b{re.escape(obj)}\b", compact)
            for obj in _MUSIC_FOREIGN_OBJECTS
        ):
            return False
        if palavras[0] in _MUSIC_PLAY_IMPERATIVES:
            return not _MUSIC_PLAY_PREPOSITION.match(compact)
        if palavras[0] in _MUSIC_PLAY_AMBIGUOUS:
            return any(cue in compact for cue in _MUSIC_PLAY_CUES)
        return False

    def _brain_skill_for(self, message: str) -> str | None:
        """Nome da skill `tool_backed` cuja keyword casa com a mensagem (ou None).

        Mesmo casamento que o autoload do cognitivo usa — de propósito: o que
        decide aqui tem que ser o MESMO critério que lá na frente carrega a skill,
        senão o turno é desviado para um caminho que não vai ativá-la. Nunca
        levanta: sem library, com library vazia ou com erro, o roteamento segue
        como estava (fail-open).
        """
        library = getattr(self, "skill_library", None)
        if library is None or not message:
            return None
        try:
            casada = library.autoload_for(message)
        except Exception as e:  # noqa: BLE001 — classificação nunca quebra por isso
            logger.warning("[SkillLibrary] autoload_for no router falhou: %s", e)
            return None
        if not casada:
            return None
        entry = casada[0]
        # Só desvia quando este trilho não sabe executar NENHUMA tool da skill.
        # `compras` (meli_search, browser_task) e `imitacao-vozes` (voice_list)
        # têm tool servível aqui — e o browser_task pelo SERVER_TOOL foi conserto
        # anterior (docs: jobs no registry global sem executor). Desviar essas
        # seria desfazer trabalho feito. Já `playlists`/`financas`/`produtos` só
        # têm tool gated: o trilho não tem o que oferecer, e insistir é o bug.
        tools = [str(t) for t in (getattr(entry, "tools", None) or [])]
        if any(server_tool_can_run(t) for t in tools):
            return None
        return getattr(entry, "name", None) or None

    def _music_decision(self, message: str) -> RoutingDecision:
        """Route a music request to the cognitive core with a music handoff hint."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation=(
                "Pedido de tocar música → núcleo cognitivo (lê o PKS — gosto e regras "
                "— e decide o handoff para a skill de música)"
            ),
            needs_memory=True,
            knowledge_keywords=["musica"],
            requires_approval=False,
            needs_plan=False,
            skill_hint="music",
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @classmethod
    def _is_media_status_query(cls, message: str) -> bool:
        """True for a QUESTION about open media tabs / what's playing.

        These are answered by the cognitive core from its injected media_context
        note (the live browser tabs), never by a terminal command. Precision-
        oriented: the tab-listing case requires an interrogative framing so
        imperatives like "fecha as abas abertas" don't get hijacked.
        """
        normalized = _strip_accents(message)
        compact = " ".join(normalized.split())
        if not compact:
            return False

        is_question = (
            "?" in message
            or compact.startswith(_INTERROGATIVE_STARTS)
            or compact.startswith(("quais", "que ", "quantas", "quantos"))
            or compact.startswith(_MEDIA_QUESTION_STARTS)
            or any(v in compact for v in ("mostra", "lista", "me diz", "me fala", "ver as aba", "ver aba"))
        )

        # "quais abas do youtube estão abertas?", "tem aba do youtube aberta?"
        has_tab = any(re.search(rf"\b{t}\b", compact) for t in _MEDIA_TAB_NOUNS)
        has_ctx = any(c in compact for c in _MEDIA_STATUS_CTX)
        if has_tab and has_ctx and is_question:
            return True

        # "ta vendo o video aberto no YouTube?" — pergunta de PERCEPÇÃO, não de
        # listagem, e não pode engolir ordem de tarefa no navegador.
        if (
            _MEDIA_SEEING.search(compact)
            and any(re.search(rf"\b{c}\b", compact) for c in _MEDIA_SEEING_CTX)
            and not _BROWSER_ACTION_VERBS.search(compact)
        ):
            return True

        # "o que tá tocando?", "que música está tocando agora?"
        if _MEDIA_PLAYING_QUERY.search(compact):
            return True

        # "como tá o YouTube?", "status do YouTube/player/navegador"
        # A mesma exclusão de ordem-de-tarefa: "acessar pelo menu navegador local
        # a amazon e veja qual o ULTIMO STATUS na entrega" é tarefa de navegador,
        # e casava aqui só por ter "status" + "navegador" (FP que já existia).
        if (
            ("status" in compact or "como esta" in compact or "como ta " in compact)
            and any(
                s in compact
                for s in ("youtube", "musica", "player", "spotify", "navegador")
            )
            and not _BROWSER_ACTION_VERBS.search(compact)
        ):
            return True

        return False

    def _media_status_decision(self, message: str) -> RoutingDecision:
        """Route a media-status question to the cognitive core (media_context note)."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.9,
            explanation=(
                "Pergunta sobre abas/mídia aberta → núcleo cognitivo (lê o "
                "media_context em tempo real e responde, sem comando de terminal)"
            ),
            needs_memory=False,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    def _memory_decision(self, message: str) -> RoutingDecision:
        """Direct-response decision for an explicit memory directive (no tools)."""
        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.95,
            explanation="Diretiva de memória — resposta direta; gravação via pipeline do PKS",
            needs_memory=True,
            requires_approval=False,
            needs_plan=False,
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    @staticmethod
    def _token_hits(text: str, tokens: list[str]) -> int:
        """Count tokens present as whole words (avoids 'disco' in 'discordar')."""
        return sum(1 for t in tokens if re.search(rf"\b{re.escape(t)}\b", text))

    def _fallback_classify(self, message: str) -> RoutingDecision:
        """Keyword-based fallback when LLM is unavailable."""
        # Training messages are never a tool call, regardless of keywords.
        if self._is_training_message(message):
            return self._training_decision(message)

        # Memory directives are never a tool call (they go to the PKS pipeline).
        if self._is_memory_directive(message):
            return self._memory_decision(message)

        # Known/registered-devices questions are a PKS query, not a live tool.
        if self._is_known_devices_query(message):
            return self._known_devices_decision(message)

        # Referential follow-up questions stay on DIRECT_RESPONSE.
        if self._is_referential_question(message):
            return self._referential_question_decision(message)

        # Addressed by another assistant's name → cognitive core (persona rules).
        if self._is_foreign_assistant_address(message):
            return self._foreign_assistant_decision(message)

        # Conditional/habit-based actions and preference questions → cognitive core.
        if self._is_brain_reasoning_request(message):
            return self._brain_reasoning_decision(message)

        # Media-status questions → cognitive core (answered from media_context note).
        if self._is_media_status_query(message):
            return self._media_status_decision(message)

        # Music requests → cognitive core (handoff to the music skill).
        if self._is_music_request(message):
            return self._music_decision(message)

        text = (message or "").lower()
        # Normalize accents
        for src, dst in {
            "á": "a", "à": "a", "â": "a", "ã": "a",
            "é": "e", "ê": "e", "í": "i",
            "ó": "o", "ô": "o", "õ": "o",
            "ú": "u", "ç": "c",
        }.items():
            text = text.replace(src, dst)

        # Client-side indicators
        client_tokens = [
            "arquivos", "files", "pastas", "folders", "diretorio", "directory",
            "downloads", "desktop", "home", "disco", "disk", "espaco", "space",
            "tamanho", "size", "processos", "processes", "versao", "version",
            "conteudo", "listar", "lista", "mostra", "terminal", "comando",
        ]
        # Multi-step file task: "leia ... e crie" or "ler ... escrever"
        file_read_write = (
            ("leia" in text or "ler" in text or "leia" in text or "read" in text)
            and ("crie" in text or "escreva" in text or "criar" in text or "write" in text or "salve" in text)
        )
        if file_read_write:
            return RoutingDecision(
                intent=IntentType.CLIENT_TOOL,
                confidence=0.7,
                explanation="Fallback: leitura + escrita de arquivos detectada",
                requires_approval=False,
                needs_plan=True,
            )
        if self._token_hits(text, client_tokens) >= 2:
            return RoutingDecision(
                intent=IntentType.CLIENT_TOOL,
                confidence=0.6,
                raw_command='ls -la "$HOME"',
                explanation="Fallback: detectado pedido de operação local",
                requires_approval=True,
            )

        # Home Assistant indicators (smart-home control/query). Checked before
        # the weather/server block so "qual a temperatura do termostato" and
        # control phrases ("acende a luz") don't fall into weather.
        ha_strong_nouns = [
            "luz", "luzes", "lampada", "tomada", "interruptor", "termostato",
            "persiana", "cortina", "cena", "cinema", "home assistant", "automacao", "script",
        ]
        ha_control_verbs = [
            "acende", "acender", "apaga", "apagar", "liga", "ligar",
            "desliga", "desligar", "ativa", "ativar", "abre", "abrir",
            "fecha", "fechar", "ajusta", "ajustar",
        ]
        ha_rooms = ["sala", "quarto", "cozinha", "garagem", "casa", "banheiro", "escritorio"]
        has_ha_noun = self._token_hits(text, ha_strong_nouns) >= 1
        has_ha_verb = self._token_hits(text, ha_control_verbs) >= 1
        has_room = self._token_hits(text, ha_rooms) >= 1
        if has_ha_noun or (has_ha_verb and has_room):
            return RoutingDecision(
                intent=IntentType.HOME_ASSISTANT,
                confidence=0.6,
                skill_name="home_assistant",
                explanation="Fallback: detectado controle/consulta da casa inteligente (Home Assistant)",
            )

        # Server-side indicators
        server_tokens = [
            "clima", "tempo", "weather", "temperatura", "previsao",
            "cotacao", "dolar", "bitcoin", "busca", "search", "pesquisa",
        ]
        if self._token_hits(text, server_tokens) >= 1:
            return RoutingDecision(
                intent=IntentType.SERVER_TOOL,
                confidence=0.5,
                explanation="Fallback: detectado pedido de dados externos",
            )

        # WhatsApp indicators
        whatsapp_tokens = [
            "whatsapp", "wpp", "zap", "mensagem", "mensagens",
            "manda", "mandar", "envia", "enviar", "texto",
            "contato", "telefone", "celular",
        ]
        if self._token_hits(text, whatsapp_tokens) >= 1:
            return RoutingDecision(
                intent=IntentType.WHATSAPP_MESSAGING,
                confidence=0.7,
                skill_name="whatsapp_messaging",
                explanation="Fallback: detectado pedido via WhatsApp",
            )

        decision = RoutingDecision(
            intent=IntentType.DIRECT_RESPONSE,
            confidence=0.5,
            explanation="Fallback: resposta direta",
        )
        self._apply_memory_heuristics(decision, message)
        return decision

    def _reject_prompt_echo(
        self, decision: RoutingDecision, user_message: str
    ) -> RoutingDecision:
        """Classificação que carrega o NOSSO prompt como conteúdo não vira ação.

        Determinística e sem LLM, no molde das guardas do núcleo cognitivo. Só
        atua quando o payload ecoa instrução nossa E o usuário não a escreveu —
        perguntar "o que significa 'você é um classificador'?" continua passando.

        Cai no cognitivo em vez de levantar: o turno tem que sobreviver, e o
        cérebro lê o pedido de verdade. O que não pode é a instrução chegar numa
        skill que fala com o mundo — foi assim que o prompt do Neo virou rascunho
        de WhatsApp para um contato real (sessão 289b6c29, 2026-09-07).
        """
        payload = (decision.tool_arguments, decision.raw_command)
        if not _echoes_own_prompt(payload):
            return decision
        if _echoes_own_prompt(user_message):
            return decision  # o usuário falou disso de propósito; não é eco nosso

        logger.error(
            "[Router] classificação DESCARTADA: o payload ecoa o prompt do próprio "
            "Neo (intent=%s tool=%s skill=%s) — caindo no cognitivo. args=%.200s",
            decision.intent, decision.tool_name, decision.skill_name,
            str(decision.tool_arguments),
        )
        decision.intent = IntentType.DIRECT_RESPONSE
        decision.tool_name = None
        decision.tool_arguments = None
        decision.raw_command = None
        decision.skill_name = None
        decision.skill_hint = None
        decision.requires_approval = False
        decision.explanation = (
            "classificação descartada: o conteúdo proposto era instrução interna do "
            "Neo, não algo que o usuário pediu"
        )
        return decision

    @staticmethod
    def _reject_classifier_file_authoring(
        decision: RoutingDecision, user_message: str
    ) -> RoutingDecision:
        """O classificador não redige arquivo nem decide sobrescrever.

        Sessão 30b1e484: "atualiza o arquivo .md que fica em /Users/…" virou
        ``write_file`` com um ``content`` que o classificador (modelo flash, sem
        ter lido o arquivo) INVENTOU do zero — 3 turnos seguidos, cada um com um
        texto diferente, apagando tabelas e seções do original. Só não destruiu o
        arquivo porque o cliente devolveu ``conflict``. E "atualizou o arquivo
        mesmo?" — uma PERGUNTA — virou ``save_generated_file`` com
        ``overwrite=true`` apontando o anexo antigo.

        Teste por CONTEÚDO, em código (o prompt já dizia "se é preciso GERAR o
        conteúdo, é DIRECT_RESPONSE" e foi ignorado):

        * ``write_file`` só fica com o classificador quando o texto está LITERAL
          na mensagem do usuário (ditado). Editar/gerar é do cérebro, que lê a
          versão atual antes de gravar;
        * pergunta nunca grava — verificar é do cérebro;
        * ``overwrite`` sai sempre: é decisão de quem viu o arquivo.
        """
        if decision.intent is not IntentType.CLIENT_TOOL:
            return decision
        tool = decision.tool_name
        args = decision.tool_arguments if isinstance(decision.tool_arguments, dict) else {}

        motivo = None
        if _DOWNLOAD_BUTTON_RE.search(user_message or ""):
            motivo = "botão de download é file_offer/file_save do núcleo cognitivo, não comando na máquina"
        elif tool in (None, "run_command"):
            comando = str(decision.raw_command or args.get("command") or "")
            if comando and _SHELL_WRITES_FILE_RE.search(comando) and not _text_is_dictated(
                _written_text(comando), user_message
            ):
                motivo = (
                    "comando que GRAVA texto no disco do usuário com conteúdo não ditado — "
                    "redigir é do núcleo cognitivo"
                )
        if motivo is None and tool not in ("write_file", "save_generated_file"):
            return decision

        if motivo is not None:
            pass
        elif decision.speech_act == "question":
            motivo = "pergunta não grava arquivo — verificar é do núcleo cognitivo"
        elif tool == "write_file":
            content = args.get("content")
            if not isinstance(content, str) or not _text_is_dictated(content, user_message):
                motivo = (
                    "o conteúdo do arquivo não foi ditado pelo usuário — editar ou redigir "
                    "é do núcleo cognitivo, que lê a versão atual antes de gravar"
                )
        elif tool == "save_generated_file" and _CONTENT_CHANGE_RE.search(user_message or ""):
            motivo = (
                "o pedido muda o CONTEÚDO do arquivo antes de entregar — é do núcleo "
                "cognitivo, que sabe qual é a versão atual e o que acrescentar"
            )

        if motivo is None:
            if "overwrite" in args:
                decision.tool_arguments = {k: v for k, v in args.items() if k != "overwrite"}
            return decision

        logger.warning(
            "[Router] CLIENT_TOOL %s descartado (%s) — caindo no cognitivo. msg=%.120s",
            tool, motivo, user_message,
        )
        decision.intent = IntentType.DIRECT_RESPONSE
        decision.tool_name = None
        decision.tool_arguments = None
        decision.raw_command = None
        decision.skill_name = None
        decision.requires_approval = False
        decision.needs_memory = True
        decision.explanation = f"classificação descartada: {motivo}"
        return decision

    @staticmethod
    def _reject_recall_shortcut(
        decision: RoutingDecision, user_message: str
    ) -> RoutingDecision:
        """Pedido de recall não vira entrega de arquivo pelo classificador.

        Sessão 7330e40c: "pega a última reanálise completa que fizemos e coloca
        tudo em um arquivo .md" virou ``save_generated_file`` apontando um
        ``resumo_fichas.md`` de OUTRA conversa, com confiança 0,95 e uma
        explicação inventada ("o histórico mostra que este arquivo já existe
        lá"). O cérebro — o único que sabe chamar `conversation_recall` — nunca
        rodou. Sete turnos depois o Neo entregou a análise ERRADA num arquivo e
        afirmou que era a mensagem que ele tinha postado no chat.

        A referência para trás é a única pista confiável aqui: o classificador
        vê no máximo 6 mensagens de histórico, então ele não tem como saber se a
        "última reanálise" é um arquivo do catálogo ou um texto de 11 turnos
        atrás. Quem sabe é quem pode buscar. Entregar arquivo continua valendo —
        só depois de o cérebro recuperar o conteúdo certo.
        """
        if decision.intent is not IntentType.CLIENT_TOOL:
            return decision
        if decision.tool_name not in ("save_generated_file", "write_file"):
            return decision
        if not is_conversation_recall_request(user_message):
            return decision

        logger.warning(
            "[Router] CLIENT_TOOL %s descartado (pedido se refere a algo dito antes — "
            "só o cognitivo sabe buscar no arquivo de conversas). msg=%.120s",
            decision.tool_name, user_message,
        )
        decision.intent = IntentType.DIRECT_RESPONSE
        decision.tool_name = None
        decision.tool_arguments = None
        decision.raw_command = None
        decision.skill_name = None
        decision.requires_approval = False
        decision.needs_memory = True
        decision.explanation = (
            "classificação descartada: o pedido se refere a algo dito antes na "
            "conversa — o núcleo cognitivo busca o conteúdo real antes de entregar"
        )
        return decision

    def _reject_ungrounded_recipient(
        self,
        decision: RoutingDecision,
        user_message: str,
        conversation_history: list[dict[str, str]] | None,
    ) -> RoutingDecision:
        """WhatsApp para alguém que o usuário nunca citou não é pedido dele.

        A REGRA DURA do prompt exige ``speech_act=imperative`` e alta confiança —
        e as duas passaram no incidente da sessão 289b6c29, porque "PARE." É um
        imperativo e o modelo estava 95% confiante. É o formato errado de teste:
        ele descreve como a frase foi CONJUGADA, não se ela é sobre mandar
        mensagem. Toda ordem é imperativa.

        O teste que faltava é sobre CONTEÚDO, e roda em código, não no prompt: o
        destinatário tem que existir na conversa. Sem isso, o classificador
        podia inventar a pessoa (`recipient="minha mãe"` num turno sobre uma
        playlist de rock), o pipeline resolvia o apelido numa agenda de verdade
        e montava o rascunho para um número de verdade.

        Olha a mensagem E o histórico recente (a mesma janela que o
        classificador vê), porque "manda: chego 19h" depois de combinar o
        contato é legítimo e não repete o nome.
        """
        if decision.intent is not IntentType.WHATSAPP_MESSAGING:
            return decision
        args = decision.tool_arguments or {}
        recipient = args.get("recipient") or args.get("contact") or args.get("to")
        haystack = user_message or ""
        for msg in (conversation_history or [])[-6:]:
            haystack += " " + str(msg.get("content") or "")
        if _recipient_is_grounded(recipient, haystack):
            return decision

        logger.error(
            "[Router] WHATSAPP_MESSAGING DESCARTADO: destinatário %r não aparece na "
            "mensagem nem no histórico recente — caindo no cognitivo. msg=%.120s",
            recipient, user_message,
        )
        decision.intent = IntentType.DIRECT_RESPONSE
        decision.tool_name = None
        decision.tool_arguments = None
        decision.skill_name = None
        decision.requires_approval = False
        decision.explanation = (
            f"classificação descartada: o destinatário {recipient!r} não foi citado "
            "pelo usuário nesta conversa"
        )
        return decision

    def _apply_memory_heuristics(self, decision: RoutingDecision, message: str) -> None:
        """Backend guardrail: factual personal messages must not lose memory flags."""
        text = (message or "").lower()
        normalized = text
        for src, dst in {
            "á": "a", "à": "a", "â": "a", "ã": "a",
            "é": "e", "ê": "e", "í": "i",
            "ó": "o", "ô": "o", "õ": "o",
            "ú": "u", "ç": "c",
        }.items():
            normalized = normalized.replace(src, dst)

        memory_terms = {
            "academia": "academia",
            "treino": "treino",
            "exercicio": "exercicio",
            "exercicios": "exercicio",
            "supino": "supino",
            "crucifixo": "crucifixo",
            "crusifixo": "crucifixo",
            "triceps": "triceps",
            "biceps": "biceps",
            "peito": "peito",
            "costa": "costas",
            "costas": "costas",
            "perna": "perna",
            "ombro": "ombro",
            "reps": "reps",
            "repeticoes": "reps",
            "sessoes": "series",
            "series": "series",
            "quilos": "peso",
            "kg": "peso",
            "meu nome": "nome",
            "menu nome": "nome",
            "eu me chamo": "nome",
            "meu pc": "pc",
            "minha irma": "familia",
            "dispositivo": "dispositivo",
            "device": "dispositivo",
            "aparelho": "dispositivo",
            "equipamento": "dispositivo",
        }
        weekdays = ("segunda", "terca", "terça", "quarta", "quinta", "sexta", "sabado", "sábado", "domingo")
        has_numeric_training = bool(re.search(r"\b\d+\s*(kg|quilo|quilos|reps?|repeticoes|repetições|sessoes|sessões|series|séries)\b", normalized))
        detected = [kw for token, kw in memory_terms.items() if token in normalized]
        if any(day in normalized for day in weekdays):
            detected.extend(["rotina", "treino"])
        if has_numeric_training:
            detected.extend(["exercicio", "peso", "series", "reps"])

        if not detected:
            return

        decision.needs_memory = True
        existing = list(decision.knowledge_keywords or [])
        for kw in detected:
            if kw not in existing:
                existing.append(kw)
        decision.knowledge_keywords = existing[:6] or None
        if decision.intent == IntentType.DIRECT_RESPONSE and decision.confidence < 0.65:
            decision.confidence = 0.65
