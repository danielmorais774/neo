#!/usr/bin/env python3
"""Shadow report do Registro de Contexto (F8 — docs/context-register-plan.md).

Com ``neo_register_shadow=True`` em prod, cada turno loga uma linha::

    [Register] derived=oficina reason=tipo_tarefa=analitica gravity=normal mode=shadow

Este script lê logs (arquivos ou stdin) e imprime a distribuição de registros,
motivos e gravidade derivados com tráfego REAL — a calibração antes do go-live
(passo 2 do rollout). Uma distribuição torta (ex.: tudo caindo em oficina)
significa derivação a ajustar ANTES de ligar ``neo_register_enabled``.

Uso:
    uv run python scripts/register_shadow_report.py logs/*.log
    docker logs neo-core 2>&1 | uv run python scripts/register_shadow_report.py
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

_LINE_RE = re.compile(
    r"\[Register\] derived=(?P<register>\S+) reason=(?P<reason>\S+) "
    r"gravity=(?P<gravity>\S+) mode=(?P<mode>\S+)"
)


def _iter_lines(args: list[str]):
    if args:
        for arg in args:
            path = Path(arg)
            if path.is_file():
                yield from path.read_text(encoding="utf-8", errors="replace").splitlines()
            else:
                print(f"aviso: {arg} não é arquivo — ignorado", file=sys.stderr)
    else:
        yield from sys.stdin


def _bar(count: int, total: int, width: int = 32) -> str:
    filled = round(width * count / total) if total else 0
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    registers: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    gravities: Counter[str] = Counter()
    modes: Counter[str] = Counter()

    for line in _iter_lines(sys.argv[1:]):
        match = _LINE_RE.search(line)
        if not match:
            continue
        registers[match["register"]] += 1
        reasons[match["reason"]] += 1
        gravities[match["gravity"]] += 1
        modes[match["mode"]] += 1

    total = sum(registers.values())
    if not total:
        print("Nenhuma linha [Register] encontrada — o shadow mode está ligado? "
              "(neo_register_shadow=True)")
        return 1

    print(f"Turnos com registro derivado: {total} "
          f"(shadow={modes.get('shadow', 0)}, inject={modes.get('inject', 0)})\n")

    print("Distribuição por registro:")
    for register, count in registers.most_common():
        pct = 100.0 * count / total
        print(f"  {register:<12} {count:>6}  {pct:5.1f}%  {_bar(count, total)}")

    print("\nTop motivos de derivação:")
    for reason, count in reasons.most_common(12):
        print(f"  {reason:<32} {count:>6}")

    grave = sum(count for gravity, count in gravities.items() if gravity != "normal")
    print(f"\nGravidade: normal={gravities.get('normal', 0)} "
          f"alta={gravities.get('alta', 0)} critica={gravities.get('critica', 0)} "
          f"({100.0 * grave / total:.1f}% dos turnos graves)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
