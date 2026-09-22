"""Guardas que rodam ANTES de gastar o disparo (docs/scheduler-plan.md §8).

Duas coisas moram aqui, e as duas existem pelo mesmo motivo: **todo alerta
periódico sem guarda vira ruído em uma semana, e aí o usuário desliga a agenda
inteira** — inclusive as tarefas que prestavam.

1. :func:`evaluate` — a condição. Barrou, o disparo nem acontece: não conta
   como execução, não conta como erro, não notifica. Só reagenda.
2. :func:`fingerprint` — o "só se mudou". É o que separa "a cada 15 min me diz
   o preço" (ruído) de "me avisa quando o preço mudar" (valor).

Nenhuma condição chama LLM. Uma condição cara derruba o argumento inteiro de
ter condição.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

CONDITION_TYPES = ("sempre", "tool", "device", "http")

_OPERADORES = {
    ">": lambda a, b: _num(a) > _num(b),
    ">=": lambda a, b: _num(a) >= _num(b),
    "<": lambda a, b: _num(a) < _num(b),
    "<=": lambda a, b: _num(a) <= _num(b),
    "==": lambda a, b: str(a).strip().lower() == str(b).strip().lower(),
    "!=": lambda a, b: str(a).strip().lower() != str(b).strip().lower(),
    "contem": lambda a, b: str(b).strip().lower() in str(a).lower(),
    "nao_vazio": lambda a, _b: _truthy(a),
    "vazio": lambda a, _b: not _truthy(a),
}


class ConditionError(ValueError):
    """Condição malformada — erro de configuração, não de execução."""


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value or ""))
    if not match:
        raise ConditionError(f"Valor não numérico na comparação: {value!r}")
    return float(match.group(0).replace(",", "."))


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, tuple, dict, set, str)):
        return len(value) > 0
    return True


def _pluck(payload: Any, campo: str | None) -> Any:
    """Caminho pontuado no resultado ("dados.total", "itens.0.preco")."""
    if not campo:
        return payload
    cursor = payload
    for part in str(campo).split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, (list, tuple)) and part.isdigit():
            idx = int(part)
            cursor = cursor[idx] if idx < len(cursor) else None
        else:
            return None
    return cursor


def validate(condition: dict | None) -> dict:
    """Valida a condição na hora de SALVAR — erro de forma nunca em runtime."""
    if not condition:
        return {}
    tipo = str(condition.get("tipo") or "sempre").strip()
    if tipo not in CONDITION_TYPES:
        raise ConditionError(
            f"Tipo de condição desconhecido: {tipo!r}. Use um de {', '.join(CONDITION_TYPES)}."
        )
    if tipo == "tool" and not condition.get("tool"):
        raise ConditionError("Condição do tipo 'tool' precisa do campo 'tool'.")
    if tipo == "http" and not condition.get("url"):
        raise ConditionError("Condição do tipo 'http' precisa do campo 'url'.")
    espera = condition.get("espera") or {}
    if espera:
        operador = str(espera.get("operador") or "nao_vazio")
        if operador not in _OPERADORES:
            raise ConditionError(
                f"Operador desconhecido: {operador!r}. Use um de {', '.join(_OPERADORES)}."
            )
    return condition


async def evaluate(
    condition: dict | None,
    *,
    run_tool: Callable[[str, dict], Awaitable[Any]] | None = None,
    devices_online: Callable[[], Awaitable[list[str]]] | None = None,
    http_get: Callable[..., Awaitable[Any]] | None = None,
) -> tuple[bool, str, Any]:
    """``(passou, explicação, resultado)``.

    Erro ao AVALIAR conta como não-passou, com a explicação no log da tarefa —
    e nunca como falha do disparo: uma caixa de email fora do ar não pode
    queimar o disjuntor de uma tarefa que nem chegou a rodar.
    """
    if not condition or condition.get("tipo") in (None, "", "sempre"):
        return True, "sem condição", None

    tipo = condition["tipo"]
    try:
        if tipo == "tool":
            if run_tool is None:
                return False, "condição de tool sem executor disponível", None
            payload = await run_tool(str(condition["tool"]), dict(condition.get("arguments") or {}))
        elif tipo == "device":
            if devices_online is None:
                return False, "condição de device sem fonte de aparelhos", None
            online = await devices_online()
            alvo = condition.get("device_id")
            payload = [d for d in online if not alvo or d == alvo]
        elif tipo == "http":
            if http_get is None:
                return False, "condição http sem cliente", None
            payload = await http_get(
                str(condition["url"]),
                method=str(condition.get("metodo") or "GET"),
                headers=condition.get("headers") or {},
                json_body=condition.get("body"),
            )
        else:  # pragma: no cover — validate() já barrou
            return False, f"tipo desconhecido: {tipo}", None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agenda] condição %s falhou: %s", tipo, exc)
        return False, f"a condição não pôde ser avaliada ({exc})", None

    espera = condition.get("espera") or {}
    operador = str(espera.get("operador") or "nao_vazio")
    alvo = _pluck(payload, espera.get("campo"))
    try:
        passou = _OPERADORES[operador](alvo, espera.get("valor"))
    except ConditionError as exc:
        return False, str(exc), payload
    except Exception as exc:  # noqa: BLE001
        return False, f"comparação inválida: {exc}", payload

    campo = espera.get("campo") or "resultado"
    valor = espera.get("valor")
    explicacao = (
        f"{campo} {operador} {valor} → {'passou' if passou else 'não passou'} (valor: {_short(alvo)})"
        if espera else
        f"{'passou' if passou else 'não passou'} (valor: {_short(alvo)})"
    )
    return passou, explicacao, payload


def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def fingerprint(value: Any) -> str:
    """Hash estável do resultado, para o ``only_if_changed``.

    Normaliza antes de hashear: dicionário com chaves fora de ordem e número
    formatado de outro jeito não podem parecer "mudança". O que sobra de
    variação real (um timestamp no meio do texto) é problema do conteúdo, não
    do hash — e aí a tarefa quer mesmo é uma condição, não este atalho.
    """
    if value is None:
        normal = ""
    elif isinstance(value, str):
        normal = " ".join(value.split())
    else:
        normal = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()[:32]
