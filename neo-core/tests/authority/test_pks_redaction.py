"""Redação de campo sensível do PKS (docs/release-request-plan.md §7, F2)."""

from __future__ import annotations

import pytest

from core.pks.sensitivity import (
    NORMAL,
    SECRET,
    SENSITIVE,
    classify_key,
    effective_level,
    handle_for,
    redact_properties,
    redact_value,
)


@pytest.mark.parametrize(
    "chave",
    ["cpf", "CPF", "cnpj", "rg", "cnh", "passaporte", "endereco", "endereço",
     "cep", "logradouro", "bairro", "email", "e_mail", "telefone", "celular",
     "whatsapp", "saldo", "balance", "amount", "cartao", "agencia",
     "conta_bancaria", "data_nascimento", "birth_date"],
)
def test_chave_de_identificacao_e_sensivel(chave):
    assert classify_key(chave) == SENSITIVE


@pytest.mark.parametrize("chave", ["senha", "password", "api_key", "token", "cvv", "chave_pix"])
def test_credencial_e_secret(chave):
    assert classify_key(chave) == SECRET


@pytest.mark.parametrize(
    "chave",
    ["name", "full_name", "preferred_name", "birthday", "reps", "sets",
     "description", "label", "category", "ram_gb", "weekday"],
)
def test_o_que_o_neo_usa_todo_dia_continua_visivel(chave):
    assert classify_key(chave) == NORMAL


def test_birthday_e_excecao_porque_ja_sai_truncado():
    """dia/mês é o que faz 'amanhã é aniversário dele' funcionar."""
    assert classify_key("birthday") == NORMAL
    assert classify_key("birth_date") == SENSITIVE


def test_nivel_gravado_mais_restritivo_vence_o_regex():
    assert effective_level("reps", "secret") == SECRET
    assert effective_level("reps", "private") == NORMAL
    # e o regex vence um gravado mais frouxo (o caso real: tudo é 'private')
    assert effective_level("cpf", "private") == SENSITIVE


def test_valor_vira_endereco_nao_dado():
    valor, foi = redact_value("cpf", "123.456.789-00")
    assert foi and valor == handle_for("cpf") == "«pks:cpf»"
    assert "123" not in str(valor)


def test_campo_normal_passa_intacto():
    assert redact_value("reps", 12) == (12, False)


def test_redact_properties_mistura_os_dois():
    props = {"name": "Fulano", "cpf": "123.456.789-00", "reps": 10}
    saida = redact_properties(props)
    assert saida["name"] == "Fulano"
    assert saida["reps"] == 10
    assert saida["cpf"] == "«pks:cpf»"


def test_redacao_desligada_devolve_tudo(monkeypatch):
    monkeypatch.setenv("NEO_PKS_REDACTION_ENABLED", "false")
    from shared.config import get_settings

    get_settings.cache_clear()
    try:
        assert redact_properties({"cpf": "x"}) == {"cpf": "x"}
    finally:
        get_settings.cache_clear()


def test_dict_vazio_nao_quebra():
    assert redact_properties({}) == {}
    assert redact_properties(None) is None


# ── ponta a ponta no bloco do prompt ─────────────────────────────────────

from uuid import uuid4  # noqa: E402

from core.pks.context_builder import ContextBuilder  # noqa: E402
from core.pks.schemas import (  # noqa: E402
    EntityRecord,
    FieldRecord,
    Neighborhood,
    RetrievalResult,
)


def _bloco(campos: list[tuple[str, object]], *, gravado: str = "private") -> str:
    """Bloco de memória como ele entra no prompt, com os campos dados."""
    workspace_id, entity_id = uuid4(), uuid4()
    resultado = RetrievalResult(
        workspace_id=workspace_id,
        keywords=["daniel"],
        neighborhoods=[
            Neighborhood(
                entity=EntityRecord(
                    id=entity_id, workspace_id=workspace_id, name="Daniel", type="person"
                ),
                fields=[
                    FieldRecord(
                        id=uuid4(),
                        entity_id=entity_id,
                        key=chave,
                        value_type="text",
                        value=valor,
                        sensitivity=gravado,
                    )
                    for chave, valor in campos
                ],
            )
        ],
    )
    return ContextBuilder().format(resultado, user_message="quem sou eu?")


def test_o_valor_do_cep_nao_entra_no_prompt():
    texto = _bloco([("cep", "01234-567"), ("preferred_name", "Daniel")])
    assert "01234-567" not in texto
    assert "cep: «pks:cep»" in texto
    assert "preferred_name: Daniel" in texto  # o que ele usa todo dia continua


def test_credencial_some_do_bloco_inteira():
    texto = _bloco([("senha", "hunter2"), ("cpu", "Ryzen")])
    assert "hunter2" not in texto and "senha" not in texto
    assert "cpu: Ryzen" in texto


def test_unidade_nao_acompanha_endereco_redigido():
    """`«pks:saldo» BRL` entregaria a forma do dado sem entregar o dado."""
    texto = _bloco([("balance", 1234.5)])
    assert "1234" not in texto and "«pks:balance»" in texto
