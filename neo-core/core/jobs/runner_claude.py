"""Runner de jobs ``kind="claude_code"`` — monta o orquestrador com o corpo do job.

O orquestrador (``core/claude_code/``) é puro: não conhece banco, device nem
LLMRouter. Este arquivo é quem conhece, e entra no orquestrador por ganchos
(docs/claude-code-delegation-plan.md §2.3).

O estado da tarefa mora em ``spec_json["claude"]`` e é regravado a cada mudança;
as mensagens do Daniel no meio da tarefa chegam em
``extra_metadata["claude_mensagens"]`` (o ``claude_reply`` só acrescenta, e o
orquestrador guarda quantas já leu). Colunas separadas de propósito: o runner
regrava o spec sem pisar numa mensagem que chegou no mesmo instante.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from db.models.agent_job import AgentJob

logger = logging.getLogger(__name__)

#: Progresso mais frequente que isto é só escrita no banco.
_PROGRESSO_MIN_S = 3.0
#: Arquivo recolhido para a continuação: o `then` lê com os tetos de sempre.
_RECOLHER_MAX = 12
#: Diário: só as tarefas mais recentes ficam em disco, e cada uma tem teto.
_DIARIOS_MANTIDOS = 20
_DIARIO_MAX_BYTES = 256 * 1024
#: A saída de um comando fica alinhada sob o texto do passo ("HH:MM:SS  ").
_RECUO = " " * 10
#: O que vai para o `progress_note` (a faixa do chat): uma linha, curta.
_NOTA_DA_FAIXA_MAX = 300


class Diario:
    """O que o Claude está fazendo, uma linha por passo — para a janela Tarefas.

    Mora no disco do core (``logs/claude_code/<job>/trace.md``, o mesmo lugar do
    diário do navegador local), e o `job.log_path` aponta para cá: a rota de log
    e a tool `job_logs` já sabem ler o FIM de um diário local. Não vai para o chat
    nem para o prompt do Neo. Guarda o que o Claude disse e rodou, inteiro, e a
    saída dos comandos com começo e fim (`stream.py`): no job 8f83b37c a versão
    curta escondia o fim do resumo e um `rm` no fim de um comando. Não guarda
    conteúdo de arquivo.
    """

    def __init__(self, raiz: str, job_id: str) -> None:
        self.dir: Path | None = None
        self._bytes = 0
        if not raiz:
            return
        try:
            base = Path(raiz)
            base.mkdir(parents=True, exist_ok=True)
            self.dir = base / job_id
            self.dir.mkdir(exist_ok=True)
            arquivo = self.dir / "trace.md"
            self._bytes = arquivo.stat().st_size if arquivo.exists() else 0
            _podar(base, manter=self.dir)
        except OSError as exc:
            logger.warning("[claude] diário desligado (%s)", exc)
            self.dir = None

    def escrever(self, linha: str) -> None:
        if self.dir is None or self._bytes > _DIARIO_MAX_BYTES:
            return
        bruto = linha.removeprefix("Claude: ")
        cabeca, _, saida = bruto.partition("\n")
        if saida and cabeca.startswith("↳"):
            # A saída de um comando mantém as linhas, recuadas sob o passo.
            corpo = " ".join(cabeca.split())
            texto = f"{datetime.now():%H:%M:%S}  {corpo}\n" + "".join(
                f"{_RECUO}│ {parte.rstrip()}\n" for parte in saida.splitlines()
            )
        else:
            # Uma linha por passo: o resumo do fim vem com parágrafos.
            corpo = " ".join(bruto.split())
            texto = f"{datetime.now():%H:%M:%S}  {corpo}\n"
        if self._bytes + len(texto) > _DIARIO_MAX_BYTES:
            texto = "(diário cortado: passou do teto; o resto está no relatório)\n"
        try:
            with (self.dir / "trace.md").open("a", encoding="utf-8") as fh:
                fh.write(texto)
            self._bytes += len(texto.encode("utf-8"))
        except OSError as exc:  # noqa: PERF203 — diário nunca derruba a tarefa
            logger.warning("[claude] diário não gravou: %s", exc)
            self.dir = None


def _podar(base: Path, *, manter: Path) -> None:
    """Fica com os ``_DIARIOS_MANTIDOS`` diários mais recentes (o atual sempre)."""
    pastas = sorted(
        (p for p in base.iterdir() if p.is_dir() and p != manter),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for velha in pastas[_DIARIOS_MANTIDOS - 1:]:
        shutil.rmtree(velha, ignore_errors=True)


def build_claude_runner(
    *,
    db: Any,
    llm_router: Any,
    get_file_store: Callable[[], Any] = lambda: None,
    modelo_padrao: str = "sonnet",
    max_turnos: int = 60,
    limites: Any = None,
    diario_dir: str = "",
    modo_permissao: str = "auto",
) -> Callable[[AgentJob], Awaitable[dict]]:
    """Fecha as dependências e devolve ``run_claude_job(job) -> dict``."""

    async def _completar(sistema: str, usuario: str, max_tokens: int) -> str:
        """O supervisor usa o modelo do cérebro, resolvido POR JOB (troca sem restart)."""
        from modules.model_config import get_model_config
        from shared.types import Message, MessageRole

        cfg = await get_model_config(db, "cognitive")
        provider = llm_router.get_provider(cfg["provider"])
        resposta = await asyncio.wait_for(
            provider.complete(
                messages=[
                    Message(role=MessageRole.SYSTEM, content=sistema),
                    Message(role=MessageRole.USER, content=usuario),
                ],
                model=cfg.get("model"),
                temperature=0.1,
                max_tokens=max_tokens,
                # Raciocínio ligado come o teto e devolve content vazio
                # (deepseek-empty-response-reasoning).
                disable_thinking=True,
                response_format={"type": "json_object"},
            ),
            timeout=90,
        )
        return resposta.get_content() or ""

    async def run_claude_job(job: AgentJob) -> dict:
        from core.claude_code import (
            CorpoLocal,
            Ganchos,
            Limites,
            Orquestrador,
            SupervisorLLM,
            Tarefa,
        )
        from core.notifications.device_control import get_device_control_broadcaster

        spec = dict(job.spec_json or {})
        if str(spec.get("corpo") or "local") != "local":
            return {
                "status": "failed",
                "result_summary": "O Claude no servidor ainda não existe (é a F2 do plano); só no Mac.",
            }
        device_id = str(spec.get("device_id") or "").strip()
        if not device_id:
            return {"status": "failed", "result_summary": "Tarefa do Claude sem o Mac de destino."}
        corpo = CorpoLocal(get_device_control_broadcaster(), device_id, modo_permissao=modo_permissao)

        tarefa = Tarefa(
            objetivo=str(spec.get("objetivo") or job.title or ""),
            contexto=str(spec.get("contexto") or ""),
            entregaveis=[str(e) for e in spec.get("entregaveis") or [] if str(e).strip()],
            pasta=str(spec.get("pasta") or ""),
            modelo=str(spec.get("modelo") or modelo_padrao),
            esforco=str(spec.get("esforco") or ""),
            max_rodadas=int(spec.get("max_rodadas") or 3),
            max_turnos=max_turnos,
            instrucao_inicial=str(spec.get("instrucao_inicial") or ""),
            liberadas=[str(r) for r in spec.get("liberadas") or [] if str(r).strip()],
            titulo=job.title,
        )
        estado: dict[str, Any] = dict(spec.get("claude") or {})
        estado.setdefault("session_id", str(uuid.uuid4()))
        estado.setdefault("tarefa_id", str(spec.get("tarefa_id") or job.id))

        ultimo: dict[str, Any] = {"progresso": 0.0, "batida": 0.0, "pendente": ""}
        diario = Diario(diario_dir, str(job.id))
        if diario.dir is not None and not job.log_path:
            try:
                await db.update_agent_job(job.id, log_path=str(diario.dir)[:512])
            except Exception as exc:  # noqa: BLE001 — sem log_path a tela só não mostra
                logger.warning("[claude] log_path não gravou: %s", exc)
        if not estado.get("rodada"):
            onde = "Mac do Daniel" if str(spec.get("corpo") or "local") == "local" else "servidor"
            diario.escrever(f"Tarefa: {tarefa.objetivo}")
            diario.escrever(f"Onde: {onde} · pasta {tarefa.pasta or '(nova)'} · modelo {tarefa.modelo}")
            if tarefa.instrucao_inicial:
                diario.escrever(f"Continuação: {tarefa.instrucao_inicial}")

        async def salvar(e: dict) -> None:
            # Relê o spec antes de gravar: o verificador de promessa pode ter
            # pendurado um `then` nele com o job rodando (`_then_pendurado`).
            fresco = await db.get_agent_job(job.id)
            base = dict((fresco.spec_json if fresco else None) or spec)
            await db.update_agent_job(job.id, spec_json={**base, "claude": dict(e)})

        async def gravar_nota(nota: str) -> None:
            ultimo["progresso"] = ultimo["batida"] = time.monotonic()
            ultimo["pendente"] = ""
            try:
                await db.update_agent_job(
                job.id, progress_note=nota[:_NOTA_DA_FAIXA_MAX], heartbeat_at=datetime.utcnow()
            )
            except Exception as exc:  # noqa: BLE001 — progresso é observabilidade
                logger.warning("[claude] progress_note falhou: %s", exc)

        async def progresso(nota: str) -> None:
            # O diário recebe TODA nota; o banco, no máximo uma a cada 3 s. A
            # pulada fica PENDENTE e sai na próxima volta: sem isso a última nota
            # do banco era a de antes ("rodando") e a que importava ("sem
            # permissão"), no mesmo segundo, se perdia.
            diario.escrever(nota)
            if nota.removeprefix("Claude: ").startswith("↳"):
                return  # a saída do comando é do diário; a faixa mostra o passo
            if time.monotonic() - ultimo["progresso"] < _PROGRESSO_MIN_S:
                ultimo["pendente"] = nota
                return
            await gravar_nota(nota)

        async def mensagens() -> list[dict]:
            if ultimo["pendente"] and time.monotonic() - ultimo["progresso"] >= _PROGRESSO_MIN_S:
                await gravar_nota(ultimo["pendente"])
            fresco = await db.get_agent_job(job.id)
            agora = time.monotonic()
            if agora - ultimo["batida"] > 30:
                # Pensando sem chamar ferramenta não gera nota; a batida segue.
                ultimo["batida"] = agora
                try:
                    await db.update_agent_job(job.id, heartbeat_at=datetime.utcnow())
                except Exception:  # noqa: BLE001
                    pass
            return list(((fresco.extra_metadata if fresco else None) or {}).get("claude_mensagens") or [])

        orq = Orquestrador(
            corpo=corpo,
            tarefa=tarefa,
            estado=estado,
            ganchos=Ganchos(salvar=salvar, progresso=progresso, mensagens=mensagens),
            supervisor=SupervisorLLM(_completar),
            limites=limites or Limites(),
            tag=str(job.id),
        )
        try:
            desfecho = await orq.executar()
        except asyncio.CancelledError:
            # job_cancel: o processo no Mac não morre sozinho com a task do core.
            run_id = orq.run_atual
            if run_id:
                try:
                    await asyncio.shield(corpo.parar(run_id))
                    await asyncio.shield(corpo.limpar(run_id))
                except Exception:  # noqa: BLE001
                    pass
            raise

        # Inteiro: o corte em 400 caía em cima de "Não testei o modo de dois
        # jogadores nem o reinício" (job 8f83b37c).
        diario.escrever(f"Fim: {desfecho.status} — {desfecho.resumo}")
        for recusa in desfecho.recusas:
            diario.escrever(f"  pedindo permissão para {recusa.descricao()}")
        arquivos = []
        if desfecho.status == "completed" and str(spec.get("then") or "").strip():
            arquivos = await _recolher(corpo, job, desfecho)
        # O `_settle` SOBRESCREVE o extra_metadata: tem que partir do que está no
        # banco agora, senão as mensagens que o Daniel mandou durante a tarefa somem.
        fresco = await db.get_agent_job(job.id)
        meta = dict((fresco.extra_metadata if fresco else None) or job.extra_metadata or {})
        return _terminal(job, spec, device_id, desfecho, arquivos, meta)

    async def _recolher(corpo: Any, job: AgentJob, desfecho: Any) -> list[dict]:
        """Traz os arquivos do relatório para o File Store — só quando há `then`.

        Sem continuação ninguém vai ler o conteúdo, e copiar arquivo do Mac para
        a VPS sem motivo é exatamente o que o Daniel não quer
        (feedback-no-conversation-copies-on-vps).
        """
        store = get_file_store()
        if store is None or not desfecho.arquivos:
            return []
        try:
            recolhidos = await corpo.recolher(desfecho.cwd, desfecho.arquivos[:_RECOLHER_MAX])
        except Exception as exc:  # noqa: BLE001 — sem os arquivos, o relatório ainda vale
            logger.warning("[claude] não recolhi os arquivos do job %s: %s", job.id, exc)
            return []
        saida: list[dict] = []
        for arq in recolhidos:
            conteudo = arq.get("conteudo")
            if not isinstance(conteudo, str):
                continue
            nome = str(arq.get("nome") or arq.get("path") or "arquivo.txt").rsplit("/", 1)[-1]
            try:
                guardado = await store.ingest_bytes(
                    conteudo.encode("utf-8"),
                    name=nome,
                    origin="job",
                    mime="text/plain",
                    user_id=job.user_id,
                    session_id=job.session_id,
                    retention="temp",
                    source={"kind": "claude_code", "job_id": str(job.id), "path": arq.get("path")},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[claude] %s não entrou no File Store: %s", nome, exc)
                continue
            saida.append({
                "file_id": str(guardado.id),
                "path": str(arq.get("path") or nome),
                "truncado": bool(arq.get("truncado")),
            })
        return saida

    return run_claude_job


def _terminal(
    job: AgentJob, spec: dict, device_id: str, desfecho: Any, arquivos: list[dict], meta: dict
) -> dict:
    """Traduz o desfecho para o estado terminal do job + a entrega em duas metades."""
    from core.claude_code import regra_de_permissao

    itens: list[str] = []
    if desfecho.status == "waiting" and desfecho.recusas:
        for recusa in desfecho.recusas:
            liberavel = regra_de_permissao(recusa) is not None
            itens.append(
                f"• {recusa.descricao()}" + ("" if liberavel else " (não dá para liberar daqui)")
            )
    if desfecho.pergunta:
        itens.append(f"Pergunta do Claude: {desfecho.pergunta}")
    if desfecho.arquivos:
        itens.append("Arquivos: " + ", ".join(f"`{a}`" for a in desfecho.arquivos[:20]))
    if desfecho.session_id and desfecho.cwd:
        itens.append(f"Para abrir no terminal: `cd {desfecho.cwd} && claude --resume {desfecho.session_id}`")

    custo = f"{desfecho.rodadas} rodada(s)" if desfecho.rodadas else "nenhuma rodada chegou a rodar"
    if desfecho.cota is not None and desfecho.cota.janela_5h is not None:
        custo += f", {round(desfecho.cota.janela_5h * 100)}% da janela de 5 h da assinatura"
    resumo = f"{desfecho.resumo}\n\n({custo}.)"
    if desfecho.status == "waiting":
        resumo += (
            f"\n\nPara continuar: claude_reply(job_id={job.id}, mensagem=<a resposta dele>"
            + (", liberar=true se ele autorizou as ações acima" if desfecho.recusas else "")
            + ")."
        )
    if arquivos:
        # Estão no catálogo, não no /workspace: o turno do `then` do job 8f83b37c
        # gastou 8 chamadas tentando `file_save` em caminhos que não existiam.
        resumo += "\n\nArquivos recolhidos para você trabalhar em cima: " + ", ".join(
            f"{a['path']} (file_id={a['file_id']}{', cortado' if a['truncado'] else ''})" for a in arquivos
        ) + (
            ". Eles estão no catálogo, NÃO no /workspace: para ler ou mexer, "
            "file_stage(file_id=…, target='workbench') primeiro."
        )

    meta = dict(meta)
    meta["claude"] = {
        "session_id": desfecho.session_id,
        "cwd": desfecho.cwd,
        "device_id": device_id,
        "tarefa_id": str(spec.get("tarefa_id") or job.id),
        "rodadas": desfecho.rodadas,
        "custo_estimado_usd": round(desfecho.custo_usd, 4),
        "cota_5h": desfecho.cota.janela_5h if desfecho.cota else None,
        "recusas": [{"ferramenta": r.ferramenta, "entrada": r.entrada} for r in desfecho.recusas],
        "pergunta": desfecho.pergunta,
        "relatorio": desfecho.relatorio,
        "arquivos": arquivos,
    }
    # LITERAL: o texto já vem escrito para o Daniel (pelo Claude, no relatório,
    # ou pelo orquestrador, na pausa e nas falhas). Passado pelo redator, o do
    # job 8f83b37c virou "Arquivos salvos e disponíveis para download." e a
    # ressalva do que ficou sem teste não chegou.
    fecho = desfecho.resumo
    if desfecho.status == "completed":
        fecho = f"O Claude terminou. O relatório dele:\n\n{desfecho.resumo}"
    meta["entrega"] = {"fecho": fecho, "itens": itens, "literal": True}
    return {
        "status": desfecho.status,
        "result_summary": resumo[:6000],
        "extra_metadata": meta,
    }
