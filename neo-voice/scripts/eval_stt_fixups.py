#!/usr/bin/env python3
"""Mede o corretor de ASR (service/stt_text.py) contra o corpus REAL de producao.

Corpus (Postgres da VPS, ver neo-core/docs/voice-input-normalization-plan.md §7):

  # turnos de VOZ (recall: quantos vocativos ele conserta)
  psql -tAc "\\copy (SELECT content FROM conversation_message
       WHERE role='user' AND msg_metadata->>'mode'='live' ORDER BY created_at)
       TO STDOUT CSV" > voice.csv

  # turnos DIGITADOS (precisao: o corretor nao pode mudar NADA aqui)
  psql -tAc "\\copy (SELECT content FROM conversation_message
       WHERE role='user' AND coalesce(msg_metadata->>'mode','') <> 'live'
       ORDER BY created_at) TO STDOUT CSV" > typed.csv

Uso:
  python3 eval_stt_fixups.py voice.csv                 # relatorio de recall
  python3 eval_stt_fixups.py --precision typed.csv     # falha se mudar algo
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service"))

from stt_text import normalize_stt_text  # noqa: E402


def load(path: str) -> list[str]:
    with open(path, newline="", encoding="utf-8") as f:
        return [row[0] for row in csv.reader(f) if row and row[0].strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--precision", action="store_true",
                    help="corpus digitado: QUALQUER alteracao e falso positivo (exit 1)")
    ap.add_argument("--show", type=int, default=40, help="quantos diffs imprimir")
    args = ap.parse_args()

    rows = load(args.csv_path)
    rules = Counter()
    changed, dropped = [], []
    for text in rows:
        res = normalize_stt_text(text)
        if res.dropped:
            dropped.append(text)
            continue
        if res.fixes:
            changed.append((text, res.text, res.fixes))
            rules.update(f["rule"] for f in res.fixes)

    print(f"corpus: {len(rows)} mensagens ({args.csv_path})")
    print(f"corrigidas: {len(changed)}  ({100*len(changed)/max(1,len(rows)):.1f}%)")
    print(f"descartadas (eco/grunhido): {len(dropped)}  ({100*len(dropped)/max(1,len(rows)):.1f}%)")
    for rule, n in rules.most_common():
        print(f"  {rule:18s} {n}")

    if changed:
        print("\n--- correcoes ---")
        for raw, new, _ in changed[:args.show]:
            print(f"  - {raw}\n  + {new}")
    if dropped:
        print("\n--- descartes ---")
        for raw in dropped[:args.show]:
            print(f"  x {raw!r}")

    if args.precision:
        bad = len(changed) + len(dropped)
        print(f"\nFALSOS POSITIVOS no corpus digitado: {bad} (criterio de aceite: 0)")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
