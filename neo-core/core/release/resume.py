"""Retomada depois da aprovação (F3c do docs/release-request-plan.md).

Você clica "Liberar" e o trabalho continua de onde parou, sem redigitar o pedido.
É conveniência, então TODA falha aqui é silenciosa para a decisão: a liberação já
está gravada e vale mesmo que a retomada não aconteça.

As guardas existem porque isto dispara um turno a partir de um clique:

- **só aprovação** (recusa não retoma nada);
- **só com sessão** (sem ela não há trabalho para continuar);
- **uma vez por liberação** — o mesmo pedido não reabre turno duas vezes;
- **prazo** no turno inteiro, como o disparo da agenda;
- ``unattended`` no metadata, que tira as tools client-side da mesa: o turno
  nasce de um clique num card, não de alguém olhando o terminal. As tools de job
  (inclusive o navegador local) continuam valendo — é por elas que o trabalho
  segue;
- a instrução diz explicitamente para **não pedir a mesma liberação de novo**,
  senão um turno que falha vira um card atrás do outro.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from db.models.release_request import STATUS_APROVADA, STATUS_RECUSADA, ReleaseRequest

logger = logging.getLogger(__name__)

#: Teto do turno de retomada. Mesma ordem de grandeza do disparo da agenda.
TIMEOUT_S = 420

#: Liberações já retomadas neste processo. Em memória de propósito: se o core
#: reinicia, o turno pendente morreu junto e não há o que duplicar.
_retomadas: set[str] = set()


def _instrucao(row: ReleaseRequest) -> str:
    campos = ", ".join(str(c).split(":", 1)[-1] for c in (row.campos or [])) or "o dado"
    return (
        f"O Daniel ACABOU DE APROVAR a liberação de {campos} para {row.destino}"
        + (f" ({row.finalidade})" if row.finalidade else "")
        + ". Continue a tarefa de onde parou e conclua agora.\n"
        "Regras deste turno:\n"
        f"- use SOMENTE o que foi liberado ({campos}) e só com {row.destino};\n"
        "- ao preencher campo em página, escreva o marcador «pks:campo» como valor — "
        "quem troca pelo dado real é o sistema, e só nesse site;\n"
        "- NÃO chame solicitar_dado para o mesmo campo e destino outra vez: já está "
        "aprovado. Se faltar outra coisa, termine explicando o que falta;\n"
        "- se não der para concluir, diga o que aconteceu — não prometa para depois."
    )


def build_resumer(
    *, get_ai_service: Any, announce: Any = None, resume_job: Any = None
) -> Any:
    """Devolve ``async (row) -> bool`` — ``True`` quando um turno foi disparado.

    ``resume_job`` (``core/jobs/release_resume.py``) vem PRIMEIRO: quando o
    pedido nasceu de uma tarefa do navegador que parou no campo, continuar
    aquela tarefa é exato — mesma tarefa, mesma aba — e não depende de um
    modelo redescobrir o que estava sendo feito. O turno cognitivo fica para o
    pedido que não tem job esperando (``solicitar_dado`` de um e-mail, etc.).
    """

    async def resume(row: ReleaseRequest) -> bool:
        if row is None:
            return False
        if row.status != STATUS_APROVADA:
            # Recusa não abre turno nenhum, mas a tarefa do navegador que parou
            # esperando por ela tem de deixar de esperar.
            if resume_job is not None and row.status == STATUS_RECUSADA:
                try:
                    await resume_job(row)
                except Exception:  # noqa: BLE001
                    logger.exception("[release][retomada] encerrar a tarefa de %s falhou", row.id)
            return False
        if row.session_id is None:
            logger.info("[release][retomada] %s não tem sessão — nada a continuar", row.id)
            return False
        chave = str(row.id)
        if chave in _retomadas:
            logger.info("[release][retomada] %s já foi retomada", chave)
            return False
        if resume_job is not None:
            try:
                if await resume_job(row):
                    _retomadas.add(chave)
                    logger.warning("[release][retomada] %s continuou a tarefa do navegador", chave)
                    return True
            except Exception:  # noqa: BLE001 — cai no turno, que ainda pode resolver
                logger.exception("[release][retomada] continuação do job de %s falhou", chave)
        ai_service = get_ai_service() if callable(get_ai_service) else get_ai_service
        if ai_service is None:
            logger.warning("[release][retomada] AIService indisponível")
            return False
        _retomadas.add(chave)

        asyncio.create_task(_rodar(ai_service, row, announce))
        logger.warning("[release][retomada] turno disparado para %s", chave)
        return True

    return resume


async def _rodar(ai_service: Any, row: ReleaseRequest, announce: Any) -> None:
    metadata = {
        "source": "release",
        "unattended": True,
        "client": "neo-release",
        # Quem aprovou foi VOCÊ, num aparelho pareado: a intenção deste turno é
        # sua, não da página que começou tudo.
        "principal": "admin",
        "release_id": str(row.id),
    }
    final: str | None = None

    async def _consumir() -> None:
        nonlocal final
        async for resp in ai_service.process_user_input(
            user_message=_instrucao(row),
            session_id=row.session_id,
            user_id=row.user_id,
            service="neo-release",
            metadata=metadata,
        ):
            if getattr(resp, "is_final", False):
                final = resp.message

    try:
        await asyncio.wait_for(_consumir(), timeout=TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("[release][retomada] %s passou de %ds", row.id, TIMEOUT_S)
        return
    except Exception:  # noqa: BLE001 — a decisão já está gravada
        logger.exception("[release][retomada] turno de %s quebrou", row.id)
        return

    if final and announce is not None:
        try:
            await announce(row, final)
        except Exception:  # noqa: BLE001
            logger.exception("[release][retomada] falha ao publicar o resultado de %s", row.id)


def _esquecer(release_id: UUID | str) -> None:
    """Só para teste: libera a trava de 'uma vez por liberação'."""
    _retomadas.discard(str(release_id))
