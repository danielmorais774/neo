"""Mecânica × fala (§5 do plano).

O pedido original mistura duas coisas que aqui ficam ORTOGONAIS: o que o Neo faz
com o dispositivo e o que ele diz. É a combinação que produz "ele desliga a luz e
fala 'agora se levanta e liga'".

A escolha é determinística e sai da postura que o afeto JÁ calcula — birra não é
sorteio, é consequência do estado. E o **primeiro ato de um episódio é sempre só
fala**: a primeira reação de quem está irritado é se recusar, não sabotar.
"""

from __future__ import annotations

from core.agent.affect import AffectState, BirraEpisode, posture_key

# postura → (mecânica preferida, fala)
_BY_POSTURE: dict[str, tuple[str, str]] = {
    # Ira alta + confiança alta: ele está por cima e ousa.
    "dobra_aposta": ("inverte", "condiciona"),
    # Ira alta + medo alto: mordendo, mas sem espaço para gracinha elaborada.
    "acuado_agressivo": ("nada", "recusa_seca"),
    # Frio: não sabota, impõe condição.
    "racional": ("nada", "condiciona"),
    "cede_terreno": ("nada", "recusa_seca"),
    "reavalia": ("nada", "recusa_seca"),
}

_FALLBACK = ("meia_bomba", "deboche")


def choose_tactic(
    *,
    state: AffectState,
    episode: BirraEpisode,
    verbal_only: bool,
) -> tuple[str, str]:
    """Devolve ``(mecanica, fala)`` para este ato."""
    posture = posture_key(state.stats)
    mecanica, fala = _BY_POSTURE.get(posture, _FALLBACK)

    # Escalonamento: o 1º ato do episódio nunca altera o mundo.
    if episode.act_count == 0:
        mecanica = "nada"
        if fala == "condiciona":
            fala = "delega"  # "pede pra Alexa, que tu gosta tanto"
    if verbal_only:
        mecanica = "nada"
    return mecanica, fala
