"""Os arquivos que a transcrição deixa: texto corrido (.txt) e legenda (.srt)."""

from __future__ import annotations

from typing import Any


def tempo_srt(segundos: float) -> str:
    """``HH:MM:SS,mmm`` (o formato da legenda)."""
    ms_total = max(0, int(round(float(segundos) * 1000)))
    horas, resto = divmod(ms_total, 3_600_000)
    minutos, resto = divmod(resto, 60_000)
    seg, ms = divmod(resto, 1000)
    return f"{horas:02d}:{minutos:02d}:{seg:02d},{ms:03d}"


def duracao_legivel(segundos: float) -> str:
    """``1h02min``, ``12min05s`` ou ``45s``."""
    total = max(0, int(round(float(segundos))))
    horas, resto = divmod(total, 3600)
    minutos, seg = divmod(resto, 60)
    if horas:
        return f"{horas}h{minutos:02d}min"
    if minutos:
        return f"{minutos}min{seg:02d}s"
    return f"{seg}s"


def _limpo(texto: Any) -> str:
    return " ".join(str(texto or "").split())


def segmentos_validos(segmentos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in segmentos if _limpo(s.get("text"))]


def para_txt(segmentos: list[dict[str, Any]]) -> str:
    """Um trecho por linha, sem tempos — é o texto para ler."""
    linhas = [_limpo(s.get("text")) for s in segmentos_validos(segmentos)]
    return ("\n".join(linhas) + "\n") if linhas else ""


def para_srt(segmentos: list[dict[str, Any]]) -> str:
    blocos = []
    for i, s in enumerate(segmentos_validos(segmentos), start=1):
        inicio = float(s.get("start") or 0.0)
        fim = max(inicio, float(s.get("end") or inicio))
        blocos.append(f"{i}\n{tempo_srt(inicio)} --> {tempo_srt(fim)}\n{_limpo(s.get('text'))}\n")
    return "\n".join(blocos)
