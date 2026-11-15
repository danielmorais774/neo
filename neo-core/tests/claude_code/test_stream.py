"""O leitor do stream-json contra rodadas REAIS gravadas na F0 (CLI 2.1.302).

As fixtures saíram de ``claude -p --output-format stream-json`` no Mac do Daniel,
com caminhos trocados por ``/home/neo`` e os eventos de hook tirados
(docs/claude-code-delegation-plan.md §13).
"""

from __future__ import annotations

import json
from pathlib import Path

from core.claude_code.stream import LeitorDeStream, Recusa

FIX = Path(__file__).parent / "fixtures"


def _ler(nome: str, *, pedacos: int = 1) -> tuple[LeitorDeStream, list[str]]:
    texto = (FIX / nome).read_text()
    leitor = LeitorDeStream(cwd="/home/neo/proj")
    notas: list[str] = []
    tamanho = max(1, len(texto) // pedacos)
    for i in range(0, len(texto), tamanho):
        notas += leitor.alimentar(texto[i: i + tamanho])
    return leitor, notas


def test_rodada_concluida_traz_relatorio_cota_e_notas():
    leitor, notas = _ler("auto_concluido.jsonl")
    assert leitor.modo_permissao == "auto"
    assert leitor.origem_da_chave == "none"  # assinatura, não API key
    assert leitor.fim is not None and not leitor.fim.erro
    assert leitor.fim.relatorio["status"] == "concluido"
    assert leitor.fim.turnos == 5
    assert leitor.cota is not None and leitor.cota.janela_5h is not None
    assert not leitor.cota.estourou
    assert "escrevendo `hello.py`" in notas
    assert "rodando `python3 hello.py`" in notas
    assert "escrevendo o relatório final" in notas


def test_pedaco_cortado_no_meio_da_linha_da_o_mesmo_resultado():
    inteiro, notas_inteiro = _ler("accept_negado.jsonl")
    picado, notas_picado = _ler("accept_negado.jsonl", pedacos=37)
    assert notas_picado == notas_inteiro
    assert picado.fim.relatorio == inteiro.fim.relatorio
    assert picado.desconhecidos == 0


def test_recusas_vem_na_hora_e_no_result_sem_duplicar():
    leitor, notas = _ler("accept_negado.jsonl")
    assert leitor.fim.relatorio["status"] == "precisa_de_voce"
    ferramentas = sorted(r.ferramenta for r in leitor.fim.recusas)
    assert ferramentas == ["Bash", "Write"]
    assert any(n.startswith("sem permissão para rodar `python3 hello.py`") for n in notas)
    assert {r.descricao() for r in leitor.fim.recusas} == {
        "rodar `python3 hello.py`",
        "Write em `/tmp/neo-f0-fora.txt`",
    }


def test_rodada_morta_no_meio_nao_tem_fim():
    leitor, notas = _ler("morto_no_meio.jsonl")
    assert leitor.fim is None
    assert leitor.session_id
    assert "rodando `sleep 6`" in notas
    # o comando composto foi negado pela allowlist (só `sleep` estava liberado)
    assert [r.ferramenta for r in leitor.recusas] == ["Bash"]


def test_retomada_mantem_a_sessao():
    morto, _ = _ler("morto_no_meio.jsonl")
    retomada, _ = _ler("retomada.jsonl")
    assert retomada.fim is not None and not retomada.fim.erro
    assert retomada.fim.session_id == morto.session_id


def test_evento_desconhecido_e_lixo_nao_derrubam():
    leitor = LeitorDeStream()
    notas = leitor.alimentar('{"type":"coisa_nova_da_cli"}\nnão é json\n{"type":"assistant"')
    assert notas == []
    assert leitor.desconhecidos == 2
    # a linha incompleta fica guardada até chegar o resto
    leitor.alimentar(',"message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}\n')
    assert leitor.desconhecidos == 2
    assert leitor.eventos == 2


def test_limite_atingido_pelo_status_da_api():
    leitor = LeitorDeStream()
    leitor.alimentar(
        '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","unifiedWindows":'
        '{"five_hour":{"utilization":1.0,"resetsAt":1794079200}}}}\n'
        '{"type":"result","subtype":"error","is_error":true,"api_error_status":429,'
        '"result":"limit","session_id":"s","num_turns":1}\n'
    )
    assert leitor.cota.estourou
    assert leitor.fim.limite_atingido


def test_descricao_de_recusa_sem_caminho():
    assert Recusa("WebFetch", {"url": "https://x.com"}).descricao() == "WebFetch em `https://x.com`"
    assert Recusa("Coisa", {}).descricao() == "usar Coisa"


def test_comando_longo_mostra_o_fim_onde_mora_o_rm():
    """Job 8f83b37c: o diário cortava em 100 e escondia `> RELATORIO.md; rm saida.txt`."""
    leitor = LeitorDeStream(cwd="/home/neo/proj")
    comando = "{ cat <<'EOF'\n# Relatório\n" + "linha de texto do relatório\n" * 300 + "EOF\n} > RELATORIO.md; rm saida.txt"
    evento = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": comando}},
    ]}}
    (nota,) = leitor.alimentar(json.dumps(evento) + "\n")
    assert nota.startswith("rodando `{ cat <<'EOF' # Relatório")
    assert nota.endswith("> RELATORIO.md; rm saida.txt`")
    assert " … " in nota and "\n" not in nota


