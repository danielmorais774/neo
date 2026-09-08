"""EventRouter — do evento cru à ação de volta (docs/playlists-skill-plan.md §7.5).

O caminho quente inteiro mora aqui, e ele tem uma regra: **não acorda o
cognitivo**. Nenhum prompt, nenhum LLM, nenhuma sessão de conversa. Um fim de
faixa custa duas queries e um update.

As quatro guardas, na ordem em que rodam (§7.4):

1. **casamento** — aparelho, canal, evento e alvo (aba). Não casou, acabou.
2. **dedup/atraso** — o mesmo evento reenviado (reconnect da extensão, alarme e
   listener juntos) e o evento *atrasado* da faixa que já avançou usam a mesma
   chave: ``<aba>:<faixa>``. Sem isso a fila pula três músicas de uma vez.
3. **faixa esperada** — se o que terminou não é o que despachamos, o usuário
   assumiu o player: a inscrição se **pausa**, não avança e não briga.
4. **tetos** — TTL, ``max_fires`` e disjuntor de erros seguidos.

Concorrência: um lock em memória por inscrição. O neo-core roda em um processo
só, e a chave de dedup persistida cobre o caso de dois processos — mas se um dia
houver mais de um worker, é aqui que entra um advisory lock do Postgres.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from core.events.registry import Subscription, SubscriptionRegistry
from core.events.runners import RUNNERS, RunnerContext, RunnerResult
from core.events.schema import ADVANCING_EVENTS, EventTrigger, TriggerError, parse_triggers

logger = logging.getLogger(__name__)

#: Erros seguidos antes de desistir da inscrição. O antídoto do retry infinito
#: que já travou mic + wake word uma vez (memória neo-live-tts-stall-lockout).
MAX_CONSECUTIVE_ERRORS = 3

#: Resposta "não faça nada" — o aparelho desarma o watch e volta ao normal.
NOTHING: dict[str, Any] = {"acao": "nada"}


class EventRouter:
    """Recebe eventos do aparelho e devolve a ação (ou nada)."""

    def __init__(
        self,
        *,
        db: Any,
        registry: SubscriptionRegistry,
        skill_library: Any = None,
        dataset_registry: Any = None,
        dataset_service_factory: Any = None,
        broker: Any = None,
    ) -> None:
        self._db = db
        self._subs = registry
        self._library = skill_library
        self._datasets = dataset_registry
        self._service_factory = dataset_service_factory
        self._broker = broker
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Gatilhos declarados nas skills
    # ------------------------------------------------------------------

    def triggers_of(self, skill_name: str) -> list[EventTrigger]:
        """Gatilhos do ``eventos:`` de uma skill. Lista vazia se não houver."""
        if self._library is None:
            return []
        entry = self._library.get(skill_name)
        if entry is None:
            return []
        try:
            return parse_triggers(getattr(entry, "eventos", None), skill=skill_name)
        except TriggerError as exc:
            logger.warning("[events] gatilhos de '%s' inválidos: %s", skill_name, exc)
            return []

    def resolve_trigger(self, handler_ref: str) -> EventTrigger | None:
        """"<skill>.<gatilho>" → gatilho, lido do SKILL.md AGORA.

        De propósito: editar o SKILL.md corrige uma fila em andamento, e uma
        skill removida encerra a inscrição em vez de disparar receita velha.
        """
        skill, _, nome = (handler_ref or "").partition(".")
        if not skill or not nome:
            return None
        return next((t for t in self.triggers_of(skill) if t.nome == nome), None)

    # ------------------------------------------------------------------
    # Caminho quente
    # ------------------------------------------------------------------

    @staticmethod
    def event_key(target: dict[str, Any], ref: str) -> str:
        """``<aba>:<faixa>`` — dedup e detecção de evento atrasado na mesma chave."""
        tab = (target or {}).get("tab_id")
        return f"{tab if tab not in (None, '') else '-'}:{(ref or '-')[:120]}"

    def _lock(self, subscription_id: uuid.UUID) -> asyncio.Lock:
        key = str(subscription_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Processa um evento. Devolve a ação para o aparelho (ou ``NOTHING``)."""
        channel = str(payload.get("canal") or payload.get("channel") or "musica").strip().lower()
        event = str(payload.get("evento") or payload.get("event") or "").strip().lower()
        device_id = str(payload.get("device_id") or "").strip()
        ref = str(payload.get("video_id") or payload.get("ref") or "").strip()
        # Código do item da fila que o aparelho está reportando. É ele que faz o
        # status acertar a LINHA certa (e não "o que estiver como tocando").
        codigo = str(payload.get("codigo") or "").strip()
        target = {
            k: v
            for k, v in {
                "site": (payload.get("site") or "").strip().lower() or None,
                "tab_id": payload.get("tab_id"),
            }.items()
            if v not in (None, "")
        }

        if not device_id or not event:
            return {**NOTHING, "motivo": "evento sem device_id/evento"}

        # O aparelho devolve o `inscricao_id` que NÓS entregamos junto com a
        # ordem de tocar. Quando ele vem, é a identificação mais forte que
        # existe — e resolve o caso em que a música abriu numa ABA NOVA (o
        # `open_url` faz isso): o alvo gravado aponta para a aba velha e o
        # casamento por aba recusaria o evento da fila que nós mesmos tocamos.
        sub = None
        sub_id = str(payload.get("inscricao_id") or "").strip()
        if sub_id:
            try:
                candidate = await self._subs.get(uuid.UUID(sub_id))
            except (ValueError, TypeError):
                candidate = None
            if (
                candidate is not None
                and candidate.device_id == device_id
                and candidate.is_live()
                and (not candidate.events or event in candidate.events)
            ):
                sub = candidate
                # A aba mudou debaixo da inscrição: seguimos o player.
                new_tab = target.get("tab_id")
                if new_tab not in (None, "") and str((sub.target or {}).get("tab_id") or "") != str(new_tab):
                    novo_alvo = {**(sub.target or {}), "tab_id": new_tab}
                    await self._subs.update(sub.id, target=novo_alvo)
                    sub.target = novo_alvo
        if sub is None:
            sub = await self._subs.live_for_event(
                device_id=device_id, channel=channel, event=event, target=target
            )
        if sub is None:
            # O caso mais comum e o mais importante: NÃO havia inscrição. É o
            # "toca um AC/DC" — nada acontece, e nem entra no log (senão o log
            # vira ruído de todo evento de mídia da máquina).
            return {**NOTHING, "motivo": "sem inscrição para este evento"}

        async with self._lock(sub.id):
            # Relê dentro do lock: outro evento pode ter avançado a fila enquanto
            # esperávamos, e aí o `expected_ref` já é outro.
            fresh = await self._subs.get(sub.id)
            if fresh is None or not fresh.is_live():
                return {**NOTHING, "motivo": "inscrição não está mais armada"}
            return await self._dispatch(
                fresh, channel=channel, event=event, ref=ref, target=target, codigo=codigo
            )

    async def _dispatch(
        self,
        sub: Subscription,
        *,
        channel: str,
        event: str,
        ref: str,
        target: dict[str, Any],
        codigo: str = "",
    ) -> dict[str, Any]:
        key = self.event_key(sub.target or target, ref)
        # Evento de STATUS (faixa_iniciou) não faz a fila andar: não passa pela
        # dedup nem carimba a chave. Se passasse, o "começou a tocar" comeria a
        # vez do "está acabando" da mesma faixa e a fila travaria na primeira.
        advancing = event in ADVANCING_EVENTS

        # (2) repetido ou atrasado — mesma chave, mesma resposta.
        if advancing and key and key == sub.last_event_key:
            await self._log(sub, channel, event, key, "ignorado", "evento repetido ou atrasado")
            return {**NOTHING, "motivo": "evento repetido"}

        # (3) não é a faixa que eu despachei → o usuário assumiu o player.
        if sub.expected_ref and ref and ref != sub.expected_ref:
            await self._subs.pause(
                sub.id, f"faixa inesperada ({ref}) — o player mudou fora da fila"
            )
            await self._log(sub, channel, event, key, "ignorado", "faixa diferente da esperada")
            return {**NOTHING, "motivo": "faixa não é a da fila; inscrição pausada"}

        # (4) tetos.
        if advancing and sub.max_fires and sub.fires >= sub.max_fires:
            await self._subs.end(sub.id, "teto de disparos atingido")
            await self._log(sub, channel, event, key, "encerrado", "teto de disparos")
            return {"acao": "desarmar", "motivo": "teto de disparos atingido"}

        trigger = self.resolve_trigger(sub.handler_ref)
        if trigger is None:
            await self._subs.end(sub.id, f"gatilho '{sub.handler_ref}' não existe mais")
            await self._log(sub, channel, event, key, "encerrado", "gatilho ausente")
            return {"acao": "desarmar", "motivo": "gatilho não existe mais"}

        runner = RUNNERS.get(trigger.faz)
        if runner is None:  # pragma: no cover — validado no parse
            await self._subs.end(sub.id, f"runner '{trigger.faz}' desconhecido")
            return {"acao": "desarmar", "motivo": "runner desconhecido"}

        ctx = RunnerContext(
            trigger=trigger,
            subscription=sub,
            event={
                "evento": event,
                "ref": ref,
                "codigo": codigo,
                "target": target,
                "canal": channel,
            },
            user_id=sub.user_id,
            db=self._db,
            dataset_registry=self._datasets,
            dataset_service=self._service(),
        )
        try:
            result: RunnerResult = await runner(ctx)
        except Exception as exc:  # noqa: BLE001 — falha do runner não derruba o endpoint
            logger.warning("[events] runner '%s' falhou: %s", trigger.faz, exc)
            errors = sub.consecutive_errors + 1
            if errors >= MAX_CONSECUTIVE_ERRORS:
                await self._subs.end(sub.id, f"{errors} erros seguidos: {exc}")
                await self._notify(
                    sub, f"Parei a fila: deu erro {errors} vezes seguidas ({exc})."
                )
                await self._log(sub, channel, event, key, "erro", f"disjuntor: {exc}")
                return {"acao": "desarmar", "motivo": "erros seguidos"}
            await self._subs.update(sub.id, consecutive_errors=errors, reason=str(exc)[:500])
            await self._log(sub, channel, event, key, "erro", str(exc))
            return {**NOTHING, "motivo": f"erro no gatilho: {exc}"}

        return await self._apply(
            sub, result, channel=channel, event=event, key=key, advancing=advancing
        )

    async def _apply(
        self,
        sub: Subscription,
        result: RunnerResult,
        *,
        channel: str,
        event: str,
        key: str,
        advancing: bool = True,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {
            "consecutive_errors": 0,
            "last_event_at": datetime.utcnow(),
            "reason": (result.reason or "")[:500],
        }
        if advancing:
            updates["last_event_key"] = key
        if result.action is not None:
            updates["fires"] = sub.fires + 1
            updates["expected_ref"] = (result.expected_ref or "")[:128]
            # O código do despacho novo passa a ser o "que estou vigiando": é ele
            # que o aparelho devolve nos próximos eventos.
            updates["current_code"] = str(result.action.get("codigo") or "")[:16]
        if result.end:
            updates["state"] = "encerrada"

        await self._subs.update(sub.id, **updates)
        if result.notify:
            await self._notify(sub, result.notify)

        if result.action is not None:
            action = dict(result.action)
            action["rearmar"] = {
                "inscricao_id": str(sub.id),
                "ref": result.expected_ref,
                "tab_id": (sub.target or {}).get("tab_id"),
            }
            await self._log(sub, channel, event, key, "despachado", result.reason, action)
            return action

        await self._log(
            sub, channel, event, key,
            "encerrado" if result.end else "ignorado",
            result.reason,
        )
        if result.end:
            return {"acao": "desarmar", "motivo": result.reason or "inscrição encerrada"}
        return {**NOTHING, "motivo": result.reason or "nada a fazer"}

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------

    def _service(self) -> Any:
        if self._service_factory is None:
            return None
        try:
            return self._service_factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[events] DatasetService indisponível: %s", exc)
            return None

    async def _log(
        self,
        sub: Subscription | None,
        channel: str,
        event: str,
        key: str,
        outcome: str,
        reason: str = "",
        action: dict[str, Any] | None = None,
    ) -> None:
        await self._subs.log_delivery(
            subscription=sub,
            user_id=sub.user_id if sub else None,
            channel=channel,
            event=event,
            event_key=key,
            outcome=outcome,
            reason=reason,
            action=action,
        )

    async def _notify(self, sub: Subscription, text: str) -> None:
        """Aviso proativo no chat. Reusa o canal `chat_message` dos jobs.

        Nunca levanta: um aviso que falha não pode derrubar o despacho da fila.
        """
        if sub.user_id is None or not text:
            return
        notif: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "user_id": str(sub.user_id),
            "mission_id": "",
            "kind": "chat_message",
            "title": "Fila de música",
            "body": text,
            "description": text,
            "payload": {
                "session_id": str(sub.session_id) if sub.session_id else "",
                "text": text,
                "kind_detail": "evento",
            },
            "created_at_unix": int(datetime.utcnow().timestamp()),
        }
        try:
            created = await self._db.create_notification(
                user_id=sub.user_id,
                mission_id=None,
                kind="chat_message",
                title=notif["title"],
                body=text,
                payload=notif["payload"],
            )
            if created is not None:
                notif["id"] = str(created.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[events] create_notification falhou: %s", exc)
        if self._broker is not None:
            try:
                await self._broker.publish(sub.user_id, notif)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[events] broker publish falhou: %s", exc)
        try:
            from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

            broadcaster = get_device_notif_broadcaster()
            devices = await self._db.get_connected_devices(sub.user_id)
            sent: set[str] = set()
            for device in devices:
                for candidate in (device.device_id, getattr(device, "ws_connection_id", None)):
                    if candidate and candidate not in sent:
                        if await broadcaster.send_notification(candidate, notif):
                            sent.add(candidate)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[events] fan-out por device falhou: %s", exc)
