"""O sandbox recusa comando que cita o disco do usuário.

Sessão d9f9417b (2026-09-20). O usuário: "cria a pasta, voce vai ter que baixar
os pdfs na internet". O trilho SERVER_TOOL abriu três `sandbox_task` mirando o
Desktop dele — de dentro de um container Linux na VPS:

    mkdir -p /Users/daniel/Desktop/teste-de-analise/relatorios-anuais
      → mkdir: cannot create directory '/Users': Permission denied   (×2)
    ls -la /Users/daniel/Desktop/teste-de-analise/relatorios-anuais/
      → No such file or directory

Falha garantida, e cara: cada um virou job, entrou na fila, rodou e falhou.
Nenhum apareceu no log da sessão — só no banco. Do lado do usuário, o Neo não
fez nada e não disse por quê.

A guarda é estreita de propósito (barrar comando bom é pior que deixar passar um
ruim) e a mensagem de recusa é metade do conserto: sem ela o modelo repete o
mesmo comando, que foi exatamente o que aconteceu três vezes.
"""

import pytest

from core.workbench.user_paths import refusal_for, user_machine_paths


# ── o que a sessão mandou ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p /Users/daniel/Desktop/teste-de-analise/relatorios-anuais",
        "mkdir -p /Users/daniel/Desktop/teste-de-analise/relatorios-anuais && echo 'Pasta criada'",
        "ls -la /Users/daniel/Desktop/teste-de-analise/relatorios-anuais/",
        "cp relatorio.pdf /Users/daniel/Downloads/",
        "curl -sL https://x/y.pdf -o ~/Desktop/y.pdf",
        r"copy arquivo.txt C:\Users\daniel\Desktop\arquivo.txt",
        "cat C:/Users/daniel/Documents/nota.md",
    ],
)
def test_comando_no_disco_do_usuario_e_recusado(command):
    assert refusal_for(command) is not None


def test_a_recusa_nomeia_o_caminho_e_ensina_a_ponte():
    recusa = refusal_for("mkdir -p /Users/daniel/Desktop/teste-de-analise")
    assert "/Users/daniel" in recusa
    # O modelo precisa saber PARA ONDE ir, não só que errou.
    assert "file_save" in recusa
    assert "save_generated_file" in recusa
    assert "/workspace" in recusa


def test_lista_os_caminhos_achados_sem_repetir():
    achados = user_machine_paths(
        "cp /Users/daniel/a.txt /Users/daniel/b.txt && ls ~/Desktop"
    )
    assert achados == ["/Users/daniel", "~/Desktop"]


# ── e o trabalho legítimo de sandbox continua passando ────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p /workspace/planos && cd /workspace/planos",
        "curl -sL https://exemplo.com/plano.pdf -o /workspace/plano.pdf",
        "python3 script.py > saida.txt",
        "ls -la",
        "cd /tmp && tar -xzf pacote.tgz",
        "grep -r 'Users' /workspace/notas",        # a palavra solta não é caminho
        "echo '/Usersomething' ",                   # não é /Users/<algo>
        "ls ~/.cache",                              # `~` genérico não conta
        "ls /home/appuser/dados",                   # /home existe no container
        "sed -i 's|/Users|/workspace|' migra.sh",   # sem <algo> depois da barra
    ],
)
def test_comando_de_sandbox_passa(command):
    assert refusal_for(command) is None, command


def test_comando_vazio_nao_quebra():
    assert refusal_for("") is None
    assert user_machine_paths("") == []


# ── a guarda no ponto único: WorkbenchSandbox.run ─────────────────────────
#
# `neo_bash` (turno) e `sandbox_task` (job) chegam os dois aqui. Nenhum dos dois
# deve alcançar o Docker com um caminho do disco dele.


@pytest.mark.asyncio
async def test_run_recusa_sem_tocar_no_docker():
    from core.workbench.sandbox import WorkbenchSandbox

    sandbox = WorkbenchSandbox()
    # Se a guarda deixar passar, isto tenta falar com o socket do Docker e o
    # teste quebra de um jeito bem diferente de uma recusa limpa.
    sandbox._docker_socket = "/caminho/que/nao/existe.sock"

    res = await sandbox.run("mkdir -p /Users/daniel/Desktop/teste-de-analise")

    assert res.ok is False
    assert res.exit_code == 126
    assert "máquina do usuário" in res.stderr.lower() or "MÁQUINA DO USUÁRIO" in res.stderr
    assert "file_save" in res.stderr


@pytest.mark.asyncio
async def test_run_recusa_o_comando_embrulhado_do_job():
    """`_run_sandbox_task` embrulha o comando em `( … ) > log 2>&1` antes de
    chamar o run — a guarda tem que enxergar através do embrulho."""
    from core.workbench.sandbox import WorkbenchSandbox

    sandbox = WorkbenchSandbox()
    sandbox._docker_socket = "/caminho/que/nao/existe.sock"
    embrulhado = (
        "mkdir -p '/workspace/.jobs' && "
        "( mkdir -p /Users/daniel/Desktop/teste-de-analise/relatorios-anuais ) "
        "> '/workspace/.jobs/abc.log' 2>&1"
    )

    res = await sandbox.run(embrulhado)

    assert res.exit_code == 126