def test_texto_do_claude_vai_inteiro_numa_linha():
    leitor = LeitorDeStream()
    evento = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Primeira linha.\n\nSegunda linha que antes sumia."},
    ]}}
    assert leitor.alimentar(json.dumps(evento) + "\n") == ["💬 Primeira linha. Segunda linha que antes sumia."]


def test_saida_do_comando_vem_com_as_linhas_e_erros_de_outras_ferramentas():
    leitor = LeitorDeStream()
    pedido = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "python3 jogo.py"}},
        {"type": "tool_use", "id": "b2", "name": "Bash", "input": {"command": "rm x"}},
        {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "/a"}},
        {"type": "tool_use", "id": "w2", "name": "Write", "input": {"file_path": "/b"}},
    ]}}
    volta = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "b1", "content": "  1 | 2 | 3\n ---+---+---\n*** O venceu! ***"},
        {"type": "tool_result", "tool_use_id": "b2", "content": [{"type": "text", "text": "(Bash completed with no output)"}]},
        {"type": "tool_result", "tool_use_id": "w1", "content": "File created successfully at: /a"},
        {"type": "tool_result", "tool_use_id": "w2", "is_error": True, "content": "File has not been read yet."},
    ]}}
    leitor.alimentar(json.dumps(pedido) + "\n")
    notas = leitor.alimentar(json.dumps(volta) + "\n")
    assert notas == [
        "↳ saída (3 linhas):\n  1 | 2 | 3\n ---+---+---\n*** O venceu! ***",
        "↳ sem saída",
        "↳ erro em Write: File has not been read yet.",
    ]


def test_saida_gigante_guarda_comeco_e_fim():
    leitor = LeitorDeStream()
    leitor.alimentar(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "seq 500"}},
    ]}}) + "\n")
    saida = "\n".join(str(i) for i in range(1, 501))
    (nota,) = leitor.alimentar(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "b1", "content": saida},
    ]}}) + "\n")
    linhas = nota.splitlines()
    assert linhas[0] == "↳ saída (500 linhas):"
    assert linhas[1] == "1" and linhas[-1] == "500"
    assert "… (440 linhas no meio) …" in linhas


def test_resultado_de_comando_negado_nao_repete_a_recusa():
    _, notas = _ler("accept_negado.jsonl")
    assert not [n for n in notas if n.startswith("↳")]
    _, notas = _ler("auto_concluido.jsonl")
    assert "↳ saída (1 linha):\nola neo" in notas
