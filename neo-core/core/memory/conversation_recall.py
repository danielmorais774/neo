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
    #: Quantos acertos desta conversa apareceram de fato no bloco. Sem isto o
    #: bloco dizia "24 mensagens no assunto", mostrava 4 e não avisava que 20
    #: tinham ficado de fora — o modelo lia aquilo como a conversa inteira.
    hits_shown: int = 0


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


def _rank_hit_indices(
    messages: list[dict[str, Any]],
    folded_terms: list[str],
    hit_idx: list[int],
    limit: int,
    prefer_substance: bool = False,
) -> list[int]:
    """Ordena os acertos por relevância ANTES do corte.

    `hit_idx[:limit]` cortava pelos mais ANTIGOS — a lista nasce da varredura
    cronológica. Numa conversa que evoluiu, o que sobrava era a PRIMEIRA
    versão, e o pedido "me mostra aquilo que você escreveu" recebia justamente
    a versão superada. Foi assim que a "Reanálise Completa" da sessão 7330e40c
    ficou de fora: era o 6º acerto de 31, com per_session=4 — e o modelo, sem a
    fonte, preencheu o vão de cabeça.

    Critério: quantos termos DISTINTOS a mensagem casa (quem bate
    "reanalise"+"completa"+"pior" conta mais que quem só bate "pior").

    O DESEMPATE depende do que está sendo pedido, e errar isso custou uma
    sessão inteira:

    - ``prefer_substance`` (o pedido é VER o texto, `completo=true`): ganha a
      mensagem MAIOR. Quem pede uma tabela de volta quer a tabela, não quem
      fala dela. Na sessão 7330e40c, cinco mensagens empatavam em 6/6 termos;
      desempatando por recência, a "Reanálise Completa" (12.811 chars) caía em
      5º atrás de quatro recados curtos do tipo "a tabela está corrigida"
      (255-445 chars) — que eram as tentativas ERRADAS do próprio Neo. Com
      `per_session=4` ela ficava de fora por uma posição, e o modelo então
      reproduzia o erro.
    - sem ele (o pedido é SABER se falaram disso): ganha a mais recente, que é
      o estado atual do assunto.

    A ordem CRONOLÓGICA volta na apresentação (`sorted(keep)`); aqui é só quem
    entra.
    """
    scored: list[tuple[int, int, int]] = []
    for i in hit_idx:
        folded = _strip_accents(str(messages[i]["content"])).lower()
        distintos = sum(1 for t in folded_terms if t in folded)
        scored.append((distintos, len(str(messages[i]["content"])), i))
    if prefer_substance:
        scored.sort(key=lambda s: (-s[0], -s[1], -s[2]))
    else:
        scored.sort(key=lambda s: (-s[0], -s[2]))
    return [i for _, _, i in scored[:limit]]


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
        current_session_before: datetime | None = None,
        limit_sessions: int = 3,
        per_session: int = 4,
        full_excerpts: bool = False,
        only_session: str | None = None,
    ) -> RecallResult:
        """Busca conversas antigas sobre um assunto.

        ``before`` pagina para trás no tempo (o "não foi isso, foi antes ainda").

        ``exclude_session_id`` tira a conversa ATUAL do resultado — sem isso a
        própria pergunta casa com a busca e a resposta fica circular. Só que
        excluir a sessão INTEIRA jogou fora o caso mais comum de todos: o
        usuário pedindo algo que o Neo escreveu HOJE, nesta mesma conversa, mas
        longe demais para caber na janela de histórico. Foi exatamente isso na
        sessão 7330e40c — a "Reanálise Completa" estava a 11 mensagens de
        distância, e a única tool capaz de achá-la tinha a própria sessão na
        lista negra.

        ``current_session_before`` resolve os dois: a sessão atual VOLTA a ser
        pesquisável, mas só até esse instante. Passando o timestamp da mensagem
        que abriu o turno, a pergunta de agora continua fora (sem circularidade)
        e todo o resto da conversa fica ao alcance.

        ``only_session`` foca numa conversa que o bloco anterior já mostrou
        (pelo prefixo de 8 chars que aparece ali). A paginação por ``before``
        só anda para conversas mais ANTIGAS; não havia como dizer "é essa
        conversa mesmo, me mostra o resto dela" — e era esse o caso da sessão
        7330e40c, onde a conversa certa estava na tela e a mensagem certa,
        fora do corte.

        ``full_excerpts`` entrega INTEIRAS as mensagens que casaram, em vez do
        trecho de 240 chars. É o que separa "a gente já falou disso?" (o trecho
        basta) de "me mostra aquela análise de novo" (o trecho não basta: com o
        texto cortado a única saída do modelo é reescrever de cabeça).
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
            current_session_before=current_session_before,
        )
        track = "fts"
        if not rows:
            rows = await self._search_trigram(
                user_id=user_id, terms=terms, before=before,
                exclude_session_id=exclude_session_id,
                current_session_before=current_session_before,
            )
            track = "trgm"
        if not rows:
            rows = await self._search_vector(
                user_id=user_id, query=query, before=before,
                exclude_session_id=exclude_session_id,
                current_session_before=current_session_before,
            )
            track = "vector"
        if not rows:
            result.track = "none"
            return result

        if only_session:
            pref = str(only_session).strip().lower().replace("-", "")[:8]
            focadas = [
                r for r in rows
                if str(r["session_id"]).replace("-", "").lower().startswith(pref)
            ]
            if focadas:
                rows = focadas
            else:
                logger.info("[Recall] conversa %r não está nos resultados", only_session)

        result.track = track
        result.total_sessions = len(rows)
        result.total_messages = sum(int(r["hit_count"]) for r in rows)

        window_rows = rows[:limit_sessions]
        result.has_more = len(rows) > limit_sessions
        if result.has_more:
            # Cursor para a próxima página: a busca seguinte começa ANTES do
            # primeiro hit da conversa mais antiga desta janela.
            result.next_before = window_rows[-1]["first_hit_at"]

        await self._fill_excerpts(
            window_rows, terms, per_session, result,
            full_excerpts=full_excerpts,
            exclude_session_id=exclude_session_id,
            current_session_before=current_session_before,
        )
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
        current_session_before: datetime | None = None,
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
            if current_session_before is not None:
                # A sessão atual entra, mas só o que é anterior ao turno de
                # agora — ver a docstring de `search`.
                clauses.append(
                    "(cm.session_id <> :exclude_session"
                    " OR cm.created_at < :cur_session_before)"
                )
                params["cur_session_before"] = current_session_before
            else:
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
        current_session_before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "user_id": str(user_id),
            "tsq": build_tsquery(terms),
            "limit": _MAX_AGG_SESSIONS,
        }
        filters = self._common_filters(
            params, before, exclude_session_id, current_session_before,
        )
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
        current_session_before: datetime | None = None,
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
        filters = self._common_filters(
            params, before, exclude_session_id, current_session_before,
        )
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
        current_session_before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Último resgate: paráfrase sem nenhum termo em comum.

        Valia pouco quando só mensagens do USUÁRIO tinham embedding (0 de
        1520 respostas do Neo). Hoje o `add_message` embeda os dois lados
        (2299 de 2444 respostas cobertas), então este trilho enxerga também o
        que o NEO escreveu — que é justamente o que o usuário costuma pedir de
        volta. Roda só quando os dois trilhos textuais vieram vazios.
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
        filters = self._common_filters(
            params, before, exclude_session_id, current_session_before,
        )
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
        full_excerpts: bool = False,
        exclude_session_id: UUID | None = None,
        current_session_before: datetime | None = None,
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

        # O mesmo corte da busca vale para os TRECHOS: sem ele, a sessão atual
        # devolveria como "lembrança" a pergunta que o usuário acabou de fazer.
        corte = ""
        if exclude_session_id is not None and current_session_before is not None:
            corte = (
                " AND (session_id <> :cut_sid OR created_at < :cut_before)"
            )
            params["cut_sid"] = str(exclude_session_id)
            params["cut_before"] = current_session_before

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
                          {corte}
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

        # Orçamento SOMADO dos trechos longos, gasto da sessão mais recente para
        # a mais antiga. Sem ele o teto por mensagem não limita nada.
        from shared.config import get_settings

        budget = [
            getattr(get_settings(), "conversation_recall_full_total_chars", 40000)
            if full_excerpts else 0
        ]

        # ORDEM DE GASTO do orçamento ≠ ordem de apresentação. Gastar da
        # sessão mais recente para a mais antiga entregava o orçamento à
        # conversa de AGORA — cujo conteúdo o modelo já tem no histórico — e a
        # conversa que realmente importa ficava com as sobras. Pior: na sessão
        # cbff8f81 isso devolveu ao modelo, como "memória recuperada", a
        # resposta ERRADA que ele mesmo tinha dado dois turnos antes, e ele a
        # confirmou. Gasta-se por RELEVÂNCIA, com a sessão atual por último.
        atual = str(exclude_session_id) if exclude_session_id else None
        ordem_gasto = sorted(
            window_rows,
            key=lambda a: (
                str(a["session_id"]) == atual,      # a de agora por último
                -float(a.get("best_rank") or 0.0),  # depois, a mais relevante
            ),
        )
        por_sessao: dict[str, list[RecallExcerpt]] = {}
        for agg in ordem_gasto:
            sid = str(agg["session_id"])
            por_sessao[sid] = self._pick_excerpts(
                by_session.get(sid, []), folded_terms, per_session,
                full_excerpts=full_excerpts, budget=budget,
            )

        for agg in window_rows:
            sid = str(agg["session_id"])
            excerpts = por_sessao.get(sid, [])
            result.window.append(RecallSession(
                session_id=agg["session_id"],
                service=str(agg.get("service") or ""),
                first_hit_at=agg["first_hit_at"],
                last_hit_at=agg["last_hit_at"],
                hit_count=int(agg["hit_count"]),
                best_rank=float(agg["best_rank"] or 0.0),
                excerpts=excerpts,
                hits_shown=sum(1 for e in excerpts if e.is_hit),
            ))

    @staticmethod
    def _pick_excerpts(
        messages: list[dict[str, Any]],
        folded_terms: list[str],
        per_session: int,
        full_excerpts: bool = False,
        budget: list[int] | None = None,
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

        chosen = _rank_hit_indices(
            messages, folded_terms, hit_idx, per_session,
            prefer_substance=full_excerpts,
        )
        chosen_set = set(chosen)

        keep: set[int] = set()
        for i in chosen:
            keep.add(i)
            # A vizinha que fecha o par pergunta→resposta.
            if i + 1 < len(messages):
                keep.add(i + 1)
            elif i - 1 >= 0:
                keep.add(i - 1)

        from shared.config import get_settings

        _st = get_settings()
        curto = getattr(_st, "conversation_recall_excerpt_chars", _EXCERPT_CHARS)
        longo = getattr(_st, "conversation_recall_full_excerpt_chars", 12000)

        excerpts: list[RecallExcerpt] = []
        for i in sorted(keep):
            msg = messages[i]
            # Só o acerto ESCOLHIDO vem inteiro; a vizinha é contexto e
            # continua curta (antes bastava ser acerto, e uma vizinha que
            # casava o termo comia o orçamento dos trechos longos).
            teto = curto
            if full_excerpts and i in chosen_set and budget is not None:
                disponivel = min(longo, budget[0])
                if disponivel > curto:
                    teto = disponivel
            raw = str(msg["content"])
            if teto > curto:
                # No trecho integral as quebras de linha SÃO o conteúdo:
                # achatar aqui transformava a tabela markdown da mensagem numa
                # linha só, e a única saída do modelo era reescrevê-la de
                # cabeça — exatamente a confabulação que `completo=true`
                # existe para evitar.
                content = raw.strip()
            else:
                content = " ".join(raw.split())
            if len(content) > teto:
                content = content[:teto].rstrip() + "…"
            if teto > curto and budget is not None:
                budget[0] = max(0, budget[0] - len(content))
            excerpts.append(RecallExcerpt(
                role=str(msg["role"]),
                created_at=msg["created_at"],
                content=content,
                is_hit=i in hit_idx,
            ))
        return excerpts
