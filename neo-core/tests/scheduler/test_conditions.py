"""As guardas (docs/scheduler-plan.md §8).

Condição barrada não pode contar como erro: uma caixa de email fora do ar não
pode queimar o disjuntor de uma tarefa que nem chegou a rodar.
"""

from __future__ import annotations

import pytest

from core.scheduler.conditions import ConditionError, evaluate, fingerprint, validate


async def _tool_ok(nome: str, args: dict):
    return {"total": 3, "itens": [{"preco": 2500}], "texto": "tem coisa nova"}


async def _tool_vazio(nome: str, args: dict):
    return {"total": 0, "itens": []}


async def _tool_quebra(nome: str, args: dict):
    raise RuntimeError("IMAP fora do ar")


@pytest.mark.asyncio
async def test_sem_condicao_sempre_passa() -> None:
    passou, _, _ = await evaluate({})
    assert passou


@pytest.mark.asyncio
async def test_compara_campo_do_resultado() -> None:
    cond = {"tipo": "tool", "tool": "email_inbox", "espera": {"campo": "total", "operador": ">", "valor": 0}}
    passou, explicacao, _ = await evaluate(cond, run_tool=_tool_ok)
    assert passou and "passou" in explicacao

    passou, _, _ = await evaluate(cond, run_tool=_tool_vazio)
    assert not passou


@pytest.mark.asyncio
async def test_caminho_pontuado() -> None:
    cond = {"tipo": "tool", "tool": "x", "espera": {"campo": "itens.0.preco", "operador": "<", "valor": 3000}}
    passou, _, _ = await evaluate(cond, run_tool=_tool_ok)
    assert passou


@pytest.mark.asyncio
async def test_condicao_que_quebra_nao_dispara_e_nao_explode() -> None:
    cond = {"tipo": "tool", "tool": "email_inbox"}
    passou, explicacao, _ = await evaluate(cond, run_tool=_tool_quebra)
    assert not passou
    assert "IMAP fora do ar" in explicacao


@pytest.mark.asyncio
async def test_device_online() -> None:
    async def online():
        return ["mac-do-daniel"]

    passou, _, _ = await evaluate({"tipo": "device"}, devices_online=online)
    assert passou
    passou, _, _ = await evaluate({"tipo": "device", "device_id": "outro"}, devices_online=online)
    assert not passou


def test_validate_recusa_forma_errada_na_hora_de_salvar() -> None:
    with pytest.raises(ConditionError):
        validate({"tipo": "inexistente"})
    with pytest.raises(ConditionError):
        validate({"tipo": "tool"})  # sem 'tool'
    with pytest.raises(ConditionError):
        validate({"tipo": "http", "url": "x", "espera": {"operador": "≈"}})


def test_fingerprint_ignora_ordem_de_chave_e_espaco() -> None:
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint("preço:  10") == fingerprint("preço: 10")
    assert fingerprint("preço: 10") != fingerprint("preço: 11")
