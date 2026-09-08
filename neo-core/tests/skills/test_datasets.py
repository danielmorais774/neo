"""Cadastros generativos — o que quebra silencioso (docs/datasets-crud-plan.md).

Mesma linha de test_finance.py: o executor contra Postgres real não é testado
aqui (o CRUD é exercitado em prod pelas rotas), e sim o que dá bug mudo:

- a fronteira que torna seguro montar DDL por interpolação (§9.3): identificador,
  tipo fechado e nome de tabela vindo do registry;
- o parsing de dinheiro BR — herdado da regra "centavos inteiros" do fin_*;
- os erros que ENSINAM (campo desconhecido lista os válidos), porque erro mudo
  vira retry infinito no tool loop;
- o agregador: colisão de nome RECUSA em vez de renomear, e a busca acha.
"""

from pathlib import Path

import pytest
import yaml

from core.datasets.ddl import (
    add_column_sql,
    build_table_name,
    create_table_sql,
    drop_column_sql,
    qualified,
)
from core.datasets.schema import (
    FIELD_TYPES,
    DatasetSchema,
    FieldDef,
    SchemaError,
    coerce_value,
    parse_fields,
    parse_money_to_cents,
    parse_schema,
    serialize_value,
    slugify,
    validate_identifier,
)
from core.tools.aggregator import ToolAggregator, ToolNameConflict
from modules.datasets.tools import ALL_DATASET_TOOLS, DATA_TOOL_NAMES
from shared import ToolParameter, ToolSchema


def _schema(**over):
    base = dict(
        fields=[
            {"name": "nome", "type": "texto", "required": True, "max": 120},
            {"name": "preco", "type": "dinheiro", "required": True},
            {"name": "estoque", "type": "inteiro", "default": 0},
            {"name": "categoria", "type": "opcao", "options": ["caneca", "camiseta"]},
            {"name": "ativo", "type": "booleano", "default": True},
        ]
    )
    base.update(over)
    return DatasetSchema.model_validate(base)


# ---------------------------------------------------------------------------
# Fronteira do DDL — a linha que separa "seguro" de "injeção no banco de prod"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "produtos; DROP TABLE user",
        "produtos--",
        'produtos" ; --',
        "1produtos",         # começa com dígito
        "produtos com espaco",
        "produtos'",
        "",
        "a" * 40,
        "select",            # reservada do Postgres
        "table",
    ],
)
def test_identificador_recusa_tudo_que_nao_e_identificador(bad):
    with pytest.raises(SchemaError):
        validate_identifier(bad, what="slug")


def test_identificador_aceita_o_que_deve():
    assert validate_identifier("produtos") == "produtos"
    assert validate_identifier("ordens_servico_2") == "ordens_servico_2"
    # Maiúscula e espaço em volta são NORMALIZADOS, não recusados: quem digita
    # "Produtos" no painel quer o cadastro `produtos`, não um erro de validação.
    # O que é recusado é o que não vira identificador só com lower()+strip().
    assert validate_identifier(" PRODUTOS ") == "produtos"


@pytest.mark.parametrize("reserved", ["id", "user_id", "created_at", "updated_at", "deleted_at"])
def test_campo_nao_pode_sombrear_a_espinha(reserved):
    """Um campo 'deleted_at' do usuário quebraria o soft delete inteiro."""
    with pytest.raises(Exception) as exc:
        FieldDef.model_validate({"name": reserved, "type": "texto"})
    assert "controle" in str(exc.value).lower()


def test_tipo_desconhecido_e_recusado_com_a_lista_dos_validos():
    with pytest.raises(SchemaError) as exc:
        parse_fields([{"name": "x", "type": "blob"}])
    assert "Tipos válidos" in str(exc.value)
    assert "dinheiro" in str(exc.value)


def test_nome_de_tabela_so_nasce_do_par_slug_codigo():
    assert build_table_name("produtos", "k3f9") == "produtos_k3f9"
    with pytest.raises(SchemaError):
        build_table_name("produtos", "k3f9; DROP TABLE user")
    with pytest.raises(SchemaError):
        build_table_name("pro dutos", "k3f9")


def test_qualified_recusa_nome_que_nao_veio_do_registry():
    assert qualified("produtos_k3f9") == "neo_data.produtos_k3f9"
    with pytest.raises(SchemaError):
        qualified("produtos; DROP TABLE user")


def test_ddl_tem_espinha_indices_e_check_do_enum():
    sql = create_table_sql("produtos_k3f9", _schema())
    create = sql[0]
    for spine in ("id UUID PRIMARY KEY", "user_id UUID NOT NULL", "deleted_at TIMESTAMPTZ NULL"):
        assert spine in create
    assert "nome VARCHAR(120)" in create
    assert "preco BIGINT" in create  # dinheiro é centavos inteiros, nunca float
    joined = " ".join(sql)
    assert "ck_produtos_k3f9_categoria" in joined
    assert "'caneca'" in joined and "'camiseta'" in joined
    assert "ix_produtos_k3f9_alive" in joined


