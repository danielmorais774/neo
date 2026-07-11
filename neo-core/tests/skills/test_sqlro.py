"""validate_ro_query — a camada 1 da defesa do sql_query_ro.

(A camada 2, default_transaction_read_only=on na sessão, é garantida pelo
Postgres e não é testável em unit test.)
"""

import pytest

from modules.sqlro import validate_ro_query


@pytest.mark.parametrize(
    "query",
    [
        "SELECT count(*) FROM conversation_session",
        "  select id, status from mission where status = 'failed' LIMIT 10  ",
        "WITH m AS (SELECT status FROM mission) SELECT status, count(*) FROM m GROUP BY 1",
        "SELECT count(*) FROM conversation_session;",  # ; final é tolerado
        # verbo proibido DENTRO de string literal não dispara
        "SELECT * FROM conversation_message WHERE content LIKE '%delete%' LIMIT 5",
        "SELECT 'DROP TABLE x' AS texto",
        # nomes de coluna que contêm verbos como substring não disparam (\b)
        "SELECT created_at, updated_at FROM pks_entity WHERE deleted_at IS NULL LIMIT 1",
    ],
)
def test_accepts_read_queries(query):
    assert validate_ro_query(query)


@pytest.mark.parametrize(
    "query",
    [
        "",
        "INSERT INTO mission (title) VALUES ('x')",
        "DELETE FROM conversation_message",
        "UPDATE mission SET status = 'done'",
        "DROP TABLE pks_entity",
        "SELECT 1; DELETE FROM mission",                       # multi-statement
        "WITH x AS (DELETE FROM mission RETURNING *) SELECT * FROM x",  # CTE de escrita
        "SELECT * FROM mission FOR UPDATE",                    # lock
        "SELECT 1 /* comentário com DELETE */; DROP TABLE x",
        "SELECT set_config('x', 'y', false)",                  # \bset\b
        "-- DELETE\nDELETE FROM mission",                      # comentário não esconde
        "CREATE TABLE hack (id int)",
    ],
)
def test_rejects_non_read_queries(query):
    with pytest.raises(ValueError):
        validate_ro_query(query)


def test_comments_are_stripped_from_executed_query():
    out = validate_ro_query("SELECT 1 -- até aqui\n LIMIT 1")
    assert "--" not in out
