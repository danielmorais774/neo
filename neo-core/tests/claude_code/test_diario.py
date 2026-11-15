"""O diário do Claude: o que a janela Tarefas mostra, uma linha por passo."""

from __future__ import annotations

import os

from core.jobs import runner_claude
from core.jobs.runner_claude import Diario


def test_linha_curta_com_hora_e_sem_o_prefixo(tmp_path):
    d = Diario(str(tmp_path), "job-1")
    d.escrever("Claude: rodou `pytest`")
    d.escrever("Claude: 💬 Vou criar o arquivo.")
    linhas = (tmp_path / "job-1" / "trace.md").read_text().splitlines()
    assert linhas[0].endswith("  rodou `pytest`")
    assert linhas[1].endswith("  💬 Vou criar o arquivo.")
    assert linhas[0][2] == ":" and linhas[0][5] == ":"


def test_teto_corta_uma_vez_e_para(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_claude, "_DIARIO_MAX_BYTES", 200)
    d = Diario(str(tmp_path), "job-1")
    for i in range(50):
        d.escrever(f"passo {i} " + "x" * 20)
    texto = (tmp_path / "job-1" / "trace.md").read_text()
    assert texto.count("diário cortado") == 1
    assert len(texto.encode()) < 400


def test_guarda_so_os_mais_recentes(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_claude, "_DIARIOS_MANTIDOS", 3)
    for i in range(5):
        pasta = tmp_path / f"velho-{i}"
        pasta.mkdir()
        os.utime(pasta, (1000 + i, 1000 + i))
    Diario(str(tmp_path), "novo")
    restantes = sorted(p.name for p in tmp_path.iterdir())
    assert restantes == ["novo", "velho-3", "velho-4"]


def test_sem_pasta_configurada_nao_grava_nada(tmp_path):
    d = Diario("", "job-1")
    d.escrever("rodou `ls`")
    assert d.dir is None


def test_resumo_com_paragrafos_vira_uma_linha(tmp_path):
    """O "Fim:" do job decc8224 quebrou em cinco linhas no diário."""
    d = Diario(str(tmp_path), "job-1")
    d.escrever("Fim: waiting — O Claude parou.\n\nO Claude perguntou: libero?\n\nAté aqui: criei o hello.py.")
    linhas = (tmp_path / "job-1" / "trace.md").read_text().splitlines()
    assert len(linhas) == 1
    assert linhas[0].endswith("Fim: waiting — O Claude parou. O Claude perguntou: libero? Até aqui: criei o hello.py.")


def test_saida_de_comando_mantem_as_linhas_recuadas(tmp_path):
    d = Diario(str(tmp_path), "job-1")
    d.escrever("Claude: ↳ saída (2 linhas):\n  1 | 2 | 3\n*** O venceu! ***")
    linhas = (tmp_path / "job-1" / "trace.md").read_text().splitlines()
    assert linhas[0].endswith("  ↳ saída (2 linhas):")
    assert linhas[1] == " " * 10 + "│   1 | 2 | 3"
    assert linhas[2] == " " * 10 + "│ *** O venceu! ***"
