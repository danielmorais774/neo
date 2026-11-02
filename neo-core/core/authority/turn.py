"""Autoridade do turno: quem pediu, o que entrou de fora, o que já foi visto.

Mesmo desenho do :mod:`core.liveness`: o objeto é instalado no início do turno
e as camadas de baixo escrevem nele sem recebê-lo por parâmetro. A diferença é
que este objeto é LIDO por quem decide (o portão), então ele nunca pode ser
escrito pelo modelo — só por código que sabe de onde a informação veio.

Três coisas moram aqui:

1. **Principal** — quem originou a intenção deste turno. ``admin`` é você;
   ``externo`` é um texto que chegou de fora (remetente de e-mail, contato do
   WhatsApp); ``agenda`` é um disparo sem ninguém na frente.
2. **Contaminação** — se conteúdo de terceiro entrou no contexto. Contaminado
   não quer dizer proibido: quer dizer que saída passa pelo portão.
3. **Procedência de URL** — as URLs que apareceram em RESULTADO de tool neste
   turno. É o que transforma a regra de prompt "só use URL que você viu num
   resultado" (``modules/desktop/ai_service.py``) numa verificação.

Fora de um turno (job em background, boot) não há objeto instalado e as funções
de módulo viram no-op — ninguém quebra por causa disso.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from core.authority.risk import classes_of, FONTE_EXTERNA

logger = logging.getLogger(__name__)

PRINCIPAL_ADMIN = "admin"
PRINCIPAL_EXTERNO = "externo"
PRINCIPAL_AGENDA = "agenda"

#: URL dentro de um texto qualquer. Para de propósito nos caracteres que
#: costumam FECHAR a URL em prosa/JSON/markdown — senão `(https://x)` entraria
#: com o parêntese e nunca casaria com a URL que o modelo escreve depois.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}\\]+", re.IGNORECASE)

#: Tetos: um resultado de tool pode ser enorme (deep_read, web_fetch) e isto
#: roda no caminho quente de todo turno.
_MAX_SCAN_CHARS = 300_000
_MAX_URLS = 2_000

#: Pontuação que gruda no fim de URL em texto corrido.
_TRAILING = ".,;:!?'\"”’)]}"

#: Até quantos segmentos uma URL vista serve de base para a forma curta. Ver
#: `url_is_short_form`: é o teto que mantém pequeno o número de trechos possíveis.
_MAX_SEGMENTOS_BASE = 12


def canonical_url(url: str) -> str:
    """Forma comparável de uma URL: esquema/host minúsculos, sem lixo no fim.

    Caminho e query ficam como estão — CDN de loja assina a query, e ignorá-la
    deixaria `?d=<segredo>` passar por ser "a mesma URL".
    """
    bruta = (url or "").strip().rstrip(_TRAILING)
    if not bruta:
        return ""
    try:
        esquema, resto = bruta.split("://", 1)
    except ValueError:
        return bruta
    host, _, cauda = resto.partition("/")
    canonica = f"{esquema.lower()}://{host.lower()}"
    return f"{canonica}/{cauda}" if cauda else canonica


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value[:_MAX_SCAN_CHARS]
    try:
        return str(value)[:_MAX_SCAN_CHARS]
    except Exception:  # noqa: BLE001 — resultado exótico não derruba o turno
        return ""


@dataclass
class TurnAuthority:
    """O que se sabe sobre a autoridade deste turno."""

    principal: str = PRINCIPAL_ADMIN
    #: Quem é o externo (endereço, jid, domínio) — vai no card de aprovação.
    principal_ref: str | None = None
    #: Dono do turno. É por ele que o portão acha as liberações aprovadas — e
    #: vem do turno, nunca de argumento escrito pelo modelo.
    user_id: Any = None
    tainted: bool = False
    #: De onde veio a contaminação, em ordem ("web_fetch:loja.com.br").
    taint_sources: list[str] = field(default_factory=list)
    #: URLs vistas em resultado de tool (já canônicas).
    seen_urls: set[str] = field(default_factory=set)
    #: ``tool_call.id`` já avaliados pelo portão. O trilho cognitivo delega ao
    #: global para parte das tools, então a MESMA chamada passaria duas vezes e
    #: o relatório da sombra contaria o dobro. Deduplicar por id (e não por
    #: nome) preserva a contagem de duas chamadas iguais no mesmo turno.
    gated_calls: set[str] = field(default_factory=set)

    # ── escrita (só código, nunca o modelo) ──────────────────────────────

    def taint(self, source: str) -> None:
        self.tainted = True
        if source and source not in self.taint_sources:
            self.taint_sources.append(source)

    def note_tool_result(self, tool_name: str, result: Any) -> None:
        """Registra o que um resultado de tool trouxe para o turno.

        Duas coisas de uma vez, porque as duas dependem do mesmo texto: a
        contaminação (se a tool é fonte externa) e as URLs de procedência (de
        QUALQUER tool — a foto do anúncio chega pelo ``meli_product``).
        """
        texto = _as_text(result)
        if FONTE_EXTERNA in classes_of(tool_name):
            self.taint(tool_name)
        if not texto:
            return
        for bruta in _URL_RE.findall(texto):
            if len(self.seen_urls) >= _MAX_URLS:
                return
            canonica = canonical_url(bruta)
            if canonica:
                self.seen_urls.add(canonica)

    def note_user_text(self, text: str) -> None:
        """URL que VOCÊ escreveu tem procedência — é um pedido seu."""
        for bruta in _URL_RE.findall(_as_text(text)):
            if len(self.seen_urls) >= _MAX_URLS:
                return
            canonica = canonical_url(bruta)
            if canonica:
                self.seen_urls.add(canonica)

    # ── leitura ──────────────────────────────────────────────────────────

    def url_has_provenance(self, url: str) -> bool:
        """Esta URL apareceu num resultado de tool (ou na sua mensagem)?"""
        canonica = canonical_url(url)
        return bool(canonica) and canonica in self.seen_urls

    def url_is_short_form(self, url: str) -> bool:
        """Esta URL é um TRECHO do caminho de uma URL vista, sem nada de novo?

        Loja publica `/Nome-do-Produto/dp/B083LGFDGJ/ref=x?tag=y` e o modelo, com
        razão, encurta para `/dp/B083LGFDGJ`: nada ali é novo, e a recusa só atrapalha.

        A regra é estreita de propósito, porque esta trava existe contra dado saindo
        DENTRO da URL (`docs/release-request-plan.md` §12.3):

        * mesmo esquema e mesmo host, sem query nem fragmento;
        * trecho CONTÍGUO de UMA URL vista. Subsequência abriria o canal que a trava
          fecha: uma página publicaria um link com o alfabeto repetido em segmentos, e
          qualquer texto seria subsequência dele. Misturar URLs daria o mesmo;
        * a base tem no máximo `_MAX_SEGMENTOS_BASE` segmentos, senão um caminho enorme
          montado para conter toda combinação curta traria o mesmo canal de volta. Com
          o teto são menos de 80 trechos por URL, menos escolhas do que as 2.000 URLs
          exatas que uma página já pode oferecer hoje;
        * pelo menos 2 segmentos: um só não identifica nada.

        Fica de fora de `url_has_provenance` para valer só no `web_fetch`.
        """
        canonica = canonical_url(url)
        if "://" not in canonica or "?" in canonica or "#" in canonica:
            return False
        esquema, resto = canonica.split("://", 1)
        host, _, cauda = resto.partition("/")
        pedido = [p for p in cauda.split("/") if p]
        tamanho = len(pedido)
        if tamanho < 2:
            return False
        prefixo = f"{esquema}://{host}/"
        for vista in self.seen_urls:
            if not vista.startswith(prefixo):
                continue
            caminho = re.split(r"[?#]", vista[len(prefixo):], maxsplit=1)[0]
            partes = [p for p in caminho.split("/") if p]
            if len(partes) > _MAX_SEGMENTOS_BASE:
                continue
            if any(partes[i:i + tamanho] == pedido for i in range(len(partes) - tamanho + 1)):
                return True
        return False

    @property
    def from_external(self) -> bool:
        return self.principal == PRINCIPAL_EXTERNO

    @property
    def unattended(self) -> bool:
        return self.principal == PRINCIPAL_AGENDA

    def summary(self) -> str:
        """Uma linha para log/card."""
        partes = [f"principal={self.principal}"]
        if self.principal_ref:
            partes.append(f"ref={self.principal_ref}")
        partes.append(f"contaminado={'sim' if self.tainted else 'não'}")
        if self.taint_sources:
            partes.append(f"fontes={','.join(self.taint_sources[:5])}")
        return " ".join(partes)


_current: ContextVar[TurnAuthority | None] = ContextVar("neo_turn_authority", default=None)


def principal_from_metadata(metadata: dict[str, Any] | None) -> tuple[str, str | None]:
    """Deriva o principal do metadata do turno. Nunca lê nada escrito pelo LLM.

    - ``principal``/``principal_ref`` explícitos: quem sabe a origem declara
      (é como o pipeline do WhatsApp e o leitor de e-mail vão marcar ``externo``).
    - ``source=schedule`` ou ``unattended``: disparo da agenda.
    - resto: você, falando por um cliente seu.
    """
    meta = metadata or {}
    declarado = str(meta.get("principal") or "").strip().lower()
    if declarado in (PRINCIPAL_ADMIN, PRINCIPAL_EXTERNO, PRINCIPAL_AGENDA):
        ref = meta.get("principal_ref")
        return declarado, (str(ref) if ref else None)
    if str(meta.get("source") or "").strip().lower() == "schedule" or meta.get("unattended"):
        return PRINCIPAL_AGENDA, None
    return PRINCIPAL_ADMIN, None


def bind_authority(authority: TurnAuthority) -> Token:
    """Instala a autoridade no contexto atual (e nas tasks criadas a partir dele)."""
    return _current.set(authority)


def current_authority() -> TurnAuthority | None:
    return _current.get()


def reset_authority(token: Token) -> None:
    """Desinstala a autoridade (par do :func:`bind_authority`)."""
    try:
        _current.reset(token)
    except ValueError:  # pragma: no cover - token de outro contexto
        _current.set(None)


@contextmanager
def turno_de_fundo(
    metadata: dict[str, Any] | None = None, user_id: Any = None
) -> Iterator[TurnAuthority | None]:
    """Autoridade para um disparo que NÃO passa pelo ``process_message``.

    A agenda com ação ``tool`` (``core/scheduler/runners.py``) e a condição de
    uma tarefa (``core/scheduler/tick.py``) chamam o executor direto, sem turno
    cognitivo. Sem isto rodavam como "fora de turno", que o portão trata como
    permitir: a regra de ``unattended`` (disparo sem ninguém na frente pede
    aprovação) nunca valia para o caminho mais desacompanhado que existe.

    Respeita autoridade já instalada — aninhar não reclassifica um turno seu.
    """
    if current_authority() is not None:
        yield current_authority()
        return
    principal, ref = principal_from_metadata(metadata)
    autoridade = TurnAuthority(principal=principal, principal_ref=ref, user_id=user_id)
    token = bind_authority(autoridade)
    try:
        yield autoridade
    finally:
        reset_authority(token)


def note_tool_result(tool_name: str, result: Any) -> None:
    """Registra resultado de tool no turno em curso, se houver um."""
    authority = _current.get()
    if authority is None:
        return
    try:
        authority.note_tool_result(tool_name, result)
    except Exception:  # noqa: BLE001 — observabilidade não derruba turno
        logger.exception("[authority] falha ao registrar resultado de %s", tool_name)
