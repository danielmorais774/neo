"""O app REAL barra as rotas do plano quando o portão está em `enforce`.

Diferente de ``test_principal.py``, que monta um app de brinquedo: aqui o alvo é
``api.main:app`` com a fiação de verdade (``include_router(..., dependencies=...)``).
É o teste que pega o erro clássico de montar a dependência no router errado — o
código passa, os unitários passam, e a rota continua aberta.

Sem banco: a dependência resolve ANTES do corpo da rota, então o 401 sai sem
tocar em Postgres. Onde a credencial é aceita, o teste para no 503/500 do serviço
não inicializado — o que já prova que passou do portão.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.auth.principal import MODE_ENFORCE, MODE_SHADOW
from api.main import app
from shared.config import get_settings

#: As rotas que o Daniel listou, uma por linha, com um corpo qualquer.
ROTAS = [
    ("post", "/api/v1/desktop/chat", {"json": {"type": "user_input", "user_message": "oi"}}),
    ("post", "/api/v1/desktop/chat/stream", {"json": {"type": "user_input", "user_message": "oi"}}),
    ("post", "/api/v1/desktop/execute", {"json": {"task": "oi"}}),
    (
        "post",
        "/api/v1/desktop/chat/command-response",
        {
            "json": {
                "session_id": "00000000-0000-0000-0000-000000000001",
                "command_id": "c1",
                "command": "ls",
                "approved": True,
            }
        },
    ),
    ("get", "/api/v1/desktop/sessions", {}),
    ("get", "/api/v1/desktop/session/00000000-0000-0000-0000-000000000001", {}),
    ("get", "/api/v1/desktop/session/00000000-0000-0000-0000-000000000001/history", {}),
    ("delete", "/api/v1/desktop/session/00000000-0000-0000-0000-000000000001", {}),
    ("get", "/api/v1/desktop/jobs", {}),
    ("get", "/api/v1/desktop/jobs/00000000-0000-0000-0000-000000000001", {}),
    ("get", "/api/v1/desktop/jobs/00000000-0000-0000-0000-000000000001/logs", {}),
    ("post", "/api/v1/desktop/jobs/00000000-0000-0000-0000-000000000001/cancel", {}),
    ("get", "/api/v1/desktop/working-memory/notes", {}),
    ("get", "/api/v1/desktop/working-memory/notes/alguma", {}),
    ("get", "/api/v1/desktop/tools", {}),
    (
        "post",
        "/api/v1/context/retrieve",
        {"json": {"user_id": "00000000-0000-0000-0000-000000000001", "user_input": "oi"}},
    ),
    (
        "post",
        "/api/v1/context/prompt-preview",
        {"json": {"user_id": "00000000-0000-0000-0000-000000000001", "user_input": "oi"}},
    ),
    ("post", "/api/v1/quick-help/ask", {"json": {"question": "oi"}}),
    ("get", "/api/v1/files", {}),
    ("get", "/api/v1/files/usage", {}),
]


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("metodo,caminho,kwargs", ROTAS, ids=[f"{m.upper()} {c}" for m, c, _ in ROTAS])
def test_enforce_barra_sem_credencial(client, monkeypatch, metodo, caminho, kwargs):
    monkeypatch.setattr(get_settings(), "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    resposta = getattr(client, metodo)(caminho, **kwargs)
    assert resposta.status_code == 401, (
        f"{metodo.upper()} {caminho} respondeu {resposta.status_code} sem credencial — "
        "essa rota está aberta"
    )


def test_upload_de_arquivo_tambem_barra(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    resposta = client.post(
        "/api/v1/files",
        files={"file": ("a.txt", b"oi", "text/plain")},
        data={"retention": "temp"},
    )
    assert resposta.status_code == 401


def test_download_sem_assinatura_barra(client, monkeypatch):
    """O link assinado é a ÚNICA alternativa ao header — sem os dois, 401."""
    monkeypatch.setattr(get_settings(), "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    resposta = client.get("/api/v1/files/00000000-0000-0000-0000-000000000001/download")
    assert resposta.status_code == 401


def test_download_com_assinatura_passa_do_portao(client, monkeypatch):
    from uuid import uuid4

    from api.auth import signed_url

    monkeypatch.setattr(get_settings(), "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    file_id = "00000000-0000-0000-0000-000000000001"
    params = signed_url.sign(f"file:{file_id}", uuid4())

    resposta = client.get(f"/api/v1/files/{file_id}/download", params=params)
    # Passou do portão: o que barra agora é o store não inicializado (503) ou o
    # arquivo inexistente (404) — nunca 401.
    assert resposta.status_code != 401


def test_assinatura_de_um_arquivo_nao_serve_para_outro(client, monkeypatch):
    from uuid import uuid4

    from api.auth import signed_url

    monkeypatch.setattr(get_settings(), "neo_api_auth_mode", MODE_ENFORCE, raising=False)
    params = signed_url.sign("file:00000000-0000-0000-0000-00000000000a", uuid4())

    resposta = client.get(
        "/api/v1/files/00000000-0000-0000-0000-00000000000b/download", params=params
    )
    assert resposta.status_code == 401


@pytest.mark.parametrize("metodo,caminho,kwargs", ROTAS[:6], ids=[f"{m.upper()} {c}" for m, c, _ in ROTAS[:6]])
def test_sombra_nao_barra_nada(client, monkeypatch, metodo, caminho, kwargs):
    """O degrau que existe para a virada de chave ser reversível."""
    monkeypatch.setattr(get_settings(), "neo_api_auth_mode", MODE_SHADOW, raising=False)
    resposta = getattr(client, metodo)(caminho, **kwargs)
    assert resposta.status_code != 401
