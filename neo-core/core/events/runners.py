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
import secrets
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


def _play_action(
    link: str, target: dict[str, Any], titulo: str, codigo: str = ""
) -> dict[str, Any] | None:
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
            "codigo": codigo,
        }
    if parsed.service == "spotify":
        return {
            "acao": "abrir_uri",
            "servico": "spotify",
            "uri": parsed.uri,
            "url": parsed.url,
            "titulo": titulo,
            "codigo": codigo,
        }
    return {
        "acao": "abrir_url",
        "servico": parsed.service,
        "url": parsed.url,
        "titulo": titulo,
        "codigo": codigo,
    }


async def _find_by_code(ctx: RunnerContext, dataset: Any, codigo: str) -> dict[str, Any] | None:
    """A linha do código despachado. É o que torna o status PRECISO.

    Sem código a gente marcaria "quem estiver como tocando", que erra assim que a
    fila é mexida no meio (o usuário reordena, some uma linha, entra outra).
    """
    trigger = ctx.trigger
    if not codigo or not trigger.campo_codigo:
        return None
    listed = await ctx.dataset_service.list_rows(
        dataset, user_id=ctx.user_id, filtro={trigger.campo_codigo: codigo}, limite=1,
    )
    rows = listed.get("linhas", [])
    return rows[0] if rows else None


async def _mark(ctx: RunnerContext, dataset: Any, row: dict[str, Any], estado: str) -> None:
    await ctx.dataset_service.update_row(
        dataset,
        user_id=ctx.user_id,
        row_id=uuid.UUID(str(row["id"])),
        campos={ctx.trigger.campo_estado: estado},
        actor="evento",
    )


async def proxima_da_fila(ctx: RunnerContext) -> RunnerResult:
    """Máquina de estados da fila, dirigida pelos eventos do aparelho.

    - ``faixa_iniciou``  → a linha daquele CÓDIGO vira "tocando" (começou de
      verdade — antes disso ela estava só "despachada", que é diferente);
    - ``faixa_terminando``/``faixa_terminou`` → fecha a linha do código e
      despacha a próxima pendente, com um código novo.

    A ordem das escritas no avanço importa: marcamos a linha nova ANTES de
    responder. Se o aparelho não executar, perde-se uma faixa; se marcássemos
    depois, um evento repetido despacharia a mesma faixa duas vezes — e essa é a
    falha que o usuário vê ("ele repetiu a música três vezes").
    """
    trigger = ctx.trigger
    dataset = await _resolve_dataset(ctx, trigger.fila)
    service = ctx.dataset_service
    if service is None:
        raise RuntimeError("DatasetService indisponível")

    evento = str(ctx.event.get("evento") or "")
    codigo_evento = str(ctx.event.get("codigo") or "")
    estado_campo = trigger.campo_estado
    despachada = trigger.marca.get("despachada") or ""
    tocando = trigger.marca.get("tocando") or despachada
    anterior = trigger.marca.get("anterior") or ""
    pulada = trigger.marca.get("pulada") or ""

    # ---- começou a tocar: só muda status, a fila não anda -------------------
    if evento == "faixa_iniciou":
        row = await _find_by_code(ctx, dataset, codigo_evento)
        if row is None:
            return RunnerResult(reason=f"início sem linha para o código {codigo_evento or '-'}")
        if str(row.get(estado_campo) or "") == tocando:
            return RunnerResult(reason="já estava marcada como tocando")
        await _mark(ctx, dataset, row, tocando)
        titulo = str(row.get(trigger.campo_titulo) or "") if trigger.campo_titulo else ""
        return RunnerResult(
            reason=f"'{titulo or codigo_evento}' começou a tocar",
            touched={"linha": str(row["id"]), "codigo": codigo_evento},
        )

    # ---- acabou (ou está acabando): fecha a atual e despacha a próxima ------
    fechada = None
    if anterior:
        fechada = await _find_by_code(ctx, dataset, codigo_evento)
        if fechada is not None:
            await _mark(ctx, dataset, fechada, anterior)
        else:
            # Sem código (evento vindo do caminho de fallback do desktop): fecha
            # o que estiver em voo, senão a fila trava na mesma linha.
            for estado in {tocando, despachada}:
                if not estado:
                    continue
                listed = await service.list_rows(
                    dataset, user_id=ctx.user_id, filtro={estado_campo: estado}, limite=5,
                )
                for row in listed.get("linhas", []):
                    await _mark(ctx, dataset, row, anterior)

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
        # Código NOVO a cada despacho: identifica esta reprodução, não a música.
        # A mesma faixa enfileirada duas vezes tem dois códigos — e é isso que
        # deixa o "começou"/"terminou" casarem com a linha certa.
        codigo = secrets.token_hex(4) if trigger.campo_codigo else ""
        action = _play_action(
            str(row.get(trigger.campo_link) or ""), ctx.subscription.target, titulo, codigo
        )
        if action is None:
            skipped.append(titulo or str(row_id))
            await _mark(ctx, dataset, row, pulada or anterior or despachada)
            continue

        campos: dict[str, Any] = {estado_campo: despachada}
        if trigger.campo_codigo:
            campos[trigger.campo_codigo] = codigo
        await service.update_row(
            dataset, user_id=ctx.user_id, row_id=row_id, campos=campos, actor="evento",
        )
        expected = str(action.get("video_id") or action.get("uri") or action.get("url") or "")
        return RunnerResult(
            action=action,
            expected_ref=expected,
            reason=f"despachei '{titulo or expected}' (código {codigo or '-'})",
            touched={
                "linha": str(row_id),
                "codigo": codigo,
                "fechada": str(fechada["id"]) if fechada else "",
                "puladas": skipped,
            },
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