def test_opcao_com_aspas_e_escapada_no_check():
    """O ÚNICO texto livre que chega ao SQL são as opções — e ele é escapado."""
    schema = DatasetSchema.model_validate(
        {"fields": [{"name": "estilo", "type": "opcao", "options": ["d'agua", "normal"]}]}
    )
    joined = " ".join(create_table_sql("t_ab12", schema))
    assert "'d''agua'" in joined


def test_add_e_drop_column_validam_o_nome():
    field = FieldDef.model_validate({"name": "cor", "type": "texto"})
    assert "ADD COLUMN IF NOT EXISTS cor VARCHAR(200)" in add_column_sql("t_ab12", field)[0]
    with pytest.raises(SchemaError):
        drop_column_sql("t_ab12", "cor; DROP TABLE user")


# ---------------------------------------------------------------------------
# Dinheiro — regra herdada do fin_*: centavos inteiros, nunca float
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,cents",
    [
        ("1.234,56", 123456),
        ("49,90", 4990),
        ("R$ 49,90", 4990),
        ("1234.56", 123456),
        (49.9, 4990),
        (50, 5000),        # int puro é REAIS (é o que "preco=50" quer dizer)
        ("0,01", 1),
        ("-12,50", -1250),
    ],
)
def test_dinheiro_aceita_formato_br_e_americano(raw, cents):
    assert parse_money_to_cents(raw) == cents


@pytest.mark.parametrize("bad", ["", "abc", "R$", True, None])
def test_dinheiro_recusa_lixo(bad):
    with pytest.raises(SchemaError):
        parse_money_to_cents(bad)


# ---------------------------------------------------------------------------
# Coerção e erros que ENSINAM
# ---------------------------------------------------------------------------


def test_opcao_invalida_lista_as_validas():
    field = _schema().field("categoria")
    with pytest.raises(SchemaError) as exc:
        coerce_value(field, "boné")
    assert "camiseta" in str(exc.value) and "caneca" in str(exc.value)


def test_booleano_entende_portugues():
    field = _schema().field("ativo")
    assert coerce_value(field, "sim") is True
    assert coerce_value(field, "não") is False
    assert coerce_value(field, 0) is False


def test_texto_acima_do_limite_e_recusado_com_o_numero():
    field = _schema().field("nome")
    with pytest.raises(SchemaError) as exc:
        coerce_value(field, "x" * 200)
    assert "120" in str(exc.value)


def test_schema_recusa_campo_repetido_e_coluna_fantasma():
    with pytest.raises(SchemaError):
        parse_schema({"fields": [{"name": "a", "type": "texto"}, {"name": "a", "type": "inteiro"}]})
    with pytest.raises(SchemaError) as exc:
        parse_schema({"fields": [{"name": "a", "type": "texto"}], "list_columns": ["b"]})
    assert "b" in str(exc.value)


def test_schema_preenche_defaults_uteis():
    schema = _schema()
    assert schema.label_field == "nome"          # 1º campo de texto
    assert schema.order_by == "created_at desc"  # mais recente primeiro
    assert "nome" in schema.search_fields
    assert schema.list_columns


def test_order_by_invalido_e_recusado():
    with pytest.raises(SchemaError):
        parse_schema({"fields": [{"name": "a", "type": "texto"}], "order_by": "b asc"})


def test_erro_do_pydantic_vira_uma_frase():
    """Sem isto o modelo recebe '1 validation error for DatasetSchema\\nfields.0…'."""
    with pytest.raises(SchemaError) as exc:
        parse_schema({"fields": [{"name": "select", "type": "texto"}]})
    message = str(exc.value)
    assert "validation error" not in message
    assert "reservada" in message


def test_render_para_o_modelo_cabe_numa_linha_por_campo():
    text = _schema().render_for_model(slug="produtos", table_name="produtos_k3f9")
    assert "neo_data.produtos_k3f9" in text
    assert "nome (texto, obrigatório)" in text
    assert "categoria (opção: caneca|camiseta)" in text


def test_serialize_ida_e_volta_de_data_e_relacao():
    import uuid
    from datetime import date

    d = FieldDef.model_validate({"name": "quando", "type": "data"})
    assert serialize_value(d, date(2026, 9, 6)) == "2026-09-06"
    r = FieldDef.model_validate({"name": "dono", "type": "relacao", "dataset": "clientes"})
    ident = uuid.uuid4()
    assert serialize_value(r, ident) == str(ident)


def test_slugify():
    assert slugify("Ordens de Serviço") == "ordens_de_servico"
    assert slugify("  Produtos!!  ") == "produtos"


