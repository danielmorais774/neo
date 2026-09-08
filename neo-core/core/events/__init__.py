"""Barramento de eventos das skills (docs/playlists-skill-plan.md §7).

Uma skill declara ``eventos:`` no SKILL.md, o cérebro cria uma **inscrição
concreta** (aparelho + aba + faixa) quando começa a tocar uma fila, e o aparelho
manda o evento de volta quando aquela faixa está acabando. Sem inscrição não há
evento: um pedido avulso de música continua custando exatamente o que custava.
"""

from core.events.registry import Subscription, SubscriptionRegistry
from core.events.router import EventRouter
from core.events.runners import RUNNERS, RunnerContext, RunnerResult
from core.events.schema import (
    KNOWN_CHANNELS,
    KNOWN_EVENTS,
    EventTrigger,
    TriggerError,
    parse_triggers,
)

__all__ = [
    "EventRouter",
    "EventTrigger",
    "KNOWN_CHANNELS",
    "KNOWN_EVENTS",
    "RUNNERS",
    "RunnerContext",
    "RunnerResult",
    "Subscription",
    "SubscriptionRegistry",
    "TriggerError",
    "parse_triggers",
]
