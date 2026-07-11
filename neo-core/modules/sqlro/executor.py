"""Executor da sql_query_ro — SELECT-only, com defesa em camadas.

Camadas (da mais fraca à mais forte):
1. Validação sintática (:func:`validate_ro_query`): um único statement, começa
   com SELECT/WITH, sem verbo de escrita fora de string literal.
2. Sessão Postgres com ``default_transaction_read_only=on`` — mesmo o que passar
   pela validação NÃO escreve (o servidor rejeita).
3. ``statement_timeout`` curto + cap de linhas/bytes na saída.

DSN: ``sqlro_dsn`` quando setado (ideal: role dedicada só-leitura); senão cai no
``database_url`` do app — ainda seguro por causa da camada 2.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from shared.config import Settings, get_settings

logger = logging.getLogger(__name__)

_ALLOWED_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# Verbos que nunca aparecem num read legítimo. Checados com as string literals
# removidas ('...like ''%delete%''...' não dispara). "update" também pega
# SELECT ... FOR UPDATE (lock — igualmente indesejado aqui).
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|truncate|grant|revoke|copy|call|do|"
    r"create|vacuum|listen|notify|refresh|reindex|cluster|lock|prepare|execute|"
    r"set|reset|"
    # Funções chamáveis de dentro de um SELECT que mexem em estado/arquivos/rede
    # (o \bset\b não pega set_config: underscore é word char).
    r"set_config|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|"
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|lo_import|lo_export|dblink\w*)\b",
    re.IGNORECASE,
)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HAS_LIMIT = re.compile(r"\blimit\s+\d+", re.IGNORECASE)

_MAX_OUTPUT_CHARS = 8_000
_MAX_CELL_CHARS = 200


def validate_ro_query(query: str) -> str:
    """Valida e normaliza a query. Levanta ``ValueError`` com motivo claro."""
    q = (query or "").strip()
    if not q:
        raise ValueError("Query vazia.")
    q = q.rstrip(";").strip()
    if ";" in q:
        raise ValueError("Apenas UM statement por chamada (encontrei ';' no meio).")
    if not _ALLOWED_START.match(q):
        raise ValueError("Só SELECT/WITH são permitidos (leitura pura).")
    # Comentários podem esconder verbos da validação — remove antes de checar
    # e não os devolve (a query executada é a query auditada).
    stripped = _LINE_COMMENT.sub(" ", q)
    stripped = _BLOCK_COMMENT.sub(" ", stripped)
    if "/*" in stripped:
        raise ValueError("Comentário de bloco não terminado.")
    no_strings = _STRING_LITERAL.sub("''", stripped)
    hit = _FORBIDDEN.search(no_strings)
    if hit:
        raise ValueError(f"Verbo não permitido em query de leitura: {hit.group(0).upper()}.")
    return stripped.strip()


class SqlRoExecutor:
    """Executa sql_query_ro com conexão por chamada (volume baixo, sem pool)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _dsn(self) -> str:
        dsn = (getattr(self.settings, "sqlro_dsn", "") or "").strip()
        if not dsn:
            # database_url é formato SQLAlchemy (postgresql+asyncpg://) — asyncpg
            # puro quer postgresql://.
            dsn = self.settings.database_url.replace("+asyncpg", "")
        return dsn

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name != "sql_query_ro":
            raise ValueError(f"Tool desconhecida no módulo sqlro: {tool_name}")

        query = validate_ro_query(str(arguments.get("query") or ""))
        max_rows = int(getattr(self.settings, "sqlro_max_rows", 200))
        limit = min(int(arguments.get("limit") or 50), max_rows)

        import asyncpg  # import local: módulo só paga a dep quando usado

        timeout_ms = int(getattr(self.settings, "sqlro_statement_timeout_ms", 5_000))
        conn = await asyncpg.connect(
            dsn=self._dsn(),
            timeout=10,
            server_settings={
                # Camada 2: o servidor rejeita QUALQUER escrita nesta sessão.
                "default_transaction_read_only": "on",
                "statement_timeout": str(timeout_ms),
                "application_name": "neo-sqlro",
            },
        )
        try:
            rows = await conn.fetch(query)
        finally:
            await conn.close()

        truncated_rows = len(rows) > limit
        rows = rows[:limit]
        logger.info(
            "[sqlro] query ok — %d linha(s)%s", len(rows), " (truncado)" if truncated_rows else ""
        )
        return _format_rows(rows, truncated=truncated_rows)


def _format_rows(rows: list, *, truncated: bool) -> str:
    """Tabela texto compacta (legível pelo modelo), capada em bytes."""
    if not rows:
        return "(0 linhas)"
    columns = list(rows[0].keys())
    lines = [" | ".join(columns)]
    for row in rows:
        cells = []
        for col in columns:
            value = row[col]
            text = "NULL" if value is None else str(value)
            if len(text) > _MAX_CELL_CHARS:
                text = text[:_MAX_CELL_CHARS] + "…"
            cells.append(text)
        lines.append(" | ".join(cells))
    out = f"({len(rows)} linha(s){' — resultado truncado no limit' if truncated else ''})\n"
    out += "\n".join(lines)
    if len(out) > _MAX_OUTPUT_CHARS:
        out = out[:_MAX_OUTPUT_CHARS] + "\n… [saída truncada — refine a query/limit]"
    return out
