"""Preenchimento escopado por domínio (F3b do release-request-plan)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.release import ReleaseFiller, domain_matches, host_of
from core.release.fill import PLACEHOLDER_RE


class _Row:
    def __init__(self, campos, destino, rid=None):
        self.id = rid or uuid4()
        self.campos = campos
        self.destino = destino


class _Service:
    def __init__(self, aprovadas):
        self._aprovadas = list(aprovadas)
        self.consumidas: list = []

    async def list_approved(self, user_id):
        return [r for r in self._aprovadas if r.id not in self.consumidas]

    async def consume(self, rid, payload=None):
        if rid in self.consumidas:
            raise RuntimeError("liberação já usada")
        self.consumidas.append(rid)
        return None


class _PKS:
    def __init__(self, props=None):
        self.props = props if props is not None else {"cep": "01234-567"}
        self.pedidos: list[bool] = []

    async def search_entities(self, *, user_id, query, redact=True):
        self.pedidos.append(redact)
        return [{"name": "Daniel", "properties": dict(self.props)}]


def _filler(aprovadas, props=None):
    return ReleaseFiller(_Service(aprovadas), _PKS(props))


# ── casamento de domínio ─────────────────────────────────────────────────

def test_apex_e_subdominio_casam_o_resto_nao():
    assert domain_matches("loja.com.br", "loja.com.br")
    assert domain_matches("loja.com.br", "www.loja.com.br")
    assert not domain_matches("loja.com.br", "outraloja.com.br")
    # o sufixo tem que vir depois de um ponto, senão `loja.com.br.atacante.tld`
    # colheria o dado liberado para a loja
    assert not domain_matches("loja.com.br", "loja.com.br.atacante.tld")
    assert not domain_matches("", "loja.com.br")


def test_host_of():
    assert host_of("https://www.Loja.com.br/frete?cep=1") == "www.loja.com.br"
    assert host_of("não é url") == ""


def test_regex_pega_as_duas_formas():
    assert PLACEHOLDER_RE.findall("«pks:cep» e <<pks:cpf>>") == ["cep", "cpf"]


# ── substituição ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_liberado_para_o_dominio_preenche():
    f = _filler([_Row(["pks:cep"], "loja.com.br")])
    out = await f.resolve(
        user_id=uuid4(), texto="«pks:cep»", url="https://www.loja.com.br/frete"
    )
    assert out.texto == "01234-567" and out.ok and out.substituidos == ["cep"]


@pytest.mark.asyncio
async def test_outro_dominio_nao_recebe_nada():
    """A liberação é para a loja; a página hostil não colhe."""
    f = _filler([_Row(["pks:cep"], "loja.com.br")])
    out = await f.resolve(
        user_id=uuid4(), texto="«pks:cep»", url="https://atacante.tld/form"
    )
    assert out.texto == "«pks:cep»" and not out.ok
    assert "não existe liberação aprovada" in out.motivos[0]


@pytest.mark.asyncio
async def test_campo_nao_liberado_nao_sai_nem_no_dominio_certo():
    """Liberou o CEP; a página pede CPF. Não há valor para preencher."""
    f = _filler([_Row(["pks:cep"], "loja.com.br")], props={"cep": "01234-567", "cpf": "123"})
    out = await f.resolve(
        user_id=uuid4(), texto="«pks:cpf»", url="https://loja.com.br/checkout"
    )
    assert "123" not in out.texto and not out.ok


@pytest.mark.asyncio
async def test_uso_unico_a_segunda_vez_nao_preenche():
    f = _filler([_Row(["pks:cep"], "loja.com.br")])
    user = uuid4()
    primeira = await f.resolve(user_id=user, texto="«pks:cep»", url="https://loja.com.br/a")
    segunda = await f.resolve(user_id=user, texto="«pks:cep»", url="https://loja.com.br/b")
    assert primeira.ok and not segunda.ok


@pytest.mark.asyncio
async def test_sem_aba_nao_substitui():
    f = _filler([_Row(["pks:cep"], "loja.com.br")])
    out = await f.resolve(user_id=uuid4(), texto="«pks:cep»", url="")
    assert not out.ok and "sem domínio" in out.motivos[0]


@pytest.mark.asyncio
async def test_valor_e_buscado_sem_redacao():
    f = _filler([_Row(["pks:cep"], "loja.com.br")])
    await f.resolve(user_id=uuid4(), texto="«pks:cep»", url="https://loja.com.br/a")
    assert f._pks.pedidos == [False]  # noqa: SLF001


@pytest.mark.asyncio
async def test_campo_ambiguo_recusa_em_vez_de_chutar():
    class _PKSDois(_PKS):
        async def search_entities(self, *, user_id, query, redact=True):
            return [
                {"name": "Daniel", "properties": {"email": "a@x.com"}},
                {"name": "E-mail do Neo", "properties": {"email": "b@x.com"}},
            ]

    f = ReleaseFiller(_Service([_Row(["pks:email"], "loja.com.br")]), _PKSDois())
    out = await f.resolve(
        user_id=uuid4(), texto="«pks:email»", url="https://loja.com.br/cadastro"
    )
    assert not out.ok and "mais de um lugar" in out.motivos[0]
    assert "a@x.com" not in out.texto and "b@x.com" not in out.texto


@pytest.mark.asyncio
async def test_texto_sem_placeholder_passa_intacto():
    f = _filler([])
    out = await f.resolve(user_id=uuid4(), texto="cortina azul", url="https://loja.com.br")
    assert out.texto == "cortina azul" and not out.tem_placeholder


@pytest.mark.asyncio
async def test_valor_nao_existe_na_memoria():
    f = _filler([_Row(["pks:cep"], "loja.com.br")], props={})
    out = await f.resolve(user_id=uuid4(), texto="«pks:cep»", url="https://loja.com.br/a")
    assert not out.ok and "não está na memória" in out.motivos[0]
