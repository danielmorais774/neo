"""A tool ``neo_transcrever`` e o executor de módulo dela.

O prefixo ``neo_`` põe a tool na família do sandbox e a deixa só para o cérebro:
o classificador não a vê (``_SKILL_OWNED_TOOL_PREFIXES``) e o trilho SERVER_TOOL
não a executa (``server_tool_can_run``). Quem chama sabe onde o arquivo está.

Ela entra no toolset só quando há chave do Groq (``config.disponivel()``, conferido
em ``AIService._agent_action_tools``). Sem chave, oferecer a tool seria prometer
o que não roda.
"""

from __future__ import annotations

from typing import Any, Callable

from shared import ToolParameter, ToolSchema

TOOL_NAME = "neo_transcrever"

TRANSCRICAO_TOOLS: list[ToolSchema] = [
    ToolSchema(
        # Literal, não TOOL_NAME: tests/authority/test_risk_cobre_o_registry.py
        # acha as tools varrendo o fonte por `ToolSchema(name="…")`.
        name="neo_transcrever",
        description=(
            "Transcreve a FALA de um vídeo ou áudio (mp4, mov, mkv, webm, mp3, m4a, wav, ogg...) que está no "
            "SEU sandbox (/workspace) ou no catálogo (file_id). O sandbox só tira o áudio; quem transcreve é o "
            "Whisper do Groq, então é rápido e não pesa na máquina. Grava <nome>.transcricao.txt (texto) e "
            "<nome>.transcricao.srt (legenda com tempos) ao lado do arquivo e devolve o texto. Para a FALA, "
            "use sempre esta tool: não instale whisper, vosk nem outro reconhecedor no sandbox. O TEXTO que "
            "aparece na tela (gravação de tela de conversa, slides) não é fala: isso é OCR, com a skill "
            "ocr-video, mesmo que o usuário diga \"transcrever\". Arquivo no computador do usuário: traga "
            "com request_user_file antes. Vídeo da internet: baixe para o /workspace com neo_bash antes."
        ),
        module="transcricao",
        execution="server-side",
        # Teto do executor global. O fluxo tem os próprios prazos por etapa
        # ([transcricao] no neo.toml), que estouram antes deste.
        timeout=1800,
        parameters={
            "caminho": ToolParameter(
                type="string",
                description="Caminho do vídeo/áudio no /workspace (ex.: /workspace/files/reuniao.mp4)",
                required=False,
            ),
            "file_id": ToolParameter(
                type="string",
                description="Id do arquivo no catálogo, em vez do caminho",
                required=False,
            ),
            "idioma": ToolParameter(
                type="string",
                description="Código do idioma da fala (pt, en, es...). Padrão: pt. 'auto' detecta sozinho",
                required=False,
            ),
            "contexto": ToolParameter(
                type="string",
                description=(
                    "Nomes próprios e termos que aparecem na fala, para o Whisper acertar a grafia "
                    "(ex.: 'Neo, Daniel, Contabo'). Curto; não é instrução"
                ),
                required=False,
            ),
        },
    ),
]

TRANSCRICAO_TOOL_NAMES = frozenset(t.name for t in TRANSCRICAO_TOOLS)


class TranscricaoExecutor:
    """Executor de módulo (``execute(nome, args) -> dict``) do ``ToolExecutor`` global."""

    def __init__(
        self,
        *,
        sandbox: Any,
        db: Any = None,
        workspace_local: str = "/workspace",
        get_file_store: Callable[[], Any] = lambda: None,
    ) -> None:
        from core.transcricao.servico import Transcritor

        self.transcritor = Transcritor(
            sandbox=sandbox, db=db, workspace_local=workspace_local, get_file_store=get_file_store
        )

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name != TOOL_NAME:
            raise ValueError(f"tool desconhecida no módulo transcricao: {tool_name}")
        args = arguments or {}
        return await self.transcritor.transcrever(
            caminho=args.get("caminho"),
            file_id=args.get("file_id"),
            idioma=args.get("idioma"),
            contexto=args.get("contexto"),
        )
