"""ReleaseService — o ciclo de vida de uma Solicitação de Liberação (F3).

Contrato com o resto do sistema:

- ``create`` é chamado por quem PRECISA do dado (hoje a tool ``solicitar_dado``;
  amanhã o portão, quando entrar em ``enforce``). Ele grava a linha, monta o card
  e empurra a notificação para os aparelhos. Nunca resolve valor.
- ``decide`` é chamado SÓ pelas rotas de aprovação, com a identidade já resolvida
  pelo token do aparelho ou pelo login do painel (§11). Ninguém decide por
  ``user_id`` que o cliente afirmou, e **nenhuma tool chama isto** — se o modelo
  pudesse aprovar, a aprovação não valeria nada (é o defeito que o
  ``service_confirm`` tem hoje).
- ``consume`` é chamado por quem vai USAR a liberação. Uso único: a segunda
  chamada não vale, e o hash do conteúdo amarra a aprovação ao texto que você viu.

Fora deste módulo não existe caminho para mudar ``status``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlmodel import select

from db.models.release_request import (
    DESTINO_TIPOS,
    ORIGENS,
    STATUS_APROVADA,
    STATUS_EXPIRADA,
    STATUS_PENDENTE,
    STATUS_RECUSADA,
    STATUS_USADA,
    ReleaseRequest,
)

logger = logging.getLogger(__name__)

#: 15 minutos, não os 5 do ConfirmStore do gateway: liberar dado exige LER o
#: card, e você pode estar longe do aparelho. Curto demais vira "expirou de novo".
DEFAULT_TTL_MIN = 15

#: Teto de pedidos pendentes por usuário. Passar disso é sinal de laço, não de
#: demanda — e uma fila de cards é como a fadiga de aprovação começa.
MAX_PENDENTES = 20

#: Quanto do texto do terceiro vai para a evidência do card. O suficiente para
#: você reconhecer o pedido, não tanto que a tela vire o e-mail inteiro.
EVIDENCIA_MAX = 1200


class ReleaseError(Exception):
    """Erro de negócio da liberação (pedido inexistente, estado errado)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def payload_fingerprint(payload: Any) -> str:
    """Hash do conteúdo aprovado. Muda o texto, morre a aprovação."""
    if payload is None:
        return ""
    texto = payload if isinstance(payload, str) else repr(payload)
    normalizado = " ".join(texto.split())
    return hashlib.sha256(normalizado.encode("utf-8")).hexdigest()


def motivos_de_origem(
    principal: str,
    principal_ref: str | None = None,
    taint_sources: list[str] | None = None,
    tainted: bool | None = None,
) -> list[str]:
    """Os ``risco_motivos`` que dizem de onde partiu o pedido.

    Duas portas criam pedido — a tool ``solicitar_dado``, dentro do turno, e o
    job do navegador, que para no campo e pede sozinho, FORA do turno. As duas
    escrevem as mesmas frases, porque é delas que ``nasceu_de_terceiro`` e o card
    tiram o aviso "⚠️ nasceu de conteúdo de terceiro".
    """
    motivos: list[str] = []
    if principal == "externo":
        motivos.append(f"o pedido veio de {principal_ref or 'origem externa'}, não de você")
    elif principal == "agenda":
        motivos.append("disparo da agenda, sem ninguém na frente")
    fontes = [str(f) for f in (taint_sources or []) if str(f).strip()]
    if fontes if tainted is None else tainted:
        motivos.append(f"o turno leu conteúdo de terceiro ({', '.join(fontes[:3])})")
    return motivos


def nasceu_de_terceiro(row: ReleaseRequest) -> bool:
    """O pedido veio de conteúdo que entrou no turno, e não da fala dele?

    ``origem`` responde "quem mandou a mensagem"; isto responde "quem escreveu o
    pedido". São diferentes quando ele cola um texto, abre uma página ou manda o
    Neo ler um e-mail: a mensagem é dele, o pedido é de quem escreveu o conteúdo.
    Uma definição só — o card do aparelho (``notify.py``) e a tela do painel
    mostram o MESMO aviso.
    """
    return any("terceiro" in str(m).lower() for m in (row.risco_motivos or []))


