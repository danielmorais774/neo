"""Caderno não é depósito de leitura (docs/deep-read-plan.md §6).

A nota `sessao-f33632b4` terminou com 49 KB e 37 entradas: `head -c 8000` de um
relatório anual, um grep de 63 KB cortado em 4 000 chars com cara de completo e a
conclusão inteira que o próprio Neo tinha acabado de gravar no /workspace.
"""

from core.working_memory.render import render_auto_entry
from modules.desktop.ai_service import _summarize_tool_call


class _TR:
    error = None

    def __init__(self, result):
        self.result = result


def _resumo(nome, args, result):
    return _summarize_tool_call(nome, args, _TR(result))


def test_leitura_de_arquivo_por_neo_bash_vira_ponteiro():
    texto = "Propostas concretas para os maiores problemas do país. " * 150
    resumo = _resumo("neo_bash", {"command": "head -c 8000 /tmp/Relatorio_Echo.txt"},
                     {"stdout": texto, "exit_code": 0})
    assert "note_detail" not in resumo and "stdout_preview" not in resumo
    linha = render_auto_entry([resumo])
    assert "head -c 8000 /tmp/Relatorio_Echo.txt" in linha
    assert "NÃO copiados" in linha
    assert "Propostas concretas" not in linha
    assert len(linha) < 400


def test_leitura_com_saida_curta_e_resultado_e_vai_inteira():
    resumo = _resumo("neo_bash", {"command": "grep -c imposto /tmp/Relatorio_Charlie.txt"},
                     {"stdout": "37\n", "exit_code": 0})
    assert resumo["note_detail"] == "37"


def test_saida_enorme_de_script_guarda_o_comeco_e_diz_que_o_resto_se_perdeu():
    saida = "ARQUIVO: Relatorio_Echo\n" + ("[...14386...] trecho qualquer\n" * 2500)
    resumo = _resumo("neo_bash", {"command": "python3 /tmp/analisar_planos.py"},
                     {"stdout": saida, "exit_code": 0})
    detalhe = resumo["note_detail"]
    assert detalhe.startswith("ARQUIVO: Relatorio_Echo")
    assert f"saída de {len(saida.strip())} caracteres" in detalhe
    assert "o resto não está em lugar nenhum" in detalhe
    assert len(detalhe) < 1900


def test_neo_write_file_aponta_para_o_arquivo_sem_copiar():
    conclusao = "# Análise Comparativa: Relatórios Anuais 2026\n\n" + "Echo não propõe aumento. " * 500
    resumo = _resumo("neo_write_file",
                     {"path": "/workspace/conclusao-relatorios-anuais-2026.md", "content": conclusao},
                     {"ok": True})
    assert "note_detail" not in resumo
    linha = render_auto_entry([resumo])
    assert "/workspace/conclusao-relatorios-anuais-2026.md" in linha
    assert "Análise Comparativa: Relatórios Anuais 2026" in linha
    assert "Echo não propõe" not in linha


def test_deep_read_e_registrado_como_job_aberto_nao_como_conteudo():
    resumo = _resumo("deep_read", {"then": "leia a sintese"},
                     {"ok": True, "job_id": "3f2a1b9c", "title": "Leitura: Planos", "status": "queued"})
    assert resumo["async_job"] is True and resumo["then"] is True
