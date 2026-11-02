"""Dado pessoal LITERAL num formulário — o buraco da sessão e0905aae (2026-10-07).

A trava de preenchimento era opt-in do modelo: ela só olhava um `fill` cujo
valor contivesse ``pks:``. Valor literal passava sem conferência nenhuma, e o
modelo não precisa do dado real para preencher — ele inventa. No teste E2E do
§24 o cérebro escreveu, dentro do próprio `task`, "preencha o CPF com
00000000000 (se recusar, use 11144477735)"; o agente digitou; a tabela de
necessidade, a liberação e o card nunca rodaram.

Estes testes fixam os dois lados do conserto:

* o `fill` é classificado pelo CAMPO de destino e pela FORMA do valor;
* a tarefa do navegador é recusada quando já traz o documento escrito.
"""

from __future__ import annotations

import pytest

from core.release.necessity import explicar
from core.release.sensitive import (
    campo_do_fill,
    campos_pessoais_no_texto,
    classificar_campo,
    classificar_valor,
    cnpj_valido,
    cpf_valido,
    dados_pessoais_no_texto,
)

#: O formulário da fixture `scripts/teste_liberacao_web/frete.html`, como o
#: snapshot do driver realmente o entrega (o `<input>` se nomeia pelo
#: placeholder; o nome de verdade está no `<label>` anterior, com `controla`).
FRETE = {
    "url": "http://localhost:8099/frete.html",
    "elements": [
        {"ref": 0, "tag": "label", "text": "CEP de entrega", "controla": "input[cep]"},
        {"ref": 1, "tag": "input", "text": "00000-000"},
        {"ref": 2, "tag": "label", "text": "CPF do destinatário", "controla": "input[cpf]"},
        {"ref": 3, "tag": "input", "text": "000.000.000-00"},
        {"ref": 4, "tag": "button", "type": "submit", "text": "Calcular frete"},
        {"ref": 5, "tag": "input", "text": "Buscar produto"},
    ],
}


# ── forma do valor ───────────────────────────────────────────────────────


def test_cpf_e_cnpj_pelo_digito_verificador():
    assert cpf_valido("111.444.777-35")
    # Repetição é inválida por definição — e é o inventado mais comum.
    assert not cpf_valido("00000000000")
    assert cnpj_valido("11.222.333/0001-81")
    assert not cnpj_valido("11111111111111")


@pytest.mark.parametrize(
    "valor,esperado",
    [
        ("11144477735", "cpf"),
        ("62500-000", "cep"),
        ("11222333000181", "cnpj"),
        ("4111111111111111", "cartao"),
        # Nada disto pode virar documento por acidente.
        ("12345678901", None),
        ("289,90", None),
        ("62500000", None),
        ("cortina blackout", None),
        ("", None),
    ],
)
def test_classificar_valor_so_no_inequivoco(valor, esperado):
    assert classificar_valor(valor) == esperado


# ── rótulo do campo ──────────────────────────────────────────────────────


def test_campo_pega_o_que_a_forma_nao_pega():
    # DV não fecha, mas o campo diz "CPF" — e é justamente o valor inventado.
    assert classificar_valor("00000000000") is None
    assert campo_do_fill(FRETE, 3, "00000000000") == "cpf"


def test_cep_e_cpf_do_formulario_do_24():
    assert campo_do_fill(FRETE, 1, "62500-000") == "cep"
    assert campo_do_fill(FRETE, 3, "11144477735") == "cpf"


def test_botao_nao_herda_o_rotulo_do_campo_de_cima():
    """Falso positivo que recusaria um clique legítimo com a frase errada."""
    assert campo_do_fill(FRETE, 4, "x") is None


def test_campo_seguinte_nao_herda_o_placeholder_do_anterior():
    """A busca logo depois do formulário não é um campo de CPF."""
    assert campo_do_fill(FRETE, 5, "cortina blackout") is None


def test_palavra_inteira_nunca_pedaco():
    assert classificar_campo(["consumo de energia"]) is None
    assert classificar_campo(["arquivo zipado"]) is None
    assert classificar_campo(["RG do titular"]) == "rg"


def test_email_e_nome_nao_sao_barrados():
    """O Neo tem e-mail próprio e a persona manda usá-lo em cadastro."""
    assert classificar_campo(["E-mail", "Seu nome completo"]) is None


def test_ref_inexistente_nao_explode():
    assert campo_do_fill(FRETE, 99, "x") is None
    assert campo_do_fill(None, 1, "x") is None


def test_device_antigo_sem_tag_ainda_classifica():
    velho = {
        "elements": [
            {"ref": 0, "text": "CPF", "controla": "input[cpf]"},
            {"ref": 1, "text": "000.000.000-00"},
        ]
    }
    assert campo_do_fill(velho, 1, "11144477735") == "cpf"


# ── o veredito do §24 ────────────────────────────────────────────────────


def test_cpf_para_frete_nao_vira_card_vira_recado():
    """A tabela RECUSA: não existe aprovação que conserte um campo que não se pede."""
    motivo = explicar("frete", ["cpf"])
    assert motivo
    assert "'cpf' não é necessário" in motivo
    assert "cep" in motivo


def test_cep_para_frete_e_admitido_entao_tem_caminho():
    """Admitido = segue para solicitar_dado → card → aprovação → «pks:cep»."""
    assert explicar("frete", ["cep"]) == ""


# ── dado literal dentro da tarefa do navegador ───────────────────────────


def test_a_tarefa_real_que_vazou_e_recusada():
    task = (
        'Na página http://localhost:8099/frete.html ... Preencha o campo CEP de '
        "entrega com 62500-000 e preencha o campo CPF do destinatário com "
        "00000000000 (se o campo validar CPF e recusar esse valor, use "
        '11144477735). Depois clique em "Calcular frete".'
    )
    assert dados_pessoais_no_texto(task) == ["cpf", "cep"]


