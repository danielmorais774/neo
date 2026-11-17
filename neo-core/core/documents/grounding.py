"""Aterramento das citações (docs/deep-read-plan.md §4.3) — sem LLM.

O subagente devolve "citação literal + página". Aqui ela é procurada no texto da
parte que ele leu. Não achou → não é evidência, por mais plausível que soe: é
exatamente assim que conhecimento geral do modelo ("o PT defende regulação das
redes") vira citação inventada numa análise de documento.

A página também sai daqui: é a do marcador ``[[página N]]`` do trecho onde a
citação foi ENCONTRADA, não a que o modelo escreveu.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_MARKER_RE = re.compile(r"\[\[(?:página|seção|slide|planilha) (\d+)\]\]\n?")
_HYPHEN_BREAK_RE = re.compile(r"(\w)-\s*\n\s*(\w)")
_EDGE_QUOTES = "\"'“”‘’«»`´ "
_QUOTES = str.maketrans({c: "" for c in _EDGE_QUOTES.strip()})
_SPACE_RE = re.compile(r"\s+")
#: Pontas que o modelo usa para indicar corte — não fazem parte do texto.
_ELLIPSIS_RE = re.compile(r"^(?:\.{3}|…|\[\.\.\.\]|\(\.\.\.\))\s*|\s*(?:\.{3}|…|\[\.\.\.\]|\(\.\.\.\))$")

#: Citação curta demais casa em qualquer lugar ("sim", "o Estado") e não prova nada.
MIN_QUOTE_CHARS = 20
#: Janela das pontas usada quando a citação não bate inteira (o modelo pula uma
#: palavra no meio, troca uma quebra de coluna). As duas pontas precisam existir,
#: nessa ordem e perto uma da outra.
_EDGE_WORDS = 6


def normalize(text: str) -> str:
    """Forma comparável: NFKC, sem hífen de quebra, sem aspas, espaço único, minúsculas."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u00ad", "")
    text = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    text = text.translate(_QUOTES)
    return _SPACE_RE.sub(" ", text).strip().lower()


def split_pages(chunk_text: str, default_page: int | None = None) -> list[tuple[int | None, str]]:
    """Separa a parte nos trechos de cada marcador ``[[página N]]``."""
    pedacos: list[tuple[int | None, str]] = []
    pos = 0
    pagina = default_page
    for m in _MARKER_RE.finditer(chunk_text or ""):
        if m.start() > pos:
            pedacos.append((pagina, chunk_text[pos:m.start()]))
        pagina = int(m.group(1))
        pos = m.end()
    pedacos.append((pagina, (chunk_text or "")[pos:]))
    return [(p, t) for p, t in pedacos if t.strip()]


@dataclass
class Grounded:
    ok: bool
    page: int | None = None
    #: "exata" — a citação inteira está no texto; "pontas" — começo e fim estão,
    #: com pouca coisa diferente no meio.
    match: str = ""
    quote: str = ""


class ChunkIndex:
    """Texto normalizado de uma parte, por página, pronto para várias consultas."""

    def __init__(self, chunk_text: str, default_page: int | None = None) -> None:
        self.pages = [(p, normalize(t)) for p, t in split_pages(chunk_text, default_page)]
        self.whole = " ".join(t for _, t in self.pages)

    def ground(self, quote: str) -> Grounded:
        limpa = (quote or "").strip()
        # Aspas e reticências se aninham nas pontas ("“…trecho…”") — tira até parar.
        while True:
            antes = limpa
            limpa = _ELLIPSIS_RE.sub("", limpa.strip(_EDGE_QUOTES)).strip()
            if limpa == antes:
                break
        alvo = normalize(limpa)
        if len(alvo) < MIN_QUOTE_CHARS:
            return Grounded(ok=False, quote=limpa)
        for pagina, texto in self.pages:
            if alvo in texto:
                return Grounded(ok=True, page=pagina, match="exata", quote=limpa)
        if alvo in self.whole:
            # Atravessa a virada de página: vale a página onde ela começa.
            return Grounded(ok=True, page=self._page_at(alvo[:40]), match="exata", quote=limpa)
        palavras = alvo.split(" ")
        if len(palavras) >= _EDGE_WORDS * 2:
            inicio = " ".join(palavras[:_EDGE_WORDS])
            fim = " ".join(palavras[-_EDGE_WORDS:])
            a = self.whole.find(inicio)
            if a >= 0:
                b = self.whole.find(fim, a + len(inicio))
                # O miolo pode variar pouco: no máximo 30% maior que a citação.
                if b >= 0 and (b + len(fim) - a) <= int(len(alvo) * 1.3):
                    return Grounded(ok=True, page=self._page_at(inicio), match="pontas", quote=limpa)
        return Grounded(ok=False, quote=limpa)

    def _page_at(self, fragment: str) -> int | None:
        for pagina, texto in self.pages:
            if fragment in texto:
                return pagina
        return self.pages[0][0] if self.pages else None
