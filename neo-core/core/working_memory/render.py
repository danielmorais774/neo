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

import re
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
        # Não prometa o que não dá para cumprir: não existe tool de LISTAR
        # notas, só `wm_read` por nome exato. O conselho antigo ("se você
        # lembrar do nome") mandava o modelo lembrar de um nome que ele nunca
        # viu. O que ele PODE fazer é perguntar — e é isso que a linha diz.
        lines.append(
            "- (há mais notas ativas que não couberam aqui. Só dá para abrir uma "
            "nota pelo nome exato, então, se o assunto do pedido parecer trabalho "
            "que você já fez e não estiver na lista acima, pergunte ao usuário em "
            "vez de refazer tudo do zero)"
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
    # 25 e não 10: o corpo da nota é o lugar barato, e um turno que encadeou 15
    # comandos é justamente o que dói refazer.
    for entry in trace[:25]:
        lines.extend(_trace_entry_lines(entry))
    for f in (files or [])[:8]:
        lines.append(f"- entreguei ao usuário: '{f.get('name')}' (file_id={f.get('file_id')})")
    if not lines:
        return ""
    return (
        "Registro automático do turno (eu não anotei nada por conta própria aqui — "
        "isto é o que de fato foi executado):\n" + "\n".join(lines)
    )


def _trace_entry_lines(entry: dict[str, Any]) -> list[str]:
    """As linhas de UMA entrada de ``tool_trace``.

    Compartilhadas pelo registro do fim do turno (`render_auto_entry`) e pelo
    registro passo a passo (`render_step_entry`): o mesmo passo não pode ser
    descrito de dois jeitos dependendo de quando foi anotado.
    """
    lines: list[str] = []
    tool = entry.get("tool", "?")
    # `note_detail` é o resultado FARTO, que só existe deste lado (ver
    # `_summarize_tool_call`). Quando há, ele vem DEPOIS da linha curta: a
    # linha diz o que foi feito, o bloco diz o que saiu.
    detalhe = str(entry.get("note_detail") or "").strip()
    if not entry.get("ok", True):
        lines.append(f"- {tool}: falhou ({str(entry.get('error', '?'))[:400]})")
    elif entry.get("async_job"):
        titulo = entry.get("job_title") or "(sem título)"
        comando = f" `{entry.get('command')}`" if entry.get("command") else ""
        lines.append(
            f"- abri o job '{titulo}' via {tool}{comando} (job {entry.get('job_id')}) — "
            "ele ainda estava rodando quando este passo foi anotado; o RESULTADO "
            "entra nesta nota numa entrada própria quando terminar"
        )
    elif tool == "run_command":
        lines.append(f"- rodei NA MÁQUINA DO USUÁRIO `{entry.get('command')}`")
    elif tool == "request_user_file":
        onde = (
            f"materializado no sandbox em {entry.get('path')}"
            if entry.get("path")
            else "ainda NÃO materializado no sandbox (use file_stage)"
        )
        lines.append(
            f"- recebi do usuário '{entry.get('name')}'{_tamanho(entry.get('size_bytes'))} — "
            f"file_id={entry.get('file_id')}; {onde}. Não peça de novo: já está no catálogo"
        )
    elif tool == "read_user_file":
        lines.append(f"- li do disco do usuário: {entry.get('path') or entry.get('name')}")
    elif tool in ("edit_user_file", "delegate_file_edit"):
        via = " (via opencode)" if tool == "delegate_file_edit" else ""
        lines.append(f"- alterei no disco do usuário{via}: {entry.get('path') or entry.get('name')}")
    elif tool in ("save_generated_file", "write_file"):
        lines.append(f"- entreguei no disco do usuário: {entry.get('path') or entry.get('name')}")
    elif tool == "neo_write_file":
        comeco = f" — começa com: {entry['primeira_linha']}" if entry.get("primeira_linha") else ""
        lines.append(
            f"- escrevi {entry.get('path')} ({entry.get('bytes')} bytes){comeco} "
            "(conteúdo NÃO copiado para cá; releia com neo_read_file)"
        )
    elif tool == "neo_bash" and entry.get("leitura_chars") is not None:
        lines.append(
            f"- li conteúdo de arquivo com `{entry.get('command')}` (exit={entry.get('exit_code')}) — "
            f"{entry.get('leitura_chars')} chars NÃO copiados para cá (releia rodando o comando)"
        )
    elif tool == "neo_bash":
        # Com o bloco farto logo abaixo, a prévia diria a mesma coisa duas
        # vezes — e a primeira ainda achatada numa linha só.
        preview = (
            "" if detalhe
            else str(entry.get("stdout_preview") or "").strip().replace("\n", " ")
        )
        suffix = f" → {preview[:160]}" if preview else ""
        lines.append(
            f"- rodei `{entry.get('command')}` (exit={entry.get('exit_code')}){suffix}"
        )
    elif tool == "file_read":
        # Ponteiro, não payload. O caderno guarda ONDE está o arquivo e o
        # que foi lido dele; o conteúdo se relê (e a cópia envelheceria).
        trecho = f", linhas {entry['linhas']}" if entry.get("linhas") else ""
        resto = " (tem mais)" if entry.get("tem_mais") else ""
        lines.append(
            f"- li '{entry.get('name')}'{trecho}{resto} — {entry.get('chars', 0)} chars "
            f"NÃO copiados para cá (file_id={entry.get('file_id')}; releia com "
            "file_read, ou file_stage p/ processar no sandbox com neo_bash)"
        )
    elif tool == "file_stage":
        lines.append(
            f"- materializei no sandbox: {entry.get('path')} "
            f"(file_id={entry.get('file_id')}) — processe com neo_bash"
        )
    elif tool == "file_save":
        lines.append(
            f"- guardei '{entry.get('name')}' no catálogo (file_id={entry.get('file_id')})"
        )
    else:
        lines.append(f"- {tool}: {str(entry.get('summary', ''))[:160]}")
    if detalhe and entry.get("ok", True):
        # Indentado: continua sendo parte do bullet acima, e o preview do
        # desktop desenha como bloco de código em vez de prosa solta.
        lines.append("  ```")
        lines.extend(f"  {linha}" for linha in detalhe.splitlines())
        lines.append("  ```")
    return lines


def _tamanho(size_bytes: Any) -> str:
    try:
        n = int(size_bytes)
    except (TypeError, ValueError):
        return ""
    if n >= 1024 * 1024:
        return f" ({n / (1024 * 1024):.0f} MB)"
    if n >= 1024:
        return f" ({n / 1024:.0f} KB)"
    return f" ({n} bytes)"


def render_step_entry(entry: dict[str, Any]) -> str:
    """UM passo, anotado na hora em que aconteceu.

    O registro do fim do turno (`render_auto_entry`) nunca roda num turno
    cancelado nem no caminho de atuação no cliente — foi assim que as sessões
    c4f3f7de e 45bd5ad0 subiram um vídeo de 392 MB, geraram 127 quadros e
    deixaram o caderno sem uma linha.
    """
    lines = _trace_entry_lines(entry)
    if not lines:
        return ""
    return "Passo (registrado na hora, automático):\n" + "\n".join(lines)


def render_step_summary(entry: dict[str, Any]) -> str:
    """Linha do ÍNDICE a partir do passo mais recente — é ela que entra no prompt.

    O resumo fixo ("registro automático do que foi executado nesta sessão") não
    dizia nada que ajudasse a decidir abrir a nota; o endereço do último passo diz.
    """
    tool = entry.get("tool", "?")
    if not entry.get("ok", True):
        texto = f"{tool} falhou: {entry.get('error', '?')}"
    elif entry.get("async_job"):
        texto = f"job '{entry.get('job_title') or '?'}' em background (job {entry.get('job_id')})"
    elif tool == "request_user_file":
        texto = (
            f"recebi '{entry.get('name')}' (file_id={entry.get('file_id')}) → "
            f"{entry.get('path') or 'catálogo'}"
        )
    elif tool == "read_user_file":
        texto = f"li {entry.get('path')}"
    elif tool in ("edit_user_file", "delegate_file_edit"):
        texto = f"alterei {entry.get('path')}"
    elif tool in ("save_generated_file", "write_file"):
        texto = f"entreguei {entry.get('path') or entry.get('name')}"
    elif tool in ("neo_bash", "run_command"):
        onde = "na máquina do usuário: " if tool == "run_command" else ""
        texto = f"{onde}`{entry.get('command')}`"
        if entry.get("exit_code") is not None:
            texto += f" exit={entry.get('exit_code')}"
    elif tool == "file_stage":
        texto = f"{entry.get('path')} no sandbox (file_id={entry.get('file_id')})"
    elif tool == "file_save":
        texto = f"guardei '{entry.get('name')}' (file_id={entry.get('file_id')})"
    elif tool == "neo_write_file":
        texto = f"escrevi {entry.get('path')}"
    else:
        texto = f"{tool}: {entry.get('summary', '')}"
    return _uma_linha(f"último passo: {texto}", 240)


# Teto do resumo do job dentro da entrada. Generoso de propósito: o corpo da
# nota tem 32 KB e o `wm_read` traz as últimas entradas inteiras, enquanto os
# 160 caracteres do `render_auto_entry` são para uma LINHA de tool_trace. Foi
# justamente o teto curto que transformou "reclamação 258599465, monitor
# Alienware Fast IPS 280 Hz, status Em réplica" em "{'ok': True, 'job_id': ...".
_JOB_SUMMARY_CHARS = 8000

# Uma URL que o trabalho alcançou vale mais que o texto em volta: é o endereço
# exato para retomar sem refazer a navegação. O runner do navegador local já a
# publica como "(a aba ficou em <url>)".
_TAB_URL_RE = re.compile(r"\(a aba ficou em (\S+?)\)")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def render_job_entry(
    *,
    title: str,
    kind: str,
    status: str,
    job_id: str,
    summary: str,
    pedido: str = "",
    urls: list[str] | None = None,
    arquivos: list[str] | None = None,
    duracao_s: float | None = None,
    diario: str = "",
) -> str:
    """Entrada do caderno para um job de segundo plano que ACABOU.

    Existe porque o resultado de um job nunca passa por um turno cognitivo: o
    ``browser_local_task`` devolve `status: queued` na hora e o achado real só
    aparece minutos depois, entregue pelo ``ProactiveDispatcher`` com uma
    chamada de LLM própria. Ou seja, **não há turno em que o modelo pudesse
    chamar `wm_write`** — não é o modelo que esquece de anotar, é a arquitetura
    que não lhe dá a chance. Medido na sessão 09cfa763: o subagente apurou o
    número da reclamação, o modelo do monitor, o status e a URL, e o caderno
    ficou com o comprovante de enfileiramento.

    Determinística e sem LLM, como todo o resto do caderno: o que entra aqui é o
    ``result_summary`` que o runner já redigiu, não uma segunda interpretação
    dele.
    """
    texto = (summary or "").strip()
    if not texto:
        texto = "(o job terminou sem devolver resumo)"
    elif len(texto) > _JOB_SUMMARY_CHARS:
        texto = texto[:_JOB_SUMMARY_CHARS].rstrip() + "\n[…resumo do job truncado]"

    desfecho = {
        "completed": "concluiu",
        "failed": "FALHOU",
        "stopped": "foi interrompido",
        "cancelled": "foi cancelado",
    }.get(status, status)

    linhas = [
        f"Resultado do job de segundo plano ({kind}) — {desfecho}.",
        f"Tarefa: {title}",
    ]
    if pedido:
        # O que foi PEDIDO, não só o que voltou. Sem isto, uma entrada de job
        # que falhou não diz o que ele tentava fazer — e o turno seguinte repete
        # a mesma tentativa por não saber que ela já foi feita.
        linhas.append(f"Pedido: {_uma_linha(pedido, 600)}")
    url = _TAB_URL_RE.search(summary or "")
    if url:
        # Repetida numa linha própria mesmo já estando no corpo: é o dado que o
        # próximo turno mais precisa e o que o `derive_summary` tende a perder.
        linhas.append(f"Onde parou: {url.group(1)}")

    # Toda URL que o trabalho tocou. É o tipo de dado que só existe uma vez —
    # reabrir o navegador para redescobrir um endereço é exatamente o desperdício
    # que o caderno evita.
    vistas = _urls_do_texto(summary, extra=urls, exceto=url.group(1) if url else None)
    if vistas:
        linhas.append("Links vistos:")
        linhas.extend(f"  - {u}" for u in vistas)

    for arquivo in (arquivos or [])[:20]:
        linhas.append(f"Arquivo: {arquivo}")
    if duracao_s is not None:
        linhas.append(f"Durou: {duracao_s:.0f}s")
    linhas.append(f"(job {job_id})")
    if diario:
        # O passo a passo completo mora em disco; a nota guarda o ponteiro para
        # quem precisar auditar sem inchar o corpo com centenas de KB.
        linhas.append(f"(diário completo: {diario})")
    linhas.append("")
    linhas.append(texto)
    return "\n".join(linhas)


def _uma_linha(texto: str, teto: int) -> str:
    achatado = " ".join((texto or "").split())
    return achatado[:teto] + ("…" if len(achatado) > teto else "")


def _urls_do_texto(
    texto: str, *, extra: list[str] | None = None, exceto: str | None = None
) -> list[str]:
    """URLs do resumo, sem repetir, na ordem em que aparecem."""
    vistas: list[str] = []
    for bruto in _URL_RE.findall(texto or "") + list(extra or []):
        limpo = bruto.rstrip(").,;\'\"")
        if limpo and limpo != exceto and limpo not in vistas:
            vistas.append(limpo)
    return vistas[:25]


def render_job_summary(*, title: str, status: str, summary: str) -> str:
    """Linha do ÍNDICE para a nota depois deste job.

    É a parte de maior alavancagem do conserto: esta linha entra no prompt de
    TODO turno, e até aqui toda nota automática carregava o mesmo texto fixo
    ("registro automático do que foi executado nesta sessão") — indistinguíveis
    entre si, como já observado na docstring de ``WorkingMemoryStore.index``.
    Com o achado do job aqui, o modelo consegue DECIDIR se vale abrir a nota.
    """
    corpo = " ".join((summary or "").split())
    # Fora as muletas do runner: elas não ajudam a decidir se vale abrir a nota.
    corpo = re.sub(r"\((?:a aba ficou em|diário do passo a passo)[^)]*\)", "", corpo).strip()
    prefixo = "" if status == "completed" else f"[{status}] "
    if corpo:
        return f"{prefixo}{title}: {corpo}"
    return f"{prefixo}{title}"


def split_entries(body: str) -> tuple[str, list[str]]:
    """Separa o cabeçalho (``# título`` + linha de metadados) das entradas.

    Entrada = bloco que começa com ``## `` no início de uma linha (o carimbo de
    tempo). Corpo sem nenhuma entrada devolve ``(body, [])``.
    """
    head, sep, rest = body.partition("\n## ")
    if not sep:
        return body, []
    return head, [f"## {chunk}" for chunk in rest.split("\n## ")]


#: Teto de CARACTERES do `wm_read` padrão. É a válvula que deixa o caderno
#: escrever à vontade: o corpo guarda tudo, e quem lê recebe o mais recente
#: dentro de um orçamento. Sem isto, entradas fartas (stdout inteiro, resultado
#: de job) fariam um `wm_read` de 4 entradas despejar dezenas de milhares de
#: tokens no contexto — o oposto do que o caderno existe para fazer.
#: `full=true` continua trazendo tudo, e o visor do desktop também.
TAIL_MAX_CHARS = 14000


def render_tail(
    body: str, *, max_entries: int = 4, max_chars: int = TAIL_MAX_CHARS
) -> tuple[str, int]:
    """Últimas ``max_entries`` entradas do corpo. Devolve (texto, omitidas).

    É o default do ``wm_read``: quase sempre o que interessa é o que foi
    concluído por último, e o caderno inteiro pode ser grande.

    Dois limites, e eles são diferentes de propósito: ``max_entries`` é sobre
    RELEVÂNCIA (o que foi feito por último) e ``max_chars`` é sobre CUSTO. Uma
    entrada só já pode passar do orçamento — e aí ela é cortada pelo fim, não
    descartada, porque uma conclusão pela metade ainda evita refazer o trabalho.
    """
    head, entries = split_entries(body)
    omitted = 0
    if len(entries) > max_entries:
        omitted = len(entries) - max_entries
        entries = entries[-max_entries:]

    # Da mais recente para trás: o orçamento é gasto no que interessa primeiro.
    kept: list[str] = []
    cortou = False
    budget = max(0, max_chars - len(head))
    for entry in reversed(entries):
        if len(entry) <= budget:
            kept.insert(0, entry)
            budget -= len(entry)
            continue
        if not kept and budget > 200:
            # Nem a entrada mais nova cabe: entrega o começo dela, que é onde o
            # `render_job_entry` põe o título, o pedido, a URL e o job id.
            kept.insert(0, entry[:budget].rstrip() + "\n_[entrada truncada por tamanho]_")
            cortou = True
        else:
            omitted += 1
        budget = 0

    # `cortou` separado de `omitted`: cortar a única entrada NÃO omite entrada
    # nenhuma, e sem esta distinção o retorno antecipado devolvia o corpo
    # inteiro — justamente o estouro de contexto que o orçamento existe para
    # impedir. (Era um bug de verdade; pego pelo teste da entrada gigante.)
    if not omitted and not cortou:
        return body, 0
    if not omitted:
        return "\n\n".join([head.rstrip(), *kept]), 0
    note = (
        f"_[{omitted} entrada(s) mais antiga(s) omitida(s) — chame wm_read com "
        "full=true para o caderno inteiro]_"
    )
    return "\n\n".join([head.rstrip(), note, *kept]), omitted
