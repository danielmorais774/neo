"""Executor das tools ``event_*`` (docs/playlists-skill-plan.md §7.7).

User-scoped como ``data_*``/``fin_*``: o executor global fica sem usuário e
recusa; o cognitivo cria um fresco por request, e é ele que carrega o contexto
que o modelo NÃO deveria precisar adivinhar — qual aparelho, qual aba, qual
faixa acabou de tocar.

É esse contexto que permite a regra dura do subsistema: ``event_subscribe`` sem
alvo concreto **recusa**. O modelo não inventa `tab_id`; ele herda o alvo do que
o próprio Neo acabou de fazer, ou não inscreve.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.events.registry import SubscriptionRegistry
from core.events.router import EventRouter

logger = logging.getLogger(__name__)

_MAX_TTL_MINUTES = 480
_DEFAULT_TTL_MINUTES = 120


class EventsExecutor:
    """Despacha ``event_subscribe``/``event_unsubscribe``/``event_list``."""

    def __init__(self, *, db: Any, registry: SubscriptionRegistry, router: EventRouter) -> None:
        self._db = db
        self._subs = registry
        self._router = router
        self._user_id: uuid.UUID | None = None
        self._session_id: uuid.UUID | None = None
        self._device_id: str = ""
        self._media_context: dict[str, Any] = {}

    def set_context(
        self,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID | None = None,
        device_id: str | None = None,
        media_context: dict[str, Any] | None = None,
    ) -> None:
        self._user_id = user_id
        self._session_id = session_id
        self._device_id = (device_id or "").strip()
        self._media_context = media_context or {}

    # ------------------------------------------------------------------

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if self._user_id is None:
            raise ValueError(f"{tool_name} precisa de um usuário identificado.")
        args = arguments or {}
        if tool_name == "event_subscribe":
            return await self._subscribe(args)
        if tool_name == "event_unsubscribe":
            return await self._unsubscribe(args)
        if tool_name == "event_list":
            return await self._list(args)
        raise ValueError(f"Tool desconhecida: {tool_name}")

    # ------------------------------------------------------------------

    def _known_triggers(self) -> list[str]:
        library = getattr(self._router, "_library", None)
        if library is None:
            return []
        out: list[str] = []
        for entry in library.all():
            for trigger in self._router.triggers_of(entry.name):
                out.append(f"{entry.name}.{trigger.nome}")
        return out

    def _tab_from_media(self, faixa: str = "") -> dict[str, Any]:
        """A aba em que o Neo acabou de tocar — o alvo que o modelo não inventa.

        Preferência: a aba cujo vídeo é exatamente a faixa informada; depois a
        aba de YouTube tocando. Sem `media_context` (chamada fora do desktop),
        devolve {} e o subscribe recusa com uma frase explicando.
        """
        tabs = [t for t in (self._media_context.get("browser_tabs") or []) if isinstance(t, dict)]
        if not tabs:
            return {}
        if faixa:
            for tab in tabs:
                if str(tab.get("videoId") or "") and str(tab.get("videoId")) == faixa:
                    return tab
        playing = [t for t in tabs if t.get("site") == "youtube" and t.get("playing")]
        if playing:
            return playing[0]
        youtube = [t for t in tabs if t.get("site") == "youtube"]
        return youtube[0] if youtube else {}

    async def _subscribe(self, args: dict[str, Any]) -> dict[str, Any]:
        handler_ref = str(args.get("gatilho") or "").strip()
        if "." not in handler_ref:
            disponiveis = ", ".join(self._known_triggers()) or "(nenhum)"
            raise ValueError(
                f"'gatilho' precisa ser '<skill>.<nome>'. Disponíveis: {disponiveis}."
            )
        trigger = self._router.resolve_trigger(handler_ref)
        if trigger is None:
            disponiveis = ", ".join(self._known_triggers()) or "(nenhum)"
            raise ValueError(
                f"Gatilho '{handler_ref}' não existe. Disponíveis: {disponiveis}."
            )

        alvo_arg = args.get("alvo") if isinstance(args.get("alvo"), dict) else {}
        faixa = str(args.get("faixa") or "").strip()
        tab = self._tab_from_media(faixa)

        target: dict[str, Any] = {}
        site = str(alvo_arg.get("site") or tab.get("site") or "").strip().lower()
        tab_id = alvo_arg.get("tab_id", alvo_arg.get("tabId", tab.get("tabId")))
        if site:
            target["site"] = site
        if tab_id not in (None, ""):
            target["tab_id"] = tab_id
        if not faixa:
            faixa = str(tab.get("videoId") or "").strip()
        if not faixa:
            # Um link inteiro também serve como referência (Spotify, Deezer).
            faixa = str(tab.get("videoUrl") or "").strip()

        faixa = _canonical_ref(faixa)
        if not faixa:
            raise ValueError(
                "Não dá para inscrever no vazio: preciso da FAIXA concreta — o link "
                "(ou id) da música que você vai tocar agora, que é a primeira linha da "
                "fila. Passe faixa=<link>, ou inscreva logo depois de tocar (aí eu uso "
                "a aba onde você acabou de tocar). Um pedido avulso de música não deve "
                "armar gatilho nenhum."
            )
        if not self._device_id:
            raise ValueError(
                "Não sei de qual aparelho é esta conversa — sem isso o evento do fim "
                "da faixa não teria como casar com a inscrição."
            )

        # Uma inscrição por gatilho e aparelho: reinscrever é re-armar, não
        # acumular. Duas inscrições vivas no mesmo gatilho avançariam a fila
        # duas vezes por faixa.
        encerradas = await self._subs.end_many(
            user_id=self._user_id,
            device_id=self._device_id,
            handler_ref=handler_ref,
            reason="substituída por uma inscrição nova",
        )

        minutos = int(args.get("minutos") or _DEFAULT_TTL_MINUTES)
        sub = await self._subs.create(
            user_id=self._user_id,
            device_id=self._device_id,
            channel=trigger.canal,
            events=list(trigger.quando),
            target=target,
            handler_ref=handler_ref,
            handler_snapshot=trigger.model_dump(),
            expected_ref=faixa,
            session_id=self._session_id,
            ttl_minutes=max(1, min(minutos, _MAX_TTL_MINUTES)),
        )
        return {
            "ok": True,
            "conferido": _describe(sub),
            "substituidas": encerradas,
            "nota": (
                "Armado. Quando esta faixa terminar eu despacho a próxima da fila "
                "sozinho. Para parar: 'para a fila'."
            ),
        }

    async def _unsubscribe(self, args: dict[str, Any]) -> dict[str, Any]:
        sub_id = str(args.get("id") or "").strip()
        gatilho = str(args.get("gatilho") or "").strip()
        if sub_id:
            try:
                target_id = uuid.UUID(sub_id)
            except ValueError as exc:
                raise ValueError(f"id inválido: {sub_id!r}") from exc
            existing = await self._subs.get(target_id)
            if existing is None or existing.user_id != self._user_id:
                raise ValueError(f"Inscrição {sub_id} não existe.")
            await self._subs.end(target_id, "encerrada pelo usuário")
            return {"ok": True, "encerradas": 1, "conferido": _describe(await self._subs.get(target_id))}

        count = await self._subs.end_many(
            user_id=self._user_id,
            handler_ref=gatilho or None,
            reason="encerrada pelo usuário",
        )
        return {"ok": True, "encerradas": count}

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        states = ("armada", "pausada")
        if args.get("incluir_encerradas"):
            states = ("armada", "pausada", "encerrada")
        subs = await self._subs.list(user_id=self._user_id, states=states, limit=20)
        return {
            "total": len(subs),
            "inscricoes": [_describe(s) for s in subs],
        }


def _describe(sub: Any) -> dict[str, Any]:
    if sub is None:
        return {}
    return {
        "id": str(sub.id),
        "gatilho": sub.handler_ref,
        "canal": sub.channel,
        "eventos": sub.events,
        "alvo": sub.target,
        "faixa_esperada": sub.expected_ref,
        "estado": sub.state,
        "motivo": sub.reason,
        "disparos": sub.fires,
        "expira_em": sub.expires_at.isoformat() if sub.expires_at else None,
    }


def _canonical_ref(faixa: str) -> str:
    """Link salvo → a referência que o aparelho vai reportar.

    A fila guarda URL; a extensão reporta `videoId`. Sem esta normalização a
    primeira faixa seria "inesperada" e a inscrição se pausaria sozinha no
    primeiro evento — o bug mais chato possível, porque parece "não funciona".
    """
    ref = (faixa or "").strip()
    if not ref:
        return ""
    try:
        from core.skills.music.service import parse_direct_link

        parsed = parse_direct_link(ref)
    except Exception:  # noqa: BLE001
        parsed = None
    if parsed is not None and parsed.id:
        return parsed.id
    return ref[:128]
