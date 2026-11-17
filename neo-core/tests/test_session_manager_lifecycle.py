from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from core.session.session_manager import PersistentSessionManager
from db.models.conversation import ConversationMessage, ConversationSession


class _FakeEmbeddings:
    available = False


class _AvailableEmbeddings:
    """Embeddings stub that reports available but is never reached when a
    deterministic guard short-circuits rotation first."""

    available = True

    async def embed_batch(self, texts):  # pragma: no cover - guard returns first
        raise AssertionError("embed_batch should not be called for referential follow-ups")


class _OrthogonalEmbeddings:
    """Dois vetores perpendiculares: similaridade 0, ou seja, tópico totalmente
    diferente. É o cenário mais favorável possível à rotação."""

    available = True

    async def embed_batch(self, texts):
        return [[1.0, 0.0], [0.0, 1.0]]

    async def embed(self, text):
        return [1.0, 0.0]


class _FakeDB:
    def __init__(self, existing: ConversationSession | None = None) -> None:
        self.sessions: dict = {}
        if existing:
            self.sessions[existing.id] = existing
        self.created: list[ConversationSession] = []
        self.closed_updates: list[ConversationSession] = []
        self.histories: dict = {}
        self.messages: dict = {}

    async def get_conversation_session(self, session_id):
        return self.sessions.get(session_id)

    async def create_conversation_session(
        self,
        session_id=None,
        user_id=None,
        service="neo-desktop",
        metadata=None,
    ):
        session = ConversationSession(
            id=session_id or uuid4(),
            user_id=user_id,
            service=service,
            extra_metadata=metadata,
        )
        self.sessions[session.id] = session
        self.created.append(session)
        return session

    async def fetch_session_messages(self, session_id, limit=50):
        return self.messages.get(session_id, [])[-limit:]

    async def fetch_session_history(self, session_id, limit=20):
        return self.histories.get(session_id, [])[-limit:]

    async def fetch_recent_sessions(
        self,
        user_id=None,
        service="neo-desktop",
        limit=5,
        statuses=None,
        exclude_session_id=None,
    ):
        sessions = [
            session for session in self.sessions.values()
            if session.service == service and session.id != exclude_session_id
        ]
        if user_id:
            sessions = [session for session in sessions if session.user_id == user_id]
        if statuses:
            sessions = [session for session in sessions if session.status in statuses]
        sessions.sort(key=lambda session: session.last_activity_at, reverse=True)
        return sessions[:limit]

    async def update_session(self, session):
        self.sessions[session.id] = session
        if session.status == "closed":
            self.closed_updates.append(session)


def _manager(db: _FakeDB) -> PersistentSessionManager:
    return PersistentSessionManager(
        db=db,
        embedding_service=_FakeEmbeddings(),
        hybrid_search=None,
        token_counter=None,
    )


def _idle_session(hours: float) -> ConversationSession:
    return ConversationSession(
        id=uuid4(),
        status="active",
        message_count=4,
        last_activity_at=datetime.utcnow() - timedelta(hours=hours),
    )


@pytest.mark.asyncio
async def test_expired_live_session_creates_new_backend_session():
    """No live, o relógio curto continua valendo: pausa longa encerra a conversa."""
    old = _idle_session(hours=2)
    old_id = old.id
    db = _FakeDB(old)

    session = await _manager(db).get_or_create_session(
        session_id=old_id,
        user_id=None,
        service="neo-desktop",
        mode="live",
    )

    assert session.id != old_id
    assert old.status == "closed"
    assert old.extra_metadata["close_reason"] == "inactivity"
    assert session.extra_metadata["created_after_expired_session_id"] == str(old_id)


@pytest.mark.asyncio
async def test_chat_session_survives_a_gap_that_would_expire_live():
    """A regressão que motivou o mode: 2h de pausa NÃO podem cortar um chat.

    Era assim que "converte o preço da ponte que eu tinha pedido" chegava numa
    sessão em branco — 3h27 depois da resposta que continha o preço.
    """
    old = _idle_session(hours=2)
    old_id = old.id
    db = _FakeDB(old)

    session = await _manager(db).get_or_create_session(
        session_id=old_id,
        user_id=None,
        service="neo-desktop",
        mode="agent",
    )

    assert session.id == old_id
    assert old.status == "active"


