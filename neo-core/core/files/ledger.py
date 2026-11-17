"""Registro de vínculos de arquivo — onde está cada cópia de um documento.

docs/file-links-plan.md. Três lugares que não se enxergam: o disco do usuário
(host), o catálogo (File Store) e o sandbox (/workspace). O Neo move arquivo entre
eles o tempo todo — puxa um PDF, gera uma conclusão, entrega em duas pastas, edita
direto no Mac — e até aqui não guardava em lugar nenhum O QUE foi parar ONDE.

Sessão f33632b4, interações 5–10: "adiciona ao arquivo final e coloca em
~/Desktop/teste-de-analise" virou o PDF do Charlie gravado com nome de ``.md``
("Pronto, salvei"); cobrado, o Neo não sabia qual cópia era qual, tentou ``rm``
num caminho do sandbox na máquina do usuário, inventou o conteúdo de um markdown
e terminou num plano de 3 passos que falhou inteiro.

Tudo aqui é determinístico. As funções puras (:func:`resolve_doc_key`,
:func:`build_views`, :func:`render_block`) recebem a lista de eventos e são o que
os testes exercitam; :class:`FileLedger` só persiste e consulta.

**Dois anéis (session-20cb02b6, 09/11).** O registro lê os movimentos da conversa
E os do mesmo usuário nas últimas 72 h — a sessão pode rotacionar e o arquivo
continua o mesmo. Mas tudo caía num bloco só, chamado "ARQUIVOS DESTA CONVERSA",
com a regra "na dúvida, o gerado mais recente". Numa conversa NOVA sobre o template
de Processos de Software que ele anexou, o TXT de questões de semiótica de outra
conversa (08/11) apareceu ali como se fosse desta; o Neo batizou o anexo de
`questoes.docx` e dois turnos depois propôs encher o template com aquelas questões.
Agora:

* anel 1 — documento com movimento NESTA conversa (ou anexado nela): uso livre;
* anel 2 — documento só de outras conversas: entra só quando a mensagem cita o
  nome ou o assunto dele (:func:`ancorado`), com a origem dita e sem a regra do
  mais recente; quando entra pela citação, é ADOTADO (evento ``adopted``) e passa
  para o anel 1. Conversa sem arquivo nenhum ainda mostra os recentes — é o caso
  da rotação que motivou as 72 h — mas como "pergunte qual é".
* na hora do USO, ler um documento do anel 2 devolve um aviso de origem junto do
  resultado da tool (:meth:`FileLedger.nota_de_origem`).
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: Eventos que mudam o CONTEÚDO do documento (a versão atual passa a ser a deles).
#: ``attached``: o arquivo que ele anexou no chat (vai direto para o catálogo).
_CONTENT_ACTIONS = {"pulled", "attached", "saved", "edited_sandbox", "written_host", "edited_host"}
_HOST_ACTIONS = {"pulled", "delivered", "written_host", "edited_host", "read_host", "deleted_host"}
_MAX_DOCS_IN_BLOCK = 8
#: Documentos de OUTRAS conversas mostrados quando esta ainda não tem nenhum.
_MAX_OUTRAS_SEM_ANCORA = 3
#: Quanto tempo um documento do usuário continua visível em conversas novas.
USER_HORIZON_HOURS = 72

# ── Âncora: a mensagem cita este documento? ──────────────────────────────────
#: Palavras que aparecem em nome de arquivo e em qualquer pedido — não ancoram.
_PALAVRAS_SEM_ANCORA = frozenset({
    "arquivo", "arquivos", "documento", "documentos", "final", "versao", "versoes",
    "copia", "novo", "nova", "file", "files", "workspace", "users", "desktop",
    "downloads", "documents", "home", "daniel", "esse", "essa", "este", "esta",
    "desse", "dessa", "deste", "desta", "nesse", "nessa", "neste", "nesta", "aquele",
    "aquela", "isso", "isto", "para", "como", "outro", "outra", "coloca", "coloque",
    "manda", "mande", "gera", "gere", "retorna", "retorne", "salva", "salve", "faz",
    "fazer", "agora", "depois", "aqui", "tambem", "sobre", "preenchido", "preenchida",
    "editado", "editada", "corrigido", "corrigida", "teste", "texto",
    "docx", "xlsx", "pptx", "html", "json", "jpeg", "pdf", "txt", "csv", "png", "jpg",
})


def _sem_acento(texto: str) -> str:
    normal = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in normal if not unicodedata.combining(c)).lower()


def _palavras(texto: str) -> set[str]:
    """Palavras com 4+ letras que podem ancorar (sem id, número nem genérico)."""
    out: set[str] = set()
    for p in re.findall(r"[a-z0-9]{4,}", _sem_acento(texto)):
        if p in _PALAVRAS_SEM_ANCORA or p.isdigit():
            continue
        # Pedaço de id ("f7794968", "48fc71b7abad…"): hex com dígito não é nome.
        if re.fullmatch(r"[0-9a-f]+", p) and re.search(r"\d", p):
            continue
        out.add(p)
    return out


def _mesma_palavra(a: str, b: str) -> bool:
    # "questao" × "questoes": o plural/acento muda o fim, o começo fica.
    return a == b or (len(a) >= 6 and len(b) >= 6 and a[:5] == b[:5])


#: "faz isso", "sim, pode": a resposta curta que confirma a pergunta anterior.
_CONFIRMACAO_CURTA = re.compile(
    r"^\s*(sim|pode|faz|faca|isso|ok|okay|beleza|blz|manda|bora|confirmo|confirmado|"
    r"claro|exato|perfeito|vai|segue|quero|aham|uhum)\b",
    re.IGNORECASE,
)


def texto_de_ancora(user_message: str | None, history: list[dict[str, Any]] | None = None) -> str:
    """O texto contra o qual um documento de outra conversa é "citado".

    É a mensagem dele. Quando ela é só uma confirmação curta ("faz isso"), soma-se
    a ÚLTIMA fala do Neo: se o Neo perguntou "uso as questões do Portal de 08/11?"
    e ele disse "faz isso", ele citou o documento por tabela.
    """
    texto = str(user_message or "")
    if len(texto.strip()) <= 40 and _CONFIRMACAO_CURTA.match(_sem_acento(texto)):
        anterior = next(
            (str(m.get("content") or "") for m in reversed(history or []) if m.get("role") == "assistant"),
            "",
        )
        if anterior:
            texto = f"{texto}\n{anterior[:1500]}"
    return texto


def _ext(name: str | None) -> str:
    base = str(name or "").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _short(dt: datetime | None) -> str:
    if dt is None:
        return "?"
    try:
        from core.working_memory.render import stamp

        return stamp(dt)
    except Exception:  # noqa: BLE001
        return dt.strftime("%d/%m %H:%M")


def _size(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return f"{n / 1024:.0f} KB" if n >= 1024 else f"{n} bytes"


def _get(e: Any, attr: str) -> Any:
    return e.get(attr) if isinstance(e, dict) else getattr(e, attr, None)


def _sid(value: Any) -> str | None:
    return str(value) if value else None


def resolve_doc_key(
    events: Iterable[Any],
    *,
    action: str,
    host_path: str | None = None,
    sandbox_path: str | None = None,
    file_id: Any = None,
    versao_de: str | None = None,
) -> str:
    """A que documento pertence este movimento (o ``doc_key`` do evento).

    A ordem de busca depende de QUEM define a identidade do movimento:

    * publicar/editar no sandbox → o caminho do sandbox (a mesma conclusão
      publicada de novo é outra versão do mesmo documento);
    * publicar com ``versao_de`` (caminho do sandbox ou file_id do documento de
      origem) → o documento de origem: o anexo preenchido é a versão 2 DO ANEXO,
      não um documento novo (session-20cb02b6: sem isso, "esse arquivo" no turno
      seguinte abriu o original em branco e jogou fora o turno anterior);
    * materializar/entregar/anexar/adotar → o ``file_id`` (o que foi para o disco
      é o CONTEÚDO daquele id — se o id era do PDF, a cópia é do PDF);
    * mexer no disco dele → o caminho do host.
    """
    lista = list(events)
    fid = _sid(file_id)

    def ultimo(pred) -> str | None:
        for e in reversed(lista):
            if pred(e):
                return str(_get(e, "doc_key"))
        return None

    por_sandbox = (lambda: ultimo(lambda e: sandbox_path and _get(e, "sandbox_path") == sandbox_path))
    por_id = (lambda: ultimo(lambda e: fid and _sid(_get(e, "file_id")) == fid))
    por_host = (lambda: ultimo(lambda e: host_path and _get(e, "host_path") == host_path))
    origem = str(versao_de or "").strip()
    por_origem = (lambda: ultimo(lambda e: origem and (
        _get(e, "sandbox_path") == origem or _sid(_get(e, "file_id")) == origem
    )))

    if action in ("saved", "edited_sandbox"):
        # O caminho já conhecido vence: publicar de novo a mesma saída é a mesma
        # versão em andamento. A origem só decide quando a saída é nova.
        ordem = (por_sandbox, por_origem, por_id)
    elif action in ("attached", "adopted"):
        ordem = (por_id, por_sandbox, por_host)
    elif action in ("staged", "delivered"):
        ordem = (por_id, por_host, por_sandbox)
    elif action == "written_host":
        ordem = (por_host, por_sandbox, por_id)
    else:
        ordem = (por_host, por_id, por_sandbox)
    for busca in ordem:
        chave = busca()
        if chave:
            return chave
    return f"doc-{uuid.uuid4().hex[:12]}"


def versao_provavel(events: Iterable[Any], *, sandbox_path: str | None, session_id: Any) -> str | None:
    """Saída nova que é, pelo nome, versão de um documento desta conversa.

    Quando o modelo publica sem ``versao_de``: `questoes_preenchido.docx` saindo na
    mesma conversa em que `questoes.docx` foi materializado é a versão 2 dele. Só
    decide com UM candidato — mesma extensão, nome do original contido no da saída,
    movimento nesta conversa; dois candidatos é chute, e chute aqui junta
    documentos que não são o mesmo.
    """
    if not sandbox_path:
        return None
    lista = list(events)
    if any(_get(e, "sandbox_path") == sandbox_path for e in lista):
        return None  # saída já conhecida: resolve_doc_key acha pelo caminho
    base = sandbox_path.rsplit("/", 1)[-1]
    ext = _ext(base)
    radical_saida = _sem_acento(base.rsplit(".", 1)[0])
    sessao = _sid(session_id)
    candidatos: dict[str, str] = {}
    for e in lista:
        caminho = _get(e, "sandbox_path")
        if not caminho or (sessao and _sid(_get(e, "session_id")) not in (sessao, None)):
            continue
        nome = str(caminho).rsplit("/", 1)[-1]
        radical = _sem_acento(nome.rsplit(".", 1)[0])
        if _ext(nome) == ext and len(radical) >= 4 and radical != radical_saida and radical in radical_saida:
            candidatos[str(_get(e, "doc_key"))] = str(caminho)
    return next(iter(candidatos.values())) if len(candidatos) == 1 else None


@dataclass
class HostCopy:
    path: str
    action: str
    at: datetime | None
    file_id: str | None = None
    sha256: str | None = None
    status: str = "ok"
    state: str = "desconhecido"


@dataclass
class DocView:
    key: str
    name: str
    origin: str  # "host" (veio do disco dele) | "anexo" (anexou no chat) | "sandbox" (gerado pelo Neo)
    origin_path: str | None
    current_where: str  # "host" | "sandbox" | "catalogo"
    current_file_id: str | None
    current_sha: str | None
    current_at: datetime | None
    current_host_path: str | None = None
    current_size: int | None = None
    unpublished: bool = False
    sandbox_path: str | None = None
    sandbox_state: str | None = None
    host_copies: list[HostCopy] = field(default_factory=list)
    last_at: datetime | None = None
    #: O anexo como ele mandou (file_id e cópia no sandbox), quando já há versão nova.
    original_file_id: str | None = None
    original_sandbox_path: str | None = None
    #: Conversas em que o documento teve movimento, e se a do turno é uma delas.
    sessions: set[str] = field(default_factory=set)
    in_session: bool = True
    #: Teve movimento DE VERDADE nesta conversa (não só `adopted`, que nasce de uma
    #: palavra do nome batendo com a mensagem). É o que libera mandar o arquivo a
    #: um site (`core/browser_local/pedido.arquivos_do_catalogo`).
    worked_in_session: bool = True
    #: Nomes e caminhos que este documento já teve — é por eles que uma mensagem o cita.
    aliases: set[str] = field(default_factory=set)


def ancorado(view: DocView, texto: str | None) -> bool:
    """A mensagem cita este documento pelo nome ou pelo assunto do nome?

    "usa as questões do Portal" ancora «questoes-portal.txt»; "coloca meu R.A. no
    cabeçalho desse arquivo" não ancora nada. Id e palavra genérica não contam.
    """
    if not texto:
        return False
    do_texto = _palavras(texto)
    if not do_texto:
        return False
    do_doc: set[str] = set()
    for nome in {view.name, *view.aliases}:
        base = str(nome or "").rsplit("/", 1)[-1]
        do_doc |= _palavras(base.rsplit(".", 1)[0] if "." in base else base)
    return any(_mesma_palavra(a, b) for a in do_doc for b in do_texto)


def build_views(events: Iterable[Any], *, session_id: Any = None) -> list[DocView]:
    """Agrupa os eventos por documento e calcula versão atual e estado de cada cópia.

    ``session_id`` (a conversa do turno) marca em cada documento se ele é desta
    conversa (``in_session``). Sem ele, tudo conta como desta — o comportamento de
    antes dos anéis.
    """
    lista = sorted(list(events), key=lambda e: _get(e, "created_at") or datetime.min)
    sessao = _sid(session_id)
    # Um caminho do disco pertence ao documento do evento MAIS RECENTE nele: depois
    # de consertar a cópia errada, ela sai do PDF e vai para a conclusão.
    dono_do_caminho: dict[str, str] = {}
    for e in lista:
        if _get(e, "host_path"):
            dono_do_caminho[_get(e, "host_path")] = str(_get(e, "doc_key"))

    grupos: dict[str, list[Any]] = {}
    for e in lista:
        grupos.setdefault(str(_get(e, "doc_key")), []).append(e)

    views: list[DocView] = []
    for chave, evs in grupos.items():
        primeiro = evs[0]
        primeira_acao = _get(primeiro, "action")
        if primeira_acao in ("pulled", "read_host", "edited_host"):
            origem = "host"
        elif primeira_acao == "attached":
            origem = "anexo"
        else:
            origem = "sandbox"
        sha_por_id: dict[str, str] = {}
        for e in evs:
            if _get(e, "file_id") and _get(e, "sha256"):
                sha_por_id[_sid(_get(e, "file_id"))] = _get(e, "sha256")

        view = DocView(
            key=chave,
            name=str(_get(evs[-1], "name") or _get(primeiro, "name") or ""),
            origin=origem,
            origin_path=_get(primeiro, "host_path") if origem == "host" else _get(primeiro, "sandbox_path"),
            current_where="catalogo",
            current_file_id=None,
            current_sha=None,
            current_at=None,
            last_at=_get(evs[-1], "created_at"),
        )
        copias: dict[str, HostCopy] = {}
        sandbox_sha: str | None = None

        trabalhadas: set[str] = set()
        for e in evs:
            acao = _get(e, "action")
            fid = _sid(_get(e, "file_id"))
            sha = _get(e, "sha256") or (sha_por_id.get(fid) if fid else None)
            host = _get(e, "host_path")
            quando = _get(e, "created_at")
            if _sid(_get(e, "session_id")):
                view.sessions.add(str(_get(e, "session_id")))
                if acao != "adopted":
                    trabalhadas.add(str(_get(e, "session_id")))
            for apelido in (_get(e, "name"), _get(e, "sandbox_path"), host):
                if apelido:
                    view.aliases.add(str(apelido))
            if acao == "adopted":
                # Só diz "ele citou este documento nesta conversa": não muda conteúdo.
                continue
            if _get(e, "name") and acao not in ("delivered", "read_host"):
                view.name = _get(e, "name")

            if acao == "read_host" and host:
                anterior = copias.get(host)
                mudou_fora = bool(anterior and anterior.sha256 and sha and anterior.sha256 != sha)
                copias[host] = HostCopy(host, acao, quando, anterior.file_id if anterior else None, sha)
                if mudou_fora or (view.current_sha is None and sha and origem == "host"):
                    # Alguém mexeu no arquivo dele depois da nossa última cópia: a
                    # versão do disco passa a ser a atual.
                    _set_current(view, "host", None, sha, quando, host=host)
                continue

            if acao in _CONTENT_ACTIONS:
                if acao == "attached":
                    _set_current(view, "catalogo", fid, sha, quando, size=_get(e, "size_bytes"))
                    view.original_file_id = fid
                    continue
                if acao in ("pulled", "written_host", "edited_host"):
                    _set_current(view, "host" if acao != "pulled" else "catalogo", fid, sha, quando, host=host,
                                 size=_get(e, "size_bytes"))
                    if host:
                        copias[host] = HostCopy(host, acao, quando, fid, sha)
                    if acao == "pulled" and fid:
                        view.current_where = "catalogo"
                elif acao == "saved":
                    _set_current(view, "sandbox", fid, sha, quando, size=_get(e, "size_bytes"))
                    view.sandbox_path = _get(e, "sandbox_path") or view.sandbox_path
                    sandbox_sha = sha
                elif acao == "edited_sandbox":
                    view.sandbox_path = _get(e, "sandbox_path") or view.sandbox_path
                    _set_current(view, "sandbox", None, None, quando)
                    view.unpublished = True
                    sandbox_sha = None
                if acao in ("written_host", "edited_host") and host:
                    copias[host].state = "atual"
                continue

            if acao == "staged":
                view.sandbox_path = _get(e, "sandbox_path") or view.sandbox_path
                if origem == "anexo" and fid == view.original_file_id and not view.original_sandbox_path:
                    view.original_sandbox_path = _get(e, "sandbox_path")
                sandbox_sha = sha
            elif acao == "delivered" and host:
                copias[host] = HostCopy(host, acao, quando, fid, sha, status=str(_get(e, "status") or "ok"))
            elif acao == "deleted_host" and host:
                copias[host] = HostCopy(host, acao, quando, status="deleted")

        # Estado de cada cópia contra a versão atual.
        for caminho, copia in copias.items():
            if dono_do_caminho.get(caminho) != chave:
                continue
            if copia.status == "wrong_content":
                copia.state = "errada"
            elif copia.status == "deleted":
                copia.state = "apagada"
            elif view.current_where == "host" and view.current_host_path == caminho:
                copia.state = "atual"
            elif copia.sha256 and view.current_sha and copia.sha256 == view.current_sha:
                copia.state = "igual"
            elif copia.sha256 and view.current_sha:
                copia.state = "desatualizada"
            elif view.unpublished:
                copia.state = "desatualizada"
            view.host_copies.append(copia)

        if view.sandbox_path:
            if view.unpublished:
                view.sandbox_state = "nao_publicada"
            elif view.current_where == "host" and (not sandbox_sha or sandbox_sha != view.current_sha):
                view.sandbox_state = "desatualizada"
            else:
                view.sandbox_state = "atual"
        # Evento sem conversa (registro antigo, teste) não tira o documento dela.
        view.in_session = sessao is None or not view.sessions or sessao in view.sessions
        view.worked_in_session = sessao is None or not view.sessions or sessao in trabalhadas
        views.append(view)

    views.sort(key=lambda v: v.last_at or datetime.min, reverse=True)
    return views


def _set_current(view: DocView, where: str, fid: str | None, sha: str | None, at: Any,
                 *, host: str | None = None, size: Any = None) -> None:
    view.current_where = where
    view.current_file_id = fid
    view.current_sha = sha
    view.current_at = at
    view.current_host_path = host if where == "host" else None
    view.unpublished = False
    try:
        view.current_size = int(size) if size is not None else None
    except (TypeError, ValueError):
        view.current_size = None


_RULES = (
    "Como usar este registro (vale para decidir e para agir):\n"
    "- \"o arquivo\", \"ele\", \"esse arquivo\", \"o arquivo final\", \"aquele .md\" = um "
    "documento DESTA conversa; um anexo desta mensagem vence qualquer outro e, na dúvida, "
    "vale o mais recente desta conversa. Use os caminhos e file_ids DAQUI — nunca invente "
    "nem use o id de outro documento.\n"
    "- Mudar um documento (anexo dele ou gerado por você): parta da VERSÃO ATUAL — nunca da "
    "anterior nem do original, a não ser para gerar tudo de novo por script — e publique com "
    "`file_save(path=..., versao_de=<caminho ou file_id da versão que você abriu>)`, para a "
    "nova virar a versão atual do MESMO documento.\n"
    "- Pôr no disco dele (\"baixa pra mim\", \"coloca em <pasta>\"): `save_generated_file` com o "
    "file_id da VERSÃO ATUAL e file_name = o nome do documento. Se o destino já tem uma cópia "
    "listada aqui, `overwrite=true` é seguro (a cópia é sua).\n"
    "- Versão atual sem file_id (alterada só no sandbox): `file_save` no caminho do sandbox "
    "antes de entregar. Para mostrar o botão de download de uma versão que já tem file_id: "
    "`file_offer`.\n"
    "- Editar \"no meu arquivo\", \"direto na minha máquina\", ou quando a versão atual está no "
    "disco dele: `read_user_file` + `edit_user_file` no caminho do disco listado — sem sandbox.\n"
    "- Cópia do sandbox desatualizada (a mais nova está no disco dele): puxe de volta com "
    "`request_user_file(mode=\"path\", stage=true)` antes de mexer nela.\n"
    "- Apagar uma cópia do disco dele: `run_command` com `rm` no caminho EXATO daqui. Nada "
    "de caminho /workspace na máquina dele."
)

_ESTADO_COPIA = {
    "igual": "igual à versão atual",
    "atual": "É A VERSÃO ATUAL (mais nova que o sandbox)",
    "desatualizada": "desatualizada (versão anterior)",
    "apagada": "apagada",
    "desconhecido": "estado desconhecido",
}


_RULES_OUTRAS = (
    "Como usar ESTES (são de OUTRA conversa, não desta):\n"
    "- Só trate um deles como \"o arquivo\" se ele citou pelo nome ou pelo assunto nesta "
    "conversa. Nunca troque o documento desta conversa por um destes.\n"
    "- Para pôr o CONTEÚDO de um deles no que vai entregar aqui, antes diga a ele de qual "
    "conversa veio (nome e data) e confirme."
)
_RULES_OUTRAS_SEM_ANCORA = (
    "Esta conversa ainda não tem arquivo nenhum. Se ele falou de \"o arquivo\" sem dizer "
    "qual, confirme com ele qual destes é antes de agir — são de outras conversas."
)


def _origem_legivel(v: DocView) -> str:
    if v.origin == "host":
        return f"veio do disco dele ({v.origin_path})" if v.origin_path else "veio do disco dele"
    if v.origin == "anexo":
        return "veio DELE (anexou no chat)"
    return "gerado por você no sandbox"


def _linhas_do_documento(i: int, v: DocView, *, outra: bool = False) -> list[str]:
    linhas: list[str] = []
    origem = _origem_legivel(v)
    if outra:
        origem += f" · de OUTRA conversa, último movimento {_short(v.last_at)}"
    linhas.append(f"{i}. «{v.name}» — {origem}")
    linhas.extend(_linhas_de_copias(v))
    return linhas


def render_block(views: list[DocView], *, anchor_text: str | None = None) -> str:
    """Bloco para o ``system_context`` — o classificador e o cérebro leem o mesmo.

    Documentos desta conversa (``in_session``) e de outras saem em blocos
    separados; os de outras só aparecem citados por ``anchor_text`` (a mensagem do
    turno), ou quando esta conversa ainda não tem documento nenhum.
    """
    if not views:
        return ""
    desta = [v for v in views if v.in_session]
    outras = [v for v in views if not v.in_session]
    citadas = [v for v in outras if ancorado(v, anchor_text)]
    sem_ancora = False
    if not desta and not citadas and outras:
        citadas, sem_ancora = outras[:_MAX_OUTRAS_SEM_ANCORA], True

    linhas: list[str] = []
    if desta:
        linhas.append(
            "## ARQUIVOS DESTA CONVERSA — onde está cada cópia (registro automático do que "
            "realmente aconteceu; vale mais que a sua memória da conversa)"
        )
        for i, v in enumerate(desta[:_MAX_DOCS_IN_BLOCK], start=1):
            linhas.extend(_linhas_do_documento(i, v))
            if v.origin == "anexo" and v.original_file_id and v.original_file_id != v.current_file_id:
                onde = f", cópia no sandbox {v.original_sandbox_path}" if v.original_sandbox_path else ""
                linhas.append(f"   original (como ele mandou): catálogo file_id={v.original_file_id}{onde}")
        if len(desta) > _MAX_DOCS_IN_BLOCK:
            linhas.append(f"(+{len(desta) - _MAX_DOCS_IN_BLOCK} documento(s) mais antigos omitidos)")
    if citadas:
        if linhas:
            linhas.append("")
        linhas.append(
            "## ARQUIVOS DE OUTRAS CONVERSAS (recentes) — NÃO são desta conversa"
            + (" · esta ainda não tem nenhum" if sem_ancora else " · ele citou o nome ou o assunto")
        )
        for i, v in enumerate(citadas[:_MAX_DOCS_IN_BLOCK], start=1):
            linhas.extend(_linhas_do_documento(i, v, outra=True))
        linhas.append(_RULES_OUTRAS_SEM_ANCORA if sem_ancora else _RULES_OUTRAS)
    if not linhas:
        return ""
    linhas.append(_RULES)
    return "\n".join(linhas)


def _linhas_de_copias(v: DocView) -> list[str]:
    """Versão atual, cópia no sandbox e cópias no disco dele (o miolo de cada item)."""
    linhas: list[str] = []
    if v.current_where == "host":
        atual = f"versão atual: no disco dele em {v.current_host_path} ({_short(v.current_at)})"
        if v.current_sha:
            atual += f", sha256={v.current_sha[:12]}…"
    elif v.unpublished:
        atual = (
            f"versão atual: só no sandbox em {v.sandbox_path}, NÃO publicada "
            f"({_short(v.current_at)}) — file_save antes de entregar"
        )
    else:
        onde = f"sandbox {v.sandbox_path} → " if v.current_where == "sandbox" and v.sandbox_path else ""
        tamanho = f", {_size(v.current_size)}" if v.current_size else ""
        atual = f"versão atual: {onde}catálogo file_id={v.current_file_id}{tamanho} ({_short(v.current_at)})"
    linhas.append(f"   {atual}")
    if v.sandbox_path and not (v.current_where == "sandbox" and not v.unpublished):
        estado = {
            "atual": "atual",
            "desatualizada": "⚠ DESATUALIZADA — puxe de volta antes de editar lá",
            "nao_publicada": "com alterações não publicadas",
        }.get(v.sandbox_state or "", v.sandbox_state or "")
        if not v.unpublished:
            linhas.append(f"   no sandbox: {v.sandbox_path} ({estado})")
    if v.host_copies:
        linhas.append("   no disco dele:")
        for c in v.host_copies:
            if c.state == "errada":
                linhas.append(
                    f"   - ⚠ {c.path} — CONTEÚDO ERRADO: é «{v.name}» gravado com esse nome "
                    f"({_short(c.at)}). Conserte entregando por cima (overwrite=true) o documento "
                    "que deveria estar ali."
                )
                continue
            como = {
                "pulled": "de onde veio", "delivered": "entregue por você", "written_host": "gravado por você",
                "edited_host": "editado direto no disco", "read_host": "lido", "deleted_host": "apagado",
            }.get(c.action, c.action)
            linhas.append(f"   - {c.path} — {_ESTADO_COPIA.get(c.state, c.state)} ({como} {_short(c.at)})")
    return linhas


class FileLedger:
    """Persistência do registro. Nunca levanta: registro fora do ar não derruba turno."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def events(
        self, session_id: Any, *, user_id: Any = None, horizon_hours: int = USER_HORIZON_HOURS, limit: int = 400
    ) -> list[Any]:
        """Movimentos desta conversa + os do mesmo usuário nas últimas ``horizon_hours``.

        Por usuário e não só por sessão: a sessão ROTACIONA (mudança de tópico,
        expiração) e o arquivo continua sendo o mesmo — na f33632b4 "tenta salvar o
        arquivo lá agora" caiu noutra sessão e perdeu tudo o que sabia dele.
        """
        if not session_id and not user_id:
            return []
        try:
            from datetime import timedelta

            from sqlalchemy import and_, or_
            from sqlmodel import select

            from db.models.file_link import FileLink

            condicoes = []
            if session_id:
                condicoes.append(FileLink.session_id == uuid.UUID(str(session_id)))
            if user_id:
                condicoes.append(and_(
                    FileLink.user_id == uuid.UUID(str(user_id)),
                    FileLink.created_at >= datetime.utcnow() - timedelta(hours=horizon_hours),
                ))
            stmt = (
                select(FileLink)
                .where(or_(*condicoes))
                .order_by(FileLink.created_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
            async with self._db.get_session() as s:
                return list(reversed((await s.execute(stmt)).scalars().all()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FileLedger] leitura falhou: %s", exc)
            return []

    async def record(
        self,
        *,
        user_id: Any,
        session_id: Any,
        action: str,
        name: str = "",
        host_path: str | None = None,
        sandbox_path: str | None = None,
        file_id: Any = None,
        sha256: str | None = None,
        size_bytes: Any = None,
        status: str = "ok",
        note: str = "",
        versao_de: str | None = None,
    ) -> Any:
        if not session_id:
            return None
        try:
            from db.models.file_link import FileLink

            eventos = await self.events(session_id, user_id=user_id)
            if action == "saved" and not versao_de:
                versao_de = versao_provavel(eventos, sandbox_path=sandbox_path, session_id=session_id)
            if versao_de and not note:
                note = f"versão de {versao_de}"
            chave = resolve_doc_key(
                eventos, action=action, host_path=host_path, sandbox_path=sandbox_path, file_id=file_id,
                versao_de=versao_de,
            )
            if action == "delivered" and host_path and name and _ext(name) != _ext(host_path):
                status = "wrong_content"
            link = FileLink(
                user_id=uuid.UUID(str(user_id)) if user_id else None,
                session_id=uuid.UUID(str(session_id)),
                doc_key=chave,
                action=action,
                name=str(name or (host_path or sandbox_path or "").rsplit("/", 1)[-1])[:255],
                host_path=host_path,
                sandbox_path=sandbox_path,
                file_id=uuid.UUID(str(file_id)) if file_id else None,
                sha256=sha256,
                size_bytes=int(size_bytes) if isinstance(size_bytes, (int, float)) else None,
                status=status,
                note=note[:2000],
            )
            async with self._db.get_session() as s:
                s.add(link)
                await s.commit()
            return link
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FileLedger] registro de %s falhou: %s", action, exc)
            return None

    async def views(self, session_id: Any, user_id: Any = None) -> list[DocView]:
        return build_views(await self.events(session_id, user_id=user_id), session_id=session_id)

    async def block(self, session_id: Any, user_id: Any = None, anchor_text: str | None = None) -> str:
        """O bloco do turno. Documento de outra conversa citado por ``anchor_text``
        é ADOTADO por esta (evento ``adopted``): ele mesmo trouxe o assunto, então o
        documento passa a ser desta conversa nos turnos seguintes. Neste turno ele
        ainda aparece no bloco de outras conversas — é aqui que o modelo precisa
        saber de onde o arquivo veio."""
        views = await self.views(session_id, user_id)
        if anchor_text and session_id:
            for v in views:
                if not v.in_session and ancorado(v, anchor_text):
                    await self.record(
                        user_id=user_id, session_id=session_id, action="adopted", name=v.name,
                        file_id=v.current_file_id, sandbox_path=v.sandbox_path,
                        note="citado pelo nome/assunto na mensagem",
                    )
        return render_block(views, anchor_text=anchor_text)

    async def nota_de_origem(
        self,
        session_id: Any,
        user_id: Any = None,
        *,
        file_ids: Iterable[Any] = (),
        textos: Iterable[str] = (),
    ) -> str | None:
        """Aviso para o resultado de uma tool que LEU um documento de outra conversa.

        Na session-20cb02b6 o Neo abriu, com ``cd /workspace/files && head …``, o TXT
        de questões de outra conversa e propôs pôr aquilo no template desta. Ler não
        é proibido — a memória continua valendo —, mas usar no entregável sem ele
        saber é. Casa por file_id e pelo NOME do arquivo dentro do comando (o
        caminho costuma vir relativo).
        """
        if not session_id:
            return None
        ids = {str(f) for f in file_ids if f}
        corpo = " ".join(str(t) for t in textos if t)
        achados: list[DocView] = []
        for v in await self.views(session_id, user_id):
            if v.in_session:
                continue
            nomes = {
                str(a).rsplit("/", 1)[-1] for a in (v.aliases | {v.sandbox_path or ""}) if a
            }
            por_id = bool(ids & {x for x in (v.current_file_id, v.original_file_id) if x})
            por_nome = any(len(n) >= 6 and n in corpo for n in nomes)
            if por_id or por_nome:
                achados.append(v)
        if not achados:
            return None
        itens = "; ".join(f"«{v.name}» ({_short(v.last_at)})" for v in achados[:3])
        return (
            f"⚠ ORIGEM: {itens} é de OUTRA conversa, não desta — ele não citou esse arquivo "
            "aqui. Pode consultar, mas antes de pôr o conteúdo dele no que vai entregar, diga "
            "a ele de qual conversa veio e confirme."
        )

    async def knows_host_path(self, session_id: Any, path: str, user_id: Any = None) -> bool:
        """O Neo já pôs, puxou, leu ou editou esta cópia (nesta conversa ou nas recentes)?"""
        if not path:
            return False
        return any(_get(e, "host_path") == path for e in await self.events(session_id, user_id=user_id))
