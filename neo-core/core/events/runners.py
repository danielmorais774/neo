"""Runners: a reação que um gatilho de skill pode disparar.

Conjunto **fechado** e determinístico. Nenhum runner chama LLM: um fim de faixa
acontece a cada três minutos, e pagar um turno cognitivo por faixa transformaria
uma fila em improviso caro e lento. O cérebro entra só na borda — quando a fila
acaba e alguém precisa decidir o que vem depois (aí é `aviso_final`, uma
mensagem, não um turno).

Regra de segurança que vale para todos: **o link despachado sai sempre de uma
linha do cadastro**, nunca do payload do evento. O que o navegador manda é só
identificação (aba, faixa, estado); se o link viesse de lá, uma página
comprometida faria o Neo abrir qualquer coisa.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Quantas linhas sem link o runner tolera pular antes de desistir.
_MAX_SKIPS = 5


@dataclass
class RunnerContext:
    """Tudo que um runner precisa — nada global, nada de sessão de conversa."""

    trigger: Any                      # core.events.schema.EventTrigger
    subscription: Any                 # core.events.registry.Subscription
    event: dict[str, Any]             # payload normalizado do evento
    user_id: uuid.UUID
    db: Any
    dataset_registry: Any = None
    dataset_service: Any = None


@dataclass
class RunnerResult:
    """O que sai de um runner: uma ação para o aparelho e/ou o fim da inscrição."""

    #: Ação devolvida na resposta do POST (o desktop executa e re-arma).
    action: dict[str, Any] | None = None
    #: Nova referência esperada (a faixa que acabamos de despachar).
    expected_ref: str = ""
    #: Encerra a inscrição?
    end: bool = False
    reason: str = ""
    #: Texto proativo para o chat (fila acabou, disjuntor abriu…).
    notify: str = ""
    #: Linhas tocadas por este disparo — só para o log de entrega.
    touched: dict[str, Any] = field(default_factory=dict)


async def _resolve_dataset(ctx: RunnerContext, slug: str) -> Any:
    if ctx.dataset_registry is None:
        raise RuntimeError("cadastros indisponíveis (NEO_DATASETS_ENABLED off?)")
    return await ctx.dataset_registry.resolve(slug, user_id=ctx.user_id)


def _play_action(link: str, target: dict[str, Any], titulo: str) -> dict[str, Any] | None:
    """Traduz o link salvo na ação concreta que o aparelho sabe executar.

    YouTube com aba conhecida vira `media_command` (reusa a aba, sem piscar);
    o resto vira `open_url`/`launch_uri`, o mesmo par que a MusicPipeline já usa.
    """
    from core.skills.music.service import parse_direct_link

    parsed = parse_direct_link(link or "")
    if parsed is None:
        return None
    if parsed.service == "youtube" and parsed.kind == "video":
        return {
            "acao": "tocar",
            "servico": "youtube",
            "video_id": parsed.id,
            "url": parsed.url,
            "tab_id": target.get("tab_id"),
            "titulo": titulo,
        }
    if parsed.service == "spotify":
        return {
            "acao": "abrir_uri",
            "servico": "spotify",
            "uri": parsed.uri,
            "url": parsed.url,
            "titulo": titulo,
        }
    return {"acao": "abrir_url", "servico": parsed.service, "url": parsed.url, "titulo": titulo}


async def proxima_da_fila(ctx: RunnerContext) -> RunnerResult:
    """Despacha a próxima linha pendente da fila e marca o estado das linhas.

    A ordem das escritas importa: marcamos a linha ANTES de responder. Se o
    aparelho não executar, perdemos uma faixa; se marcássemos depois, um evento
    repetido despacharia a mesma faixa duas vezes — e essa é a falha que o
    usuário vê ("ele repetiu a música três vezes").
    """
    trigger = ctx.trigger
    dataset = await _resolve_dataset(ctx, trigger.fila)
    service = ctx.dataset_service
    if service is None:
        raise RuntimeError("DatasetService indisponível")

    estado_campo = trigger.campo_estado
    despachada = trigger.marca.get("despachada") or ""
    anterior = trigger.marca.get("anterior") or ""
    pulada = trigger.marca.get("pulada") or ""

    # 1. A que estava tocando vira "tocada" — o evento que chegou é justamente
    #    o fim dela. Sem isso ela continuaria elegível e a fila travaria nela.
    if anterior and despachada:
        try:
            tocando = await service.list_rows(
                dataset, user_id=ctx.user_id, filtro={estado_campo: despachada}, limite=5,
            )
            for row in tocando.get("linhas", []):
                await service.update_row(
                    dataset,
                    user_id=ctx.user_id,
                    row_id=uuid.UUID(str(row["id"])),
                    campos={estado_campo: anterior},
                    actor="evento",
                )
        except Exception as exc:  # noqa: BLE001 — não impede o avanço
            logger.warning("[events] não marquei a faixa anterior: %s", exc)

    # 2. Próxima pendente, na ordem da fila. Linha sem link tocável é pulada —
    #    marcada, para não voltar na próxima rodada.
    skipped: list[str] = []
    for _ in range(_MAX_SKIPS):
        listed = await service.list_rows(
            dataset,
            user_id=ctx.user_id,
            filtro=dict(trigger.filtro or {}),
            ordem=trigger.ordem,
            limite=1,
        )
        rows = listed.get("linhas", [])
        if not rows:
            fim = trigger.encerra_quando == "fila_vazia"
            return RunnerResult(
                end=fim,
                reason="fila vazia",
                notify=(trigger.aviso_final or "") if fim else "",
                touched={"puladas": skipped},
            )
        row = rows[0]
        row_id = uuid.UUID(str(row["id"]))
        titulo = str(row.get(trigger.campo_titulo) or "") if trigger.campo_titulo else ""
        action = _play_action(str(row.get(trigger.campo_link) or ""), ctx.subscription.target, titulo)
        if action is None:
            skipped.append(titulo or str(row_id))
            await service.update_row(
                dataset,
                user_id=ctx.user_id,
                row_id=row_id,
                campos={estado_campo: pulada or anterior or despachada},
                actor="evento",
            )
            continue

        # 3. Marca ANTES de responder (ver docstring).
        await service.update_row(
            dataset,
            user_id=ctx.user_id,
            row_id=row_id,
            campos={estado_campo: despachada},
            actor="evento",
        )
        expected = str(action.get("video_id") or action.get("uri") or action.get("url") or "")
        return RunnerResult(
            action=action,
            expected_ref=expected,
            reason=f"despachei '{titulo or expected}'",
            touched={"linha": str(row_id), "puladas": skipped},
        )

    return RunnerResult(
        end=True,
        reason=f"{_MAX_SKIPS} linhas seguidas sem link tocável",
        notify="Parei a fila: as próximas faixas estão sem link para tocar.",
        touched={"puladas": skipped},
    )


async def avisa_no_chat(ctx: RunnerContext) -> RunnerResult:
    """Só avisa — usado por gatilhos que observam sem atuar."""
    texto = ctx.trigger.aviso_final or "Aconteceu o evento que você pediu para eu observar."
    return RunnerResult(notify=texto, reason="aviso enviado")


#: Runners disponíveis para o ``faz:`` de um gatilho. É esta lista que o
#: SKILL.md pode nomear — e só ela.
RUNNERS: dict[str, Callable[[RunnerContext], Any]] = {
    "proxima_da_fila": proxima_da_fila,
    "avisa_no_chat": avisa_no_chat,
}
