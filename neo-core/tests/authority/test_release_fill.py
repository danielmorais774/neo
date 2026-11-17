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
async def test_uso_unico_outro_job_nao_reusa_a_liberacao():
    """Uso único é por TAREFA: o filler nasce por job, e o job seguinte não herda."""
    servico = _Service([_Row(["pks:cep"], "loja.com.br")])
    user = uuid4()
    deste_job = ReleaseFiller(servico, _PKS())
    do_proximo = ReleaseFiller(servico, _PKS())
    primeira = await deste_job.resolve(user_id=user, texto="«pks:cep»", url="https://loja.com.br/a")
    segunda = await do_proximo.resolve(user_id=user, texto="«pks:cep»", url="https://loja.com.br/b")
    assert primeira.ok and not segunda.ok
    assert segunda.sem_liberacao == ["cep"]


@pytest.mark.asyncio
async def test_uma_aprovacao_preenche_todos_os_campos_do_formulario():
    """Sessão 1a605544: aprovar CEP+CPF e ver o CPF recusado porque o CEP já
    tinha gastado a liberação seria pedir duas vezes a mesma coisa."""
    servico = _Service([_Row(["pks:cep", "pks:cpf"], "docs.google.com")])
    f = ReleaseFiller(servico, _PKS({"cep": "72630-123", "cpf": "52998224725"}))
    user = uuid4()
    url = "https://docs.google.com/forms/d/e/x/viewform"
    cep = await f.resolve(user_id=user, texto="«pks:cep»", url=url)
    cpf = await f.resolve(user_id=user, texto="«pks:cpf»", url=url)
    assert cep.texto == "72630-123" and cpf.texto == "52998224725"
    assert len(servico.consumidas) == 1  # gasta UMA vez, no primeiro campo


@pytest.mark.asyncio
async def test_a_aprovacao_em_uso_nao_vale_em_outro_dominio():
    servico = _Service([_Row(["pks:cep", "pks:cpf"], "docs.google.com")])
    f = ReleaseFiller(servico, _PKS({"cep": "72630-123", "cpf": "52998224725"}))
    user = uuid4()
    await f.resolve(user_id=user, texto="«pks:cep»", url="https://docs.google.com/forms/x")
    fora = await f.resolve(user_id=user, texto="«pks:cpf»", url="https://atacante.tld/forms")
    assert not fora.ok and "52998224725" not in fora.texto


@pytest.mark.asyncio
async def test_sem_liberacao_separa_o_que_o_daniel_resolve_com_um_clique():
    """Só a falta de aprovação vira pedido; memória vazia não se resolve aprovando."""
    sem_aprovacao = await _filler([]).resolve(
        user_id=uuid4(), texto="«pks:cep»", url="https://loja.com.br/a"
    )
    assert sem_aprovacao.sem_liberacao == ["cep"]
    sem_memoria = await _filler([_Row(["pks:cep"], "loja.com.br")], props={}).resolve(
        user_id=uuid4(), texto="«pks:cep»", url="https://loja.com.br/a"
    )
    assert not sem_memoria.ok and sem_memoria.sem_liberacao == []


@pytest.mark.asyncio
async def test_nome_acha_full_name_na_memoria():
    """O formulário pergunta "nome"; a memória de produção guarda `full_name`."""
    f = _filler([_Row(["pks:nome"], "docs.google.com")], props={"full_name": "Daniel Morais"})
    out = await f.resolve(user_id=uuid4(), texto="«pks:nome»", url="https://docs.google.com/f")
    assert out.ok and out.texto == "Daniel Morais"


@pytest.mark.asyncio
async def test_tem_liberacao_e_disponivel():
    f = _filler([_Row(["pks:cep"], "loja.com.br")], props={"cep": "01234-567"})
    user = uuid4()
    assert await f.tem_liberacao(user_id=user, campo="cep", url="https://www.loja.com.br/x")
    assert not await f.tem_liberacao(user_id=user, campo="cpf", url="https://loja.com.br/x")
    assert not await f.tem_liberacao(user_id=user, campo="cep", url="")
    assert await f.disponivel(user_id=user, campo="cep") is None
    assert "não está na memória" in (await f.disponivel(user_id=user, campo="cpf") or "")


def test_campos_do_texto_em_ordem_e_sem_repetir():
    from core.release import campos_do_texto

    texto = "preencha «pks:nome», «pks:endereco», «pks:cep» e «pks:CPF» (de novo «pks:cep»)"
    assert campos_do_texto(texto) == ["nome", "endereco", "cep", "cpf"]
    assert campos_do_texto("nada aqui") == []


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