@dataclass
class ReleaseView:
    """Vista de um pedido — o que vai para o card, o painel e o log."""

    id: str
    status: str
    origem: str
    origem_ref: str | None
    campos: list[str]
    destino: str
    destino_tipo: str
    finalidade: str
    tool: str | None
    risco: str
    risco_motivos: list[str]
    evidencia: str
    criado_em: str | None
    expira_em: str | None
    #: O pedido nasceu de conteúdo de terceiro (colagem, página, e-mail, PDF)?
    nasceu_de_terceiro: bool = False
    decidido_em: str | None = None
    usado_em: str | None = None

    @classmethod
    def of(cls, row: ReleaseRequest) -> "ReleaseView":
        return cls(
            id=str(row.id),
            status=row.status,
            origem=row.origem,
            origem_ref=row.origem_ref,
            campos=list(row.campos or []),
            destino=row.destino,
            destino_tipo=row.destino_tipo,
            finalidade=row.finalidade,
            tool=row.tool,
            risco=row.risco,
            risco_motivos=list(row.risco_motivos or []),
            evidencia=row.origem_evidencia or "",
            criado_em=row.created_at.isoformat() if row.created_at else None,
            expira_em=row.expires_at.isoformat() if row.expires_at else None,
            nasceu_de_terceiro=nasceu_de_terceiro(row),
            decidido_em=row.decided_at.isoformat() if row.decided_at else None,
            usado_em=row.used_at.isoformat() if row.used_at else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "origem": self.origem,
            "origem_ref": self.origem_ref,
            "campos": self.campos,
            "destino": self.destino,
            "destino_tipo": self.destino_tipo,
            "finalidade": self.finalidade,
            "tool": self.tool,
            "risco": self.risco,
            "risco_motivos": self.risco_motivos,
            "evidencia": self.evidencia,
            "criado_em": self.criado_em,
            "expira_em": self.expira_em,
            "nasceu_de_terceiro": self.nasceu_de_terceiro,
            "decidido_em": self.decidido_em,
            "usado_em": self.usado_em,
        }


