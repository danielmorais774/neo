"""Runner de jobs ``kind="deep_read"`` — monta os adapters do DeepReader.

Mesmo desenho do ``runner_research``: ``core/documents`` é puro e este arquivo é
quem conhece banco, LLMRouter, File Store e caderno. O JobManager recebe
:func:`build_deep_read_runner` fechado sobre as dependências (docs/deep-read-plan.md §3).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from db.models.agent_job import AgentJob
from shared import Message, MessageRole

logger = logging.getLogger(__name__)

#: Padrão do slot quando o painel não tem linha para ele: a carteira viva
#: (neo-llm-slots-and-wallets), com o pin de endpoint que o cérebro já usa.
DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash@fireworks"

#: Uma chamada de leitura presa não pode segurar a vaga do semáforo para sempre.
_LLM_CALL_TIMEOUT_S = 180
#: Progresso de "parte lida" mais frequente que isto é só escrita no banco.
_PROGRESS_MIN_INTERVAL_S = 3.0


class _LLMAdapter:
    """ReaderLLM: JSON, temperatura baixa e sem raciocínio (a resposta É o JSON)."""

    def __init__(self, provider: Any, model: str | None) -> None:
        self.provider = provider
        self.model = model

    async def complete(self, system: str, user: str, *, max_tokens: int) -> str:
        resp = await asyncio.wait_for(
            self.provider.complete(
                messages=[
                    Message(role=MessageRole.SYSTEM, content=system),
                    Message(role=MessageRole.USER, content=user),
                ],
                model=self.model,
                temperature=0.1,
                max_tokens=max_tokens,
                # Raciocínio ligado come o teto e devolve content vazio
                # (openrouter-reasoning-default-on, deepseek-empty-response).
                disable_thinking=True,
                response_format={"type": "json_object"},
            ),
            timeout=_LLM_CALL_TIMEOUT_S,
        )
        return resp.get_content() or ""


class _ProgressAdapter:
    """ReaderProgress: ``progress_note``/heartbeat no job + nota rolante do caderno."""

    def __init__(self, *, db: Any, job: AgentJob, spec: Any, working_memory: Any) -> None:
        self.db = db
        self.job = job
        self.spec = spec
        self.wm = working_memory
        self._last = 0.0

    async def update(self, docs: list, note: str, *, milestone: bool) -> None:
        loop = asyncio.get_running_loop()
        if not milestone and loop.time() - self._last < _PROGRESS_MIN_INTERVAL_S:
            return
        self._last = loop.time()
        try:
            await self.db.update_agent_job(
                self.job.id, progress_note=note[:500], heartbeat_at=datetime.utcnow()
            )
        except Exception as exc:  # noqa: BLE001 — progresso é observabilidade
            logger.warning("[DeepRead] progress_note falhou: %s", exc)
        if milestone:
            await self._note(docs)

    async def _note(self, docs: list) -> None:
        """Nota ``leitura-<pasta>`` substituída a cada marco: endereço e status, nada mais."""
        if self.wm is None or self.job.user_id is None:
            return
        from core.documents.render import render_memory_note

        corpo, resumo = render_memory_note(self.spec, docs, datetime.utcnow())
        pasta = self.spec.out_dir.rstrip("/").rsplit("/", 1)[-1]
        try:
            await self.wm.write(
                user_id=self.job.user_id,
                session_id=self.job.session_id,
                slug=f"leitura-{pasta}"[:64],
                title=f"Leitura: {self.spec.title}"[:120],
                content=corpo,
                summary=resumo,
                mode="replace",
                origin="auto",
            )
        except Exception as exc:  # noqa: BLE001 — caderno nunca derruba a leitura
            logger.warning("[DeepRead] caderno não atualizou: %s", exc)


def build_deep_read_runner(
    *,
    db: Any,
    llm_router: Any,
    sandbox: Any,
    get_file_store: Callable[[], Any] = lambda: None,
    get_working_memory: Callable[[], Any] = lambda: None,
    timeout_s: int = 3600,
    doc_concurrency: int = 3,
    llm_concurrency: int = 6,
    chunk_chars: int = 16_000,
    max_parts_per_doc: int = 150,
    max_ocr_pages: int = 60,
) -> Callable[[AgentJob], Awaitable[dict]]:
    """Fecha as dependências e devolve ``run_deep_read_job(job) -> dict``."""

    async def _llm() -> _LLMAdapter:
        """Slot ``document_reader`` do painel, resolvido POR JOB (troca sem restart).

        Provider sem chave configurada cai no slot do cérebro — melhor ler com o
        modelo do Neo do que morrer em "Provider não disponível" minutos depois.
        """
        from modules.model_config import get_model_config

        cfg = await get_model_config(
            db, "document_reader", fallback_provider=DEFAULT_PROVIDER, fallback_model=DEFAULT_MODEL
        )
        try:
            return _LLMAdapter(llm_router.get_provider(cfg["provider"]), cfg.get("model"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DeepRead] slot document_reader indisponível (%s) — usando o do cérebro", exc)
            brain = await get_model_config(db, "cognitive")
            return _LLMAdapter(llm_router.get_provider(brain["provider"]), brain.get("model"))

    async def run_deep_read_job(job: AgentJob) -> dict:
        from core.documents import DeepReader, ReadSpec, render_result_summary

        spec_json = job.spec_json or {}
        spec = ReadSpec(
            title=job.title or "Leitura de documentos",
            goal=str(spec_json.get("goal") or job.title or ""),
            questions=[str(q) for q in spec_json.get("questions") or [] if str(q).strip()],
            instructions=str(spec_json.get("instructions") or ""),
            files=[f for f in spec_json.get("files") or [] if isinstance(f, dict)],
            out_dir=str(spec_json.get("out_dir") or ""),
            job_id=str(job.id),
            extend_of=str(spec_json.get("extend_of") or ""),
        )
        if not spec.files or not spec.questions or not spec.out_dir:
            return {"status": "failed", "result_summary": "Leitura sem arquivos, perguntas ou pasta."}

        store = get_file_store()

        async def stager(file_id: str) -> tuple[str, str]:
            if store is None:
                raise RuntimeError("o catálogo de arquivos está desligado")
            ref = await store.stage(file_id, target="workbench")
            entry = await store.get(file_id)
            return str(ref.get("path") or ""), str(getattr(entry, "name", "") or "")

        try:
            await db.update_agent_job(
                job.id,
                timeout_at=datetime.utcnow() + timedelta(seconds=timeout_s),
                heartbeat_at=datetime.utcnow(),
                progress_note=f"Preparando a leitura de {len(spec.files)} documento(s)",
            )
        except Exception:  # noqa: BLE001
            pass

        reader = DeepReader(
            spec=spec,
            sandbox=sandbox,
            llm=await _llm(),
            progress=_ProgressAdapter(db=db, job=job, spec=spec, working_memory=get_working_memory()),
            stager=stager,
            doc_concurrency=doc_concurrency,
            llm_concurrency=llm_concurrency,
            chunk_chars=chunk_chars,
            max_parts_per_doc=max_parts_per_doc,
            max_ocr_pages=max_ocr_pages,
        )
        status_final: str | None = None
        try:
            report = await asyncio.wait_for(reader.run(), timeout=timeout_s)
        except asyncio.TimeoutError:
            status_final = "timeout"
            report = await reader.finish(interrupted="tempo esgotado")
        except asyncio.CancelledError:
            # job_cancel: o que já foi lido fica gravado e a síntese diz o que
            # faltou. Blindado porque a task está sendo cancelada agora.
            try:
                await asyncio.shield(reader.finish(interrupted="cancelada"))
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[DeepRead] job %s quebrou", job.id)
            return {"status": "failed", "result_summary": f"A leitura falhou antes de começar: {exc}"}

        resumo = render_result_summary(spec, report.docs, synthesis_bytes=report.synthesis_bytes)
        if status_final == "timeout":
            resumo = f"[tempo esgotado após {timeout_s // 60} min — resultado parcial]\n{resumo}"
        return {"status": status_final or report.status, "result_summary": resumo}

    return run_deep_read_job