@pytest.mark.parametrize(
    "task",
    [
        "Abrir http://localhost:8099/frete.html e ler o valor do frete",
        "Buscar cortina blackout 2,00 x 1,80 por ate R$ 289,90 no Mercado Livre",
        "Pedido 2026-10-07 numero 4815162342 na loja",
        "Preencha o CEP com «pks:cep» e confirme",
        "baixar nota fiscal de 01/02/2026 a 28/02/2026",
    ],
)
def test_tarefa_legitima_nao_e_recusada(task):
    """Falso positivo aqui recusa a tarefa INTEIRA — o custo é alto."""
    assert dados_pessoais_no_texto(task) == []


# ── um veredito por tarefa, não um por rodada (2026-10-18) ───────────────
#
# O §24 rodou de novo e passou pela METADE: a tarefa trazia CEP e CPF, e a
# recusa nomeou só o CEP. O CPF era `00000000000` — dígito verificador não
# fecha, então `dados_pessoais_no_texto` (que só olha a FORMA) não tinha o que
# ver, apesar de "CPF do destinatário" estar escrito ao lado do número. O
# Daniel leu no chat que o CPF fora barrado; a tabela de necessidade nunca
# rodou nele. O bloqueio era o modelo repetindo o exemplo da própria recusa.

#: A tarefa EXATA que o cérebro despachou em 2026-10-18, sessão e0905aae.
TAREFA_18_10 = (
    "Abrir http://localhost:8099/frete.html e preencher o formulário de cálculo "
    'de frete. Campos: "CEP de entrega" (preencher com 62500-000) e "CPF do '
    'destinatário" (preencher com 00000000000). Depois clicar no botão '
    '"Calcular frete" e aguardar o resultado.'
)


def test_a_forma_sozinha_perdia_o_cpf_inventado():
    """A regressão, nomeada: o detector antigo via um campo de dois."""
    assert dados_pessoais_no_texto(TAREFA_18_10) == ["cep"]
    assert campos_pessoais_no_texto(TAREFA_18_10) == ["cpf", "cep"]


def test_placeholder_certo_nunca_e_recusado():
    """Anti-laço: a recusa MANDA escrever «pks:cpf» — ela não pode barrar isso."""
    assert campos_pessoais_no_texto("Preencha o CPF com «pks:cpf» e confirme") == []
    assert campos_pessoais_no_texto("Preencha o CEP com <<pks:cep>>") == []


def test_o_rotulo_mais_perto_do_numero_e_que_nomeia():
    """Varrer na ordem da tabela culparia o CPF pelo literal do CEP."""
    texto = "Preencha o CPF com «pks:cpf» e o CEP com 62500-000"
    assert campos_pessoais_no_texto(texto) == ["cep"]


@pytest.mark.parametrize(
    "task",
    [
        "Abrir http://localhost:8099/frete.html e ler o valor do frete",
        "Buscar cortina blackout 2,00 x 1,80 por ate R$ 289,90 no Mercado Livre",
        "Pedido 2026-10-07 numero 4815162342 na loja",
        "baixar nota fiscal de 01/02/2026 a 28/02/2026",
        # "zip" vale como CEP num <label>, mas numa tarefa é formato de arquivo.
        "baixe o arquivo zip 12345678 da pagina",
    ],
)
def test_tarefa_legitima_continua_passando(task):
    """Falso positivo aqui recusa a tarefa INTEIRA — o custo segue alto."""
    assert campos_pessoais_no_texto(task) == []


def _recusa_da_tarefa_real():
    from core.jobs.executor import JobToolExecutor

    return JobToolExecutor._recusar_dado_literal(
        TAREFA_18_10, "", "http://localhost:8099/frete.html"
    )


def test_recusa_resolve_os_dois_campos_numa_resposta_so():
    recusa = _recusa_da_tarefa_real()
    assert recusa is not None and recusa["ok"] is False
    erro = recusa["error"]
    # O que TEM caminho volta como o marcador certo, já escrito.
    assert "«pks:cep»" in erro
    # O que a tabela recusa não vira card — vira recado, com o motivo dela.
    assert "'cpf' não é necessário para 'frete'" in erro
    # E nunca o contrário: o CPF não pode aparecer como marcador a usar.
    assert "«pks:cpf»" not in erro


def test_quem_pede_e_a_tarefa_nao_o_cerebro():
    """Sessão 1a605544: a frase mandava chamar `solicitar_dado`, o agente do
    SERVER_TOOL não tinha a tool e respondeu "ficou esperando aprovação" sem
    pedido nenhum. Agora a tarefa para no campo e pede sozinha — a frase não
    pode depender de tool que o leitor talvez não tenha."""
    erro = _recusa_da_tarefa_real()["error"]
    assert "browser_local_task" in erro
    assert "CONTINUA SOZINHA" in erro
    assert "Não chame `solicitar_dado`" in erro


def test_destino_citado_e_o_host_que_o_preenchimento_vai_comparar():
    """O card de 18/10 nasceu com `localhost:8099` e NÃO casaria no `fill`.

    `host_of` tira a porta e `domain_matches` compara host com host, então
    `domain_matches('localhost:8099', 'localhost')` é False. Hoje quem escreve o
    destino do card é o job, a partir da ABA; a frase só o cita — e na mesma
    forma canônica.
    """
    from core.release.fill import domain_matches, host_of

    assert not domain_matches("localhost:8099", host_of("http://localhost:8099/x"))
    erro = _recusa_da_tarefa_real()["error"]
    assert "para localhost e" in erro and "localhost:8099" not in erro
