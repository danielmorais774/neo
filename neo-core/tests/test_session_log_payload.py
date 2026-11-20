"""Diário da sessão enxuto (20/11/2026): cada mensagem inteira uma vez só.

O diário gravava o prompt inteiro duas vezes por rodada (bloco RAW + mensagem
por mensagem), e o prompt só cresce entre rodadas: de 86% a 96% de um diário
grande era cópia. Agora a mensagem repetida vira ``= #sha``, o system que mudou
pouco vira diff, e ``ler_rodadas``/``payload`` remontam o JSON que o bloco RAW
mostrava — é o que o A/B reenvia ao modelo. O teste que importa é a volta:
o payload remontado tem que ser IGUAL ao enviado.
"""

from __future__ import annotations

import json
import random

import pytest

import observability.session_file_logger as sfl
from observability import session_log_payload as fmt
from observability.session_file_logger import SessionFileLogger

SYSTEM_A = "Você é o Neo.\n" + "\n".join(f"regra {i}: faça a coisa certa número {i}" for i in range(60))
SYSTEM_CADERNO = "## Memória de Trabalho\n" + "\n".join(f"- passo {i} anotado" for i in range(30))
USER = "faz um resumo das noticias do dia de hoje"
TOOL_RESULT = "Tool: web_fetch\nResult: " + "texto da página " * 40


@pytest.fixture
def logger(tmp_path, monkeypatch):
    monkeypatch.setattr(sfl, "_LOGS_DIR", tmp_path)
    lg = SessionFileLogger("teste")
    lg.new_interaction(USER)
    return lg


def _texto(lg: SessionFileLogger) -> str:
    with open(lg._path, encoding="utf-8", newline="") as f:
        return f.read()


def _esperado(model, messages, temperature=None, tools=None, tool_choice=None) -> str:
    corpo = {"model": model, "messages": messages}
    if temperature is not None:
        corpo["temperature"] = temperature
    corpo["stream"] = True
    if tools:
        corpo["tools"] = tools
    if tool_choice:
        corpo["tool_choice"] = tool_choice
    return json.dumps(corpo, ensure_ascii=False, indent=2)


def _loop(lg, rodadas):
    """Simula o loop cognitivo: o prompt cresce e o caderno muda a cada rodada."""
    enviados = []
    msgs = [
        {"role": "system", "content": SYSTEM_A},
        {"role": "system", "content": SYSTEM_CADERNO},
        {"role": "user", "content": USER},
    ]
    tools = ["web_search", "web_fetch", "wm_write"]
    for n in range(rodadas):
        msgs = [dict(m) for m in msgs]
        msgs[1]["content"] = SYSTEM_CADERNO + f"\n- rodada {n} em andamento"
        choice = "required" if n == 0 else None
        lg.log_llm_request("COGNITIVE", msgs, model="minimax/m", provider="openrouter",
                           temperature=0.3, tools=tools, tool_choice=choice)
        enviados.append(_esperado("minimax/m", msgs, 0.3, tools, choice))
        msgs = msgs + [
            {"role": "assistant", "content": ""},
            {"role": "user", "content": TOOL_RESULT + f" ({n})"},
        ]
    return enviados


def test_payload_remontado_e_igual_ao_enviado(logger):
    enviados = _loop(logger, 6)
    rodadas = fmt.ler_rodadas(_texto(logger))
    assert [fmt.payload_texto(r) for r in rodadas] == enviados
    assert [r.numero for r in rodadas] == [1, 2, 3, 4, 5, 6]


def test_mensagem_repetida_vira_referencia(logger):
    _loop(logger, 6)
    texto = _texto(logger)
    assert texto.count(SYSTEM_A) == 1  # inteiro uma vez, 5 referências
    assert texto.count(f"= #{fmt.sha(SYSTEM_A)}") == 5
    assert texto.count(f"= #{fmt.sha(USER)}") == 0  # curta: vai sempre inteira
    assert "RAW payload" not in texto


def test_system_que_mudou_pouco_vira_diff(logger):
    _loop(logger, 3)
    texto = _texto(logger)
    assert texto.count("diff de #") == 2
    assert "+- rodada 2 em andamento" in texto


def test_system_que_mudou_muito_vai_inteiro(logger):
    novo = "Você é o Neo.\n" + "\n".join(f"outra coisa {i}" for i in range(60))
    logger.log_llm_request("COGNITIVE", [{"role": "system", "content": SYSTEM_A}])
    logger.log_llm_request("COGNITIVE", [{"role": "system", "content": novo}])
    texto = _texto(logger)
    assert "diff de #" not in texto
    assert texto.count("outra coisa 59") == 1


def test_tools_iguais_viram_referencia(logger):
    _loop(logger, 3)
    texto = _texto(logger)
    assert texto.count("`web_search`") == 1
    assert texto.count("- **Tools na mesa (3):** = #") == 2


