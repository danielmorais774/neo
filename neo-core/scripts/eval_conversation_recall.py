#!/usr/bin/env python3
"""Eval do recall de conversas contra o corpus REAL do banco.

Roda o `ConversationRecallSearch` de verdade (mesmo SQL, mesma cascata) contra
as conversas de produção e imprime o que voltaria no prompt. É o teste que os
unitários não fazem: eles provam a lógica, este prova que o dado está lá.

    docker exec -w /app neo-core python scripts/eval_conversation_recall.py
    docker exec -w /app neo-core python scripts/eval_conversation_recall.py \
        --query "frança" --user <uuid>
    docker exec -w /app neo-core python scripts/eval_conversation_recall.py --tool

Sem --query, roda o conjunto de casos do docs/conversation-recall-plan.md §F6.
Com --tool, exercita o caminho REAL da tool (read set → CognitiveReadExecutor →
paginação por `before`), que é o que os unitários não cobrem.
Somente LEITURA: nenhuma escrita no banco.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

sys.path.insert(0, "/app")

from core.memory.conversation_recall import (  # noqa: E402
    ConversationRecallSearch,
    build_tsquery,
    extract_subject_terms,
)
from db.async_client import AsyncPostgresClient  # noqa: E402
from modules.conversation_recall.render import render_recall_block  # noqa: E402

# Perguntas como o usuário realmente as escreve — o ponto é provar que a
# extração de termos sobrevive à frase inteira.
CASES: list[tuple[str, str]] = [
    ("caso real (session-860d0fb1)", "ta de boa, eu conversei algo contigo sobre a frança?"),
    ("typo (trilho trigram)", "a gente falou de franssa?"),
    ("assunto que só o Neo disse", "você lembra o que você me falou sobre paris?"),
    ("assunto inexistente", "a gente conversou sobre criptomoeda mongoliana?"),
    ("sem assunto isolável", "você lembra?"),
]


async def _top_user(db: AsyncPostgresClient) -> UUID | None:
    from sqlalchemy import text

    async with db.get_session() as session:
        res = await session.execute(text(
            "SELECT user_id, count(*) c FROM conversation_message "
            "WHERE user_id IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 1"
        ))
        row = res.first()
    return row[0] if row else None


async def run_case(
    search: ConversationRecallSearch,
    user_id: UUID,
    label: str,
    question: str,
    show_block: bool,
) -> None:
    terms = extract_subject_terms(question)
    print(f"\n{'=' * 78}\n{label}\n  pergunta: {question!r}")
    print(f"  termos:   {terms}")
    print(f"  tsquery:  {build_tsquery(terms) or '(vazio)'}")

    result = await search.search(user_id=user_id, query=question)
    print(
        f"  → trilho={result.track} conversas={result.total_sessions} "
        f"mensagens={result.total_messages} tem_mais={result.has_more} "
        f"erro={result.error}"
    )
    for sess in result.window:
        print(
            f"     · {sess.first_hit_at:%d/%m %H:%M} [{sess.service}] "
            f"{sess.hit_count} hits — {str(sess.session_id)[:8]}"
        )
        for exc in sess.excerpts[:3]:
            quem = "usuário" if exc.role == "user" else "Neo"
            print(f"         [{quem}] {exc.content[:90]}")
    if show_block:
        print("\n--- bloco que iria para o prompt ---")
        print(render_recall_block(result))


def _headers(bloco: str) -> list[str]:
    return [ln for ln in bloco.splitlines() if ln.startswith("###")]


async def run_tool_path(db, user_id: UUID) -> None:
    """Exercita o caminho real: read set → executor → paginação por `before`."""
    from core.cognitive.read_tools import CognitiveReadExecutor, cognitive_tools_for
    from shared import ToolCall

    com_user = [t.name for t in cognitive_tools_for(user_id)]
    sem_user = [t.name for t in cognitive_tools_for(None)]
    print(f"\n{'=' * 78}\ncaminho da tool")
    print(f"  no read set (com usuário): {'conversation_recall' in com_user}")
    print(f"  retida sem usuário:        {'conversation_recall' not in sem_user}")

    ex = CognitiveReadExecutor(
        global_executor=None, db=db, pks_service=None,
        user_id=user_id, session_id=None,
    )
    tr = await ex.execute_tool_call(
        ToolCall(id="t1", name="conversation_recall", arguments={"query": "frança"})
    )
    if tr.error:
        print(f"  ERRO: {tr.error}")
        return
    print(f"\n  página 1: {tr.result['resumo']}")
    for h in _headers(tr.result["bloco"]):
        print(f"     {h}")

    proxima = tr.result.get("proxima_busca_before")
    print(f"     próxima_before={proxima}")
    if not proxima:
        return

    tr2 = await ex.execute_tool_call(ToolCall(
        id="t2", name="conversation_recall",
        arguments={"query": "frança", "before": proxima},
    ))
    print(f"\n  página 2: {tr2.result['resumo']}")
    for h in _headers(tr2.result["bloco"]):
        print(f"     {h}")
    repetiu = set(_headers(tr.result["bloco"])) & set(_headers(tr2.result["bloco"]))
    print(f"\n  páginas não se repetem: {not repetiu}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", help="Roda uma pergunta específica")
    parser.add_argument("--user", help="UUID do usuário (default: o mais ativo)")
    parser.add_argument("--block", action="store_true", help="Imprime o bloco do prompt")
    parser.add_argument("--tool", action="store_true", help="Exercita o caminho da tool")
    args = parser.parse_args()

    db = AsyncPostgresClient()
    try:
        user_id = UUID(args.user) if args.user else await _top_user(db)
        if user_id is None:
            print("nenhum usuário com mensagens no banco")
            return 1
        print(f"usuário: {user_id}")

        search = ConversationRecallSearch(db=db)
        has_tsv = await search._detect_tsv_column()
        print(f"content_tsv presente: {has_tsv}"
              + ("" if has_tsv else "  ← rode a migration r1s2t3u4v5w6"))

        if args.tool:
            await run_tool_path(db, user_id)
            return 0

        cases = [("query manual", args.query)] if args.query else CASES
        for label, question in cases:
            await run_case(search, user_id, label, question, args.block or bool(args.query))
        return 0
    finally:
        await db.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
