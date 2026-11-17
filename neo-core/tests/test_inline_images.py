"""Imagens no chat: o marcador `[[IMAGE:…]]` e as regras que o cercam.

O turno real que motivou a feature: "carrega uma imagem aqui no chat pra eu ver" →
o modelo rodou `base64 -i foto.jpeg`, jogou o blob no contexto e o usuário não viu
imagem nenhuma. Aqui garantimos as três bordas do lado do core:

1. quem RECEBE a instrução (só cliente que renderiza o marcador — senão o usuário
   leria `[[IMAGE:…]]` cru);
2. a proibição do base64 nas regras de shell (vale para todo cliente com shell) e a
   alternativa (o marcador), que é condicional;
3. o site CLIENT_TOOL (`_format_tool_result`), onde o caminho da imagem costuma
   aparecer (saída de `ls`/`find`) e antes terminava em "está em ~/Downloads".

O parse/render do marcador é do cliente (neo-desktop-tauri: src/lib/inlineImages.ts
+ o comando Rust load_chat_image) — o core só instrui e nunca vê os bytes.
"""

from __future__ import annotations

import pytest

from core.router.intent_router import ROUTER_SYSTEM_PROMPT
from modules.desktop.ai_service import (
    _INLINE_IMAGE_INSTRUCTION,
    INLINE_IMAGE_MARKER_PREFIX,
    AIService,
)
from shared.config import get_settings


class _FakeResp:
    def get_content(self) -> str:
        return "resposta"


class _CaptureProvider:
    """Provider falso que captura as mensagens enviadas ao LLM."""

    model = "test-model"

    def __init__(self) -> None:
        self.messages = None

    async def complete(self, messages, model=None, temperature=0.0,
                       max_tokens=0, **kwargs):
        self.messages = messages
        return _FakeResp()


class _FakeLLM:
    def __init__(self, provider) -> None:
        self._p = provider

    def get_provider(self):
        return self._p


def _service(provider) -> AIService:
    # Evita o __init__ pesado (router/db/agent/...); só precisamos de llm + settings.
    svc = AIService.__new__(AIService)
    svc.llm = _FakeLLM(provider)
    svc.settings = get_settings()
    return svc


def _desktop_metadata(**extra: str) -> dict[str, str]:
    return {
        "client": "neo-desktop-tauri",
        "os_platform": "darwin",
        "home_dir": "/Users/daniel",
        **extra,
    }


# ── 1. Quem recebe a instrução ──────────────────────────────────────────────


def test_desktop_renderiza_marcador():
    assert AIService._client_renders_inline_images(_desktop_metadata()) is True


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {"client": "neo-panel"},
        {"client": "neo-whatsapp", "os_platform": "linux"},
    ],
)
def test_cliente_sem_render_nao_recebe(metadata):
    assert AIService._client_renders_inline_images(metadata) is False


def test_opt_in_explicito_de_outro_cliente():
    # Um cliente novo pode optar sem que o core precise conhecê-lo pelo nome.
    assert (
        AIService._client_renders_inline_images(
            {"client": "neo-mobile", "supports_inline_images": "true"}
        )
        is True
    )


def test_instrucao_proibe_base64_e_caminho_inventado():
    assert INLINE_IMAGE_MARKER_PREFIX in _INLINE_IMAGE_INSTRUCTION
    assert "base64" in _INLINE_IMAGE_INSTRUCTION
    assert "NUNCA invente" in _INLINE_IMAGE_INSTRUCTION
    assert "file:<file_id>" in _INLINE_IMAGE_INSTRUCTION


def test_router_manda_exibicao_de_imagem_pro_cerebro():
    # O turno real foi classificado como CLIENT_TOOL (`base64 -i foto.jpeg`); a
    # regra existe para que "mostra a imagem" com caminho conhecido vire
    # DIRECT_RESPONSE, onde o marcador pode ser escrito.
    assert INLINE_IMAGE_MARKER_PREFIX in ROUTER_SYSTEM_PROMPT
    assert "base64" in ROUTER_SYSTEM_PROMPT


# ── 2. Regras de shell ──────────────────────────────────────────────────────


def test_shell_rules_proibem_base64_para_qualquer_cliente():
    rules = AIService._build_shell_rules("darwin", can_show_images=False)
    assert "base64" in rules
    assert INLINE_IMAGE_MARKER_PREFIX not in rules  # sem render, sem marcador


def test_shell_rules_oferecem_marcador_quando_o_cliente_renderiza():
    rules = AIService._build_shell_rules("darwin", can_show_images=True)
    assert INLINE_IMAGE_MARKER_PREFIX in rules


def test_system_context_do_desktop_menciona_o_marcador():
    ctx = AIService._build_system_context(_desktop_metadata())
    assert ctx is not None
    assert INLINE_IMAGE_MARKER_PREFIX in ctx


def test_system_context_de_outro_cliente_nao_menciona_o_marcador():
    ctx = AIService._build_system_context({"os_platform": "linux", "client": "neo-panel"})
    assert ctx is not None
    assert "base64" in ctx  # a proibição continua valendo
    assert INLINE_IMAGE_MARKER_PREFIX not in ctx


# ── 3. Resposta de um client tool (onde o caminho aparece) ──────────────────


@pytest.mark.asyncio
async def test_format_tool_result_libera_marcador():
    prov = _CaptureProvider()
    await _service(prov)._format_tool_result(
        "mostra a imagem", "ls ~/Downloads", "foto.jpg", can_show_images=True
    )
    prompt = prov.messages[-1].content
    assert INLINE_IMAGE_MARKER_PREFIX in prompt
    assert "nunca invente" in prompt


@pytest.mark.asyncio
async def test_format_tool_result_sem_capacidade_nao_cita_marcador():
    prov = _CaptureProvider()
    await _service(prov)._format_tool_result(
        "mostra a imagem", "ls ~/Downloads", "foto.jpg", can_show_images=False
    )
    assert INLINE_IMAGE_MARKER_PREFIX not in prov.messages[-1].content