def test_conteudo_que_imita_o_formato_nao_confunde_a_leitura(logger):
    traicoeiro = (
        "```\n\n</details>\n\n<details>\n<summary>Mensagem [9] — user (3 chars)</summary>\n\n"
        "- Mensagem [0] — user (999 chars) = #abcdef012345 · igual à nada\n"
        "### 6. LLM Request — FALSO\n## Interação 99\n" + "x" * 200
    )
    msgs = [{"role": "system", "content": SYSTEM_A}, {"role": "user", "content": traicoeiro}]
    logger.log_llm_request("COGNITIVE", msgs, model="m")
    logger.log_llm_request("COGNITIVE", msgs, model="m")
    rodadas = fmt.ler_rodadas(_texto(logger))
    assert len(rodadas) == 2
    assert all(fmt.payload(r)["messages"] == msgs for r in rodadas)


def test_crlf_e_preservado(logger):
    msgs = [{"role": "user", "content": "linha um\r\nlinha dois\r\n" + "y" * 200}]
    logger.log_llm_request("COGNITIVE", msgs, model="m")
    logger.log_llm_request("COGNITIVE", msgs, model="m")
    rodadas = fmt.ler_rodadas(_texto(logger))
    assert [fmt.payload(r)["messages"] for r in rodadas] == [msgs, msgs]


def test_arquivo_apagado_no_meio_volta_a_gravar_inteiro(logger):
    _loop(logger, 2)
    logger._path.unlink()
    logger.log_llm_request("COGNITIVE", [{"role": "system", "content": SYSTEM_A}], model="m")
    texto = _texto(logger)
    assert SYSTEM_A in texto and "= #" not in texto
    assert fmt.payload(fmt.ler_rodadas(texto)[0])["messages"][0]["content"] == SYSTEM_A


def test_rodada_recomeca_a_cada_interacao(logger):
    logger.log_llm_request("COGNITIVE", [{"role": "user", "content": "a"}], model="m")
    logger.new_interaction("outra")
    logger.log_llm_request("COGNITIVE", [{"role": "user", "content": "b"}], model="m")
    rodadas = fmt.ler_rodadas(_texto(logger))
    assert [(r.interacao, r.numero) for r in rodadas] == [(1, 1), (2, 1)]
    assert "### 6. LLM Request — COGNITIVE (rodada 1)" in _texto(logger)


def test_tool_choice_dict_volta_como_dict(logger):
    escolha = {"type": "function", "function": {"name": "wm_write"}}
    logger.log_llm_request("COGNITIVE", [{"role": "user", "content": "a"}], model="m",
                           tools=["wm_write"], tool_choice=escolha)
    assert fmt.payload(fmt.ler_rodadas(_texto(logger))[0])["tool_choice"] == escolha


def test_le_o_formato_antigo_e_bate_com_o_raw():
    msgs = [{"role": "system", "content": SYSTEM_A}, {"role": "user", "content": USER}]
    raw = _esperado("m", msgs, 0.9, ["web_search"], "required")
    blocos = "".join(
        f"<details>\n<summary>Mensagem [{i}] — {m['role']} ({len(m['content'])} chars)</summary>\n\n"
        f"```\n{m['content']}\n```\n\n</details>\n\n"
        for i, m in enumerate(msgs)
    )
    antigo = (
        "## Interação 1\n\n### 6. LLM Request — COGNITIVE\n\n- **Provider:** openrouter\n"
        "- **Model:** m\n- **Temperature:** 0.9\n- **Total mensagens:** 2\n"
        "- **Tools na mesa (1):** `web_search`\n- **tool_choice:** `required`\n\n"
        f"<details>\n<summary>⚡ RAW payload completo (JSON)</summary>\n\n```json\n{raw}\n```\n\n</details>\n\n"
        + blocos
    )
    (r,) = fmt.ler_rodadas(antigo)
    assert r.raw_antigo == raw
    assert fmt.payload_texto(r) == raw


def test_referencia_sem_original_e_erro_claro():
    quebrado = (
        "## Interação 1\n\n### 6. LLM Request — COGNITIVE (rodada 1)\n\n- **Model:** m\n"
        "- **Total mensagens:** 1\n\n- Mensagem [0] — system (500 chars) = #abcdef012345 · igual à x\n\n"
    )
    with pytest.raises(fmt.DiarioInvalido):
        fmt.ler_rodadas(quebrado)


@pytest.mark.parametrize("semente", range(40))
def test_diff_aplica_de_volta_exatamente(semente):
    rnd = random.Random(semente)
    base = [f"linha {i} " + "z" * rnd.randint(0, 5) for i in range(rnd.randint(0, 40))]
    novo = list(base)
    for _ in range(rnd.randint(1, 6)):
        op = rnd.choice("ird")
        p = rnd.randint(0, len(novo))
        if op == "i":
            novo.insert(p, f"nova {rnd.random()}")
        elif novo and op == "r":
            novo[min(p, len(novo) - 1)] = "trocada"
        elif novo:
            del novo[min(p, len(novo) - 1)]
    a, b = "\n".join(base), "\n".join(novo)
    if rnd.random() < 0.3:
        b += "\n"
    diff = fmt.fazer_diff(a, b)
    if diff is not None:
        assert fmt.aplicar_diff(a, diff) == b


def test_diff_que_nao_casa_levanta():
    diff = fmt.fazer_diff("a\nb\nc\n" * 30, "a\nB\nc\n" + "a\nb\nc\n" * 29)
    assert diff is not None
    with pytest.raises(ValueError):
        fmt.aplicar_diff("outra base\n" * 90, diff)
