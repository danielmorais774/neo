"""Superfícies de navegador: quem pode dirigir um navegador, e o que cada uma sabe.

docs/browser-surface-plan.md §2, §5, §13.

Uma **superfície** é um navegador concreto que o Neo consegue dirigir. Existem dois
provedores, e a diferença entre eles é o motivo deste módulo existir:

``desktop``
    O ``neo-desktop-tauri``, que fala com o navegador visível pelo host
    ``open-browser-use`` **e** sabe iniciar um headless. É o provedor **completo**:
    a lógica do braço (os payloads de JS, a marcação numerada do print, o
    casamento do download com o DISCO) mora nele.
``extension``
    A extensão pareada como device próprio, falando com o core direto pelo canal de
    controle. É o provedor de **maior alcance** — funciona com o desktop fechado,
    que é o caso "peço pelo celular e uso o navegador do macbook" — e de **menor
    capacidade**: extensão não lê disco e não inicia navegador.

**A superfície direta não substitui o desktop.** Essa era a suposição errada da
primeira versão do plano (§12): quando as duas estão disponíveis, a resolução
prefere a completa. A direta faz existir um caminho que hoje simplesmente não
existe — hoje, pedido sem desktop no canal **degrada em silêncio** para o navegador
do servidor, que loja brasileira bloqueia.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: Capability de "sei dirigir o navegador visível do usuário". Mesmo nome que o
#: desktop já declara em `services/device_control.rs` — alinhar em vez de inventar
#: um segundo vocabulário para a mesma pergunta.
BROWSER_CAPABILITY = "browser_local"
#: "sei iniciar um navegador headless nesta máquina". Só o desktop.
HEADLESS_CAPABILITY = "browser_headless"

PROVIDER_DESKTOP = "desktop"
PROVIDER_EXTENSION = "extension"

#: Primitivas que o core assume quando o provedor é o desktop. Ele não precisa
#: declarar nada: as implementações nativas dele são o degrau 1 da escada (§13.3),
#: e são as que existem hoje em produção.
_PRIMITIVAS_DO_DESKTOP: dict[str, bool] = {
    "native_actions": True,   # o desktop implementa `browser.*` de alto nível
    "eval": True,
    "cdp": True,
    "screenshot": True,
    "marks": True,            # draw_marks em Rust
    "downloads_api": True,
    "downloads_disk": True,   # lê a pasta de Downloads da máquina
    "tabs": True,
    "banner": True,
}

#: O mínimo que uma superfície direta precisa declarar para servir de alguma coisa.
#: Sem conseguir avaliar JS nem falar CDP, ela não executa ação nenhuma — e recusar
#: no pareamento é melhor que descobrir no meio de uma tarefa.
PRIMITIVAS_MINIMAS = ("eval", "cdp")


@dataclass
class _Declarado:
    """O que uma superfície disse de si ao conectar, e quando."""

    provider: str
    engine: str
    primitives: dict[str, bool]
    em: float = field(default_factory=time.time)
    #: Último snapshot de abas que ela empurrou (§7). `None` = nunca empurrou.
    tabs: list[dict] | None = None
    tabs_em: float = 0.0


class SurfaceRegistry:
    """O que cada superfície declarou nesta conexão. Memória, não banco.

    Em memória de propósito: é estado de CONEXÃO, e uma declaração de uma conexão
    que já caiu é pior que nenhuma — mandaria o core prometer uma capacidade que
    ninguém está segurando. O que precisa sobreviver a restart (a capability que
    entra na resolução) é gravado em ``connected_device.capabilities`` pelo
    ``hello``; isto aqui é o detalhe fino da sessão.
    """

    def __init__(self) -> None:
        self._por_device: dict[str, _Declarado] = {}

    def declare(
        self, device_id: str, *, provider: str, engine: str, primitives: dict[str, Any]
    ) -> _Declarado:
        limpo = {str(k): bool(v) for k, v in (primitives or {}).items()}
        registro = _Declarado(
            provider=(provider or PROVIDER_EXTENSION), engine=(engine or ""), primitives=limpo
        )
        self._por_device[device_id] = registro
        logger.info(
            "[surface] %s declarou provider=%s engine=%s primitivas=%s",
            device_id, registro.provider, registro.engine,
            sorted(k for k, v in limpo.items() if v),
        )
        return registro

    def forget(self, device_id: str) -> None:
        self._por_device.pop(device_id, None)

    def primitives(self, device_id: str) -> dict[str, bool]:
        registro = self._por_device.get(device_id)
        return dict(registro.primitives) if registro else {}

    def provider(self, device_id: str) -> str | None:
        registro = self._por_device.get(device_id)
        return registro.provider if registro else None

    def set_tabs(self, device_id: str, tabs: list[dict]) -> None:
        registro = self._por_device.get(device_id)
        if registro is None:
            # Declarou abas sem ter dito `hello`: aceita, tratando como a mais
            # pobre (§13.2) — perder o snapshot seria pior que assumir pouco.
            registro = self.declare(
                device_id, provider=PROVIDER_EXTENSION, engine="", primitives={}
            )
        registro.tabs = list(tabs or [])
        registro.tabs_em = time.time()

    def tabs(self, device_id: str, *, max_idade_s: float = 120.0) -> list[dict]:
        """Último snapshot, se ainda é recente.

        Snapshot velho é pior que nenhum: o core decidiria reuso de aba sobre uma
        foto de cinco minutos atrás e mandaria tocar numa aba que o usuário já
        fechou.
        """
        registro = self._por_device.get(device_id)
        if registro is None or registro.tabs is None:
            return []
        if time.time() - registro.tabs_em > max_idade_s:
            return []
        return list(registro.tabs)

    def _reset_for_tests(self) -> None:
        self._por_device.clear()


_registry: SurfaceRegistry | None = None


def get_surface_registry() -> SurfaceRegistry:
    global _registry
    if _registry is None:
        _registry = SurfaceRegistry()
    return _registry


@dataclass(frozen=True)
class SurfaceMatch:
    """Uma superfície resolvida, pronta para receber ações."""

    device_id: str
    label: str
    provider: str
    host_device_id: str | None
    is_online: bool
    primitives: dict[str, bool]

    @property
    def completa(self) -> bool:
        """Provedor desktop = tem tudo (disco, headless, marcação)."""
        return self.provider == PROVIDER_DESKTOP

    def pode(self, primitiva: str) -> bool:
        return bool(self.primitives.get(primitiva, False))


def _label(registro: Any) -> str:
    nome = getattr(registro, "display_name", None)
    if nome:
        return str(nome)
    modelo = getattr(registro, "model", "") or "navegador"
    plataforma = getattr(registro, "os_platform", "") or ""
    return f"{modelo} ({plataforma})" if plataforma else str(modelo)


async def list_surfaces(db: Any, user_id: UUID) -> list[SurfaceMatch]:
    """Todas as superfícies do usuário — desktop e extensão, na mesma lista.

    O ``is_online`` sai do canal de CONTROLE, não do WS de presença: é o único que
    consegue executar alguma coisa.
    """
    from core.notifications.device_control import get_device_control_broadcaster

    broadcaster = get_device_control_broadcaster()
    registry = get_surface_registry()
    registros = await db.get_connected_devices(user_id) if db else []

    saida: list[SurfaceMatch] = []
    for r in registros:
        caps = getattr(r, "capabilities", None) or {}
        if not caps.get(BROWSER_CAPABILITY) and not caps.get(HEADLESS_CAPABILITY):
            continue

        declarado = registry.provider(r.device_id)
        tipo = getattr(r, "device_type", "") or ""
        from modules.devices.control import BROWSER_SURFACE_TYPE

        provider = declarado or (
            PROVIDER_EXTENSION if tipo == BROWSER_SURFACE_TYPE else PROVIDER_DESKTOP
        )
        primitivas = (
            dict(_PRIMITIVAS_DO_DESKTOP)
            if provider == PROVIDER_DESKTOP
            else registry.primitives(r.device_id) or dict(caps.get("primitives") or {})
        )
        # O headless é da MÁQUINA, não da superfície: só entra se o registro
        # declarou (o desktop declara; extensão nunca).
        primitivas["headless"] = bool(caps.get(HEADLESS_CAPABILITY))

        saida.append(
            SurfaceMatch(
                device_id=r.device_id,
                label=_label(r),
                provider=provider,
                host_device_id=getattr(r, "host_device_id", None),
                is_online=broadcaster.is_connected(r.device_id),
                primitives=primitivas,
            )
        )
    return saida


def _normalize(text: str) -> str:
    from modules.devices.control import _normalize as base

    return base(text)


def _casa_nome(candidatos: list[SurfaceMatch], nome: str) -> list[SurfaceMatch]:
    """Mesma cascata do ``_match_by_name`` dos devices: exato → prefixo → trecho."""
    agulha = _normalize(nome)
    if not agulha:
        return []
    for regra in (
        lambda s: _normalize(s.label) == agulha,
        lambda s: _normalize(s.label).startswith(agulha),
        lambda s: agulha in _normalize(s.label),
    ):
        casam = [s for s in candidatos if regra(s)]
        if casam:
            return casam
    return []


async def resolve_browser_surface(
    db: Any,
    user_id: UUID,
    hint: str | None = None,
    *,
    turn_device_id: str | None = None,
) -> SurfaceMatch:
    """Em qual navegador agir. Levanta ``DeviceResolutionError`` com texto pronto.

    Precedência (docs/browser-surface-plan.md §5), e a ordem importa:

    1. ``hint`` casa o nome de uma **superfície** ("Edge do macbook");
    2. ``hint`` casa o nome de um **hospedeiro** ("macbook") → as superfícies
       daquela máquina  ← *"pelo celular, usando o navegador do macbook"*;
    3. sem ``hint``, o turno veio de uma máquina → a superfície DELA
       ← *"peço dentro do desktop"*;
    4. sem ``hint``, existe exatamente uma superfície online;
    5. empate → **pergunta**, listando os nomes. Nunca chuta: chutar aqui abre a
       aba errada na tela dele.

    Em qualquer empate entre superfícies da MESMA máquina, a completa (desktop)
    ganha da direta — ela faz tudo que a outra faz, e mais (§12.3).
    """
    from modules.devices.control import DeviceResolutionError

    todas = await list_surfaces(db, user_id)
    if not todas:
        raise DeviceResolutionError(
            "Nenhum navegador registrado nesta conta. Abra o Neo Desktop, ou pareie "
            "a extensão do Neo no navegador."
        )

    online = [s for s in todas if s.is_online]
    dica = (hint or "").strip()

    if dica:
        # UNIÃO, não saída antecipada: "macbook" é o nome da máquina, e o usuário
        # quer "o navegador do macbook" — que pode ser o desktop dali OU a extensão
        # hospedada nele. Parar no primeiro casamento fazia "macbook" achar só o
        # desktop e falhar com "canal fechado" enquanto o navegador dele estava
        # aberto do lado, o que é o oposto do que o plano existe para resolver.
        candidatos = list(_casa_nome(todas, dica))
        vistos = {s.device_id for s in candidatos}
        for extra in await _por_hospedeiro(db, user_id, dica, todas):
            if extra.device_id not in vistos:
                candidatos.append(extra)
                vistos.add(extra.device_id)
        if not candidatos:
            raise DeviceResolutionError(
                f"Não achei nenhum navegador chamado '{dica}'.",
                candidates=[s.label for s in todas],
            )
        escolha = _preferir(candidatos)
        if escolha is None:
            raise DeviceResolutionError(
                f"'{dica}' casa com mais de um navegador — diga qual.",
                candidates=[s.label for s in candidatos],
            )
        return _exigir_online(escolha, online)

    if turn_device_id:
        # A máquina da conversa: ela mesma (provedor desktop) ou a superfície
        # hospedada nela. É o caso que não precisa de nome nenhum.
        daqui = [
            s for s in todas
            if s.device_id == turn_device_id or s.host_device_id == turn_device_id
        ]
        escolha = _preferir(daqui)
        if escolha is not None:
            return _exigir_online(escolha, online)

    if len(online) == 1:
        return online[0]
    if not online:
        raise DeviceResolutionError(
            "Nenhum navegador está com o canal aberto agora. Abra o navegador com a "
            "extensão do Neo, ou o Neo Desktop na máquina.",
            candidates=[s.label for s in todas],
        )
    raise DeviceResolutionError(
        "Tem mais de um navegador disponível — diga qual.",
        candidates=[s.label for s in online],
    )


async def _por_hospedeiro(
    db: Any, user_id: UUID, nome: str, todas: list[SurfaceMatch]
) -> list[SurfaceMatch]:
    """As superfícies da máquina que o usuário chamou por este nome."""
    registros = await db.get_connected_devices(user_id) if db else []
    agulha = _normalize(nome)
    hosts = {
        r.device_id
        for r in registros
        if agulha and (
            agulha == _normalize(getattr(r, "display_name", "") or "")
            or agulha in _normalize(getattr(r, "display_name", "") or "")
            or agulha in _normalize(getattr(r, "model", "") or "")
        )
    }
    if not hosts:
        return []
    return [s for s in todas if s.device_id in hosts or (s.host_device_id in hosts)]


def _preferir(candidatos: list[SurfaceMatch]) -> SurfaceMatch | None:
    """Uma escolha, ou ``None`` quando é empate de verdade.

    Online ganha de offline; completa ganha de direta. Duas completas online, ou
    duas diretas online, é empate — e empate pergunta.
    """
    if not candidatos:
        return None
    if len(candidatos) == 1:
        return candidatos[0]

    # Online ganha de offline ANTES de completa ganhar de direta: uma superfície
    # completa que está fechada não executa nada, e preferi-la só produziria a
    # mensagem "canal fechado" com a alternativa viva ao lado.
    for grupo in ([s for s in candidatos if s.is_online], candidatos):
        if not grupo:
            continue
        if len(grupo) == 1:
            return grupo[0]
        completas = [s for s in grupo if s.completa]
        if len(completas) == 1:
            return completas[0]
        # Duas completas, ou duas diretas: empate de verdade.
        return None
    return None


def _exigir_online(escolha: SurfaceMatch, online: list[SurfaceMatch]) -> SurfaceMatch:
    from modules.devices.control import DeviceResolutionError

    if escolha.is_online:
        return escolha
    raise DeviceResolutionError(
        f"'{escolha.label}' não está com o canal aberto agora — abra o navegador com "
        "a extensão do Neo (ou o Neo Desktop nessa máquina).",
        candidates=[s.label for s in online],
    )


# ── A escada de capacidade (docs/browser-surface-plan.md §13.3) ───────────────


class AcaoIndisponivel(Exception):
    """A superfície resolvida não sabe fazer esta ação.

    Erro separado de propósito: "o navegador recusou a ação" não é acionável, e
    "baixar arquivo precisa do Neo Desktop aberto nessa máquina" é. A mensagem
    carrega o que falta e, quando existe, para onde ir.
    """

    def __init__(self, message: str, *, falta: str = "", alternativa: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.falta = falta
        self.alternativa = alternativa


#: Ações do vocabulário que o core sabe **derivar** de primitivas (degrau 2).
#:
#: **Vazio hoje, e isso é deliberado.** O plano (§13.4) prevê a biblioteca de
#: `js_*` migrada do Rust para cá; o que NÃO fazemos é uma tradução de ~1.000
#: linhas de JavaScript sem cliente que a exercite — nenhuma superfície direta
#: existe ainda, então cada linha portada seria código não executado, do tipo que
#: apodrece afirmando funcionar. O formato certo de crescer é este dicionário:
#: uma ação por vez, cada uma com o par de testes do §13.7 (capaz usa a nativa,
#: pobre degrada), no momento em que houver uma superfície para provar contra.
#:
#: Contrato de cada entrada: ``acao -> (primitiva_exigida, construtor)``, onde
#: ``construtor(args) -> (acao_primitiva, args_primitivos)``. Explícito em vez de
#: adivinhado: qual primitiva a derivação precisa é decisão de quem a escreve, não
#: dedução do nome da ação.
DERIVADAS: dict[str, tuple[str, Any]] = {}

#: Ação → (primitiva exigida, o que dizer quando falta). É a tabela que faz a
#: recusa ENSINAR em vez de só negar.
EXIGE_PRIMITIVA: dict[str, tuple[str, str]] = {
    "browser.downloads": (
        "downloads_api",
        "listar downloads precisa de um navegador com a extensão do Neo",
    ),
}

#: Ações cujo resultado o desktop produz lendo o DISCO da máquina — extensão não
#: alcança, e o core está na VPS. Degradam em vez de falhar (§13.3).
DEGRADA_SEM_DISCO: frozenset[str] = frozenset({"browser.downloads"})


async def dispatch(
    surface: SurfaceMatch,
    act: Any,
    action: str,
    args: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Executa uma ação do vocabulário pela escada: nativa → derivada → ausente.

    ``act`` é o transporte cru (o mesmo ``broadcaster.request`` de sempre). A
    escada fica ACIMA dele, então o driver não muda — é o que permite trocar o
    executor sem tocar nas 2.900 linhas que já funcionam.

    Com o desktop na frente, o degrau 1 responde e o comportamento é **idêntico
    ao de hoje**: mesma ação, mesma implementação nativa em Rust.
    """
    argumentos = dict(args or {})

    # Degrau 1 — nativa. O provedor implementa o vocabulário de alto nível.
    if surface.pode("native_actions"):
        return await _chamar(act, action, argumentos, timeout)

    # Degrau 2 — derivada: o core compõe com as primitivas que a superfície tem.
    derivada = DERIVADAS.get(action)
    if derivada is not None:
        primitiva_exigida, construtor = derivada
        if surface.pode(primitiva_exigida):
            acao_primitiva, args_primitivos = construtor(argumentos)
            return await _chamar(act, acao_primitiva, args_primitivos, timeout)

    # Degrau 3 — ausente. Recusa que diz o que falta.
    exigida = EXIGE_PRIMITIVA.get(action)
    if exigida is not None and not surface.pode(exigida[0]):
        raise AcaoIndisponivel(
            f"'{surface.label}' não faz isso: {exigida[1]}.",
            falta=exigida[0],
        )
    if action in DEGRADA_SEM_DISCO and not surface.pode("downloads_disk"):
        # Sabe o que o navegador acha, não confirma o byte no disco. Responder o
        # que sabe DIZENDO o que não sabe é melhor que fingir certeza.
        resultado = await _chamar(act, action, argumentos, timeout)
        if isinstance(resultado, dict):
            resultado.setdefault(
                "aviso",
                "lista vinda do navegador, sem conferir o arquivo no disco — "
                "para confirmar o download, o Neo Desktop precisa estar aberto "
                "nessa máquina",
            )
        return resultado

    raise AcaoIndisponivel(
        f"'{surface.label}' não sabe executar '{action}'. Com o Neo Desktop aberto "
        "nessa máquina eu consigo; pela extensão sozinha, não.",
        falta="native_actions",
    )


