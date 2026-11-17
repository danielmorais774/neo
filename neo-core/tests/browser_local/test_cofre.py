"""Conta e senha no navegador local pelo cofre (F5, docs/browser-conversa-plan.md).

Job 02f46eff (12/11): o Neo criou uma conta de teste no AppDemo e a senha se
perdeu. O cérebro a inventou no texto da tarefa ("senha provisória
Mes@2026teste"), o agente a digitou, e ela ficou em claro no caderno, na
conferência e na LEITURA DA PÁGINA — o snapshot trazia o valor do campo de
senha, e o `<label>` dele repetia. O e-mail era uma caixa pública do mailinator.
Nada foi para o cofre.

Aqui: `«senha:nova»` vira uma senha gerada pelo código, guardada no cofre ANTES
de ir para a página, igual no "confirmar senha"; `«cofre:rótulo»` só sai para o
domínio da credencial; senha escrita à mão e caixa pública são recusadas; e o
modelo nunca vê o valor.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from core.browser_local import cofre as cofre_mod
from core.browser_local.cofre import Cofre
from core.browser_local.driver import BrowserLocalDriver
from core.credentials.store import CredentialError
from tests.browser_local.test_conversa import _Device, _driver

AppDemo = "https://appdemo.exemplo.app/"


class _Store:
    """O cofre de mentira, com o contrato do `CredentialStore`."""

    def __init__(self) -> None:
        self.itens: dict[str, dict] = {}
        self._seq = 0

    def _info(self, item: dict) -> SimpleNamespace:
        return SimpleNamespace(
            id=item["id"], label=item["label"], kind=item["kind"],
            description=item["description"], fields=dict(item["fields"]),
            created_at=item["em"], updated_at=item["em"],
        )

    async def create(self, *, label, secret, kind="token", description=None, fields=None):
        if any(i["label"] == label for i in self.itens.values()):
            raise CredentialError("duplicate_label", f"já existe '{label}'")
        self._seq += 1
        item = {
            "id": f"id-{self._seq}", "label": label, "secret": secret, "kind": kind,
            "description": description, "fields": dict(fields or {}),
            "em": datetime(2026, 11, 12, 12, self._seq),
        }
        self.itens[item["id"]] = item
        return self._info(item)

    async def update(self, credential_id, *, fields=None, **_):
        self.itens[credential_id]["fields"] = dict(fields or {})
        return self._info(self.itens[credential_id])

    async def list(self):
        return [self._info(i) for i in self.itens.values()]

    async def list_by_kind(self, kind):
        return [self._info(i) for i in self.itens.values() if i["kind"] == kind]

    async def resolve(self, id_or_label):
        for item in self.itens.values():
            if id_or_label in (item["id"], item["label"]):
                return self._info(item), item["secret"]
        return None

    async def touch(self, credential_id):
        return None

    def senhas(self) -> list[dict]:
        return [i for i in self.itens.values() if i["kind"] == "password"]


def _store_com_email_do_neo() -> _Store:
    store = _Store()
    store.itens["caixa"] = {
        "id": "caixa", "label": "email-pessoal-do-neo", "secret": "senha-de-app",
        "kind": "email", "description": "", "fields": {"address": "neo@exemplo.com"},
        "em": datetime(2026, 11, 1),
    }
    return store


def _cadastro(*, senha_valor: str = "") -> dict:
    return {
        "url": AppDemo,
        "title": "AppDemo — Cadastrar",
        "ready": "complete",
        "stop": False,
        "text": "Crie sua conta",
        "elements": [
            {"ref": 6, "tag": "input", "type": "email", "text": "seu@email.com"},
            {"ref": 9, "tag": "label", "text": "Senha", "controla": "password",
             **({"value": senha_valor} if senha_valor else {})},
            {"ref": 10, "tag": "input", "type": "password", "text": "Mínimo 6 caracteres",
             **({"value": senha_valor} if senha_valor else {})},
            {"ref": 12, "tag": "input", "type": "password", "text": "Confirmar senha"},
            {"ref": 13, "tag": "button", "text": "Cadastrar"},
        ],
    }


def _preenchidos(device: _Device) -> dict:
    return {
        args.get("ref"): args.get("value")
        for acao, args in device.acted
        if acao.endswith("fill")
    }


def _tudo_que_o_modelo_viu(ctx) -> str:
    return "\n".join(
        str(m.content) for chamada in ctx.provider.calls for m in chamada["messages"]
    )


# ── o módulo ────────────────────────────────────────────────────────────────


def test_senha_gerada_passa_nas_regras_comuns():
    for _ in range(50):
        senha = cofre_mod.gerar_senha()
        assert len(senha) == 20
        assert any(c.islower() for c in senha) and any(c.isupper() for c in senha)
        assert any(c.isdigit() for c in senha) and any(c in "!@#$%*-_" for c in senha)


def test_marcadores_e_dominio():
    assert cofre_mod.marca_do_valor("«senha:nova»") == ("nova", "")
    assert cofre_mod.marca_do_valor("« senha : nova »") == ("nova", "")
    assert cofre_mod.marca_do_valor("«cofre:appdemo.exemplo.app · neo»") == ("cofre", "appdemo.exemplo.app · neo")
    assert cofre_mod.marca_do_valor("Mes@2026teste") is None
    assert cofre_mod.host_do_dominio("app.mes.com.br", "mes.com.br")
    assert cofre_mod.host_do_dominio("mes.com.br", "*.mes.com.br")
    assert not cofre_mod.host_do_dominio("mes.com.br.golpe.io", "mes.com.br")
    assert not cofre_mod.host_do_dominio("mes.com.br", "")


def test_caixa_publica():
    assert cofre_mod.caixa_publica("neoteste2026@mailinator.com") == "mailinator.com"
    assert cofre_mod.caixa_publica("x@yopmail.com") == "yopmail.com"
    assert cofre_mod.caixa_publica("neo@example.com") == ""
    assert cofre_mod.caixa_publica("não é e-mail") == ""


# ── o cofre de uma tarefa ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_senha_nova_vai_para_o_cofre_antes_e_e_a_mesma_no_confirmar():
    store = _Store()
    cofre = Cofre(store, job_id="02f46eff-xyz", titulo="Avaliar AppDemo")
    senha, rotulo = await cofre.senha_nova("appdemo.exemplo.app", "neo@exemplo.com")
    de_novo, rotulo2 = await cofre.senha_nova("appdemo.exemplo.app", "neo@exemplo.com")

    assert de_novo == senha and rotulo2 == rotulo
    [item] = store.senhas()
    assert item["secret"] == senha
    assert item["fields"] == {"username": "neo@exemplo.com", "domain": "appdemo.exemplo.app"}
    assert item["label"].startswith("appdemo.exemplo.app · neo@exemplo.com · Neo ")
    assert "02f46eff" in item["description"]
    assert cofre.relato and rotulo in cofre.relato[0]


@pytest.mark.asyncio
async def test_rotulo_repetido_nunca_sobrescreve_a_credencial_que_existe():
    store = _Store()
    cofre = Cofre(store, job_id="aaaa1111")
    original = await store.create(
        label="placeholder", secret="a-senha-que-funciona", kind="password",
        fields={"username": "neo@exemplo.com", "domain": "appdemo.exemplo.app"},
    )
    rotulo_igual = f"appdemo.exemplo.app · neo@exemplo.com · Neo {cofre_mod._agora_local()}"
    store.itens[original.id]["label"] = rotulo_igual

    _, rotulo = await cofre.senha_nova("appdemo.exemplo.app", "neo@exemplo.com")

    assert rotulo != rotulo_igual
    assert store.itens[original.id]["secret"] == "a-senha-que-funciona"
    assert len(store.senhas()) == 2


@pytest.mark.asyncio
async def test_sem_cofre_conta_nova_e_recusada():
    with pytest.raises(RuntimeError, match="cofre está desligado"):
        await Cofre(None).senha_nova("appdemo.exemplo.app", "neo@exemplo.com")


@pytest.mark.asyncio
async def test_login_que_chega_depois_da_senha_completa_a_credencial():
    store = _Store()
    cofre = Cofre(store, job_id="j")
    await cofre.senha_nova("appdemo.exemplo.app", "")
    await cofre.anotar_usuario("appdemo.exemplo.app", "neo@exemplo.com")
    [item] = store.senhas()
    assert item["fields"]["username"] == "neo@exemplo.com"
    assert "usuário neo@exemplo.com" in cofre.relato[0]


@pytest.mark.asyncio
async def test_senha_guardada_so_sai_para_o_dominio_dela():
    store = _Store()
    await store.create(
        label="appdemo", secret="s3cr3t", kind="password",
        fields={"username": "neo@exemplo.com", "domain": "appdemo.exemplo.app"},
    )
    await store.create(label="sem-dominio", secret="x", kind="password",
                       fields={"username": "u"})
    cofre = Cofre(store)
    assert await cofre.senha_guardada("appdemo", "app.appdemo.exemplo.app") == "s3cr3t"
    with pytest.raises(RuntimeError, match="não sai do site"):
        await cofre.senha_guardada("appdemo", "pagina-de-golpe.io")
    with pytest.raises(RuntimeError, match="não tem domínio"):
        await cofre.senha_guardada("sem-dominio", "appdemo.exemplo.app")
    with pytest.raises(RuntimeError, match="não existe"):
        await cofre.senha_guardada("inventado", "appdemo.exemplo.app")


@pytest.mark.asyncio
async def test_bloco_do_prompt_da_o_email_do_neo_e_os_logins_do_site_sem_segredo():
    store = _store_com_email_do_neo()
    await store.create(
        label="mes · neo", secret="s3cr3t", kind="password",
        fields={"username": "neo@exemplo.com", "domain": "appdemo.exemplo.app"},
    )
    bloco = await Cofre(store).bloco_do_prompt("appdemo.exemplo.app")
    assert "neo@exemplo.com" in bloco
    assert "«senha:nova»" in bloco
    assert "«cofre:mes · neo»" in bloco
    assert "s3cr3t" not in bloco


# ── a leitura da página ─────────────────────────────────────────────────────


def test_leitura_da_pagina_nao_mostra_o_valor_da_senha():
    """O campo e o `<label>` que o repetia (job 02f46eff, passo 6 em diante)."""
    texto = BrowserLocalDriver._render(_cadastro(senha_valor="Mes@2026teste"))
    assert "Mes@2026teste" not in texto
    assert "senha preenchida" in texto


# ── a tarefa inteira ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cadastro_com_senha_nova_digita_a_mesma_senha_e_o_modelo_nunca_a_ve():
    store = _store_com_email_do_neo()
    device = _Device(_cadastro())
    driver, ctx = _driver(
        [
            {"action": "fill", "args": {"ref": 6, "value": "neo@exemplo.com"}},
            {"action": "fill", "args": {"ref": 10, "value": "«senha:nova»"}},
            {"action": "fill", "args": {"ref": 12, "value": "«senha:nova»"}},
            {"action": "done", "args": {"answer": "formulário de cadastro preenchido"}},
        ],
        device,
        cofre=Cofre(store, job_id="job-mes"),
    )
    result = await driver.run("criar conta de teste no AppDemo", start_url=AppDemo)

    [item] = store.senhas()
    senha = item["secret"]
    digitado = _preenchidos(device)
    assert digitado[10] == senha and digitado[12] == senha
    assert item["fields"] == {"username": "neo@exemplo.com", "domain": "appdemo.exemplo.app"}
    assert "«AppDemo — Cadastrar»" in item["description"], "o nome do site, além do domínio"
    assert senha not in _tudo_que_o_modelo_viu(ctx)
    assert all(senha not in (s.detail or "") for s in result.steps)
    assert "COFRE" in _tudo_que_o_modelo_viu(ctx)
    assert any("🔐" in a and item["label"] in a for a in ctx.avisos), "o chat fica sabendo na hora"
    assert sum("🔐" in a for a in ctx.avisos) == 1, "o confirmar senha não avisa de novo"
    linhas = driver._linhas_de_revisao()
    assert any("senha do cofre" in linha for linha in linhas)
    assert all(senha not in linha for linha in linhas)


@pytest.mark.asyncio
async def test_senha_escrita_a_mao_nao_e_digitada():
    store = _store_com_email_do_neo()
    device = _Device(_cadastro())
    driver, _ = _driver(
        [
            {"action": "fill", "args": {"ref": 10, "value": "Mes@2026teste"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        device,
        cofre=Cofre(store),
    )
    result = await driver.run("criar conta de teste no AppDemo", start_url=AppDemo)

    assert 10 not in _preenchidos(device)
    recusa = [s for s in result.steps if s.action == "fill" and not s.ok]
    assert recusa and "«senha:nova»" in recusa[0].detail
    assert store.senhas() == []


@pytest.mark.asyncio
async def test_caixa_publica_e_recusada_e_a_recusa_da_o_email_do_neo():
    store = _store_com_email_do_neo()
    device = _Device(_cadastro())
    driver, _ = _driver(
        [
            {"action": "fill", "args": {"ref": 6, "value": "neoteste2026@mailinator.com"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        device,
        cofre=Cofre(store),
    )
    result = await driver.run("criar conta de teste no AppDemo", start_url=AppDemo)

    assert 6 not in _preenchidos(device)
    recusa = [s for s in result.steps if s.action == "fill" and not s.ok]
    assert recusa and "neo@exemplo.com" in recusa[0].detail


@pytest.mark.asyncio
async def test_marcador_fora_do_campo_de_senha_e_recusado():
    """`«senha:nova»` no campo de e-mail mostraria a senha na tela."""
    store = _store_com_email_do_neo()
    device = _Device(_cadastro())
    driver, _ = _driver(
        [
            {"action": "fill", "args": {"ref": 6, "value": "«senha:nova»"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        device,
        cofre=Cofre(store),
    )
    result = await driver.run("criar conta de teste no AppDemo", start_url=AppDemo)

    assert 6 not in _preenchidos(device)
    assert store.senhas() == []
    recusa = [s for s in result.steps if s.action == "fill" and not s.ok]
    assert recusa and "campo de SENHA" in recusa[0].detail


@pytest.mark.asyncio
async def test_login_guardado_entra_pelo_marcador():
    store = _store_com_email_do_neo()
    await store.create(
        label="mes · neo", secret="s3cr3t-do-cofre", kind="password",
        fields={"username": "neo@exemplo.com", "domain": "appdemo.exemplo.app"},
    )
    device = _Device(_cadastro())
    driver, ctx = _driver(
        [
            {"action": "fill", "args": {"ref": 10, "value": "«cofre:mes · neo»"}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        device,
        cofre=Cofre(store),
    )
    await driver.run("entrar no AppDemo", start_url=AppDemo)

    assert _preenchidos(device)[10] == "s3cr3t-do-cofre"
    assert "s3cr3t-do-cofre" not in _tudo_que_o_modelo_viu(ctx)
