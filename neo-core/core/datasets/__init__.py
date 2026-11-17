"""Cadastros generativos do Neo (docs/datasets-crud-plan.md).

Um cadastro é uma tabela REAL em ``neo_data``, descrita por um
:class:`~core.datasets.schema.DatasetSchema` guardado em ``neo_data_dataset``.
O mesmo schema alimenta o DDL, a validação de write, o bloco lido pelo cérebro
e a tela genérica do neo-panel.
"""

from core.datasets.ddl import DATA_SCHEMA, build_table_name, new_code
from core.datasets.registry import Dataset, DatasetNotFound, DatasetRegistry
from core.datasets.schema import (
    FIELD_TYPES,
    DatasetSchema,
    FieldDef,
    SchemaError,
    slugify,
    validate_identifier,
)
from core.datasets.service import DatasetService

__all__ = [
    "DATA_SCHEMA",
    "FIELD_TYPES",
    "Dataset",
    "DatasetNotFound",
    "DatasetRegistry",
    "DatasetSchema",
    "DatasetService",
    "FieldDef",
    "SchemaError",
    "build_table_name",
    "new_code",
    "slugify",
    "validate_identifier",
]
