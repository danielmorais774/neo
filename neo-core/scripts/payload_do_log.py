#!/usr/bin/env python3
"""Remonta, do diário da sessão, o payload que uma rodada mandou ao modelo.

O diário não grava mais o bloco "⚡ RAW payload": cada mensagem vai inteira só
na 1ª vez que aparece, e depois vira referência ``= #sha`` ou diff
(``observability/session_log_payload.py``). Este script faz o caminho de
volta e imprime o mesmo JSON que o bloco RAW mostrava. É a entrada do A/B que
reenvia o request real ao modelo. Lê também os diários antigos.

Roda dentro do container, lendo o diário no lugar (nenhuma cópia da conversa
sai do servidor):

    ssh contabo-neo 'docker exec -w /app neo-core python scripts/payload_do_log.py \\
        /app/logs/session-<id>.md --listar'
    ssh contabo-neo 'docker exec -w /app neo-core python scripts/payload_do_log.py \\
        /app/logs/session-<id>.md --interacao 1 --rodada 13' > payload.json

Sem ``--rodada``, imprime a última rodada da interação (ou do arquivo).
``--conferir`` compara cada rodada remontada com o bloco RAW dos diários antigos
e sai com erro se alguma não bater byte a byte.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observability.session_log_payload import (  # noqa: E402
    DiarioInvalido,
    ler_rodadas,
    payload_texto,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("diario", help="caminho do session-<id>.md")
    ap.add_argument("--interacao", type=int, help="número da '## Interação N'")
    ap.add_argument("--rodada", type=int, help="N-ésima chamada de LLM dentro da interação")
    ap.add_argument("--listar", action="store_true", help="lista as rodadas e sai")
    ap.add_argument("--conferir", action="store_true", help="confere contra o RAW antigo")
    args = ap.parse_args()

    # newline="" preserva \r\n dentro das mensagens: o tamanho e o sha contam com ele.
    with open(args.diario, encoding="utf-8", newline="") as f:
        texto = f.read()
    try:
        rodadas = ler_rodadas(texto)
    except DiarioInvalido as e:
        print(f"diário não remonta: {e}", file=sys.stderr)
        return 2

    if args.listar:
        for r in rodadas:
            print(
                f"Interação {r.interacao:>3}  rodada {r.numero:>3}  {r.estagio:<12} "
                f"{r.model:<40} {len(r.mensagens):>3} mensagens  {len(r.tools):>3} tools"
            )
        return 0

    if args.conferir:
        com_raw = [r for r in rodadas if r.raw_antigo is not None]
        ruins = [r for r in com_raw if payload_texto(r) != r.raw_antigo]
        for r in ruins:
            print(f"DIFERENTE: Interação {r.interacao}, rodada {r.numero}", file=sys.stderr)
        print(f"{len(com_raw) - len(ruins)}/{len(com_raw)} rodadas batem com o RAW antigo")
        return 1 if ruins else 0

    escolhidas = [r for r in rodadas if args.interacao is None or r.interacao == args.interacao]
    if args.rodada is not None:
        escolhidas = [r for r in escolhidas if r.numero == args.rodada]
    if not escolhidas:
        print("nenhuma rodada com esse filtro (use --listar)", file=sys.stderr)
        return 1
    print(payload_texto(escolhidas[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