@pytest.mark.asyncio
async def test_chat_backstop_resume_o_topico_sem_trocar_de_sessao():
    """Passado o backstop longo, o que o chat perde é o SILÊNCIO, não a sessão.

    O resumo/embedding de tópico (que alimenta a busca entre sessões) sai do
    mesmo jeito; a conversa continua onde estava. Até 2026-09-17 este caminho
    forkava uma sessão nova — era o "ele rotacionou sozinho depois de um tempo".
    """
    old = _idle_session(hours=24)
    old_id = old.id
    db = _FakeDB(old)
    db.messages[old_id] = [
        ConversationMessage(session_id=old_id, role="user", content="como fica o cálculo da ponte?"),
        ConversationMessage(session_id=old_id, role="assistant", content="Dá 4.200 no total."),
    ]
    manager = PersistentSessionManager(
        db=db,
        embedding_service=_OrthogonalEmbeddings(),
        hybrid_search=None,
        token_counter=None,
    )

    session = await manager.get_or_create_session(
        session_id=old_id,
        user_id=None,
        service="neo-desktop",
        mode="agent",
    )

    assert session.id == old_id
    assert session.status == "active"
    assert db.closed_updates == []
    assert "ponte" in session.topic_summary
    assert session.topic_embedding == [1.0, 0.0]


@pytest.mark.asyncio
async def test_sessao_fechada_pedida_por_id_no_chat_e_reativada():
    """`_is_expired` devolvia True para QUALQUER sessão não-ativa, então o ramo de
    reativação era inalcançável: voltar a escrever numa sessão fechada (pelo
    relógio do live num desvio por voz, por exemplo) sempre criava outra."""
    old = _idle_session(hours=1)
    old.status = "closed"
    old.ended_at = datetime.utcnow() - timedelta(hours=1)
    old_id = old.id
    db = _FakeDB(old)

    session = await _manager(db).get_or_create_session(
        session_id=old_id,
        user_id=None,
        service="neo-desktop",
        mode="agent",
    )

    assert session.id == old_id
    assert session.status == "active"
    assert session.ended_at is None
    assert db.created == []


@pytest.mark.asyncio
async def test_turn_resumption_without_mode_is_treated_as_chat():
    """tool_result/aprovação de comando não carregam mode e não podem expirar."""
    old = _idle_session(hours=2)
    old_id = old.id
    db = _FakeDB(old)

    session = await _manager(db).get_or_create_session(
        session_id=old_id,
        user_id=None,
        service="neo-desktop",
    )

    assert session.id == old_id
    assert old.status == "active"


@pytest.mark.asyncio
async def test_unknown_explicit_session_id_is_accepted_for_new_chat():
    requested_id = uuid4()
    db = _FakeDB()

    session = await _manager(db).get_or_create_session(
        session_id=requested_id,
        user_id=None,
        service="neo-desktop",
    )

    assert session.id == requested_id
    assert session.status == "active"


def test_detect_context_dependency_for_elliptical_followups():
    detector = PersistentSessionManager.detect_context_dependency

    assert detector("continua")["is_context_dependent"] is True
    assert detector("Eu estava pensando e não gostei muito dessa forma como me passou")["is_context_dependent"] is True
    assert detector("voltando ao assunto, queria ajustar o pipeline")["kind"] == "explicit_return"
    assert detector("qual é a capital da França?")["is_context_dependent"] is False


def test_detect_context_dependency_for_referential_questions():
    detector = PersistentSessionManager.detect_context_dependency

    # The exact bug from session 9d61bfba: a referential follow-up question.
    res = detector("como tu chegou nessa conta de 22?")
    assert res["is_context_dependent"] is True
    assert res["kind"] == "referential_question"

    assert detector("de onde veio isso?")["is_context_dependent"] is True
    # A self-contained question must NOT be treated as context-dependent.
    assert detector("como funciona um motor a diesel?")["is_context_dependent"] is False


def test_detect_context_dependency_numeric_back_reference():
    detector = PersistentSessionManager.detect_context_dependency

    recent = "diminui o ar-condicionado do quarto para 22°C"
    # "de quanto?" alone is generic, but "22" echoes the recent context.
    res = detector("por que ficou em 22 mesmo?", recent_context=recent)
    assert res["is_context_dependent"] is True
    assert res["reason"] in ("numeric_back_reference", "referential_question_demonstrative")


