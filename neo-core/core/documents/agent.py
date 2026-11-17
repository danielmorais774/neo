"""DeepReader — lê N documentos por inteiro, em paralelo, fora do contexto do cérebro.

docs/deep-read-plan.md §4. Puro: recebe sandbox, LLM e progresso por protocolo
(``core/documents/types.py``); quem conhece banco, LLMRouter e File Store é o
``core/jobs/runner_deep_read.py``.

Um documento passa por: extração no sandbox → leitura de cada parte (subagente,
JSON com citação literal) → aterramento das citações → consolidação (outro
subagente, que só vê as evidências conferidas) → ``notas/<doc>.md``. Cada
documento que termina reescreve ``sintese.md`` e ``estado.json`` e avisa o
progresso. Falha de um documento não derruba os outros.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from core.documents.extract import EXTRACT_SCRIPT, SCRIPT_PATH, extract_command
from core.documents.grounding import ChunkIndex, normalize
from core.documents.prompts import (
    MAP_SYSTEM,
    REDUCE_SYSTEM,
    map_user,
    parse_json_object,
    reduce_user,
    salvage_findings,
)
from core.documents.render import (
    coverage_text,
    evidence_for_reduce,
    render_note,
    render_state,
    render_synthesis,
    status_line,
    topics_text,
)
from core.documents.types import DocState, Finding, ReaderLLM, ReaderProgress, ReaderSandbox, ReadSpec

logger = logging.getLogger(__name__)

#: Resolve um file_id do catálogo para (caminho no sandbox, nome original).
Stager = Callable[[str], Awaitable[tuple[str, str]]]

_BASE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_EXTRACT_TIMEOUT_S = 900
_EXTRACT_STALL_S = 180
_MAP_MAX_TOKENS = 3000
_REDUCE_MAX_TOKENS = 4000
#: Mesmo com o prompt pedindo 8, um modelo empolgado devolve 40 achados por
#: pergunta numa parte só. Acima disto é repetição.
_MAX_FINDINGS_PER_QUESTION_PER_PART = 12


class ReadError(RuntimeError):
    """Falha de UM documento — vira ``status=falhou`` com o motivo."""


@dataclass
class DeepReadReport:
    docs: list[DocState]
    synthesis_bytes: int
    status: str  # completed | failed

    @property
    def done(self) -> list[DocState]:
        return [d for d in self.docs if d.status == "feito"]


def _num(answer: dict) -> int | None:
    try:
        return int(answer.get("pergunta"))
    except (TypeError, ValueError):
        return None


def doc_base_names(names: list[str]) -> list[str]:
    """Nome de arquivo seguro e ÚNICO por documento (``Relatorio_Charlie``, ``Relatorio_Charlie-2``)."""
    vistos: set[str] = set()
    saida: list[str] = []
    for i, nome in enumerate(names, start=1):
        haste = nome.rsplit("/", 1)[-1]
        haste = haste.rsplit(".", 1)[0] if "." in haste else haste
        base = _BASE_RE.sub("_", haste).strip("._-")[:60] or f"documento-{i}"
        candidato, n = base, 2
        while candidato.lower() in vistos:
            candidato = f"{base}-{n}"
            n += 1
        vistos.add(candidato.lower())
        saida.append(candidato)
    return saida


class DeepReader:
    def __init__(
        self,
        *,
        spec: ReadSpec,
        sandbox: ReaderSandbox,
        llm: ReaderLLM,
        progress: ReaderProgress,
        stager: Stager | None = None,
        doc_concurrency: int = 3,
        llm_concurrency: int = 6,
        chunk_chars: int = 16_000,
        max_parts_per_doc: int = 150,
        max_ocr_pages: int = 60,
        keep_text: bool = False,
        clock: Callable[[], datetime] = datetime.utcnow,
    ) -> None:
        self.spec = spec
        self.sandbox = sandbox
        self.llm = llm
        self.progress = progress
        self.stager = stager
        self.chunk_chars = chunk_chars
        self.max_parts_per_doc = max_parts_per_doc
        self.max_ocr_pages = max_ocr_pages
        self.keep_text = keep_text
        self.clock = clock
        self._doc_sem = asyncio.Semaphore(max(1, doc_concurrency))
        self._llm_sem = asyncio.Semaphore(max(1, llm_concurrency))
        self._lock = asyncio.Lock()
        nomes = [
            f.get("path") or f.get("name") or f"arquivo-{i}"
            for i, f in enumerate(spec.files, start=1)
        ]
        bases = doc_base_names(nomes)
        self.docs: list[DocState] = []
        for i, (f, base) in enumerate(zip(spec.files, bases)):
            caminho = str(f.get("path") or "")
            self.docs.append(
                DocState(
                    index=i,
                    source=caminho or f"file_id:{f.get('file_id')}",
                    name=str(f.get("name") or caminho.rsplit("/", 1)[-1] or base),
                    base=base,
                    path=caminho,
                )
            )

    # ------------------------------------------------------------------

    async def run(self) -> DeepReadReport:
        await self._prepare()
        await asyncio.gather(*(self._process_doc(d) for d in self.docs))
        return await self.finish()

    async def finish(self, *, interrupted: str = "") -> DeepReadReport:
        """Escreve o estado final e limpa o texto extraído.

        Chamado também por quem interrompe (timeout/cancelamento): o que já foi
        lido continua valendo, e a síntese precisa dizer o que ficou de fora.
        """
        if interrupted:
            for d in self.docs:
                if d.status not in ("feito", "falhou"):
                    d.status = "falhou"
                    d.error = f"interrompido ({interrupted}) antes de terminar"
        feitos = [d for d in self.docs if d.status == "feito"]
        status = "completed" if feitos else "failed"
        sintese = await self._write_outputs(status=interrupted or status)
        if not self.keep_text:
            # Cópia integral do documento: o produto são as notas e a síntese.
            await self._safe_run(f"rm -rf {shlex.quote(self.spec.out_dir + '/.texto')}")
        await self.progress.update(
            self.docs, f"Leitura terminada: {status_line(self.docs)}", milestone=True
        )
        return DeepReadReport(docs=self.docs, synthesis_bytes=len(sintese.encode("utf-8")), status=status)

    # ------------------------------------------------------------------

    async def _prepare(self) -> None:
        out = self.spec.out_dir
        res = await self.sandbox.run(
            f"mkdir -p {shlex.quote(out + '/notas')} {shlex.quote(out + '/.texto')} "
            f"{shlex.quote(out + '/.dados')} "
            f"{shlex.quote(SCRIPT_PATH.rsplit('/', 1)[0])}",
            timeout=60,
        )
        if not getattr(res, "ok", False):
            raise RuntimeError(f"não consegui criar a pasta da leitura: {getattr(res, 'stderr', '')[:200]}")
        gravado = await self.sandbox.write_file(SCRIPT_PATH, EXTRACT_SCRIPT)
        if not getattr(gravado, "ok", False):
            raise RuntimeError("não consegui gravar o extrator no sandbox")
        await self._write_outputs(status="running")
        await self.progress.update(
            self.docs, f"Leitura começou: {len(self.docs)} documento(s)", milestone=True
        )

    async def _process_doc(self, doc: DocState) -> None:
        async with self._doc_sem:
            try:
                pendentes = await self._pending_questions(doc)
                if not pendentes:
                    await self._set_status(doc, "feito", f"{doc.name}: já lido antes, mantido", milestone=True)
                    return
                if not doc.path:
                    await self._stage(doc)
                await self._set_status(doc, "extraindo", f"extraindo o texto de {doc.name}")
                partes = await self._extract(doc)
                doc.parts_read = doc.parts_failed = 0
                await self._set_status(doc, "lendo", f"lendo {doc.name} ({len(partes)} partes)")
                await asyncio.gather(*(self._read_part(doc, p, pendentes) for p in partes))
                if doc.parts_read == 0:
                    raise ReadError("nenhuma parte pôde ser lida (o modelo falhou em todas)")
                await self._set_status(doc, "resumindo", f"consolidando {doc.name}")
                await self._reduce(doc, pendentes)
                doc.answered = [q for _, q in self.spec.numbered()]
                nota = render_note(self.spec, doc, self.clock())
                gravado = await self.sandbox.write_file(f"{self.spec.out_dir}/{doc.note_rel}", nota)
                if not getattr(gravado, "ok", False):
                    raise ReadError("não consegui gravar a nota do documento")
                await self._save_state(doc)
                await self._set_status(doc, "feito", f"{doc.name} lido", milestone=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — um documento não derruba os outros
                logger.warning("[DeepRead %s] %s falhou: %s", self.spec.job_id[:8], doc.name, exc)
                doc.error = str(exc)[:300] or exc.__class__.__name__
                await self._set_status(doc, "falhou", f"{doc.name} falhou: {doc.error}", milestone=True)

    async def _pending_questions(self, doc: DocState) -> list[tuple[int, str]]:
        """Perguntas que ESTE documento ainda não respondeu.

        Leitura nova: todas. Leitura estendida (§10): o documento já lido volta de
        ``.dados/<doc>.json`` com respostas e evidências, renumeradas pelo TEXTO da
        pergunta — e só as perguntas novas fazem ele ser relido. É o que impede a
        correção de "e o Foxtrot?" de jogar fora o que já estava apurado dos outros.
        """
        todas = self.spec.numbered()
        if not self.spec.extend_of:
            return todas
        estado = await self._load_state(doc)
        if estado is None:
            return todas
        doc.restore(estado)
        numero = {normalize(q): n for n, q in todas}
        mapa = {i: numero.get(normalize(q)) for i, q in enumerate(doc.answered, start=1)}
        for f in doc.findings:
            f.question = mapa.get(f.question) or 0
        doc.findings = [f for f in doc.findings if f.question]
        respostas = []
        for a in doc.answers:
            try:
                novo = mapa.get(int(a.get("pergunta")))
            except (TypeError, ValueError):
                novo = None
            if novo:
                respostas.append({**a, "pergunta": novo})
        doc.answers = respostas
        feitas = {n for n in mapa.values() if n}
        pendentes = [(n, q) for n, q in todas if n not in feitas]
        if pendentes:
            doc.reused = False
            doc.status = "pendente"
        return pendentes

    async def _load_state(self, doc: DocState) -> dict | None:
        bruto = await self._read_big(f"{self.spec.out_dir}/.dados/{doc.base}.json")
        if not bruto:
            return None
        try:
            estado = json.loads(bruto)
        except json.JSONDecodeError:
            return None
        mesma_origem = estado.get("origem") == doc.source or (doc.path and estado.get("caminho") == doc.path)
        if estado.get("status") != "feito" or not mesma_origem:
            return None
        return estado

    async def _save_state(self, doc: DocState) -> None:
        estado = {**doc.to_state(), "status": "feito"}
        gravado = await self.sandbox.write_file(
            f"{self.spec.out_dir}/.dados/{doc.base}.json", json.dumps(estado, ensure_ascii=False)
        )
        if not getattr(gravado, "ok", False):
            # Sem o estado a leitura continua valendo; só a extensão futura relê.
            logger.warning("[DeepRead %s] estado de %s não gravado", self.spec.job_id[:8], doc.name)

    async def _read_big(self, path: str) -> str | None:
        """Arquivo inteiro do sandbox, em janelas base64 (o exec corta a saída em 60 KB).

        Base64 e não bytes crus: uma janela cortada no meio de um caractere UTF-8
        viraria lixo na junção.
        """
        alvo = shlex.quote(path)
        tamanho = await self.sandbox.run(
            f"python3 -c 'import os,sys;print(os.path.getsize(sys.argv[1]))' {alvo}", timeout=30
        )
        if not getattr(tamanho, "ok", False):
            return None
        try:
            total = 4 * -(-int(str(tamanho.stdout).strip()) // 3)
        except ValueError:
            return None
        partes: list[str] = []
        passo = 48_000
        for inicio in range(0, total, passo):
            janela = await self.sandbox.run(
                "python3 -c 'import base64,sys;"
                "print(base64.b64encode(open(sys.argv[1],\"rb\").read())[int(sys.argv[2]):int(sys.argv[3])].decode())' "
                f"{alvo} {inicio} {inicio + passo}",
                timeout=60,
            )
            if not getattr(janela, "ok", False):
                return None
            partes.append(str(janela.stdout).strip())
        try:
            return base64.b64decode("".join(partes)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    async def _stage(self, doc: DocState) -> None:
        file_id = doc.source.split(":", 1)[-1]
        if self.stager is None:
            raise ReadError("file_id sem catálogo disponível para materializar")
        doc.path, nome = await self.stager(file_id)
        doc.name = nome or doc.name

    async def _extract(self, doc: DocState) -> list[dict]:
        texto_dir = self._texto_dir(doc)
        res = await self.sandbox.run(
            extract_command(doc.path, texto_dir, chunk_chars=self.chunk_chars, max_ocr_pages=self.max_ocr_pages),
            timeout=_EXTRACT_TIMEOUT_S,
            stall_timeout=_EXTRACT_STALL_S,
        )
        linhas = [ln for ln in str(getattr(res, "stdout", "") or "").splitlines() if ln.strip()]
        if not getattr(res, "ok", False) or not linhas:
            erro = str(getattr(res, "stderr", "") or "").strip().splitlines()
            raise ReadError(f"extração falhou: {(erro[-1] if erro else 'sem saída')[:200]}")
        try:
            info = json.loads(linhas[-1])
        except json.JSONDecodeError as exc:
            raise ReadError(f"extração devolveu saída ilegível: {linhas[-1][:120]}") from exc
        doc.unit = str(info.get("unidade") or "página")
        doc.units = int(info.get("unidades") or 0)
        doc.chars = int(info.get("chars") or 0)
        doc.parts = int(info.get("partes") or 0)
        doc.method = str(info.get("metodo") or "")
        doc.ocr_pages = int(info.get("ocr") or 0)
        doc.warnings = [str(a) for a in info.get("avisos") or []][:5]
        if doc.parts == 0:
            raise ReadError("documento sem texto extraível (vazio, protegido ou só imagem sem OCR)")
        lido = await self.sandbox.read_file(f"{texto_dir}/partes.json")
        try:
            partes = json.loads(getattr(lido, "stdout", "") or "[]")
        except json.JSONDecodeError as exc:
            raise ReadError("índice de partes ilegível") from exc
        if len(partes) > self.max_parts_per_doc and not any("grande demais" in a for a in doc.warnings):
            doc.warnings.append(
                f"documento grande demais: lidas só as primeiras {self.max_parts_per_doc} de {len(partes)} partes"
            )
            partes = partes[: self.max_parts_per_doc]
        if not doc.reused and len(doc.topics) != doc.parts:
            doc.topics = [""] * doc.parts
        return partes

    async def _read_part(self, doc: DocState, parte: dict, perguntas: list[tuple[int, str]]) -> None:
        n = int(parte.get("n") or 0)
        lido = await self.sandbox.read_file(f"{self._texto_dir(doc)}/{parte.get('arquivo')}")
        texto = str(getattr(lido, "stdout", "") or "")
        if not getattr(lido, "ok", False) or not texto.strip():
            doc.parts_failed += 1
            return
        user = map_user(
            goal=self.spec.goal,
            questions=perguntas,
            instructions=self.spec.instructions,
            doc_name=doc.name,
            part_n=n,
            part_total=doc.parts,
            unit=doc.unit,
            first=parte.get("de"),
            last=parte.get("ate"),
            text=texto,
        )
        obj: dict | None = None
        for tentativa in (1, 2):
            try:
                async with self._llm_sem:
                    raw = await self.llm.complete(MAP_SYSTEM, user, max_tokens=_MAP_MAX_TOKENS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info("[DeepRead] %s parte %d: LLM falhou (%d/2): %s", doc.name, n, tentativa, exc)
                continue
            try:
                obj = parse_json_object(raw)
                break
            except (json.JSONDecodeError, ValueError):
                resgatados = salvage_findings(raw)
                if resgatados:
                    obj = {"achados": resgatados}
                    break
        if obj is None:
            doc.parts_failed += 1
            return
        self._ground(doc, n, texto, parte.get("de"), obj.get("achados"), {num for num, _ in perguntas})
        if 0 < n <= len(doc.topics) and not doc.topics[n - 1]:
            doc.topics[n - 1] = " ".join(str(obj.get("temas") or "").split())[:240]
        doc.parts_read += 1
        await self.progress.update(
            self.docs, f"lendo {doc.name}: {doc.parts_read}/{doc.parts} partes", milestone=False
        )

    def _ground(self, doc: DocState, part_n: int, texto: str, first_page, achados, ativas: set[int]) -> None:
        indice = ChunkIndex(texto, default_page=first_page if isinstance(first_page, int) else None)
        vistos = {(f.question, normalize(f.quote)[:160]) for f in doc.findings}
        por_pergunta: dict[int, int] = {}
        for bruto in achados if isinstance(achados, list) else []:
            if not isinstance(bruto, dict):
                continue
            try:
                pergunta = int(bruto.get("pergunta"))
            except (TypeError, ValueError):
                continue
            if pergunta not in ativas:
                continue
            if por_pergunta.get(pergunta, 0) >= _MAX_FINDINGS_PER_QUESTION_PER_PART:
                continue
            achado = indice.ground(str(bruto.get("citacao") or ""))
            if not achado.ok:
                doc.quotes_dropped += 1
                continue
            chave = (pergunta, normalize(achado.quote)[:160])
            if chave in vistos:
                continue
            vistos.add(chave)
            por_pergunta[pergunta] = por_pergunta.get(pergunta, 0) + 1
            doc.findings.append(
                Finding(
                    question=pergunta,
                    page=achado.page,
                    quote=" ".join(achado.quote.split())[:400],
                    note=" ".join(str(bruto.get("nota") or "").split())[:300],
                    part=part_n,
                    match=achado.match,
                )
            )
            doc.quotes_kept += 1

    async def _reduce(self, doc: DocState, perguntas: list[tuple[int, str]]) -> None:
        """Consolida as perguntas lidas agora e MESCLA com o que o documento já tinha."""
        numeros = {n for n, _ in perguntas}
        anteriores = [a for a in doc.answers if _num(a) not in numeros]
        user = reduce_user(
            goal=self.spec.goal,
            questions=perguntas,
            instructions=self.spec.instructions,
            doc_name=doc.name,
            coverage=coverage_text(doc),
            evidence=evidence_for_reduce(perguntas, doc),
            topics=topics_text(doc),
        )
        for tentativa in (1, 2):
            try:
                async with self._llm_sem:
                    raw = await self.llm.complete(REDUCE_SYSTEM, user, max_tokens=_REDUCE_MAX_TOKENS)
                obj = parse_json_object(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.info("[DeepRead] %s consolidação falhou (%d/2): %s", doc.name, tentativa, exc)
                continue
            # Documento que já tinha resumo (extensão) mantém o dele: o novo só
            # enxergou as perguntas acrescentadas.
            if not doc.summary:
                doc.summary = str(obj.get("resumo") or "").strip()
            novas = [a for a in obj.get("perguntas") or [] if isinstance(a, dict) and _num(a) in numeros]
            doc.answers = sorted(anteriores + novas, key=lambda a: _num(a) or 0)
            return
        # Sem consolidação o documento ainda tem valor: as evidências conferidas
        # vão para a nota, e o veredito diz só o que dá para afirmar sem LLM.
        doc.warnings.append("a consolidação por LLM falhou: a nota traz só as evidências")
        novas = []
        for num, _ in perguntas:
            total = sum(1 for f in doc.findings if f.question == num)
            novas.append({
                "pergunta": num,
                "veredito": f"{total} evidência(s) — ver notas" if total else "Não encontrado",
            })
        doc.answers = sorted(anteriores + novas, key=lambda a: _num(a) or 0)

    # ------------------------------------------------------------------

    async def _set_status(self, doc: DocState, status: str, note: str, *, milestone: bool = False) -> None:
        doc.status = status
        if milestone:
            await self._write_outputs(status="running")
        await self.progress.update(self.docs, note, milestone=milestone)

    async def _write_outputs(self, *, status: str) -> str:
        """Reescreve ``sintese.md`` e ``estado.json`` inteiros, na ordem de entrada."""
        async with self._lock:
            agora = self.clock()
            sintese = render_synthesis(self.spec, self.docs, agora)
            estado = json.dumps(
                render_state(self.spec, self.docs, agora, status=status), ensure_ascii=False, indent=1
            )
            try:
                await self.sandbox.write_file(f"{self.spec.out_dir}/sintese.md", sintese)
                await self.sandbox.write_file(f"{self.spec.out_dir}/estado.json", estado)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a próxima escrita tenta de novo
                logger.warning("[DeepRead %s] não gravei síntese/estado: %s", self.spec.job_id[:8], exc)
            return sintese

    async def _safe_run(self, command: str) -> None:
        try:
            await self.sandbox.run(command, timeout=60)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DeepRead] limpeza falhou: %s", exc)

    def _texto_dir(self, doc: DocState) -> str:
        return f"{self.spec.out_dir}/.texto/{doc.base}"
