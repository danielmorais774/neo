"""
tts_text.py -- normalizacao de texto pt-BR para sintese de voz (todos os engines).

Problema que resolve: os engines de TTS (Qwen/CosyVoice/F5/... e o fallback Kokoro)
erram a leitura de digitos e simbolos em pt-BR -- "2026" vira ruido ou ingles,
"25°C" vira "vinte e cinco C", e "Neo" sai como "neu" (grafema 'eo' lido como
ditongo 'eu'). A correcao e feita AQUI, no orquestrador, uma vez por request:
o texto que chega ao engine ja vai por extenso e com a pronuncia corrigida,
entao TODOS os engines (inclusive o Kokoro do fallback) falam certo.

Regras (aplicadas nesta ordem; so quando o idioma e portugues):
  1. lexico de pronuncia   Neo -> "níu" (configuravel via NEO_TTS_LEXICON, JSON)
  2. "nº 12"               -> "número doze"
  3. moeda                 R$ 1.234,56 -> "mil duzentos e trinta e quatro reais e
                           cinquenta e seis centavos"; US$/$ -> dolares
  4. porcentagem           50% -> "cinquenta por cento"
  5. temperatura           25°C/25ºC -> "vinte e cinco graus Celsius" (°F idem)
  6. horas                 14h30 / 14:30 -> "quatorze e trinta"; 14h -> "quatorze horas"
  7. datas                 11/07/2026 -> "onze de julho de dois mil e vinte e seis"
  8. ordinais              1º -> "primeiro", 2ª -> "segunda"
  9. graus soltos          25° -> "vinte e cinco graus"
 10. unidades pos-numero   10 km -> "dez quilômetros", 1 kg -> "um quilograma"
 11. milhar com ponto      1.234.567 -> 1234567 (segue p/ regra 13)
 12. decimais com virgula  3,14 -> "três vírgula quatorze" (0,05 -> "zero vírgula zero cinco")
 13. inteiros              2026 -> "dois mil e vinte e seis" (>= 8 digitos: digito a digito)

URLs (http/https) sao preservadas intactas (nao normalizamos o que ja e ilegivel).
Sem dependencias externas: o conversor numero->extenso e nativo deste modulo.
"""
from __future__ import annotations

import os
import re
import json
import logging

log = logging.getLogger("neo-voice.ttstext")

# ---------------------------------------------------------------------------
# numero -> extenso (pt-BR), nativo (sem num2words)
# ---------------------------------------------------------------------------
_UNITS = ["zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete",
          "oito", "nove", "dez", "onze", "doze", "treze", "quatorze", "quinze",
          "dezesseis", "dezessete", "dezoito", "dezenove"]
_UNITS_F = {"um": "uma", "dois": "duas"}  # feminino (horas: "duas horas")
_TENS = ["", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta",
         "setenta", "oitenta", "noventa"]
_HUNDREDS = ["", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos",
             "seiscentos", "setecentos", "oitocentos", "novecentos"]
_SCALES = [(10 ** 12, "trilhão", "trilhões"), (10 ** 9, "bilhão", "bilhões"),
           (10 ** 6, "milhão", "milhões"), (10 ** 3, "mil", "mil")]

_ORD_UNITS = ["", "primeiro", "segundo", "terceiro", "quarto", "quinto",
              "sexto", "sétimo", "oitavo", "nono"]
_ORD_TENS = ["", "décimo", "vigésimo", "trigésimo", "quadragésimo", "quinquagésimo",
             "sexagésimo", "septuagésimo", "octogésimo", "nonagésimo"]
_ORD_HUNDREDS = ["", "centésimo", "ducentésimo", "trecentésimo", "quadringentésimo",
                 "quingentésimo", "sexcentésimo", "septingentésimo", "octingentésimo",
                 "noningentésimo"]


def _under_1000(n: int, fem: bool = False) -> str:
    if n == 100:
        return "cem"
    parts = []
    if n >= 100:
        parts.append(_HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10])
        n %= 10
    if n:
        w = _UNITS[n]
        parts.append(_UNITS_F.get(w, w) if fem else w)
    return " e ".join(parts)


def int_to_words(n: int, fem: bool = False) -> str:
    """Cardinal por extenso. `fem` flexiona um/uma e dois/duas (p/ horas)."""
    if n == 0:
        return "zero"
    if n < 0:
        return "menos " + int_to_words(-n, fem)
    groups = []  # (palavras, valor_do_grupo)
    for scale, sing, plur in _SCALES:
        q, n = divmod(n, scale)
        if q:
            if scale == 1000 and q == 1:
                groups.append(("mil", scale))
            else:
                groups.append((f"{int_to_words(q)} {sing if q == 1 else plur}", q * scale))
    if n:
        groups.append((_under_1000(n, fem), n))
    if len(groups) == 1:
        return groups[0][0]
    # regra do "e": o ultimo grupo leva "e" quando < 100 ou centena redonda
    # ("dois mil E vinte e seis", "mil E duzentos", mas "mil duzentos e trinta e quatro")
    last_words, last_val = groups[-1]
    head = " ".join(w for w, _ in groups[:-1])
    if last_val < 100 or last_val % 100 == 0:
        return f"{head} e {last_words}"
    return f"{head} {last_words}"


