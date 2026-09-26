"""`pks_reveal` — a válvula da redação (docs/release-request-plan.md §7, F2)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from modules.pks_search import PKSSearchExecutor


class _PKSFalso:
    """Devolve sempre a mesma entidade; registra se pediram sem redação."""

    def __init__(self) -> None:
        self.chamadas: list[bool] = []

    async def search_entities(self, *, user_id, query, redact: bool = True):
        self.chamadas.append(redact)
        props = {"cep": "01234-567", "cpf": "123.456.789-00", "senha": "hunter2"}
        if redact:
            from core.pks.sensitivity import redact_properties

            props = redact_properties(props)
        return [{"name": "Daniel", "entity_type": "person", "properties": props}]


@pytest.fixture()
def executor() -> PKSSearchExecutor:
    ex = PKSSearchExecutor(_PKSFalso())
    ex.set_context(uuid4())
    return ex


@pytest.mark.asyncio
async def test_revela_um_campo_e_so_ele(executor):
    saida = await executor.execute("pks_reveal", {"entidade": "Daniel", "campo": "cep"})
    assert saida == {"found": True, "entidade": "Daniel", "campo": "cep", "valor": "01234-567"}
    # o CPF estava na mesma entidade e NÃO veio junto
    assert "123.456.789-00" not in str(saida)


@pytest.mark.asyncio
async def test_pede_sem_redacao_por_baixo(executor):
    """Se pedisse redigido, devolveria o próprio endereço no lugar do valor."""
    await executor.execute("pks_reveal", {"entidade": "Daniel", "campo": "cep"})
    assert executor.pks.chamadas == [False]


@pytest.mark.asyncio
async def test_aceita_o_campo_escrito_como_no_prompt(executor):
    """O modelo lê `«pks:cep»` no bloco e tende a repetir do jeito que viu."""
    saida = await executor.execute("pks_reveal", {"entidade": "Daniel", "campo": "«pks:cep»"})
    assert saida["found"] and saida["campo"] == "cep"


@pytest.mark.asyncio
async def test_credencial_nao_sai_nem_por_aqui(executor):
    saida = await executor.execute("pks_reveal", {"entidade": "Daniel", "campo": "senha"})
    assert saida["found"] is False
    assert "hunter2" not in str(saida)
    assert "cofre" in saida["motivo"]


@pytest.mark.asyncio
async def test_campo_inexistente_explica_em_vez_de_inventar(executor):
    saida = await executor.execute("pks_reveal", {"entidade": "Daniel", "campo": "rg"})
    assert saida["found"] is False and "search_pks" in saida["motivo"]


@pytest.mark.asyncio
async def test_exige_entidade_e_campo(executor):
    with pytest.raises(ValueError):
        await executor.execute("pks_reveal", {"entidade": "", "campo": "cep"})


@pytest.mark.asyncio
async def test_sem_usuario_recusa():
    ex = PKSSearchExecutor(_PKSFalso())
    with pytest.raises(ValueError):
        await ex.execute("pks_reveal", {"entidade": "Daniel", "campo": "cep"})
