"""Entrada por voz: o sinal `input_source=voice` e o backstop do nome.

Contexto medido (Postgres de produção, 368 turnos com `mode='live'`, 02/08/2026):
o Parakeet escreveu "Neo" ZERO vezes — escreveu Neil (6), New (6), Nil (3),
knew (2), Nir. O corretor completo mora no neo-voice (service/stt_text.py); aqui
testamos as duas bordas do core:

1. `input_source` é sinal EXPLÍCITO do cliente e não pode ser inferido de
   `mode="live"` (aquele fala da saída falada, este da entrada transcrita);
2. o backstop determinístico do nome só corrige em posição de vocativo — o usuário
   é dev e fala "retorna nil" sem estar chamando ninguém.

Ver docs/voice-input-normalization-plan.md.
"""

from __future__ import annotations

import pytest

from core.text.asr_fixups import canonicalize_assistant_name, is_voice_input
from modules.desktop.ai_service import _VOICE_INPUT_INSTRUCTION, AIService


# ---------------------------------------------------------------------------
# 1. o sinal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("metadata,expected", [
    ({"input_source": "voice"}, True),
    ({"input_source": "VOICE"}, True),
    ({"input_source": " voice "}, True),
    ({"input_source": "text"}, False),
    ({"input_source": ""}, False),
    ({}, False),
    (None, False),
    # o ponto central: modo Live é ESTILO DE SAÍDA, não origem da entrada
    ({"mode": "live"}, False),
    ({"mode": "live", "input_source": "voice"}, True),
])
def test_is_voice_input(metadata, expected):
    assert AIService._is_voice_input(metadata) is expected
    assert is_voice_input(metadata) is expected


def test_instrucao_entra_pelo_system_context():
    """Todo caminho que reconstrói o turno passa por _build_system_context — é lá
    que a instrução mora, senão a retomada (plano, arquivo, client tool) perderia."""
    meta = {"os_platform": "darwin", "home_dir": "/Users/x", "input_source": "voice"}
    ctx = AIService._build_system_context(meta)
    assert _VOICE_INPUT_INSTRUCTION in ctx
    assert "Sistema operacional" in ctx  # não substituiu o contexto de SO, somou

    typed = AIService._build_system_context({**meta, "input_source": "text"})
    assert _VOICE_INPUT_INSTRUCTION not in typed
    # e o modo Live sozinho (saída falada) não liga a tolerância de entrada
    live_only = AIService._build_system_context({"os_platform": "darwin", "mode": "live"})
    assert _VOICE_INPUT_INSTRUCTION not in live_only


def test_instrucao_cobre_as_4_regras():
    """A instrução perde o valor se algum bullet sumir num refactor de prompt."""
    txt = _VOICE_INPUT_INSTRUCTION
    assert "TRANSCRITA" in txt
    assert "FONÉTICA" in txt
    # nome: as variantes reais têm que estar citadas
    for variant in ("Neil", "Nil", "New", "He knew"):
        assert variant in txt
    # a regra que impede o token torto de virar caminho/id inventado
    assert "NUNCA transforme um token estranho" in txt
    assert "entity_id" in txt
    # e a que impede o "não entendi" por erro pequeno
    assert "não entendi" in txt


# ---------------------------------------------------------------------------
# 2. o backstop do nome (casos reais do corpus)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("Neil, que horas são?", "Neo, que horas são?"),
    ("Neil que hora são", "Neo que hora são"),
    ("Foi tá bom, Nir. Foi tá bom.", "Foi tá bom, Neo. Foi tá bom."),
    ("E aí, Neil, como vai?", "E aí, Neo, como vai?"),
    ("Nil, você se lembra do Lorenzo?", "Neo, você se lembra do Lorenzo?"),
    ("Alô New testando", "Alô Neo testando"),
    ("Oi, Neil, como você vai?", "Oi, Neo, como você vai?"),
])
def test_canonicaliza_o_nome_em_vocativo(raw, expected):
    fixed, variants = canonicalize_assistant_name(raw)
    assert fixed == expected
    assert variants


@pytest.mark.parametrize("raw", [
    "por que isso retorna nil?",           # dev falando de código, não chamando
    "o ponteiro veio nil de novo",
    "Comprei um celular novo ontem.",
    "Me manda o link do New York Times.",
    "A nova versão do New Relic subiu.",
    "Não sei, cara.",
    "Neo, que horas são?",                 # já está certo
])
def test_nao_toca_no_que_nao_e_vocativo(raw):
    fixed, variants = canonicalize_assistant_name(raw)
    assert fixed == raw
    assert variants == []


def test_nunca_levanta():
    assert canonicalize_assistant_name("") == ("", [])
    assert canonicalize_assistant_name(None) == (None, [])
