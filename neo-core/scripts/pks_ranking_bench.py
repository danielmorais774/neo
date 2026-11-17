"""Benchmark do ranking do PKS — docs/pks-ranking-plan.md §6.

Roda o bloco ATUAL (retriever antigo + as regras do seletor) e o v2
(core/pks/ranking.py) sobre o gabarito, contra o banco real, SÓ LEITURA, sem LLM.

    docker exec -w /app neo-core python scripts/pks_ranking_bench.py
    docker exec -w /app neo-core python scripts/pks_ranking_bench.py --search 300

Métricas (o aceite está no plano):
  recall_must   fração das entidades "must" que chegaram ao bloco FINAL
  completude    "must" que é contêiner e entrou: TODOS os membros estão no bloco
  ruido         % dos tokens do bloco gastos com entidades fora de must/useful
  negativos     casos negativos com bloco vazio
  ndcg@10       ordem do ranking antes do empacotamento
  ms            latência por caso
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import re
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from core.pks.ranking import DEFAULT_WEIGHTS, RankConfig, build_workspace_index, rank_pks  # noqa: E402
from core.pks.retriever import _block_entities, _count_tokens  # noqa: E402
from shared.config import get_settings  # noqa: E402

OWNER = UUID("5a998544-c3f3-423f-bebb-1cc42fca6063")
GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "pks_ranking" / "golden.yaml"
WRAP = "## Conhecimento do Usuário\n"
_CONTAINER_FROM = {"has_member", "has_exercise", "contains", "includes"}
_CONTAINER_TO = {"part_of", "belongs_to", "member_of"}


def _sections(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in re.split(r"(?m)^(?=### )", block or ""):
        names = _block_entities(part)
        if names:
            out[names[0]] = part
    return out


def _members(relations, by_id) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    for r in relations:
        if r.from_entity_id not in by_id or r.to_entity_id not in by_id:
            continue
        if r.relation_type in _CONTAINER_FROM:
            members.setdefault(by_id[r.from_entity_id].name, set()).add(by_id[r.to_entity_id].name)
        elif r.relation_type in _CONTAINER_TO:
            members.setdefault(by_id[r.to_entity_id].name, set()).add(by_id[r.from_entity_id].name)
    return members


def _ndcg(ranked: list[str], must: set[str], useful: set[str], k: int = 10) -> float:
    def gain(n: str) -> int:
        return 2 if n in must else 1 if n in useful else 0

    dcg = sum((2 ** gain(n) - 1) / math.log2(i + 2) for i, n in enumerate(ranked[:k]))
    ideal = sorted([2] * len(must) + [1] * len(useful), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 1.0


def score_case(case, block: str, ranked_names: list[str], members) -> dict:
    must, useful = set(case["must"]), set(case["useful"])
    secs = _sections(block)
    total = _count_tokens(block) if block else 0
    noise = sum(_count_tokens(t) for n, t in secs.items() if n not in must | useful)
    present = {n for n in must if n in block}
    complete = [
        all(m in block for m in members.get(n, ()))
        for n in must if n in secs and members.get(n)
    ]
    return {
        "recall": (len(present) / len(must)) if must else None,
        "missing": sorted(must - present),
        "complete": (sum(complete) / len(complete)) if complete else None,
        "noise_tokens": noise,
        "tokens": total,
        "empty": not block.strip(),
        "ndcg": _ndcg(ranked_names, must, useful) if must else None,
    }


def summarize(rows: list[dict], cases) -> dict:
    pos = [r for r, c in zip(rows, cases) if c["must"]]
    neg = [r for r, c in zip(rows, cases) if c["tema"] == "negativo"]
    comp = [r["complete"] for r in pos if r["complete"] is not None]
    tok = sum(r["tokens"] for r in rows)
    return {
        "recall_must": round(statistics.mean(r["recall"] for r in pos), 3) if pos else None,
        "casos_completos": f"{sum(1 for r in pos if r['recall'] == 1.0)}/{len(pos)}",
        "completude": round(statistics.mean(comp), 3) if comp else None,
        "ruido": round(sum(r["noise_tokens"] for r in rows) / tok, 3) if tok else 0.0,
        "negativos_ok": f"{sum(1 for r in neg if r['empty'])}/{len(neg)}",
        "negativos_ok_frac": (sum(1 for r in neg if r["empty"]) / len(neg)) if neg else 1.0,
        "ndcg10": round(statistics.mean(r["ndcg"] for r in pos), 3) if pos else None,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=str(OWNER))
    ap.add_argument("--search", type=int, default=0, help="amostras de busca aleatória de pesos")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--grid", action="store_true", help="grade em relative_cutoff × min_score (pesos do plano)")
    ap.add_argument("--json", default="", help="grava o resultado completo neste caminho")
    args = ap.parse_args()

    from core.llm.router import LLMRouter
    from core.pks.service import PKSService
    from db.async_client import AsyncPostgresClient

    settings = get_settings()
    if not GOLDEN.exists():
        # O gabarito é feito de conversas reais e não vai para o repositório.
        sys.exit(f"gabarito não encontrado em {GOLDEN} (fica só na máquina do dono: conversas reais)")
    cases = yaml.safe_load(GOLDEN.read_text())["cases"]
    db = AsyncPostgresClient()
    svc = PKSService(db, LLMRouter())
    retriever = svc.retriever

    # SÓ LEITURA: o caminho antigo grava last_used_at a cada busca.
    async def _no_touch(*_a, **_k):
        return None

    svc.repository.touch_entities = _no_touch  # type: ignore[method-assign]
    retriever.repository.touch_entities = _no_touch  # type: ignore[method-assign]

    user = UUID(args.user)
    ws = await svc.bootstrap.ensure_workspace(user)
    async with db.get_session() as s:
        entities, fields, relations = await svc.repository.load_workspace_graph(s, ws)
    by_id = {e.id: e for e in entities}
    names = {e.name for e in entities}
    members = _members(relations, by_id)

    faltando = sorted({n for c in cases for n in c["must"] + c["useful"] if n not in names})
    if faltando:
        print("⚠️ nomes do gabarito que NÃO existem no PKS:", faltando)

    # ── atual ──
    old_rows, old_ms = [], []
    for c in cases:
        t0 = time.perf_counter()
        res, text = await retriever._retrieve_legacy(user_id=user, user_message=c["message"], keywords=c["keywords"])
        old_ms.append((time.perf_counter() - t0) * 1000)
        best = max((cd.score for cd in res.candidates), default=0.0)
        wrapped = _count_tokens(WRAP + text) if text else 0
        # As duas regras do seletor que decidem se o bloco entra: limiar 0,25 e
        # orçamento tudo-ou-nada.
        entra = bool(text) and best >= settings.context_relevance_threshold and wrapped <= settings.context_budget_user_pks
        final = text if entra else ""
        old_rows.append(score_case(c, final, [cd.entity.name for cd in res.candidates], members))

    # ── v2 ──
    base = retriever.rank_config()

    # Em produção o índice fica em cache entre turnos (retriever._workspace_graph);
    # a latência medida aqui é a do turno com cache quente.
    t0 = time.perf_counter()
    windex = build_workspace_index(entities, fields, relations)
    print(f"índice do workspace: {(time.perf_counter() - t0) * 1000:.0f} ms (uma vez, cacheado)")

    def run_v2(cfg: RankConfig) -> tuple[list[dict], list[float]]:
        rows, ms = [], []
        for c in cases:
            t0 = time.perf_counter()
            out = rank_pks(
                workspace=windex,
                message=c["message"], keywords=c["keywords"], recent_messages=c["history"],
                config=cfg, count_tokens=_count_tokens,
            )
            ms.append((time.perf_counter() - t0) * 1000)
            ranked = [r.entity.name for r in out.ranked if r.eligible]
            rows.append(score_case(c, out.text, ranked, members))
        return rows, ms

    new_rows, new_ms = run_v2(base)

    def p95(xs):
        return round(sorted(xs)[int(0.95 * (len(xs) - 1))], 1)

    s_old, s_new = summarize(old_rows, cases), summarize(new_rows, cases)
    s_old["ms_p95"], s_new["ms_p95"] = p95(old_ms), p95(new_ms)
    print("\n| métrica | atual | v2 |\n|---|---:|---:|")
    for k in ("recall_must", "casos_completos", "completude", "ruido", "negativos_ok", "ndcg10", "ms_p95"):
        print(f"| {k} | {s_old[k]} | {s_new[k]} |")

    print("\n## por caso (recall atual → v2 | tokens atual → v2 | faltando no v2)")
    for c, o, n in zip(cases, old_rows, new_rows):
        if c["must"]:
            flag = "✅" if n["recall"] == 1.0 else "❌"
            print(f"{flag} {c['id']:12} [{c['tema']:11}] {o['recall']:.2f} → {n['recall']:.2f} | "
                  f"{o['tokens']:4} → {n['tokens']:4} | {n['missing'] or ''} — {c['message'][:50]!r}")
        elif c["tema"] == "negativo":
            flag = "✅" if n["empty"] else "❌"
            print(f"{flag} {c['id']:12} [negativo   ] vazio: {o['empty']} → {n['empty']} | "
                  f"{o['tokens']:4} → {n['tokens']:4} — {c['message'][:50]!r}")
        else:
            print(f"·  {c['id']:12} [{c['tema']:11}] só ruído: {o['noise_tokens']} → {n['noise_tokens']} de "
                  f"{o['tokens']} → {n['tokens']} tokens — {c['message'][:50]!r}")

    result = {"atual": s_old, "v2": s_new, "config": {
        "weights": {**DEFAULT_WEIGHTS, **base.weights}, "min_score": base.min_score,
        "relative_cutoff": base.relative_cutoff, "kappa": base.kappa}}

    # ── calibragem: busca aleatória, 70% calibra / 30% valida ──
    if args.search:
        rng = random.Random(args.seed)
        split = [int(hashlib.md5(c["id"].encode()).hexdigest(), 16) % 10 < 7 for c in cases]

        def objetivo(rows) -> float:
            cal = [(r, c) for r, c, s in zip(rows, cases, split) if s]
            pos = [r["recall"] for r, c in cal if c["must"]]
            comp = [r["complete"] for r, c in cal if c["must"] and r["complete"] is not None]
            neg = [r["empty"] for r, c in cal if c["tema"] == "negativo"]
            negfrac = sum(neg) / len(neg) if neg else 1.0
            if negfrac < 0.9:
                return -1 + negfrac
            tok = sum(r["tokens"] for r, _ in cal) or 1
            ruido = sum(r["noise_tokens"] for r, _ in cal) / tok
            return statistics.mean(pos) + 0.3 * (statistics.mean(comp) if comp else 1) - 0.2 * ruido

        best = (objetivo(new_rows), base)
        for _ in range(args.search):
            w = {k: rng.choice([0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4]) for k in ("nome", "lex", "conv", "grafo", "tipo", "prior")}
            w["sem"] = 0.0
            cfg = replace(base, weights=w, min_score=rng.choice([0.08, 0.1, 0.12, 0.15, 0.18, 0.22]),
                          relative_cutoff=rng.choice([0.3, 0.35, 0.4, 0.45, 0.5, 0.55]),
                          kappa=rng.choice([3.0, 4.5, 6.0, 8.0, 10.0]))
            rows, _ = run_v2(cfg)
            val = objetivo(rows)
            if val > best[0]:
                best = (val, cfg)
        cfg = best[1]
        rows, _ = run_v2(cfg)
        cal = summarize([r for r, s in zip(rows, split) if s], [c for c, s in zip(cases, split) if s])
        val = summarize([r for r, s in zip(rows, split) if not s], [c for c, s in zip(cases, split) if not s])
        base_val = summarize([r for r, s in zip(new_rows, split) if not s], [c for c, s in zip(cases, split) if not s])
        print("\n## calibragem (melhor de", args.search, "amostras)")
        print("pesos:", cfg.weights, "| min_score:", cfg.min_score, "| relative_cutoff:", cfg.relative_cutoff, "| kappa:", cfg.kappa)
        print("calibração (70%):", cal)
        print("validação  (30%):", val, "| pesos do plano na validação:", base_val)
        result["calibrado"] = {"weights": cfg.weights, "min_score": cfg.min_score,
                               "relative_cutoff": cfg.relative_cutoff, "kappa": cfg.kappa,
                               "calibracao": cal, "validacao": val, "plano_validacao": base_val}

    if args.grid:
        print("\n## grade: relative_cutoff × min_score (pesos do plano) — recall | completos | ruído | negativos")
        for rc in (0.35, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7):
            linha = []
            for ms in (0.1, 0.12, 0.15, 0.18, 0.22):
                rows, _ = run_v2(replace(base, relative_cutoff=rc, min_score=ms))
                s = summarize(rows, cases)
                linha.append(f"{s['recall_must']:.3f}|{s['casos_completos']}|{s['ruido']:.3f}|{s['negativos_ok']}")
            print(f"rc={rc:.2f}  " + "   ".join(f"ms={m}: {v}" for m, v in zip((0.1, 0.12, 0.15, 0.18, 0.22), linha)))

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
