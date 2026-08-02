"""Intensidade relativa ao repouso (docs/modo-birra-plan.md §3.7.1).

O teste central deste arquivo é ``test_repouso_nunca_e_saliente``: com baseline
65, ``confianca`` em repouso cai na banda ABSOLUTA "alto" — se alguém trocar a
`intensity` por `band` de novo, o roteamento passa a desviar 100 % dos turnos
para o cérebro, para sempre. Este arquivo existe para essa regressão não voltar.
"""

from __future__ import annotations

import pytest

from core.agent.affect import (
    AXES,
    AffectState,
    AffectTuning,
    Band,
    Level,
    affect_salience,
    band,
    intensity,
    level,
)
from shared.config import Settings
from tests.birra.conftest import make_state


def test_repouso_nunca_e_saliente(tuning: AffectTuning) -> None:
    fresh = AffectState.fresh(tuning)
    sal = affect_salience(fresh, tuning)
    assert sal.calm is True
    for axis in AXES:
        assert sal.reading(axis).level is Level.REPOUSO, axis


def test_confianca_em_repouso_e_alto_na_banda_absoluta(tuning: AffectTuning) -> None:
    """A armadilha, explicitada: por isso o critério não pode ser a banda."""
    assert band(65.0, medio=30.0, alto=60.0) is Band.ALTO
    assert level(65.0, 65.0) is Level.REPOUSO


@pytest.mark.parametrize("axis", ["ira", "medo", "tristeza"])
def test_escala_coincide_com_as_bandas_onde_o_repouso_e_zero(
    axis: str, tuning: AffectTuning
) -> None:
    """Property: para baseline 0 os cortes 0.30/0.60 são exatamente 30 e 60."""
    baseline = tuning.baselines[axis]
    assert baseline == 0.0
    assert level(29.9, baseline) is Level.REPOUSO
    assert level(30.0, baseline) is Level.ELEVADO
    assert level(59.9, baseline) is Level.ELEVADO
    assert level(60.0, baseline) is Level.FORTE
    assert level(85.0, baseline) is Level.EXTREMO
    for value in (0, 15, 30, 45, 60, 75, 100):
        assert intensity(float(value), baseline) == pytest.approx(value / 100.0)


def test_queda_de_confianca_e_medida_pelo_espaco_abaixo(tuning: AffectTuning) -> None:
    # 65 → 31 é uma queda grande; a banda absoluta chamaria de "medio".
    assert intensity(31.0, 65.0) == pytest.approx((65 - 31) / 65)
    assert level(31.0, 65.0) is Level.ELEVADO
    assert band(31.0, medio=30.0, alto=60.0) is Band.MEDIO


def test_posture_active_conta_como_nao_calmo(tuning: AffectTuning) -> None:
    state = make_state(pressure_trigger="contested")
    sal = affect_salience(state, tuning)
    assert sal.posture_active is True
    assert sal.calm is False


def test_render_line_traz_valor_repouso_e_nivel(tuning: AffectTuning) -> None:
    state = make_state(ira=87.0, confianca=31.0)
    line = affect_salience(state, tuning).render_line()
    assert "ira 87 (repouso 0) EXTREMO" in line
    assert "confianca 31 (repouso 65) elevado" in line
    # Todos os cinco eixos, sempre — o classificador precisa do quadro inteiro.
    for axis in AXES:
        assert axis in line


def test_at_least_usa_a_escala_nominal(tuning: AffectTuning) -> None:
    sal = affect_salience(make_state(ira=62.0), tuning)
    assert sal.at_least("ira", Level.FORTE) is True
    assert sal.at_least("ira", Level.EXTREMO) is False
    assert sal.at_least("medo", Level.ELEVADO) is False
