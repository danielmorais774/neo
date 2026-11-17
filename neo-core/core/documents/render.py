"""O que a leitura delegada escreve (docs/deep-read-plan.md §2 e §4.5).

Tudo determinístico. O LLM produz JSON (achados, veredito, análise); a forma dos
arquivos sai daqui, para que o ``sintese.md`` de um job e o do próximo tenham a
mesma cara e o cérebro saiba onde procurar.

Três destinos com orçamentos opostos:

- ``notas/<doc>.md`` — completo: análise + TODAS as evidências conferidas;
- ``sintese.md`` — o que o passo final lê: quadro comparativo + resumo e análise
  por pergunta, sem as citações (essas ficam nas notas);
- caderno e ``result_summary`` — só endereço e status. Conteúdo aqui é o defeito
  D6 da sessão f33632b4.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.documents.types import DocState, Finding, ReadSpec
from core.working_memory.render import stamp

#: Teto das evidências mandadas à redução de UM documento. Acima disto a
#: seleção distribui o orçamento entre as perguntas.
REDUCE_EVIDENCE_CHARS = 40_000
#: Evidências listadas por pergunta na nota. A nota é para achar a citação certa,
#: não para reproduzir o documento.
NOTE_MAX_QUOTES_PER_QUESTION = 40
_VERDICT_CHARS = 90


def _cell(text: Any, limit: int = _VERDICT_CHARS) -> str:
    plano = " ".join(str(text or "").split()).replace("|", "/")
    return plano[:limit].rstrip() + ("…" if len(plano) > limit else "")


def _size(n_bytes: int) -> str:
    return f"{n_bytes / 1024:.0f} KB" if n_bytes >= 1024 else f"{n_bytes} bytes"


def coverage_text(doc: DocState) -> str:
    """A frase de cobertura — é ela que autoriza (ou não) dizer "não encontrado"."""
    unidades = f"{doc.units} {doc.unit}{'s' if doc.units != 1 else ''}"
    if doc.fully_read:
        return f"TOTAL — {unidades} lidas por inteiro em {doc.parts} parte(s)"
    return (
        f"PARCIAL — {doc.parts_read} de {doc.parts} parte(s) lidas ({unidades} no documento); "
        f"{doc.parts_failed} parte(s) falharam"
    )


def evidence_for_reduce(
    questions: list[tuple[int, str]], doc: DocState, *, budget: int = REDUCE_EVIDENCE_CHARS
) -> str:
    """Evidências conferidas agrupadas por pergunta, dentro do orçamento.

    Orçamento dividido entre as perguntas, e dentro de cada uma por ordem de
    página: um documento que fala MUITO de um tema não pode empurrar para fora
    a única citação de outro.
    """
    blocos: list[str] = []
    fatia = max(1500, budget // max(1, len(questions)))
    for num, pergunta in questions:
        achados = sorted(
            (f for f in doc.findings if f.question == num),
            key=lambda f: (f.page if f.page is not None else 10**9, f.part),
        )
        linhas = [f"## Pergunta {num}: {pergunta}"]
        if not achados:
            linhas.append("(nenhuma evidência)")
        gasto = 0
        omitidos = 0
        for f in achados:
            linha = f"- (p. {f.page if f.page is not None else '?'}) “{f.quote}” — {f.note}"
            if gasto + len(linha) > fatia:
                omitidos += 1
                continue
            linhas.append(linha)
            gasto += len(linha)
        if omitidos:
            linhas.append(f"(+{omitidos} evidência(s) semelhantes omitidas por espaço)")
        blocos.append("\n".join(linhas))
    return "\n\n".join(blocos)


def topics_text(doc: DocState) -> str:
    return "\n".join(f"- parte {i}: {t}" for i, t in enumerate(doc.topics, start=1) if t)


def _answer(doc: DocState, num: int) -> dict[str, Any]:
    for a in doc.answers:
        try:
            if int(a.get("pergunta")) == num:
                return a
        except (TypeError, ValueError):
            continue
    return {}


def render_note(spec: ReadSpec, doc: DocState, now: datetime) -> str:
    """``notas/<doc>.md`` — a análise completa de um documento."""
    linhas = [
        f"# {doc.name}",
        "",
        f"> Leitura: {spec.title} · {stamp(now)}",
        f"> Arquivo no sandbox: {doc.path}",
        f"> Cobertura: {coverage_text(doc)} · extração: {doc.method or '?'}"
        + (f" (OCR em {doc.ocr_pages} páginas)" if doc.ocr_pages else ""),
    ]
    for aviso in doc.warnings:
        linhas.append(f"> Aviso: {aviso}")
    linhas += ["", "## Resumo", "", doc.summary or "_(sem resumo — a consolidação falhou; use as evidências abaixo)_", ""]
    for num, pergunta in spec.numbered():
        resposta = _answer(doc, num)
        linhas.append(f"## {num}. {pergunta}")
        linhas.append("")
        linhas.append(f"**Veredito:** {resposta.get('veredito') or '—'}")
        if resposta.get("analise"):
            linhas += ["", str(resposta["analise"]).strip()]
        achados = sorted(
            (f for f in doc.findings if f.question == num),
            key=lambda f: (f.page if f.page is not None else 10**9, f.part),
        )
        linhas += ["", "**Evidências (citações conferidas no texto):**"]
        if achados:
            for f in achados[:NOTE_MAX_QUOTES_PER_QUESTION]:
                pagina = f"p. {f.page}" if f.page is not None else "p. ?"
                linhas.append(f"- {pagina} — “{f.quote}” — {f.note}")
            if len(achados) > NOTE_MAX_QUOTES_PER_QUESTION:
                linhas.append(
                    f"- (+{len(achados) - NOTE_MAX_QUOTES_PER_QUESTION} evidência(s) além destas, "
                    "omitidas para a nota não virar o documento)"
                )
        else:
            linhas.append("- nenhuma")
        linhas.append("")
    linhas += [
        "## Cobertura da leitura",
        "",
        f"- partes lidas: {doc.parts_read}/{doc.parts}"
        + (f" · falharam: {doc.parts_failed}" if doc.parts_failed else ""),
        f"- citações conferidas: {doc.quotes_kept} · descartadas por não baterem com o texto: {doc.quotes_dropped}",
    ]
    if doc.topics:
        linhas += ["", "### Temas por parte", "", topics_text(doc)]
    return "\n".join(linhas).rstrip() + "\n"


def _status_label(doc: DocState) -> str:
    return {
        "feito": "✓ lido" if doc.fully_read else "✓ lido em parte",
        "falhou": "✗ falhou",
        "pendente": "⏳ pendente",
    }.get(doc.status, f"… {doc.status}")


#: Teto da ``sintese.md`` em BYTES: ela tem de caber inteira numa chamada de
#: ``neo_read_file`` (32 KB). Na sessão ad3be3bf ela saiu com 42,8 KB, a leitura
#: cortou no quarto documento e a conclusão ficou sem o Echo — duas vezes.
SYNTHESIS_MAX_BYTES = 26_000

#: Degraus de compactação: (análise por pergunta, resumo, veredito), em chars.
#: ``0`` na análise = só o veredito na síntese; a análise inteira segue na nota.
_SYNTHESIS_STEPS = (
    (None, None, _VERDICT_CHARS),
    (1200, 1200, _VERDICT_CHARS),
    (800, 900, _VERDICT_CHARS),
    (500, 700, _VERDICT_CHARS),
    (300, 500, _VERDICT_CHARS),
    (0, 400, _VERDICT_CHARS),
    (0, 200, 60),
    (0, 0, 40),
)


def _clip(text: Any, limit: int | None, note_rel: str) -> str:
    plano = str(text or "").strip()
    if limit is None or len(plano) <= limit:
        return plano
    corte = plano[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{corte}… _(continua em {note_rel})_"


def render_synthesis(
    spec: ReadSpec, docs: list[DocState], now: datetime, *, max_bytes: int = SYNTHESIS_MAX_BYTES
) -> str:
    """``sintese.md`` — o que o passo final lê. Sempre cabe numa leitura só.

    Reescrita inteira a cada documento. Quando não cabe, encurta análise e resumo
    em degraus (o quadro e os vereditos de TODOS os documentos ficam sempre): é
    melhor o passo final ver todos os candidatos com menos texto do que ver
    quatro com texto inteiro e não saber que existia um quinto.
    """
    texto = ""
    for analise, resumo, veredito in _SYNTHESIS_STEPS:
        texto = _render_synthesis(
            spec, docs, now, analysis_limit=analise, summary_limit=resumo, verdict_limit=veredito
        )
        if len(texto.encode("utf-8")) <= max_bytes:
            break
    return texto


def _render_synthesis(
    spec: ReadSpec,
    docs: list[DocState],
    now: datetime,
    *,
    analysis_limit: int | None,
    summary_limit: int | None,
    verdict_limit: int,
) -> str:
    feitos = [d for d in docs if d.status == "feito"]
    falhas = [d for d in docs if d.status == "falhou"]
    compactada = analysis_limit is not None
    linhas = [
        f"# Síntese — {spec.title}",
        "",
        f"> Objetivo: {spec.goal}",
        f"> Documentos: {len(docs)} · concluídos {len(feitos)} · falhas {len(falhas)} · atualizado {stamp(now)}",
        "> Detalhes e citações literais com página de cada documento: `notas/<documento>.md` nesta pasta.",
    ]
    if compactada:
        linhas.append(
            "> Compactada para caber numa leitura: o que foi cortado aqui continua inteiro na nota do documento."
        )
    linhas += ["", "## Perguntas", ""]
    linhas += [f"{i}. {q}" for i, q in spec.numbered()]
    linhas += ["", "## Documentos desta leitura", ""]
    linhas += [f"- {d.name} — {_status_label(d)}" for d in docs]
    linhas += ["", "## Quadro comparativo", ""]
    cabecalho = "| Documento | " + " | ".join(str(i) for i, _ in spec.numbered()) + " |"
    linhas += [cabecalho, "|" + "---|" * (len(spec.questions) + 1)]
    for d in docs:
        if d.status == "feito":
            celulas = [_cell(_answer(d, i).get("veredito") or "—", verdict_limit) for i, _ in spec.numbered()]
        else:
            celulas = [_status_label(d)] * len(spec.questions)
        linhas.append(f"| {_cell(d.name, 60)} | " + " | ".join(celulas) + " |")
    linhas += ["", "## Por documento", ""]
    for d in docs:
        linhas.append(f"### {d.name}")
        if d.status != "feito":
            motivo = f" — {d.error}" if d.error else ""
            linhas += [f"_{_status_label(d)}{motivo}_", ""]
            continue
        linhas += [f"_cobertura: {coverage_text(d)} · notas completas: {d.note_rel}_", ""]
        if summary_limit != 0:
            linhas += [_clip(d.summary, summary_limit, d.note_rel) or "_(sem resumo)_", ""]
        for i, pergunta in spec.numbered():
            resposta = _answer(d, i)
            linhas.append(f"**{i}. {pergunta}** — {resposta.get('veredito') or '—'}")
            if resposta.get("analise") and analysis_limit != 0:
                linhas.append(_clip(resposta["analise"], analysis_limit, d.note_rel))
            linhas.append("")
    return "\n".join(linhas).rstrip() + "\n"


def render_state(spec: ReadSpec, docs: list[DocState], now: datetime, *, status: str) -> dict[str, Any]:
    return {
        "job_id": spec.job_id,
        "titulo": spec.title,
        "objetivo": spec.goal,
        "perguntas": spec.questions,
        "status": status,
        "atualizado": now.isoformat(timespec="seconds") + "Z",
        "documentos": [d.to_json() for d in docs],
    }


def status_line(docs: list[DocState]) -> str:
    """``3/6 feitos (a, b, c) · em andamento: d · pendentes: e · falhas: f``."""
    feitos = [d.name for d in docs if d.status == "feito"]
    andamento = [d.name for d in docs if d.status in ("extraindo", "lendo", "resumindo")]
    pendentes = [d.name for d in docs if d.status == "pendente"]
    falhas = [d.name for d in docs if d.status == "falhou"]
    partes = [f"{len(feitos)}/{len(docs)} feitos" + (f" ({', '.join(feitos)})" if feitos else "")]
    if andamento:
        partes.append("em andamento: " + ", ".join(andamento))
    if pendentes:
        partes.append("pendentes: " + ", ".join(pendentes))
    if falhas:
        partes.append("falhas: " + ", ".join(falhas))
    return " · ".join(partes)


def render_memory_note(spec: ReadSpec, docs: list[DocState], now: datetime) -> tuple[str, str]:
    """(corpo, linha do índice) da nota rolante do caderno. Só endereço e status."""
    perguntas = " ".join(f"{i}) {q}" for i, q in spec.numbered())
    corpo = "\n".join([
        f"Leitura delegada (deep_read, job {spec.job_id[:8]}) — {spec.goal}",
        f"Pasta: {spec.out_dir}/ → notas/<documento>.md (um por documento) + sintese.md",
        f"Status {stamp(now)}: {status_line(docs)}",
        f"Perguntas: {perguntas}",
        "O conteúdo da análise NÃO fica aqui: para concluir ou responder sobre estes "
        "documentos, leia sintese.md (e a nota do documento, se precisar de citação).",
    ])
    feitos = sum(1 for d in docs if d.status == "feito")
    resumo = f"leitura '{spec.title}': {feitos}/{len(docs)} documentos · {spec.out_dir}/sintese.md"
    return corpo, resumo[:240]


def render_result_summary(spec: ReadSpec, docs: list[DocState], *, synthesis_bytes: int | None) -> str:
    """O ``result_summary`` do job — vai para a continuação, o aviso e o caderno."""
    feitos = [d for d in docs if d.status == "feito"]
    inteiros = [d for d in feitos if d.fully_read]
    unidades = sum(d.units for d in feitos)
    partes = sum(d.parts_read for d in feitos)
    citacoes = sum(d.quotes_kept for d in feitos)
    if not feitos:
        cabeca = f"Leitura FALHOU: nenhum dos {len(docs)} documento(s) pôde ser lido."
    else:
        cabeca = (
            f"Leitura concluída: {len(feitos)}/{len(docs)} documento(s) "
            f"({len(inteiros)} por inteiro; {unidades} páginas/unidades em {partes} partes; "
            f"{citacoes} citações conferidas)."
        )
    reaproveitados = [d for d in feitos if d.reused]
    if reaproveitados:
        cabeca += (
            f" Leitura ESTENDIDA: {len(reaproveitados)} documento(s) já lidos antes foram mantidos, "
            "e a síntese agora cobre todos juntos."
        )
    linhas = [cabeca, f"Pasta: {spec.out_dir}/"]
    if feitos:
        tamanho = f" ({_size(synthesis_bytes)}, cabe inteira numa leitura)" if synthesis_bytes else ""
        linhas.append(
            f"- sintese.md{tamanho}: quadro comparativo + resumo e análise por pergunta de "
            f"TODOS os {len(docs)} documentos — é ELE que você lê para a conclusão."
        )
        linhas.append(
            "- notas/<documento>.md: análise completa de cada documento, com as citações "
            "literais e as páginas."
        )
    for d in docs:
        if d.status == "feito":
            extra = "" if d.fully_read else f" (parcial: {d.parts_read}/{d.parts} partes)"
            extra += " (já lido antes, mantido)" if d.reused else ""
            linhas.append(f"  ✓ {d.name} → {d.note_rel}{extra}")
        else:
            linhas.append(f"  ✗ {d.name}: {d.error or d.status}")
    linhas.append("(O conteúdo da análise não vem aqui de propósito — abra os arquivos.)")
    return "\n".join(linhas)


def render_continuation_contract(spec_json: dict[str, Any]) -> str:
    """O que a continuação de uma leitura PRECISA cumprir — vai no texto do turno.

    Duas falhas da sessão ad3be3bf viram regra escrita onde o turno lê primeiro:
    a conclusão que esqueceu um documento que estava na síntese, e a "correção"
    que escreveu um arquivo novo do zero e perdeu o que a conclusão anterior dizia.
    """
    nomes = [
        str((f or {}).get("name") or str((f or {}).get("path") or "").rsplit("/", 1)[-1])
        for f in spec_json.get("files") or []
        if isinstance(f, dict)
    ]
    perguntas = [str(q) for q in spec_json.get("questions") or []]
    linhas = [
        "",
        "Contrato desta leitura (vale mesmo que a ordem acima diga outra coisa):",
        f"- A conclusão cobre TODOS os {len(nomes)} documentos — {', '.join(nomes)} — e TODAS as "
        f"{len(perguntas)} perguntas. Antes do file_save, confira o arquivo contra o quadro da sintese.md: "
        "documento que não aparece nele é erro, não escolha.",
        "- Leia a sintese.md INTEIRA (ela cabe numa leitura). Se o resultado da leitura disser "
        "'truncated', leia o resto antes de escrever.",
    ]
    entregas = [e for e in spec_json.get("entregas_anteriores") or [] if isinstance(e, dict)]
    if entregas:
        caminho = entregas[0].get("path") or entregas[0].get("name")
        linhas.append(
            f"- Já existe conclusão entregue a partir desta leitura: {caminho}. Isto é uma ATUALIZAÇÃO: "
            "leia esse arquivo primeiro, mantenha tudo o que continua valendo (não troque análise por "
            "'sem menção'), acrescente/ajuste só o que a leitura nova trouxe e grave no MESMO caminho; "
            "depois file_save. Não crie um arquivo novo com outro nome."
        )
    return "\n".join(linhas)


__all__ = [
    "Finding",
    "coverage_text",
    "evidence_for_reduce",
    "render_memory_note",
    "render_continuation_contract",
    "render_note",
    "render_result_summary",
    "render_state",
    "render_synthesis",
    "SYNTHESIS_MAX_BYTES",
    "status_line",
    "topics_text",
]
