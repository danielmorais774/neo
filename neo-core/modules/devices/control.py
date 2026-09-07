"""Controle de device — resolução por nome e despacho de ações.

Este é o módulo que transforma "abaixa o volume do PC da sala" em um comando
entregue ao device certo, do dono certo, com resultado verificado.

Três invariantes, todas aprendidas em falhas anteriores do repo:

1. **A tool recebe NOME, nunca id.** O ``device_id`` é um UUID opaco; se ele
   entrar no schema, o modelo inventa um. Foi exatamente isso no
   ``file_id`` (o classificador punha o *nome* do arquivo no campo do id) e no
   ``entity_id`` do Home Assistant (entidade inventada → "desliguei" sem
   desligar). A resolução acontece AQUI, contra o catálogo do banco.
2. **Ambiguidade pergunta, não chuta.** Dois devices casando com "notebook"
   devolvem ``status="ambiguous"`` com os candidatos, e o cérebro pergunta.
3. **Offline falha duro.** Nunca reportar sucesso sem confirmação do device.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from core.notifications.device_control import (
    DeviceOffline,
    DeviceTimeout,
    get_device_control_broadcaster,
)

logger = logging.getLogger(__name__)

# Ações de mídia/volume — respondem rápido e devolvem estado.
CONTROL_ACTIONS: frozenset[str] = frozenset(
    {
        "volume_set",
        "volume_up",
        "volume_down",
        "mute",
        "unmute",
        "play",
        "pause",
        "play_pause",
        "next",
        "previous",
        "lock",
        "get_status",
    }
)

# Ações que derrubam a máquina: a confirmação é a desconexão do canal, não uma
# resposta (a máquina cai antes de conseguir responder).
POWER_ACTIONS: frozenset[str] = frozenset({"suspend", "shutdown", "restart"})

# Ações que exigem um nível 0-100.
_NEEDS_LEVEL: frozenset[str] = frozenset({"volume_set"})

# Qual capability cada ação exige. Sem isso o cérebro oferece `shutdown` num
# device que não implementa e a tool falha DEPOIS de prometer.
_CAPABILITY_FOR: dict[str, str] = {
    **{a: "volume" for a in ("volume_set", "volume_up", "volume_down", "mute", "unmute")},
    **{a: "media" for a in ("play", "pause", "play_pause", "next", "previous")},
    "lock": "lock",
    **{a: "power" for a in POWER_ACTIONS},
}


def _normalize(text: str) -> str:
    """Casefold sem acento — "PC da Sala" casa com "pc da sala"."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return stripped.casefold().strip()


