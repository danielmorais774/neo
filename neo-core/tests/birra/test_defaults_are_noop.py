"""Com os defaults, NADA muda em produção.

O neo-core roda com ``--reload`` e o Mutagen sincroniza: salvar um arquivo é
deploy. Então a suíte precisa provar que, sem ninguém mexer no ``.env``, o
comportamento é byte-a-byte o de antes — e que as flags são a única porta de
entrada da feature.
"""

from __future__ import annotations

from core.agent.affect import AffectState, AffectTuning, render_directive
from core.agent.birra.policy import evaluate
from shared.config import Settings
from tests.birra.conftest import make_state


def test_flags_desligadas_por_default() -> None:
    s = Settings()
    assert s.neo_birra_enabled is False
    assert s.neo_affect_route_to_brain == "never"
    assert s.neo_affect_router_hint_enabled is False
    assert s.neo_affect_skill_directive_enabled is False


def test_birra_nunca_arma_com_defaults(tuning: AffectTuning) -> None:
    """Nem no estado mais extremo possível."""
    state = make_state(ira=100.0, offenses=10, medo=0.0, confianca=100.0)
    d = evaluate(state, tuning, Settings(), user_message="desliga a luz")
    assert d.armed is False
    assert d.may_alter is False


def test_diretiva_sem_birra_e_identica(tuning: AffectTuning) -> None:
    """O bloco de birra não vaza para a diretiva de quem não tem episódio."""
    state = make_state(ira=90.0, offenses=3)
    sem = render_directive(state, tuning)
    com = render_directive(state, tuning, birra_armed=True)
    assert "BIRRA ATIVA" not in sem
    assert "BIRRA ATIVA" in com
    assert com.startswith(sem.rsplit("\n", 1)[0][: len(sem) // 2])


def test_estado_de_repouso_nao_renderiza_birra(tuning: AffectTuning) -> None:
    calmo = AffectState.fresh(tuning)
    assert "BIRRA" not in render_directive(calmo, tuning)
