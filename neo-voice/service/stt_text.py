"""
stt_text.py -- normalizacao do texto que SAI do ASR, antes de qualquer consumidor.

Espelho do tts_text.py, na direcao inversa. Problema que resolve: o Parakeet nunca
escreve o nome do assistente certo. Medido no corpus de producao (368 turnos de voz,
02/08/2026): "neo" apareceu ZERO vezes; o que aparece e "neil" (6x), "new" (6x),
"nil" (3x), "knew" (2x), "nir", "bonio". O loop fecha com o TTS: o tts_text pronuncia
"níu" de proposito (/niw/), o usuario fala "níu" e o ASR mapeia esse som para o ingles
"new". Os dois lados estao coerentes -- faltava o corretor.

Alem disso o parakeet-tdt-0.6b-v3 e multilingue e AUTODETECTA o idioma (o `language=`
do onnx-asr so vale p/ Whisper e Canary), entao enunciado curto vira ingles/frances
inteiro -- "He knew", "A new", "And you", "How", "Hey Live" sao, quase sempre, eco do
wake word "Ei Neo" e nao um turno de verdade.

Camadas (nesta ordem):
  1. eco do wake word / grunhido -> descarta o enunciado inteiro (lista FECHADA)
  2. vocativo forte  (neil/nil/nir/niu/nio/knew/...) -> "Neo" em posicao ancorada
  3. vocativo fraco  (new/any/...) -> "Neo" so com contexto explicito de chamamento
  4. fonetica        token curto comecando com n, chave a distancia <=1 de "niu",
                     em posicao de vocativo e fora da keep-list -> "Neo"
  5. lexico de dominio (frases coladas: "desligaliz" -> "desliga a luz")

Precisao acima de cobertura: na duvida NAO mexe -- quem tolera o resto e a instrucao
de entrada por voz no neo-core (docs/voice-input-normalization-plan.md, camada 3).
O texto original NUNCA e perdido: volta em `raw_text` no WS e em `asr_raw` no metadata.

Config (env):
  NEO_STT_NORMALIZE      default 1  -- kill switch geral
  NEO_STT_DROP_ECHO      default 1  -- descartar eco do wake word/grunhido
  NEO_STT_ASSISTANT_NAME default "Neo"
  NEO_STT_LEXICON        JSON {"variante": "Neo", "desligaliz": "desliga a luz"}
  NEO_STT_LEXICON_KEEP   JSON ["Neil", ...] -- palavras que NUNCA sao corrigidas
                         (nome de gente de verdade; vence tudo, inclusive o lexico)

Sem dependencias externas (so stdlib) -- o modulo tem que rodar tambem no teste.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field

log = logging.getLogger("neo-voice.stttext")

ASSISTANT_NAME = os.environ.get("NEO_STT_ASSISTANT_NAME", "Neo")


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# listas base (medidas no corpus; ver §0 do plano)
# ---------------------------------------------------------------------------
# Variantes FORTES: nao sao palavra de uso normal em pt (nem em ingles corrente no
# meio de uma frase em pt). Corrigidas em posicao ancorada -- inicio do enunciado,
# colada a virgula/ponto, ou depois de uma palavra de chamamento.
_STRONG_VARIANTS = {
    "neil", "niel", "nil", "nir", "niu", "nio", "neu", "neo",
    "knew", "neal", "nell", "niw", "nieu", "nihil",
}

# Variantes FRACAS: sao palavras de verdade (ingles). So viram "Neo" com contexto
# EXPLICITO de chamamento ("alô new", "testando a new", ", new,").
_WEAK_VARIANTS = {"new", "any", "knew", "anu"}

# Palavras de chamamento que ancoram o vocativo. Sem "e"/"aí": sao conectivo em
# pt ("É no YouTube" virava "É Neo YouTube"), e o caso real "E aí, Neil," ja e
# coberto pela virgula.
_LEAD_WORDS = {
    "ei", "oi", "ola", "alo", "opa", "hey", "hei", "hi", "hello",
    "fala", "fale", "escuta", "escute", "olha", "olhe", "diz", "diga",
    "testando", "teste", "testa", "testar",
}

# Determinantes que o ASR intercala antes do nome ("testando A new").
_DETERMINERS = {"a", "o", "um", "uma", "the"}

# Palavras pt curtas comecando com "n" que a camada fonetica NAO pode tocar.
_PT_KEEP_DEFAULT = {
    "na", "nao", "nada", "ne", "nem", "nela", "nele", "nelas", "neles",
    "nessa", "nesse", "nesta", "neste", "nessas", "nesses", "nestas", "nestes",
    "no", "nos", "nossa", "nosso", "nova", "novo", "nove", "novas", "novos",
    "num", "numa", "nunca", "nuvem", "nivel", "ninguem", "noite", "nome",
    "norte", "nota", "neca", "null", "none",
}

# Enunciado INTEIRO que e eco do wake word "Ei Neo" (o ASR ouve em ingles) ou
# grunhido. Lista FECHADA: so descarta o que casar exatamente, ate 3 palavras.
_WAKE_ECHO_DEFAULT = {
    # eco do wake word
    "he knew", "a new", "and you", "any", "anu", "how", "hey live", "hei live",
    "come by", "her best", "new", "knew", "a new one", "he new", "and new",
    "in you", "on you", "a knew", "hey new", "hey knew", "i knew", "you knew",
    # grunhido/ruido de fundo que abre turno a toa
    "mm", "mmm", "mm-hmm", "mmhmm", "hm", "hmm", "uh", "uh-huh", "ah", "eh",
    "hum", "humm", "uhum", "ahn", "hein",
}

# Frases coladas que o ASR repete (Classe C previsivel). So o que o corpus provou.
_PHRASE_LEXICON_DEFAULT = {
    "desligaliz": "desliga a luz",
    "desligalis": "desliga a luz",
    "ligaliz": "liga a luz",
    "ligalis": "liga a luz",
}


# ---------------------------------------------------------------------------
# config em runtime (env + arquivo de estado, igual ao /stt/fallback)
# ---------------------------------------------------------------------------
def _env_json(name: str):
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        log.warning("%s invalido (%s) — ignorado", name, e)
        return None


_extra_lexicon: dict[str, str] = {}   # variante -> substituicao (do env/painel)
_keep_words: set[str] = set()         # palavras intocaveis (do env/painel)


def _reload_config() -> None:
    """(Re)le lexico e keep-list do env. Chamado no import e pelo /stt/lexicon."""
    global _extra_lexicon, _keep_words
    lex = {}
    raw = _env_json("NEO_STT_LEXICON")
    if isinstance(raw, dict):
        lex = {_fold(str(k)): str(v) for k, v in raw.items() if str(k).strip()}
    elif raw is not None:
        log.warning("NEO_STT_LEXICON nao e um objeto JSON — ignorado")
    keep = set()
    raw_keep = _env_json("NEO_STT_LEXICON_KEEP")
    if isinstance(raw_keep, list):
        keep = {_fold(str(w)) for w in raw_keep if str(w).strip()}
    elif raw_keep is not None:
        log.warning("NEO_STT_LEXICON_KEEP nao e uma lista JSON — ignorado")
    _extra_lexicon, _keep_words = lex, keep


def set_runtime_config(lexicon: dict | None = None, keep: list | None = None) -> None:
    """Override em runtime (endpoint /stt/lexicon). `None` mantem o valor atual."""
    global _extra_lexicon, _keep_words
    if lexicon is not None:
        _extra_lexicon = {_fold(str(k)): str(v) for k, v in lexicon.items() if str(k).strip()}
    if keep is not None:
        _keep_words = {_fold(str(w)) for w in keep if str(w).strip()}


def get_runtime_config() -> dict:
    """Estado atual (lido pelo /stt/lexicon e pelo neo-panel)."""
    return {
        "enabled": _flag("NEO_STT_NORMALIZE"),
        "drop_echo": _flag("NEO_STT_DROP_ECHO"),
        "assistant_name": ASSISTANT_NAME,
        "strong_variants": sorted(_STRONG_VARIANTS),
        "weak_variants": sorted(_WEAK_VARIANTS),
        "wake_echo": sorted(_WAKE_ECHO_DEFAULT),
        "phrase_lexicon": dict(_PHRASE_LEXICON_DEFAULT),
        "extra_lexicon": dict(_extra_lexicon),
        "keep": sorted(_keep_words),
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _fold(s: str) -> str:
    """minusculas + sem acento (comparacao insensivel a como o ASR pontuou)."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


