"""Baixar da internet pelo shell DELE com URL inventada volta pro cognitivo.

Sessão d9f9417b (2026-09-20). "coloca uma pasta com os relatórios anuais de cada
candidato na pasta teste-de-analise na area de trabalho" e, dois turnos depois,
"cria a pasta, voce vai ter que baixar os pdfs na internet". As três tentativas:

    curl -sL "https://relatorios.exemplo.com.br/$candidato.pdf"
    curl -sL "https://raw.githubusercontent.com/neo-core/government-plans/main/…"
    curl -L -o relatorio_charlie.pdf 'https://www.planalto.gov.br/ccivil_03/ato2019-2022/…'

Nenhum desses endereços existe.

A máquina do usuário não sabe PROCURAR — ela executa a URL que o modelo escreveu.
E o modelo não tem como saber o deep link de um PDF que nunca viu, então inventa
um plausível. Quem sabe procurar é o cérebro (`web_search` + sandbox com
internet), e ele tem a ponte pro disco dele (`file_save` →
`save_generated_file`, que aceita pasta literal e a cria).

Efeito colateral que fecha o círculo: o loop de clarificação só existe no trilho
cognitivo. Enquanto isso era CLIENT_TOOL com 0,95 de confiança, "internet ou
disco?" não era nem uma pergunta possível.
"""

import json

from core.router.intent_router import IntentRouter
from core.tools.registry import ToolRegistry
from modules.desktop.tools import DESKTOP_TOOLS
from shared import IntentType

PEDIDO = (
    "coloca uma pasta com os relatórios anuais de cada empresa na pasta "
    "teste-de-analise na area de trabalho, queor o pdf de cada um deles lá"
)


def _router() -> IntentRouter:
    router = object.__new__(IntentRouter)
    registry = ToolRegistry()
    registry.register_many(DESKTOP_TOOLS)
    router.tool_registry = registry
    return router


def _raw(command: str, *, needs_plan: bool = False) -> str:
    return json.dumps(
        {
            "intent": "CLIENT_TOOL",
            "confidence": 0.95,
            "tool_name": "run_command",
            "tool_arguments": {},
            "raw_command": command,
            "explanation": "teste",
            "requires_approval": False,
            "needs_memory": False,
            "knowledge_keywords": [],
            "needs_plan": needs_plan,
            "tipo_tarefa": "simples",
            "skill_name": None,
        }
    )


def _decide(command, user_message=PEDIDO, history=None, **kw):
    return _router()._parse_classification(
        _raw(command, **kw),
        user_message=user_message,
        conversation_history=history or [],
    )


# ── os três comandos da sessão ────────────────────────────────────────────


def test_dominio_inventado_volta_pro_cerebro():
    d = _decide(
        'mkdir -p /Users/daniel/Desktop/teste-de-analise/relatorios_anuais && '
        'for c in relatorio_charlie echo; do curl -sL "https://relatorios.exemplo.com.br/$c.pdf" '
        '-o "$c.pdf"; done'
    )
    assert d.intent is IntentType.DIRECT_RESPONSE
    assert d.raw_command is None
    assert d.tool_name is None


def test_repositorio_inventado_no_github_volta_pro_cerebro():
    d = _decide(
        'curl -sL "https://raw.githubusercontent.com/neo-core/government-plans/'
        'main/relatorio_charlie.pdf" -o relatorio_charlie.pdf',
        user_message="tenta de novo",
        history=[{"role": "user", "content": PEDIDO}],
    )
    assert d.intent is IntentType.DIRECT_RESPONSE


def test_host_plausivel_com_caminho_chutado_tambem_volta():
    """"Pega no site do planalto" não torna o deep link real — o host é
    plausível e o caminho continua sendo palpite (é o que devolve 404 salvo
    com extensão .pdf)."""
    d = _decide(
        "curl -L -o relatorio_charlie.pdf "
        "'https://www.planalto.gov.br/ccivil_03/ato2019-2022/2022/Plano.pdf'",
        user_message="cria a pasta, voce vai ter que baixar os pdfs na internet "
        "do site do planalto",
    )
    assert d.intent is IntentType.DIRECT_RESPONSE


def test_wget_conta_igual():
    d = _decide("wget -O ~/Desktop/plano.pdf https://exemplo-inventado.org/plano.pdf")
    assert d.intent is IntentType.DIRECT_RESPONSE


# ── o que continua sendo trabalho da máquina dele ─────────────────────────


def test_url_que_o_usuario_deu_continua_client_tool():
    """O shell dele funciona quando o endereço é real — e é o caminho mais curto."""
    url = "https://exemplo.com/relatorio_q3.pdf"
    d = _decide(
        f"curl -sL '{url}' -o ~/Desktop/relatorio_q3.pdf",
        user_message=f"baixa {url} pro meu desktop",
    )
    assert d.intent is IntentType.CLIENT_TOOL
    assert d.raw_command is not None


def test_url_dita_no_historico_tambem_fundamenta():
    url = "https://exemplo.com/nota_fiscal.pdf"
    d = _decide(
        f"curl -sL '{url}' -o ~/Downloads/nota_fiscal.pdf",
        user_message="baixa aquele link que te mandei",
        history=[{"role": "user", "content": f"olha esse aqui: {url}"}],
    )
    assert d.intent is IntentType.CLIENT_TOOL


def test_localhost_nao_e_palpite():
    """Endereço de convenção não exige conhecimento do mundo."""
    d = _decide(
        "curl -s http://localhost:3000/health",
        user_message="ve se meu servidor local subiu",
    )
    assert d.intent is IntentType.CLIENT_TOOL


def test_ip_da_rede_local_nao_e_palpite():
    d = _decide(
        "curl -s http://192.168.4.200/admin/api.php",
        user_message="ve se o pihole responde",
    )
    assert d.intent is IntentType.CLIENT_TOOL


def test_comando_sem_rede_nao_e_afetado():
    d = _decide(
        "ls -la /Users/daniel/Desktop/teste-de-analise",
        user_message="o que tem na pasta teste-de-analise",
    )
    assert d.intent is IntentType.CLIENT_TOOL


def test_curl_sem_url_literal_nao_dispara():
    """A guarda é estreita: sem URL pra julgar, não julga."""
    d = _decide(
        'curl -sL "$URL" -o saida.bin',
        user_message="roda isso aí",
    )
    assert d.intent is IntentType.CLIENT_TOOL


def test_palavra_curl_no_meio_de_outra_nao_conta():
    d = _decide(
        "ls ~/Projetos/curly && cat ~/Projetos/curly/nota.md",
        user_message="abre a nota do projeto curly",
        history=[{"role": "assistant", "content": "tem o `nota.md` em ~/Projetos/curly"}],
    )
    assert d.intent is IntentType.CLIENT_TOOL
