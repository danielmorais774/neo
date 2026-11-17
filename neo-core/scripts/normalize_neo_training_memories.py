#!/usr/bin/env python
"""Phase 9 — Normalize legacy Neo training memories.

Scans the Neo system workspace for training rules that were saved *before* the
Memory Pipeline v2 and:
  - moves raw/envelope text into ``source.raw_message``;
  - regenerates a clean ``description`` (envelope removed);
  - generates a canonical ``description`` + semantic ``rule_key`` when possible;
  - removes duplicates by ``semantic_rule_key``;
  - flags Neo behavior that was incorrectly saved as a personal user preference.

Usage:
    python scripts/normalize_neo_training_memories.py --dry-run   # default, no writes
    python scripts/normalize_neo_training_memories.py --apply      # persist changes

In ``--dry-run`` the script never mutates anything; it only prints a report.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from typing import Any

# Allow running as a standalone script (python scripts/normalize_...py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from core.pks.memory_canonicalizer import MemoryCanonicalizer
from core.pks.memory_plan_validator import _HASH_KEY_SUFFIX
from core.pks.repository import PKSRepository
from core.pks.schemas import UpsertFieldInput
from core.pks.training_envelope import TrainingEnvelopeParser, normalize_text
from db.async_client import AsyncPostgresClient
from shared.config import get_settings

# Entity types whose `properties` field holds training lists.
_TRAINING_ENTITY_TYPES = (
    "identity",
    "preference",
    "document",
    "correction_example",
    "learned_lesson",
)

# List-valued property keys that may hold training items.
_LIST_KEYS = ("rules", "procedures", "examples", "lessons", "training_rules")

_ENVELOPE_FRAGMENTS = (
    "modo treino",
    "nao e uma preferencia pessoal",
    "salve no workspace do neo",
    "salve este aprendizado",
    "esta mensagem e um treinamento",
    "como voce deve se comportar como neo",
)

_parser = TrainingEnvelopeParser()
_canonicalizer = MemoryCanonicalizer()


def _is_hash_like(rule_key: str | None) -> bool:
    if not rule_key:
        return True
    return bool(_HASH_KEY_SUFFIX.search(rule_key)) or not re.search(r"[a-z]", rule_key)


def _has_envelope(description: str | None) -> bool:
    normalized = normalize_text(description or "")
    return any(fragment in normalized for fragment in _ENVELOPE_FRAGMENTS)


def _needs_normalization(item: dict[str, Any]) -> bool:
    description = item.get("description") or item.get("rule_description") or ""
    return _has_envelope(description) or _is_hash_like(item.get("rule_key"))


def _canonicalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized copy of a training item."""
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    raw = source.get("raw_message") or item.get("description") or item.get("rule_description") or ""
    envelope = _parser.parse(raw)
    clean = envelope.clean_content or raw

    canonical_description = clean.strip()[:240]
    semantic_rule_key = item.get("rule_key") or ""

    ops = _canonicalizer.canonicalize(
        clean_content=clean,
        source_message_id=(source.get("source_message_id") or item.get("source_message_id")),
        user_id=None,
        source_audit=envelope.source_audit or {"raw_message": raw, "clean_content": clean},
    )
    if ops:
        op = ops[0]
        canonical_description = op["description"]
        semantic_rule_key = op["semantic_rule_key"]
    elif _is_hash_like(semantic_rule_key):
        slug = "_".join(normalize_text(clean).split()[:6]) or "training_rule"
        semantic_rule_key = f"custom_{slug}"[:80]

    new_item = dict(item)
    new_item["description"] = canonical_description
    new_item["rule_key"] = semantic_rule_key
    new_item["semantic_rule_key"] = semantic_rule_key
    new_item["source"] = {
        **source,
        "raw_message": raw,
        "clean_content": clean,
        "removed_envelope_phrases": envelope.envelope_phrases,
    }
    return new_item


def _normalize_list(items: list[Any]) -> tuple[list[Any], list[str]]:
    """Normalize a list of training items, dedup by semantic_rule_key."""
    changes: list[str] = []
    by_key: dict[str, Any] = {}
    anonymous: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            anonymous.append(item)
            continue
        if _needs_normalization(item):
            normalized = _canonicalize_item(item)
            changes.append(
                f"      rule_key {item.get('rule_key')!r} -> {normalized['rule_key']!r}; "
                f"description -> {normalized['description'][:60]!r}"
            )
            item = normalized
        key = item.get("semantic_rule_key") or item.get("rule_key")
        if key:
            if key in by_key:
                changes.append(f"      deduplicated rule_key {key!r}")
            by_key[key] = item
        else:
            anonymous.append(item)
    return [*anonymous, *by_key.values()], changes