async def _chamar(act: Any, action: str, args: dict[str, Any], timeout: float | None):
    if timeout is None:
        return await act(action, args)
    return await act(action, args, timeout)


async def get_surface(db: Any, user_id: UUID, device_id: str) -> SurfaceMatch | None:
    """A superfície de um ``device_id`` conhecido (sem resolver por nome)."""
    for s in await list_surfaces(db, user_id):
        if s.device_id == device_id:
            return s
    return None


async def abas_conhecidas(db: Any, user_id: UUID) -> list[dict]:
    """Abas que as superfícies do usuário empurraram, de qualquer máquina.

    docs/browser-surface-plan.md §7. Hoje o snapshot de abas pega carona no turno
    do desktop (``ai_agent.rs``: ``metadata["media_context"]["browser_tabs"]``), o
    que significa que ele **não existe** quando o pedido vem do painel ou do
    celular — exatamente o caso que o plano abre. Com a superfície empurrando pelo
    canal, o core sabe o que está aberto sem depender de quem iniciou o turno.

    Só superfície ONLINE entra: aba reportada por um canal que caiu é uma foto de
    um navegador que talvez nem esteja mais aberto.
    """
    registry = get_surface_registry()
    saida: list[dict] = []
    try:
        for s in await list_surfaces(db, user_id):
            if not s.is_online:
                continue
            for aba in registry.tabs(s.device_id):
                # De qual superfície veio: sem isso, o core não sabe em qual
                # máquina mandar tocar quando há duas reportando.
                saida.append({**aba, "surface_id": s.device_id, "surface": s.label})
    except Exception as exc:  # noqa: BLE001 — contexto nunca derruba o turno
        logger.debug("[surface] não consegui listar abas: %s", exc)
    return saida
