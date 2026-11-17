"""Procedência do marcador de imagem (docs/release-request-plan.md §12.2).

A regra já existia — como TEXTO, no prompt
(``modules/desktop/ai_service.py``: *"A URL também tem que ser REAL: só use uma
que VOCÊ viu num resultado de tool"*). Aqui ela vira verificação.

O que ela impede: o app do usuário fazer um GET para uma URL que o modelo
INVENTOU. `fetch_remote_image_bytes` (Rust) baixa fora do webview e recusa host
privado, então a foto do anúncio é segura; o que sobra é o query string —
``https://cdn.qualquer/f.jpg?d=<cep do usuário>`` é uma imagem perfeitamente
válida e um vazamento perfeitamente silencioso. Por isso a query FAZ PARTE da
identidade da URL (``canonical_url``): a foto que veio do ``meli_product`` passa,
a mesma foto com um parâmetro a mais não.

Caminho local e ``file:<id>`` não passam por aqui: quem os resolve é o cliente,
lendo disco/File Store, sem falar com terceiro.
"""

from __future__ import annotations

import logging
import re

from core.authority.turn import TurnAuthority, current_authority

logger = logging.getLogger(__name__)

#: ``[[IMAGE:origem]]`` ou ``[[IMAGE:origem|legenda]]``. Tolerante a espaço,
#: como o resto da convenção de marcadores da casa.
_MARKER_RE = re.compile(
    r"\[\[\s*IMAGE\s*:\s*(?P<origem>[^\]|]+?)\s*(?:\|(?P<legenda>[^\]]*))?\]\]",
    re.IGNORECASE,
)

_REMOTA_RE = re.compile(r"^https?://", re.IGNORECASE)


def check_inline_images(
    text: str, authority: TurnAuthority | None = None
) -> tuple[str, list[str]]:
    """Devolve ``(texto_saneado, urls_sem_procedência)``.

    Só marcador REMOTO sem procedência é removido. Erro nenhum sobe daqui: uma
    falha de saneamento não pode derrubar a resposta do turno.
    """
    if not text or "[[" not in text:
        return text, []
    autoridade = authority if authority is not None else current_authority()
    if autoridade is None:
        # Fora de turno não há procedência para comparar — deixar passar é o
        # comportamento de hoje, e barrar tudo quebraria job/scheduler.
        return text, []

    recusadas: list[str] = []

    def _troca(m: re.Match[str]) -> str:
        origem = (m.group("origem") or "").strip()
        if not _REMOTA_RE.match(origem):
            return m.group(0)
        try:
            if autoridade.url_has_provenance(origem):
                return m.group(0)
        except Exception:  # noqa: BLE001
            return m.group(0)
        recusadas.append(origem)
        legenda = (m.group("legenda") or "").strip()
        return f"_(imagem não exibida: {legenda})_" if legenda else ""

    try:
        return _MARKER_RE.sub(_troca, text), recusadas
    except Exception:  # noqa: BLE001
        logger.exception("[authority] saneamento de imagem falhou; texto intacto")
        return text, []


def enforce_inline_images(text: str) -> str:
    """Aplica a regra segundo o modo (``NEO_RELEASE_GATE_MODE``).

    Em ``shadow`` o texto sai INTACTO e a recusa vira log — a medição aqui
    importa mais do que no resto do portão, porque existe um falso positivo
    conhecido: "mostra de novo aquela foto" num turno seguinte cita uma URL que
    apareceu no turno ANTERIOR, e a procedência é por turno.
    """
    saneado, recusadas = check_inline_images(text)
    if not recusadas:
        return text

    try:
        from shared.config import get_settings

        modo = str(getattr(get_settings(), "neo_release_gate_mode", "shadow") or "shadow").lower()
    except Exception:  # noqa: BLE001
        modo = "shadow"

    if modo == "enforce":
        logger.warning(
            "[authority] imagem sem procedência removida (%d): %s",
            len(recusadas), ", ".join(u[:120] for u in recusadas[:3]),
        )
        return saneado

    logger.warning(
        "[authority][sombra] imagem sem procedência TERIA sido removida (%d): %s",
        len(recusadas), ", ".join(u[:120] for u in recusadas[:3]),
    )
    return text
