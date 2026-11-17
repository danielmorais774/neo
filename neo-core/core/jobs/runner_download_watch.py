"""Runner de jobs ``kind="download_watch"`` — o vigia de um download.

Nasceu de uma observação do usuário: *"eu só pedi para ele baixar; podia já
dizer que a tarefa está concluída com o download em andamento"*. Mandar o
navegador baixar é a ação; **esperar o byte chegar não é ação, é espera** — e
gastar passos do agente (cada um com uma chamada de LLM e uma ida-e-volta até a
máquina do usuário) para ficar olhando um arquivo crescer é o pior jeito
possível de esperar.

Então a tarefa do navegador termina assim que o download COMEÇA, e quem cuida do
resto é este job: um laço em código puro, sem LLM nenhum no meio, que pergunta à
pasta de downloads de tempos em tempos. O LLM só volta a ser chamado UMA vez, no
fim, quando há um veredito para contar — e quem faz isso é o
``ProactiveDispatcher`` de sempre, que já sabe redigir o aviso no chat.

Quatro desfechos, todos com evidência do disco (nunca da narrativa de alguém):

* **concluído** — o arquivo parou de ter extensão temporária;
* **travado** — o tamanho não muda há ``stall_s`` e ainda está incompleto;
* **sumiu** — o arquivo desapareceu (download cancelado no navegador);
* **tempo esgotado** — passou de ``timeout_s`` e continua vindo.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from db.models.agent_job import AgentJob

logger = logging.getLogger(__name__)


def _mb(size: Any) -> str:
    try:
        return f"{int(size) / 1_048_576:.1f} MB"
    except (TypeError, ValueError):
        return "tamanho desconhecido"


def build_download_watch_runner(
    *,
    db: Any,
    poll_s: int = 10,
    stall_s: int = 120,
    default_timeout_s: int = 1800,
) -> Callable[[AgentJob], Awaitable[dict]]:
    """Fecha as dependências e devolve ``run_download_watch_job(job) -> dict``."""

    async def run_download_watch_job(job: AgentJob) -> dict:
        from core.notifications.device_control import (
            DeviceOffline,
            get_device_control_broadcaster,
        )

        spec = job.spec_json or {}
        device_id = str(spec.get("device_id") or "").strip()
        alvo = str(spec.get("name") or "").strip()
        if not device_id or not alvo:
            return {"status": "failed", "result_summary": "Vigia de download sem device ou arquivo."}

        # O id do download no navegador, quando o driver conseguiu um. Sem ele o
        # vigia continua casando por nome, como antes.
        download_id = str(spec.get("download_id") or "").strip()
        timeout_s = int(spec.get("timeout_s") or default_timeout_s)
        limite = datetime.utcnow() + timedelta(seconds=timeout_s)
        broadcaster = get_device_control_broadcaster()

        # O nome muda quando o navegador tira o sufixo temporário
        # (`Print.zip.crdownload` → `Print.zip`), então a busca é pelo prefixo.
        raiz = alvo
        for sufixo in (".crdownload", ".download", ".part", ".tmp"):
            if raiz.lower().endswith(sufixo):
                raiz = raiz[: -len(sufixo)]
                break

        ultimo_tamanho: int | None = None
        parado_desde = datetime.utcnow()

        while datetime.utcnow() < limite:
            await asyncio.sleep(poll_s)
            try:
                payload = await broadcaster.request(
                    device_id, "browser.downloads", {"since_s": 86_400}, timeout=15.0
                )
            except DeviceOffline:
                return {
                    "status": "failed",
                    "result_summary": (
                        f"Perdi contato com a sua máquina enquanto acompanhava o download de "
                        f"{raiz}. Não sei dizer se ele terminou."
                    ),
                }
            except Exception as exc:  # noqa: BLE001 — device instável não encerra o vigia
                logger.debug("[DownloadWatch] consulta falhou: %s", exc)
                continue

            if payload.get("ok") is not True:
                continue
            resultado = payload.get("result") or {}
            pasta = str(resultado.get("dir") or "").rstrip("/")
            arquivos = resultado.get("files") or []
            # Casar por id é exato; por prefixo de nome é o que sobrou quando só
            # o disco responde (o sufixo temporário cai quando o arquivo fecha,
            # então o nome muda no meio do caminho).
            atual = None
            if download_id:
                atual = next(
                    (f for f in arquivos if str(f.get("download_id") or "") == download_id),
                    None,
                )
            if atual is None:
                atual = next(
                    (f for f in arquivos if str(f.get("name") or "").startswith(raiz)), None
                )

            if atual is None:
                return {
                    "status": "failed",
                    "result_summary": (
                        f"O download de {raiz} sumiu da pasta de downloads antes de terminar — "
                        "provavelmente foi cancelado no navegador."
                    ),
                }

            nome = str(atual.get("name"))
            tamanho = int(atual.get("bytes") or 0)
            estado = str(atual.get("state") or "")

            # Quando o navegador é quem respondeu (`state` presente), ele sabe do
            # desfecho antes e melhor que o disco: um download interrompido some
            # da pasta ou deixa um `.crdownload` órfão, e das duas formas o
            # veredito de disco só chegaria depois de `stall_s` — dizendo
            # "travou", que não é o que aconteceu.
            if estado == "interrupted":
                motivo = str(atual.get("error") or "").strip()
                detalhe = f" ({motivo})" if motivo else ""
                return {
                    "status": "failed",
                    "result_summary": (
                        f"O download de {nome} falhou{detalhe} e parou em {_mb(tamanho)}. "
                        "Não terminou — o arquivo não está completo na pasta de downloads."
                    ),
                }
            if estado == "in_progress" and atual.get("paused"):
                return {
                    "status": "failed",
                    "result_summary": (
                        f"O download de {nome} está pausado no navegador em {_mb(tamanho)}. "
                        "Ele não continua sozinho: é preciso retomar na barra de downloads."
                    ),
                }

            if not atual.get("incomplete"):
                # Fim de fila do registro de artefatos: aqui o nome é o
                # definitivo e o tamanho é o real. Sobrescreve (por nome) a linha
                # provisória que o runner do navegador gravou quando o download
                # começou. Best-effort — o veredito do job não depende disto.
                try:
                    from core.working_memory import artifacts

                    store = artifacts.store_for(db)
                    if store is not None and pasta:
                        await artifacts.record(
                            store,
                            user_id=job.user_id,
                            session_id=job.session_id,
                            items=[{
                                "name": nome,
                                "path": f"{pasta}/{nome}",
                                "bytes": tamanho,
                                "source": "baixado pelo navegador local",
                            }],
                        )
                except Exception as exc:  # noqa: BLE001 — caderno nunca derruba o vigia
                    logger.debug("[DownloadWatch] registro de artefato falhou: %s", exc)
                return {
                    "status": "completed",
                    "result_summary": (
                        f"O download terminou: {nome} ({_mb(tamanho)}) já está na pasta de "
                        "downloads do usuário."
                    ),
                }

            if ultimo_tamanho is not None and tamanho != ultimo_tamanho:
                parado_desde = datetime.utcnow()
            ultimo_tamanho = tamanho

            if (datetime.utcnow() - parado_desde).total_seconds() >= stall_s:
                return {
                    "status": "failed",
                    "result_summary": (
                        f"O download de {nome} travou: parou em {_mb(tamanho)} e não avança há "
                        f"{stall_s // 60} minutos. Vale conferir o navegador."
                    ),
                }

            try:
                await db.update_agent_job(
                    job.id,
                    progress_note=f"{nome}: {_mb(tamanho)} baixados"[:200],
                    heartbeat_at=datetime.utcnow(),
                )
            except Exception:  # noqa: BLE001
                pass

        return {
            "status": "failed",
            "result_summary": (
                f"Acompanhei o download de {raiz} por {timeout_s // 60} minutos e ele não "
                "terminou. Pode ser conexão lenta ou arquivo grande demais — não cancelei nada."
            ),
        }

    return run_download_watch_job
