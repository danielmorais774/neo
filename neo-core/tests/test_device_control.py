"""Testes do controle de device (docs/device-control-plan.md).

Cobre as três coisas que decidem se a feature presta:

1. o canal de controle correlaciona resposta com pedido, e falha quando não há
   resposta (nunca devolve sucesso sem confirmação do device);
2. a resolução por NOME — porque o modelo inventa ids, e um nome ambíguo tem
   que perguntar em vez de chutar;
3. o pareamento — código de uso único, token que só existe como hash.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from core.notifications.device_control import (
    DeviceControlBroadcaster,
    DeviceOffline,
    DeviceTimeout,
)
from modules.devices.control import (
    DeviceResolutionError,
    execute_device_action,
    resolve_device,
)
from modules.devices.pairing import (
    _reset_for_tests,
    consume_pairing_code,
    generate_pairing_code,
    hash_device_token,
    issue_device_token,
)


# ── Fakes ────────────────────────────────────────────────────────────────────

class _FakeDevice:
    def __init__(
        self,
        device_id: str,
        display_name: str | None = None,
        *,
        model: str = "MacBookPro18,3",
        os_platform: str = "darwin",
        capabilities: dict | None = None,
    ):
        self.device_id = device_id
        self.display_name = display_name
        self.model = model
        self.os_platform = os_platform
        self.capabilities = capabilities


class _FakeDB:
    def __init__(self, devices):
        self._devices = devices

    async def get_connected_devices(self, user_id=None):
        return list(self._devices)


async def _fake_device_loop(
    broadcaster: DeviceControlBroadcaster,
    device_id: str,
    responder,
    *,
    stop: asyncio.Event | None = None,
):
    """Simula um desktop: consome comandos e responde pelo `responder`."""
    async for command in broadcaster.register(device_id):
        reply = responder(command)
        if reply is not None:
            await broadcaster.resolve(device_id, reply)
        if stop is not None and stop.is_set():
            return


def _patch_broadcaster(monkeypatch, broadcaster):
    """Faz control.py e service.py enxergarem ESTE broadcaster."""
    import core.notifications.device_control as mod
    import modules.devices.control as control_mod

    monkeypatch.setattr(mod, "_device_control_broadcaster", broadcaster)
    monkeypatch.setattr(
        control_mod, "get_device_control_broadcaster", lambda: broadcaster
    )


# ── Canal de controle ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_request_casa_resposta_com_pedido():
    b = DeviceControlBroadcaster()
    task = asyncio.create_task(
        _fake_device_loop(
            b,
            "dev-a",
            lambda cmd: {"id": cmd["id"], "ok": True, "result": {"level": 30}},
        )
    )
    await asyncio.sleep(0.01)

    result = await b.request("dev-a", "volume_set", {"level": 30})
    assert result["ok"] is True
    assert result["result"]["level"] == 30

    task.cancel()


@pytest.mark.asyncio
async def test_request_em_device_sem_canal_levanta_offline():
    b = DeviceControlBroadcaster()
    with pytest.raises(DeviceOffline):
        await b.request("fantasma", "mute")


@pytest.mark.asyncio
async def test_device_mudo_vira_timeout_nao_sucesso():
    """Device que recebe e não responde NÃO pode virar 'abaixei o volume'."""
    b = DeviceControlBroadcaster()
    task = asyncio.create_task(_fake_device_loop(b, "dev-a", lambda cmd: None))
    await asyncio.sleep(0.01)

    with pytest.raises(DeviceTimeout):
        await b.request("dev-a", "mute", timeout=0.05)

    task.cancel()


@pytest.mark.asyncio
async def test_desconexao_falha_comandos_em_voo():
    """Sem isso, um comando pendurado ficaria esperando até o timeout."""
    b = DeviceControlBroadcaster()
    task = asyncio.create_task(_fake_device_loop(b, "dev-a", lambda cmd: None))
    await asyncio.sleep(0.01)

    pending = asyncio.create_task(b.request("dev-a", "mute", timeout=5))
    await asyncio.sleep(0.01)
    task.cancel()  # device caiu

    with pytest.raises(DeviceOffline):
        await pending


@pytest.mark.asyncio
async def test_reconexao_derruba_canal_anterior():
    """Dois canais para o mesmo device mandariam comando para o socket morto."""
    b = DeviceControlBroadcaster()
    first = asyncio.create_task(_fake_device_loop(b, "dev-a", lambda cmd: None))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(
        _fake_device_loop(
            b, "dev-a", lambda cmd: {"id": cmd["id"], "ok": True, "result": {}}
        )
    )
    await asyncio.sleep(0.02)

    assert b.connected_devices() == ["dev-a"]
    result = await b.request("dev-a", "mute", timeout=1)
    assert result["ok"] is True

    first.cancel()
    second.cancel()


@pytest.mark.asyncio
async def test_power_confirma_por_desconexao():
    """suspend/shutdown matam o WS antes de responder — sumir É o sucesso."""
    b = DeviceControlBroadcaster()
    stop = asyncio.Event()

    async def device():
        async for _command in b.register("dev-a"):
            stop.set()  # "a máquina caiu"
            return

    task = asyncio.create_task(device())
    await asyncio.sleep(0.01)

    assert await b.send_and_expect_disconnect("dev-a", "suspend", timeout=1) is True
    task.cancel()


@pytest.mark.asyncio
async def test_power_que_nao_derruba_devolve_false():
    """Usuário cancelou o diálogo: o device continua vivo — não é sucesso."""
    b = DeviceControlBroadcaster()
    task = asyncio.create_task(_fake_device_loop(b, "dev-a", lambda cmd: None))
    await asyncio.sleep(0.01)

    assert await b.send_and_expect_disconnect("dev-a", "shutdown", timeout=0.05) is False
    task.cancel()


# ── Resolução por nome ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_por_nome_ignora_acento_e_caixa(monkeypatch):
    b = DeviceControlBroadcaster()
    _patch_broadcaster(monkeypatch, b)
    db = _FakeDB([_FakeDevice("dev-a", "PC da Sala"), _FakeDevice("dev-b", "Notebook")])

    match = await resolve_device(db, uuid4(), "pc da sala")
    assert match.device_id == "dev-a"


@pytest.mark.asyncio
async def test_nome_ambiguo_pergunta_em_vez_de_chutar(monkeypatch):
    b = DeviceControlBroadcaster()
    _patch_broadcaster(monkeypatch, b)
    db = _FakeDB(
        [_FakeDevice("dev-a", "Notebook Trabalho"), _FakeDevice("dev-b", "Notebook Casa")]
    )

    with pytest.raises(DeviceResolutionError) as exc:
        await resolve_device(db, uuid4(), "notebook")
    assert len(exc.value.candidates) == 2


@pytest.mark.asyncio
async def test_nome_inexistente_lista_os_que_existem(monkeypatch):
    b = DeviceControlBroadcaster()
    _patch_broadcaster(monkeypatch, b)
    db = _FakeDB([_FakeDevice("dev-a", "PC da Sala")])

    with pytest.raises(DeviceResolutionError) as exc:
        await resolve_device(db, uuid4(), "PC do Quarto")
    assert exc.value.candidates == ["PC da Sala"]


@pytest.mark.asyncio
async def test_sem_nome_usa_o_device_da_conversa(monkeypatch):
    b = DeviceControlBroadcaster()
    _patch_broadcaster(monkeypatch, b)
    db = _FakeDB([_FakeDevice("dev-a", "PC da Sala"), _FakeDevice("dev-b", "Notebook")])

    match = await resolve_device(db, uuid4(), None, current_device_id="dev-b")
    assert match.device_id == "dev-b"


# ── Ação ponta a ponta ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acao_devolve_estado_real_do_device(monkeypatch):
    """O Neo deve reportar o que o aparelho mediu, não o que ele pediu."""
    b = DeviceControlBroadcaster()
    _patch_broadcaster(monkeypatch, b)
    db = _FakeDB([_FakeDevice("dev-a", "PC da Sala")])

    # O device arredondou 30 → 31 (passo do hardware).
    task = asyncio.create_task(
        _fake_device_loop(
            b, "dev-a", lambda cmd: {"id": cmd["id"], "ok": True, "result": {"level": 31}}
        )
    )
    await asyncio.sleep(0.01)

    out = await execute_device_action(
        db, uuid4(), action="volume_set", device_name="PC da Sala", value=30
    )
    assert out["ok"] is True
    assert out["result"]["level"] == 31
    task.cancel()


@pytest.mark.asyncio
async def test_device_offline_falha_duro(monkeypatch):
    """Nada de 'abaixei' num aparelho que nem canal tem."""
    b = DeviceControlBroadcaster()
    _patch_broadcaster(monkeypatch, b)
    db = _FakeDB([_FakeDevice("dev-a", "PC da Sala")])

    out = await execute_device_action(
        db, uuid4(), action="mute", device_name="PC da Sala"
    )
    assert out["ok"] is False
    assert "canal de controle" in out["error"]


@pytest.mark.asyncio
async def test_volume_fora_da_faixa_e_recusado(monkeypatch):
    b = DeviceControlBroadcaster()
    _patch_broadcaster(monkeypatch, b)
    db = _FakeDB([_FakeDevice("dev-a", "PC da Sala")])
    task = asyncio.create_task(
        _fake_device_loop(
            b, "dev-a", lambda cmd: {"id": cmd["id"], "ok": True, "result": {}}
        )
    )
    await asyncio.sleep(0.01)

    out = await execute_device_action(
        db, uuid4(), action="volume_set", device_name="PC da Sala", value=150
    )
    assert out["ok"] is False
    assert "0-100" in out["error"]
    task.cancel()


@pytest.mark.asyncio
async def test_capability_ausente_recusa_antes_de_prometer(monkeypatch):
    """Não oferecer shutdown num device que declarou não saber desligar."""
    b = DeviceControlBroadcaster()
    _patch_broadcaster(monkeypatch, b)
    db = _FakeDB(
        [_FakeDevice("dev-a", "Tablet", capabilities={"volume": True, "power": False})]
    )
    task = asyncio.create_task(_fake_device_loop(b, "dev-a", lambda cmd: None))
    await asyncio.sleep(0.01)

    out = await execute_device_action(
        db, uuid4(), action="shutdown", device_name="Tablet"
    )
    assert out["ok"] is False
    assert "não suporta" in out["error"]
    task.cancel()


@pytest.mark.asyncio
async def test_acao_desconhecida_nao_vai_pro_device(monkeypatch):
    b = DeviceControlBroadcaster()
    _patch_broadcaster(monkeypatch, b)
    db = _FakeDB([_FakeDevice("dev-a", "PC da Sala")])

    out = await execute_device_action(
        db, uuid4(), action="format_disk", device_name="PC da Sala"
    )
    assert out["ok"] is False
    assert "desconhecida" in out["error"]


@pytest.mark.asyncio
async def test_device_power_exige_alvo_explicito():
    """Escolher sozinho qual máquina desligar é irreversível — não pode."""
    from modules.devices import DevicesExecutor

    executor = DevicesExecutor(db=_FakeDB([]))
    executor.set_context(uuid4(), current_device_id="dev-a")

    out = await executor.execute("device_power", {"action": "suspend"})
    assert out["ok"] is False
    assert "qual dispositivo" in out["error"]


# ── Pareamento ───────────────────────────────────────────────────────────────

def test_codigo_de_pareamento_e_de_uso_unico():
    _reset_for_tests()
    user_id = uuid4()
    code, ttl = generate_pairing_code(user_id)

    assert len(code) == 6 and code.isdigit()
    assert ttl > 0
    assert consume_pairing_code(code) == user_id
    assert consume_pairing_code(code) is None  # replay não vale


def test_gerar_codigo_novo_invalida_o_anterior():
    """Códigos válidos esquecidos por aí são cada um uma chave da casa."""
    _reset_for_tests()
    user_id = uuid4()
    first, _ = generate_pairing_code(user_id)
    second, _ = generate_pairing_code(user_id)

    assert consume_pairing_code(first) is None
    assert consume_pairing_code(second) == user_id


def test_codigo_invalido_nao_pareia():
    _reset_for_tests()
    assert consume_pairing_code("000000") is None
    assert consume_pairing_code("") is None


def test_token_do_device_so_existe_como_hash():
    token, token_hash = issue_device_token()

    assert len(token) >= 32
    assert token != token_hash
    assert hash_device_token(token) == token_hash
    assert hash_device_token("outro-token") != token_hash


# ── F5: autenticação dos canais antigos ──────────────────────────────────────

class _FakeWebSocket:
    """WebSocket mínimo: só registra se foi fechado e com qual código."""

    def __init__(self):
        self.closed_with: tuple[int, str] | None = None

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed_with = (code, reason)


class _FakeAuthDB:
    def __init__(self, devices_by_hash: dict):
        self._by_hash = devices_by_hash

    async def get_device_by_token_hash(self, token_hash: str):
        return self._by_hash.get(token_hash)


def _patch_auth(monkeypatch, db, *, required: bool):
    import api.routes.devices as routes
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(routes, "_db", db)
    monkeypatch.setattr(settings, "neo_device_auth_required", required, raising=False)
    monkeypatch.setattr(routes, "get_settings", get_settings, raising=False)
    return routes


@pytest.mark.asyncio
async def test_token_errado_e_recusado_mesmo_com_auth_desligada(monkeypatch):
    """Credencial INVÁLIDA nunca é 'tudo bem' — só a ausência é negociável."""
    from modules.devices.pairing import hash_device_token

    db = _FakeAuthDB({hash_device_token("token-do-a"): _FakeDevice("dev-a")})
    routes = _patch_auth(monkeypatch, db, required=False)

    ws = _FakeWebSocket()
    ok = await routes._device_ws_authorized(ws, "dev-a", "token-de-outro")

    assert ok is False
    assert ws.closed_with == (4401, "unpaired")


@pytest.mark.asyncio
async def test_token_de_outro_device_nao_abre_o_canal_deste(monkeypatch):
    """Um device pareado não pode se passar por outro."""
    from modules.devices.pairing import hash_device_token

    db = _FakeAuthDB({hash_device_token("token-do-b"): _FakeDevice("dev-b")})
    routes = _patch_auth(monkeypatch, db, required=False)

    ws = _FakeWebSocket()
    assert await routes._device_ws_authorized(ws, "dev-a", "token-do-b") is False
    assert ws.closed_with == (4401, "unpaired")


@pytest.mark.asyncio
async def test_sem_token_passa_na_fase_permissiva(monkeypatch):
    """Canais antigos nasceram sem auth: virar a chave de uma vez derruba o app."""
    routes = _patch_auth(monkeypatch, _FakeAuthDB({}), required=False)

    ws = _FakeWebSocket()
    assert await routes._device_ws_authorized(ws, "dev-a", None) is True
    assert ws.closed_with is None


@pytest.mark.asyncio
async def test_sem_token_e_recusado_com_auth_obrigatoria(monkeypatch):
    routes = _patch_auth(monkeypatch, _FakeAuthDB({}), required=True)

    ws = _FakeWebSocket()
    assert await routes._device_ws_authorized(ws, "dev-a", None) is False
    assert ws.closed_with == (4401, "unpaired")


@pytest.mark.asyncio
async def test_token_certo_passa_com_auth_obrigatoria(monkeypatch):
    from modules.devices.pairing import hash_device_token

    db = _FakeAuthDB({hash_device_token("token-do-a"): _FakeDevice("dev-a")})
    routes = _patch_auth(monkeypatch, db, required=True)

    ws = _FakeWebSocket()
    assert await routes._device_ws_authorized(ws, "dev-a", "token-do-a") is True
    assert ws.closed_with is None


# ── Remoção de device ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remover_device_derruba_o_canal_aberto():
    """O token some com a linha, mas a conexão já aberta continuaria valendo."""
    b = DeviceControlBroadcaster()
    task = asyncio.create_task(_fake_device_loop(b, "dev-a", lambda cmd: None))
    await asyncio.sleep(0.01)
    assert b.is_connected("dev-a")

    assert await b.kick("dev-a") is True
    await asyncio.sleep(0.02)
    assert not b.is_connected("dev-a")

    task.cancel()


@pytest.mark.asyncio
async def test_kick_em_device_sem_canal_nao_explode():
    b = DeviceControlBroadcaster()
    assert await b.kick("fantasma") is False
