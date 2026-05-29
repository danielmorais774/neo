import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.pks.schemas import EntityRecord, FieldRecord
from core.pks.system_bootstrap import NeoSystemBootstrap, SYSTEM_MEMORY_BLOCKS


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class _FakeSessionContext:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeDB:
    def __init__(self) -> None:
        self.session = _FakeSession()

    def get_session(self) -> _FakeSessionContext:
        return _FakeSessionContext(self.session)


class _FakeRepository:
    def __init__(self) -> None:
        self.workspaces: dict[UUID, dict] = {}
        self.entities: dict[UUID, EntityRecord] = {}
        self.fields: dict[tuple[UUID, str], FieldRecord] = {}
        self.spaces: dict[tuple[UUID, str], UUID] = {}
        self.collections: dict[tuple[UUID, UUID, str], UUID] = {}
        self.workspace_creates = 0
        self.entity_creates = 0
        self.field_creates = 0

    async def get_workspace_by_id(self, session, workspace_id: UUID):
        return self.workspaces.get(workspace_id)

    async def find_workspace_by_name(self, session, *, name: str, type_: str | None = None):
        for workspace in self.workspaces.values():
            if workspace["name"].lower() == name.lower() and (type_ is None or workspace["type"] == type_):
                return workspace
        return None

    async def create_workspace(self, session, *, name: str, type_: str, owner_user_id=None, metadata=None, external_refs=None):
        workspace_id = uuid4()
        workspace = {
            "id": workspace_id,
            "owner_user_id": owner_user_id,
            "name": name,
            "type": type_,
            "metadata": metadata or {},
            "external_refs": external_refs or {},
        }
        self.workspaces[workspace_id] = workspace
        self.workspace_creates += 1
        return workspace

    async def merge_workspace_metadata(self, session, *, workspace_id: UUID, name=None, metadata=None, external_refs=None):
        workspace = self.workspaces[workspace_id]
        if name is not None:
            workspace["name"] = name
        workspace["metadata"] = {**workspace.get("metadata", {}), **(metadata or {})}
        workspace["external_refs"] = {**workspace.get("external_refs", {}), **(external_refs or {})}
        return workspace

    async def ensure_space(self, session, workspace_id: UUID, name: str, type_: str) -> UUID:
        key = (workspace_id, name.lower())
        if key not in self.spaces:
            self.spaces[key] = uuid4()
        return self.spaces[key]

    async def ensure_collection(self, session, workspace_id: UUID, space_id: UUID, name: str, type_: str) -> UUID:
        key = (workspace_id, space_id, name.lower())
        if key not in self.collections:
            self.collections[key] = uuid4()
        return self.collections[key]

    async def upsert_entity(self, session, data):
        for entity in self.entities.values():
            if entity.workspace_id == data.workspace_id and entity.type == data.type and entity.name.lower() == data.name.lower():
                merged_metadata = {**entity.metadata, **data.metadata}
                self.entities[entity.id] = entity.model_copy(update={"metadata": merged_metadata})
                return self.entities[entity.id], entity.model_dump(mode="json")
        entity = EntityRecord(
            id=uuid4(),
            workspace_id=data.workspace_id,
            name=data.name,
            type=data.type,
            aliases=data.aliases,
            search_text=None,
            metadata=data.metadata,
            confidence=data.confidence,
            sensitivity=data.sensitivity,
            last_used_at=None,
        )
        self.entities[entity.id] = entity
        self.entity_creates += 1
        return entity, None

    async def upsert_field(self, session, data):
        key = (data.entity_id, data.key)
        before = self.fields.get(key)
        if before is None:
            self.field_creates += 1
            record = FieldRecord(
                id=uuid4(),
                entity_id=data.entity_id,
                key=data.key,
                value_type=data.value_type,
                value=data.value,
                unit=data.unit,
                confidence=data.confidence,
                sensitivity=data.sensitivity,
            )
        else:
            record = before.model_copy(
                update={
                    "value_type": data.value_type,
                    "value": data.value,
                    "unit": data.unit,
                    "confidence": max(before.confidence, data.confidence),
                    "sensitivity": data.sensitivity,
                }
            )
        self.fields[key] = record
        return record, before.model_dump(mode="json") if before else None

    async def get_field(self, session, entity_id: UUID, key: str):
        return self.fields.get((entity_id, key))


@pytest.mark.asyncio
async def test_system_bootstrap_is_idempotent_and_persists_state(tmp_path: Path) -> None:
    state_file = tmp_path / "pks-system-bootstrap.json"
    bootstrap = NeoSystemBootstrap(
        _FakeDB(),
        _FakeRepository(),
        state_file=str(state_file),
    )

    first = await bootstrap.ensure()
    second = await bootstrap.ensure()

    assert first["workspace_id"] == second["workspace_id"]
    assert first["agent_id"] == second["agent_id"]
    assert first["personality_entity_id"] == second["personality_entity_id"]
    assert first["goals_entity_id"] == second["goals_entity_id"]
    assert first["profile_entity_id"] == second["profile_entity_id"]
    assert first["projects_entity_id"] == second["projects_entity_id"]
    assert first["world_state_entity_id"] == second["world_state_entity_id"]
    assert first["communication_preferences_entity_id"] == second["communication_preferences_entity_id"]
    assert first["response_rules_entity_id"] == second["response_rules_entity_id"]
    assert first["operating_procedures_entity_id"] == second["operating_procedures_entity_id"]
    assert first["training_examples_entity_id"] == second["training_examples_entity_id"]
    assert set(second["memory_block_field_ids"]) == set(SYSTEM_MEMORY_BLOCKS)

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["workspace_id"] == second["workspace_id"]
    assert saved["agent_id"] == second["agent_id"]
    assert saved["personality_entity_id"] == second["personality_entity_id"]
    assert saved["goals_entity_id"] == second["goals_entity_id"]
    assert saved["profile_entity_id"] == second["profile_entity_id"]
    assert saved["projects_entity_id"] == second["projects_entity_id"]
    assert saved["world_state_entity_id"] == second["world_state_entity_id"]
    assert saved["communication_preferences_entity_id"] == second["communication_preferences_entity_id"]
    assert saved["response_rules_entity_id"] == second["response_rules_entity_id"]
    assert saved["operating_procedures_entity_id"] == second["operating_procedures_entity_id"]


@pytest.mark.asyncio
async def test_system_bootstrap_reuses_state_file_ids(tmp_path: Path) -> None:
    repository = _FakeRepository()
    db = _FakeDB()
    state_file = tmp_path / "pks-system-bootstrap.json"
    bootstrap = NeoSystemBootstrap(db, repository, state_file=str(state_file))

    first = await bootstrap.ensure()

    fresh_bootstrap = NeoSystemBootstrap(db, repository, state_file=str(state_file))
    second = await fresh_bootstrap.ensure()

    assert first["workspace_id"] == second["workspace_id"]
    assert first["agent_id"] == second["agent_id"]
    assert repository.workspace_creates == 1
    assert repository.entity_creates == 10
    assert repository.field_creates == len(SYSTEM_MEMORY_BLOCKS) + 9
