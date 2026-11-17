"""Tools do cognitivo ficam FORA da palette do classificador.

``file_*`` (docs/file-store-plan.md §4) e ``service_*``
(docs/service-gateway-dsl-plan.md) são decisão do cérebro: o cognitivo descobre
(service_list / manifesto do anexo) e só então age. Se o classificador as
enxergasse, ele rotearia direto — inventando service/path/body ou file_id sem
passar pela descoberta (o mesmo bypass que fazia a skill HA alucinar entity_id).
"""

from core.files.tools import FILE_TOOLS
from core.router.intent_router import _SKILL_OWNED_TOOL_PREFIXES
from modules.services.tools import SERVICE_TOOLS


def test_file_tools_are_excluded_from_router_palette():
    for tool in FILE_TOOLS:
        assert tool.name.startswith(_SKILL_OWNED_TOOL_PREFIXES), (
            f"{tool.name} apareceria na palette do classificador"
        )


def test_service_tools_are_excluded_from_router_palette():
    for tool in SERVICE_TOOLS:
        assert tool.name.startswith(_SKILL_OWNED_TOOL_PREFIXES), (
            f"{tool.name} apareceria na palette do classificador"
        )


def test_agent_owned_prefixes_still_excluded():
    """Guarda de regressão dos prefixos que já eram do cérebro."""
    for name in ("neo_bash", "job_status", "skill_read", "web_search", "fin_add"):
        assert name.startswith(_SKILL_OWNED_TOOL_PREFIXES)


def test_router_prompt_forbids_treating_attachment_as_local_file():
    """Bug de prod (session a65830cf): o follow-up "de qual instituição de ensino?"
    virou CLIENT_TOOL read_file num path INVENTADO (/Users/daniel/Downloads/form.html,
    que não existe). Excluir as file_* da palette não basta — o classificador ainda
    tinha read_file e um nome de arquivo no histórico.
    """
    from core.router.intent_router import ROUTER_SYSTEM_PROMPT

    assert "ARQUIVOS ANEXADOS NESTA MENSAGEM" in ROUTER_SYSTEM_PROMPT
    assert "NUNCA invente um caminho" in ROUTER_SYSTEM_PROMPT
