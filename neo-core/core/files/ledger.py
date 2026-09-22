"""Registro de vínculos de arquivo — onde está cada cópia de um documento.

docs/file-links-plan.md. Três lugares que não se enxergam: o disco do usuário
(host), o catálogo (File Store) e o sandbox (/workspace). O Neo move arquivo entre
eles o tempo todo — puxa um PDF, gera uma conclusão, entrega em duas pastas, edita
direto no Mac — e até aqui não guardava em lugar nenhum O QUE foi parar ONDE.

Sessão f33632b4, interações 5–10: "adiciona ao arquivo final e coloca em
~/Desktop/teste-de-analise" virou o PDF do Charlie gravado com nome de ``.md``
("Pronto, salvei"); cobrado, o Neo não sabia qual cópia era qual, tentou ``rm``
num caminho do sandbox na máquina do usuário, inventou o conteúdo de um markdown
e terminou num plano de 3 passos que falhou inteiro.

Tudo aqui é determinístico. As funções puras (:func:`resolve_doc_key`,
:func:`build_views`, :func:`render_block`) recebem a lista de eventos e são o que
os testes exercitam; :class:`FileLedger` só persiste e consulta.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: Eventos que mudam o CONTEÚDO do documento (a versão atual passa a ser a deles).
_CONTENT_ACTIONS = {"pulled", "saved", "edited_sandbox", "written_host", "edited_host"}
_HOST_ACTIONS = {"pulled", "delivered", "written_host", "edited_host", "read_host", "deleted_host"}
_MAX_DOCS_IN_BLOCK = 8
#: Quanto tempo um documento do usuário continua visível em conversas novas.
USER_HORIZON_HOURS = 72


def _ext(name: str | None) -> str:
    base = str(name or "").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _short(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    try:
        from core.working_memory.render import stamp

        return stamp(dt)
    except Exception:  # noqa: BLE001
        return dt.strftime("%d/%m %H:%M")


def _size(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return f"{n / 1024:.0f} KB" if n >= 1024 else f"{n} bytes"


def _get(e: Any, attr: str) -> Any:
    return e.get(attr) if isinstance(e, dict) else getattr(e, attr, None)


def _sid(value: Any) -> str | None:
    return str(value) if value else None


def resolve_doc_key(
    events: Iterable[Any],
    *,
    action: str,
    host_path: str | None = None,
    sandbox_path: str | None = None,
    file_id: Any = None,
) -> str:
    """A que documento pertence este movimento (o ``doc_key`` do evento).

    A ordem de busca depende de QUEM define a identidade do movimento:

    * publicar/editar no sandbox → o caminho do sandbox (a mesma conclusão
      publicada de novo é outra versão do mesmo documento);
    * materializar/entregar → o ``file_id`` (o que foi para o disco é o CONTEÚDO
      daquele id — se o id era do PDF, a cópia é do PDF, com o nome que tiver);
    * mexer no disco dele → o caminho do host.
    """
    lista = list(events)
    fid = _sid(file_id)

    def ultimo(pred) -> str | None:
        for e in reversed(lista):
            if pred(e):
                return str(_get(e, "doc_key"))
        return None

    por_sandbox = (lambda: ultimo(lambda e: sandbox_path and _get(e, "sandbox_path") == sandbox_path))
    por_id = (lambda: ultimo(lambda e: fid and _sid(_get(e, "file_id")) == fid))
    por_host = (lambda: ultimo(lambda e: host_path and _get(e, "host_path") == host_path))

    if action in ("saved", "edited_sandbox"):
        ordem = (por_sandbox, por_id)
    elif action in ("staged", "delivered"):
        ordem = (por_id, por_host, por_sandbox)
    elif action == "written_host":
        ordem = (por_host, por_sandbox, por_id)
    else:
        ordem = (por_host, por_id, por_sandbox)
    for busca in ordem:
        chave = busca()
        if chave:
            return chave
    return f"doc-{uuid.uuid4().hex[:12]}"


@dataclass
class HostCopy:
    path: str
    action: str
    at: datetime | None
    file_id: str | None = None
    sha256: str | None = None
    status: str = "ok"
    state: str = "desconhecido"


@dataclass
class DocView:
    key: str
    name: str
    origin: str  # "host" (veio do disco dele) | "sandbox" (gerado pelo Neo)
    origin_path: str | None
    current_where: str  # "host" | "sandbox" | "catalogo"
    current_file_id: str | None
    current_sha: str | None
    current_at: datetime | None
    current_host_path: str | None = None
    current_size: int | None = None
    unpublished: bool = False
    sandbox_path: str | None = None
    sandbox_state: str | None = None
    host_copies: list[HostCopy] = field(default_factory=list)
    last_at: datetime | None = None


def build_views(events: Iterable[Any]) -> list[DocView]:
    """Agrupa os eventos por documento e calcula versão atual e estado de cada cópia."""
    lista = sorted(list(events), key=lambda e: _get(e, "created_at") or datetime.min)
    # Um caminho do disco pertence ao documento do evento MAIS RECENTE nele: depois
    # de consertar a cópia errada, ela sai do PDF e vai para a conclusão.
    dono_do_caminho: dict[str, str] = {}
    for e in lista:
        if _get(e, "host_path"):
            dono_do_caminho[_get(e, "host_path")] = str(_get(e, "doc_key"))

    grupos: dict[str, list[Any]] = {}
    for e in lista:
        grupos.setdefault(str(_get(e, "doc_key")), []).append(e)

    views: list[DocView] = []
    for chave, evs in grupos.items():
        primeiro = evs[0]
        origem = "host" if _get(primeiro, "action") in ("pulled", "read_host", "edited_host") else "sandbox"
        sha_por_id: dict[str, str] = {}
        for e in evs:
            if _get(e, "file_id") and _get(e, "sha256"):
                sha_por_id[_sid(_get(e, "file_id"))] = _get(e, "sha256")

        view = DocView(
            key=chave,
            name=str(_get(evs[-1], "name") or _get(primeiro, "name") or ""),
            origin=origem,
            origin_path=_get(primeiro, "host_path") if origem == "host" else _get(primeiro, "sandbox_path"),
            current_where="catalogo",
            current_file_id=None,
            current_sha=None,
            current_at=None,
            last_at=_get(evs[-1], "created_at"),
        )
        copias: dict[str, HostCopy] = {}
        sandbox_sha: str | None = None

        for e in evs:
            acao = _get(e, "action")
            fid = _sid(_get(e, "file_id"))
            sha = _get(e, "sha256") or (sha_por_id.get(fid) if fid else None)
            host = _get(e, "host_path")
            quando = _get(e, "created_at")
            if _get(e, "name") and acao not in ("delivered", "read_host"):
                view.name = _get(e, "name")

            if acao == "read_host" and host:
                anterior = copias.get(host)
                mudou_fora = bool(anterior and anterior.sha256 and sha and anterior.sha256 != sha)
                copias[host] = HostCopy(host, acao, quando, anterior.file_id if anterior else None, sha)
                if mudou_fora or (view.current_sha is None and sha and origem == "host"):
                    # Alguém mexeu no arquivo dele depois da nossa última cópia: a
                    # versão do disco passa a ser a atual.
                    _set_current(view, "host", None, sha, quando, host=host)
                continue

            if acao in _CONTENT_ACTIONS:
                if acao in ("pulled", "written_host", "edited_host"):
                    _set_current(view, "host" if acao != "pulled" else "catalogo", fid, sha, quando, host=host,
                                 size=_get(e, "size_bytes"))
                    if host:
                        copias[host] = HostCopy(host, acao, quando, fid, sha)
                    if acao == "pulled" and fid:
                        view.current_where = "catalogo"
                elif acao == "saved":
                    _set_current(view, "sandbox", fid, sha, quando, size=_get(e, "size_bytes"))
                    view.sandbox_path = _get(e, "sandbox_path") or view.sandbox_path
                    sandbox_sha = sha
                elif acao == "edited_sandbox":
                    view.sandbox_path = _get(e, "sandbox_path") or view.sandbox_path
                    _set_current(view, "sandbox", None, None, quando)
                    view.unpublished = True
                    sandbox_sha = None
                if acao in ("written_host", "edited_host") and host:
                    copias[host].state = "atual"
                continue

            if acao == "staged":
                view.sandbox_path = _get(e, "sandbox_path") or view.sandbox_path
                sandbox_sha = sha
            elif acao == "delivered" and host:
                copias[host] = HostCopy(host, acao, quando, fid, sha, status=str(_get(e, "status") or "ok"))
            elif acao == "deleted_host" and host:
                copias[host] = HostCopy(host, acao, quando, status="deleted")

        # Estado de cada cópia contra a versão atual.
        for caminho, copia in copias.items():
            if dono_do_caminho.get(caminho) != chave:
                continue
            if copia.status == "wrong_content":
                copia.state = "errada"
            elif copia.status == "deleted":
                copia.state = "apagada"
            elif view.current_where == "host" and view.current_host_path == caminho:
                copia.state = "atual"
            elif copia.sha256 and view.current_sha and copia.sha256 == view.current_sha:
                copia.state = "igual"
            elif copia.sha256 and view.current_sha:
                copia.state = "desatualizada"
            elif view.unpublished:
                copia.state = "desatualizada"
            view.host_copies.append(copia)

        if view.sandbox_path:
            if view.unpublished:
                view.sandbox_state = "nao_publicada"
            elif view.current_where == "host" and (not sandbox_sha or sandbox_sha != view.current_sha):
                view.sandbox_state = "desatualizada"
            else:
                view.sandbox_state = "atual"
        views.append(view)

    views.sort(key=lambda v: v.last_at or datetime.min, reverse=True)
    return views


def _set_current(view: DocView, where: str, fid: str | None, sha: str | None, at: Any,
                 *, host: str | None = None, size: Any = None) -> None:
    view.current_where = where
    view.current_file_id = fid
    view.current_sha = sha
    view.current_at = at
    view.current_host_path = host if where == "host" else None
    view.unpublished = False
    try:
        view.current_size = int(size) if size is not None else None
    except (TypeError, ValueError):
        view.current_size = None


_RULES = (
    "Como usar este registro (vale para decidir e para agir):\n"
    "- \"o arquivo\", \"ele\", \"o arquivo final\", \"aquele .md\" = um documento desta lista; na "
    "dúvida, o gerado mais recente. Use os caminhos e file_ids DAQUI — nunca invente nem use "
    "o id de outro documento.\n"
    "- Pôr no disco dele (\"baixa pra mim\", \"coloca em <pasta>\"): `save_generated_file` com o "
    "file_id da VERSÃO ATUAL e file_name = o nome do documento. Se o destino já tem uma cópia "
    "listada aqui, `overwrite=true` é seguro (a cópia é sua).\n"
    "- Versão atual sem file_id (alterada só no sandbox): `file_save` no caminho do sandbox "
    "antes de entregar. Para mostrar o botão de download de uma versão que já tem file_id: "
    "`file_offer`.\n"
    "- Editar \"no meu arquivo\", \"direto na minha máquina\", ou quando a versão atual está no "
    "disco dele: `read_user_file` + `edit_user_file` no caminho do disco listado — sem sandbox.\n"
    "- Cópia do sandbox desatualizada (a mais nova está no disco dele): puxe de volta com "
    "`request_user_file(mode=\"path\", stage=true)` antes de mexer nela.\n"
    "- Apagar uma cópia do disco dele: `run_command` com `rm` no caminho EXATO daqui. Nada "
    "de caminho /workspace na máquina dele."
)

_ESTADO_COPIA = {
    "igual": "igual à versão atual",
    "atual": "É A VERSÃO ATUAL (mais nova que o sandbox)",
    "desatualizada": "desatualizada (versão anterior)",
    "apagada": "apagada",
    "desconhecido": "estado desconhecido",
}


def render_block(views: list[DocView]) -> str:
    """Bloco para o ``system_context`` — o classificador e o cérebro leem o mesmo."""
    if not views:
        return ""
    linhas = [
        "## ARQUIVOS DESTA CONVERSA — onde está cada cópia (registro automático do que "
        "realmente aconteceu; vale mais que a sua memória da conversa)"
    ]
    for i, v in enumerate(views[:_MAX_DOCS_IN_BLOCK], start=1):
        if v.origin == "host":
            origem = f"veio do disco dele ({v.origin_path})" if v.origin_path else "veio do disco dele"
        else:
            origem = "gerado por você no sandbox"
        linhas.append(f"{i}. «{v.name}» — {origem}")
        if v.current_where == "host":
            atual = f"versão atual: no disco dele em {v.current_host_path} ({_short(v.current_at)})"
            if v.current_sha:
                atual += f", sha256={v.current_sha[:12]}…"
        elif v.unpublished:
            atual = (
                f"versão atual: só no sandbox em {v.sandbox_path}, NÃO publicada "
                f"({_short(v.current_at)}) — file_save antes de entregar"
            )
        else:
            onde = f"sandbox {v.sandbox_path} → " if v.current_where == "sandbox" and v.sandbox_path else ""
            tamanho = f", {_size(v.current_size)}" if v.current_size else ""
            atual = f"versão atual: {onde}catálogo file_id={v.current_file_id}{tamanho} ({_short(v.current_at)})"
        linhas.append(f"   {atual}")
        if v.sandbox_path and not (v.current_where == "sandbox" and not v.unpublished):
            estado = {
                "atual": "atual",
                "desatualizada": "⚠ DESATUALIZADA — puxe de volta antes de editar lá",
                "nao_publicada": "com alterações não publicadas",
            }.get(v.sandbox_state or "", v.sandbox_state or "")
            if not v.unpublished:
                linhas.append(f"   no sandbox: {v.sandbox_path} ({estado})")
        if v.host_copies:
            linhas.append("   no disco dele:")
            for c in v.host_copies:
                if c.state == "errada":
                    linhas.append(
                        f"   - ⚠ {c.path} — CONTEÚDO ERRADO: é «{v.name}» gravado com esse nome "
                        f"({_short(c.at)}). Conserte entregando por cima (overwrite=true) o documento "
                        "que deveria estar ali."
                    )
                    continue
                como = {
                    "pulled": "de onde veio", "delivered": "entregue por você", "written_host": "gravado por você",
                    "edited_host": "editado direto no disco", "read_host": "lido", "deleted_host": "apagado",
                }.get(c.action, c.action)
                linhas.append(f"   - {c.path} — {_ESTADO_COPIA.get(c.state, c.state)} ({como} {_short(c.at)})")
    if len(views) > _MAX_DOCS_IN_BLOCK:
        linhas.append(f"(+{len(views) - _MAX_DOCS_IN_BLOCK} documento(s) mais antigos omitidos)")
    linhas.append(_RULES)
    return "\n".join(linhas)


class FileLedger:
    """Persistência do registro. Nunca levanta: registro fora do ar não derruba turno."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def events(
        self, session_id: Any, *, user_id: Any = None, horizon_hours: int = USER_HORIZON_HOURS, limit: int = 400
    ) -> list[Any]:
        """Movimentos desta conversa + os do mesmo usuário nas últimas ``horizon_hours``.

        Por usuário e não só por sessão: a sessão ROTACIONA (mudança de tópico,
        expiração) e o arquivo continua sendo o mesmo — na f33632b4 "tenta salvar o
        arquivo lá agora" caiu noutra sessão e perdeu tudo o que sabia dele.
        """
        if not session_id and not user_id:
            return []
        try:
            from datetime import timedelta

            from sqlalchemy import and_, or_
            from sqlmodel import select

            from db.models.file_link import FileLink

            condicoes = []
            if session_id:
                condicoes.append(FileLink.session_id == uuid.UUID(str(session_id)))
            if user_id:
                condicoes.append(and_(
                    FileLink.user_id == uuid.UUID(str(user_id)),
                    FileLink.created_at >= datetime.utcnow() - timedelta(hours=horizon_hours),
                ))
            stmt = (
                select(FileLink)
                .where(or_(*condicoes))
                .order_by(FileLink.created_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            async with self._db.get_session() as s:
                return list(reversed((await s.execute(stmt)).scalars().all()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FileLedger] leitura falhou: %s", exc)
            return []

    async def record(
        self,
        *,
        user_id: Any,
        session_id: Any,
        action: str,
        name: str = "",
        host_path: str | None = None,
        sandbox_path: str | None = None,
        file_id: Any = None,
        sha256: str | None = None,
        size_bytes: Any = None,
        status: str = "ok",
        note: str = "",
    ) -> Any:
        if not session_id:
            return None
        try:
            from db.models.file_link import FileLink

            eventos = await self.events(session_id, user_id=user_id)
            chave = resolve_doc_key(
                eventos, action=action, host_path=host_path, sandbox_path=sandbox_path, file_id=file_id
            )
            if action == "delivered" and host_path and name and _ext(name) != _ext(host_path):
                status = "wrong_content"
            link = FileLink(
                user_id=uuid.UUID(str(user_id)) if user_id else None,
                session_id=uuid.UUID(str(session_id)),
                doc_key=chave,
                action=action,
                name=str(name or (host_path or sandbox_path or "").rsplit("/", 1)[-1])[:255],
                host_path=host_path,
                sandbox_path=sandbox_path,
                file_id=uuid.UUID(str(file_id)) if file_id else None,
                sha256=sha256,
                size_bytes=int(size_bytes) if isinstance(size_bytes, (int, float)) else None,
                status=status,
                note=note[:2000],
            )
            async with self._db.get_session() as s:
                s.add(link)
                await s.commit()
            return link
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FileLedger] registro de %s falhou: %s", action, exc)
            return None

    async def views(self, session_id: Any, user_id: Any = None) -> list[DocView]:
        return build_views(await self.events(session_id, user_id=user_id))

    async def block(self, session_id: Any, user_id: Any = None) -> str:
        return render_block(await self.views(session_id, user_id))

    async def knows_host_path(self, session_id: Any, path: str, user_id: Any = None) -> bool:
        """O Neo já pôs, puxou, leu ou editou esta cópia (nesta conversa ou nas recentes)?"""
        if not path:
            return False
        return any(_get(e, "host_path") == path for e in await self.events(session_id, user_id=user_id))
