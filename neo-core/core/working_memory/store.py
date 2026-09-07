"""WorkingMemoryStore — leitura/escrita do caderno de trabalho do Neo.

Ver docs/working-memory-plan.md §4/§5. Responsabilidades: resolver a nota por
``(user_id, slug)`` (upsert — nunca duplica), acrescentar entradas
timestampadas ao markdown, extrair os ``[[links]]``, manter o corpo dentro do
teto (jogando fora o mais ANTIGO), montar o índice que vai para o prompt e
recolher nota vencida.

Todo acesso a banco passa por três métodos finos (``_select_one``,
``_select_index``, ``_persist``) de propósito: a lógica que erra é a de cima
(carimbo, truncagem, upsert, links) e ela fica testável sem Postgres.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlmodel import select

from core.working_memory.render import render_tail, split_entries, stamp, to_brazil
from db.models.working_note import LIVE_STATUS, NOTE_STATUSES, WorkingNote

logger = logging.getLogger(__name__)

# [[alvo]] — nota, file:<id> ou path:/workspace/…
_LINK_RE = re.compile(r"\[\[([^\[\]\n]{1,120})\]\]")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9-]+")
_MD_NOISE_RE = re.compile(r"[`*_>#]+")

_TRUNCATION_MARKER = "_[entradas mais antigas removidas por limite de tamanho]_"

MAX_SLUG_LEN = 64
MAX_TITLE_LEN = 120
MAX_SUMMARY_LEN = 240
MAX_LINKS = 20


class WorkingMemoryError(Exception):
    """Erro de negócio do caderno. A mensagem é lida pelo MODELO — é acionável."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def slugify(raw: str) -> str:
    """Nome de nota estável: minúsculo, sem acento, só ``[a-z0-9-]``.

    O modelo escreve o slug à mão em toda chamada ("extrato-nubank-março" num
    turno, "Extrato Nubank Marco" no outro); sem normalizar, cada variação
    criaria uma nota nova e o caderno viraria confete.
    """
    decomposed = unicodedata.normalize("NFD", (raw or "").strip().lower())
    ascii_only = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    slug = _SLUG_STRIP_RE.sub("-", ascii_only).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:MAX_SLUG_LEN].strip("-")


def extract_links(text: str) -> list[str]:
    """Alvos dos ``[[…]]`` do texto, na ordem, sem repetir."""
    out: list[str] = []
    for match in _LINK_RE.findall(text or ""):
        target = match.strip()
        if target and target not in out:
            out.append(target)
    return out


def derive_summary(content: str) -> str:
    """Resumo de uma linha a partir do conteúdo, quando o modelo não mandou um.

    O resumo é o que ocupa o prompt todo turno, então ele nunca pode ficar
    vazio — sem ele a linha do índice viraria só um slug solto, e um slug não
    diz ao modelo se vale a pena abrir a nota.
    """
    for raw_line in (content or "").splitlines():
        line = _MD_NOISE_RE.sub("", raw_line).strip()
        line = re.sub(r"^\[\[|\]\]$", "", line).strip()
        if line and not line.startswith("-"):
            return line[:MAX_SUMMARY_LEN]
    # Conteúdo 100% em bullets: achata tudo numa linha só, sem o marcador na
    # frente (o índice já usa "- " para separar as notas — dois viram sujeira).
    flat = " ".join((content or "").split())
    flat = _MD_NOISE_RE.sub("", flat).lstrip("- ").strip()
    return flat[:MAX_SUMMARY_LEN]


