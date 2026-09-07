#!/usr/bin/env python3
"""Mede o portão do navegador local contra as mensagens REAIS do usuário.

Por que medir em vez de chutar: o custo dos dois erros é assimétrico e alto.
Um falso positivo faz o Neo tomar conta do Chrome do usuário sem ele ter
pedido; um falso negativo faz ele agir escondido no servidor quando o usuário
queria justamente ASSISTIR. A lista de âncoras/afordâncias do ``gate.py`` só
pode ser ajustada com o número na mão (ver docs/browser-local-plan.md §5.3).

    docker exec neo-core python scripts/eval_browser_gate.py
    docker exec neo-core python scripts/eval_browser_gate.py --limit 5000 --show 40

Critério de aceite do plano:
  • ZERO falso positivo em pedido genérico de browser/pesquisa;
  • ≥90% de recall nas frases explícitas (a bateria de tests/browser_local).

Exit code 0 = passou; 1 = alguma marcação precisa de revisão humana.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.browser_local.gate import local_browser_requested  # noqa: E402

# Sinais de que a mensagem é uma tarefa de NAVEGADOR/pesquisa — é nessa fatia
# que o falso positivo importa. Fora dela ("bom dia"), o portão nem é chamado.
_BROWSER_TASKISH = re.compile(
    r"\b(pesquis|procur|busc|abre|abrir|entra|acessa|site|url|google|youtube|"
    r"navegador|browser|chrome|aba|link|comprar|preco|preço)\w*",
    re.IGNORECASE,
)


async def _load(limit: int) -> list[str]:
    from sqlalchemy import select

    from db.async_client import AsyncPostgresClient
    from db.models.conversation import ConversationMessage

    db = AsyncPostgresClient()
    async with db.get_session() as session:
        stmt = (
            select(ConversationMessage.content)
            .where(ConversationMessage.role == "user")
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
    return [str(r).strip() for r in rows if str(r or "").strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=3000, help="mensagens a carregar")
    parser.add_argument("--show", type=int, default=25, help="quantas marcações imprimir")
    args = parser.parse_args()

    messages = asyncio.run(_load(args.limit))
    if not messages:
        print("nenhuma mensagem no corpus — nada a medir")
        return 1

    taskish = [m for m in messages if _BROWSER_TASKISH.search(m)]
    flagged = [m for m in messages if local_browser_requested(m)]
    flagged_taskish = [m for m in taskish if local_browser_requested(m)]

    print(f"corpus: {len(messages)} mensagens de usuário")
    print(f"  parecem tarefa de navegador/pesquisa: {len(taskish)}")
    print(f"  o portão marcaria como LOCAL: {len(flagged)} "
          f"({100 * len(flagged) / len(messages):.2f}% do corpus)")
    print(f"  dessas, dentro da fatia de navegador: {len(flagged_taskish)}")

    if flagged:
        print(f"\n── marcadas como local (revise: alguma NÃO pediu a tela dele?) ──")
        for message in flagged[: args.show]:
            print(f"  • {message[:150]}")
        if len(flagged) > args.show:
            print(f"  … e mais {len(flagged) - args.show}")

    # Recall na bateria explícita (as frases que o usuário de fato usa).
    from tests.browser_local.phrases import NAO_PEDIU, PEDIU

    hits = sum(1 for phrase in PEDIU if local_browser_requested(phrase))
    misses = [phrase for phrase in NAO_PEDIU if local_browser_requested(phrase)]
    recall = 100 * hits / len(PEDIU)
    print(f"\nrecall nas frases explícitas: {hits}/{len(PEDIU)} ({recall:.0f}%)")
    print(f"falsos positivos na bateria de negativos: {len(misses)}")
    for phrase in misses:
        print(f"  ✗ {phrase}")

    ok = recall >= 90 and not misses
    print("\n" + ("✅ dentro do critério do plano" if ok else "❌ fora do critério — ajuste o gate.py"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
