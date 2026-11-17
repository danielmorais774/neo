"""Bloqueio por ORIGEM é reconhecido e vira uma pergunta, não um fracasso mudo.

Job 36a81dd6 (sessão 8c449cb8, 2026-09-20): "baixar o ZIP de relatórios anuais
2026 do portal de dados". O subagente de browser rodou 22 passos e colecionou 51
CAPTCHAs, 43 "Access Denied" e 36 respostas 403. O usuário recebeu:

    A tarefa de browser falhou: timeout depois de 570s

O agente não estava quebrado. A VPS sai para a internet por
`AS51167 Contabo GmbH` (datacenter, França) e, para o WAF do portal de dados (Akamai/
EdgeSuite) e para Google/DuckDuckGo, esse IP é um robô. Confirmado por fora:
`curl` do sandbox no mesmo ZIP dá 403 mesmo com User-Agent de Chrome — é
bloqueio de rede, não de header.

Insistir no servidor queima outros 570s pelo mesmo fim. O que funciona é o
navegador DELE (IP residencial, no Brasil) — e isso o Neo não faz sozinho
(`_gate_browser`: tomar a tela do usuário sem pedir é a falha grave). O desfecho
certo é PERGUNTAR, com o motivo na mão.
"""

import pytest

from core.jobs.browser_blocked import (
    AVISO,
    anotar_se_bloqueio,
    parece_bloqueio,
    sinais_de_bloqueio,
)

# O que o agente realmente acumulou, do log do container.
DIAGNOSTICO_REAL = (
    "Chegou ao passo 22 antes de ser interrompido.\n"
    "O que o agente já tinha tentado: Tentativas falhas: 1) "
    "cdn.exemplo.gov.br/dados/relatorios/"
    "relatorios_2026_BR.zip → 403. 2) dados.exemplo.gov.br/dataset/"
    "candidatos-2026 → Access Denied (EdgeSuite). 3) DuckDuckGo → CAPTCHA. "
    "4) Google → CAPTCHA. 5) Bing → resultados irrelevantes (localização fora "
    "do Brasil). 6) www.exemplo.gov.br → Access Denied."
)


# ── reconhece ─────────────────────────────────────────────────────────────


def test_o_caso_da_sessao_e_reconhecido():
    assert parece_bloqueio(DIAGNOSTICO_REAL)
    sinais = sinais_de_bloqueio(DIAGNOSTICO_REAL)
    assert "403" in sinais
    assert "captcha" in sinais
    assert "access denied" in sinais


def test_o_aviso_explica_a_causa_e_pede_para_perguntar():
    texto = anotar_se_bloqueio(
        "A tarefa de browser falhou: timeout depois de 570s", DIAGNOSTICO_REAL
    )
    assert AVISO.split("{")[0].strip()[:20] in texto
    # Tem que dizer POR QUE (para o Neo saber explicar).
    assert "datacenter" in texto
    # Tem que apontar a saída.
    assert "local" in texto.lower()
    # E tem que proibir a tomada de tela por conta própria.
    assert "PERGUNTE" in texto


def test_cloudflare_tambem_conta():
    assert parece_bloqueio("Just a moment... Cloudflare checking your browser")


def test_um_sinal_sozinho_nao_basta():
    """Coincidência não é padrão: "403" solto num texto qualquer não vira
    sugestão de trocar de navegador."""
    assert not parece_bloqueio("O servidor respondeu 403 numa das imagens da página")
    assert sinais_de_bloqueio("erro 403") == ["403"]


# ── e não confunde falha comum com bloqueio ───────────────────────────────


@pytest.mark.parametrize(
    "texto",
    [
        "A tarefa de browser falhou: timeout depois de 570s",
        "O subagente não encontrou o botão de download na página",
        "A busca não retornou resultados relevantes",
        "Elemento não encontrado: input[name=q]",
        "O site está fora do ar (500)",
        "",
    ],
)
def test_falha_comum_nao_vira_sugestao_de_navegador_local(texto):
    assert not parece_bloqueio(texto)
    assert anotar_se_bloqueio(texto) == texto


def test_resumo_sem_bloqueio_passa_intacto():
    resumo = "A tarefa de browser falhou: timeout depois de 570s"
    assert anotar_se_bloqueio(resumo) == resumo
