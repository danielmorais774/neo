"""Recall de conversas — busca no ARQUIVO de conversas passadas do usuário.

Responde "a gente já falou sobre X?" indo ao banco em vez de responder de
cabeça. Ver docs/conversation-recall-plan.md.

Diferente da recuperação passiva (``SessionManager.recover_context``), que roda
em todo turno com orçamento apertado e ordena por similaridade, aqui:

- a busca é um PEDIDO explícito, então pode custar mais;
- o resultado é agrupado por SESSÃO e ordenado por RECÊNCIA (a pergunta é
  "quando falamos disso", e o usuário navega no tempo com "foi antes ainda");
- a janela é pequena e paginável por ``before``.

Três trilhos, em CASCATA (não em paralelo): FTS indexado → trigram (typo) →
vetor (paráfrase). Cada estágio só roda se o anterior não achou nada, então o
caminho comum paga uma query indexada só.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

if TYPE_CHECKING:
    from core.session.embedding_service import EmbeddingService
    from db.async_client import AsyncPostgresClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extração de termos
# ---------------------------------------------------------------------------

# Palavras que descrevem o ATO de conversar/lembrar, não o assunto. Sem tirá-las,
# `plainto_tsquery` liga tudo com AND e a recall morre: medido em produção,
# "eu conversei algo contigo sobre a frança?" devolvia 1 mensagem; só "frança"
# devolvia 61. Ver docs/conversation-recall-plan.md §2d.
_META_STOPWORDS = frozenset("""
    conversa conversas conversamos conversei conversou conversando conversar
    falamos falei falou falado falar fala falando disse dissemos disseram dizer
    dito comentei comentou comentamos mencionei mencionou contei contou contamos
    lembra lembro lembrar lembrou lembramos lembranca recorda recordo recordar
    esqueci esqueceu perguntei perguntou perguntamos respondi respondeu
    contigo comigo voce vc ce tu eu nos gente agente
    sobre algo alguma algum coisa coisas assunto tema negocio treco troco
    quando onde como porque pq qual quais quem que quanto quantos
    isso isto aquilo aquele aquela aqueles aquelas esse essa esses essas este
    esta ja nao sim talvez tem teve tinha ter tive tivemos foi era eram sao
    ser sendo estar esta estava estavam ficou ficamos vez vezes dia dias
    ontem hoje amanha antes depois agora ainda mais menos muito pouco
    para pra por com sem dos das nas nos aos uma uns umas dele dela deles
    algumaz coisaz tipo assim entao ai la aqui
    """
    # Saudação, gíria e enchimento: nunca são o ASSUNTO de um recall, mas vêm
    # colados na pergunta ("ta de boa, eu conversei contigo sobre a frança?" —
    # sem isto, 'boa' virava termo de busca e casava com meio banco).
    """
    oi ola opa eae eai iai fala salve bom boa bons boas dia tarde noite
    beleza blz belezura tranquilo tranquila suave certo certa show massa
    legal joia valeu vlw obrigado obrigada brigado thanks ok okay
    cara mano chapa vei velho parceiro brother mermao
    entao pois porem mas tambem so apenas mesmo ainda ja
