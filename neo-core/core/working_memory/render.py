"""Renderização do caderno de trabalho (docs/working-memory-plan.md §6).

Duas superfícies, e a diferença entre elas é o ponto inteiro da feature:

- :func:`render_index` — o que entra no prompt TODO turno: uma linha por nota
  (slug + quando + resumo). Nunca o corpo.
- :func:`render_tail` — o que ``wm_read`` devolve: as últimas entradas do corpo.

Tempo: o banco guarda naive-UTC (padrão da casa, ``datetime.utcnow``) e TUDO
que o modelo lê é convertido para Brasília aqui. Um naive comparado direto com
``utcnow`` já quebrou o "há X" do seletor de sessões do desktop — por isso a
conversão é uma função só, usada por todo mundo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# Horário de Brasília (UTC-3, sem DST desde 2019) — mesmo de context_selector.
BRAZIL_TZ = timezone(timedelta(hours=-3))


def to_brazil(dt: datetime) -> datetime:
    """Converte um datetime do banco (naive-UTC) para Brasília, aware."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BRAZIL_TZ)


def stamp(dt: datetime) -> str:
    """Carimbo de uma entrada do corpo: ``31/07 14:41`` (Brasília)."""
    return to_brazil(dt).strftime("%d/%m %H:%M")


def format_when(dt: datetime, now: datetime | None = None) -> str:
    """Quando a nota foi tocada, do jeito que o modelo entende sem fazer conta.

    ``hoje 14:41`` · ``ontem 19:02`` · ``31/07 14:41``. Ambos os argumentos são
    naive-UTC (como vêm do banco); a comparação de DIA acontece em Brasília,
    senão tudo depois das 21h vira "amanhã" para quem lê.
    """
    local = to_brazil(dt)
    ref = to_brazil(now or datetime.utcnow())
    delta_days = (ref.date() - local.date()).days
    hhmm = local.strftime("%H:%M")
    if delta_days == 0:
        return f"hoje {hhmm}"
    if delta_days == 1:
        return f"ontem {hhmm}"
    return local.strftime("%d/%m %H:%M")


_INDEX_HEADER = (
    "## Memória de Trabalho (seu caderno da tarefa atual)\n"
    "Estas anotações são SUAS, de trabalho já feito. Antes de reler um arquivo, "
    "reprocessar dados ou refazer qualquer conta, verifique se a conclusão já "
    "está aqui — abra a nota com `wm_read` usando o nome entre [[ ]]."
)

_INDEX_FOOTER = (
    "Concluiu algo neste turno que vai fazer falta depois (número apurado, "
    "estrutura de um arquivo, decisão tomada, caminho de artefato)? Grave com "
    "`wm_write` AGORA — no próximo turno o conteúdo bruto não estará mais no seu "
    "contexto. Isto é rascunho de tarefa: fato permanente sobre o usuário "
    "continua indo para `pks_remember`."
)


def render_index(
    notes: list[Any],
    *,
    now: datetime | None = None,
    truncated: bool = False,
) -> str:
    """Índice para o system prompt. String vazia quando não há nota ativa."""
    if not notes:
        return ""
    lines = [_INDEX_HEADER]
    for note in notes:
        when = format_when(note.updated_at, now)
        summary = (note.summary or note.title or "").strip()
        line = f"- [[{note.slug}]] · {when}"
        if summary:
            line += f" · {summary}"
        lines.append(line)
    if truncated:
        lines.append(
            "- (notas mais antigas ficaram de fora por espaço — elas continuam "
            "acessíveis por `wm_read` se você lembrar do nome)"
        )
    lines.append(_INDEX_FOOTER)
    return "\n".join(lines)


def render_auto_entry(
    trace: list[dict[str, Any]], files: list[dict[str, Any]] | None = None
) -> str:
    """Entrada do backstop automático (§7) — a partir do ``tool_trace`` do turno.

    Determinística e sem LLM: é o PISO da memória de trabalho, para o turno em
    que o modelo fez trabalho pesado e não anotou nada. Diz o que foi executado
    e o que saiu — não tenta adivinhar a conclusão (isso é papel do wm_write).
    """
    lines: list[str] = []
    for entry in trace[:10]:
        tool = entry.get("tool", "?")
        if not entry.get("ok", True):
            lines.append(f"- {tool}: falhou ({str(entry.get('error', '?'))[:120]})")
        elif tool == "neo_write_file":
            lines.append(f"- escrevi {entry.get('path')} ({entry.get('bytes')} bytes)")
        elif tool == "neo_bash":
            preview = str(entry.get("stdout_preview") or "").strip().replace("\n", " ")
            suffix = f" → {preview[:160]}" if preview else ""
            lines.append(
                f"- rodei `{entry.get('command')}` (exit={entry.get('exit_code')}){suffix}"
            )
        elif tool == "file_save":
            lines.append(
                f"- guardei '{entry.get('name')}' no catálogo (file_id={entry.get('file_id')})"
            )
        else:
            lines.append(f"- {tool}: {str(entry.get('summary', ''))[:160]}")
    for f in (files or [])[:5]:
        lines.append(f"- entreguei ao usuário: '{f.get('name')}' (file_id={f.get('file_id')})")
    if not lines:
        return ""
    return (
        "Registro automático do turno (eu não anotei nada por conta própria aqui — "
        "isto é o que de fato foi executado):\n" + "\n".join(lines)
    )


def split_entries(body: str) -> tuple[str, list[str]]:
    """Separa o cabeçalho (``# título`` + linha de metadados) das entradas.

    Entrada = bloco que começa com ``## `` no início de uma linha (o carimbo de
    tempo). Corpo sem nenhuma entrada devolve ``(body, [])``.
    """
    head, sep, rest = body.partition("\n## ")
    if not sep:
        return body, []
    return head, [f"## {chunk}" for chunk in rest.split("\n## ")]


def render_tail(body: str, *, max_entries: int = 4) -> tuple[str, int]:
    """Últimas ``max_entries`` entradas do corpo. Devolve (texto, omitidas).

    É o default do ``wm_read``: quase sempre o que interessa é o que foi
    concluído por último, e o caderno inteiro pode ser grande.
    """
    head, entries = split_entries(body)
    if len(entries) <= max_entries:
        return body, 0
    omitted = len(entries) - max_entries
    kept = entries[-max_entries:]
    note = (
        f"_[{omitted} entrada(s) mais antiga(s) omitida(s) — chame wm_read com "
        "full=true para o caderno inteiro]_"
    )
    return "\n\n".join([head.rstrip(), note, *kept]), omitted