@dataclass
class DeviceMatch:
    device_id: str
    display_name: str | None
    model: str
    os_platform: str
    is_online: bool
    capabilities: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Como o device é citado numa resposta ao usuário."""
        return self.display_name or f"{self.model} ({self.os_platform})"


class DeviceResolutionError(Exception):
    """Falha de resolução com uma mensagem já pronta para o usuário."""

    def __init__(self, message: str, *, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.candidates = candidates or []


async def _load_devices(db: Any, user_id: UUID) -> list[DeviceMatch]:
    """Devices do usuário, com o online vindo do canal de controle.

    O ``is_online`` daqui é mais estrito do que o de ``list_connected_devices``:
    lá vale o WS de presença (ou o último estado do banco); aqui só vale ter o
    canal de CONTROLE aberto — é o único que consegue executar alguma coisa.
    """
    broadcaster = get_device_control_broadcaster()
    records = await db.get_connected_devices(user_id) if db else []
    return [
        DeviceMatch(
            device_id=r.device_id,
            display_name=getattr(r, "display_name", None),
            model=r.model,
            os_platform=r.os_platform,
            is_online=broadcaster.is_connected(r.device_id),
            capabilities=getattr(r, "capabilities", None) or {},
        )
        for r in records
    ]


def _match_by_name(devices: list[DeviceMatch], name: str) -> list[DeviceMatch]:
    """Casamento em cascata: nome exato → prefixo → substring → modelo/SO."""
    needle = _normalize(name)
    if not needle:
        return []

    exact = [d for d in devices if _normalize(d.display_name or "") == needle]
    if exact:
        return exact

    prefix = [d for d in devices if _normalize(d.display_name or "").startswith(needle)]
    if prefix:
        return prefix

    substring = [d for d in devices if needle in _normalize(d.display_name or "")]
    if substring:
        return substring

    # Último recurso: o usuário pode citar o hardware ("o mac", "o windows")
    # quando ainda não batizou o device.
    return [
        d
        for d in devices
        if needle in _normalize(d.model) or needle in _normalize(d.os_platform)
    ]


async def resolve_device(
    db: Any,
    user_id: UUID,
    name: str | None,
    *,
    current_device_id: str | None = None,
) -> DeviceMatch:
    """Resolve o device alvo. Levanta ``DeviceResolutionError`` com texto pronto."""
    devices = await _load_devices(db, user_id)
    if not devices:
        raise DeviceResolutionError("Nenhum dispositivo registrado nesta conta.")

    if not (name or "").strip():
        # Sem nome: o device da própria conversa é o alvo natural.
        if current_device_id:
            for d in devices:
                if d.device_id == current_device_id:
                    return d
        online = [d for d in devices if d.is_online]
        if len(online) == 1:
            return online[0]
        if not online:
            raise DeviceResolutionError(
                "Nenhum dispositivo está com o canal de controle aberto agora."
            )
        raise DeviceResolutionError(
            "Tem mais de um dispositivo disponível — diga qual.",
            candidates=[d.label for d in online],
        )

    matches = _match_by_name(devices, name)
    if not matches:
        raise DeviceResolutionError(
            f"Não achei nenhum dispositivo chamado '{name}'.",
            candidates=[d.label for d in devices],
        )
    if len(matches) > 1:
        raise DeviceResolutionError(
            f"'{name}' casa com mais de um dispositivo — diga qual.",
            candidates=[d.label for d in matches],
        )
    return matches[0]


def _validate(action: str, value: Any) -> dict[str, Any]:
    """Valida a ação e monta os args. Levanta ValueError com texto pronto."""
    if action in _NEEDS_LEVEL:
        if value is None:
            raise ValueError(f"'{action}' precisa de um nível de 0 a 100.")
        try:
            level = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Nível inválido para '{action}': {value!r}")
        if not 0 <= level <= 100:
            raise ValueError(f"Nível fora da faixa 0-100: {level}")
        return {"level": level}

    if action in ("volume_up", "volume_down"):
        # Passo opcional; 10 é o degrau que o usuário espera de "aumenta um pouco".
        try:
            step = int(value) if value is not None else 10
        except (TypeError, ValueError):
            step = 10
        return {"step": max(1, min(100, step))}

    return {}


def _supports(device: DeviceMatch, action: str) -> bool:
    """O device declarou saber fazer isso?

    Ausência de ``capabilities`` é tratada como "sim": devices pareados por uma
    versão antiga do cliente não declaram nada, e recusar tudo neles seria uma
    regressão silenciosa. Quem de fato decide é o device, que responde
    ``ok:false`` se não souber — a capability só evita prometer antes de tentar.
    """
    if not device.capabilities:
        return True
    needed = _CAPABILITY_FOR.get(action)
    if needed is None:
        return True
    return bool(device.capabilities.get(needed, False))


async def execute_device_action(
    db: Any,
    user_id: UUID,
    *,
    action: str,
    device_name: str | None = None,
    value: Any = None,
    current_device_id: str | None = None,
) -> dict[str, Any]:
    """Resolve o device, valida e executa. Devolve o resultado REAL.

    Nunca devolve ``ok:true`` sem confirmação vinda do device: em
    ``CONTROL_ACTIONS`` a confirmação é a resposta; em ``POWER_ACTIONS`` é a
    desconexão do canal.
    """
    if action not in CONTROL_ACTIONS and action not in POWER_ACTIONS:
        return {"ok": False, "error": f"Ação desconhecida: {action}"}

    try:
        device = await resolve_device(
            db, user_id, device_name, current_device_id=current_device_id
        )
    except DeviceResolutionError as e:
        return {"ok": False, "error": e.message, "candidates": e.candidates}

    if not device.is_online:
        online = [d.label for d in await _load_devices(db, user_id) if d.is_online]
        return {
            "ok": False,
            "error": (
                f"'{device.label}' não está com o canal de controle aberto "
                "(desligado, ou desktop fechado)."
            ),
            "devices_online": online,
        }

    if not _supports(device, action):
        return {
            "ok": False,
            "error": f"'{device.label}' não suporta '{action}'.",
        }

    try:
        args = _validate(action, value)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    broadcaster = get_device_control_broadcaster()

    if action in POWER_ACTIONS:
        # A máquina cai antes de responder — a confirmação é sumir do canal.
        try:
            gone = await broadcaster.send_and_expect_disconnect(device.device_id, action)
        except DeviceOffline as e:
            return {"ok": False, "error": str(e)}
        if gone:
            return {"ok": True, "device": device.label, "action": action, "confirmed_by": "disconnect"}
        return {
            "ok": False,
            "device": device.label,
            "action": action,
            "error": (
                f"Mandei '{action}' para '{device.label}', mas ele continua conectado — "
                "provavelmente o usuário cancelou no diálogo da máquina."
            ),
        }

    try:
        response = await broadcaster.request(device.device_id, action, args)
    except DeviceOffline as e:
        return {"ok": False, "device": device.label, "error": str(e)}
    except DeviceTimeout as e:
        return {"ok": False, "device": device.label, "error": str(e)}

    if not response.get("ok"):
        return {
            "ok": False,
            "device": device.label,
            "action": action,
            "error": response.get("error") or "o dispositivo recusou o comando",
        }

    return {
        "ok": True,
        "device": device.label,
        "action": action,
        "result": response.get("result") or {},
    }
