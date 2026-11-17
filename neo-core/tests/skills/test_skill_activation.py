"""Ativação da skill: grudenta na sessão + guarda anti-mentira.

Regressão da sessão d4e2ba0f, onde o Neo afirmou DUAS vezes ter registrado um
recebimento de R$ 50 que nunca entrou no banco. A causa não foi o prompt: a skill
era ativada por keyword da mensagem, e o fluxo continuou em frases sem keyword
("so salva", "variados", "sim") — as tools sumiam do turno e ele alucinava a ação.
"""

from __future__ import annotations

import pytest

from core.skills.library import SkillLibrary
from modules.desktop.ai_service import _claims_action_without_tool
from shared.config import get_settings


class _FakeRegistry:
    def exists(self, name: str) -> bool:
        return True

    def get(self, name: str):
        return type("T", (), {"name": name})()


@pytest.fixture
def lib() -> SkillLibrary:
    library = SkillLibrary(root="skills", settings=get_settings(), tool_registry=_FakeRegistry())
    library.load()
    return library


# ── Skill grudenta ────────────────────────────────────────────────────────────
# As frases são as REAIS do fluxo que quebrou.
@pytest.mark.parametrize("msg", ["so salva", "variados", "tente de novo", "sim", "resgitrou mesmo?"])
def test_followup_sem_keyword_mantem_a_skill_ativa(lib: SkillLibrary, msg: str) -> None:
    assert lib.keyword_match(msg) is None  # nenhuma casa por conta própria
    loaded = lib.autoload_for(msg, sticky="financas")
    assert loaded is not None, "o follow-up perdeu as tools da skill — o bug voltou"
    entry, _body, tools = loaded
    assert entry.name == "financas"
    assert "fin_add_transaction" in {t.name for t in tools}


def test_sem_sticky_o_followup_nao_carrega_skill_nenhuma(lib: SkillLibrary) -> None:
    assert lib.autoload_for("sim", sticky=None) is None


def test_keyword_nova_troca_a_skill_ativa(lib: SkillLibrary) -> None:
    # Mudou de assunto no meio: a keyword da mensagem vence a skill grudada.
    loaded = lib.autoload_for("consulta o banco do neo", sticky="financas")
    assert loaded is not None
    assert loaded[0].name == "postgres-neo"


# ── Guarda anti-mentira ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "texto",
    [
        "Categoria \"Variados\" criada e seu recebimento de R$ 50 foi registrado nela.",
        "Criei a categoria \"Variados\" e registrei o recebimento de R$ 50 nela. Pode confiar.",
        "Já está lançado: gasto de R$1,00 via Pix hoje.",
        "Gasto de estacionamento de R$ 3,00 de hoje apagado.",
        # Escapou do detector por falta de vocabulário: banco intacto, ele disse "pronto".
        "Pronto, cartão será tratado como arquivado a partir de agora.",
        "Pagamento de R$ 300 da fatura do Nubank desfeito e fatura reaberta.",
        "Cancelei o parcelamento do Nubank.",
    ],
)
def test_detecta_afirmacao_de_acao(texto: str) -> None:
    assert _claims_action_without_tool(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "Quer que eu registre esse gasto agora?",
        "Posso lançar isso na categoria Mercado, confirma?",
        "Não registrei nada ainda — me diz a categoria primeiro.",
        "Hoje você gastou R$ 20,00. Tudo saiu da Carteira via Pix.",
        "Preciso que escolha a categoria: Apps/Serviços digitais ou Assinaturas e serviços.",
    ],
)
def test_nao_confunde_pergunta_negacao_ou_consulta_com_acao(texto: str) -> None:
    assert _claims_action_without_tool(texto) is False
