"""Persistent Session Manager — sessões com topic detection e memória híbrida.

Responsabilidades:
- Criar / recuperar / fechar sessões
- Detectar mudanças de tópico via embeddings → auto-rotacionar
- Recuperar contexto de sessões anteriores via busca híbrida
- Contagem de tokens ao salvar mensagens
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING
from uuid import UUID

from core.session.embedding_service import EmbeddingService
from db.async_client import AsyncPostgresClient
from db.models.conversation import ConversationMessage, ConversationSession
from shared.config import get_settings
from shared.types import MemorySearchResult

if TYPE_CHECKING:
    from core.agent.token_counter import TokenCounter
    from core.memory.hybrid_search import HybridMemorySearch

logger = logging.getLogger(__name__)


def _int_setting(settings: object, nome: str, default: int) -> int:
    """Lê um setting numérico tolerando mock, None e versão antiga sem o campo.

    O chunking roda no caminho de `add_message`, que é o turno do usuário: um
    `getattr` que devolva algo não-comparável (um MagicMock nos testes, um
    Settings em cache sem o campo novo) não pode virar TypeError e derrubar o
    salvamento da mensagem.
    """
    valor = getattr(settings, nome, default)
    # `int(MagicMock())` devolve 1 (o mock implementa __int__), o que ligava o
    # chunking para qualquer texto acima de 1 char. Checar o tipo de verdade é
    # o que distingue "campo ausente" de "campo com valor".
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return default
    return int(valor)


def _flag(settings: object, nome: str, default: bool) -> bool:
    """Idem para flags booleanas."""
    valor = getattr(settings, nome, default)
    return default if not isinstance(valor, bool) else valor

#: Superfícies em que a conversa é DO USUÁRIO: ele abre, ele troca (Novo chat,
#: seletor de sessões) e nada mais mexe nisso. Prefixos porque o mesmo cliente
#: aparece com sufixo (`neo-desktop`, `neo-desktop-tauri`). Ver
#: ``PersistentSessionManager._is_user_owned_chat``.
_CHAT_SERVICE_PREFIXES = ("neo-desktop", "desktop-chat", "neo-panel")

# Quantas mensagens do assistente (as mais recentes) ganham a nota técnica —
# só o turno imediatamente anterior importa pra continuidade de curto prazo
# ("cade os arquivos?"); memória de longo prazo é papel do PKS, não daqui.
_TOOL_TRACE_ENRICH_LAST_N_ASSISTANT = 2
_TOOL_TRACE_NOTE_MAX_CHARS = 800


def _render_tool_trace_note(extra_metadata: dict[str, Any] | None) -> str | None:
    """Resumo compacto do que as tools fizeram num turno, pro PRÓXIMO turno.

    Nunca é mostrado ao usuário (o chat sempre mostra só ``content``) — isso só
    entra na lista de mensagens montada pro LLM, em get_history(). Sem isso, um
    follow-up curto ("cade os arquivos?") não sabe que 3 arquivos já foram
    escritos e o modelo refaz tudo do zero com dados inventados diferentes.
    """
    if not extra_metadata:
        return None
    trace = extra_metadata.get("tool_trace") or []
    files = extra_metadata.get("files") or []
    if not trace and not files:
        return None
    lines = [
        "[nota técnica automática do turno anterior — não é algo que você disse; "
        "use como contexto do que já foi feito, não repita isso ao usuário]"
    ]
    for entry in trace[:8]:
        tool = entry.get("tool", "?")
        if not entry.get("ok", True):
            lines.append(f"- {tool}: falhou ({entry.get('error', '?')})")
        elif tool == "neo_write_file":
            lines.append(f"- {tool} → {entry.get('path')} ({entry.get('bytes')} bytes)")
        elif tool == "neo_bash":
            lines.append(f"- {tool} → `{entry.get('command')}` (exit={entry.get('exit_code')})")
        elif tool == "file_save":
            lines.append(f"- {tool} → '{entry.get('name')}' salvo no catálogo")
        else:
            lines.append(f"- {tool}: {str(entry.get('summary', ''))[:120]}")
    for f in files[:5]:
        lines.append(f"- entregue ao usuário: '{f.get('name')}' (file_id={f.get('file_id')})")
    return "\n".join(lines)[:_TOOL_TRACE_NOTE_MAX_CHARS]


#: Texto já sem acento e em minúsculas (``_normalize_context_text``).
_DEICTIC_FOLLOWUP_RE = re.compile(
    r"\b(la|ali|de novo|novamente|outra vez|o mesmo|a mesma|"
    r"(esse|este|aquele|o) arquivo|(essa|esta|aquela|nessa|naquela|a) pasta|"
    r"tent[ae] (de novo|novamente|salvar|abrir|mandar|colocar|enviar|baixar|rodar|gravar|editar))\b"
)


class PersistentSessionManager:
    """Gerencia sessões com rotação automática e recuperação de contexto."""

    def __init__(
        self,
        db: AsyncPostgresClient,
        embedding_service: EmbeddingService,
        hybrid_search: HybridMemorySearch | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.db = db
        self.embeddings = embedding_service
        self.hybrid_search = hybrid_search
        self.token_counter = token_counter
        self.settings = get_settings()
        # Referências fortes das tasks de embedding em background (ver
        # _spawn_embedding_backfill).
        self._embedding_tasks: set[asyncio.Task[None]] = set()

    # ---------------------------------------------------------------
    # Session lifecycle
    # ---------------------------------------------------------------

    async def get_or_create_session(
        self,
        session_id: UUID | None = None,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
        mode: str | None = None,
    ) -> ConversationSession:
        """Obtém sessão ativa existente ou cria uma nova.

        ``mode`` é o canal do turno (``"live"``, ``"agent"``, ...) e decide o que
        acontece com uma sessão parada há tempo.

        **No chat a sessão que o desktop pediu é a sessão que ele recebe.** Ela só
        muda quando o USUÁRIO muda (Novo chat, seletor de sessões) — nem relógio
        nem status fecham a porta. Antes de 2026-09-18 não era assim: o backstop de
        inatividade (12h) forkava uma sessão nova, e pior, ``_is_expired`` devolve
        ``True`` para QUALQUER sessão com ``status != "active"``, então uma sessão
        fechada por outro caminho (o relógio de 30min do live num desvio por voz,
        uma rotação de tópico) nunca chegava no ramo de reativação logo abaixo —
        voltar a escrever nela sempre criava outra. Era isto que o usuário via como
        "ele rotacionou a sessão sozinha depois de um tempo parado".

        O resumo/embedding de tópico, que é o que o backstop existia para produzir
        (alimenta a busca entre sessões), continua saindo: ``_refresh_topic_summary``
        faz a parte útil do ``close_session`` sem encerrar nada.

        No **live** o relógio curto continua valendo: lá uma pausa longa quer mesmo
        dizer que a conversa acabou (e mantém ruído de teste de microfone fora das
        sessões boas).
        """
        session: ConversationSession | None = None

        if session_id:
            session = await self.db.get_conversation_session(session_id)
            if session:
                if not self._is_user_owned_chat(service, mode) and self._is_expired(
                    session, mode=mode
                ):
                    logger.info(f"Sessão {session.id} expirada por inatividade")
                    await self.close_session(session.id, reason="inactivity")
                    session = await self.db.create_conversation_session(
                        user_id=user_id,
                        service=service,
                        metadata={
                            "created_after_expired_session_id": str(session_id),
                            "created_after_expiration_reason": "inactivity",
                        },
                    )
                    logger.info(
                        "Nova sessão criada após expiração: %s (anterior=%s)",
                        session.id,
                        session_id,
                    )
                    return session

                mudou = False
                # Passou do backstop: arquiva o TÓPICO (resumo + embedding), não a
                # conversa. É o que o `close_session` tinha de útil aqui.
                if self._chat_backstop_due(session):
                    mudou = await self._refresh_topic_summary(session)
                # Sessão fechada por qualquer motivo volta a viver: o desktop pediu
                # por ela nominalmente, então é nela que a conversa continua.
                if session.status != "active":
                    logger.info(f"Reativando sessão {session.id} (status={session.status})")
                    session.status = "active"
                    session.ended_at = None
                    session.last_activity_at = datetime.utcnow()
                    mudou = True
                if mudou:
                    await self.db.update_session(session)
                return session
            else:
                # Desktop sent a new session_id (e.g. after "Novo chat") — create with that id
                logger.info(f"Session {session_id} not in DB, creating new session with given session_id")
                session = await self.db.create_conversation_session(
                    session_id=session_id,
                    user_id=user_id,
                    service=service,
                )
                logger.info(f"Nova sessão criada: {session.id}")
                return session

        # No session_id provided — fallback to active session
        session = await self.db.get_active_session(user_id=user_id, service=service)

        if session and self._is_expired(session, mode=mode):
            logger.info(f"Sessão {session.id} expirada por inatividade")
            await self.close_session(session.id, reason="inactivity")
            session = None

        if not session:
            session = await self.db.create_conversation_session(
                user_id=user_id, service=service,
            )
            logger.info(f"Nova sessão criada: {session.id}")

        return session

    async def close_session(self, session_id: UUID, reason: str = "manual") -> None:
        """Fecha sessão e gera resumo/embedding do tópico."""
        session = await self.db.get_conversation_session(session_id)
        if not session or session.status != "active":
            return

        messages = await self.db.fetch_session_messages(session_id, limit=50)
        if messages and self.embeddings.available:
            summary = self._build_topic_summary(messages)
            session.topic_summary = summary
            try:
                session.topic_embedding = await self.embeddings.embed(summary)
            except Exception as e:
                logger.warning(f"Falha ao gerar embedding do tópico: {e}")

        session.status = "closed"
        session.ended_at = datetime.utcnow()
        session.extra_metadata = {**(session.extra_metadata or {}), "close_reason": reason}
        await self.db.update_session(session)
        logger.info(f"Sessão {session_id} fechada (motivo={reason})")

    @staticmethod
    def _is_user_owned_chat(service: str | None, mode: str | None) -> bool:
        """A sessão é do USUÁRIO neste canal — ele abre, ele troca, ninguém mais?

        Vale para as superfícies de conversa (desktop, painel). NÃO vale para o
        live, onde a pausa longa encerra mesmo, nem para canais automáticos como
        a agenda/cron: um job diário que reusasse a MESMA sessão para sempre
        acumularia meses de histórico dentro de um turno só.

        Canal desconhecido cai no comportamento antigo (expira) de propósito.
        """
        if mode == "live":
            return False
        canal = (service or "").lower()
        return canal.startswith(_CHAT_SERVICE_PREFIXES)

    def _chat_backstop_due(self, session: ConversationSession) -> bool:
        """A sessão está parada há mais que o backstop longo do chat?

        Não decide mais se ela ACABA (no chat ela não acaba sozinha) — decide só
        se vale regerar o resumo do tópico. ``0`` desliga.
        """
        minutes = self.settings.session_inactivity_chat_minutes
        if minutes <= 0 or not session.last_activity_at:
            return False
        return session.last_activity_at < datetime.utcnow() - timedelta(minutes=minutes)

    async def _refresh_topic_summary(self, session: ConversationSession) -> bool:
        """Gera `topic_summary`/`topic_embedding` SEM encerrar a sessão.

        É a metade do ``close_session`` que a memória entre sessões precisa. A
        outra metade — fechar — é justamente o que o chat não quer: o usuário
        volta horas depois e continua o mesmo assunto.

        Devolve se algo mudou (o chamador é quem grava).
        """
        messages = await self.db.fetch_session_messages(session.id, limit=50)
        if not messages or not self.embeddings.available:
            return False
        session.topic_summary = self._build_topic_summary(messages)
        try:
            session.topic_embedding = await self.embeddings.embed(session.topic_summary)
        except Exception as e:
            logger.warning(f"Falha ao gerar embedding do tópico: {e}")
        session.extra_metadata = {
            **(session.extra_metadata or {}),
            "topic_summary_at": datetime.utcnow().isoformat(),
        }
        logger.info("Resumo de tópico regerado para a sessão %s (sem encerrá-la)", session.id)
        return True

    # ---------------------------------------------------------------
    # Message management
    # ---------------------------------------------------------------

    async def add_message(
        self,
        session_id: UUID,
        role: str,
        content: str,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        generate_embedding: bool = False,
        user_id: UUID | None = None,
        mission_id: UUID | None = None,
    ) -> ConversationMessage:
        """Adiciona mensagem com contagem de tokens e embedding opcional."""
        embedding = None
        if generate_embedding and self.embeddings.available and role in ("user", "assistant"):
            try:
                embedding = await self.embeddings.embed(content)
            except Exception as e:
                logger.warning(f"Falha ao gerar embedding: {e}")

        # Contagem de tokens
        token_count = None
        if self.token_counter:
            token_count = self.token_counter.count(content)

        msg = await self.db.save_message(
            session_id=session_id,
            role=role,
            content=content,
            embedding=embedding,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            metadata=metadata,
            token_count=token_count,
            user_id=user_id,
            mission_id=mission_id,
        )

        # Resposta do Neo sem embedding: preenche DEPOIS, fora do caminho da
        # resposta. Todos os ~20 pontos que salvam `assistant` passam
        # `generate_embedding=False`, e o resultado medido era 0 de 1520
        # respostas embeddadas — metade da conversa invisível para a busca
        # vetorial ("o que EU te respondi sobre X" nunca casava). Fazer inline
        # custaria dezenas de ms de CPU no turno; em background custa zero.
        if (
            embedding is None
            and role == "assistant"
            and getattr(self.settings, "assistant_embeddings_enabled", True)
            and self.embeddings.available
        ):
            self._spawn_embedding_backfill(msg.id, content)

        # Mensagem longa: indexar os pedaços também, sempre em background (o
        # embedding de N pedaços custa CPU e não pode entrar no caminho da
        # resposta). Vale para user e assistant: a pergunta longa também some
        # da busca quando só os 256 primeiros tokens viram vetor.
        #
        # Tudo aqui é best-effort e envolto em try: indexação não pode derrubar
        # o salvamento da mensagem, que é o caminho crítico do turno.
        try:
            if role in ("user", "assistant") and _flag(
                self.settings, "embedding_chunk_enabled", True
            ):
                minimo = _int_setting(
                    self.settings, "embedding_chunk_min_chars", 1000
                )
                if len(content or "") > minimo:
                    task = asyncio.create_task(
                        self.index_message_chunks(
                            msg.id, session_id, user_id, content
                        )
                    )
                    self._embedding_tasks.add(task)
                    task.add_done_callback(self._embedding_tasks.discard)
        except Exception as e:  # noqa: BLE001
            logger.warning("Agendamento do chunking falhou: %s", e)

        return msg

    def _spawn_embedding_backfill(self, message_id: UUID, content: str) -> None:
        """Agenda o embedding de uma mensagem já salva (fire-and-forget)."""
        task = asyncio.create_task(self._embed_saved_message(message_id, content))
        # Sem guardar a referência, o event loop pode coletar a task no meio do
        # caminho (asyncio só mantém weakrefs) e o embedding some em silêncio.
        self._embedding_tasks.add(task)
        task.add_done_callback(self._embedding_tasks.discard)

    async def _embed_saved_message(self, message_id: UUID, content: str) -> None:
        try:
            embedding = await self.embeddings.embed(content)
            await self.db.set_message_embedding(message_id, embedding)
        except Exception as e:  # noqa: BLE001 — embedding tardio nunca afeta o turno
            logger.warning("Embedding em background falhou (%s): %s", message_id, e)

    async def index_message_chunks(
        self,
        message_id: UUID,
        session_id: UUID,
        user_id: UUID | None,
        content: str,
    ) -> int:
        """Indexa os PEDAÇOS de uma mensagem longa.

        O embedder local trunca em 256 tokens sem avisar, então o vetor da
        linha só cobre a abertura de um texto longo. Cada pedaço cabe na
        janela e ganha vetor próprio; `find_similar_messages` faz UNION dos
        dois e devolve a mensagem. Best-effort: falhar aqui nunca afeta o
        turno — no pior caso a busca fica como era antes.
        """
        st = self.settings
        if not _flag(st, "embedding_chunk_enabled", True):
            return 0
        if not self.embeddings.available:
            return 0
        minimo = _int_setting(st, "embedding_chunk_min_chars", 1000)
        if len(content or "") <= minimo:
            return 0
        try:
            from core.memory.chunking import chunk_text

            pedacos = chunk_text(
                content,
                size=_int_setting(st, "embedding_chunk_chars", 500),
                overlap=_int_setting(st, "embedding_chunk_overlap", 250),
            )
            if len(pedacos) < 2:
                return 0
            vetores = await self.embeddings.embed_batch(pedacos)
            pares = [
                (p, v) for p, v in zip(pedacos, vetores) if v is not None
            ]
            gravados = await self.db.replace_message_chunks(
                message_id, session_id, user_id, pares
            )
            logger.info(
                "[EMBEDDING] %s pedaços indexados para a mensagem %s (%s chars)",
                gravados, message_id, len(content),
            )
            return gravados
        except Exception as e:  # noqa: BLE001 — nunca derruba o turno
            logger.warning("Chunking falhou (%s): %s", message_id, e)
            return 0

    async def get_history(
        self,
        session_id: UUID,
        limit: int | None = None,
        max_tokens_budget: int | None = None,
        current_embedding: list[float] | None = None,
    ) -> list[dict[str, str]]:
        """Obtém histórico filtrado e com orçamento de tokens para o LLM.

        1. Busca as últimas `limit` mensagens (user/assistant, sem tool calls, >5 chars).
        2. Aplica orçamento de tokens: mantém mensagens mais recentes até atingir o limite.
        3. Mensagem grande demais para o que sobrou do orçamento não corta mais a
           conversa inteira: ela vira um MARCADOR de omissão e a varredura segue
           para trás (ver `_apply_token_budget`).
        4. Se um embedding da mensagem atual chegar, mescla por similaridade as
           mensagens da própria sessão que não couberam na janela.

        Retorna lista de {role, content} (sem token_count — limpo para o LLM).
        """
        limit = limit if limit is not None else self.settings.history_limit_messages
        if max_tokens_budget is None:
            max_tokens_budget = self.settings.history_max_tokens_budget

        raw_history = await self.db.fetch_session_history(session_id, limit=limit)

        if not raw_history:
            return []

        budgeted = self._apply_token_budget(raw_history, max_tokens_budget)

        # --- Resgate semântico DENTRO da sessão ---
        if current_embedding is not None:
            budgeted = await self._merge_relevant_session_messages(
                session_id=session_id,
                budgeted=budgeted,
                current_embedding=current_embedding,
            )

        return self._render_history(budgeted)

    # ── Janela de histórico — orçamento e resgate ─────────────────────

    def _count_tokens(self, msg: dict[str, Any]) -> int:
        tc = msg.get("token_count", 0) or 0
        if tc == 0 and self.token_counter:
            tc = self.token_counter.count(msg.get("content") or "")
        return tc

    def _apply_token_budget(
        self,
        raw_history: list[dict[str, Any]],
        max_tokens_budget: int,
    ) -> list[dict[str, Any]]:
        """Mantém as mensagens mais recentes que cabem no orçamento.

        O `break` que existia aqui era o defeito central da sessão 7330e40c: ao
        topar com uma mensagem maior que o orçamento restante, a varredura
        PARAVA, e com ela ia embora todo o resto da conversa — não só a mensagem
        grande. Com orçamento de 3.000 tokens e uma resposta de 3.982, isso
        significava que o próprio texto que o usuário estava pedindo era
        inalcançável para sempre, e o modelo, sem saber que havia um buraco,
        concluía que a mensagem nunca existiu e improvisava.

        Agora a mensagem que não cabe vira um marcador explícito e a varredura
        continua para trás. O marcador custa ~40 tokens e faz duas coisas que o
        silêncio não fazia: preserva o contexto mais antigo e AVISA o modelo de
        que falta algo ali, com a instrução de buscar em vez de deduzir.
        """
        budgeted: list[dict[str, Any]] = []
        remaining = max_tokens_budget
        elisions = 0
        max_elisions = self.settings.history_max_elisions

        for msg in reversed(raw_history):
            tc = self._count_tokens(msg)

            if tc <= remaining:
                remaining -= tc
                budgeted.append(msg)
                continue

            if not budgeted:
                # A mais recente entra mesmo estourando: um turno sem histórico
                # nenhum é pior que um turno acima do orçamento.
                budgeted.append(msg)
                remaining = 0
                continue

            if elisions >= max_elisions:
                break

            elisions += 1
            marker = self._elision_marker(msg, tc)
            marker_cost = self._count_tokens(marker)
            if marker_cost > remaining:
                break
            remaining -= marker_cost
            budgeted.append(marker)

        budgeted.reverse()
        return budgeted

    @staticmethod
    def _elision_marker(msg: dict[str, Any], tokens: int) -> dict[str, Any]:
        """Placeholder de uma mensagem que não coube — ver `_apply_token_budget`."""
        quem = "sua" if msg.get("role") == "assistant" else "do usuário"
        primeira_linha = (msg.get("content") or "").strip().splitlines()
        titulo = primeira_linha[0][:80] if primeira_linha else ""
        content = (
            f"[mensagem {quem} de ~{tokens} tokens OMITIDA desta janela por tamanho"
            + (f" — começava com “{titulo}…”" if titulo else "")
            + ". O texto continua salvo na conversa. Se o usuário se referir a "
            "ela, BUSQUE com `conversation_recall` — não deduza o conteúdo nem "
            "substitua por um arquivo do disco.]"
        )
        return {"role": msg.get("role", "assistant"), "content": content, "token_count": 0}

    async def _merge_relevant_session_messages(
        self,
        session_id: UUID,
        budgeted: list[dict[str, Any]],
        current_embedding: list[float],
    ) -> list[dict[str, Any]]:
        """Traz de volta, por similaridade, o que não coube na janela.

        Este bloco existia e era CÓDIGO MORTO: ficava atrás de
        ``if current_embedding is not None`` e o único chamador de produção
        passava ``None`` cravado, porque o embedding da mensagem só nasce depois
        do `add_message` que roda em paralelo com o `get_history`. Nunca rodou
        uma vez em produção. Agora o `ai_service` faz um segundo passe depois
        que o embedding existe, e o piso de tamanho da sessão caiu de >30 para
        `history_semantic_merge_min_messages` — 30 mensagens é mais do que a
        maioria das conversas tem quando já começou a perder contexto.
        """
        if not self.embeddings.available:
            return budgeted

        try:
            session = await self.db.get_conversation_session(session_id)
            msg_count = session.message_count if session else 0
        except Exception:  # noqa: BLE001 — sem contagem, não arrisca o turno
            return budgeted

        if msg_count < self.settings.history_semantic_merge_min_messages:
            return budgeted

        limite = self.settings.history_semantic_merge_limit
        if limite <= 0:
            return budgeted

        try:
            similar = await self.db.fetch_similar_session_messages(
                session_id=session_id,
                embedding=current_embedding,
                limit=limite,
                min_similarity=self.settings.history_semantic_merge_min_similarity,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[HISTORY] Embedding relevance merge failed: {e}")
            return budgeted

        existing_contents = {m.get("content") for m in budgeted}
        new_relevant = [
            s for s in similar if s.get("content") not in existing_contents
        ]
        if not new_relevant:
            return budgeted

        logger.info(
            "[HISTORY] Resgatadas %d mensagens antigas da sessão %s por similaridade",
            len(new_relevant),
            session_id,
        )
        return new_relevant + budgeted

    def _render_history(self, budgeted: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Corta para {role, content} e anexa a nota técnica de tools.

        A nota (tools executadas, arquivos criados) vai SÓ no content mandado
        pro LLM — o texto salvo no banco, que é o que o chat mostra, nunca é
        tocado, porque a enriquização acontece aqui e não em `add_message`.
        """
        result: list[dict[str, str]] = []
        enriched = 0
        for m in reversed(budgeted):
            content = m["content"]
            if m["role"] == "assistant" and enriched < _TOOL_TRACE_ENRICH_LAST_N_ASSISTANT:
                note = _render_tool_trace_note(m.get("extra_metadata"))
                if note:
                    content = f"{content}\n\n{note}"
                    enriched += 1
            result.append({"role": m["role"], "content": content})
        result.reverse()
        return result

    # ---------------------------------------------------------------
    # Topic change detection
    # ---------------------------------------------------------------

    async def should_rotate_session(
        self,
        session: ConversationSession,
        new_message: str,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Detecta mudança de tópico via similaridade de embeddings.

        **Só vale no live.** No chat a sessão é do usuário: ela troca quando ele
        troca (Novo chat, seletor), e não quando um cosseno passa de 0,45. O
        gatilho real era "parado há mais que o cooldown + mensagem que o
        embedding achou diferente" — ou seja, exatamente voltar de uma pausa e
        mudar de assunto, que num chat é o comportamento NORMAL, não o fim da
        conversa. Rotacionar ali custava o histórico inteiro do follow-up
        seguinte (f33632b4 → 9b0b10cc) e ainda arrastava junto o pendente do
        gRPC. Ver [[neo-topic-rotation-hijack]].

        Conservative approach (live): only rotate on extreme topic changes after
        a significant number of messages AND a cooldown period.

        Returns a dict with rotation details:
            should_rotate (bool), similarity (float|None), threshold (float),
            reason (str), message_count (int), min_required (int),
            cooldown_minutes (int), elapsed_minutes (float|None),
            context_text (str), duration_ms (float)
        """
        import time as _time
        t0 = _time.monotonic()

        base = {
            "should_rotate": False,
            "similarity": None,
            "threshold": self.settings.session_topic_similarity_threshold,
            "reason": "",
            "message_count": session.message_count,
            "min_required": getattr(self.settings, 'session_rotation_min_messages', 8),
            "cooldown_minutes": getattr(self.settings, 'session_rotation_cooldown_minutes', 5),
            "elapsed_minutes": None,
            "context_text": "",
            "new_message": new_message,
            "duration_ms": 0,
        }

        if self._is_user_owned_chat(session.service, mode):
            base["reason"] = (
                f"canal {session.service or 'chat'} — a sessão só muda quando o usuário muda"
            )
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        if not self.embeddings.available:
            base["reason"] = "embeddings indisponíveis"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        min_msgs = base["min_required"]
        if session.message_count < min_msgs:
            base["reason"] = f"poucas mensagens ({session.message_count} < {min_msgs})"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        # Short messages (game moves, yes/no, numbers) are likely continuations.
        if len(new_message.strip()) < 30:
            base["reason"] = f"mensagem curta ({len(new_message.strip())} chars < 30)"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        # Referential follow-ups ("como tu chegou nessa conta de 22?") depend on
        # the current topic — rotating would orphan the very context they need.
        # Embeddings can't see this (low lexical overlap), so guard lexically.
        recent_for_dep = await self.db.fetch_session_messages(session.id, limit=6)
        recent_text = " ".join(
            m.content for m in recent_for_dep if m.role in ("user", "assistant")
        )
        dependency = self.detect_context_dependency(new_message, recent_context=recent_text)
        if dependency["is_context_dependent"]:
            base["reason"] = (
                f"follow-up dependente de contexto ({dependency['kind']}) — não rotacionar"
            )
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        # Time-based cooldown: don't rotate if session was active recently.
        cooldown = base["cooldown_minutes"]
        if cooldown > 0 and session.last_activity_at:
            elapsed = (datetime.utcnow() - session.last_activity_at).total_seconds() / 60
            base["elapsed_minutes"] = elapsed
            if elapsed < cooldown:
                base["reason"] = f"cooldown ativo ({elapsed:.1f}min < {cooldown}min)"
                base["duration_ms"] = (_time.monotonic() - t0) * 1000
                logger.debug(f"Topic rotation skipped: session active {elapsed:.1f}min ago (cooldown={cooldown}min)")
                return base

        recent = await self.db.fetch_session_messages(session.id, limit=10)
        if not recent:
            base["reason"] = "sem mensagens recentes para comparar"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

        context_text = " ".join(
            m.content for m in recent[-5:] if m.role in ("user", "assistant")
        )
        base["context_text"] = context_text

        try:
            embeddings = await self.embeddings.embed_batch([context_text, new_message])
            similarity = EmbeddingService.cosine_similarity(embeddings[0], embeddings[1])
            base["similarity"] = similarity
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            logger.debug(f"Similaridade de tópico: {similarity:.3f}")

            if similarity < self.settings.session_topic_similarity_threshold:
                base["should_rotate"] = True
                base["reason"] = f"similaridade {similarity:.4f} < threshold {self.settings.session_topic_similarity_threshold}"
            else:
                base["reason"] = f"similaridade {similarity:.4f} >= threshold {self.settings.session_topic_similarity_threshold}"
            return base
        except Exception as e:
            logger.warning(f"Detecção de tópico falhou: {e}")
            base["reason"] = f"erro: {e}"
            base["duration_ms"] = (_time.monotonic() - t0) * 1000
            return base

    # ---------------------------------------------------------------
    # Context recovery — busca híbrida
    # ---------------------------------------------------------------

    async def recover_context(
        self,
        message: str,
        user_id: UUID | None = None,
        service: str | None = None,
        exclude_session_id: UUID | None = None,
    ) -> list[MemorySearchResult]:
        """Recupera contexto de sessões e mensagens anteriores.

        Usa busca híbrida (vetor + FTS + temporal) se disponível,
        senão fallback para busca vetorial pura.

        ``service=None`` (default) busca em TODOS os canais do usuário. O default
        antigo era ``"neo-desktop"`` e nenhum chamador passava outro, enquanto o
        desktop grava as sessões como ``"neo-desktop-tauri"``: a memória entre
        sessões via 1,8% do corpus. Ver docs/conversation-recall-plan.md §2a.

        ``exclude_session_id`` tira a conversa de AGORA do resultado — esta é a
        camada de memória ENTRE conversas, e a de agora já vai inteira pelo
        histórico. Sem isso a própria pergunta recém-salva casa consigo mesma e
        ganha o primeiro lugar.
        """
        # Busca híbrida
        if self.hybrid_search:
            try:
                return await self.hybrid_search.search(
                    query=message,
                    user_id=user_id,
                    service=service,
                    limit=self.settings.session_history_recovery_limit,
                    exclude_session_id=exclude_session_id,
                )
            except Exception as e:
                logger.warning(f"Busca híbrida falhou, usando fallback: {e}")

        # Fallback: busca vetorial pura
        return await self._vector_only_recovery(
            message, user_id, service, exclude_session_id,
        )

    async def _vector_only_recovery(
        self,
        message: str,
        user_id: UUID | None,
        service: str | None,
        exclude_session_id: UUID | None = None,
    ) -> list[MemorySearchResult]:
        """Fallback: recuperação por vetor puro (comportamento original)."""
        if not self.embeddings.available:
            return []

        try:
            msg_embedding = await self.embeddings.embed(message)
        except Exception as e:
            logger.warning(f"Falha ao gerar embedding para recovery: {e}")
            return []

        similar_sessions = await self.db.find_similar_sessions(
            embedding=msg_embedding,
            user_id=user_id,
            service=service,
            limit=self.settings.session_history_recovery_limit,
        )

        results: list[MemorySearchResult] = []
        for session, similarity in similar_sessions:
            if exclude_session_id is not None and session.id == exclude_session_id:
                continue
            results.append(
                MemorySearchResult(
                    content=session.topic_summary or "",
                    source="session",
                    similarity=similarity,
                    final_score=similarity,
                    session_id=session.id,
                    created_at=session.started_at,
                )
            )
        return results

    # ---------------------------------------------------------------
    # Context continuity - expired-session follow-up recovery
    # ---------------------------------------------------------------

    async def resolve_continuity_context(
        self,
        session: ConversationSession,
        message: str,
        user_id: UUID | None = None,
        service: str = "neo-desktop",
    ) -> dict[str, Any]:
        """Resolve context for elliptical follow-ups after session boundaries.

        This is intentionally conservative: the strongest signal is a new
        session created by core because the desktop-provided session expired.
        A user-created new chat has no such metadata, so it will not silently
        inherit the previous conversation unless the user explicitly asks to
        return to the prior subject and there is only one recent candidate.
        """
        detection = self.detect_context_dependency(message)
        result: dict[str, Any] = {
            "detected": detection,
            "action": "none",
            "confidence": 0.0,
            "history": [],
            "source_session_id": None,
            "candidates": [],
            "reason": detection.get("reason", "not_context_dependent"),
        }
        if not detection["is_context_dependent"]:
            return result

        # A new session created by core (either inactivity expiration or a topic
        # rotation) keeps a link to its predecessor. A context-dependent follow-up
        # right after that boundary must inherit the previous conversation.
        meta = session.extra_metadata or {}
        previous_session_id = meta.get("created_after_expired_session_id") or meta.get(
            "created_after_rotated_session_id"
        )
        if previous_session_id:
            history = await self._history_from_previous_session(previous_session_id)
            if history:
                reason = (
                    "rotated_session_continuity"
                    if meta.get("created_after_rotated_session_id")
                    else "expired_session_continuity"
                )
                result.update(
                    {
                        "action": "inject",
                        "confidence": max(0.9, float(detection["confidence"])),
                        "history": history,
                        "source_session_id": previous_session_id,
                        "reason": reason,
                    }
                )
                return result

        if detection["kind"] != "explicit_return":
            return result

        recent_sessions = await self._recent_context_candidates(
            current_session_id=session.id,
            user_id=user_id,
            service=service,
        )
        candidates = [
            {
                "session_id": str(candidate.id),
                "summary": self._session_candidate_summary(candidate),
                "last_activity_at": candidate.last_activity_at.isoformat(),
            }
            for candidate in recent_sessions
        ]
        result["candidates"] = candidates

        if len(recent_sessions) == 1:
            previous = recent_sessions[0]
            history = await self.get_history(previous.id, limit=12, max_tokens_budget=2000)
            if history:
                result.update(
                    {
                        "action": "inject",
                        "confidence": 0.82,
                        "history": history,
                        "source_session_id": str(previous.id),
                        "reason": "single_recent_explicit_return",
                    }
                )
                return result

        if candidates:
            result.update(
                {
                    "action": "disambiguate",
                    "confidence": float(detection["confidence"]),
                    "reason": "multiple_recent_context_candidates",
                }
            )
        return result

    @classmethod
    def detect_context_dependency(
        cls,
        message: str,
        recent_context: str | None = None,
    ) -> dict[str, Any]:
        """Detect messages that cannot be answered safely without prior context.

        ``recent_context`` (optional) is a concatenation of recent turns used to
        spot numeric back-references (a number in the message that only makes
        sense relative to something said earlier, e.g. "como chegou nessa conta
        de 22?" after the A/C was set to 22°C).
        """
        normalized = cls._normalize_context_text(message)
        compact = " ".join(normalized.split())
        if not compact:
            return {
                "is_context_dependent": False,
                "kind": "empty",
                "confidence": 0.0,
                "reason": "empty_message",
            }

        explicit_markers = (
            "voltando ao assunto",
            "voltando no assunto",
            "continuando o assunto",
            "continuando aquele assunto",
            "sobre o assunto anterior",
            "sobre aquilo que falamos",
            "retomando o assunto",
        )
        if any(marker in compact for marker in explicit_markers):
            return {
                "is_context_dependent": True,
                "kind": "explicit_return",
                "confidence": 0.92,
                "reason": "explicit_return_marker",
            }

        short_commands = {
            "continua",
            "continue",
            "segue",
            "faz isso",
            "refaz",
            "refaca",
            "refaz isso",
            "melhora isso",
            "corrige isso",
            "muda isso",
            "troca isso",
        }
        if compact in short_commands:
            return {
                "is_context_dependent": True,
                "kind": "short_followup",
                "confidence": 0.86,
                "reason": "short_context_command",
            }

        referential_markers = (
            "isso",
            "essa forma",
            "desse jeito",
            "dessa forma",
            "essa parte",
            "esse ponto",
            "aquilo",
            "anterior",
            "resposta anterior",
            "como me passou",
        )
        dissatisfaction_markers = (
            "nao gostei",
            "nao curti",
            "ficou ruim",
            "ta errado",
            "esta errado",
            "nao era isso",
            "pensei melhor",
        )
        if len(compact) <= 180 and (
            any(marker in compact for marker in referential_markers)
            or any(marker in compact for marker in dissatisfaction_markers)
        ):
            return {
                "is_context_dependent": True,
                "kind": "referential_followup",
                "confidence": 0.74,
                "reason": "referential_or_dissatisfaction_marker",
            }

        # Referential questions: an interrogative that points back to an earlier
        # turn via a demonstrative+preposition contraction ("como tu chegou
        # NESSA conta de 22?", "de onde veio ISSO?", "por que DISSO?"). These have
        # low embedding overlap with the prior turns, so they must be caught
        # lexically — otherwise they get misread as a brand-new topic.
        demonstrative_markers = (
            "nessa", "nesse", "nesses", "nessas",
            "desse", "dessa", "desses", "dessas", "disso", "disto",
            "neste", "nesta", "nisto", "nisso",
            "naquele", "naquela", "naquilo",
            "porque disso", "por que disso", "pra que isso",
            "de onde veio", "como voce chegou", "como tu chegou", "como chegou",
        )
        interrogative_starts = (
            "como", "porque", "por que", "pq", "de onde", "quando",
            "onde", "qual", "quanto", "quantos", "quanta", "quantas", "o que", "oq",
        )
        is_question = "?" in compact or compact.startswith(interrogative_starts)
        if len(compact) <= 180 and is_question and any(
            marker in compact for marker in demonstrative_markers
        ):
            return {
                "is_context_dependent": True,
                "kind": "referential_question",
                "confidence": 0.78,
                "reason": "referential_question_demonstrative",
            }

        # Numeric back-reference: a question that cites a number which already
        # appeared in the recent conversation ("...conta de 22?" after 22°C).
        if recent_context and len(compact) <= 180 and is_question:
            msg_numbers = set(re.findall(r"\d+", compact))
            ctx_numbers = set(
                re.findall(r"\d+", cls._normalize_context_text(recent_context))
            )
            if msg_numbers and (msg_numbers & ctx_numbers):
                return {
                    "is_context_dependent": True,
                    "kind": "referential_question",
                    "confidence": 0.72,
                    "reason": "numeric_back_reference",
                }

        # Retomada que aponta para um LUGAR ou OBJETO da conversa, ou repete uma
        # tentativa. Sessão f33632b4: "tenta salvar o arquivo lá agora", depois de 26
        # min parado, deu similaridade 0,398 com a conversa sobre o arquivo — o
        # embedding não enxerga "lá" nem "o arquivo" — e o pedido foi parar numa
        # conversa sobre o clima. Custo de errar aqui é só não rotacionar.
        if len(compact) <= 180 and _DEICTIC_FOLLOWUP_RE.search(compact):
            return {
                "is_context_dependent": True,
                "kind": "deictic_followup",
                "confidence": 0.8,
                "reason": "deictic_or_retry_marker",
            }

        return {
            "is_context_dependent": False,
            "kind": "standalone",
            "confidence": 0.0,
            "reason": "no_context_dependency_marker",
        }

    async def _history_from_previous_session(
        self,
        session_id: str,
    ) -> list[dict[str, str]]:
        try:
            previous_id = UUID(str(session_id))
        except (TypeError, ValueError):
            return []
        return await self.get_history(previous_id, limit=12, max_tokens_budget=2000)

    async def _recent_context_candidates(
        self,
        current_session_id: UUID,
        user_id: UUID | None,
        service: str,
    ) -> list[ConversationSession]:
        fetch_recent = getattr(self.db, "fetch_recent_sessions", None)
        if not fetch_recent:
            return []
        sessions = await fetch_recent(
            user_id=user_id,
            service=service,
            limit=3,
            statuses=["active", "closed"],
            exclude_session_id=current_session_id,
        )
        return [session for session in sessions if session.message_count > 0]

    @staticmethod
    def _normalize_context_text(text: str) -> str:
        normalized = unicodedata.normalize("NFD", text or "")
        without_accents = "".join(
            char for char in normalized if unicodedata.category(char) != "Mn"
        )
        return without_accents.lower().strip()

    @staticmethod
    def _session_candidate_summary(session: ConversationSession) -> str:
        if session.topic_summary:
            return session.topic_summary[:160]
        last_activity = session.last_activity_at.strftime("%d/%m %H:%M")
        return f"Sessão de {last_activity} com {session.message_count} mensagens"

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _is_expired(self, session: ConversationSession, mode: str | None = None) -> bool:
        """Verifica se sessão expirou por inatividade.

        Depois de 2026-09-18 isto NÃO decide mais o destino de uma sessão pedida
        por id no chat (ver ``get_or_create_session``): sobrou para o **live**,
        onde uma pausa longa realmente encerra a conversa e cortar ali mantém o
        ruído de teste de microfone fora das sessões boas, e para o ramo de
        FALLBACK sem id — ali expirar é o que impede `get_active_session` de
        ressuscitar uma conversa de dias atrás (foi assim que "tenta salvar o
        arquivo lá agora" caiu dentro de uma conversa sobre o clima).

        ``mode=None`` (default) é tratado como chat de propósito: os caminhos de
        retomada de turno — tool_result, aprovação de comando — não carregam o
        mode, e expirar a sessão no meio de um turno em andamento seria pior
        ainda do que entre turnos.
        """
        if session.status != "active":
            return True
        minutes = (
            self.settings.session_inactivity_minutes
            if mode == "live"
            else self.settings.session_inactivity_chat_minutes
        )
        if minutes <= 0:
            return False
        return session.last_activity_at < datetime.utcnow() - timedelta(minutes=minutes)

    @staticmethod
    def _build_topic_summary(messages: list[ConversationMessage]) -> str:
        """Resumo conciso do tópico a partir das mensagens."""
        user_msgs = [m.content for m in messages if m.role == "user"]
        if len(user_msgs) <= 3:
            summary_parts = user_msgs
        else:
            summary_parts = [user_msgs[0], user_msgs[len(user_msgs) // 2], user_msgs[-1]]
        return " | ".join(summary_parts)[:500]