""".split())

# Termos curtos demais viram ruído no tsquery (e no trigram).
_MIN_TERM_LEN = 3
_MAX_TERMS = 6

_WORD_RE = re.compile(r"[\wÀ-ÿ]+")


def _strip_accents(text_in: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text_in)
        if unicodedata.category(c) != "Mn"
    )


def extract_subject_terms(
    query: str,
    keywords: list[str] | None = None,
) -> list[str]:
    """Isola os termos de ASSUNTO de uma pergunta de recall.

    Começa pelas ``knowledge_keywords`` do classificador (que já vêm limpas:
    para "eu conversei algo contigo sobre a frança?" ele devolveu
    ``["conversa", "frança"]``), acrescenta as palavras de conteúdo da própria
    query e remove a stoplist de meta-conversa. Preserva o termo ACENTUADO como
    veio — quem gera as variantes é ``build_tsquery``.

    Devolve [] quando não sobra assunto nenhum ("você lembra?" sozinho); o
    chamador tem que tratar isso como "não sei do que ele fala", NUNCA como
    "não achei nada" — afirmar ausência sem ter buscado é a mentira que este
    módulo existe para matar.
    """
    terms: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        for word in _WORD_RE.findall(raw or ""):
            if len(word) < _MIN_TERM_LEN:
                continue
            folded = _strip_accents(word).lower()
            if folded in _META_STOPWORDS or folded in seen:
                continue
            if folded.isdigit():
                continue
            seen.add(folded)
            terms.append(word.lower())

    for kw in keywords or []:
        _add(kw)
    _add(query)
    return terms[:_MAX_TERMS]


def build_tsquery(terms: list[str]) -> str:
    """Monta a expressão OR para ``to_tsquery('portuguese', ...)``.

    Emite a variante ACENTUADA e a SEM ACENTO de cada termo, porque o stemmer
    português só dobra o acento em alguns casos — medido: 'frança' e 'franca'
    caem no mesmo lexema 'franc', mas 'avião' vira 'aviã' e 'aviao' vira
    'avia', que NÃO casam. Com as duas variantes, tanto faz como o usuário
    digitou agora e como digitou na época.

    Os termos já vêm sanitizados por ``_WORD_RE`` (só letras/dígitos), então
    não há como injetar sintaxe de tsquery aqui.
    """
    pieces: list[str] = []
    for term in terms:
        for variant in (term, _strip_accents(term)):
            if variant and variant not in pieces:
                pieces.append(variant)
    return " | ".join(pieces)


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

@dataclass
class RecallExcerpt:
    """Uma mensagem recuperada (com o papel de quem falou e a hora)."""

    role: str
    created_at: datetime
    content: str
    is_hit: bool = False  # False = vizinha, incluída só para dar sentido


@dataclass
class RecallSession:
    """Uma conversa antiga que casou com a busca."""

    session_id: UUID
    service: str
    first_hit_at: datetime
    last_hit_at: datetime
    hit_count: int
    best_rank: float
    excerpts: list[RecallExcerpt] = field(default_factory=list)


@dataclass
class RecallResult:
    """Saída completa de um recall."""

    query: str
    terms: list[str]
    window: list[RecallSession] = field(default_factory=list)
    total_sessions: int = 0
    total_messages: int = 0
    has_more: bool = False
    next_before: datetime | None = None
    track: str = "none"          # fts | trgm | vector | none
    error: str | None = None     # "sem_termo" quando não dá pra buscar

    @property
    def found(self) -> bool:
        return bool(self.window)


# ---------------------------------------------------------------------------
# Busca
# ---------------------------------------------------------------------------

_TRGM_MIN = 0.45          # medido: word_similarity('franssa', '...frança') = 0.5
_MAX_AGG_SESSIONS = 50    # teto do "quantas conversas casaram"
_MAX_WINDOW_MESSAGES = 200
_EXCERPT_CHARS = 240
_FUZZY_WORD_MIN = 0.7     # semelhança palavra-a-palavra no resgate de trechos
_FUZZY_MAX_WORDS = 60     # palavras avaliadas por mensagem (teto de custo)


def _fuzzy_hit_indices(
    messages: list[dict[str, Any]],
    folded_terms: list[str],
    limit: int,
) -> list[int]:
    """Mensagens PARECIDAS com os termos, quando não há acerto literal.

    Espelha em Python o que o `word_similarity` do pg_trgm fez no SQL: compara
    o termo com cada PALAVRA da mensagem, não com a mensagem inteira (comparar
    com o texto todo dilui a semelhança a ponto de nada passar).
    """
    from difflib import SequenceMatcher

    scored: list[tuple[float, int]] = []
    for i, msg in enumerate(messages):
        folded = _strip_accents(str(msg["content"])).lower()
        best = 0.0
        for word in _WORD_RE.findall(folded)[:_FUZZY_MAX_WORDS]:
            if len(word) < _MIN_TERM_LEN:
                continue
            for term in folded_terms:
                ratio = SequenceMatcher(None, term, word).ratio()
                if ratio > best:
                    best = ratio
        if best >= _FUZZY_WORD_MIN:
            scored.append((best, i))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return sorted(i for _, i in scored[:limit])


class ConversationRecallSearch:
    """Busca conversas passadas do usuário por assunto."""

    def __init__(
        self,
        db: AsyncPostgresClient,
        embeddings: EmbeddingService | None = None,
    ) -> None:
        self._db = db
        self._embeddings = embeddings
        # None = ainda não checado. A coluna é criada pela migration
        # r1s2t3u4v5w6; antes dela, o FTS roda com to_tsvector on-the-fly
        # (medido: 120ms num corpus de 3010 mensagens). Assim a feature
        # funciona antes E depois da migration, sem meio-termo silencioso.
        self._has_tsv_column: bool | None = None

    # ── superfície pública ────────────────────────────────────────────

    async def search(
        self,
        *,
        user_id: UUID,
        query: str,
        keywords: list[str] | None = None,
        before: datetime | None = None,
        exclude_session_id: UUID | None = None,
        limit_sessions: int = 3,
        per_session: int = 4,
    ) -> RecallResult:
        """Busca conversas antigas sobre um assunto.

        ``before`` pagina para trás no tempo (o "não foi isso, foi antes ainda").
        ``exclude_session_id`` tira a conversa ATUAL do resultado — sem isso a
        própria pergunta casa com a busca e a resposta fica circular.
        """
        terms = extract_subject_terms(query, keywords)
        if not terms:
            logger.info("[Recall] sem termo de assunto em %r", query)
            return RecallResult(query=query, terms=[], error="sem_termo")

        result = RecallResult(query=query, terms=terms)

        # Cascata: cada estágio só roda se o anterior veio vazio.
        rows = await self._search_fts(
            user_id=user_id, terms=terms, before=before,
            exclude_session_id=exclude_session_id,
        )
        track = "fts"
        if not rows:
            rows = await self._search_trigram(
                user_id=user_id, terms=terms, before=before,
                exclude_session_id=exclude_session_id,
            )
            track = "trgm"
        if not rows:
            rows = await self._search_vector(
                user_id=user_id, query=query, before=before,
                exclude_session_id=exclude_session_id,
            )
            track = "vector"
        if not rows:
            result.track = "none"
            return result

        result.track = track
        result.total_sessions = len(rows)
        result.total_messages = sum(int(r["hit_count"]) for r in rows)

        window_rows = rows[:limit_sessions]
        result.has_more = len(rows) > limit_sessions
        if result.has_more:
            # Cursor para a próxima página: a busca seguinte começa ANTES do
            # primeiro hit da conversa mais antiga desta janela.
            result.next_before = window_rows[-1]["first_hit_at"]

        await self._fill_excerpts(window_rows, terms, per_session, result)
        return result

    # ── trilhos ───────────────────────────────────────────────────────

    async def _tsv_expr(self) -> str:
        """`content_tsv` quando a coluna existe; senão, calcula na hora."""
        if self._has_tsv_column is None:
            self._has_tsv_column = await self._detect_tsv_column()
        if self._has_tsv_column:
            return "cm.content_tsv"
        return "to_tsvector('portuguese', cm.content)"

    async def _detect_tsv_column(self) -> bool:
        try:
            async with self._db.get_session() as db:
                res = await db.execute(text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'conversation_message' "
                    "AND column_name = 'content_tsv'"
                ))
                present = res.first() is not None
        except Exception as e:  # noqa: BLE001 — nunca derruba o turno
            logger.warning("[Recall] detecção de content_tsv falhou: %s", e)
            return False
        if not present:
            logger.warning(
                "[Recall] coluna content_tsv AUSENTE — usando to_tsvector "
                "on-the-fly (rode a migration r1s2t3u4v5w6)"
            )
        return present

    def _common_filters(
        self,
        params: dict[str, Any],
        before: datetime | None,
        exclude_session_id: UUID | None,
    ) -> str:
        """Filtros que valem para os três trilhos."""
        clauses = [
            "cm.user_id = :user_id",
            "cm.role IN ('user', 'assistant')",
            "cm.tool_name IS NULL",
            "length(btrim(cm.content)) > 8",
        ]
        if before is not None:
            clauses.append("cm.created_at < :before")
            params["before"] = before
        if exclude_session_id is not None:
            clauses.append("cm.session_id <> :exclude_session")
            params["exclude_session"] = str(exclude_session_id)
        return " AND ".join(clauses)

    async def _aggregate(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Roda a query de agregação por sessão e devolve dicts."""
        try:
            async with self._db.get_session() as db:
                res = await db.execute(text(sql), params)
                return [dict(r._mapping) for r in res.fetchall()]
        except Exception as e:  # noqa: BLE001 — busca nunca derruba o turno
            logger.warning("[Recall] busca falhou: %s", e, exc_info=True)
            return []

    async def _search_fts(
        self,
        *,
        user_id: UUID,
        terms: list[str],
        before: datetime | None,
        exclude_session_id: UUID | None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "user_id": str(user_id),
            "tsq": build_tsquery(terms),
            "limit": _MAX_AGG_SESSIONS,
        }
        filters = self._common_filters(params, before, exclude_session_id)
        tsv = await self._tsv_expr()
        sql = f"""
            WITH q AS (SELECT to_tsquery('portuguese', :tsq) AS tq)
            SELECT cm.session_id,
                   max(cs.service)               AS service,
                   count(*)                      AS hit_count,
                   min(cm.created_at)            AS first_hit_at,
                   max(cm.created_at)            AS last_hit_at,
                   max(ts_rank_cd({tsv}, q.tq))  AS best_rank
            FROM conversation_message cm
            JOIN conversation_session cs ON cs.id = cm.session_id, q
            WHERE {tsv} @@ q.tq AND {filters}
            GROUP BY cm.session_id
            ORDER BY max(cm.created_at) DESC
            LIMIT :limit
        """
        return await self._aggregate(sql, params)

    async def _search_trigram(
        self,
        *,
        user_id: UUID,
        terms: list[str],
        before: datetime | None,
        exclude_session_id: UUID | None,
    ) -> list[dict[str, Any]]:
        """Resgate por semelhança de trigramas — pega erro de digitação.

        Usa ``word_similarity`` (termo curto DENTRO de documento longo) e não
        ``similarity``: medido, similarity('a capital de paris é a frança',
        'frança') = 0.25, porque normaliza pelo tamanho da mensagem inteira;
        word_similarity dá 1.0 no mesmo par e 0.5 com o typo 'franssa'.
        """
        params: dict[str, Any] = {
            "user_id": str(user_id),
            "trgm_min": _TRGM_MIN,
            "limit": _MAX_AGG_SESSIONS,
        }
        filters = self._common_filters(params, before, exclude_session_id)
        sim_pieces = []
        for i, term in enumerate(terms):
            params[f"t{i}"] = _strip_accents(term)
            sim_pieces.append(f"word_similarity(:t{i}, lower(cm.content))")
        best_sim = f"GREATEST({', '.join(sim_pieces)})" if len(sim_pieces) > 1 else sim_pieces[0]
        sql = f"""
            SELECT cm.session_id,
                   max(cs.service)      AS service,
                   count(*)             AS hit_count,
                   min(cm.created_at)   AS first_hit_at,
                   max(cm.created_at)   AS last_hit_at,
                   max({best_sim})      AS best_rank
            FROM conversation_message cm
            JOIN conversation_session cs ON cs.id = cm.session_id
            WHERE {best_sim} >= :trgm_min AND {filters}
            GROUP BY cm.session_id
            ORDER BY max(cm.created_at) DESC
            LIMIT :limit
        """
        return await self._aggregate(sql, params)

    async def _search_vector(
        self,
        *,
        user_id: UUID,
        query: str,
        before: datetime | None,
        exclude_session_id: UUID | None,
    ) -> list[dict[str, Any]]:
        """Último resgate: paráfrase sem nenhum termo em comum.

        Vale pouco hoje — só mensagens do USUÁRIO têm embedding (0 de 1520
        respostas do Neo têm) — mas é barato e não atrapalha: só roda quando os
        dois trilhos textuais vieram vazios.
        """
        if self._embeddings is None or not self._embeddings.available:
            return []
        try:
            embedding = await self._embeddings.embed(query)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Recall] embedding da query falhou: %s", e)
            return []

        params: dict[str, Any] = {
            "user_id": str(user_id),
            "embedding": str(embedding),
            "min_sim": 0.45,
            "limit": _MAX_AGG_SESSIONS,
        }
        filters = self._common_filters(params, before, exclude_session_id)
        sim = "1 - (cm.embedding <=> CAST(:embedding AS vector))"
        sql = f"""
            SELECT cm.session_id,
                   max(cs.service)    AS service,
                   count(*)           AS hit_count,
                   min(cm.created_at) AS first_hit_at,
                   max(cm.created_at) AS last_hit_at,
                   max({sim})         AS best_rank
            FROM conversation_message cm
            JOIN conversation_session cs ON cs.id = cm.session_id
            WHERE cm.embedding IS NOT NULL AND {sim} > :min_sim AND {filters}
            GROUP BY cm.session_id
            ORDER BY max(cm.created_at) DESC
            LIMIT :limit
        """
        return await self._aggregate(sql, params)

    # ── trechos ───────────────────────────────────────────────────────

    async def _fill_excerpts(
        self,
        window_rows: list[dict[str, Any]],
        terms: list[str],
        per_session: int,
        result: RecallResult,
    ) -> None:
        """Carrega as mensagens das sessões da janela e escolhe os trechos.

        Puxa a conversa inteira de cada sessão da janela (elas são pequenas:
        mediana de 6 mensagens no corpus real) e monta os trechos em Python —
        assim dá para incluir a mensagem VIZINHA de cada acerto, que é o que
        faz o trecho ter sentido: pergunta sem resposta não conta a história.
        """
        session_ids = [str(r["session_id"]) for r in window_rows]
        if not session_ids:
            return

        # IN com um bind por sessão em vez de `= ANY(:array)`: o driver
        # (asyncpg) recusa array passado como string e exige lista Python, que
        # o `text()` não tipa sozinho. São no máximo 10 sessões — o IN é mais
        # simples do que ensinar o tipo ao driver.
        params: dict[str, Any] = {"cap": _MAX_WINDOW_MESSAGES}
        placeholders: list[str] = []
        for i, sid in enumerate(session_ids):
            params[f"s{i}"] = sid
            placeholders.append(f":s{i}")

        rows: list[dict[str, Any]] = []
        try:
            async with self._db.get_session() as db:
                res = await db.execute(
                    text(f"""
                        SELECT id, session_id, role, content, created_at
                        FROM conversation_message
                        WHERE session_id IN ({", ".join(placeholders)})
                          AND role IN ('user', 'assistant')
                          AND tool_name IS NULL
                          AND length(btrim(content)) > 8
                        ORDER BY session_id, created_at
                        LIMIT :cap
                    """),
                    params,
                )
                rows = [dict(r._mapping) for r in res.fetchall()]
        except Exception as e:  # noqa: BLE001
            logger.warning("[Recall] carga de trechos falhou: %s", e, exc_info=True)
            return

        by_session: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_session.setdefault(str(row["session_id"]), []).append(row)

        folded_terms = [_strip_accents(t).lower() for t in terms]

        for agg in window_rows:
            sid = str(agg["session_id"])
            messages = by_session.get(sid, [])
            excerpts = self._pick_excerpts(messages, folded_terms, per_session)
            result.window.append(RecallSession(
                session_id=agg["session_id"],
                service=str(agg.get("service") or ""),
                first_hit_at=agg["first_hit_at"],
                last_hit_at=agg["last_hit_at"],
                hit_count=int(agg["hit_count"]),
                best_rank=float(agg["best_rank"] or 0.0),
                excerpts=excerpts,
            ))

    @staticmethod
    def _pick_excerpts(
        messages: list[dict[str, Any]],
        folded_terms: list[str],
        per_session: int,
    ) -> list[RecallExcerpt]:
        """Escolhe os acertos e puxa a mensagem vizinha de cada um."""
        hit_idx: list[int] = []
        for i, msg in enumerate(messages):
            folded = _strip_accents(str(msg["content"])).lower()
            if any(t in folded for t in folded_terms):
                hit_idx.append(i)

        # Trigram/vetor casam SEM o termo aparecer literalmente (é o caso do
        # typo: a busca por "franssa" acha a conversa sobre "frança", mas
        # nenhuma mensagem contém "franssa"). Sem este resgate, o bloco
        # mostrava o COMEÇO da conversa — normalmente o "oi, tudo bem?" — em
        # vez do trecho que casou.
        if not hit_idx:
            hit_idx = _fuzzy_hit_indices(messages, folded_terms, per_session)
        if not hit_idx:
            hit_idx = list(range(min(len(messages), per_session)))

        keep: set[int] = set()
        for i in hit_idx[:per_session]:
            keep.add(i)
            # A vizinha que fecha o par pergunta→resposta.
            if i + 1 < len(messages):
                keep.add(i + 1)
            elif i - 1 >= 0:
                keep.add(i - 1)

        excerpts: list[RecallExcerpt] = []
        for i in sorted(keep):
            msg = messages[i]
            content = " ".join(str(msg["content"]).split())
            if len(content) > _EXCERPT_CHARS:
                content = content[:_EXCERPT_CHARS].rstrip() + "…"
            excerpts.append(RecallExcerpt(
                role=str(msg["role"]),
                created_at=msg["created_at"],
                content=content,
                is_hit=i in hit_idx,
            ))
        return excerpts
