"""Resolução de superfície de navegador (docs/browser-surface-plan.md §5).

As duas histórias que motivaram o plano, e as regras que impedem o pior erro
possível aqui — abrir a aba errada na tela dele:

1. **pedindo de dentro do desktop**: a superfície da própria máquina, sem nome;
2. **pedindo pelo celular**: "o navegador do macbook", resolvido pelo nome do
   HOSPEDEIRO, porque a superfície está vinculada a ele (`host_device_id`).

E a invariante: empate **pergunta**. Nunca chuta.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.browser_local.surfaces import (
    BROWSER_CAPABILITY,
    SurfaceMatch,
    HEADLESS_CAPABILITY,
    PROVIDER_DESKTOP,
    PROVIDER_EXTENSION,
    get_surface_registry,
    list_surfaces,
    resolve_browser_surface,
)
from core.notifications.device_control import DeviceControlBroadcaster
from modules.devices.control import BROWSER_SURFACE_TYPE, DeviceResolutionError


class _FakeDB:
    def __init__(self, devices):
        self._devices = devices

    async def get_connected_devices(self, user_id=None):
        return list(self._devices)


def _desktop(device_id: str, nome: str, *, headless: bool = True):
    return SimpleNamespace(
        device_id=device_id,
        display_name=nome,
        model="MacBookPro18,3",
        os_platform="darwin",
        device_type="neo-desktop",
        host_device_id=None,
        capabilities={
            "volume": True, "media": True, "lock": True, "power": True,
            BROWSER_CAPABILITY: True, HEADLESS_CAPABILITY: headless,
        },
    )


def _superficie(device_id: str, nome: str, host: str | None):
    return SimpleNamespace(
        device_id=device_id,
        display_name=nome,
        model="Edge",
        os_platform="darwin",
        device_type=BROWSER_SURFACE_TYPE,
        host_device_id=host,
        capabilities={
            BROWSER_CAPABILITY: True,
            HEADLESS_CAPABILITY: False,
            "primitives": {"eval": True, "cdp": True, "screenshot": True},
        },
    )


@pytest.fixture
def online(monkeypatch):
    """Controla quem está com o canal aberto, sem WebSocket de verdade."""
    b = DeviceControlBroadcaster()
    conectados: set[str] = set()
    monkeypatch.setattr(b, "is_connected", lambda d: d in conectados)
    monkeypatch.setattr(
        "core.notifications.device_control.get_device_control_broadcaster", lambda: b
    )
    get_surface_registry()._reset_for_tests()
    return conectados


# ── história 1: de dentro do desktop ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_do_desktop_resolve_a_propria_maquina_sem_nome(online):
    db = _FakeDB([_desktop("dev-mac", "macbook")])
    online.add("dev-mac")

    s = await resolve_browser_surface(db, uuid4(), None, turn_device_id="dev-mac")
    assert s.device_id == "dev-mac"
    assert s.provider == PROVIDER_DESKTOP
    assert s.completa


@pytest.mark.asyncio
async def test_com_desktop_e_extensao_na_mesma_maquina_a_completa_ganha(online):
    """A direta não substitui o desktop: ela faz menos (§12.3)."""
    db = _FakeDB([
        _desktop("dev-mac", "macbook"),
        _superficie("surf-edge", "Edge do macbook", "dev-mac"),
    ])
    online.update({"dev-mac", "surf-edge"})

    s = await resolve_browser_surface(db, uuid4(), None, turn_device_id="dev-mac")
    assert s.device_id == "dev-mac"


@pytest.mark.asyncio
async def test_desktop_fechado_usa_a_extensao_da_mesma_maquina(online):
    """O caminho que hoje não existe: sem desktop, o navegador dele continua lá."""
    db = _FakeDB([
        _desktop("dev-mac", "macbook"),
        _superficie("surf-edge", "Edge do macbook", "dev-mac"),
    ])
    online.add("surf-edge")  # desktop fechado

    s = await resolve_browser_surface(db, uuid4(), None, turn_device_id="dev-mac")
    assert s.device_id == "surf-edge"
    assert s.provider == PROVIDER_EXTENSION
    assert not s.completa


# ── história 2: pelo celular, nomeando a máquina ─────────────────────────────

@pytest.mark.asyncio
async def test_pelo_celular_o_nome_do_hospedeiro_acha_a_superficie(online):
    """Sem device no canal (pedido pelo painel), "macbook" resolve o navegador dele."""
    db = _FakeDB([
        _desktop("dev-mac", "macbook"),
        _superficie("surf-edge", "Edge do macbook", "dev-mac"),
    ])
    online.add("surf-edge")

    s = await resolve_browser_surface(db, uuid4(), "macbook", turn_device_id=None)
    assert s.device_id == "surf-edge"


@pytest.mark.asyncio
async def test_pelo_celular_com_o_nome_da_superficie(online):
    db = _FakeDB([_superficie("surf-edge", "Edge do macbook", "dev-mac")])
    online.add("surf-edge")

    s = await resolve_browser_surface(db, uuid4(), "Edge do macbook")
    assert s.device_id == "surf-edge"


@pytest.mark.asyncio
async def test_nome_que_nao_existe_diz_o_que_existe(online):
    db = _FakeDB([_superficie("surf-edge", "Edge do macbook", "dev-mac")])
    online.add("surf-edge")

    with pytest.raises(DeviceResolutionError) as erro:
        await resolve_browser_surface(db, uuid4(), "firefox do escritório")
    assert "Edge do macbook" in erro.value.candidates


# ── a invariante: empate pergunta ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dois_navegadores_na_mesma_maquina_pergunta(online):
    """Edge + Chrome no mesmo Mac é o caso NORMAL, não a exceção."""
    db = _FakeDB([
        _superficie("surf-edge", "Edge do macbook", "dev-mac"),
        _superficie("surf-chrome", "Chrome do macbook", "dev-mac"),
    ])
    online.update({"surf-edge", "surf-chrome"})

    with pytest.raises(DeviceResolutionError) as erro:
        await resolve_browser_surface(db, uuid4(), "macbook")
    assert len(erro.value.candidates) == 2


@pytest.mark.asyncio
async def test_duas_maquinas_sem_nome_pergunta(online):
    db = _FakeDB([_desktop("dev-mac", "macbook"), _desktop("dev-pc", "PC da sala")])
    online.update({"dev-mac", "dev-pc"})

    with pytest.raises(DeviceResolutionError) as erro:
        await resolve_browser_surface(db, uuid4(), None)
    assert len(erro.value.candidates) == 2


@pytest.mark.asyncio
async def test_superficie_offline_diz_que_esta_fechada(online):
    """"Não existe" e "está fechado agora" pedem ações diferentes."""
    db = _FakeDB([_superficie("surf-edge", "Edge do macbook", "dev-mac")])
    # ninguém online

    with pytest.raises(DeviceResolutionError) as erro:
        await resolve_browser_surface(db, uuid4(), "Edge do macbook")
    assert "canal aberto" in erro.value.message


@pytest.mark.asyncio
async def test_conta_sem_navegador_nenhum(online):
    db = _FakeDB([])
    with pytest.raises(DeviceResolutionError) as erro:
        await resolve_browser_surface(db, uuid4(), None)
    assert "Nenhum navegador registrado" in erro.value.message


# ── quem NÃO é superfície não entra na lista ──────────────────────────────────

@pytest.mark.asyncio
async def test_celular_sem_navegador_nao_e_candidato(online):
    """Um device que não declara `browser_local` não é navegador."""
    celular = SimpleNamespace(
        device_id="dev-phone", display_name="celular", model="iPhone",
        os_platform="ios", device_type="neo-mobile", host_device_id=None,
        capabilities={"volume": True},
    )
    db = _FakeDB([celular, _desktop("dev-mac", "macbook")])
    online.update({"dev-phone", "dev-mac"})

    superficies = await list_surfaces(db, uuid4())
    assert [s.device_id for s in superficies] == ["dev-mac"]


# ── o registry: declaração é estado de conexão ────────────────────────────────

def test_declaracao_morre_com_a_conexao():
    r = get_surface_registry()
    r._reset_for_tests()
    r.declare("surf-x", provider=PROVIDER_EXTENSION, engine="edge",
              primitives={"eval": True, "cdp": True})
    assert r.primitives("surf-x")["eval"] is True

    r.forget("surf-x")
    assert r.primitives("surf-x") == {}


def test_snapshot_de_abas_velho_e_descartado():
    """Decidir reuso de aba sobre uma foto de 5 min atrás manda tocar na aba errada."""
    r = get_surface_registry()
    r._reset_for_tests()
    r.set_tabs("surf-x", [{"site": "youtube", "tabId": 7}])
    assert len(r.tabs("surf-x")) == 1

    # Envelhece o registro em vez de mexer no relógio: menos frágil, e é
    # exatamente o estado que interessa (snapshot antigo, conexão viva).
    r._por_device["surf-x"].tabs_em -= 600
    assert r.tabs("surf-x") == []
    # Com a janela aberta o bastante, volta a valer.
    assert len(r.tabs("surf-x", max_idade_s=3600)) == 1


# ── A escada de capacidade (§13.3) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_desktop_cai_no_degrau_1_e_nada_muda():
    """Com o desktop na frente, a escada é transparente: mesma ação, mesmo caminho."""
    from core.browser_local.surfaces import dispatch

    chamadas: list[tuple[str, dict]] = []

    async def act(action, args, timeout=None):
        chamadas.append((action, args))
        return {"ok": True}

    s = SurfaceMatch(
        device_id="dev-mac", label="macbook", provider=PROVIDER_DESKTOP,
        host_device_id=None, is_online=True,
        primitives={"native_actions": True, "downloads_disk": True},
    )
    await dispatch(s, act, "browser.click", {"reference": 7}, 10)
    assert chamadas == [("browser.click", {"reference": 7})]


@pytest.mark.asyncio
async def test_superficie_pobre_recusa_ensinando():
    """Recusa acionável: diz o que falta e para onde ir."""
    from core.browser_local.surfaces import AcaoIndisponivel, dispatch

    async def act(action, args, timeout=None):  # pragma: no cover - não deve rodar
        raise AssertionError("não deveria chegar no transporte")

    s = SurfaceMatch(
        device_id="surf-edge", label="Edge do macbook", provider=PROVIDER_EXTENSION,
        host_device_id="dev-mac", is_online=True,
        primitives={"eval": True, "cdp": True},  # sem native_actions
    )
    with pytest.raises(AcaoIndisponivel) as erro:
        await dispatch(s, act, "browser.click", {"reference": 7}, 10)
    assert "Neo Desktop" in erro.value.message
    assert erro.value.falta == "native_actions"


@pytest.mark.asyncio
async def test_download_sem_disco_responde_dizendo_o_que_nao_sabe():
    """"Vira como pode": entrega a visão do navegador e declara o limite."""
    from core.browser_local.surfaces import dispatch

    async def act(action, args, timeout=None):
        return {"downloads": [{"name": "nota.pdf"}]}

    s = SurfaceMatch(
        device_id="surf-edge", label="Edge do macbook", provider=PROVIDER_EXTENSION,
        host_device_id="dev-mac", is_online=True,
        primitives={"eval": True, "cdp": True, "downloads_api": True},
    )
    out = await dispatch(s, act, "browser.downloads", {}, 10)
    assert out["downloads"]
    assert "sem conferir o arquivo no disco" in out["aviso"]


@pytest.mark.asyncio
async def test_derivada_usa_a_primitiva_declarada(monkeypatch):
    """O degrau 2 existe e funciona — o que falta é conteúdo, não mecanismo."""
    from core.browser_local import surfaces as mod

    chamadas: list[tuple[str, dict]] = []

    async def act(action, args, timeout=None):
        chamadas.append((action, args))
        return {"ok": True}

    monkeypatch.setitem(
        mod.DERIVADAS,
        "browser.click",
        ("eval", lambda args: ("browser.eval", {"expression": f"clica({args['reference']})"})),
    )
    s = SurfaceMatch(
        device_id="surf-edge", label="Edge", provider=PROVIDER_EXTENSION,
        host_device_id=None, is_online=True, primitives={"eval": True},
    )
    await mod.dispatch(s, act, "browser.click", {"reference": 7}, 10)
    assert chamadas == [("browser.eval", {"expression": "clica(7)"})]


@pytest.mark.asyncio
async def test_derivada_nao_usada_quando_falta_a_primitiva(monkeypatch):
    from core.browser_local import surfaces as mod

    async def act(action, args, timeout=None):  # pragma: no cover
        raise AssertionError("não deveria chegar no transporte")

    monkeypatch.setitem(
        mod.DERIVADAS, "browser.click", ("eval", lambda args: ("browser.eval", {}))
    )
    s = SurfaceMatch(
        device_id="surf-x", label="Sem eval", provider=PROVIDER_EXTENSION,
        host_device_id=None, is_online=True, primitives={"cdp": True},
    )
    with pytest.raises(mod.AcaoIndisponivel):
        await mod.dispatch(s, act, "browser.click", {"reference": 7}, 10)


# ── Continuidade de aba chaveada pelo NAVEGADOR (§12.5) ──────────────────────
#
# O `browser_session` carrega o `pending`: a chave que autoriza AQUELE clique
# depois da aprovação de liberação. Chaveá-lo pelo transporte (`device_id`) faria
# a troca de via perder uma autorização já dada, que não pode ser reemitida.


def test_chave_do_navegador_e_a_maquina_nao_o_transporte():
    from core.jobs.runner_browser_local import chave_do_navegador

    desktop = SurfaceMatch(
        device_id="dev-mac", label="macbook", provider=PROVIDER_DESKTOP,
        host_device_id=None, is_online=True, primitives={"native_actions": True},
    )
    extensao = SurfaceMatch(
        device_id="surf-edge", label="Edge do macbook", provider=PROVIDER_EXTENSION,
        host_device_id="dev-mac", is_online=True, primitives={"eval": True},
    )

    # As duas vias do MESMO Mac colidem de propósito: é o mesmo Edge, e a aba
    # (com o clique autorizado) precisa ser a mesma.
    assert chave_do_navegador(desktop, "dev-mac") == "dev-mac"
    assert chave_do_navegador(extensao, "surf-edge") == "dev-mac"

    # Sem superfície resolvida, o device_id vale como chave — comportamento
    # histórico, para não invalidar sessão gravada antes disto existir.
    assert chave_do_navegador(None, "dev-mac") == "dev-mac"


def test_superficie_sem_hospedeiro_usa_o_proprio_id():
    """Extensão pareada antes da coluna existir não vira chave vazia."""
    from core.jobs.runner_browser_local import chave_do_navegador

    orfa = SurfaceMatch(
        device_id="surf-solta", label="Chrome", provider=PROVIDER_EXTENSION,
        host_device_id=None, is_online=True, primitives={"eval": True},
    )
    assert chave_do_navegador(orfa, "surf-solta") == "surf-solta"
