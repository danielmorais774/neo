"""AgentResponse.files (gRPC) — paridade com o download de arquivo do neo-panel.

O REST/NDJSON (api/routes/desktop.py) já expunha `files` pro neo-panel; o
desktop-tauri usa gRPC e o proto (AgentResponse) não carregava esse campo — o
file_id de um `file_save` bem-sucedido nunca chegava no cliente, então não
tinha como oferecer o botão de download. Estes testes travam a paridade.
"""

from uuid import uuid4

from grpc_server import NeoDesktopServicer
from modules.desktop.ai_service import AIServiceResponse
from shared.types import IntentType


def _produced_files():
    return [{"file_id": "abc-123", "name": "naruto-site.zip", "size_bytes": 9003464}]


def test_final_response_carries_files():
    resp = AIServiceResponse(
        intent=IntentType.DIRECT_RESPONSE,
        session_id=uuid4(),
        message="Zip pronto.",
        is_final=True,
        metadata={"files": _produced_files()},
    )
    core_msg = NeoDesktopServicer._to_core_message(str(resp.session_id), resp)
    assert core_msg.response.files[0].id == "abc-123"
    assert core_msg.response.files[0].name == "naruto-site.zip"
    assert core_msg.response.files[0].size_bytes == 9003464


def test_persist_reply_intermediate_carries_files():
    """Handoff de música (persist_reply, is_final=False) também deve levar
    files — a mesma mensagem pode carregar um arquivo já entregue."""
    resp = AIServiceResponse(
        intent=IntentType.DIRECT_RESPONSE,
        session_id=uuid4(),
        message="Zip pronto, tocando a música agora.",
        is_final=False,
        metadata={"persist_reply": True, "files": _produced_files()},
    )
    core_msg = NeoDesktopServicer._to_core_message(str(resp.session_id), resp)
    assert core_msg.response.files[0].id == "abc-123"


def test_final_response_without_files_is_empty():
    resp = AIServiceResponse(
        intent=IntentType.DIRECT_RESPONSE,
        session_id=uuid4(),
        message="Oi.",
        is_final=True,
    )
    core_msg = NeoDesktopServicer._to_core_message(str(resp.session_id), resp)
    assert list(core_msg.response.files) == []