class WorkingMemoryStore:
    """Caderno de trabalho por usuário. Sem LLM, sem rede — só banco e texto."""

    def __init__(
        self,
        *,
        db: Any,
        max_note_bytes: int = 32 * 1024,
        ttl_days: int = 30,
        max_index_notes: int = 8,
        read_tail_entries: int = 4,
    ) -> None:
        self._db = db
        self.max_note_bytes = max_note_bytes
        self.ttl_days = ttl_days
        self.max_index_notes = max_index_notes
        self.read_tail_entries = read_tail_entries

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    async def write(
        self,
        *,
        user_id: UUID | None,
        slug: str,
        content: str,
        session_id: UUID | None = None,
        title: str | None = None,
        summary: str | None = None,
        mode: str = "append",
        status: str = LIVE_STATUS,
        links: list[str] | None = None,
        origin: str = "model",
        now: datetime | None = None,
    ) -> WorkingNote:
        """Cria ou atualiza a nota ``slug`` do usuário. Devolve a nota salva."""
        if user_id is None:
            raise WorkingMemoryError(
                "no_user", "o caderno de trabalho precisa de um usuário identificado."
            )
        clean_slug = slugify(slug)
        if not clean_slug:
            raise WorkingMemoryError(
                "bad_slug",
                "nome de nota inválido — use algo curto e descritivo, ex.: 'extrato-marco'.",
            )
        body_text = (content or "").strip()
        if not body_text:
            raise WorkingMemoryError("empty", "conteúdo vazio — não há o que anotar.")
        if mode not in ("append", "replace"):
            raise WorkingMemoryError("bad_mode", "mode deve ser 'append' ou 'replace'.")
        if status not in NOTE_STATUSES:
            raise WorkingMemoryError(
                "bad_status", f"status deve ser um de: {', '.join(NOTE_STATUSES)}."
            )

        now = now or datetime.utcnow()
        note = await self._select_one(user_id, clean_slug)
        entry = f"## {stamp(now)}\n{body_text}"

        if note is None:
            note = WorkingNote(
                user_id=user_id,
                session_id=session_id,
                slug=clean_slug,
                title=(title or clean_slug.replace("-", " ")).strip()[:MAX_TITLE_LEN],
                origin=origin,
                created_at=now,
            )
            note.body = f"{self._header(note, now)}\n\n{entry}"
            note.entry_count = 1
        else:
            if title:
                note.title = title.strip()[:MAX_TITLE_LEN]
            if mode == "replace":
                note.body = f"{self._header(note, now)}\n\n{entry}"
                note.entry_count = 1
            else:
                note.body = f"{note.body.rstrip()}\n\n{entry}"
                note.entry_count = int(note.entry_count or 0) + 1
            # A sessão de origem acompanha o último toque: é o que faz a nota
            # subir no índice quando a tarefa é retomada numa sessão nova.
            if session_id is not None:
                note.session_id = session_id

        note.body = self._truncate(note.body)
        note.summary = (summary or derive_summary(body_text) or note.title)[:MAX_SUMMARY_LEN]
        note.status = status
        note.links = self._merge_links(note.links, links, body_text)
        note.updated_at = now
        note.expires_at = None if note.pinned else now + timedelta(days=self.ttl_days)
        return await self._persist(note)

    async def append_auto(
        self,
        *,
        user_id: UUID | None,
        session_id: UUID | None,
        entry: str,
        slug: str | None = None,
        title: str = "Trabalho desta sessão",
    ) -> WorkingNote | None:
        """Backstop determinístico do turno (§7) — sem LLM, best-effort.

        Nunca levanta: é chamado de uma task de background depois da resposta já
        ter ido para o usuário, e um caderno que falha não pode virar erro de
        turno. Devolve ``None`` quando não deu para gravar.
        """
        if user_id is None or not (entry or "").strip():
            return None
        auto_slug = slug or (
            f"sessao-{str(session_id)[:8]}" if session_id else "trabalho-recente"
        )
        try:
            return await self.write(
                user_id=user_id,
                session_id=session_id,
                slug=auto_slug,
                content=entry,
                title=title,
                summary="registro automático do que foi executado nesta sessão",
                origin="auto",
            )
        except Exception as exc:  # noqa: BLE001 — caderno nunca derruba o turno
            logger.warning("[WorkingMemory] append automático falhou: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    async def read(
        self, *, user_id: UUID | None, slug: str, full: bool = False
    ) -> dict[str, Any]:
        """Corpo da nota para o ``wm_read``. Nunca levanta por nota ausente."""
        if user_id is None:
            return {
                "error": "no_user",
                "message": "o caderno de trabalho precisa de um usuário identificado.",
            }
        clean_slug = slugify(slug)
        note = await self._select_one(user_id, clean_slug) if clean_slug else None
        if note is None:
            return {
                "error": "not_found",
                "message": (
                    f"não existe nota '{slug}' no seu caderno. Confira os nomes entre "
                    "[[ ]] na seção 'Memória de Trabalho' do seu contexto — e se o "
                    "trabalho realmente não foi anotado, refaça e grave com wm_write."
                ),
            }
        if full:
            body, omitted = note.body, 0
        else:
            body, omitted = render_tail(note.body, max_entries=self.read_tail_entries)
        payload: dict[str, Any] = {
            "note": note.slug,
            "title": note.title,
            "status": note.status,
            "updated_at": note.updated_at.isoformat(),
            "entries": note.entry_count,
            "body": body,
        }
        if omitted:
            payload["omitted_entries"] = omitted
        if note.links:
            payload["links"] = list(note.links)
        return payload

    async def index(
        self, *, user_id: UUID | None, session_id: UUID | None = None, limit: int | None = None
    ) -> list[WorkingNote]:
        """Notas ativas para o índice do prompt, já na ordem de exibição.

        Ordem: nota tocada NESTA sessão primeiro, depois ``pinned``, depois a
        mais recente. A tarefa em andamento é o que interessa; o resto é
        contexto de fundo.
        """
        if user_id is None:
            return []
        rows = await self._select_index(user_id, (limit or self.max_index_notes) * 3)
        rows.sort(
            key=lambda n: (
                0 if (session_id is not None and n.session_id == session_id) else 1,
                0 if n.pinned else 1,
                -(n.updated_at or datetime.min).timestamp(),
            )
        )
        return rows[: (limit or self.max_index_notes)]

    # ------------------------------------------------------------------
    # GC
    # ------------------------------------------------------------------

    async def gc_pass(self, *, now: datetime | None = None) -> dict[str, int]:
        """Arquiva nota vencida (``active`` + ``expires_at`` no passado).

        Arquiva, não apaga: a linha some do índice (deixa de custar tokens) e
        continua legível por ``wm_read`` se o modelo lembrar do nome.
        """
        now = now or datetime.utcnow()
        async with self._db.get_session() as session:
            result = await session.execute(
                select(WorkingNote).where(
                    WorkingNote.status == LIVE_STATUS,
                    WorkingNote.pinned.is_(False),  # type: ignore[attr-defined]
                    WorkingNote.expires_at.is_not(None),  # type: ignore[attr-defined]
                    WorkingNote.expires_at < now,  # type: ignore[operator]
                )
            )
            stale = list(result.scalars().all())
            for note in stale:
                note.status = "done"
                session.add(note)
            if stale:
                await session.commit()
        if stale:
            logger.info("[WorkingMemory] gc: %d notas arquivadas", len(stale))
        return {"archived": len(stale)}

    # ------------------------------------------------------------------
    # Texto
    # ------------------------------------------------------------------

    @staticmethod
    def _header(note: WorkingNote, now: datetime) -> str:
        created = to_brazil(note.created_at or now).strftime("%d/%m/%Y %H:%M")
        return f"# {note.title}\n> caderno de trabalho · criado {created}"

    def _truncate(self, body: str) -> str:
        """Mantém o corpo sob o teto jogando fora as entradas MAIS ANTIGAS.

        Cortar pelo fim seria pior: a conclusão mais recente é justamente a que
        o próximo turno vai procurar.
        """
        if len(body.encode("utf-8")) <= self.max_note_bytes:
            return body
        head, entries = split_entries(body)
        if not entries:
            return body.encode("utf-8")[: self.max_note_bytes].decode("utf-8", "ignore")
        prefix = f"{head.rstrip()}\n\n{_TRUNCATION_MARKER}\n\n"
        while len(entries) > 1:
            candidate = prefix + "\n\n".join(entries)
            if len(candidate.encode("utf-8")) <= self.max_note_bytes:
                return candidate
            entries.pop(0)
        # Sobrou uma entrada e ela sozinha estoura o teto: corta a entrada.
        room = self.max_note_bytes - len(prefix.encode("utf-8"))
        if room <= 0:
            return (prefix + entries[0]).encode("utf-8")[: self.max_note_bytes].decode(
                "utf-8", "ignore"
            )
        return prefix + entries[0].encode("utf-8")[:room].decode("utf-8", "ignore")

    @staticmethod
    def _merge_links(
        existing: list[str] | None, explicit: list[str] | None, content: str
    ) -> list[str]:
        out: list[str] = []
        for group in (existing or [], explicit or [], extract_links(content)):
            for raw in group:
                target = str(raw).strip()
                if target and target not in out:
                    out.append(target)
        return out[:MAX_LINKS]

    # ------------------------------------------------------------------
    # Banco (superfície fina — ver docstring do módulo)
    # ------------------------------------------------------------------

    async def _select_one(self, user_id: UUID, slug: str) -> WorkingNote | None:
        async with self._db.get_session() as session:
            result = await session.execute(
                select(WorkingNote)
                .where(WorkingNote.user_id == user_id, WorkingNote.slug == slug)
                .limit(1)
            )
            return result.scalars().first()

    async def _select_index(self, user_id: UUID, limit: int) -> list[WorkingNote]:
        async with self._db.get_session() as session:
            result = await session.execute(
                select(WorkingNote)
                .where(WorkingNote.user_id == user_id, WorkingNote.status == LIVE_STATUS)
                .order_by(WorkingNote.updated_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            return list(result.scalars().all())

    async def _persist(self, note: WorkingNote) -> WorkingNote:
        async with self._db.get_session() as session:
            session.add(note)
            await session.commit()
            await session.refresh(note)
        return note
