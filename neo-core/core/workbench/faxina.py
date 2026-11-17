"""Faxina do ``/workspace`` do sandbox: o que ficou grande e parado, e os anexos velhos.

Nada mais apagava o que o Neo baixa ou gera no sandbox: o volume persiste para
sempre. O teste do Sintel (session-34788907) deixou 1,6 GB ali (o zip baixado, o
mkv extraído dele, o mp4 convertido e as partes), com a VPS a 94% de disco. O que
foi entregue com ``file_save`` já está no catálogo (``/files``), então a cópia no
sandbox é só sobra.

Regras, de ``[faxina]`` no ``neo.toml`` (relidas a cada passada):

- arquivo acima de ``grande_mb`` sem modificação há ``grande_horas``;
- qualquer arquivo em ``/workspace/files`` (anexos do Daniel em stage) há
  ``anexos_horas``: é conteúdo pessoal. 0 desliga a regra, e é o padrão: nem
  todo arquivo ali tem original no catálogo (em 14/11, 19 de 31 não tinham);
- pasta vazia parada há ``grande_horas`` (a que sobrou da faxina, sem pressa).

Fica de fora ``/workspace/.jobs`` (os logs dos jobs, pequenos, lidos pelo
``job_logs``). Exclusão por ``-not -path`` e não por ``-prune``: o ``-delete`` do
GNU find liga ``-depth``, e com ``-depth`` o ``-prune`` não faz nada, calado.

Apagar um arquivo que um job ainda está lendo não quebra o job: no Linux o
processo segue com o arquivo aberto até fechar. Por isso a regra é por tempo sem
modificação, que é o que protege o arquivo em uso de verdade (o que está sendo
escrito).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

RAIZ = "/workspace"

#: Espera antes da primeira passada depois do start: o core recarrega a cada
#: save, e a faxina não precisa disputar o boot.
ATRASO_INICIAL_S = 300.0

#: Teto do comando no sandbox. `find` em algumas centenas de MB leva segundos.
TIMEOUT_S = 120


@dataclass(frozen=True)
class Regras:
    grande_mb: int
    grande_horas: float
    anexos_horas: float


@dataclass
class Resultado:
    #: (regra, bytes, caminho) de cada coisa apagada.
    apagados: list[tuple[str, int, str]] = field(default_factory=list)
    erro: str = ""

    @property
    def bytes(self) -> int:
        return sum(b for _, b, _ in self.apagados)


def regras_do_ajuste() -> Regras | None:
    """As regras do ``neo.toml``; ``None`` quando a faxina está desligada."""
    from shared.ajustes import ajustes

    cfg = ajustes().faxina
    if not cfg.ligada:
        return None
    return Regras(cfg.grande_mb, cfg.grande_horas, cfg.anexos_horas)


def comando(regras: Regras, raiz: str = RAIZ) -> str:
    """O shell que apaga e imprime ``regra<TAB>bytes<TAB>caminho`` de cada item."""
    r = shlex.quote(raiz)
    jobs = shlex.quote(f"{raiz}/.jobs")
    jobs_dentro = shlex.quote(f"{raiz}/.jobs/*")
    anexos = shlex.quote(f"{raiz}/files")
    grande_min = max(1, int(regras.grande_horas * 60))
    partes = [
        # `-size +NM`: acima de N MiB (o GNU arredonda o tamanho para cima em MiB).
        f"find {r} -xdev -type f -not -path {jobs_dentro} -size +{regras.grande_mb}M "
        f"-mmin +{grande_min} -printf 'grande\\t%s\\t%p\\n' -delete",
    ]
    if regras.anexos_horas > 0:
        anexos_min = max(1, int(regras.anexos_horas * 60))
        partes.append(
            f"if [ -d {anexos} ]; then find {anexos} -xdev -type f -mmin +{anexos_min} "
            f"-printf 'anexo\\t%s\\t%p\\n' -delete; fi"
        )
    partes += [
        f"find {r} -xdev -mindepth 1 -type d -empty -not -path {jobs} -not -path {anexos} "
        f"-mmin +{grande_min} -printf 'pasta\\t0\\t%p\\n' -delete",
        "true",
    ]
    return " ; ".join(partes)


def ler_saida(stdout: str) -> list[tuple[str, int, str]]:
    """As linhas ``regra<TAB>bytes<TAB>caminho`` que o ``comando`` imprime."""
    apagados: list[tuple[str, int, str]] = []
    for linha in (stdout or "").splitlines():
        partes = linha.split("\t", 2)
        if len(partes) != 3 or partes[0] not in ("grande", "anexo", "pasta"):
            continue
        try:
            tamanho = int(partes[1])
        except ValueError:
            tamanho = 0
        apagados.append((partes[0], tamanho, partes[2]))
    return apagados


async def passar(sandbox: Any, regras: Regras, raiz: str = RAIZ) -> Resultado:
    """Uma passada da faxina. Nunca levanta: o erro volta no ``Resultado``."""
    try:
        res = await sandbox.run(comando(regras, raiz), timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — sandbox fora do ar não é motivo de crash
        return Resultado(erro=f"sandbox: {exc}")
    resultado = Resultado(apagados=ler_saida(res.stdout))
    if res.stderr.strip():
        resultado.erro = res.stderr.strip()[:500]
    if resultado.apagados:
        logger.info(
            "[faxina] apagou %d item(ns) do sandbox, %.1f MB: %s",
            len(resultado.apagados),
            resultado.bytes / (1024 * 1024),
            "; ".join(f"{regra} {caminho}" for regra, _, caminho in resultado.apagados[:20]),
        )
    if resultado.erro:
        logger.warning("[faxina] a passada reclamou: %s", resultado.erro)
    return resultado


async def faxina_loop(sandbox: Any, *, atraso_inicial_s: float = ATRASO_INICIAL_S) -> None:
    """Loop infinito da faxina. Cancelado no shutdown do app."""
    logger.info("[faxina] ligada (primeira passada em %.0fs)", atraso_inicial_s)
    await asyncio.sleep(atraso_inicial_s)
    while True:
        intervalo_s = 6 * 3600.0
        try:
            from shared.ajustes import ajustes

            intervalo_s = ajustes().faxina.intervalo_horas * 3600
            regras = regras_do_ajuste()
            if regras is not None:
                await passar(sandbox, regras)
        except asyncio.CancelledError:
            logger.info("[faxina] encerrada")
            raise
        except Exception as exc:  # noqa: BLE001 — uma passada ruim não mata o loop
            logger.warning("[faxina] passada falhou: %s", exc)
        await asyncio.sleep(intervalo_s)
