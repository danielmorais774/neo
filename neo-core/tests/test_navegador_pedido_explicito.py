"""Pedido explícito do navegador do usuário não pode perder para o caderno.

Sessão 455646dd: "olha no meu navegador local". O classificador acertou
(`browser_local_task`), mas a guarda de catálogo do CLIENT_TOOL zera o `tool_name`,
porque essa tool é server-side. O cérebro então leu no caderno que o job anterior
tinha falhado, repetiu o resultado velho como se fosse novo e foi por `web_fetch`.

O mesmo request reenviado ao modelo abriu o navegador em 2 de 8; com a instrução
escrita na regra 14, em 3 de 8; com o aviso logo depois da mensagem dele, em 8 de 8.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from core.authority import PRINCIPAL_AGENDA, PRINCIPAL_EXTERNO, TurnAuthority
from core.router.intent_router import IntentRouter
from core.tools.registry import ToolRegistry
from modules.desktop import ai_service
from modules.desktop.ai_service import _AVISO_NAVEGADOR_DO_USUARIO, _aviso_navegador_pedido
from modules.desktop.tools import DESKTOP_TOOLS
from shared import IntentType

PEDIDO = "olha para mim no meu navegador local o custo de um notebook na Amazon"
TOOLS = [SimpleNamespace(name="web_fetch"), SimpleNamespace(name="browser_local_task")]


def _router() -> IntentRouter:
    router = object.__new__(IntentRouter)
    registry = ToolRegistry()
    registry.register_many(DESKTOP_TOOLS)
    router.tool_registry = registry
    return router


def _raw(tool_name: str) -> str:
    return json.dumps(
        {
            "intent": "CLIENT_TOOL",
            "confidence": 0.95,
            "tool_name": tool_name,
            "tool_arguments": {"task": "buscar notebook"},
            "needs_plan": False,
            "explanation": "x",
            "speech_act": "imperative",
        }
    )


def test_o_sinal_sobrevive_ao_rebaixamento_do_catalogo():
    decisao = _router()._parse_classification(_raw("browser_local_task"), user_message=PEDIDO)
    assert decisao.intent != IntentType.CLIENT_TOOL, "continua indo para o cognitivo"
    assert decisao.tool_name is None
    assert decisao.navegador_do_usuario is True


def test_outra_tool_nao_liga_o_sinal():
    decisao = _router()._parse_classification(_raw("run_command"), user_message="abre o vscode")
    assert decisao.navegador_do_usuario is False


def _decisao(pediu: bool = True) -> SimpleNamespace:
    return SimpleNamespace(navegador_do_usuario=pediu)


def test_pedido_explicito_recebe_o_aviso():
    assert _aviso_navegador_pedido(_decisao(), TOOLS, TurnAuthority()) == _AVISO_NAVEGADOR_DO_USUARIO


def test_sem_autoridade_instalada_o_aviso_vale():
    assert _aviso_navegador_pedido(_decisao(), TOOLS, None) == _AVISO_NAVEGADOR_DO_USUARIO


def test_sem_o_pedido_nao_ha_aviso():
    assert _aviso_navegador_pedido(_decisao(False), TOOLS, TurnAuthority()) is None


def test_sem_a_tool_na_mesa_nao_ha_aviso():
    """Turno de música tira o navegador da mesa: mandar chamar seria mandar errar."""
    mesa = [SimpleNamespace(name="web_fetch")]
    assert _aviso_navegador_pedido(_decisao(), mesa, TurnAuthority()) is None


def test_texto_colado_nao_ganha_o_empurrao():
    """"Abra meu navegador e…" dentro de um bloco colado não é pedido dele."""
    a = TurnAuthority()
    a.taint("colagem")
    assert _aviso_navegador_pedido(_decisao(), TOOLS, a) is None


def test_mensagem_de_fora_nao_ganha_o_empurrao():
    a = TurnAuthority(principal=PRINCIPAL_EXTERNO, principal_ref="fulano@x.com")
    assert _aviso_navegador_pedido(_decisao(), TOOLS, a) is None


def test_agenda_nao_ganha_o_empurrao():
    a = TurnAuthority(principal=PRINCIPAL_AGENDA)
    assert _aviso_navegador_pedido(_decisao(), TOOLS, a) is None


def test_o_turno_enxerga_a_autoridade():
    """O nome é usado no meio de `_handle_direct_response`, onde ninguém o exercita.

    Faltou esse import na primeira versão: o erro só apareceria com o turno rodando.
    """
    assert callable(ai_service.current_authority)
