"""Backstop determinístico do turno (docs/working-memory-plan.md §7).

A decisão do piso: só grava quando o turno fez trabalho CARO de refazer e o
modelo não anotou sozinho. Sem LLM — é a rede que sustenta a feature nos ~50%
de turnos em que o modelo ignora a instrução do prompt.
"""

from __future__ import annotations

from modules.desktop.ai_service import _WM_HEAVY_TOOLS, _autonote_entry


def test_turno_pesado_sem_wm_write_vira_nota():
    trace = [{"tool": "neo_bash", "ok": True, "command": "python3 p.py", "exit_code": 0}]
    entry = _autonote_entry(trace, [])
    assert entry and "python3 p.py" in entry


def test_modelo_que_anotou_sozinho_nao_ganha_nota_automatica():
    trace = [
        {"tool": "neo_bash", "ok": True, "command": "python3 p.py", "exit_code": 0},
        {"tool": "wm_write", "ok": True, "summary": "anotado"},
    ]
    assert _autonote_entry(trace, []) is None


def test_turno_leve_nao_vira_nota():
    # Refazer uma consulta de tempo é barato; a nota só sujaria o índice.
    trace = [
        {"tool": "get_weather", "ok": True, "summary": "28C"},
        {"tool": "search_pks", "ok": True, "summary": "nada"},
    ]
    assert _autonote_entry(trace, []) is None


def test_turno_sem_tool_nenhuma_nao_vira_nota():
    assert _autonote_entry([], []) is None
    assert _autonote_entry([], None) is None


def test_tool_pesada_que_falhou_nao_conta():
    trace = [{"tool": "neo_bash", "ok": False, "error": "boom"}]
    assert _autonote_entry(trace, []) is None


def test_arquivo_entregue_conta_mesmo_sem_tool_pesada():
    entry = _autonote_entry([], [{"name": "relatorio.csv", "file_id": "abc"}])
    assert entry and "relatorio.csv" in entry


def test_conjunto_de_tools_pesadas_cobre_os_caminhos_caros():
    # Se uma dessas sair do conjunto, o turno que mais precisa do caderno passa batido.
    assert {"neo_bash", "file_read", "deep_research", "run_command"} <= _WM_HEAVY_TOOLS
