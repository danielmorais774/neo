"""Sessão a20f4722 (14/11): "O cofre está vazio — qual e-mail e senha eu uso?"

A conta do AppDemo ESTAVA no cofre, criada pela tarefa a2242336:
rótulo `sistema-exemplo.vercel.app · neo@example.com · Neo 12/11 18:16`,
campo `domain` com o site. O cérebro chamou `vault_list(query="appdemo")`. O filtro é
por texto, e "appdemo" não aparece no rótulo, na descrição nem nos campos. Veio
`count: 0`, e ele concluiu que o cofre estava vazio e pediu a senha no chat.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from core.browser_local.cofre import Cofre
from core.credentials.tools import VAULT_TOOLS, VaultToolExecutor
from shared import ToolCall

ROTULO = "sistema-exemplo.vercel.app · neo@example.com · Neo 12/11 18:16"


def _info(label, kind, fields, description=""):
    em = datetime(2026, 11, 12, 21, 16)
    return SimpleNamespace(
        id=label, label=label, kind=kind, fields=fields, description=description,
        created_at=em, updated_at=em,
    )


class _Store:
    def __init__(self, infos=()):
        self.infos = list(infos)

    async def list(self):
        return list(self.infos)

    async def create(self, *, label, secret, kind="token", description=None, fields=None):
        info = _info(label, kind, dict(fields or {}), description or "")
        self.infos.append(info)
        return info


def _cofre_de_prod() -> _Store:
    return _Store([
        _info(
            ROTULO, "password",
            {"username": "neo@example.com", "domain": "sistema-exemplo.vercel.app"},
            "Conta criada pelo Neo na tarefa do navegador a2242336 («Abrir e mapear formulário "
            "Google Forms»). Senha gerada pelo sistema — o modelo nunca a viu.",
        ),
        _info("rede-do-neo", "password", {"username": "neo@x", "domain": "conecta.example.com"}),
        _info("email-do-neo", "email", {"address": "neo@x"}),
    ])


async def _listar(store, **args):
    res = await VaultToolExecutor(store).execute_tool_call(
        ToolCall(id="1", name="vault_list", arguments=args)
    )
    return res.result


@pytest.mark.asyncio
async def test_filtro_que_nao_casa_nao_vira_cofre_vazio():
    r = await _listar(_cofre_de_prod(), query="appdemo")
    assert r["filtro_sem_resultado"] == "appdemo"
    assert ROTULO in [c["label"] for c in r["credentials"]]
    assert "NÃO está vazio" in r["note"]
    assert "DOMÍNIO" in r["note"]


@pytest.mark.asyncio
async def test_o_tipo_continua_valendo_quando_o_texto_nao_casa():
    r = await _listar(_cofre_de_prod(), query="appdemo", kind="password")
    assert {c["kind"] for c in r["credentials"]} == {"password"}
    assert r["count"] == 2


@pytest.mark.asyncio
async def test_filtro_que_casa_nao_traz_o_resto():
    r = await _listar(_cofre_de_prod(), query="vercel")
    assert [c["label"] for c in r["credentials"]] == [ROTULO]
    assert "filtro_sem_resultado" not in r


@pytest.mark.asyncio
async def test_cofre_vazio_de_verdade_continua_vazio():
    r = await _listar(_Store(), query="appdemo")
    assert r["count"] == 0
    assert "filtro_sem_resultado" not in r
    assert "NÃO está vazio" not in r["note"]


@pytest.mark.asyncio
async def test_a_nota_diz_que_o_navegador_local_acha_o_login_e_proibe_pedir_senha():
    r = await _listar(_cofre_de_prod())
    assert "browser_local_task" in r["note"] and "Nunca peça" in r["note"]
    [schema] = VAULT_TOOLS
    assert "browser_local_task" in schema.description and "Nunca peça senha" in schema.description


@pytest.mark.asyncio
async def test_conta_nova_leva_o_nome_do_site_e_a_busca_pelo_nome_acha():
    store = _Store()
    cofre = Cofre(store, job_id="a2242336", titulo="Abrir e mapear formulário Google Forms")
    await cofre.senha_nova(
        "sistema-exemplo.vercel.app", "neo@example.com", site="AppDemo"
    )
    [info] = store.infos
    assert info.description.startswith("Login do site «AppDemo» (sistema-exemplo.vercel.app).")
    r = await _listar(store, query="appdemo")
    assert "filtro_sem_resultado" not in r and r["count"] == 1


@pytest.mark.asyncio
async def test_sem_titulo_a_descricao_fica_como_antes():
    store = _Store()
    await Cofre(store, job_id="j").senha_nova("x.app", "neo@x", site="   ")
    assert store.infos[0].description.startswith("Conta criada pelo Neo")