def ordinal_to_words(n: int, fem: bool = False) -> str:
    """Ordinal por extenso ate 9999 (alem disso, cai no cardinal)."""
    if n <= 0 or n > 9999:
        return int_to_words(n, fem)
    parts = []
    if n >= 1000:
        q, n = divmod(n, 1000)
        parts.append(("" if q == 1 else _ORD_UNITS[q] + " ") + "milésimo")
    if n >= 100:
        parts.append(_ORD_HUNDREDS[n // 100])
        n %= 100
    if n >= 10:
        parts.append(_ORD_TENS[n // 10])
        n %= 10
    if n:
        parts.append(_ORD_UNITS[n])
    words = " ".join(p for p in parts if p)
    if fem:  # "vigésimo primeiro" -> "vigésima primeira"
        words = " ".join(w[:-1] + "a" if w.endswith("o") else w for w in words.split())
    return words


def _digits_spoken(digits: str) -> str:
    """Digito a digito ("05" -> "zero cinco") p/ decimais com zero a esquerda e IDs longos."""
    return " ".join(_UNITS[int(d)] for d in digits)


# ---------------------------------------------------------------------------
# lexico de pronuncia (grafema -> como o engine deve LER)
# ---------------------------------------------------------------------------
# "Neo" em pt-BR e lido "néu" pelos engines; a grafia "níu" forca a pronuncia
# correta (/niw/, como "new"). Override/extensao via env NEO_TTS_LEXICON com um
# JSON simples: {"palavra": "pronuncia", ...} (casa palavra inteira, sem case).
_DEFAULT_LEXICON = {"neo": "níu"}


def _load_lexicon() -> dict:
    lex = dict(_DEFAULT_LEXICON)
    raw = os.environ.get("NEO_TTS_LEXICON")
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, dict):
                lex.update({str(k).lower(): str(v) for k, v in extra.items()})
            else:
                log.warning("NEO_TTS_LEXICON nao e um objeto JSON — ignorado")
        except Exception as e:
            log.warning("NEO_TTS_LEXICON invalido (%s) — usando so o default", e)
    return lex


_LEXICON = _load_lexicon()
_LEXICON_RES = [(re.compile(r"\b" + re.escape(k) + r"\b", re.IGNORECASE), v)
                for k, v in _LEXICON.items()]

_MONTHS = ["", "janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
           "agosto", "setembro", "outubro", "novembro", "dezembro"]

# unidades comuns pos-numero: (singular, plural). Conservador: so troca quando
# ha um numero imediatamente antes.
_UNITS_MAP = {
    "km/h": ("quilômetro por hora", "quilômetros por hora"),
    "km²": ("quilômetro quadrado", "quilômetros quadrados"),
    "km": ("quilômetro", "quilômetros"),
    "kg": ("quilo", "quilos"),
    "cm": ("centímetro", "centímetros"),
    "mm": ("milímetro", "milímetros"),
    "ml": ("mililitro", "mililitros"),
    "m²": ("metro quadrado", "metros quadrados"),
    "gb": ("gigabyte", "gigabytes"),
    "mb": ("megabyte", "megabytes"),
    "tb": ("terabyte", "terabytes"),
    "ghz": ("gigahertz", "gigahertz"),
    "mhz": ("megahertz", "megahertz"),
}
_UNITS_RE = re.compile(
    r"(\d+(?:,\d+)?)\s?(" + "|".join(re.escape(u) for u in _UNITS_MAP) + r")\b(?!\w)",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"https?://\S+")


def _sentinel(i: int) -> str:
    # sem digitos/letras "normais": nenhuma regra de normalizacao pode toca-lo
    return "\x00" + "\x01" * (i + 1) + "\x00"


def _num_str_to_words(s: str, fem: bool = False) -> str:
    """'1234' -> extenso; digito a digito p/ strings muito longas (IDs/telefones)."""
    if len(s) >= 8:
        return _digits_spoken(s)
    return int_to_words(int(s), fem)


def _currency(m: re.Match, sing: str, plur: str) -> str:
    inteiro = m.group(1).replace(".", "")
    cents = m.group(2)
    val = int(inteiro)
    out = f"{int_to_words(val)} {sing if val == 1 else plur}"
    if cents:
        c = int(cents) * (10 if len(cents) == 1 else 1)  # "10,5" == 50 centavos
        if c:
            out += f" e {int_to_words(c)} centavo{'s' if c != 1 else ''}"
    return out


def _temperature(m: re.Match) -> str:
    num = m.group(1)
    unit = {"c": "Celsius", "f": "Fahrenheit"}[m.group(2).lower()]
    neg = num.startswith("-")
    num = num.lstrip("-")
    grau = "grau" if num == "1" else "graus"
    return f"{'menos ' if neg else ''}{num} {grau} {unit}"


def _time_words(h: int, mins: str | None) -> str:
    hw = int_to_words(h, fem=True)
    if mins is None:
        return f"{hw} hora{'s' if h != 1 else ''}"
    m = int(mins)
    if m == 0:
        return f"{hw} hora{'s' if h != 1 else ''}"
    return f"{hw} e {int_to_words(m)}"


def _date_words(d: int, mo: int, year: str | None) -> str:
    day = "primeiro" if d == 1 else int_to_words(d)
    out = f"{day} de {_MONTHS[mo]}"
    if year:
        out += f" de {int_to_words(int(year))}"
    return out


# regras compiladas (a ordem de aplicacao e a da lista)
_RULES: list = [
    # nº 12 -> "número doze"
    (re.compile(r"\b[Nn][º°]\s*(?=\d)"), "número "),
    # R$ / US$ / $
    (re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?"),
     lambda m: _currency(m, "real", "reais")),
    (re.compile(r"(?:US?\$|\$)\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?"),
     lambda m: _currency(m, "dólar", "dólares")),
    # porcentagem (o numero segue p/ as regras de decimal/inteiro)
    (re.compile(r"(\d+(?:,\d+)?)\s*%"), r"\1 por cento"),
    # temperatura 25°C / 25ºC / -3 °F
    (re.compile(r"(-?\d+(?:,\d+)?)\s*[°º]\s*([CcFf])\b"), _temperature),
    # horas 14h30 / 14h / 14:30
    (re.compile(r"\b([01]?\d|2[0-3])h([0-5]\d)?\b"),
     lambda m: _time_words(int(m.group(1)), m.group(2))),
    (re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!:)"),
     lambda m: _time_words(int(m.group(1)), m.group(2))),
    # datas 11/07/2026 e 11/07
    (re.compile(r"\b(0?[1-9]|[12]\d|3[01])/(0?[1-9]|1[0-2])/(\d{4}|\d{2})\b"),
     lambda m: _date_words(int(m.group(1)), int(m.group(2)), m.group(3))),
    (re.compile(r"\b(0?[1-9]|[12]\d|3[01])/(0?[1-9]|1[0-2])\b(?!/)"),
     lambda m: _date_words(int(m.group(1)), int(m.group(2)), None)),
    # ordinais 1º / 2ª (º ordinal; ° de grau ja foi consumido acima quando era temp.)
    (re.compile(r"\b(\d{1,4})º(?!\w)"), lambda m: ordinal_to_words(int(m.group(1)))),
    (re.compile(r"\b(\d{1,4})ª(?!\w)"), lambda m: ordinal_to_words(int(m.group(1)), fem=True)),
    # graus soltos: 25° (sem C/F)
    (re.compile(r"(\d+(?:,\d+)?)\s*[°º](?!\w)"),
     lambda m: f"{m.group(1)} grau{'s' if m.group(1) != '1' else ''}"),
    # unidades pos-numero: 10 km, 1kg, 3,5 GB
    (_UNITS_RE,
     lambda m: f"{m.group(1)} {_UNITS_MAP[m.group(2).lower()][0 if m.group(1) == '1' else 1]}"),
    # 1.234.567 -> 1234567 (a leitura fica p/ a regra de inteiros)
    (re.compile(r"\b\d{1,3}(?:\.\d{3})+\b"), lambda m: m.group(0).replace(".", "")),
    # decimais com virgula: 3,14 / 0,05
    (re.compile(r"\b(\d+),(\d+)\b"),
     lambda m: f"{_num_str_to_words(m.group(1))} vírgula "
               + (_digits_spoken(m.group(2)) if m.group(2).startswith("0")
                  else _num_str_to_words(m.group(2)))),
    # inteiros restantes (anos inclusive): 2026 -> "dois mil e vinte e seis"
    (re.compile(r"\b\d+\b"), lambda m: _num_str_to_words(m.group(0))),
]


def _is_pt(language: str | None) -> bool:
    if not language:
        return True  # default dos engines e portugues
    return language.strip().lower().startswith(("pt", "portug"))


def normalize_tts_text(text: str, language: str | None = None) -> str:
    """Normaliza `text` para sintese em pt-BR. Idiomas nao-pt passam intactos."""
    if not text or not _is_pt(language):
        return text
    # preserva URLs (nada de "dois pontos barra barra cento e noventa e dois...")
    urls: list[str] = []

    def _stash(m: re.Match) -> str:
        urls.append(m.group(0))
        return _sentinel(len(urls) - 1)

    out = _URL_RE.sub(_stash, text)
    for rx, repl in _LEXICON_RES:
        out = rx.sub(repl, out)
    for rx, repl in _RULES:
        try:
            out = rx.sub(repl, out)
        except Exception as e:  # uma regra nunca pode derrubar a sintese
            log.warning("regra de normalizacao falhou (%s): %s", rx.pattern[:40], e)
    # do maior indice p/ o menor: o sentinel de i=0 e prefixo dos demais
    for i in range(len(urls) - 1, -1, -1):
        out = out.replace(_sentinel(i), urls[i])
    return out
