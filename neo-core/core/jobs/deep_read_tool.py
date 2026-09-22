"""Enfileiração da ``deep_read`` (docs/deep-read-plan.md §5 e §10).

Tudo que dá para saber ANTES de gastar minutos de leitura é conferido aqui, sem
LLM quando possível — a sessão ad3be3bf mostrou o custo de descobrir depois:

- **inventário**: o usuário citou uma pasta do disco dele → a leitura tem de ter
  TODOS os documentos que a listagem real dessa pasta mostrou. O Neo tinha lido
  as cópias velhas do /workspace e o Foxtrot nunca entrou;
- **cópia velha**: ``X.txt`` extraído numa sessão anterior ao lado de ``X.pdf`` →
  lê o PDF (o .txt não tem páginas: a análise saiu com "5 páginas" para 5 planos);
- **critérios**: as perguntas cobrem o que o usuário pediu? (uma chamada barata;
  as 9 perguntas genéricas daquele teste não tinham "regulação", e a conclusão
  disse "sem menção" para todos);
- **extensão**: "faltou o Foxtrot" acrescenta à MESMA leitura — mesmas perguntas,
  mesma pasta, só o novo é lido — e aponta a conclusão já entregue para ser
  ATUALIZADA, não reescrita.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import unicodedata
import uuid
from typing import Any, Awaitable, Callable
from uuid import UUID

from db.models.agent_job import AgentJob

logger = logging.getLogger(__name__)

#: Verificador de cobertura dos critérios: (pedidos do usuário, objetivo,
#: perguntas, instruções) -> critérios que ficaram de fora.
CriteriaChecker = Callable[[list[str], str, list[str], str], Awaitable[list[str]]]

_CONTINUATION_PREFIX = "[Continuação automática"
_USER_PATH_RE = re.compile(r"(?:~|/Users|/home)/[^\s'\"`<>|]+")
#: Sempre documento.
_STRONG_EXTS = {"pdf", "doc", "docx", "odt", "rtf", "pptx", "ppt", "epub"}
#: Documento só se tiver corpo — um FONTES.md de 3 KB é índice, não plano.
_WEAK_EXTS = {"md", "txt", "csv", "xlsx", "xls", "ods", "html", "htm"}
_WEAK_MIN_BYTES = 50_000
_ANY_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_LS_LONG_RE = re.compile(r"^[-dlcbps][rwxsStT-]{9}[@+.]?\s")


def as_list(value: Any, *, split_commas: bool = False) -> list[str]:
    """Lista de strings a partir do que o modelo mandou (array, JSON em texto ou linhas)."""
    if isinstance(value, (list, tuple)):
        itens = [str(v) for v in value]
    else:
        texto = str(value or "").strip()
        itens = []
        if texto.startswith("["):
            try:
                carregado = json.loads(texto)
                if isinstance(carregado, list):
                    itens = [str(v) for v in carregado]
            except json.JSONDecodeError:
                itens = []
        if not itens:
            itens = texto.splitlines()
            if split_commas and len(itens) == 1:
                itens = re.split(r"[;,]\s*", itens[0])
    return [i.strip().strip("\"'") for i in itens if i and i.strip()]


def slug(text: str, max_len: int = 40) -> str:
    plano = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    plano = re.sub(r"[^a-zA-Z0-9]+", "-", plano).strip("-").lower()
    return plano[:max_len].rstrip("-") or "leitura"


def stem(name: str) -> str:
    """Chave de comparação de documento: sem pasta, sem extensão, sem acento/pontuação."""
    base = str(name or "").rstrip("/").rsplit("/", 1)[-1]
    base = _ANY_FILE_EXT_RE.sub("", base)
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", base)


def _qkey(question: str) -> str:
    return " ".join(str(question or "").lower().split())


def _tail(path: str) -> str:
    """Caminho sem a raiz do usuário: ``~/Documents/x`` e ``/Users/daniel/Documents/x`` batem."""
    limpo = str(path or "").strip().strip("\"'").rstrip("/.,;:)")
    return re.sub(r"^(?:~|/Users/[^/]+|/home/[^/]+)", "", limpo).rstrip("/")


def user_dirs(requests: list[str]) -> list[str]:
    """Pastas do disco do usuário citadas nos pedidos (caminhos sem extensão de arquivo)."""
    vistas: list[str] = []
    for texto in requests:
        for bruto in _USER_PATH_RE.findall(texto or ""):
            caminho = bruto.rstrip("/.,;:)")
            if _ANY_FILE_EXT_RE.search(caminho.rsplit("/", 1)[-1]):
                continue
            if caminho not in vistas:
                vistas.append(caminho)
    return vistas


def parse_listing(evidence: list[str], directory: str) -> list[tuple[str, int | None]] | None:
    """(nome, tamanho) dos arquivos de ``directory`` na listagem mais RECENTE que o Neo viu.

    ``None`` = nenhum ``ls``/``find`` dessa pasta rodou na máquina do usuário nesta
    sessão (ou a evidência se perdeu num restart): aí não dá para garantir nada.
    """
    alvo = _tail(directory)
    for blob in reversed(evidence or []):
        comando, _, saida = str(blob).partition("\n")
        comando = comando.lstrip("$ ").replace('"', "").replace("'", "")
        if not re.match(r"^\s*(?:cd\s+\S+\s*&&\s*)?(?:ls|find)\b", comando):
            continue
        if not any(_tail(tok) == alvo for tok in comando.split()):
            continue
        arquivos: list[tuple[str, int | None]] = []
        for linha in saida.splitlines():
            linha = linha.rstrip()
            if not linha or linha.startswith("total "):
                continue
            if _LS_LONG_RE.match(linha):
                partes = linha.split(None, 8)
                if len(partes) == 9 and linha[0] == "-":
                    tamanho = int(partes[4]) if partes[4].isdigit() else None
                    arquivos.append((partes[8], tamanho))
                continue
            if linha.startswith("/"):
                pai, _, nome = linha.rpartition("/")
                if _tail(pai) == alvo and nome:
                    arquivos.append((nome, None))
                continue
            if "/" not in linha and _ANY_FILE_EXT_RE.search(linha):
                arquivos.append((linha, None))
        return arquivos
    return None


def is_document(name: str, size: int | None) -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in _STRONG_EXTS:
        return True
    return ext in _WEAK_EXTS and size is not None and size >= _WEAK_MIN_BYTES


def read_key(files: list, questions: list) -> tuple:
    alvos = sorted(
        str((a or {}).get("path") or (a or {}).get("file_id") or "") for a in files if isinstance(a, dict)
    )
    return tuple(alvos), tuple(_qkey(q) for q in questions)


class DeepReadEnqueuer:
    """Contexto do turno para a ``deep_read`` (quem chama é o JobToolExecutor)."""

    def __init__(
        self,
        *,
        db: Any,
        job_manager: Any,
        user_id: UUID | None,
        session_id: UUID | None,
        file_store: Any = None,
        user_message: str = "",
        disk_evidence: Callable[[], list[str]] | None = None,
        criteria_checker: CriteriaChecker | None = None,
    ) -> None:
        self.db = db
        self.jm = job_manager
        self.user_id = user_id
        self.session_id = session_id
        self.file_store = file_store
        self.user_message = user_message or ""
        self.disk_evidence = disk_evidence
        self.criteria_checker = criteria_checker

    async def enqueue(self, args: dict[str, Any]) -> dict[str, Any]:
        from shared.config import get_settings

        settings = get_settings()
        if not getattr(settings, "deep_read_enabled", False):
            return {"ok": False, "error": "deep_read está desligado na config."}
        sandbox = getattr(self.jm, "sandbox", None)
        if sandbox is None:
            return {"ok": False, "error": "o sandbox do Neo não está ligado — sem ele não há onde ler."}

        base: AgentJob | None = None
        if str(args.get("extend") or "").strip():
            base, erro = await self._base_reading(str(args["extend"]).strip())
            if erro:
                return {"ok": False, "error": erro}
        spec_base = dict((base.spec_json if base else None) or {})

        arquivos = as_list(args.get("files"), split_commas=True)
        perguntas = [" ".join(q.split())[:400] for q in as_list(args.get("questions"))]
        goal = " ".join(str(args.get("goal") or spec_base.get("goal") or "").split())[:500]
        titulo_base = str(base.title or "").removeprefix("Leitura: ").removeprefix("Leitura (ampliada): ") if base else ""
        title = " ".join(str(args.get("title") or titulo_base or goal or "Leitura de documentos").split())[:200]

        if base is None and not arquivos:
            return {"ok": False, "error": "files vazio: passe TODOS os documentos da tarefa (caminhos no /workspace ou file_ids)."}
        if base is None and not perguntas:
            return {"ok": False, "error": "questions vazio: diga o que procurar em cada documento, uma pergunta por item."}

        resolvidos, problemas = await self._resolve_files(arquivos, sandbox)
        if problemas:
            return {"ok": False, "error": "nenhuma leitura foi aberta: há arquivo que não está no seu sandbox.", "problemas": problemas}

        # Extensão: o que já estava na leitura continua nela, na mesma ordem.
        if base is not None:
            anteriores = [f for f in spec_base.get("files") or [] if isinstance(f, dict)]
            chaves = {stem(f.get("path") or f.get("name") or "") for f in anteriores}
            novos = [f for f in resolvidos if stem(f.get("path") or f.get("name") or "") not in chaves]
            perguntas_base = [str(q) for q in spec_base.get("questions") or []]
            vistas = {_qkey(q) for q in perguntas_base}
            perguntas_novas = [q for q in perguntas if _qkey(q) not in vistas]
            if not novos and not perguntas_novas:
                return {
                    "ok": False,
                    "error": (
                        f"nada novo para ler: a leitura {spec_base.get('out_dir')} já cobre esses documentos "
                        "e perguntas. Leia a sintese.md dela em vez de abrir outra."
                    ),
                }
            resolvidos = anteriores + novos
            perguntas = perguntas_base + perguntas_novas
        else:
            novos, perguntas_novas = resolvidos, perguntas

        teto = int(getattr(settings, "deep_read_max_files", 30) or 30)
        if len(resolvidos) > teto:
            return {"ok": False, "error": f"{len(resolvidos)} documentos passam do teto de {teto} por leitura — divida em lotes."}
        perguntas = perguntas[:12]

        pedidos = await self._user_requests()
        source_dir = str(args.get("source_dir") or spec_base.get("source_dir") or "").strip()
        excluidos = as_list(args.get("excluded"), split_commas=True) + [
            str(x) for x in spec_base.get("excluded") or []
        ]
        faltando = self._inventory_gap(resolvidos, pedidos, source_dir, excluidos)
        if faltando:
            return faltando

        instructions = str(args.get("instructions") or "").strip()
        if spec_base.get("instructions") and instructions not in str(spec_base.get("instructions")):
            instructions = f"{spec_base['instructions']}\n{instructions}".strip()
        elif not instructions:
            instructions = str(spec_base.get("instructions") or "")

        gap = await self._criteria_gap(pedidos, goal or title, perguntas, instructions)
        if gap:
            return {
                "ok": False,
                "error": (
                    "nenhuma leitura foi aberta: as perguntas não cobrem critério que o usuário pediu — "
                    + "; ".join(gap)
                    + ". Refaça com UMA pergunta por critério dele, com as palavras dele."
                ),
                "criterios_faltando": gap,
            }

        existente = await self._duplicate(resolvidos, perguntas)
        if existente is not None:
            return {
                "ok": True, "job_id": str(existente.id), "title": existente.title, "status": existente.status,
                "pasta": (existente.spec_json or {}).get("out_dir"), "reused": True,
                "note": "Essa mesma leitura já está rodando nesta conversa — NÃO abri outra. Avise o usuário em uma frase e encerre.",
            }

        job_id = uuid.uuid4()
        out_dir = spec_base.get("out_dir") or f"/workspace/analises/{slug(title)}-{str(job_id)[:8]}"
        spec: dict[str, Any] = {
            "goal": goal or title,
            "questions": perguntas,
            "files": resolvidos,
            "out_dir": out_dir,
        }
        if instructions:
            spec["instructions"] = instructions[:2000]
        if source_dir:
            spec["source_dir"] = source_dir
        if excluidos:
            spec["excluded"] = sorted(set(excluidos))[:50]
        entregas: list[dict[str, str]] = []
        if base is not None:
            spec["extend_of"] = str(base.id)
            entregas = await self._deliverables(out_dir)
            if entregas:
                spec["entregas_anteriores"] = entregas
        then = str(args.get("then") or "").strip()
        if then:
            spec["then"] = then[:4000]

        rotulo = "Leitura (ampliada)" if base is not None else "Leitura"
        job = await self.db.create_agent_job(
            AgentJob(
                id=job_id, user_id=self.user_id, session_id=self.session_id, kind="deep_read",
                title=f"{rotulo}: {title}"[:256], spec_json=spec, status="queued", notify_policy="chat",
            )
        )
        try:
            self.jm.poke()
        except Exception:  # noqa: BLE001
            pass
        return self._receipt(job, out_dir, resolvidos, novos, perguntas_novas, base, entregas, bool(then))

    # ------------------------------------------------------------------

    def _receipt(self, job, out_dir, todos, novos, perguntas_novas, base, entregas, tem_then) -> dict[str, Any]:
        if base is None:
            nota = (
                f"Leitura delegada enfileirada: cada documento será lido POR INTEIRO por subagentes, "
                f"em paralelo, e vira notas/<documento>.md + sintese.md em {out_dir}. NÃO leia estes "
                "documentos você mesmo agora (nem neo_read_file, nem cat/grep/pdftotext no texto) — é "
                "exatamente o que esta leitura evita. Avise o usuário em uma frase que está lendo tudo "
                "e encerre a resposta."
            )
        else:
            nota = (
                f"Leitura AMPLIADA em {out_dir}: {len(novos)} documento(s) novo(s) e "
                f"{len(perguntas_novas)} pergunta(s) nova(s); os {len(todos) - len(novos)} já lidos são "
                "mantidos (nada do que já foi apurado se perde) e a sintese.md passa a cobrir TODOS juntos."
            )
            if entregas:
                caminhos = ", ".join(e.get("path") or e.get("name") or "" for e in entregas)
                nota += (
                    f" Conclusão já entregue a partir desta leitura: {caminhos}. Quando terminar, ela é "
                    "ATUALIZADA (ler o arquivo, manter o que vale, acrescentar o que mudou, gravar no MESMO "
                    "caminho) — não escrita do zero. Avise o usuário em uma frase e encerre."
                )
        nota += (
            " Quando terminar você continua sozinho com o `then`."
            if tem_then
            else " Sem `then`: ao terminar o usuário só é avisado; a análise final fica para quando ele pedir."
        )
        return {
            "ok": True, "job_id": str(job.id), "title": job.title, "status": "queued",
            "pasta": out_dir, "documentos": len(todos), "note": nota,
        }

    async def _resolve_files(self, arquivos: list[str], sandbox: Any) -> tuple[list[dict[str, str]], list[str]]:
        """Cada item vira ``{"path"}`` (sandbox) ou ``{"file_id", "name"}`` (catálogo)."""
        resolvidos: list[dict[str, str]] = []
        problemas: list[str] = []
        caminhos: list[str] = []
        vistos: set[str] = set()
        for item in arquivos:
            try:
                UUID(item)
                eh_id = True
            except ValueError:
                eh_id = False
            if eh_id:
                entrada = None
                if self.file_store is not None:
                    try:
                        entrada = await self.file_store.get(item)
                    except Exception:  # noqa: BLE001
                        entrada = None
                if entrada is None or getattr(entrada, "status", "available") != "available":
                    problemas.append(f"{item}: file_id não existe (ou não está disponível) no catálogo")
                elif item not in vistos:
                    vistos.add(item)
                    resolvidos.append({"file_id": item, "name": str(getattr(entrada, "name", "") or "")})
                continue
            if item.startswith(("~", "/Users/", "/home/", "/Volumes/", "/mnt/")) or re.match(r"^[A-Za-z]:\\", item):
                problemas.append(
                    f"{item}: está no disco do USUÁRIO, não no seu sandbox — traga antes com "
                    "request_user_file(mode='path', path=..., stage=true) e passe o caminho "
                    "/workspace/files/<nome> que voltar"
                )
                continue
            try:
                caminho = sandbox.resolve_path(item if "/" in item else f"files/{item}")
            except ValueError as exc:
                problemas.append(f"{item}: {exc}")
                continue
            if caminho not in vistos:
                vistos.add(caminho)
                caminhos.append(caminho)
                resolvidos.append({"path": caminho})
        if not caminhos:
            return resolvidos, problemas
        # Uma consulta só: existe? e, para .txt, existe o PDF irmão?
        teste = "; ".join(
            f"[ -f {shlex.quote(c)} ] && echo 1 || echo 0; "
            f"[ -f {shlex.quote(_ANY_FILE_EXT_RE.sub('', c) + '.pdf')} ] && echo 1 || echo 0"
            for c in caminhos
        )
        res = await sandbox.run(teste, timeout=30)
        flags = str(getattr(res, "stdout", "") or "").split()
        flags += ["0"] * (2 * len(caminhos) - len(flags))
        trocas: dict[str, str] = {}
        for i, caminho in enumerate(caminhos):
            existe, tem_pdf = flags[2 * i] == "1", flags[2 * i + 1] == "1"
            if caminho.lower().endswith(".txt") and tem_pdf:
                # Texto extraído numa sessão anterior: sem páginas, e pode ser de
                # outra versão. O original é a fonte.
                trocas[caminho] = _ANY_FILE_EXT_RE.sub("", caminho) + ".pdf"
            elif not existe:
                problemas.append(
                    f"{caminho}: não existe no seu sandbox (confira com neo_list_files files; "
                    "se o arquivo é do usuário, traga com request_user_file stage=true)"
                )
        if trocas:
            unicos: list[dict[str, str]] = []
            vistos_final: set[str] = set()
            for f in resolvidos:
                if "path" in f:
                    f = {"path": trocas.get(f["path"], f["path"])}
                chave = f.get("path") or f.get("file_id") or ""
                if chave not in vistos_final:
                    vistos_final.add(chave)
                    unicos.append(f)
            resolvidos = unicos
        return resolvidos, problemas

    async def _user_requests(self) -> list[str]:
        """O que o USUÁRIO disse nas últimas mensagens (continuação automática não conta)."""
        pedidos: list[str] = []
        if self.session_id is not None:
            try:
                msgs = await self.db.fetch_session_messages(self.session_id, limit=6, roles=["user"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("[deep_read] histórico indisponível: %s", exc)
                msgs = []
            for m in msgs or []:
                texto = str(getattr(m, "content", "") or "").strip()
                if texto and not texto.startswith(_CONTINUATION_PREFIX):
                    pedidos.append(texto)
        atual = self.user_message.strip()
        if atual and not atual.startswith(_CONTINUATION_PREFIX) and atual not in pedidos:
            pedidos.append(atual)
        return pedidos[-4:]

    def _inventory_gap(
        self, arquivos: list[dict[str, str]], pedidos: list[str], source_dir: str, excluidos: list[str]
    ) -> dict[str, Any] | None:
        """Recusa quando a leitura não tem todos os documentos da pasta que o usuário citou."""
        if source_dir.lower() in ("nenhuma", "none", "-", "n/a"):
            return None
        pastas = user_dirs(pedidos)
        if not source_dir:
            if not pastas:
                return None
            return {
                "ok": False,
                "error": (
                    f"nenhuma leitura foi aberta: o usuário citou a pasta {pastas[0]} no disco dele. Passe "
                    "source_dir com ela — a leitura será conferida contra a listagem REAL dessa pasta, porque "
                    "cópias no seu /workspace podem ser velhas ou incompletas. Se ela não é a origem dos "
                    "documentos (é só destino, por exemplo), passe source_dir=\"nenhuma\"."
                ),
            }
        listagem = parse_listing(self.disk_evidence() if self.disk_evidence else [], source_dir)
        if listagem is None:
            return {
                "ok": False,
                "error": (
                    f"nenhuma leitura foi aberta: não vi a listagem de {source_dir} nesta conversa. Rode "
                    f"run_command `ls -la \"{source_dir}\"`, traga CADA documento de lá com request_user_file "
                    "(mode=\"path\", stage=true) e chame deep_read de novo."
                ),
            }
        presentes = {stem(f.get("path") or f.get("name") or "") for f in arquivos}
        fora = {stem(x) for x in excluidos}
        faltam = [
            nome for nome, tamanho in listagem
            if is_document(nome, tamanho) and stem(nome) not in presentes and stem(nome) not in fora
        ]
        if not faltam:
            return None
        return {
            "ok": False,
            "error": (
                f"nenhuma leitura foi aberta: {len(faltam)} documento(s) de {source_dir} ficaram de fora: "
                + ", ".join(faltam)
                + ". Traga cada um com request_user_file(mode=\"path\", stage=true) e inclua em files. Só "
                "se o usuário pediu para deixar algum de fora, passe o nome em `excluded`."
            ),
            "faltando": faltam,
        }

    async def _criteria_gap(self, pedidos: list[str], goal: str, perguntas: list[str], instructions: str) -> list[str]:
        if self.criteria_checker is None or not pedidos:
            return []
        try:
            faltando = await self.criteria_checker(pedidos, goal, perguntas, instructions)
        except Exception as exc:  # noqa: BLE001 — verificador fora do ar não bloqueia a leitura
            logger.warning("[deep_read] verificador de critérios falhou: %s", exc)
            return []
        return [str(c).strip() for c in faltando or [] if str(c).strip()][:5]

    async def _base_reading(self, ref: str) -> tuple[AgentJob | None, str]:
        """A leitura a estender — a MAIS RECENTE da mesma pasta (extensão de extensão acumula)."""
        if self.user_id is None:
            return None, "extend precisa de um usuário identificado."
        try:
            jobs = await self.db.list_agent_jobs(self.user_id, limit=100)
        except Exception as exc:  # noqa: BLE001
            return None, f"não consegui consultar as leituras anteriores: {exc}"
        leituras = [j for j in jobs or [] if j.kind == "deep_read"]
        alvo = None
        ref_limpa = ref.rstrip("/")
        if ref_limpa.endswith("/sintese.md"):
            ref_limpa = ref_limpa.rsplit("/", 1)[0]
        for j in leituras:
            pasta = str((j.spec_json or {}).get("out_dir") or "").rstrip("/")
            if str(j.id) == ref or str(j.id).startswith(ref) or pasta == ref_limpa or pasta.endswith("/" + ref_limpa):
                alvo = j
                break
        if alvo is None:
            return None, f"não achei a leitura '{ref}' para estender (passe o job_id ou a pasta /workspace/analises/...)."
        pasta = (alvo.spec_json or {}).get("out_dir")
        cabeca = next((j for j in leituras if (j.spec_json or {}).get("out_dir") == pasta), alvo)
        if cabeca.status in ("queued", "running"):
            return None, (
                f"a leitura {pasta} ainda está rodando (job {cabeca.id}) — espere terminar; o resultado "
                "chega sozinho. Aí estenda."
            )
        return cabeca, ""

    async def _deliverables(self, out_dir: str) -> list[dict[str, str]]:
        """Arquivos que o Neo JÁ entregou a partir desta leitura (nas continuações dela)."""
        try:
            jobs = await self.db.list_agent_jobs(self.user_id, limit=100)
        except Exception:  # noqa: BLE001
            return []
        ids = {str(j.id) for j in jobs or [] if j.kind == "deep_read" and (j.spec_json or {}).get("out_dir") == out_dir}
        entregas: list[dict[str, str]] = []
        vistos: set[str] = set()
        for j in jobs or []:  # mais recentes primeiro
            if j.kind != "scheduled_prompt" or str((j.spec_json or {}).get("continuation_of") or "") not in ids:
                continue
            for f in (j.extra_metadata or {}).get("files") or []:
                file_id = str((f or {}).get("file_id") or (f or {}).get("id") or "")
                caminho = ""
                if file_id and self.file_store is not None:
                    try:
                        entrada = await self.file_store.get(file_id)
                        caminho = str(((getattr(entrada, "meta_json", None) or {}).get("source") or {}).get("workspace_path") or "")
                    except Exception:  # noqa: BLE001
                        caminho = ""
                chave = caminho or file_id
                if chave and chave not in vistos:
                    vistos.add(chave)
                    entregas.append({"path": caminho, "name": str((f or {}).get("name") or ""), "file_id": file_id})
        return entregas[:5]

    async def _duplicate(self, arquivos: list[dict[str, str]], perguntas: list[str]) -> AgentJob | None:
        if self.user_id is None or self.session_id is None:
            return None
        try:
            vivos = await self.db.list_agent_jobs(self.user_id, statuses=["queued", "running"], limit=20)
        except Exception:  # noqa: BLE001 — sem banco, cria (o comportamento sem a guarda)
            return None
        chave = read_key(arquivos, perguntas)
        for job in vivos or []:
            if job.kind != "deep_read" or str(job.session_id) != str(self.session_id):
                continue
            spec = job.spec_json or {}
            if read_key(spec.get("files") or [], spec.get("questions") or []) == chave:
                return job
        return None
