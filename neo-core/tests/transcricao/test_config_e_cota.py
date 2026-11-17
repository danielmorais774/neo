"""Configuração do painel (chave cifrada, interruptor, modelo) e a conta da cota."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from core.transcricao import config
from core.transcricao.cota import Cota
from shared.ajustes import CotaGratuitaGroq
from tests.transcricao.conftest import FakeDB


# ── configuração ───────────────────────────────────────────────────────────


def test_sem_nada_gravado_vale_o_env_e_os_padroes(settings):
    assert config.chave() == "gsk_env_1234567890abcd"
    assert config.origem_da_chave() == "env"
    assert config.final_da_chave() == "abcd"
    assert config.so_gratuita() is True
    assert config.modelo() == "whisper-large-v3-turbo"
    assert config.disponivel()


def test_sem_chave_nenhuma_a_tool_nao_esta_disponivel(settings):
    settings.groq_api_key = None
    assert config.chave() == ""
    assert config.origem_da_chave() == "nenhuma"
    assert not config.disponivel()


@pytest.mark.asyncio
async def test_chave_do_painel_e_cifrada_e_vence_o_env(settings):
    db = FakeDB()
    await config.gravar_chave(db, "  gsk_painel_99998888  ")
    guardado = db.prefs[("neo", "transcricao", "groq_api_key_cifrada")]
    assert "gsk_painel" not in guardado
    assert Fernet(settings.neo_cred_key.encode()).decrypt(guardado.encode()).decode() == "gsk_painel_99998888"
    assert config.chave() == "gsk_painel_99998888"
    assert config.origem_da_chave() == "painel"

    await config.apagar_chave(db)
    assert config.origem_da_chave() == "env"
    assert ("neo", "transcricao", "groq_api_key_cifrada") not in db.prefs


@pytest.mark.asyncio
async def test_sem_cofre_nao_grava_chave(settings):
    settings.neo_cred_key = ""
    with pytest.raises(config.ConfigError):
        await config.gravar_chave(FakeDB(), "gsk_x")
    assert not config.cofre_disponivel()


@pytest.mark.asyncio
async def test_carregar_devolve_o_que_o_painel_gravou(settings):
    db = FakeDB()
    await config.gravar_chave(db, "gsk_painel_12345678")
    await config.gravar_opcoes(db, so_gratuita=False, modelo="whisper-large-v3")
    config._esquecer()
    assert config.origem_da_chave() == "env"

    await config.carregar(db)
    assert config.chave() == "gsk_painel_12345678"
    assert config.so_gratuita() is False
    assert config.modelo() == "whisper-large-v3"


@pytest.mark.asyncio
async def test_chave_que_nao_decifra_cai_no_env_e_avisa(settings):
    db = FakeDB()
    await config.gravar_chave(db, "gsk_painel_12345678")
    settings.neo_cred_key = Fernet.generate_key().decode()  # a NEO_CRED_KEY mudou
    assert config.chave() == "gsk_env_1234567890abcd"
    assert config.chave_quebrada()


@pytest.mark.asyncio
async def test_modelo_vazio_e_recusado(settings):
    with pytest.raises(config.ConfigError):
        await config.gravar_opcoes(FakeDB(), modelo="   ")


def test_preferencias_ficam_fora_do_prompt():
    """O cérebro lê as preferências de "neo-desktop" — a chave e a cota moram em "neo"."""
    assert config.PREF_SERVICE == "neo"


# ── cota ───────────────────────────────────────────────────────────────────


class _Relogio:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _cota(db=None, relogio=None, **limites) -> Cota:
    base = dict(audio_s_por_hora=7200, audio_s_por_dia=28800, pedidos_por_minuto=20, pedidos_por_dia=2000, folga_s=0)
    base.update(limites)
    return Cota(db=db, relogio=relogio or _Relogio(), limites=lambda: CotaGratuitaGroq(**base))


@pytest.mark.asyncio
async def test_cota_comeca_cheia_e_gastar_desconta():
    cota = _cota()
    assert await cota.espera(600) == (0.0, "")
    await cota.gastar(600)
    estado = await cota.estado()
    assert estado["audio_hora"]["restante"] == 6600
    assert estado["audio_dia"]["restante"] == 28200
    assert estado["pedidos_dia"]["restante"] == 1999


@pytest.mark.asyncio
async def test_balde_enche_com_o_tempo_como_o_do_groq():
    """3 s de áudio voltam em 1,5 s na hora (3600/7200), medido nos cabeçalhos."""
    relogio = _Relogio()
    cota = _cota(relogio=relogio)
    await cota.gastar(7200)
    espera, balde = await cota.espera(3)
    assert balde == "audio_hora" and espera == pytest.approx(1.5)
    relogio.t += 1.5
    assert (await cota.espera(3))[0] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_maior_que_o_balde_nunca_cabe():
    cota = _cota(audio_s_por_hora=100)
    espera, balde = await cota.espera(101)
    assert espera == math.inf and balde == "audio_hora"


@pytest.mark.asyncio
async def test_folga_sai_da_capacidade_de_audio():
    cota = _cota(audio_s_por_hora=7200, folga_s=30)
    assert cota.capacidades()["audio_hora"][0] == 7170
    assert cota.capacidades()["pedidos_dia"][0] == 2000


@pytest.mark.asyncio
async def test_cabecalho_do_groq_puxa_o_nivel_para_baixo_nunca_para_cima():
    cota = _cota()
    await cota.sincronizar({"x-ratelimit-remaining-audio-seconds": "5000", "x-ratelimit-remaining-requests": "1500"})
    estado = await cota.estado()
    assert estado["audio_hora"]["restante"] == 5000
    assert estado["pedidos_dia"]["restante"] == 1500
    await cota.sincronizar({"x-ratelimit-remaining-audio-seconds": "7200"})
    assert (await cota.estado())["audio_hora"]["restante"] == 5000


@pytest.mark.asyncio
async def test_cota_sobrevive_ao_reload(settings):
    db = FakeDB()
    relogio = _Relogio()
    await _cota(db=db, relogio=relogio).gastar(3600)
    salvo = json.loads(db.prefs[("neo", "transcricao", "cota")])
    assert salvo["niveis"]["audio_hora"] == 3600

    nova = _cota(db=db, relogio=relogio)  # processo novo depois do --reload
    assert (await nova.estado())["audio_hora"]["restante"] == 3600


@pytest.mark.asyncio
async def test_devolver_nao_passa_da_capacidade():
    cota = _cota()
    await cota.devolver(500)
    assert (await cota.estado())["audio_hora"]["restante"] == 7200