@pytest.mark.asyncio
async def test_should_not_rotate_on_referential_followup():
    session = ConversationSession(
        id=uuid4(),
        status="active",
        message_count=8,
        last_activity_at=datetime.utcnow(),
    )
    db = _FakeDB(session)
    manager = PersistentSessionManager(
        db=db,
        embedding_service=_AvailableEmbeddings(),
        hybrid_search=None,
        token_counter=None,
    )

    result = await manager.should_rotate_session(
        session, "como tu chegou nessa conta de 22 graus afinal?", mode="live"
    )

    assert result["should_rotate"] is False
    assert "follow-up dependente" in result["reason"]


@pytest.mark.asyncio
async def test_chat_nunca_rotaciona_nem_com_topico_totalmente_diferente():
    """Similaridade 0, 20 mensagens, 6h parado — no chat, segue a mesma sessão."""
    session = ConversationSession(
        id=uuid4(),
        status="active",
        message_count=20,
        last_activity_at=datetime.utcnow() - timedelta(hours=6),
    )
    db = _FakeDB(session)
    db.messages[session.id] = [
        ConversationMessage(session_id=session.id, role="user", content="qual a previsão do tempo pra amanhã em São Paulo?"),
    ]
    manager = PersistentSessionManager(
        db=db,
        embedding_service=_OrthogonalEmbeddings(),
        hybrid_search=None,
        token_counter=None,
    )

    for mode in ("agent", None):
        result = await manager.should_rotate_session(
            session, "monta um script de deploy pro cluster de staging", mode=mode
        )
        assert result["should_rotate"] is False, mode
        assert "só muda quando o usuário muda" in result["reason"]


@pytest.mark.asyncio
async def test_canal_automatico_continua_expirando():
    """A permanência é das superfícies de conversa. Um job de agenda que reusasse
    a mesma sessão para sempre acumularia meses de histórico num turno só."""
    old = _idle_session(hours=24)
    old.service = "neo-scheduler"
    old_id = old.id
    db = _FakeDB(old)

    session = await _manager(db).get_or_create_session(
        session_id=old_id,
        user_id=None,
        service="neo-scheduler",
    )

    assert session.id != old_id
    assert old.status == "closed"


@pytest.mark.asyncio
async def test_live_continua_rotacionando_em_troca_extrema_de_topico():
    """A rotação não foi removida: no live ela é o que encerra a conversa."""
    session = ConversationSession(
        id=uuid4(),
        status="active",
        message_count=20,
        last_activity_at=datetime.utcnow() - timedelta(hours=6),
    )
    db = _FakeDB(session)
    db.messages[session.id] = [
        ConversationMessage(session_id=session.id, role="user", content="qual a previsão do tempo pra amanhã em São Paulo?"),
    ]
    manager = PersistentSessionManager(
        db=db,
        embedding_service=_OrthogonalEmbeddings(),
        hybrid_search=None,
        token_counter=None,
    )

    result = await manager.should_rotate_session(
        session, "monta um script de deploy pro cluster de staging", mode="live"
    )

    assert result["should_rotate"] is True
    assert result["similarity"] == 0.0


@pytest.mark.asyncio
async def test_rotated_session_continuity_injects_previous_history():
    old_id = uuid4()
    new_session = ConversationSession(
        id=uuid4(),
        status="active",
        message_count=0,
        extra_metadata={"created_after_rotated_session_id": str(old_id)},
    )
    old_session = ConversationSession(
        id=old_id,
        status="closed",
        message_count=8,
        last_activity_at=datetime.utcnow() - timedelta(minutes=30),
    )
    db = _FakeDB(old_session)
    db.sessions[new_session.id] = new_session
    db.histories[old_id] = [
        {"role": "user", "content": "diminui 2 graus o ar do quarto", "token_count": 10},
        {"role": "assistant", "content": "Pronto, diminui para 22°C.", "token_count": 8},
    ]

    result = await _manager(db).resolve_continuity_context(
        session=new_session,
        message="como tu chegou nessa conta de 22?",
        user_id=None,
        service="neo-desktop",
    )

    assert result["action"] == "inject"
    assert result["source_session_id"] == str(old_id)
    assert result["reason"] == "rotated_session_continuity"


