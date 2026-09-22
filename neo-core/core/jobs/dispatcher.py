"""ProactiveDispatcher — entrega o resultado de um job de volta na conversa.

Quando um job atinge um estado terminal (ou um timer dispara), este dispatcher:
 1. redige uma mensagem curta com a persona do Neo a partir do resultado/logs;
 2. persiste como mensagem do assistente na sessão de origem (fica na história,
    inclusive offline);
 3. publica no canal de notificação existente com ``kind="chat_message"`` — reusa
    o transporte do broker + o fan-out por device (replay offline sai de graça).

Nada de RPC/proto novo: o ``payload_json`` já é livre. O cliente (neo-desktop-tauri)
ganha um handler para ``kind="chat_message"`` que injeta a mensagem na timeline (+
TTS no Modo Live). Ver ``docs/workbench-background-jobs-plan.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.jobs.screenshots import image_markers
from db.models.agent_job import AgentJob
from shared import Message, MessageRole

logger = logging.getLogger(__name__)


_COMPOSE_SYSTEM = (
    "Você é o Neo avisando o usuário que uma tarefa que rodava em segundo plano "
    "terminou. Escreva UMA mensagem curta, natural e em primeira pessoa, no seu "
    "tom de sempre, dizendo o que aconteceu e o resultado essencial. Não use "
    "markdown pesado nem cole saídas cruas gigantes; resuma. Se falhou, diga de "
    "forma direta e, se der, o motivo. Não invente dados que não estão no resultado."
    # Sem o nome do usuário no contexto, o modelo preenche o vocativo com o
    # genérico: "Fala, usuário." (job 36a81dd6). Vocativo aqui não serve para
    # nada — é aviso de tarefa, não início de conversa —, então é mais barato
    # proibir do que plumbar o nome até aqui.
    "\nComece pelo fato, sem vocativo e sem cumprimento: nada de "
    "\"Fala\", \"Oi\", \"E aí\" nem de chamar o usuário de \"usuário\"."
    # O resumo do job pode trazer uma instrução do runner sobre o próximo passo
    # (ex.: bloqueio por origem → perguntar sobre o navegador local). Ela é
    # dirigida ao Neo e precisa CHEGAR ao usuário como pergunta.
    "\nSe o resultado disser para perguntar algo ao usuário, faça essa pergunta "
    "na mensagem, em uma frase, junto do motivo."
)


class ProactiveDispatcher:
    """Redige e entrega a resposta proativa de um job concluído."""

    def __init__(
        self,
        *,
        db: Any,
        broker: Any,
        llm_router: Any = None,
        default_model: str | None = None,
        working_memory: Any = None,
    ) -> None:
        self.db = db
        self.broker = broker
        self.llm = llm_router
        self.default_model = default_model
        #: Caderno de trabalho. ``None`` com a flag desligada — e aí o passo 3
        #: do dispatch simplesmente não acontece.
        self.working_memory = working_memory

    async def dispatch(self, job: AgentJob) -> None:
        """Entrega o resultado do ``job`` (idempotente: só entrega uma vez)."""
        if job.notified_at is not None:
            return

        # 0. Caderno de trabalho, ACIMA do corte de `silent`: anotar não é avisar.
        #    Job silencioso não fala com o usuário, mas o trabalho aconteceu — e o
        #    caderno é memória do servidor, não notificação. Ver `_note_result`.
        await self._note_result(job)

        if job.notify_policy == "silent":
            await self._mark_notified(job)
            return

        text = self._append_images(job, await self._compose(job))

        # 1. Persiste na história da sessão de origem (se houver).
        if job.session_id is not None:
            try:
                metadados = {"source": "agent_job", "job_id": str(job.id), "kind": job.kind}
                if (job.extra_metadata or {}).get("files"):
                    # Mesmo formato do turno normal (`_msg_metadata["files"]`).
                    metadados["files"] = job.extra_metadata["files"]
                await self.db.save_message(
                    session_id=job.session_id,
                    role="assistant",
                    content=text,
                    user_id=job.user_id,
                    metadata=metadados,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[Dispatch] persist failed for job %s: %s", job.id, e)

        # 2. Publica no canal de notificação (chat_message).
        if job.notify_policy in ("chat", "both"):
            await self._publish_chat_message(job, text)
        elif job.notify_policy == "notch":
            await self._publish_notch(job, text)

        await self._mark_notified(job)

    # ------------------------------------------------------------------

    async def _note_result(self, job: AgentJob) -> None:
        """Grava o resultado do job no caderno de trabalho do usuário.

        **Por que aqui, e não no cérebro.** O caderno tem uma tool (`wm_write`) e
        uma regra no prompt mandando anotar toda conclusão — e mesmo assim o
        achado de um job nunca chegava lá. Não é desobediência do modelo: o
        `browser_local_task` responde `status: queued` em milissegundos, e o
        resultado real só existe minutos depois, quando este dispatcher o
        entrega com uma chamada de LLM PRÓPRIA (`_compose`). Entre um e outro
        não existe turno cognitivo — o cérebro nunca vê o resultado, então nunca
        teve como anotá-lo.

        Medido na sessão 09cfa763: o job 7c127b3b apurou o número da reclamação
        (258599465), o título, o status "Em réplica", o modelo do monitor
        (Alienware Fast IPS 280 Hz) e a URL exata da página — e o caderno ficou
        com ``{'ok': True, 'job_id': ..., 'status': 'queued'}``. O turno
        seguinte precisou reabrir tudo no navegador para recuperar o que já
        estava apurado.

        Este é, portanto, o ponto onde TODO job (navegador local, headless,
        research, workbench) passa a anotar por si. Best-effort e nunca levanta:
        um caderno que falha não pode impedir a entrega da resposta.
        """
        if self.working_memory is None or job.user_id is None:
            return
        summary = (job.result_summary or "").strip()
        # Nada apurado e nada a explicar: só o `completed` mudo é dispensável —
        # um `failed` sem resumo ainda avisa o próximo turno que não adianta
        # esperar por aquele resultado.
        if not summary and job.status == "completed":
            return
        try:
            from core.working_memory import render_job_entry, render_job_summary

            spec = job.spec_json or {}
            duracao = None
            if job.started_at and job.finished_at:
                duracao = (job.finished_at - job.started_at).total_seconds()
            entry = render_job_entry(
                title=job.title or "(sem título)",
                kind=job.kind or "job",
                status=job.status or "?",
                job_id=str(job.id),
                summary=summary,
                # O que foi PEDIDO ao subagente. Só existe no spec — o
                # `result_summary` conta o que voltou, nunca o que se tentou.
                pedido=str(
                    spec.get("task") or spec.get("goal") or spec.get("command")
                    or spec.get("topic") or ""
                ),
                urls=[u for u in (spec.get("start_url"),) if u],
                arquivos=self._arquivos_do_job(job),
                duracao_s=duracao,
                diario=f"logs/browser_local/{job.id}" if job.kind == "browser_local" else "",
            )
            await self.working_memory.append_auto(
                user_id=job.user_id,
                # Mesma nota do backstop do turno (`sessao-<8>`): a linha "abri o
                # job X" e o resultado dele ficam juntos, na ordem em que
                # aconteceram. Sem session_id o append_auto cai em
                # `trabalho-recente` sozinho.
                session_id=job.session_id,
                entry=entry,
                summary=render_job_summary(
                    title=job.title or "(sem título)",
                    status=job.status or "?",
                    summary=summary,
                ),
            )
        except Exception as e:  # noqa: BLE001 — caderno nunca derruba a entrega
            logger.warning("[Dispatch] caderno não registrou o job %s: %s", job.id, e)

    @staticmethod
    def _arquivos_do_job(job: AgentJob) -> list[str]:
        """Arquivos que o job pôs no disco, com caminho real.

        Vem do mesmo lugar que alimenta o registro de artefatos; aqui entra na
        nota do job para que o caminho fique ao lado do achado que o produziu, e
        não só numa lista solta de downloads recentes.
        """
        spec = job.spec_json or {}
        saida: list[str] = []
        for item in (spec.get("downloaded") or [])[:20]:
            if isinstance(item, dict):
                nome = item.get("name") or "(sem nome)"
                caminho = item.get("path") or item.get("file_id") or ""
                saida.append(f"{nome} · {caminho}".strip(" ·"))
            elif item:
                saida.append(str(item))
        return saida

    async def _compose(self, job: AgentJob) -> str:
        """Redige a mensagem do usuário a partir do resultado do job."""
        # Texto já pronto (agenda): entrega como está. Um alerta de texto fixo
        # reescrito pelo redator vira paráfrase — e a resposta de um turno
        # cognitivo agendado JÁ saiu com a persona certa; passá-la de novo por um
        # modelo menor só a encurta e a piora. Ver docs/scheduler-plan.md §5.
        literal = str((job.extra_metadata or {}).get("texto_literal") or "").strip()
        if literal:
            return literal
        summary = (job.result_summary or "").strip()
        fallback = self._fallback_text(job, summary)
        if self.llm is None:
            return fallback
        try:
            provider = self.llm.get_provider(None)
            spec = job.spec_json or {}
            detail = spec.get("command") or spec.get("goal") or job.title
            # "Estado final: failed" sozinho é fraco demais: o modelo lê o resumo,
            # acha uma frase otimista e escreve "deu tudo certo". A ordem em
            # português, no imperativo, é o que segura.
            aviso = (
                ""
                if job.status == "completed"
                else (
                    f"\nATENÇÃO: a tarefa NÃO foi concluída (estado {job.status}). "
                    "É PROIBIDO dizer que deu certo, que baixou, que enviou ou que "
                    "terminou. Diga o que falhou e onde parou.\n"
                )
            )
            user_ctx = (
                f"Tarefa: {job.title}\n"
                f"Detalhe: {detail}\n"
                f"Estado final: {job.status}\n"
                f"{aviso}"
                f"Resultado/logs (pode estar truncado):\n{summary[:3500] or '(sem saída)'}"
            )
            messages = [
                Message(role=MessageRole.SYSTEM, content=_COMPOSE_SYSTEM),
                Message(role=MessageRole.USER, content=user_ctx),
            ]
            resp = await provider.complete(
                messages=messages,
                model=self.default_model,
                temperature=0.6,
                # O reasoning vem LIGADO por padrão no OpenRouter e consome o
                # orçamento antes de sobrar conteúdo. Este era o único caminho
                # de LLM do sistema sem `disable_thinking` — e o efeito não é
                # resposta vazia, é resposta CORTADA no meio da palavra, que
                # chega ao usuário parecendo bug de outra coisa. Job 36a81dd6:
                # "Fala, usuário. A tarefa ... falhou — o navegador est".
                # Medido em 8 chamadas iguais: 1 truncou com finish=length e
                # 382 dos 400 tokens gastos em reasoning; com o reasoning
                # desligado, 8/8 limpas e 0 tokens de reasoning.
                # A folga de tokens é a segunda linha de defesa, para o caso de
                # algum provider ignorar a intenção.
                max_tokens=700,
                disable_thinking=True,
            )
            text = (resp.get_content() or "").strip()
            return text or fallback
        except Exception as e:  # noqa: BLE001
            logger.warning("[Dispatch] compose LLM failed for job %s: %s", job.id, e)
            return fallback

    @staticmethod
    def _append_images(job: AgentJob, text: str) -> str:
        """Pendura os `[[IMAGE:file:…]]` do job DEPOIS de redigir o texto.

        Fora do ``_compose`` de propósito: o marcador é sintaxe para o cliente, e
        um LLM redigindo "no seu tom de sempre" reescreve, resume ou come um
        token estranho no meio do texto. Passando por fora, a imagem chega
        íntegra — e como o marcador fica no ``content`` salvo, reabrir a sessão
        redesenha os prints (ver docs/file-store-plan.md + imagens inline).
        """
        markers = image_markers((job.extra_metadata or {}).get("images"))
        if not markers:
            return text
        # Se o texto redigido já citou um marcador (o resumo mencionava), não
        # duplica — o parser do cliente dedup por origem, mas o texto ficaria feio.
        novos = [m for m in markers if m.split("|")[0] not in text]
        if not novos:
            return text
        return (text.rstrip() + "\n\n" + "\n".join(novos)).strip()

    @staticmethod
    def _fallback_text(job: AgentJob, summary: str) -> str:
        if job.status == "completed":
            head = f"✅ Terminei: {job.title}."
        elif job.status == "timeout":
            head = f"⏱️ A tarefa \"{job.title}\" estourou o tempo limite e foi interrompida."
        elif job.status == "cancelled":
            head = f"🚫 Cancelei a tarefa \"{job.title}\"."
        else:
            head = f"❌ A tarefa \"{job.title}\" falhou."
        tail = f"\n\n{summary[:1200]}" if summary else ""
        return head + tail

    async def _publish_chat_message(self, job: AgentJob, text: str) -> None:
        notif = await self._create_notification(
            job,
            kind="chat_message",
            title=job.title,
            body=text,
        )
        await self._fanout(job, notif, text)

    async def _publish_notch(self, job: AgentJob, text: str) -> None:
        notif = await self._create_notification(
            job,
            kind="job_update",
            title=job.title,
            body=text[:280],
        )
        await self._fanout(job, notif, text)

    async def _create_notification(
        self, job: AgentJob, *, kind: str, title: str, body: str
    ) -> dict[str, Any]:
        """Cria a linha em user_notification (replay offline) e devolve o dict."""
        payload = {
            "job_id": str(job.id),
            "session_id": str(job.session_id) if job.session_id else "",
            "text": body,
            "kind_detail": job.kind,
            "status": job.status,
        }
        arquivos = [
            {"id": str(f.get("file_id") or f.get("id")), "name": f.get("name"), "sizeBytes": f.get("size_bytes") or 0}
            for f in ((job.extra_metadata or {}).get("files") or [])
            if isinstance(f, dict) and (f.get("file_id") or f.get("id"))
        ]
        if arquivos:
            # A bolha do desktop desenha o botão a partir disto (mesma forma
            # {id, name, sizeBytes} das respostas normais).
            payload["files"] = arquivos
        created_unix = int(datetime.utcnow().timestamp())
        db_notif = None
        if job.user_id is not None:
            try:
                db_notif = await self.db.create_notification(
                    user_id=job.user_id,
                    mission_id=None,
                    kind=kind,
                    title=title,
                    body=body,
                    payload=payload,
                )
                created_unix = (
                    int(db_notif.created_at.timestamp()) if db_notif and db_notif.created_at else created_unix
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[Dispatch] create_notification failed for job %s: %s", job.id, e)
        return {
            "id": str(db_notif.id) if db_notif else str(job.id),
            "user_id": str(job.user_id) if job.user_id else "",
            "mission_id": "",
            "kind": kind,
            "title": title,
            "body": body,
            "description": body,
            "payload": payload,
            "created_at_unix": created_unix,
        }

    async def _fanout(self, job: AgentJob, notif: dict[str, Any], text: str) -> None:
        """Envia a notificação pelos dois canais existentes (broker + por device)."""
        if job.user_id is not None and self.broker is not None:
            try:
                await self.broker.publish(job.user_id, notif)
            except Exception as e:  # noqa: BLE001
                logger.warning("[Dispatch] broker publish failed for job %s: %s", job.id, e)

        # Fan-out por device WS (mesmo caminho que o desktop usa hoje).
        try:
            from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

            if job.user_id is not None:
                broadcaster = get_device_notif_broadcaster()
                devices = await self.db.get_connected_devices(job.user_id)
                sent: set[str] = set()
                for device in devices:
                    for key in (device.device_id, getattr(device, "ws_connection_id", None)):
                        if key and key not in sent:
                            if await broadcaster.send_notification(key, notif):
                                sent.add(key)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Dispatch] device fan-out failed for job %s: %s", job.id, e)

    async def _mark_notified(self, job: AgentJob) -> None:
        try:
            await self.db.update_agent_job(job.id, notified_at=datetime.utcnow())
        except Exception as e:  # noqa: BLE001
            logger.warning("[Dispatch] mark notified failed for job %s: %s", job.id, e)
