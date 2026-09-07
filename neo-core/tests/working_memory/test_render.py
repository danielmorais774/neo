"""Renderização do caderno: índice do prompt, tempo relativo, entrada automática.

O índice é a única parte que custa tokens em TODO turno — os testes aqui
garantem que ele carrega o que decide ("já apurei isso") e nunca o corpo.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from core.working_memory.render import (
    format_when,
    render_auto_entry,
    render_index,
    render_tail,
    stamp,
)


def _note(slug, summary, updated_at, title="t"):
    return SimpleNamespace(slug=slug, summary=summary, title=title, updated_at=updated_at)


# ── tempo ───────────────────────────────────────────────────────────────────

def test_stamp_converte_utc_para_brasilia():
    # 17:41 UTC = 14:41 em Brasília. O banco guarda naive-UTC; o modelo lê BRT.
    assert stamp(datetime(2026, 7, 31, 17, 41)) == "31/07 14:41"


def test_format_when_hoje_ontem_e_data():
    agora = datetime(2026, 7, 31, 17, 0)  # 14:00 BRT
    assert format_when(datetime(2026, 7, 31, 16, 0), agora) == "hoje 13:00"
    assert format_when(datetime(2026, 7, 30, 22, 2), agora) == "ontem 19:02"
    assert format_when(datetime(2026, 7, 20, 12, 0), agora) == "20/07 09:00"


def test_format_when_usa_o_dia_de_brasilia_nao_o_de_utc():
    # 01/08 01:00 UTC ainda é 31/07 22:00 em Brasília: para quem lê, é "hoje".
    # Comparar os dias em UTC diria "ontem" — foi esse tipo de naive-vs-aware
    # que quebrou o "há X" do seletor de sessões do desktop.
    agora = datetime(2026, 8, 1, 1, 30)
    assert format_when(datetime(2026, 8, 1, 1, 0), agora) == "hoje 22:00"


# ── índice ──────────────────────────────────────────────────────────────────

def test_render_index_traz_slug_tempo_e_resumo_nunca_o_corpo():
    agora = datetime(2026, 7, 31, 17, 0)
    notes = [
        _note("extrato-marco", "412 lançamentos, R$ 7.790,00 em débitos", datetime(2026, 7, 31, 16, 41)),
        _note("analise-q1", "comparativo jan-mar", datetime(2026, 7, 30, 22, 2)),
    ]
    text = render_index(notes, now=agora)
    assert "[[extrato-marco]] · hoje 13:41 · 412 lançamentos, R$ 7.790,00 em débitos" in text
    assert "[[analise-q1]] · ontem 19:02 · comparativo jan-mar" in text
    # As duas instruções que fazem a feature funcionar têm que estar lá.
    assert "wm_read" in text and "wm_write" in text
    # E a fronteira com o PKS, que é o risco R1.
    assert "pks_remember" in text


def test_render_index_vazio_quando_nao_ha_nota():
    assert render_index([]) == ""


def test_render_index_avisa_quando_podou():
    text = render_index([_note("n", "s", datetime(2026, 7, 31, 12, 0))], truncated=True)
    assert "ficaram de fora por espaço" in text


# ── corpo ───────────────────────────────────────────────────────────────────

def test_render_tail_corta_as_entradas_antigas_e_avisa():
    body = "# t\n> meta\n\n" + "\n\n".join(f"## 31/07 10:0{i}\nentrada {i}" for i in range(6))
    text, omitted = render_tail(body, max_entries=2)
    assert omitted == 4
    assert "entrada 5" in text and "entrada 0" not in text
    assert "full=true" in text
    assert text.startswith("# t")


def test_render_tail_devolve_tudo_quando_cabe():
    body = "# t\n\n## 31/07 10:00\núnica"
    text, omitted = render_tail(body, max_entries=4)
    assert omitted == 0 and text == body


# ── entrada automática (backstop §7) ────────────────────────────────────────

def test_render_auto_entry_resume_o_trabalho_pesado():
    trace = [
        {"tool": "neo_bash", "ok": True, "command": "python3 parse.py", "exit_code": 0,
         "stdout_preview": "412 linhas"},
        {"tool": "neo_write_file", "ok": True, "path": "/workspace/parse.py", "bytes": 812},
        {"tool": "file_read", "ok": False, "error": "arquivo sumiu"},
    ]
    files = [{"name": "relatorio.csv", "file_id": "abc"}]
    entry = render_auto_entry(trace, files)
    assert "rodei `python3 parse.py` (exit=0) → 412 linhas" in entry
    assert "escrevi /workspace/parse.py (812 bytes)" in entry
    assert "file_read: falhou (arquivo sumiu)" in entry
    assert "entreguei ao usuário: 'relatorio.csv'" in entry


def test_render_auto_entry_vazia_nao_vira_nota():
    assert render_auto_entry([], []) == ""
