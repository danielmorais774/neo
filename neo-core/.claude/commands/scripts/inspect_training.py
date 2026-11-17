#!/usr/bin/env python
"""Health-check das entidades de treino do Neo (read-only).

Invocado pelo comando /inspect-training via:
    DOCKER_HOST=ssh://neo-vagrant docker exec neo-core python /app/.claude/commands/scripts/inspect_training.py [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

sys.path.insert(0, "/app")

from core.pks.memory_plan_validator import _HASH_KEY_SUFFIX
from core.pks.repository import PKSRepository
from core.pks.training_envelope import normalize_text
from db.async_client import AsyncPostgresClient
from shared.config import get_settings

_ENVELOPE_FRAGMENTS = (
    "modo treino",
    "nao e uma preferencia pessoal",
    "salve no workspace do neo",
    "salve este aprendizado",
    "esta mensagem e um treinamento",
    "como voce deve se comportar como neo",
)
_ACID_TRAITS = {"sarcastic_when_appropriate", "anti_corporate_tone",
                "low_patience_for_bad_ideas"}
_TARGETS = [
    ("neo-personality",             "identity",          ("rules", "training_rules")),
    ("neo-response-rules",          "preference",        ("rules",)),
    ("neo-operating-procedures",    "document",          ("procedures",)),
    ("neo-training-examples",       "correction_example",("examples",)),
    ("neo-communication-preferences","preference",       ("examples",)),
]


def _item_issues(item: dict) -> list[str]:
    issues: list[str] = []
    desc   = str(item.get("description") or item.get("rule_description") or "")
    key    = str(item.get("rule_key") or "")
    raw    = ""
    src = item.get("source")
    if isinstance(src, dict):
        raw = str(src.get("raw_message") or "")
    ndesc = normalize_text(desc)
    for frag in _ENVELOPE_FRAGMENTS:
        if frag in ndesc:
            issues.append(f"envelope_in_description ({frag!r})"); break
    if key:
        if _HASH_KEY_SUFFIX.search(key) or not re.search(r"[a-z]", key):
            issues.append(f"hash_like_rule_key ({key!r})")
    else:
        issues.append("missing_rule_key")
    if raw and ndesc and ndesc == normalize_text(raw):
        issues.append("raw_copied_as_description")
    return issues


def _persona_issues(props: dict) -> list[str]:
    traits = set(str(t) for t in (props.get("traits") or []) if isinstance(props.get("traits"), list))
    if "slightly_funny" in traits and traits & _ACID_TRAITS:
        return ["persona_slightly_funny misturado com traits ácidos (regressão)"]
    return []


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report: dict = {"workspace": None, "entities": [], "summary": {}}
    total_items = total_issues = 0
    missing: list[str] = []

    db = AsyncPostgresClient(get_settings().database_url)
    repo = PKSRepository(db.session_factory)
    try:
        async with db.get_session() as session:
            ws = await repo.get_system_workspace(session)
            if not ws:
                msg = "workspace neo (system) não encontrado"
                print(json.dumps({"error": msg}) if args.json else msg, file=sys.stderr)
                sys.exit(2)
            report["workspace"] = str(ws["id"])
            if not args.json:
                print(f"neo workspace: {ws['id']}\n")

            for name, etype, list_keys in _TARGETS:
                ent = await repo.find_entity_by_name(session, ws["id"], name, etype)
                entry: dict = {"name": name, "type": etype, "items": [], "issues": []}
                if ent is None:
                    missing.append(name)
                    entry["missing"] = True
                    report["entities"].append(entry)
                    if not args.json:
                        print(f"- {name} [{etype}]  ⚠ AUSENTE\n")
                    continue
                field = await repo.get_field(session, ent.id, "properties")
                props = field.value if field and isinstance(field.value, dict) else {}
                if not args.json:
                    print(f"- {name} [{etype}]")

                if name == "neo-personality":
                    pi = _persona_issues(props)
                    entry["issues"].extend(pi); total_issues += len(pi)
                    scalars = {k: v for k, v in props.items() if not isinstance(v, (list, dict))}
                    traits = props.get("traits") or []
                    entry["persona"] = scalars; entry["traits"] = traits
                    if not args.json:
                        print(f"    persona: tone={scalars.get('tone')!r} "
                              f"humor={scalars.get('humor')!r} "
                              f"sarcasm={scalars.get('sarcasm_level')} "
                              f"traits[{len(traits)}]={traits}")
                        for p in pi:
                            print(f"      ⚠ {p}")

                for lk in list_keys:
                    items = props.get(lk) or []
                    for item in items:
                        total_items += 1
                        if not isinstance(item, dict):
                            entry["items"].append({"list_key": lk, "anon": str(item)[:120]})
                            if not args.json:
                                print(f"    [{lk}] (anon) {str(item)[:120]!r}")
                            continue
                        issues = _item_issues(item)
                        total_issues += len(issues)
                        entry["items"].append({
                            "list_key": lk,
                            "rule_key": item.get("rule_key"),
                            "priority": item.get("priority"),
                            "description": (item.get("description") or "")[:120],
                            "issues": issues,
                        })
                        if not args.json:
                            tag = "  ❌" if issues else ""
                            print(f"    [{lk}] {item.get('rule_key')!r} "
                                  f"({item.get('priority')!r}){tag}")
                            print(f"        {(item.get('description') or '')[:120]!r}")
                            for iss in issues:
                                print(f"        ⚠ {iss}")
                if not args.json:
                    print()
                report["entities"].append(entry)

            report["summary"] = {"items": total_items, "issues": total_issues,
                                  "missing": missing}
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                status = "✅ tudo limpo" if total_issues == 0 and not missing else "❌ há problemas"
                print(f"=== {status} — items={total_items} issues={total_issues}", end="")
                if missing:
                    print(f" ausentes={missing}", end="")
                print(" ===")
            sys.exit(0 if total_issues == 0 and not missing else 1)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
