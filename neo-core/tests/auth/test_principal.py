"""Resolução de identidade e os degraus do portão (docs/api-auth-plan.md).

O que estes testes protegem, em ordem de importância:

1. **a amarração** — ``owner_of`` recusa (403) um pedido que declara dono
   diferente da credencial. Era por aí que o ``user_id`` do corpo escrevia na
   memória de outra pessoa; autenticar sem isto não teria fechado nada;
2. **os degraus** — ``off``/``shadow`` passam (a virada de chave não pode
   derrubar o desktop no meio de um turno), ``enforce`` barra, ``strict`` recusa
   também o JWT estático do painel;
3. **o JWT de usuário não morre na função do painel** — o bug §7.1 do plano.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.auth import jwt as jwt_mod
from api.auth import principal as principal_mod
from api.auth.principal import (
    MODE_ENFORCE,
    MODE_OFF,
    MODE_SHADOW,
    MODE_STRICT,
    Principal,
    owner_of,
    require_owner,
    require_principal,
)
from shared.config import get_settings


@pytest.fixture(autouse=True)
def _modo(monkeypatch):
    """Cada teste diz o seu modo; o default do fixture é o de produção hoje."""
    settings = get_settings()
    monkeypatch.setattr(settings, "neo_api_auth_mode", MODE_SHADOW, raising=False)
    yield settings


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/dono")
    async def rota_dono(p: Principal = Depends(require_owner)):
        return {"kind": p.kind, "user_id": str(p.user_id) if p.user_id else None}

    @app.get("/qualquer")
    async def rota_qualquer(p: Principal = Depends(require_principal)):
        return {"kind": p.kind}

    return app


@pytest.fixture
def client():
    return TestClient(_app(), raise_server_exceptions=False)


def _device_fake(user_id, monkeypatch, *, token="tok-valido"):
    """Finge o banco de devices: um token conhecido, resolvido para um dono."""
    from api.routes import devices as devices_routes

    class _DB:
        async def get_device_by_token_hash(self, token_hash):
            from modules.devices.pairing import hash_device_token

            if token_hash == hash_device_token(token):
                return SimpleNamespace(
                    user_id=user_id, device_id="mac-do-daniel", display_name="Mac"
                )
            return None

    monkeypatch.setattr(devices_routes, "_db", _DB())
    return token


# ── credencial de aparelho ───────────────────────────────────────────────────

def test_token_de_aparelho_resolve_o_dono(client, monkeypatch, _modo):
    dono = uuid4()
    token = _device_fake(dono, monkeypatch)
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)

    r = client.get("/dono", headers={"X-Device-Token": token})
    assert r.status_code == 200
    assert r.json() == {"kind": "device", "user_id": str(dono)}


def test_token_de_aparelho_desconhecido_barra_no_enforce(client, monkeypatch, _modo):
    _device_fake(uuid4(), monkeypatch)
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)

    r = client.get("/dono", headers={"X-Device-Token": "nao-existe"})
    assert r.status_code == 401


def test_token_tambem_vale_na_query(client, monkeypatch, _modo):
    """O ``<a href download>`` e os WS não mandam header — daí o query param."""
    dono = uuid4()
    token = _device_fake(dono, monkeypatch)
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)

    r = client.get(f"/dono?token={token}")
    assert r.status_code == 200
    assert r.json()["user_id"] == str(dono)


# ── credencial de usuário ────────────────────────────────────────────────────

def test_jwt_de_usuario_nao_morre_na_verificacao(client, monkeypatch, _modo):
    """§7.1: a função antiga exigia ``sub == "neo-panel"`` para QUALQUER token."""
    dono = uuid4()
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    token, _ = jwt_mod.issue(str(dono), "access", 60, username="daniel")

    r = client.get("/dono", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"kind": "user", "user_id": str(dono)}


def test_access_token_expirado_e_recusado(client, monkeypatch, _modo):
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    token, _ = jwt_mod.issue(str(uuid4()), "access", -1)

    r = client.get("/dono", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_refresh_token_nao_serve_como_credencial(client, monkeypatch, _modo):
    """``typ`` importa: refresh é para renovar, não para chamar rota."""
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    token, _ = jwt_mod.issue(str(uuid4()), "refresh", 60)

    r = client.get("/dono", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_assinatura_falsificada_e_recusada(client, monkeypatch, _modo):
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    token, _ = jwt_mod.issue(str(uuid4()), "access", 60)
    cabeca, corpo, _assinatura = token.split(".")

    r = client.get("/dono", headers={"Authorization": f"Bearer {cabeca}.{corpo}.AAAA"})
    assert r.status_code == 401


# ── JWT estático do painel ───────────────────────────────────────────────────

def _token_do_painel() -> str:
    return jwt_mod.sign({"sub": get_settings().panel_jwt_subject, "role": "admin"})


def test_painel_autentica_mas_nao_tem_dono(client, monkeypatch, _modo):
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    cabecalho = {"Authorization": f"Bearer {_token_do_painel()}"}

    assert client.get("/qualquer", headers=cabecalho).status_code == 200
    # Dado pessoal exige dono, e o token do painel não diz quem é.
    assert client.get("/dono", headers=cabecalho).status_code == 403


def test_strict_recusa_o_painel_legado(client, monkeypatch, _modo):
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_STRICT, raising=False)
    cabecalho = {"Authorization": f"Bearer {_token_do_painel()}"}

    assert client.get("/qualquer", headers=cabecalho).status_code == 403


def test_header_x_panel_token_tambem_vale(client, monkeypatch, _modo):
    """§7.2: ``require_panel_jwt`` sempre aceitou este header."""
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)

    r = client.get("/qualquer", headers={"X-Panel-Token": _token_do_painel()})
    assert r.status_code == 200


# ── degraus ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("modo", [MODE_OFF, MODE_SHADOW])
def test_sem_credencial_passa_nos_degraus_iniciais(client, monkeypatch, _modo, modo):
    """A sombra não pode barrar nada: local é produção e o core roda --reload."""
    monkeypatch.setattr(_modo, "neo_api_auth_mode", modo, raising=False)

    r = client.get("/dono")
    assert r.status_code == 200
    assert r.json() == {"kind": "anonimo", "user_id": None}


def test_sem_credencial_barra_no_enforce(client, monkeypatch, _modo):
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)

    r = client.get("/dono")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_sombra_loga_o_que_barraria(client, monkeypatch, _modo, caplog):
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_SHADOW, raising=False)
    with caplog.at_level("WARNING"):
        client.get("/dono")
    assert any("auth:sombra" in m for m in caplog.messages)


# ── a amarração do user_id ───────────────────────────────────────────────────

def test_owner_of_recusa_dono_declarado_diferente(monkeypatch, _modo):
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    dono = uuid4()
    p = Principal(kind="device", user_id=dono, device_id="mac")

    with pytest.raises(HTTPException) as erro:
        owner_of(p, uuid4())
    assert erro.value.status_code == 403


def test_owner_of_ignora_declaracao_igual(monkeypatch, _modo):
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    dono = uuid4()
    p = Principal(kind="device", user_id=dono, device_id="mac")

    assert owner_of(p, dono) == dono
    assert owner_of(p, None) == dono


def test_owner_of_na_sombra_mantem_o_declarado(monkeypatch, _modo):
    """Sem isto a sombra mudaria comportamento — e deixaria de ser sombra."""
    monkeypatch.setattr(_modo, "neo_api_auth_mode", MODE_SHADOW, raising=False)
    outro = uuid4()
    p = Principal(kind="device", user_id=uuid4(), device_id="mac")

    assert owner_of(p, outro) == outro


def test_owner_of_rejeita_uuid_invalido(monkeypatch, _modo):
    p = Principal(kind="device", user_id=uuid4(), device_id="mac")
    with pytest.raises(HTTPException) as erro:
        owner_of(p, "não-é-uuid")
    assert erro.value.status_code == 400
