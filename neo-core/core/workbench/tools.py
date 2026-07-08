"""Tools do Workbench — o ambiente de execução do PRÓPRIO Neo.

Prefixo ``neo_`` deixa explícito para o modelo (e para o router) que estas rodam
na MÁQUINA DO NEO (sandbox server-side), nunca no desktop do usuário — que tem as
tools ``run_command``/``write_file`` (client-side). Ver
``docs/workbench-background-jobs-plan.md``.

F1: só as tools síncronas (foreground). ``neo_bash_background`` + ``job_*`` entram
na F2 junto com o JobManager.
"""

from shared import ToolParameter, ToolSchema

WORKBENCH_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="neo_bash",
        description=(
            "Executa um comando de shell no SEU PRÓPRIO ambiente de execução (o "
            "sandbox do Neo — Linux, isolado, com internet e /workspace "
            "persistente). Use quando não existe uma tool específica e você precisa "
            "computar, processar dados, rodar scripts ou instalar pacotes para "
            "resolver a tarefa. NÃO é a máquina do usuário (para isso use "
            "run_command). Comandos longos podem estourar o timeout — para trabalho "
            "demorado prefira rodar em background (disponível em breve)."
        ),
        module="workbench",
        execution="server-side",
        # Hard wall for a foreground call (the global executor kills execute() at
        # this). Long work must go to neo_bash_background, not raise this. The
        # WorkbenchExecutor caps the sandbox timeout below this so the sandbox's own
        # `timeout` fires first (clean kill inside the container).
        timeout=150,
        parameters={
            "command": ToolParameter(
                type="string",
                description="Comando de shell completo (ex: 'python3 script.py', 'pip install pandas', 'curl -s URL | jq .')",
                required=True,
            ),
            "cwd": ToolParameter(
                type="string",
                description="Diretório de trabalho relativo a /workspace (padrão: raiz do workspace)",
                required=False,
            ),
            "timeout": ToolParameter(
                type="integer",
                description="Timeout em segundos (padrão: 60, máx ~120 no foreground; para mais, use neo_bash_background)",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="neo_write_file",
        description=(
            "Escreve um arquivo no SEU workspace (/workspace, sandbox do Neo). Cria "
            "os diretórios pais automaticamente. Use para salvar scripts e projetos "
            "que você mesmo cria. É o SEU ambiente, não o do usuário."
        ),
        module="workbench",
        execution="server-side",
        timeout=30,
        parameters={
            "path": ToolParameter(
                type="string",
                description="Caminho do arquivo (relativo a /workspace ou absoluto sob /workspace)",
                required=True,
            ),
            "content": ToolParameter(
                type="string",
                description="Conteúdo completo do arquivo",
                required=True,
            ),
        },
    ),
    ToolSchema(
        name="neo_read_file",
        description=(
            "Lê um arquivo do SEU workspace (/workspace, sandbox do Neo). Use para "
            "reler scripts/resultados que você criou. Não acessa a máquina do usuário."
        ),
        module="workbench",
        execution="server-side",
        timeout=30,
        parameters={
            "path": ToolParameter(
                type="string",
                description="Caminho do arquivo (relativo a /workspace ou absoluto sob /workspace)",
                required=True,
            ),
        },
    ),
    ToolSchema(
        name="neo_list_files",
        description=(
            "Lista arquivos e diretórios do SEU workspace (/workspace, sandbox do "
            "Neo). Use para ver o que você já criou antes e reaproveitar."
        ),
        module="workbench",
        execution="server-side",
        timeout=30,
        parameters={
            "path": ToolParameter(
                type="string",
                description="Diretório a listar (relativo a /workspace; padrão: raiz)",
                required=False,
            ),
        },
    ),
]
