"""ProactiveDispatcher — entrega o resultado de um job de volta na conversa.

Quando um job atinge um estado terminal (ou um timer dispara), este dispatcher:
 1. redige uma mensagem curta com a persona do Neo a partir do resultado/logs;
 2. persiste como mensagem do assistente na sessão de origem (fica na história,
    inclusive offline);
 3. publica no canal de notificação existente com ``kind="chat_message"`` — reusa
    o transporte do broker + o fan-out por device (replay offline sai de graça).

Nada de RPC/proto novo: o ``payload_json`` já é livre. O cliente (neo-desktop-tauri)
ganha um handler para ``kind="chat_message"`` que injeta a mensagem na timeline (+
TTS no Modo Live). Ver ``docs/workbench-background-jobs-plan.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from db.models.agent_job import AgentJob
from shared import Message, MessageRole

logger = logging.getLogger(__name__)


_COMPOSE_SYSTEM = (
    "Você é o Neo avisando o usuário que uma tarefa que rodava em segundo plano "
    "terminou. Escreva UMA mensagem curta, natural e em primeira pessoa, no seu "
    "tom de sempre, dizendo o que aconteceu e o resultado essencial. Não use "
    "markdown pesado nem cole saídas cruas gigantes; resuma. Se falhou, diga de "
    "forma direta e, se der, o motivo. Não invente dados que não estão no resultado."
)


class ProactiveDispatcher:
    """Redige e entrega a resposta proativa de um job concluído."""

    def __init__(
        self,
        *,
        db: Any,
        broker: Any,
        llm_router: Any = None,
        default_model: str | None = None,
    ) -> None:
        self.db = db
        self.broker = broker
        self.llm = llm_router
        self.default_model = default_model

    async def dispatch(self, job: AgentJob) -> None:
        """Entrega o resultado do ``job`` (idempotente: só entrega uma vez)."""
        if job.notified_at is not None:
            return
        if job.notify_policy == "silent":
            await self._mark_notified(job)
            return

        text = await self._compose(job)

        # 1. Persiste na história da sessão de origem (se houver).
        if job.session_id is not None:
            try:
                await self.db.save_message(
                    session_id=job.session_id,
                    role="assistant",
                    content=text,
                    user_id=job.user_id,
                    metadata={"source": "agent_job", "job_id": str(job.id), "kind": job.kind},
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[Dispatch] persist failed for job %s: %s", job.id, e)

        # 2. Publica no canal de notificação (chat_message).
        if job.notify_policy in ("chat", "both"):
            await self._publish_chat_message(job, text)
        elif job.notify_policy == "notch":
            await self._publish_notch(job, text)

        await self._mark_notified(job)

    # ------------------------------------------------------------------

    async def _compose(self, job: AgentJob) -> str:
        """Redige a mensagem do usuário a partir do resultado do job."""
        summary = (job.result_summary or "").strip()
        fallback = self._fallback_text(job, summary)
        if self.llm is None:
            return fallback
        try:
            provider = self.llm.get_provider(None)
            spec = job.spec_json or {}
            detail = spec.get("command") or spec.get("goal") or job.title
            user_ctx = (
                f"Tarefa: {job.title}\n"
                f"Detalhe: {detail}\n"
                f"Estado final: {job.status}\n"
                f"Resultado/logs (pode estar truncado):\n{summary[:3500] or '(sem saída)'}"
            )
            messages = [
                Message(role=MessageRole.SYSTEM, content=_COMPOSE_SYSTEM),
                Message(role=MessageRole.USER, content=user_ctx),
            ]
            resp = await provider.complete(
                messages=messages,
                model=self.default_model,
                temperature=0.6,
                max_tokens=400,
            )
            text = (resp.get_content() or "").strip()
            return text or fallback
        except Exception as e:  # noqa: BLE001
            logger.warning("[Dispatch] compose LLM failed for job %s: %s", job.id, e)
            return fallback

    @staticmethod
    def _fallback_text(job: AgentJob, summary: str) -> str:
        if job.status == "completed":
            head = f"✅ Terminei: {job.title}."
        elif job.status == "timeout":
            head = f"⏱️ A tarefa \"{job.title}\" estourou o tempo limite e foi interrompida."
        elif job.status == "cancelled":
            head = f"🚫 Cancelei a tarefa \"{job.title}\"."
        else:
            head = f"❌ A tarefa \"{job.title}\" falhou."
        tail = f"\n\n{summary[:1200]}" if summary else ""
        return head + tail

    async def _publish_chat_message(self, job: AgentJob, text: str) -> None:
        notif = await self._create_notification(
            job,
            kind="chat_message",
            title=job.title,
            body=text,
        )
        await self._fanout(job, notif, text)

    async def _publish_notch(self, job: AgentJob, text: str) -> None:
        notif = await self._create_notification(
            job,
            kind="job_update",
            title=job.title,
            body=text[:280],
        )
        await self._fanout(job, notif, text)

    async def _create_notification(
        self, job: AgentJob, *, kind: str, title: str, body: str
    ) -> dict[str, Any]:
        """Cria a linha em user_notification (replay offline) e devolve o dict."""
        payload = {
            "job_id": str(job.id),
            "session_id": str(job.session_id) if job.session_id else "",
            "text": body,
            "kind_detail": job.kind,
            "status": job.status,
        }
        created_unix = int(datetime.utcnow().timestamp())
        db_notif = None
        if job.user_id is not None:
            try:
                db_notif = await self.db.create_notification(
                    user_id=job.user_id,
                    mission_id=None,
                    kind=kind,
                    title=title,
                    body=body,
                    payload=payload,
                )
                created_unix = (
                    int(db_notif.created_at.timestamp()) if db_notif and db_notif.created_at else created_unix
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[Dispatch] create_notification failed for job %s: %s", job.id, e)
        return {
            "id": str(db_notif.id) if db_notif else str(job.id),
            "user_id": str(job.user_id) if job.user_id else "",
            "mission_id": "",
            "kind": kind,
            "title": title,
            "body": body,
            "description": body,
            "payload": payload,
            "created_at_unix": created_unix,
        }

    async def _fanout(self, job: AgentJob, notif: dict[str, Any], text: str) -> None:
        """Envia a notificação pelos dois canais existentes (broker + por device)."""
        if job.user_id is not None and self.broker is not None:
            try:
                await self.broker.publish(job.user_id, notif)
            except Exception as e:  # noqa: BLE001
                logger.warning("[Dispatch] broker publish failed for job %s: %s", job.id, e)

        # Fan-out por device WS (mesmo caminho que o desktop usa hoje).
        try:
            from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

            if job.user_id is not None:
                broadcaster = get_device_notif_broadcaster()
                devices = await self.db.get_connected_devices(job.user_id)
                sent: set[str] = set()
                for device in devices:
                    for key in (device.device_id, getattr(device, "ws_connection_id", None)):
                        if key and key not in sent:
                            if await broadcaster.send_notification(key, notif):
                                sent.add(key)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Dispatch] device fan-out failed for job %s: %s", job.id, e)

    async def _mark_notified(self, job: AgentJob) -> None:
        try:
            await self.db.update_agent_job(job.id, notified_at=datetime.utcnow())
        except Exception as e:  # noqa: BLE001
            logger.warning("[Dispatch] mark notified failed for job %s: %s", job.id, e)