@pytest.mark.asyncio
async def test_expired_session_continuity_injects_previous_history():
    old_id = uuid4()
    new_session = ConversationSession(
        id=uuid4(),
        status="active",
        message_count=0,
        extra_metadata={"created_after_expired_session_id": str(old_id)},
    )
    old_session = ConversationSession(
        id=old_id,
        status="closed",
        message_count=2,
        last_activity_at=datetime.utcnow() - timedelta(hours=2),
    )
    db = _FakeDB(old_session)
    db.sessions[new_session.id] = new_session
    db.histories[old_id] = [
        {"role": "user", "content": "Vamos ajustar o pipeline de memória V2", "token_count": 10},
        {"role": "assistant", "content": "Certo, vou analisar a sessão.", "token_count": 8},
    ]

    result = await _manager(db).resolve_continuity_context(
        session=new_session,
        message="continua",
        user_id=None,
        service="neo-desktop",
    )

    assert result["action"] == "inject"
    assert result["source_session_id"] == str(old_id)
    assert result["history"][0]["content"] == "Vamos ajustar o pipeline de memória V2"


@pytest.mark.asyncio
async def test_new_chat_short_followup_does_not_silently_inherit_context():
    old_session = ConversationSession(
        id=uuid4(),
        status="closed",
        message_count=2,
        last_activity_at=datetime.utcnow() - timedelta(minutes=10),
        topic_summary="pipeline de memória V2",
    )
    new_session = ConversationSession(id=uuid4(), status="active", message_count=0)
    db = _FakeDB(old_session)
    db.sessions[new_session.id] = new_session

    result = await _manager(db).resolve_continuity_context(
        session=new_session,
        message="continua",
        user_id=None,
        service="neo-desktop",
    )

    assert result["action"] == "none"


@pytest.mark.asyncio
async def test_get_history_enriches_last_assistant_turn_with_tool_trace():
    """session-7b0956de: o modelo escreveu 3 arquivos, prometeu zipar e não
    terminou o turno; o follow-up ("cade os arquivos?") não sabia de nada disso
    e refez tudo do zero. A nota técnica injetada aqui é o que fecha essa lacuna
    — sem tocar no `content` que o chat mostra."""
    session_id = uuid4()
    db = _FakeDB()
    db.histories[session_id] = [
        {"role": "user", "content": "gera um zip do portfolio", "token_count": 10},
        {
            "role": "assistant",
            "content": "Três arquivos prontos. Agora vou zipar tudo e já te mando o arquivo.",
            "token_count": 12,
            "extra_metadata": {
                "tool_trace": [
                    {"tool": "neo_write_file", "ok": True, "path": "/workspace/index.html", "bytes": 1811},
                    {"tool": "neo_write_file", "ok": True, "path": "/workspace/style.css", "bytes": 761},
                    {"tool": "neo_write_file", "ok": True, "path": "/workspace/script.js", "bytes": 1175},
                ],
            },
        },
    ]

    history = await _manager(db).get_history(session_id, current_embedding=None)

    assert history[0]["content"] == "gera um zip do portfolio"
    # O texto salvo/exibido continua intacto — a nota vem DEPOIS, anexada.
    assert history[1]["content"].startswith(
        "Três arquivos prontos. Agora vou zipar tudo e já te mando o arquivo."
    )
    assert "/workspace/index.html (1811 bytes)" in history[1]["content"]
    assert "/workspace/style.css (761 bytes)" in history[1]["content"]
    assert "/workspace/script.js (1175 bytes)" in history[1]["content"]
    assert "não repita isso ao usuário" in history[1]["content"]


@pytest.mark.asyncio
async def test_get_history_skips_note_when_no_tool_trace():
    session_id = uuid4()
    db = _FakeDB()
    db.histories[session_id] = [
        {"role": "user", "content": "oi", "token_count": 2},
        {"role": "assistant", "content": "E aí.", "token_count": 3, "extra_metadata": None},
    ]

    history = await _manager(db).get_history(session_id, current_embedding=None)

    assert history[1]["content"] == "E aí."


# ── Janela de histórico: orçamento, elisão e resgate semântico ──────────
# Regressão da sessão 7330e40c (docs/context-recall-fixes.md §2). Uma resposta
# longa do Neo (3.982 tokens) não cabia no orçamento de 3.000 e, pior, o `break`
# que a rejeitava levava junto TODA a conversa mais antiga. O usuário passou 13
# turnos pedindo aquele texto de volta enquanto o modelo jurava que ele não
# existia.


def _big(role: str, tokens: int, marker: str) -> dict:
    return {"role": role, "content": f"{marker} " + ("x " * tokens), "token_count": tokens}


