"""Leitura delegada de documentos (``deep_read``) — docs/deep-read-plan.md.

Puro como ``core/research``: o pipeline recebe sandbox, LLM e progresso por
protocolo. Quem monta as dependências é ``core/jobs/runner_deep_read.py``.
"""

from core.documents.agent import DeepReader, DeepReadReport, doc_base_names
from core.documents.render import render_result_summary
from core.documents.types import DocState, Finding, ReadSpec

__all__ = [
    "DeepReader",
    "DeepReadReport",
    "DocState",
    "Finding",
    "ReadSpec",
    "doc_base_names",
    "render_result_summary",
]
