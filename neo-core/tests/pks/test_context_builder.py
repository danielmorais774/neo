from datetime import date
from uuid import uuid4

from core.pks.context_builder import ContextBuilder
from core.pks.schemas import EntityRecord, FieldRecord, Neighborhood, RetrievalResult


def test_context_builder_filters_secret_fields():
    workspace_id = uuid4()
    entity_id = uuid4()
    result = RetrievalResult(
        workspace_id=workspace_id,
        keywords=["pc"],
        neighborhoods=[
            Neighborhood(
                entity=EntityRecord(
                    id=entity_id,
                    workspace_id=workspace_id,
                    name="PC principal",
                    type="device",
                ),
                fields=[
                    FieldRecord(
                        id=uuid4(),
                        entity_id=entity_id,
                        key="cpu",
                        value_type="text",
                        value="Ryzen",
                    ),
                    FieldRecord(
                        id=uuid4(),
                        entity_id=entity_id,
                        key="senha",
                        value_type="text",
                        value="123",
                        sensitivity="secret",
                    ),
                ],
            )
        ],
    )

    text = ContextBuilder().format(result, user_message="qual meu pc?")

    assert "cpu: Ryzen" in text
    assert "senha" not in text


def test_context_builder_formats_birthday_without_year():
    workspace_id = uuid4()
    entity_id = uuid4()
    result = RetrievalResult(
        workspace_id=workspace_id,
        keywords=["aniversario"],
        neighborhoods=[
            Neighborhood(
                entity=EntityRecord(
                    id=entity_id,
                    workspace_id=workspace_id,
                    name="Carlos Daniel Morais",
                    type="person",
                ),
                fields=[
                    FieldRecord(
                        id=uuid4(),
                        entity_id=entity_id,
                        key="birthday",
                        value_type="date",
                        value=date(1900, 11, 23),
                    ),
                ],
            )
        ],
    )

    text = ContextBuilder().format(result, user_message="qual meu aniversario?")

    assert "birthday: 23/11" in text
