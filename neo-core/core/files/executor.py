"""Module executor das tools file_* (docs/file-store-plan.md §4).

Camada fina: traduz a tool call para o FileStore e devolve texto/estrutura
compacta pensada para o modelo relatar. Erros de negócio (FileStoreError) viram
mensagem clara em vez de stack — o modelo precisa entender e corrigir o rumo
(ler por fatia, materializar no sandbox, avisar o usuário sobre a quota).

Contexto (user/session) é injetado por request via ``set_context`` — o mesmo
padrão de devices/finance, porque ``file_save``/``file_list`` são por usuário.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from core.files.store import FileStore, FileStoreError

logger = logging.getLogger(__name__)


#: Imagens por chamada do file_olhar e teto de cada uma: uma página de documento
#: a 70 dpi tem ~60 KB; 4 MB é folga para print de tela, não para foto de câmera.
_OLHAR_MAX_IMAGENS = 4
_OLHAR_MAX_BYTES = 4 * 1024 * 1024
_OLHAR_TIPOS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_OLHAR_SISTEMA = (
    "Você confere, para outro assistente que NÃO enxerga imagens, páginas de um documento "
    "(ou imagens) geradas antes de entregar ao usuário. Responda em português, em tópicos "
    "curtos: 1) o que está visível no topo (logo, cabeçalho, título); 2) cada campo de "
    "formulário e o valor que aparece — diga VAZIO quando não houver valor; 3) problemas: "
    "texto cortado ou sobreposto, formatação desigual entre blocos parecidos, imagem "
    "faltando, página em branco; 4) a resposta à pergunta. Transcreva valores exatamente "
    "como aparecem. Não invente o que não dá para ler: diga 'ilegível'."
)


class FilesExecutor:
    """Executor server-side das tools file_*."""

    def __init__(self, store: FileStore, llm: Any = None) -> None:
        self._store = store
        # Só o file_olhar usa: sem roteador de LLM, ele recusa com motivo.
        self._llm = llm
        self._user_id: UUID | None = None
        self._session_id: UUID | None = None

    def set_context(self, user_id: UUID | None = None, session_id: UUID | None = None) -> None:
        self._user_id = user_id
        self._session_id = session_id

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await self._dispatch(tool_name, arguments)
        except FileStoreError as exc:
            # Erro esperado (quota, formato, path): mensagem acionável, sem stack.
            return {"error": exc.code, "message": exc.message}

    async def _dispatch(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "file_olhar":
            return await self._olhar(arguments)
        if tool_name == "file_list":
            return await self._list(arguments)
        if tool_name == "file_info":
            return await self._info(arguments)
        if tool_name == "file_read":
            return await self._store.read_slice(
                str(arguments.get("file_id") or ""),
                start_line=int(arguments.get("start_line") or 1),
                max_lines=int(arguments.get("max_lines") or 200),
            )
        if tool_name == "file_stage":
            return await self._store.stage(
                str(arguments.get("file_id") or ""),
                target=str(arguments.get("target") or "workbench"),
                name=arguments.get("name"),
            )
        if tool_name == "file_save":
            entry = await self._store.harvest(
                str(arguments.get("path") or ""),
                name=arguments.get("name"),
                # Sempre `perm`: era o modelo que escolhia, e ele trocava de
                # turno para turno (session-a20f4722: o 2º dos 3 MP3 entregues
                # saiu `temp` sem ninguém pedir e sumiria em 7 dias). Arquivo que
                # o usuário quer descartar passa por `file_set_retention`.
                retention="perm",
                user_id=self._user_id,
                session_id=self._session_id,
            )
            return {
                "file_id": str(entry.id),
                "name": entry.name,
                "size_bytes": entry.size_bytes,
                "retention": entry.retention,
                "message": (
                    f"'{entry.name}' guardado no catálogo (id={entry.id}). A interface "
                    "já mostra um botão de download pra este arquivo automaticamente — "
                    "só confirme que salvou, em uma frase curta. Você NÃO tem uma URL "
                    "para citar: nunca escreva um link de download você mesmo."
                ),
            }
        if tool_name == "file_offer":
            # "Me mostra o botão de download" de algo que JÁ está no catálogo: sem
            # isto o único jeito era file_save de novo (outra linha, outro id) — e na
            # sessão f33632b4 a frase virou um plano de run_command que falhou inteiro.
            entry = await self._store.get(str(arguments.get("file_id") or ""))
            if entry is None or entry.status != "available":
                return {
                    "error": "not_found",
                    "message": "esse file_id não está disponível no catálogo — confira no registro de arquivos da conversa.",
                }
            return {
                "file_id": str(entry.id),
                "name": entry.name,
                "size_bytes": entry.size_bytes,
                "offered": True,
                "message": (
                    f"O botão de download de '{entry.name}' vai junto da sua resposta. Diga em uma "
                    "frase que ele está aí; não escreva link nenhum."
                ),
            }
        if tool_name == "file_set_retention":
            entry = await self._store.set_retention(
                str(arguments.get("file_id") or ""),
                str(arguments.get("retention") or "perm"),
            )
            when = f" Expira em {entry.expires_at:%d/%m}." if entry.expires_at else " Não expira."
            return {
                "file_id": str(entry.id),
                "retention": entry.retention,
                "message": f"'{entry.name}' agora é {entry.retention}.{when}",
            }
        raise ValueError(f"Unknown files tool: {tool_name}")

    async def _list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entries = await self._store.list_files(
            retention=arguments.get("retention"),
            origin=arguments.get("origin"),
            q=arguments.get("q"),
            limit=int(arguments.get("limit") or 50),
        )
        if not entries:
            return {"files": [], "note": "Nenhum arquivo no catálogo com esses filtros."}
        return {
            "files": [
                {
                    "file_id": str(e.id),
                    "name": e.name,
                    "mime": e.mime,
                    "size_bytes": e.size_bytes,
                    "retention": e.retention,
                    "origin": e.origin,
                    "created_at": e.created_at.isoformat(),
                }
                for e in entries
            ],
            "count": len(entries),
        }

    async def _info(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entry = await self._store.get(str(arguments.get("file_id") or ""))
        if entry is None:
            return {"error": "not_found", "message": "arquivo não encontrado no catálogo."}
        meta = entry.meta_json or {}
        payload: dict[str, Any] = {
            "file_id": str(entry.id),
            "name": entry.name,
            "mime": entry.mime,
            "size_bytes": entry.size_bytes,
            "retention": entry.retention,
            "status": entry.status,
            "origin": entry.origin,
            "created_at": entry.created_at.isoformat(),
            "extraction": meta.get("extraction"),
        }
        for key in ("line_count", "pages", "preview_head"):
            if meta.get(key) is not None:
                payload[key] = meta[key]
        if entry.expires_at:
            payload["expires_at"] = entry.expires_at.isoformat()
        if entry.status != "available":
            payload["note"] = f"Arquivo está '{entry.status}' — não dá para ler nem materializar."
        elif meta.get("extraction") == "pending":
            payload["note"] = (
                "Binário sem extração de texto (chega na F2). Para trabalhar nele use "
                "file_stage e processe no sandbox com neo_bash."
            )
        return payload

    async def _olhar(self, arguments: dict[str, Any]) -> Any:
        """Manda as imagens do sandbox para o modelo de visão e devolve o que ele viu.

        O cérebro (DeepSeek) só lê texto. Sem isto o Neo conferia documento por
        texto extraído, que não mostra o logo apagado do cabeçalho nem "parece que
        só jogou e deixou lá" (session-20cb02b6). Lê pelo mesmo resolvedor do
        file_save: preso ao /workspace, sem seguir symlink para fora.
        """
        import base64

        from shared.config import get_settings
        from shared.types import Message, MessageRole

        if self._llm is None:
            return {"error": "sem_visao", "message": "visão indisponível neste core (sem roteador de LLM)."}
        caminhos = arguments.get("imagens") or []
        if isinstance(caminhos, str):
            caminhos = [caminhos]
        pergunta = str(arguments.get("pergunta") or "").strip()
        if not caminhos or not pergunta:
            return {"error": "bad_args", "message": "passe 'imagens' (caminhos no sandbox) e 'pergunta'."}
        imagens: list[str] = []
        for bruto in list(caminhos)[:_OLHAR_MAX_IMAGENS]:
            real = self._store.resolve_workspace_path(str(bruto))
            tipo = _OLHAR_TIPOS.get(real.suffix.lower())
            if tipo is None:
                raise FileStoreError("bad_format", f"{bruto!r} não é PNG/JPG/WEBP — renderize antes.")
            if not real.is_file():
                raise FileStoreError("not_found", f"não existe {bruto!r} no workspace.")
            if real.stat().st_size > _OLHAR_MAX_BYTES:
                raise FileStoreError("too_large", f"{bruto!r} passa de 4 MB — renderize com menos dpi.")
            imagens.append(f"data:{tipo};base64,{base64.b64encode(real.read_bytes()).decode()}")

        modelo = get_settings().browser_local_vision_model
        provider = self._llm.get_provider_for_model(modelo)
        resposta = await provider.complete(
            messages=[
                Message(role=MessageRole.SYSTEM, content=_OLHAR_SISTEMA),
                Message(role=MessageRole.USER, content=pergunta, images=imagens),
            ],
            model=modelo,
            temperature=0.1,
            max_tokens=900,
            # Sem isto o qwen/gemini via OpenRouter raciocina e a resposta vem vazia
            # (ver a triagem do navegador local).
            disable_thinking=True,
        )
        texto = (resposta.get_content() or "").strip()
        if not texto:
            return {"error": "visao_vazia", "message": "o modelo de visão respondeu vazio — tente de novo uma vez."}
        return {"ok": True, "modelo": modelo, "imagens": len(imagens), "o_que_se_ve": texto}
