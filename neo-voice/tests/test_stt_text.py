"""Testes do corretor de saida do ASR (service/stt_text.py).

As fixtures POSITIVAS sao transcricoes REAIS do corpus de producao (turnos com
msg_metadata->>'mode'='live' no Postgres, 02/08/2026) -- nao inventadas. As
NEGATIVAS existem para provar precisao: texto que nao pode ser tocado.

Rodar:  python3 -m pytest neo-voice/tests/test_stt_text.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service"))

import stt_text  # noqa: E402
from stt_text import normalize_stt_text  # noqa: E402


# ---------------------------------------------------------------------------
# o nome (casos reais do corpus)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("Nil, você se lembra do Lorenzo?", "Neo, você se lembra do Lorenzo?"),
    ("Foi tá bom, Nir. Foi tá bom.", "Foi tá bom, Neo. Foi tá bom."),
    ("E aí, Neil, como vai?", "E aí, Neo, como vai?"),
    ("Oi, Neil, como você vai?", "Oi, Neo, como você vai?"),
    ("Neil que hora são", "Neo que hora são"),
    ("Fala aí de novo, Nil.", "Fala aí de novo, Neo."),
    ("Nil, fala aí um pouquinho, qualquer coisa.", "Neo, fala aí um pouquinho, qualquer coisa."),
    ("Agora, Neil, bota uma música de homem aí para nós escutar.",
     "Agora, Neo, bota uma música de homem aí para nós escutar."),
    ("Neil, o que você acha de um cara que larga gameplay no meio da noite.",
     "Neo, o que você acha de um cara que larga gameplay no meio da noite."),
    # variante FRACA ("new" e palavra de verdade): so com chamamento explicito
    ("Alô New Testando.", "Alô Neo Testando."),
    ("Alô, testando a New.", "Alô, testando a Neo."),
    ("Vamos lá, testando o new.", "Vamos lá, testando o Neo."),
    # ja esta certo -> intocado
    ("Neo, que horas são?", "Neo, que horas são?"),
])
def test_corrige_o_nome(raw, expected):
    assert normalize_stt_text(raw).text == expected


def test_fix_registra_a_regra_e_preserva_o_cru():
    res = normalize_stt_text("Nil, você se lembra do Lorenzo?")
    assert res.raw == "Nil, você se lembra do Lorenzo?"
    assert res.fixes == [{"from": "Nil", "to": "Neo", "rule": "vocative-strong"}]
    assert res.changed is True


def test_camada_fonetica_pega_variante_nao_catalogada():
    # "Nieu" nao esta em lista nenhuma; a chave fonetica a aproxima de "neo"
    res = normalize_stt_text("Oi, Nieu, tudo bem?")
    assert res.text == "Oi, Neo, tudo bem?"
    assert res.fixes[0]["rule"] in ("phonetic", "vocative-strong")


# ---------------------------------------------------------------------------
# precisao: o que NAO pode ser tocado
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    "Não sei, cara.",
    "No começo eu achei ruim.",
    "por que isso retorna nil?",          # conversa de dev, nao vocativo
    "o ponteiro veio nil de novo",
    "Comprei um celular novo ontem.",
    "Nunca mais faço isso.",
    "Nós fomos no mercado.",
    "Me manda o link do New York Times.",  # "new" sem chamamento
    "Quero um notebook novo, não um usado.",
    "A nova versão saiu.",
    "Ele é meu nível.",
    "Alexa, desliga a luz do quarto.",
])
def test_nao_mexe_no_que_esta_certo(raw):
    res = normalize_stt_text(raw)
    assert res.text == raw
    assert res.fixes == []
    assert res.dropped is None


# Regressao da 1a rodada do eval: a chave fonetica de "não" ("nau") e a de "no"
# ("nu") ficam a distancia 1 de "niu" -- sem a keep-list de pt o corretor trocou
# 14 dessas por "Neo" em 368 turnos ("crianças, né?" -> "crianças, Neo?").
@pytest.mark.parametrize("raw", [
    "Mano, mas deve ter umas crianças, né?",
    "Ela não botou pra tocar, não. Acho que um.",
    "É no YouTube, cara.",
    "Sim, mas você não está certo, não. Não fala assim, não.",
    "Ainda bem que sabe que é ruim, né?",
    "Mas não era um beta, uma porra assim era, não?",
])
def test_fonetica_nao_come_palavra_pt(raw):
    assert normalize_stt_text(raw).text == raw


def test_keep_list_vence_tudo(monkeypatch):
    monkeypatch.setenv("NEO_STT_LEXICON_KEEP", '["Neil"]')
    stt_text._reload_config()
    try:
        assert normalize_stt_text("Oi, Neil, tudo bem?").text == "Oi, Neil, tudo bem?"
    finally:
        monkeypatch.delenv("NEO_STT_LEXICON_KEEP", raising=False)
        stt_text._reload_config()


# ---------------------------------------------------------------------------
# eco do wake word / grunhido
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    "He knew.", "A new.", "A new", "And you", "Any", "How", "Hey Live.",
    "Come by", "Her best.", "Mm-hmm.", "Mm.", "Hm.",
])
def test_descarta_eco_do_wake_word(raw):
    res = normalize_stt_text(raw)
    assert res.text == ""
    assert res.dropped == "wakeword_echo"
    assert res.raw == raw


@pytest.mark.parametrize("raw", [
    "Como vai?",
    "How are you doing today my friend",   # >3 palavras: nao e eco
    "Any coisa me avisa",
    "Ah, entendi agora.",
])
def test_nao_descarta_turno_de_verdade(raw):
    assert normalize_stt_text(raw).dropped is None


def test_parcial_nao_descarta():
    res = normalize_stt_text("A new.", allow_drop=False)
    assert res.dropped is None


# ---------------------------------------------------------------------------
# frases coladas + kill switch
# ---------------------------------------------------------------------------
def test_frase_colada():
    res = normalize_stt_text("Desligaliz do quarto.")
    assert res.text == "Desliga a luz do quarto."   # preserva a maiuscula da frase
    assert res.fixes[0]["rule"] == "phrase-lexicon"


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("NEO_STT_NORMALIZE", "0")
    res = normalize_stt_text("Nil, você se lembra do Lorenzo?")
    assert res.text == "Nil, você se lembra do Lorenzo?"
    assert res.fixes == []


def test_texto_vazio_nao_quebra():
    assert normalize_stt_text("").text == ""
    assert normalize_stt_text(None).text == ""
