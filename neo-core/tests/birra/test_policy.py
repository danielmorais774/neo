"""Gatilho, supressores e liberação do Modo Birra (§4 do plano)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.agent.affect import AffectTuning, BirraEpisode
from core.agent.birra.policy import (
    detect_apology,
    detect_emphatic_order,
    evaluate,
    in_quiet_hours,
)
from shared.config import Settings
from tests.birra.conftest import make_state, now

# Meio-dia em Fortaleza — fora da janela silenciosa, para os testes de gatilho.
_MIDDAY = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)


def _armed_state(**kw):
    kw.setdefault("ira", 80.0)
    kw.setdefault("offenses", 2)
    return make_state(**kw)


# ── Gatilho ───────────────────────────────────────────────────────────────────

def test_arma_com_ira_alta_e_ofensas(tuning: AffectTuning, settings: Settings) -> None:
    d = evaluate(_armed_state(), tuning, settings, now=_MIDDAY)
    assert d.armed is True
    assert d.episode_id


def test_ira_alta_sem_ofensa_nao_arma(tuning: AffectTuning, settings: Settings) -> None:
    """Discussão chata irrita (ira sobe) mas `obtuse` não abre rancor."""
    d = evaluate(make_state(ira=90.0, offenses=0), tuning, settings, now=_MIDDAY)
    assert d.armed is False
    assert "ofensa" in d.reason


def test_ofensas_sem_ira_alta_nao_arma(tuning: AffectTuning, settings: Settings) -> None:
    d = evaluate(make_state(ira=40.0, offenses=3), tuning, settings, now=_MIDDAY)
    assert d.armed is False


def test_feature_desligada_nunca_arma(tuning: AffectTuning) -> None:
    d = evaluate(_armed_state(), tuning, Settings(), now=_MIDDAY)
    assert d.armed is False
    assert d.reason == "feature desligada"


def test_postura_de_recuo_so_e_alcancavel_na_histerese(tuning: AffectTuning) -> None:
    """Com ira ≥ 60 a postura é sempre dobra_aposta/acuado — nunca recuo."""
    from core.agent.affect import posture_key

    for conf in (0.0, 30.0, 65.0, 100.0):
        state = _armed_state(ira=60.0, confianca=conf)
        assert posture_key(state.stats) in ("dobra_aposta", "acuado_agressivo")


def test_postura_de_recuo_na_histerese_vira_so_verbal(
    tuning: AffectTuning, settings: Settings
) -> None:
    # ira 50 (dentro da histerese) + confiança baixa ⇒ cede_terreno.
    state = _armed_state(ira=50.0, confianca=15.0)
    state.birra = BirraEpisode(episode_id="e1", armed_at=_MIDDAY.isoformat(), act_count=2)
    d = evaluate(state, tuning, settings, now=_MIDDAY)
    assert d.armed is True
    assert d.may_alter is False
    assert "postura de recuo" in d.notes


# ── Histerese e fim de episódio ───────────────────────────────────────────────

def test_histerese_mantem_episodio_entre_45_e_60(
    tuning: AffectTuning, settings: Settings
) -> None:
    state = _armed_state(ira=50.0)
    state.birra = BirraEpisode(episode_id="e1", armed_at=_MIDDAY.isoformat(), act_count=1)
    d = evaluate(state, tuning, settings, now=_MIDDAY)
    assert d.armed is True  # 50 < 60 (arma) mas ≥ 45 (desarma)


def test_ira_abaixo_do_desarme_encerra(tuning: AffectTuning, settings: Settings) -> None:
    state = _armed_state(ira=40.0)
    state.birra = BirraEpisode(episode_id="e1", armed_at=_MIDDAY.isoformat())
    d = evaluate(state, tuning, settings, now=_MIDDAY)
    assert d.armed is False
    assert d.release_reason == "decay"


def test_ttl_encerra_episodio(tuning: AffectTuning, settings: Settings) -> None:
    state = _armed_state()
    state.birra = BirraEpisode(
        episode_id="e1", armed_at=(_MIDDAY - timedelta(minutes=90)).isoformat()
    )
    d = evaluate(state, tuning, settings, now=_MIDDAY)
    assert d.release_reason == "ttl"


def test_desculpa_encerra_mesmo_com_feature_desligada(tuning: AffectTuning) -> None:
    """Episódio aberto antes de baixarem a flag ainda tem que ser fechado."""
    state = _armed_state()
    state.birra = BirraEpisode(episode_id="e1", armed_at=_MIDDAY.isoformat())
    d = evaluate(state, tuning, Settings(), user_message="desculpa, foi mal", now=_MIDDAY)
    assert d.armed is False
    assert d.release_reason == "apology"


def test_desculpa_sem_episodio_nao_arma_birra(
    tuning: AffectTuning, settings: Settings
) -> None:
    """Regressão (achada em prod): a ira ainda está no teto quando a desculpa
    chega — quem a derruba é o appraiser, depois. Sem esta guarda, o turno do
    "desculpa, foi mal" era justamente o que ABRIA o episódio."""
    d = evaluate(_armed_state(), tuning, settings, user_message="desculpa, foi mal", now=_MIDDAY)
    assert d.armed is False
    assert d.reason == "desculpa recebida"


def test_pedido_repetido_continua_sofrendo_birra(
    tuning: AffectTuning, settings: Settings
) -> None:
    """Não existe obedecer na 2ª: só desculpa encerra (decisão do Daniel)."""
    state = _armed_state()
    state.birra = BirraEpisode(episode_id="e1", armed_at=_MIDDAY.isoformat(), act_count=1)
    d = evaluate(state, tuning, settings, user_message="diminui a temperatura", now=_MIDDAY)
    assert d.armed is True
    assert d.obey_now is False


# ── Ordem enfática ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "message",
    [
        "faz isso imediatamente",
        "desliga a luz AGORA!",
        "é sério, apaga a luz",
        "sem birra, liga o ar",
        "DESLIGA ESSA LUZ IMEDIATAMENTE",
    ],
)
def test_ordem_enfatica_detectada(message: str) -> None:
    assert detect_emphatic_order(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "acende a luz agora",          # "agora" sem ênfase é fala corriqueira
        "liga a luz do quarto",
        "e agora, o que a gente faz?",
        "urgente",                      # intensificador sem âncora de ação
    ],
)
def test_ordem_enfatica_nao_dispara_a_toa(message: str) -> None:
    assert detect_emphatic_order(message) is False


def test_ancora_pode_vir_do_ato_pendente() -> None:
    """'para com isso' logo após uma birra conta, mesmo sem verbo de casa."""
    assert detect_emphatic_order("para com isso, é sério") is False
    assert detect_emphatic_order("para com isso, é sério", has_pending_act=True) is True


def test_ordem_enfatica_obedece_mas_nao_perdoa(
    tuning: AffectTuning, settings: Settings
) -> None:
    state = _armed_state()
    state.birra = BirraEpisode(episode_id="e1", armed_at=_MIDDAY.isoformat(), act_count=1)
    d = evaluate(state, tuning, settings, user_message="apaga a luz imediatamente", now=_MIDDAY)
    assert d.obey_now is True
    assert d.armed is True            # o episódio continua de pé
    assert d.release_reason is None   # não é perdão
    assert d.may_alter is False


# ── Desculpa ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "message",
    ["desculpa", "foi mal aí", "perdão", "tu tinha razão", "peguei pesado, foi mal"],
)
def test_desculpa_detectada(message: str) -> None:
    assert detect_apology(message) is True


def test_desculpa_nao_dispara_em_qualquer_coisa() -> None:
    assert detect_apology("acende a luz") is False
    assert detect_apology("você errou de novo") is False


# ── Supressores ───────────────────────────────────────────────────────────────

def test_horario_silencioso_e_so_verbal(tuning: AffectTuning, settings: Settings) -> None:
    # 03:00 em Fortaleza (UTC-3) = 06:00 UTC.
    madrugada = datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)
    d = evaluate(_armed_state(), tuning, settings, now=madrugada)
    assert d.armed is True
    assert d.verbal_only is True
    assert d.may_alter is False
    assert "horário silencioso" in d.notes


def test_gravidade_alta_suprime_ato_fisico(tuning: AffectTuning, settings: Settings) -> None:
    d = evaluate(_armed_state(), tuning, settings, gravity="alta", now=_MIDDAY)
    assert d.may_alter is False
    assert "gravidade=alta" in d.notes


def test_medo_alto_suprime_ato_fisico(tuning: AffectTuning, settings: Settings) -> None:
    d = evaluate(_armed_state(medo=70.0), tuning, settings, now=_MIDDAY)
    assert d.may_alter is False


def test_teto_de_atos_fisicos_degrada_para_verbal(
    tuning: AffectTuning, settings: Settings
) -> None:
    state = _armed_state()
    state.birra = BirraEpisode(
        episode_id="e1", armed_at=_MIDDAY.isoformat(), act_count=3, physical_acts=2
    )
    d = evaluate(state, tuning, settings, now=_MIDDAY)
    assert d.armed is True
    assert d.may_alter is False
    assert "teto de atos físicos" in d.notes


def test_cooldown_entre_atos_fisicos(tuning: AffectTuning, settings: Settings) -> None:
    state = _armed_state()
    state.birra = BirraEpisode(
        episode_id="e1",
        armed_at=_MIDDAY.isoformat(),
        act_count=2,
        physical_acts=1,
        last_physical_at=(_MIDDAY - timedelta(minutes=2)).isoformat(),
    )
    d = evaluate(state, tuning, settings, now=_MIDDAY)
    assert d.may_alter is False
    assert "cooldown entre atos físicos" in d.notes


# ── Escalonamento ─────────────────────────────────────────────────────────────

def test_primeiro_ato_do_episodio_e_so_fala(tuning: AffectTuning, settings: Settings) -> None:
    d = evaluate(_armed_state(), tuning, settings, now=_MIDDAY)
    assert d.armed is True
    assert d.mecanica == "nada"
    assert d.may_alter is False


def test_segundo_ato_pode_alterar(tuning: AffectTuning, settings: Settings) -> None:
    state = _armed_state(confianca=70.0)  # dobra_aposta
    state.birra = BirraEpisode(episode_id="e1", armed_at=_MIDDAY.isoformat(), act_count=1)
    d = evaluate(state, tuning, settings, now=_MIDDAY)
    assert d.mecanica == "inverte"
    assert d.may_alter is True


def test_quiet_hours_parse() -> None:
    assert in_quiet_hours(datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc), "22-7", "America/Fortaleza")
    assert not in_quiet_hours(datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc), "22-7", "America/Fortaleza")
    # Spec inválida desliga a janela em vez de explodir.
    assert not in_quiet_hours(datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc), "lixo", "America/Fortaleza")
