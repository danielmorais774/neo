#!/usr/bin/env python3
"""Análise rápida de CSV — stdlib only (roda no sandbox do Neo sem pip).

Uso: analise_csv.py <caminho-ou-url> [--sep X] [--top N] [--max-rows N]

Imprime linhas/colunas e, por coluna: tipo inferido, % nulos, min/média/max
(numéricas) ou top-N valores (categóricas). Saída pensada para o modelo resumir,
não para o usuário final ler crua.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from collections import Counter

MAX_FIELD_PREVIEW = 40


def _load(source: str, max_bytes: int = 100 * 1024 * 1024) -> str:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=60) as resp:  # noqa: S310 — proxy do sandbox decide o que sai
            return resp.read(max_bytes).decode("utf-8", errors="replace")
    with open(source, "rb") as f:
        return f.read(max_bytes).decode("utf-8", errors="replace")


def _sniff_sep(sample: str, fallback: str = ",") -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return fallback


def _is_float(value: str) -> bool:
    try:
        float(value.replace(",", ".", 1) if value.count(",") == 1 and "." not in value else value)
        return True
    except ValueError:
        return False


def _to_float(value: str) -> float:
    return float(value.replace(",", ".", 1) if value.count(",") == 1 and "." not in value else value)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--sep", default=None)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--max-rows", type=int, default=200_000)
    args = ap.parse_args()

    raw = _load(args.source)
    sep = args.sep or _sniff_sep(raw[:8192])
    reader = csv.reader(io.StringIO(raw), delimiter=sep)
    try:
        header = next(reader)
    except StopIteration:
        print("CSV vazio.")
        return 1

    columns: dict[str, list[str]] = {name: [] for name in header}
    total = 0
    sampled = False
    for row in reader:
        if total >= args.max_rows:
            sampled = True
            break
        total += 1
        for name, value in zip(header, row):
            columns[name].append(value.strip())

    print(f"Linhas: {total}{' (AMOSTRA — arquivo maior)' if sampled else ''} | Colunas: {len(header)} | Separador: {sep!r}")
    for name, values in columns.items():
        non_null = [v for v in values if v not in ("", "NA", "N/A", "null", "None", "nan")]
        nulls_pct = 100.0 * (len(values) - len(non_null)) / max(len(values), 1)
        numeric = non_null and all(_is_float(v) for v in non_null[:1000])
        line = f"- {name!r}: "
        if not non_null:
            line += f"vazia (100% nulos)"
        elif numeric:
            nums = [_to_float(v) for v in non_null]
            line += (
                f"numérica | nulos {nulls_pct:.0f}% | min {min(nums):.4g} | "
                f"média {sum(nums) / len(nums):.4g} | max {max(nums):.4g}"
            )
        else:
            top = Counter(non_null).most_common(args.top)
            tops = ", ".join(f"{v[:MAX_FIELD_PREVIEW]!r}×{c}" for v, c in top)
            line += f"categórica | nulos {nulls_pct:.0f}% | únicos {len(set(non_null))} | top: {tops}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