# ---------------------------------------------------------------------------
# Agregador
# ---------------------------------------------------------------------------


def _tool(name: str, module: str = "x", execution: str = "server-side") -> ToolSchema:
    return ToolSchema(
        name=name,
        description=f"Faz {name}. Segunda frase que não deve entrar no resumo.",
        module=module,
        execution=execution,
        parameters={"a": ToolParameter(type="string", description="a", required=True)},
    )


def test_agregador_recusa_colisao_em_vez_de_renomear():
    """Renomear em silêncio quebraria o SKILL.md que cita a tool pelo nome."""
    agg = ToolAggregator()
    agg.bind(_tool("data_list"), origin="dataset", source="core/datasets")
    with pytest.raises(ToolNameConflict) as exc:
        agg.bind(_tool("data_list"), origin="skill", source="skills/x/tools.py")
    assert "core/datasets" in str(exc.value)


def test_agregador_e_idempotente_para_a_mesma_origem():
    agg = ToolAggregator()
    a = agg.bind(_tool("data_list"), origin="dataset", source="core/datasets")
    b = agg.bind(_tool("data_list"), origin="dataset", source="core/datasets")
    assert a is b


def test_agregador_recusa_nome_fora_do_padrao_do_provider():
    agg = ToolAggregator()
    with pytest.raises(ValueError):
        agg.bind(_tool("Data.List"), origin="core", source="x")


def test_resumo_e_assinatura_sao_curtos():
    agg = ToolAggregator()
    binding = agg.bind(_tool("data_list"), origin="dataset", source="core/datasets")
    assert binding.summary == "Faz data_list."
    assert binding.signature() == "data_list(a)"


def test_busca_acha_por_nome_e_por_skill():
    agg = ToolAggregator()
    agg.bind(_tool("data_list"), origin="dataset", source="core/datasets", tags=["cadastro"])
    agg.bind(_tool("fin_balances"), origin="core", source="modules/finance", owner_skill="financas")
    assert [b.name for b in agg.search("data_list")] == ["data_list"]
    assert "fin_balances" in [b.name for b in agg.search("financas")]
    assert agg.search("inexistente") == []


def test_schemas_for_pula_nome_desconhecido():
    """O gating nunca oferece tool que o executor não despacha (tools fantasma)."""
    agg = ToolAggregator()
    agg.bind(_tool("data_list"), origin="dataset", source="core/datasets")
    assert [s.name for s in agg.schemas_for(["data_list", "nao_existe"])] == ["data_list"]


def test_unbind_skill_solta_so_o_que_a_skill_trouxe():
    agg = ToolAggregator()
    agg.bind(_tool("data_list"), origin="dataset", source="core/datasets")
    agg.bind(_tool("nfe_emitir"), origin="skill", source="skills/nfe/tools.py", owner_skill="nfe")
    assert agg.unbind_skill("nfe") == ["nfe_emitir"]
    assert agg.exists("data_list") and not agg.exists("nfe_emitir")


# ---------------------------------------------------------------------------
# Contratos que precisam ficar coerentes
# ---------------------------------------------------------------------------


def test_toda_data_tool_pede_o_dataset_e_e_server_side():
    for tool in ALL_DATASET_TOOLS:
        assert tool.execution == "server-side"
        assert tool.module == "datasets"
        key = "slug" if tool.name == "data_define" else "dataset"
        assert key in tool.parameters, f"{tool.name} não pede {key}"


def test_prefixo_data_esta_no_bloqueio_do_classificador():
    """Sem isto o router inventa dataset/campos sem passar pela descoberta."""
    from core.router.intent_router import _SKILL_OWNED_TOOL_PREFIXES

    assert "data_" in _SKILL_OWNED_TOOL_PREFIXES
    assert "tool_" in _SKILL_OWNED_TOOL_PREFIXES
    assert all(n.startswith("data_") for n in DATA_TOOL_NAMES)


def test_data_tools_ficam_fora_do_read_set_do_cognitivo():
    """Elas custam prompt só depois do skill_read — é o ponto do gating."""
    from core.cognitive.read_tools import COGNITIVE_READ_TOOLS

    names = {t.name for t in COGNITIVE_READ_TOOLS}
    assert not (names & DATA_TOOL_NAMES)


def test_meta_skill_criar_cadastro_declara_data_define():
    md = Path(__file__).resolve().parents[2] / "skills" / "criar-cadastro" / "SKILL.md"
    raw = md.read_text(encoding="utf-8")
    meta = yaml.safe_load(raw.split("---")[1])
    assert meta["kind"] == "tool_backed"
    assert meta["tools"] == ["data_define"]
    # `keyword` é o que impede a meta-skill de custar catálogo em toda mensagem.
    assert meta["discovery"] == "keyword"
    assert len(" ".join(str(meta["description"]).split())) <= 200
