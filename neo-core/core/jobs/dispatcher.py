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

#: Quanto do resumo o redator chega a LER, quando o job não declarou `entrega`.
#:
#: O par com o teto de saída (`max_tokens=700`, ~1.470 caracteres de prosa em
#: pt-BR) é o defeito em si: entra mais do que sai, e a diferença é comprimida
#: por um modelo sem deixar registro. Medido em 227 jobs de prod, este corte
#: disparou UMA vez — o problema nunca foi o corte, era a compressão. Quem
#: declara `entrega` não passa por aqui; para os outros, o corte agora LOGA.
_COMPOSE_ENTRADA_MAX = 3500

#: Idem no caminho sem redator. Exposição medida: 27 de 227 jobs passam disto.
_FALLBACK_ENTRADA_MAX = 1200

#: Acima disto a mensagem redigida está perto do teto de 700 tokens, o que quer
#: dizer que o redator está TRANSPORTANDO conteúdo em vez de redigir. É a medida
#: que diz quais `kind` ainda precisam declarar a entrega em duas metades.
_COMPOSE_SAIDA_ALERTA = 1400


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

        # A ordem é a regra: o modelo escreve, o código entrega. Primeiro a
        # prosa (`_compose`), depois o bloco íntegro (`_com_itens`), depois as
        # imagens (`_append_images`) — as duas últimas por fora do modelo.
        text = self._append_images(job, self._com_itens(await self._compose(job), job))

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

    @staticmethod
    def _entrega(job: AgentJob) -> tuple[str, list[str]]:
        """A entrega em duas metades: o que se REDIGE e o que vai ÍNTEGRO.

        `(fecho, itens)`, e `("", [])` para quem não declarou nada — aí vale o
        caminho de sempre, com o resumo inteiro indo ao redator.

        **Por que existe.** O `_compose` lia até 3.500 caracteres e escrevia no
        máximo ~1.470 (700 tokens de prosa), então todo resultado entre os dois
        números era COMPRIMIDO por um modelo — sem aviso, sem log, sem registro
        do que caiu. Para lista de fatos (preço, número de pedido, nome de
        arquivo, caminho, uuid de retomada) parafrasear é a operação errada: o
        modelo derruba um item, arredonda um valor ou amacia um status, e isso
        chega ao usuário com cara de resposta certa.

        Medido em 227 jobs de prod: o corte em 3.500 disparou UMA vez, e 27 jobs
        passaram de 1.200. O problema nunca foi o corte — era a compressão.

        **O princípio já era da casa**, em dois lugares deste arquivo: o
        `texto_literal` da agenda entrega sem passar pelo redator, e o
        `_append_images` pendura as imagens DEPOIS dele, porque "um LLM
        redigindo no seu tom de sempre reescreve, resume ou come um token
        estranho no meio do texto". Isto é a mesma regra, em grão mais fino: o
        modelo escreve, o código entrega.
        """
        entrega = (job.extra_metadata or {}).get("entrega")
        if not isinstance(entrega, dict):
            return "", []
        fecho = str(entrega.get("fecho") or "").strip()
        itens = [str(i).strip() for i in (entrega.get("itens") or []) if str(i).strip()]
        return fecho, itens

    def _com_itens(self, texto: str, job: AgentJob) -> str:
        """Cola o bloco íntegro DEPOIS do redator — como o `_append_images`.

        Fora do `_compose` de propósito, e é a metade que importa do conserto: o
        bloco não entra na chamada ao modelo, então não existe caminho pelo qual
        ele possa ser reescrito. Se entrasse com a instrução "copie exatamente",
        estaríamos de volta ao defeito de origem — pedir a um modelo que
        transporte fatos que já estão guardados (job 5ab98119).
        """
        _, itens = self._entrega(job)
        if not itens:
            return texto
        # Duas famílias, e o runner já as entrega nesta ordem: o que foi
        # COLETADO (vira lista) e os RODAPÉS que o próprio runner redigiu com
        # glifo na frente (aba, downloads, retomada, diário). Juntar as duas num
        # bloco só faz o rodapé parecer item da lista.
        marcas = ("-", "•", "⏸", "⬇", "⏳", "📸", "(")
        achados = [f"- {i}" for i in itens if not i.startswith(marcas)]
        rodapes = [i for i in itens if i.startswith(marcas)]
        partes = ["\n".join(achados)] if achados else []
        if rodapes:
            partes.append("\n".join(rodapes))
        corpo = "\n\n".join(partes)
        return f"{texto.rstrip()}\n\n{corpo}" if texto.strip() else corpo

    async def _compose(self, job: AgentJob) -> str:
        """Redige a mensagem do usuário a partir do resultado do job.

        Devolve SÓ a prosa. O bloco íntegro é colado pelo `_com_itens`, no
        `dispatch`, depois desta chamada.
        """
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
            if job.status == "completed":
                aviso = ""
            elif job.status == "waiting":
                # Parada de propósito, esperando o usuário (o card no aparelho ou
                # a pergunta do clique). "Diga o que falhou" fazia o redator abrir
                # com "A tarefa falhou" em cima de um pedido de liberação que
                # estava funcionando (sessão 9b2e29e2).
                aviso = (
                    "\nATENÇÃO: a tarefa está PAUSADA esperando o usuário — não falhou "
                    "e não terminou. É PROIBIDO dizer que falhou, que deu certo ou que "
                    "enviou. Diga onde parou e o que ele precisa fazer para ela "
                    "continuar.\n"
                )
            else:
                aviso = (
                    f"\nATENÇÃO: a tarefa NÃO foi concluída (estado {job.status}). "
                    "É PROIBIDO dizer que deu certo, que baixou, que enviou ou que "
                    "terminou. Diga o que falhou e onde parou.\n"
                )
            fecho, itens = self._entrega(job)
            if fecho or itens:
                # O redator NÃO vê os itens. É isso que torna a paráfrase
                # impossível em vez de proibida: ele não pode reescrever um
                # texto que nunca recebeu.
                plural = "linha" if len(itens) == 1 else "linhas"
                bloco = (
                    f"\nUm bloco de {len(itens)} {plural} com o que foi coletado vai "
                    "ANEXADO ao final da sua mensagem, íntegro, por fora de você. "
                    "Não o reescreva, não o resuma e não repita os itens: você não "
                    "os recebeu. Escreva só a abertura e o fecho.\n"
                    if itens
                    else ""
                )
                corpo = fecho or "(sem resumo)"
            else:
                if len(summary) > _COMPOSE_ENTRADA_MAX:
                    # Até hoje este corte era mudo. Ver `_entrega`: o kind que
                    # aparecer aqui é candidato a declarar a entrega em duas
                    # metades, como o `browser_local` já faz.
                    logger.warning(
                        "[Dispatch] resumo do job %s (kind=%s) tem %d caracteres e "
                        "vai CORTADO em %d para o redator — fatos podem não chegar",
                        job.id, job.kind, len(summary), _COMPOSE_ENTRADA_MAX,
                    )
                bloco = ""
                corpo = summary[:_COMPOSE_ENTRADA_MAX] or "(sem saída)"
            user_ctx = (
                f"Tarefa: {job.title}\n"
                f"Detalhe: {detail}\n"
                f"Estado final: {job.status}\n"
                f"{aviso}{bloco}"
                f"Resultado/logs (pode estar truncado):\n{corpo}"
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
            if len(text) > _COMPOSE_SAIDA_ALERTA:
                # O LADO DA SAÍDA, que é onde a compressão acontece de fato: a
                # entrada podia trazer 3.500 caracteres e os 700 tokens só
                # comportam ~1.470. Prosa longa aqui quer dizer que o redator
                # está transportando conteúdo em vez de redigir — e é o sinal de
                # que este `kind` precisa declarar a entrega em duas metades.
                logger.warning(
                    "[Dispatch] mensagem redigida do job %s (kind=%s) tem %d "
                    "caracteres, perto do teto de 700 tokens — o redator está "
                    "transportando conteúdo",
                    job.id, job.kind, len(text),
                )
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

    @classmethod
    def _fallback_text(cls, job: AgentJob, summary: str) -> str:
        """A mensagem quando não há redator — e ela nunca pode perder fato.

        Este é o caminho em que a entrega em duas metades vale MAIS: o modelo
        redator falhou, veio vazio ou nem existe, e é justamente aí que o
        `[:1200]` cortava. Com `entrega` declarada, a cabeça vem fixa, o fecho
        vem inteiro, e os itens são colados pelo `_com_itens` depois — então o
        corte deixa de existir neste caminho em vez de ser afrouxado.
        """
        if job.status == "completed":
            head = f"✅ Terminei: {job.title}."
        elif job.status == "timeout":
            head = f"⏱️ A tarefa \"{job.title}\" estourou o tempo limite e foi interrompida."
        elif job.status == "cancelled":
            head = f"🚫 Cancelei a tarefa \"{job.title}\"."
        elif job.status == "waiting":
            head = f"⏸ A tarefa \"{job.title}\" parou esperando você."
        else:
            head = f"❌ A tarefa \"{job.title}\" falhou."
        fecho, itens = cls._entrega(job)
        if fecho or itens:
            # Os itens NÃO entram aqui: quem os cola é o `_com_itens`, uma vez
            # só, nos dois caminhos. Juntar nos dois duplicaria a lista.
            return f"{head}\n\n{fecho}".rstrip() if fecho else head
        if summary and len(summary) > _FALLBACK_ENTRADA_MAX:
            logger.warning(
                "[Dispatch] fallback do job %s (kind=%s) corta o resumo de %d "
                "caracteres em %d — este kind devia declarar `entrega`",
                job.id, job.kind, len(summary), _FALLBACK_ENTRADA_MAX,
            )
        tail = f"\n\n{summary[:_FALLBACK_ENTRADA_MAX]}" if summary else ""
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
