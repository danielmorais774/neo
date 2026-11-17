"""Pediu o navegador DELE? Então o turno é do cognitivo, custe o que custar.

A tool ``browser_local_task`` só existe no núcleo cognitivo. Quando o roteador
manda a mensagem para outro trilho, o portão do §5 nunca roda e o pedido vira
job no Chromium HEADLESS do servidor — o usuário fica olhando a tela dele, onde
nada acontece.

Foi o que houve em 2026-08-28: o classificador caiu (OpenRouter 403), o fallback
por palavra-chave viu "busca" em *"deveria ter buscado na barra de busca"* e
devolveu SERVER_TOOL. O turno virou o job `1ac2277b`, que terminou em
`about:blank`.
"""

from __future__ import annotations

import pytest

from core.router.intent_router import IntentRouter
from shared import IntentType, RoutingDecision


def _router() -> IntentRouter:
    return object.__new__(IntentRouter)


def _decision(intent: IntentType, **over) -> RoutingDecision:
    base = dict(intent=intent, confidence=0.5, explanation="teste")
    base.update(over)
    return RoutingDecision(**base)


# A frase real do turno que quebrou, e as vizinhas dela.
_PEDIDOS_LOCAIS = [
    "voce so abriu na home e la ficou procurando, deveria ter buscado na barra "
    "de busca do meu navegador local, tenta de novo",
    "era para ser no meu navegador local",
    "abre o meu drive no meu navegador local e baixa a pasta Print",
]

# Tarefa de navegador SEM âncora de posse — continua sendo o headless.
_PEDIDOS_GENERICOS = [
    "pesquisa o preço da geladeira frost free",
    "procura no google quem ganhou o jogo ontem",
    "abre o site da receita e vê o prazo do IR",
]


@pytest.mark.parametrize("message", _PEDIDOS_LOCAIS)
def test_pedido_do_navegador_do_usuario_vai_para_o_cognitivo(message, monkeypatch):
    monkeypatch.setattr(
        "shared.config.get_settings",
        lambda: type("S", (), {"browser_local_enabled": True})(),
    )
    decision = _router()._bias_local_browser(
        _decision(IntentType.SERVER_TOOL, tool_name="get_weather"), message
    )
    assert decision.intent == IntentType.COGNITIVE
    assert decision.tool_name is None
    assert decision.needs_memory is True


@pytest.mark.parametrize("message", _PEDIDOS_GENERICOS)
def test_pesquisa_generica_nao_e_sequestrada(message, monkeypatch):
    monkeypatch.setattr(
        "shared.config.get_settings",
        lambda: type("S", (), {"browser_local_enabled": True})(),
    )
    decision = _router()._bias_local_browser(_decision(IntentType.SERVER_TOOL), message)
    assert decision.intent == IntentType.SERVER_TOOL


def test_com_o_navegador_local_desligado_o_backstop_nao_existe(monkeypatch):
    """Sem a feature, promover ao cérebro só tiraria o turno do trilho certo."""
    monkeypatch.setattr(
        "shared.config.get_settings",
        lambda: type("S", (), {"browser_local_enabled": False})(),
    )
    decision = _router()._bias_local_browser(
        _decision(IntentType.SERVER_TOOL), "era para ser no meu navegador local"
    )
    assert decision.intent == IntentType.SERVER_TOOL


def test_decisao_que_ja_era_cognitiva_passa_intacta(monkeypatch):
    monkeypatch.setattr(
        "shared.config.get_settings",
        lambda: type("S", (), {"browser_local_enabled": True})(),
    )
    original = _decision(IntentType.COGNITIVE, explanation="veio assim")
    decision = _router()._bias_local_browser(original, "abre no meu navegador local")
    assert decision.explanation == "veio assim"


def test_fallback_por_palavra_chave_tambem_e_corrigido(monkeypatch):
    """O caminho onde o bug NASCEU: provider caído → heurística → SERVER_TOOL."""
    monkeypatch.setattr(
        "shared.config.get_settings",
        lambda: type("S", (), {"browser_local_enabled": True})(),
    )
    router = _router()
    message = (
        "voce so abriu na home e ficou procurando, deveria ter usado a barra de "
        "busca no meu navegador local, tenta de novo"
    )
    cru = router._fallback_classify(message)
    assert cru.intent == IntentType.SERVER_TOOL, "o fallback ainda erra sozinho"
    assert router._bias_local_browser(cru, message).intent == IntentType.COGNITIVE