async def _scan_entities(
    db: AsyncPostgresClient,
    repository: PKSRepository,
    *,
    apply: bool,
) -> int:
    total_changes = 0
    async with db.get_session() as session:
        workspace = await repository.get_system_workspace(session)
        if not workspace:
            print("No Neo system workspace found — nothing to normalize.")
            return 0
        workspace_id = workspace["id"]
        print(f"System workspace: {workspace_id}\n")

        seen: set = set()
        for entity_type in _TRAINING_ENTITY_TYPES:
            entities = await repository.list_entities_by_type(
                session, workspace_id, entity_type, limit=1000
            )
            for entity in entities:
                if entity.id in seen:
                    continue
                seen.add(entity.id)
                field = await repository.get_field(session, entity.id, "properties")
                if not field or not isinstance(field.value, dict):
                    continue
                properties = field.value
                entity_changes: list[str] = []
                new_properties = dict(properties)
                for key in _LIST_KEYS:
                    value = properties.get(key)
                    if not isinstance(value, list):
                        continue
                    normalized_list, changes = _normalize_list(value)
                    if changes:
                        entity_changes.append(f"    [{key}]")
                        entity_changes.extend(changes)
                        new_properties[key] = normalized_list
                if entity_changes:
                    total_changes += len(entity_changes)
                    print(f"  Entity {entity.name} [{entity.type}] ({entity.id}):")
                    print("\n".join(entity_changes))
                    if apply:
                        await repository.upsert_field(
                            session,
                            UpsertFieldInput(
                                entity_id=entity.id,
                                key="properties",
                                value_type="json",
                                value=new_properties,
                                confidence=0.97,
                                sensitivity="private",
                                source="normalization",
                                actor="system",
                                metadata={"structured_properties": True, "normalized": True},
                            ),
                        )
                    print()
        if apply:
            await session.commit()
    return total_changes


async def _scan_user_preferences(db: AsyncPostgresClient, *, apply: bool) -> int:
    """Identify Neo behavior incorrectly saved as personal user preferences.

    Resilient to schema drift: only selects columns that actually exist.
    """
    flagged = 0
    async with db.get_session() as session:
        try:
            cols_result = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'user_preference'"
                )
            )
            available = {row[0] for row in cols_result.all()}
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"Could not inspect user_preference table: {exc}\n")
            return 0
        if not available:
            print("No user_preference table found.\n")
            return 0
        wanted = [
            col
            for col in ("id", "user_id", "service", "category", "key", "value")
            if col in available
        ]
        result = await session.execute(
            text(f"SELECT {', '.join(wanted)} FROM user_preference")
        )
        rows = result.mappings().all()

    print("User preferences that look like Neo training (should not be personal):")
    for row in rows:
        value = row.get("value") or ""
        if _parser.parse(value).is_training or _has_envelope(value):
            flagged += 1
            print(
                f"  [{row.get('category')}] {row.get('key')} = {str(value)[:80]!r} "
                f"(user={row.get('user_id')}, service={row.get('service')})"
            )
            if apply and row.get("category") and row.get("key"):
                await db.delete_preference(
                    category=row["category"],
                    key=row["key"],
                    user_id=row.get("user_id"),
                    service=row.get("service") or "neo-desktop",
                )
                print("    -> deleted")
    if not flagged:
        print("  (none found)")
    print()
    return flagged


async def _run(apply: bool) -> int:
    settings = get_settings()
    db = AsyncPostgresClient(settings.database_url)
    repository = PKSRepository(db.session_factory)
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"=== normalize_neo_training_memories ({mode}) ===\n")
    try:
        entity_changes = await _scan_entities(db, repository, apply=apply)
        pref_flags = await _scan_user_preferences(db, apply=apply)
    finally:
        await db.dispose()
    print("=== Summary ===")
    print(f"  Entity training changes: {entity_changes}")
    print(f"  User preferences flagged: {pref_flags}")
    if not apply and (entity_changes or pref_flags):
        print("\nRun again with --apply to persist these changes.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Report only (default).")
    group.add_argument("--apply", action="store_true", help="Persist normalization changes.")
    args = parser.parse_args()
    return asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
