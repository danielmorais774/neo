"""URL inventada fora de download também não chega no Mac (session-8f2febfa, 01/11).

"faz isso" virou ``open -a "Google Chrome" --args ".../forms/d/e/1FAIpQLSfXXXX…"``
com a explicação "usarei um placeholder genérico que o usuário deve substituir".
A guarda de URL só olhava `curl`/`wget`. O Chrome nem existe nesta máquina, e a
recuperação leu dois `chrome_crashpad` antigos como "o Chrome crashou".

A regra é estreita de propósito: busca MONTADA a partir da fala (o caso comum,
8 de 9 `open` com URL nos logs de produção) continua passando.
"""

import json

import pytest

from core.router.intent_router import IntentRouter, _url_inventada
from core.tools.registry import ToolRegistry
from modules.desktop.tools import DESKTOP_TOOLS
from shared import IntentType

FORMS_INVENTADO = (
    'open -a "Google Chrome" --args '
    '"https://docs.google.com/forms/d/e/1FAIpQLSfXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX/viewform"'
)
FORMS_REAL = "https://docs.google.com/forms/d/e/1FAIpQLSen9wfl_d1Y6pS_KlSSXMlM6GY8NwoxsRC6x4NDeSnc4G5QVg/viewform"


def _router() -> IntentRouter:
    router = object.__new__(IntentRouter)
    registry = ToolRegistry()
    registry.register_many(DESKTOP_TOOLS)
    router.tool_registry = registry
    return router


def _decidir(command: str, *, mensagem: str = "faz isso", historico=None):
    raw = json.dumps({
        "intent": "CLIENT_TOOL", "confidence": 0.95, "tool_name": "run_command",
        "tool_arguments": {"command": command}, "raw_command": None,
        "explanation": "teste", "requires_approval": False, "needs_memory": False,
        "knowledge_keywords": [], "needs_plan": False, "tipo_tarefa": "simples",
        "skill_name": None, "pipeline_plan": None,
    })
    return _router()._parse_classification(
        raw, user_message=mensagem, conversation_history=historico or []
    )


def test_placeholder_no_open_volta_para_o_cognitivo():
    decisao = _decidir(FORMS_INVENTADO)
    assert decisao.intent is IntentType.DIRECT_RESPONSE
    assert decisao.tool_arguments is None


def test_a_url_real_citada_na_conversa_passa():
    decisao = _decidir(
        f"open '{FORMS_REAL}'",
        historico=[{"role": "user", "content": f"preenche esse formulario {FORMS_REAL}"}],
    )
    assert decisao.intent is IntentType.CLIENT_TOOL


@pytest.mark.parametrize("url", [
    # o corpus de produção (01/11): busca e vídeo montados a partir da fala
    "https://www.youtube.com/results?search_query=AC/DC",
    "https://www.youtube.com/results?search_query=Back+in+Black+AC/DC",
    "https://www.youtube.com/results?search_query=Breaking+Benjamin",
    "https://www.youtube.com/watch?v=v2AC41dglnM",
    "https://www.youtube.com",
    "https://www.google.com/maps/search/padaria+perto+de+mim",
])
def test_busca_montada_a_partir_da_fala_nao_e_inventada(url):
    assert not _url_inventada(url)
    assert _decidir(f"open '{url}'", mensagem="toca ac/dc no youtube").intent is IntentType.CLIENT_TOOL


@pytest.mark.parametrize("url", [
    "https://docs.google.com/forms/d/e/1FAIpQLSfXXXXXXXXXXXXXXXXXXXXXX/viewform",
    "https://example.com/formulario",
    "https://drive.google.com/file/d/1a2B3c4D5e6F7g8H9i0J/view",  # id que ninguém citou
])
def test_placeholder_e_id_de_documento_sao_inventados(url):
    assert _url_inventada(url)