@pytest.mark.asyncio
async def test_mensagem_gigante_nao_apaga_o_resto_da_conversa():
    """O texto que não cabe vira marcador; o que veio ANTES dele sobrevive."""
    session_id = uuid4()
    db = _FakeDB()
    db.histories[session_id] = [
        {"role": "user", "content": "primeira pergunta", "token_count": 10},
        {"role": "assistant", "content": "primeira resposta", "token_count": 10},
        _big("assistant", 5000, "REANALISE"),
        {"role": "user", "content": "e agora", "token_count": 10},
        {"role": "assistant", "content": "resposta recente", "token_count": 10},
    ]

    history = await _manager(db).get_history(session_id, max_tokens_budget=1000)

    textos = [m["content"] for m in history]
    # O gigante não cabe — mas as mensagens ANTERIORES a ele continuam lá.
    assert any("primeira pergunta" in t for t in textos)
    assert any("primeira resposta" in t for t in textos)
    # E o buraco é explícito, não silencioso: é o silêncio que fazia o modelo
    # concluir que a mensagem nunca existiu.
    assert any("OMITIDA desta janela" in t for t in textos)
    assert any("conversation_recall" in t for t in textos)


@pytest.mark.asyncio
async def test_orcamento_grande_cabe_a_resposta_longa_inteira():
    """Com o teto atual, uma resposta longa do Neo entra inteira na janela."""
    session_id = uuid4()
    db = _FakeDB()
    db.histories[session_id] = [
        {"role": "user", "content": "reanalisa tudo", "token_count": 127},
        _big("assistant", 3982, "REANALISE"),
        {"role": "user", "content": "e depois", "token_count": 39},
        {"role": "assistant", "content": "segue", "token_count": 560},
    ]

    history = await _manager(db).get_history(session_id)

    assert any("REANALISE" in m["content"] for m in history)
    assert not any("OMITIDA" in m["content"] for m in history)


@pytest.mark.asyncio
async def test_mensagem_mais_recente_entra_mesmo_estourando():
    """Turno sem histórico nenhum é pior que turno acima do orçamento."""
    session_id = uuid4()
    db = _FakeDB()
    db.histories[session_id] = [_big("user", 9999, "ENORME")]

    history = await _manager(db).get_history(session_id, max_tokens_budget=100)

    assert len(history) == 1
    assert "ENORME" in history[0]["content"]


class _SimilarDB(_FakeDB):
    """Fake que responde ao resgate semântico dentro da sessão."""

    def __init__(self, similar):
        super().__init__()
        self._similar = similar
        self.similar_calls = 0

    async def fetch_similar_session_messages(self, session_id, embedding, limit, min_similarity):
        self.similar_calls += 1
        return self._similar


@pytest.mark.asyncio
async def test_resgate_semantico_roda_quando_ha_embedding():
    """O bloco existia e NUNCA rodou: o chamador passava current_embedding=None.

    Aqui ele é exercitado de verdade — é o segundo passe do ai_service.
    """
    session_id = uuid4()
    resgatada = {"role": "assistant", "content": "REANALISE COMPLETA", "token_count": 3982}
    db = _SimilarDB(similar=[resgatada])
    db.sessions[session_id] = ConversationSession(
        id=session_id, status="active", started_at=datetime.utcnow(),
        last_activity_at=datetime.utcnow(), message_count=40,
    )
    db.histories[session_id] = [
        {"role": "user", "content": "e agora", "token_count": 10},
        {"role": "assistant", "content": "resposta recente", "token_count": 10},
    ]

    manager = PersistentSessionManager(
        db=db, embedding_service=_AvailableEmbeddings(), hybrid_search=None, token_counter=None,
    )
    history = await manager.get_history(session_id, current_embedding=[0.1, 0.2])

    assert db.similar_calls == 1
    assert any("REANALISE COMPLETA" in m["content"] for m in history)


@pytest.mark.asyncio
async def test_resgate_semantico_nao_roda_sem_embedding():
    """Sem embedding o caminho é o de sempre — sem custo extra de query."""
    session_id = uuid4()
    db = _SimilarDB(similar=[{"role": "assistant", "content": "X", "token_count": 1}])
    db.histories[session_id] = [{"role": "user", "content": "oi de novo", "token_count": 3}]

    await _manager(db).get_history(session_id)

    assert db.similar_calls == 0
