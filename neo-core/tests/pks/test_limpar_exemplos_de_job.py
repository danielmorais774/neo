"""O script de limpeza tira só a continuação de job, nunca o exemplo legítimo."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "pks_limpar_exemplos_de_job",
    Path(__file__).resolve().parents[2] / "scripts" / "pks_limpar_exemplos_de_job.py",
)
limpeza = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(limpeza)


def test_remove_so_os_itens_de_continuacao():
    exemplos = [
        "[Continuação automática — não é mensagem do usuário. Uma tarefa sua em segundo plano terminou.] Job: 'Extrair questões do Portal para .txt'",
        "Abrir https://12axes.vercel.app/en e clicar em Discover my profile",
        "  [Continuação automática — não é mensagem do usuário.] Job: 'Ver cota do Claude'",
    ]
    limpo, removidos = limpeza.limpar({"examples": exemplos, "priority": "high"})
    assert limpo == {"examples": ["Abrir https://12axes.vercel.app/en e clicar em Discover my profile"], "priority": "high"}
    assert len(removidos) == 2


def test_texto_que_so_cita_a_marca_fica():
    assert not limpeza.sintetico("prefiro resposta curta depois de [Continuação automática]")
    assert limpeza.sintetico("[Continuação automática — x]")
