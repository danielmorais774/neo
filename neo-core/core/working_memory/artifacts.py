"""Registro de artefatos — os arquivos que o Neo trouxe para a máquina do usuário.

É uma nota ROLANTE do caderno de trabalho (slug ``artefatos-recentes``), não uma
tabela nova. Três motivos, nesta ordem:

- a nota é **por usuário**, não por sessão: sobrevive à rotação de sessão, que é
  exatamente o caso que interessa ("cadê o NFS-e que você baixou ontem?");
- o índice do caderno já entra no prompt **todo turno, sem tool call** — então o
  caminho do arquivo chega ao modelo de graça e o ``wm_read`` vira exceção, não
  o caminho feliz. Instrução de prompt pedindo para ler falha em ~metade dos
  turnos (lição do ``force_tool_first``); um dado que já está no prompt, não;
- o corpo já é podado pelo COMEÇO em 32 KB (``WorkingMemoryStore._truncate``),
  que é a política certa para um registro rolante: o artefato mais velho é o
  primeiro a sair.

A linha do registro é legível por humano E por máquina ao mesmo tempo — o modelo
lê como texto, :func:`parse_artifacts` lê de volta como dado. Por isso o formato
é fixo e tem um prefixo próprio (``⬇``); mudar o separador quebra a leitura de
volta, e há teste para isso.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# Slug da nota rolante. Estável: é o alvo de [[artefatos-recentes]] no índice.
ARTIFACTS_SLUG = "artefatos-recentes"
ARTIFACTS_TITLE = "Arquivos que eu trouxe para a sua máquina"

# Marcador de linha do registro. Não usar "-": o índice do caderno já usa "- "
# para separar notas, e o `derive_summary` trata bullet como ruído.
_MARK = "⬇"

# ``⬇ nome · /caminho/absoluto · 72643 bytes · navegador local``
# O terceiro campo é opcional (download recém-começado ainda não tem tamanho).
_LINE_RE = re.compile(
    r"^\s*" + _MARK + r"\s*(?P<name>[^·]+?)\s*·\s*(?P<path>[^·]+?)\s*"
    r"(?:·\s*(?P<bytes>\d+)\s*bytes\s*)?"
    r"(?:·\s*(?P<source>[^·]+?)\s*)?$"
)

# Sufixos que o navegador usa enquanto o arquivo ainda está enchendo. O registro
# guarda sempre o nome FINAL: sem isso o download registrado no instante em que
# começa entraria como `nfse.pdf.crdownload` e a confirmação, minutos depois,
# como `nfse.pdf` — duas linhas para um arquivo só, e a primeira apontando para
# um caminho que já não existe.
_PARTIAL_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")

# Teto do que vale a pena carregar no prompt do classificador. O prompt do router
# já foi auditado para caber (docs/router-prompt-audit.md) — o registro entra
# como ajuda, não como novo custo fixo.
DEFAULT_CONTEXT_LIMIT = 5


def store_for(db: Any) -> Any | None:
    """Store do caderno, ou ``None`` quando a memória de trabalho está desligada.

    Existe para que os três chamadores (runner de job, epílogo do turno, montagem
    do contexto) não repitam nem a checagem da flag nem os cinco kwargs do
    construtor — e para que ligar/desligar a feature seja um lugar só.
    """
    if db is None:
        return None
    from shared import get_settings

    settings = get_settings()
    if not getattr(settings, "working_memory_enabled", False):
        return None
    from core.working_memory.store import WorkingMemoryStore

    return WorkingMemoryStore(
        db=db,
        max_note_bytes=settings.working_memory_note_max_bytes,
        ttl_days=settings.working_memory_ttl_days,
        max_index_notes=settings.working_memory_max_notes_in_index,
        index_auto_quota=settings.working_memory_index_auto_quota,
        read_tail_entries=settings.working_memory_read_tail_entries,
    )


def final_name(name: str) -> str:
    """Nome do arquivo sem o sufixo temporário do navegador."""
    clean = (name or "").strip()
    lower = clean.lower()
    for suffix in _PARTIAL_SUFFIXES:
        if lower.endswith(suffix):
            return clean[: -len(suffix)]
    return clean


def normalize(item: dict[str, Any]) -> dict[str, Any]:
    """Item pronto para o registro: nome/caminho finais e origem carimbada."""
    name = final_name(str(item.get("name") or ""))
    path = str(item.get("path") or "").strip()
    if path:
        head, _, tail = path.rpartition("/")
        cleaned_tail = final_name(tail)
        path = f"{head}/{cleaned_tail}" if head else cleaned_tail
    source = str(item.get("source") or "").strip()
    if item.get("incomplete"):
        source = (source + " (ainda baixando)").strip()
    size = item.get("bytes")
    return {
        "name": name,
        "path": path,
        # Tamanho parcial mente: só registra o número quando o arquivo fechou.
        "bytes": size if (isinstance(size, int) and not item.get("incomplete")) else None,
        "source": source or None,
    }


def render_line(item: dict[str, Any]) -> str:
    """Uma linha do registro. ``name`` e ``path`` são obrigatórios."""
    name = str(item.get("name") or "").strip()
    path = str(item.get("path") or "").strip()
    if not name or not path:
        return ""
    parts = [f"{_MARK} {name}", path]
    size = item.get("bytes")
    if isinstance(size, int) and size > 0:
        parts.append(f"{size} bytes")
    source = str(item.get("source") or "").strip()
    if source:
        parts.append(source)
    return " · ".join(parts)


def render_entry(items: list[dict[str, Any]], *, note: str | None = None) -> str:
    """Corpo da entrada gravada no caderno. Vazio quando não há item válido."""
    lines = [line for line in (render_line(item) for item in items) if line]
    if not lines:
        return ""
    head = note or (
        "Baixei/entreguei estes arquivos na máquina do usuário. O caminho abaixo é "
        "o real, conferido no disco dele — use ele direto, não monte um caminho a "
        "partir do nome."
    )
    return head + "\n" + "\n".join(lines)


def render_summary(items: list[dict[str, Any]]) -> str:
    """Linha do índice: é ISTO que o modelo vê todo turno sem abrir a nota.

    O caminho inteiro vai aqui de propósito — o campo ``summary`` tem 240 chars,
    e um caminho de download cabe. Com ele no índice, "onde está o arquivo?" não
    custa nem uma tool call.
    """
    parts: list[str] = []
    for item in items:
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        if not name or not path:
            continue
        parts.append(f"{name} em {path}")
    if not parts:
        return ""
    return "último(s) arquivo(s) na máquina dele: " + "; ".join(parts)


def parse_artifacts(body: str) -> list[dict[str, Any]]:
    """Lê o corpo da nota de volta como dado, do mais ANTIGO para o mais novo.

    Deduplica por nome mantendo a ÚLTIMA ocorrência: o registro grava uma linha
    quando o download começa (sem tamanho) e outra quando termina (com tamanho e
    caminho confirmado) — a segunda é a que vale.
    """
    found: dict[str, dict[str, Any]] = {}
    for raw in (body or "").splitlines():
        match = _LINE_RE.match(raw)
        if not match:
            continue
        name = match.group("name").strip()
        path = match.group("path").strip()
        if not name or not path:
            continue
        size = match.group("bytes")
        found[name.lower()] = {
            "name": name,
            "path": path,
            "bytes": int(size) if size else None,
            "source": (match.group("source") or "").strip() or None,
        }
    return list(found.values())


async def record(
    store: Any,
    *,
    user_id: UUID | None,
    session_id: UUID | None,
    items: list[dict[str, Any]],
    note: str | None = None,
) -> Any:
    """Grava artefatos na nota rolante. Best-effort: nunca levanta.

    Chamado de caminhos que NÃO podem falhar por causa do caderno (o runner de um
    job em segundo plano, o epílogo de um turno já respondido) — mesma política do
    ``append_auto``.
    """
    clean = [normalize(item) for item in items]
    clean = [item for item in clean if item["name"] and item["path"]]
    entry = render_entry(clean, note=note)
    if not entry or user_id is None:
        return None
    try:
        return await store.write(
            user_id=user_id,
            session_id=session_id,
            slug=ARTIFACTS_SLUG,
            title=ARTIFACTS_TITLE,
            content=entry,
            summary=render_summary(clean),
            origin="auto",
        )
    except Exception as exc:  # noqa: BLE001 — registro nunca derruba quem chamou
        logger.warning("[Artefatos] gravação falhou: %s", exc)
        return None


async def recent(
    store: Any, *, user_id: UUID | None, limit: int = DEFAULT_CONTEXT_LIMIT
) -> list[dict[str, Any]]:
    """Artefatos conhecidos, do mais RECENTE para o mais antigo. Nunca levanta."""
    if store is None or user_id is None:
        return []
    try:
        payload = await store.read(user_id=user_id, slug=ARTIFACTS_SLUG, full=True)
    except Exception as exc:  # noqa: BLE001 — leitura de contexto é best-effort
        logger.warning("[Artefatos] leitura falhou: %s", exc)
        return []
    if not isinstance(payload, dict) or payload.get("error"):
        return []
    items = parse_artifacts(str(payload.get("body") or ""))
    items.reverse()
    return items[: max(1, limit)]


def path_map(items: list[dict[str, Any]]) -> dict[str, str]:
    """``{nome-do-arquivo-em-minúsculo: caminho real}`` — o mais recente vence.

    É o que transforma "vai direto no arquivo" de instrução de prompt em
    resolução determinística: quem monta um comando com o nome do arquivo passa
    por aqui e recebe o caminho REAL, em vez de compor um a partir da pasta de
    downloads e torcer.
    """
    out: dict[str, str] = {}
    for item in items:
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        if name and path:
            out.setdefault(name.lower(), path)
    return out


def render_context_block(items: list[dict[str, Any]]) -> str:
    """Bloco para o ``system_context`` — o canal que o CLASSIFICADOR também lê.

    O índice do caderno só chega ao núcleo cognitivo (ele é montado pelo
    PromptContextSelector). Quem inventa caminho, porém, é o classificador, que
    recebe outro canal. Mesma costura do manifesto de anexos, que entrou no
    ``system_context`` exatamente para impedir esse chute.
    """
    lines = [line for line in (render_line(item) for item in items) if line]
    if not lines:
        return ""
    return (
        "## Arquivos que já estão na máquina do usuário\n"
        "Eu mesmo trouxe estes arquivos e CONFERI o caminho no disco dele. Precisou "
        "mexer em um deles? Use o caminho exato desta lista — não monte caminho a "
        "partir do nome nem saia procurando. Arquivo que NÃO está aqui é que exige "
        "procurar antes de agir.\n" + "\n".join(lines)
    )
