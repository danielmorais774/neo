"""Sessão d9fae447 (13/11): o pedido de preencher o form saiu dos trilhos duas vezes.

Turno 1, "Preenche esse formulário no meu navegador local...": o classificador
devolveu sem tool, o sinal `navegador_do_usuario` ficou desligado, e com ele o
aviso de abrir o navegador. O cérebro leu no caderno a auditoria de OUTRA conversa
(`[[sessao-898e8fc0]]`, listada como "seu caderno da tarefa atual") e respondeu
"corrijo as divergências?".

Turno 2, "eu limpei o formulario para vc preencher do zero": o classificador
ACERTOU (`browser_local_task`, argumentos bons), mas mandou `needs_plan=true`. A
guarda de catálogo não roda com plano, então o planner refez o pedido do zero a
partir da mensagem crua: `ls`, `find`, `cat ~/Documents/form.html` e um selenium
no Mac dele.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.browser_local.conversa import MARCA_DO_BLOCO
from core.router.intent_router import IntentRouter
from core.tools.registry import ToolRegistry
from core.working_memory.render import render_index
from modules.desktop.tools import DESKTOP_TOOLS
from shared import IntentType, RoutingDecision

PEDIDO = (
    "Preenche esse formulário no meu navegador local e visual, quero ver acontecendo:\n"
    "https://docs.google.com/forms/d/e/x/viewform\n\n"
    "Me avisa aqui do que ele trata assim que entender. É um teste: não clica em Enviar no final."
)
LIMPEI = "eu limpei o formulario para vc preencher do zero"


def _router() -> IntentRouter:
    router = object.__new__(IntentRouter)
    registry = ToolRegistry()
    registry.register_many(DESKTOP_TOOLS)
    router.tool_registry = registry
    return router


def _raw(**over) -> str:
    data = {
        "intent": "CLIENT_TOOL",
        "confidence": 0.95,
        "tool_name": "browser_local_task",
        "tool_arguments": {"task": "preencher o form do zero", "visible": True},
        "needs_plan": True,
        "tipo_tarefa": "multi_etapas",
        "explanation": "x",
        "speech_act": "imperative",
    }
    data.update(over)
    return json.dumps(data)


@pytest.fixture
def ligado(monkeypatch):
    monkeypatch.setattr(
        "shared.config.get_settings",
        lambda: type("S", (), {"browser_local_enabled": True})(),
    )


# ── turno 2: a tool certa com plano ──────────────────────────────────────────

def test_browser_local_task_com_plano_vai_para_o_cognitivo():
    decisao = _router()._parse_classification(_raw(), user_message=LIMPEI)
    assert decisao.intent == IntentType.COGNITIVE
    assert decisao.needs_plan is False, "o planner não pode refazer o pedido sem a tool"
    assert decisao.tool_name is None
    assert decisao.navegador_do_usuario is True, "o cérebro recebe o aviso de abrir"


def test_sem_registry_tambem_nao_vira_plano():
    """A guarda de catálogo depende do registry; esta não pode depender."""
    router = object.__new__(IntentRouter)
    router.tool_registry = None
    decisao = router._parse_classification(_raw(), user_message=LIMPEI)
    assert decisao.intent == IntentType.COGNITIVE
    assert decisao.needs_plan is False


def test_outra_client_tool_com_plano_continua_planejando():
    decisao = _router()._parse_classification(
        _raw(tool_name="run_command", tool_arguments=None),
        user_message="manda pra mim aquele pdf da nota fiscal que tá no downloads",
    )
    assert decisao.intent == IntentType.CLIENT_TOOL
    assert decisao.needs_plan is True


# ── turno 1: pediu o navegador, mas o classificador não escolheu a tool ──────

def _decisao(intent=IntentType.COGNITIVE, speech_act="imperative") -> RoutingDecision:
    return RoutingDecision(intent=intent, confidence=0.7, explanation="x", speech_act=speech_act)


def test_pedido_explicito_liga_o_aviso_mesmo_sem_tool(ligado):
    decisao = _router()._bias_local_browser(_decisao(), PEDIDO)
    assert decisao.intent == IntentType.COGNITIVE
    assert decisao.navegador_do_usuario is True


def test_promovido_de_outro_trilho_tambem_liga(ligado):
    decisao = _router()._bias_local_browser(_decisao(IntentType.SERVER_TOOL), PEDIDO)
    assert decisao.intent == IntentType.COGNITIVE
    assert decisao.navegador_do_usuario is True


def test_pergunta_sobre_o_navegador_nao_liga(ligado):
    decisao = _router()._bias_local_browser(
        _decisao(speech_act="question"), "por que o meu navegador local travou ontem?"
    )
    assert decisao.navegador_do_usuario is False


def test_com_tarefa_viva_nao_liga(ligado):
    """Com tarefa rodando, o caminho é `job_falar`, não abrir outra."""
    contexto = f"...\n{MARCA_DO_BLOCO}\n- tarefa viva"
    decisao = _router()._bias_local_browser(_decisao(), PEDIDO, contexto)
    assert decisao.navegador_do_usuario is False


def test_sem_pedir_o_navegador_nao_liga(ligado):
    decisao = _router()._bias_local_browser(_decisao(), LIMPEI)
    assert decisao.navegador_do_usuario is False


# ── o caderno diz de onde vem a nota ─────────────────────────────────────────

AGORA = datetime(2026, 11, 13, 12, 23)


def _nota(slug, session_id):
    return SimpleNamespace(
        slug=slug, summary="Revisão do formulário AppDemo", title="Trabalho desta sessão",
        updated_at=datetime(2026, 11, 13, 2, 19), session_id=session_id,
    )


def test_nota_de_outra_conversa_sai_marcada():
    esta, outra = uuid4(), uuid4()
    texto = render_index(
        [_nota("sessao-esta", esta), _nota("sessao-898e8fc0", outra)],
        now=AGORA, session_id=esta,
    )
    assert "[[sessao-898e8fc0]] · outra conversa, ontem 23:19" in texto
    assert "[[sessao-esta]] · ontem 23:19" in texto
    assert "é de OUTRO pedido: se ele pede para fazer agora, faça" in texto


def test_sem_sessao_nada_e_marcado():
    texto = render_index([_nota("n", uuid4())], now=AGORA)
    assert "outra conversa" not in texto


def test_so_notas_desta_conversa_nao_pagam_a_explicacao():
    esta = uuid4()
    texto = render_index([_nota("n", esta)], now=AGORA, session_id=esta)
    assert "outra conversa" not in texto


def test_nota_sem_sessao_nao_e_marcada():
    texto = render_index([_nota("n", None)], now=AGORA, session_id=uuid4())
    assert "outra conversa," not in texto
