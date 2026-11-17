"""Login de usuário: senha, hash legado, rotação de refresh (api-auth-plan §5).

O ponto mais fácil de errar aqui é a **migração do hash**: a senha em claro só
existe no instante do login, então é ali ou nunca. Se este teste cair, o formato
legado (SHA-256 com salt fixo, vindo de ``db/seeds.py``) fica para sempre.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import passwords, throttle
from api.routes import auth as auth_route
from shared.config import get_settings


class _FakeDB:
    """Banco mínimo: um usuário e os refresh tokens em memória."""

    def __init__(self, user):
        self.user = user
        self.refresh: dict[str, SimpleNamespace] = {}
        self.hash_gravado: str | None = None
        self.revogados = 0

    async def get_user_by_username(self, username):
        return self.user if self.user and self.user.username == username else None

    async def get_user_by_id(self, user_id):
        return self.user if self.user and self.user.id == user_id else None

    async def set_user_password_hash(self, user_id, password_hash):
        self.hash_gravado = password_hash
        self.user.password_hash = password_hash

    async def create_refresh_token(self, *, user_id, token_hash, expires_at, origem=""):
        row = SimpleNamespace(
            id=uuid4(), user_id=user_id, token_hash=token_hash,
            expires_at=expires_at, revoked_at=None, origem=origem,
        )
        self.refresh[token_hash] = row
        return row

    async def rotate_refresh_token(self, *, antigo_hash, novo_hash, expires_at, origem=""):
        antigo = self.refresh.get(antigo_hash)
        if antigo is None or antigo.revoked_at is not None or antigo.expires_at < datetime.utcnow():
            return None
        antigo.revoked_at = datetime.utcnow()
        return await self.create_refresh_token(
            user_id=antigo.user_id, token_hash=novo_hash, expires_at=expires_at, origem=origem
        )

    async def revoke_refresh_token(self, token_hash):
        row = self.refresh.get(token_hash)
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = datetime.utcnow()
        return True

    async def revoke_all_refresh_tokens(self, user_id):
        self.revogados += 1
        return 1


@pytest.fixture(autouse=True)
def _limpa_contador():
    throttle._reset_for_tests()
    yield
    throttle._reset_for_tests()


@pytest.fixture
def usuario():
    return SimpleNamespace(
        id=uuid4(), username="daniel", display_name="Daniel Morais",
        email="daniel@neo.dev", password_hash=passwords.hash_password("senha-boa-de-teste"),
    )


@pytest.fixture
def client(usuario, monkeypatch):
    db = _FakeDB(usuario)
    auth_route.set_db(db)
    app = FastAPI()
    app.include_router(auth_route.router)
    c = TestClient(app, raise_server_exceptions=False)
    c.db = db  # type: ignore[attr-defined]
    yield c
    auth_route.set_db(None)  # type: ignore[arg-type]


def test_login_devolve_par_de_tokens(client):
    r = client.post("/api/v1/auth/login", json={"username": "daniel", "password": "senha-boa-de-teste"})
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["access_token"] and corpo["refresh_token"]
    assert corpo["user"]["username"] == "daniel"
    assert corpo["expires_in"] == get_settings().access_token_ttl_s


def test_senha_errada_e_usuario_inexistente_respondem_igual(client):
    errada = client.post("/api/v1/auth/login", json={"username": "daniel", "password": "xxxxxxxxxx"})
    inexistente = client.post("/api/v1/auth/login", json={"username": "ninguem", "password": "xxxxxxxxxx"})

    assert errada.status_code == inexistente.status_code == 401
    # Mesma mensagem: qualquer diferença aqui enumera contas.
    assert errada.json()["detail"] == inexistente.json()["detail"]


def test_hash_legado_autentica_e_migra(client, usuario):
    """O formato de ``db/seeds.py``: sha256("neo-salt" + senha), sem salt por conta."""
    usuario.password_hash = hashlib.sha256(b"neo-salt123456").hexdigest()

    r = client.post("/api/v1/auth/login", json={"username": "daniel", "password": "123456"})
    assert r.status_code == 200
    assert client.db.hash_gravado.startswith("pbkdf2_sha256$")
    assert passwords.verify_password("123456", client.db.hash_gravado)


def test_usuario_sem_senha_nao_loga(client, usuario):
    usuario.password_hash = None
    r = client.post("/api/v1/auth/login", json={"username": "daniel", "password": "qualquer"})
    assert r.status_code == 401


def test_tentativas_demais_travam(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_max_attempts", 3, raising=False)
    for _ in range(3):
        client.post("/api/v1/auth/login", json={"username": "daniel", "password": "errada1234"})

    r = client.post("/api/v1/auth/login", json={"username": "daniel", "password": "errada1234"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_login_certo_zera_o_contador(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_max_attempts", 3, raising=False)
    client.post("/api/v1/auth/login", json={"username": "daniel", "password": "errada1234"})
    client.post("/api/v1/auth/login", json={"username": "daniel", "password": "errada1234"})
    assert client.post(
        "/api/v1/auth/login", json={"username": "daniel", "password": "senha-boa-de-teste"}
    ).status_code == 200

    # Se o contador não zerasse, o dono se trancaria sozinho depois de errar 2x.
    for _ in range(2):
        client.post("/api/v1/auth/login", json={"username": "daniel", "password": "errada1234"})
    assert client.post(
        "/api/v1/auth/login", json={"username": "daniel", "password": "senha-boa-de-teste"}
    ).status_code == 200


def test_refresh_rotaciona_e_queima_o_antigo(client):
    login = client.post(
        "/api/v1/auth/login", json={"username": "daniel", "password": "senha-boa-de-teste"}
    ).json()

    primeiro = client.post("/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})
    assert primeiro.status_code == 200
    assert primeiro.json()["refresh_token"] != login["refresh_token"]

    # Reapresentar o antigo é sinal de token copiado: 401, não par novo.
    segundo = client.post("/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})
    assert segundo.status_code == 401


def test_logout_revoga_o_refresh(client):
    login = client.post(
        "/api/v1/auth/login", json={"username": "daniel", "password": "senha-boa-de-teste"}
    ).json()

    assert client.post("/api/v1/auth/logout", json={"refresh_token": login["refresh_token"]}).json()["revogado"]
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]}
    ).status_code == 401


def test_me_recusa_token_invalido_mesmo_em_sombra(client, monkeypatch):
    """A sombra não pode transformar "quem sou eu" num "sim".

    Medido contra produção em 2026-09-26: `Bearer nao.e.token` respondia 200 com
    `user: null`, porque em `shadow` o portão deixa passar. Nesta rota a
    identidade É a resposta.
    """
    from api.auth.principal import MODE_SHADOW

    monkeypatch.setattr(get_settings(), "neo_api_auth_mode", MODE_SHADOW, raising=False)

    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer nao.e.token"}
    ).status_code == 401


def test_me_identifica_quem_chamou(client, usuario, monkeypatch):
    from api.auth.principal import MODE_ENFORCE

    monkeypatch.setattr(get_settings(), "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    login = client.post(
        "/api/v1/auth/login", json={"username": "daniel", "password": "senha-boa-de-teste"}
    ).json()

    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {login['access_token']}"})
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["principal"]["kind"] == "user"
    assert corpo["user"]["id"] == str(usuario.id)
    assert corpo["auth_mode"] == MODE_ENFORCE