def _phonetic_key(word: str) -> str:
    """Chave fonetica pt-BR grosseira, so o suficiente p/ aproximar 'neo'.

    knew -> niu | new -> niu | neil -> nil | nio -> niu | neo -> niu
    """
    w = _fold(word)
    w = re.sub(r"[^a-z]", "", w)
    if w.startswith("kn"):
        w = w[1:]
    w = w.replace("h", "").replace("w", "u").replace("y", "i")
    w = w.replace("e", "i")          # 'e' atono ~ 'i' (neo -> nio)
    w = re.sub(r"(.)\1+", r"\1", w)  # colapsa repetidas (niil -> nil)
    if w.endswith("o"):              # 'o' final atono ~ 'u' (nio -> niu)
        w = w[:-1] + "u"
    return w


_TARGET_KEY = _phonetic_key("neo")  # "niu"


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ]+(?:['’][A-Za-zÀ-ÿ]+)?")
_PUNCT_ANCHOR = ",.!?;:…"


@dataclass
class Normalized:
    """Resultado da normalizacao. `text` vazio + `dropped` = enunciado descartado."""
    text: str
    raw: str
    fixes: list[dict] = field(default_factory=list)
    dropped: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.fixes) or self.dropped is not None


# ---------------------------------------------------------------------------
# camada 1 -- eco do wake word / grunhido
# ---------------------------------------------------------------------------
def _is_wake_echo(text: str) -> bool:
    stripped = _fold(text).strip(" \t\n" + _PUNCT_ANCHOR + "\"'")
    stripped = re.sub(r"[.,!?;:]+", " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if not stripped or len(stripped.split()) > 3:
        return False
    return stripped in _WAKE_ECHO_DEFAULT


# ---------------------------------------------------------------------------
# camadas 2-4 -- o nome
# ---------------------------------------------------------------------------
def _prev_word(tokens: list[re.Match], i: int) -> str:
    return _fold(tokens[i - 1].group(0)) if i > 0 else ""


def _anchor_ctx(text: str, tokens: list[re.Match], i: int) -> dict:
    """Contexto de ancoragem do token i (o que vem colado antes/depois)."""
    m = tokens[i]
    before = text[:m.start()].rstrip()
    after = text[m.end():].lstrip()
    return {
        "at_start": not before,
        "after_punct": bool(before) and before[-1] in _PUNCT_ANCHOR,
        "before_punct": (not after) or after[0] in _PUNCT_ANCHOR,
        "at_end": not after,
        "prev": _prev_word(tokens, i),
        "prev2": _prev_word(tokens, i - 1) if i >= 2 else "",
    }


def _strong_anchored(ctx: dict) -> bool:
    """Vocativo forte: inicio de enunciado, depois de pontuacao, ou apos chamamento.

    `before_punct` sozinho NAO ancora de proposito: "por que isso retorna nil?" e
    conversa de dev, nao chamamento (e o usuario e dev). Todos os casos reais do
    corpus caem em at_start ou after_punct.
    """
    return bool(ctx["at_start"] or ctx["after_punct"] or ctx["prev"] in _LEAD_WORDS)


def _weak_anchored(ctx: dict) -> bool:
    """Vocativo fraco: precisa de chamamento explicito ou isolamento por virgula."""
    if ctx["prev"] in _LEAD_WORDS:
        return True
    if ctx["prev"] in _DETERMINERS and ctx["prev2"] in _LEAD_WORDS:
        return True
    return bool(ctx["after_punct"] and ctx["before_punct"])


def _phonetically_near(word: str) -> bool:
    """Aproximacao fonetica de "neo" -- a camada que pega variante nao catalogada.

    A keep-list de pt e OBRIGATORIA aqui: "não" -> chave "nau" e "no" -> "nu" ficam
    a distancia 1 de "niu". Sem ela, o corretor troca todo "né?" por "Neo?" (medido:
    14 falsos positivos em 368 turnos na primeira rodada).
    """
    w = _fold(word)
    if len(w) > 5 or len(w) < 2:
        return False
    if w in _PT_KEEP_DEFAULT or w in _keep_words:
        return False
    if not (w.startswith("n") or w.startswith("kn")):
        return False
    return _edit_distance(_phonetic_key(w), _TARGET_KEY) <= 1


def _fix_name(text: str, fixes: list[dict]) -> str:
    """Aplica as camadas 2-4 token a token (uma passada, sem reentrancia)."""
    tokens = list(_WORD_RE.finditer(text))
    out: list[str] = []
    last = 0
    for i, m in enumerate(tokens):
        word = m.group(0)
        folded = _fold(word)
        if folded in _keep_words:            # keep-list vence tudo
            continue
        if folded == _fold(ASSISTANT_NAME):  # ja esta certo
            continue
        ctx = None
        rule = None
        if folded in _extra_lexicon:
            rule = "lexicon-user"
        elif folded in _STRONG_VARIANTS:
            ctx = _anchor_ctx(text, tokens, i)
            rule = "vocative-strong" if _strong_anchored(ctx) else None
        elif folded in _WEAK_VARIANTS:
            ctx = _anchor_ctx(text, tokens, i)
            rule = "vocative-weak" if _weak_anchored(ctx) else None
        elif _phonetically_near(word):
            ctx = _anchor_ctx(text, tokens, i)
            rule = "phonetic" if _weak_anchored(ctx) else None
        if not rule:
            continue
        repl = _extra_lexicon.get(folded, ASSISTANT_NAME) if rule == "lexicon-user" else ASSISTANT_NAME
        out.append(text[last:m.start()])
        out.append(repl)
        last = m.end()
        fixes.append({"from": word, "to": repl, "rule": rule})
    out.append(text[last:])
    return "".join(out)


# ---------------------------------------------------------------------------
# camada 5 -- frases coladas
# ---------------------------------------------------------------------------
_PHRASE_RES = [(re.compile(r"\b" + re.escape(k) + r"\b", re.IGNORECASE), v)
               for k, v in _PHRASE_LEXICON_DEFAULT.items()]


def _fix_phrases(text: str, fixes: list[dict]) -> str:
    out = text
    for rx, repl in _PHRASE_RES:
        def _sub(m: re.Match, repl=repl) -> str:
            # preserva a maiuscula de inicio de frase ("Desligaliz" -> "Desliga a luz")
            new = repl[0].upper() + repl[1:] if m.group(0)[:1].isupper() else repl
            fixes.append({"from": m.group(0), "to": new, "rule": "phrase-lexicon"})
            return new
        out = rx.sub(_sub, out)
    return out


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------
def normalize_stt_text(text: str, *, allow_drop: bool = True) -> Normalized:
    """Corrige o texto transcrito. Nunca levanta: na duvida devolve o original.

    `allow_drop=False` desliga o descarte de eco (usado nas parciais, que sao so
    legenda -- descartar ali faria a legenda piscar).
    """
    raw = text or ""
    if not raw.strip() or not _flag("NEO_STT_NORMALIZE"):
        return Normalized(text=raw, raw=raw)
    try:
        if allow_drop and _flag("NEO_STT_DROP_ECHO") and _is_wake_echo(raw):
            return Normalized(text="", raw=raw, dropped="wakeword_echo")
        fixes: list[dict] = []
        out = _fix_name(raw, fixes)
        out = _fix_phrases(out, fixes)
        return Normalized(text=out, raw=raw, fixes=fixes)
    except Exception as e:  # uma regra nunca pode derrubar o turno
        log.warning("normalizacao de STT falhou (%s) — usando o texto cru", e)
        return Normalized(text=raw, raw=raw)


_reload_config()