class ReleaseService:
    def __init__(self, db: Any, notifier: Any = None) -> None:
        self._db = db
        #: Callable ``async (user_id, row) -> uuid|None`` que cria/empurra o card.
        #: Injetado para este módulo não conhecer notificação nem WebSocket.
        self._notifier = notifier

    # ── criação ──────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        user_id: UUID,
        campos: list[str],
        destino: str,
        destino_tipo: str,
        finalidade: str = "",
        session_id: UUID | None = None,
        origem: str = "admin",
        origem_ref: str | None = None,
        evidencia: str = "",
        tool: str | None = None,
        payload: Any = None,
        risco: str = "desconhecido",
        risco_motivos: list[str] | None = None,
        ttl_min: int = DEFAULT_TTL_MIN,
        preview: str = "",
    ) -> ReleaseView:
        campos_limpos = [str(c).strip() for c in (campos or []) if str(c).strip()]
        if not campos_limpos:
            raise ReleaseError("sem_campos", "uma liberação precisa dizer QUAL dado sai.")
        destino = (destino or "").strip()
        if not destino:
            raise ReleaseError("sem_destino", "uma liberação precisa dizer PARA ONDE o dado vai.")
        if destino_tipo not in DESTINO_TIPOS:
            raise ReleaseError(
                "destino_invalido",
                f"tipo de destino desconhecido: {destino_tipo!r} (use {', '.join(DESTINO_TIPOS)}).",
            )
        if origem not in ORIGENS:
            raise ReleaseError("origem_invalida", f"origem desconhecida: {origem!r}")

        # Pedido idêntico já pendente → devolve o MESMO, em vez de empilhar card.
        # Um laço do modelo não pode virar fila de aprovação.
        existente = await self._find_pendente_igual(
            user_id=user_id, campos=campos_limpos, destino=destino, finalidade=finalidade
        )
        if existente is not None:
            logger.info("[release] pedido idêntico já pendente: %s", existente.id)
            return ReleaseView.of(existente)

        pendentes = await self.list_pending(user_id)
        if len(pendentes) >= MAX_PENDENTES:
            raise ReleaseError(
                "fila_cheia",
                f"já existem {len(pendentes)} liberações pendentes — resolva as abertas antes.",
            )

        row = ReleaseRequest(
            user_id=user_id,
            session_id=session_id,
            origem=origem,
            origem_ref=(origem_ref or None),
            origem_evidencia=(evidencia or "")[:EVIDENCIA_MAX],
            campos=campos_limpos,
            destino=destino,
            destino_tipo=destino_tipo,
            finalidade=(finalidade or "")[:280],
            tool=tool,
            payload_hash=payload_fingerprint(payload) or None,
            risco=risco,
            risco_motivos=list(risco_motivos or []),
            status=STATUS_PENDENTE,
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=max(1, int(ttl_min))),
        )
        async with self._db.get_session() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)

        logger.warning(
            "[release] pedido %s criado: campos=%s destino=%s origem=%s",
            row.id, ",".join(campos_limpos), destino, origem,
        )
        if self._notifier is not None:
            try:
                # `preview` é o CONTEÚDO que você está aprovando (o texto do
                # e-mail, a mensagem). Vai para o card e NÃO para a
                # release_request: o pedido guarda o hash, não uma segunda
                # cópia do dado. Mesmo lugar onde a missão do WhatsApp guarda o
                # `draft_text`.
                notif_id = await self._notifier(user_id, row, preview=preview)
                if notif_id:
                    await self._set_notification(row.id, notif_id)
            except Exception:  # noqa: BLE001 — sem card o pedido ainda existe
                logger.exception("[release] falha ao notificar pedido %s", row.id)
        return ReleaseView.of(row)

    # ── leitura ──────────────────────────────────────────────────────────

    async def get(self, request_id: UUID | str) -> ReleaseRequest | None:
        try:
            rid = request_id if isinstance(request_id, UUID) else UUID(str(request_id))
        except (TypeError, ValueError):
            return None
        async with self._db.get_session() as session:
            row = await session.get(ReleaseRequest, rid)
            if row is None:
                return None
            if self._is_expired(row):
                row.status = STATUS_EXPIRADA
                session.add(row)
                await session.commit()
                await session.refresh(row)
            return row

    async def list_pending(self, user_id: UUID) -> list[ReleaseRequest]:
        """Pendentes e ainda no prazo. Expirado é marcado na passagem."""
        async with self._db.get_session() as session:
            stmt = (
                select(ReleaseRequest)
                .where(ReleaseRequest.user_id == user_id)
                .where(ReleaseRequest.status == STATUS_PENDENTE)
                .order_by(ReleaseRequest.created_at.desc())
            )
            rows = list((await session.execute(stmt)).scalars().all())
            vivos: list[ReleaseRequest] = []
            mudou = False
            for row in rows:
                if self._is_expired(row):
                    row.status = STATUS_EXPIRADA
                    session.add(row)
                    mudou = True
                else:
                    vivos.append(row)
            if mudou:
                await session.commit()
            return vivos

    async def list_recent(self, user_id: UUID, limit: int = 60) -> list[ReleaseRequest]:
        """TODOS os pedidos do dono, recentes primeiro — a tela de histórico.

        Varre o prazo como o ``list_pending``: um pendente vencido aparecendo como
        "pendente" numa lista de histórico seria mentira, e é a linha que faz
        alguém esperar por um card que não existe mais.
        """
        async with self._db.get_session() as session:
            stmt = (
                select(ReleaseRequest)
                .where(ReleaseRequest.user_id == user_id)
                .order_by(ReleaseRequest.created_at.desc())
                .limit(max(1, min(int(limit or 60), 300)))
            )
            rows = list((await session.execute(stmt)).scalars().all())
            mudou = False
            for row in rows:
                if row.status == STATUS_PENDENTE and self._is_expired(row):
                    row.status = STATUS_EXPIRADA
                    session.add(row)
                    mudou = True
            if mudou:
                await session.commit()
            return rows

    async def cards_of(self, rows: list[ReleaseRequest]) -> dict[str, dict[str, str]]:
        """Texto EXATO do card de cada pedido, por ``release_id``.

        Vem da NOTIFICAÇÃO, não do pedido: o ``preview`` — o texto que vai sair —
        mora lá de propósito, porque a ``release_request`` guarda só o
        ``payload_hash`` (§6.2 do plano: o que se aprova é o texto, e o hash é o
        que amarra a aprovação a ele). Para a tela mostrar o MESMO card que chegou
        no aparelho, é de lá que se lê — reconstruir aqui daria dois textos que
        divergem no primeiro ajuste de redação.

        Uma query para a lista toda. Pedido sem notificação (push falhou) sai de
        fora em vez de virar erro.
        """
        ids = {row.notification_id: str(row.id) for row in rows if row.notification_id}
        if not ids:
            return {}
        from db.models.user_notification import UserNotification

        try:
            async with self._db.get_session() as session:
                stmt = select(UserNotification).where(UserNotification.id.in_(list(ids)))
                achadas = list((await session.execute(stmt)).scalars().all())
        except Exception:  # noqa: BLE001 — tela sem o texto ainda é útil
            logger.exception("[release] falha ao ler o card das notificações")
            return {}
        cards: dict[str, dict[str, str]] = {}
        for notif in achadas:
            release_id = ids.get(notif.id)
            if not release_id:
                continue
            payload = notif.payload if isinstance(notif.payload, dict) else {}
            cards[release_id] = {
                "titulo": notif.title or "",
                "corpo": notif.body or "",
                "preview": str(payload.get("preview") or ""),
            }
        return cards

    async def list_approved(self, user_id: UUID) -> list[ReleaseRequest]:
        """Aprovadas e ainda não usadas — a autorização que o preenchimento lê.

        Vencimento NÃO se aplica aqui de propósito: o prazo é para você DECIDIR.
        Depois de aprovada, a liberação vale até ser usada (uma vez).
        """
        async with self._db.get_session() as session:
            stmt = (
                select(ReleaseRequest)
                .where(ReleaseRequest.user_id == user_id)
                .where(ReleaseRequest.status == STATUS_APROVADA)
                .order_by(ReleaseRequest.decided_at.desc())
            )
            return list((await session.execute(stmt)).scalars().all())

    # ── decisão (só as rotas autenticadas) ───────────────────────────────

    async def decide(
        self,
        request_id: UUID | str,
        *,
        approved: bool,
        decided_by: UUID,
        device_id: str | None = None,
        source: str = "desktop",
    ) -> ReleaseView:
        row = await self.get(request_id)
        if row is None:
            raise ReleaseError("nao_encontrado", "essa liberação não existe.")
        if row.user_id != decided_by:
            # Só o DONO do dado libera o próprio dado (§11).
            logger.warning(
                "[release] recusado: %s tentou decidir pedido de %s", decided_by, row.user_id
            )
            raise ReleaseError("nao_e_seu", "essa liberação é de outro usuário.")
        if row.status != STATUS_PENDENTE:
            raise ReleaseError(
                "estado_errado",
                f"essa liberação já está '{row.status}' — não dá para decidir de novo.",
            )

        async with self._db.get_session() as session:
            atual = await session.get(ReleaseRequest, row.id)
            if atual is None:
                raise ReleaseError("nao_encontrado", "essa liberação não existe.")
            if atual.status != STATUS_PENDENTE:
                raise ReleaseError(
                    "estado_errado", f"essa liberação já está '{atual.status}'."
                )
            atual.status = STATUS_APROVADA if approved else STATUS_RECUSADA
            atual.decided_at = datetime.utcnow()
            atual.decided_by = decided_by
            atual.decided_device = (device_id or None)
            atual.decision_source = source
            session.add(atual)
            await session.commit()
            await session.refresh(atual)
        logger.warning(
            "[release] pedido %s %s por %s (%s)",
            atual.id, atual.status, decided_by, source,
        )
        return ReleaseView.of(atual)

    # ── consumo (uso único) ──────────────────────────────────────────────

    async def consume(
        self, request_id: UUID | str, *, payload: Any = None
    ) -> ReleaseRequest:
        """Marca a liberação como usada e devolve a linha. Uso único.

        ``payload`` amarra o conteúdo: se o texto mudou depois da aprovação, a
        liberação não vale para ele — o que você aprovou é o que sai.
        """
        row = await self.get(request_id)
        if row is None:
            raise ReleaseError("nao_encontrado", "essa liberação não existe.")
        if row.status != STATUS_APROVADA:
            raise ReleaseError(
                "nao_aprovada",
                f"essa liberação está '{row.status}' — só uma aprovada pode ser usada.",
            )
        if row.payload_hash and payload is not None:
            if payload_fingerprint(payload) != row.payload_hash:
                logger.warning("[release] conteúdo mudou depois da aprovação: %s", row.id)
                raise ReleaseError(
                    "conteudo_mudou",
                    "o conteúdo mudou depois da aprovação — peça de novo.",
                )
        async with self._db.get_session() as session:
            atual = await session.get(ReleaseRequest, row.id)
            if atual is None or atual.status != STATUS_APROVADA:
                raise ReleaseError("nao_aprovada", "essa liberação não está mais aprovada.")
            atual.status = STATUS_USADA
            atual.used_at = datetime.utcnow()
            session.add(atual)
            await session.commit()
            await session.refresh(atual)
        logger.info("[release] pedido %s consumido", atual.id)
        return atual

    # ── internos ─────────────────────────────────────────────────────────

    @staticmethod
    def _is_expired(row: ReleaseRequest) -> bool:
        return bool(
            row.status == STATUS_PENDENTE
            and row.expires_at
            and row.expires_at < datetime.utcnow()
        )

    async def _find_pendente_igual(
        self, *, user_id: UUID, campos: list[str], destino: str, finalidade: str
    ) -> ReleaseRequest | None:
        for row in await self.list_pending(user_id):
            if (
                list(row.campos or []) == campos
                and row.destino == destino
                and (row.finalidade or "") == (finalidade or "")
            ):
                return row
        return None

    async def _set_notification(self, request_id: UUID, notification_id: UUID) -> None:
        async with self._db.get_session() as session:
            row = await session.get(ReleaseRequest, request_id)
            if row is None:
                return
            row.notification_id = notification_id
            session.add(row)
            await session.commit()
