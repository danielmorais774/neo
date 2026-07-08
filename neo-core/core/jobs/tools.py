"""Tools de jobs em background — oferecidas inline ao núcleo cognitivo.

``neo_bash_background`` enfileira um comando no sandbox e volta na hora com um
``job_id`` (não bloqueia a conversa); quando terminar, o Neo avisa sozinho. As
``job_*`` consultam/controlam esses jobs. Prefixos ``neo_``/``job_`` mantêm tudo
fora do roteamento direto (só o cognitivo aciona). Ver
``docs/workbench-background-jobs-plan.md``.
"""

from shared import ToolParameter, ToolSchema

JOB_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="neo_bash_background",
        description=(
            "Roda um comando no SEU sandbox EM SEGUNDO PLANO e devolve um job_id na "
            "hora, sem travar a conversa. Use para trabalho DEMORADO (baixar/processar "
            "arquivos grandes, treinar/converter algo, esperar um tempo) — você é "
            "avisado automaticamente quando terminar e aí responde ao usuário. Diga ao "
            "usuário que vai avisar quando concluir. NÃO fique esperando com neo_bash."
        ),
        module="jobs",
        execution="server-side",
        timeout=30,
        parameters={
            "command": ToolParameter(
                type="string",
                description="Comando de shell a rodar em background no sandbox do Neo",
                required=True,
            ),
            "title": ToolParameter(
                type="string",
                description="Título curto do job para o usuário (ex: 'Converter CSV', 'Baixar dataset')",
                required=True,
            ),
            "cwd": ToolParameter(
                type="string",
                description="Diretório de trabalho relativo a /workspace (opcional)",
                required=False,
            ),
            "timeout": ToolParameter(
                type="integer",
                description="Timeout do job em segundos (opcional; padrão 1800 = 30min)",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="job_status",
        description="Consulta o estado de um job em background (fila/rodando/concluído/falhou) e o resumo do resultado.",
        module="jobs",
        execution="server-side",
        timeout=15,
        parameters={
            "job_id": ToolParameter(type="string", description="ID do job", required=True),
        },
    ),
    ToolSchema(
        name="job_list",
        description="Lista os jobs em background recentes do usuário (título, estado, quando).",
        module="jobs",
        execution="server-side",
        timeout=15,
        parameters={
            "only_active": ToolParameter(
                type="boolean",
                description="Se true, mostra só os que estão na fila ou rodando",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="job_logs",
        description="Lê a saída (logs) de um job em background — útil enquanto roda ou para explicar o que aconteceu.",
        module="jobs",
        execution="server-side",
        timeout=20,
        parameters={
            "job_id": ToolParameter(type="string", description="ID do job", required=True),
            "tail": ToolParameter(
                type="integer",
                description="Quantidade de bytes finais do log a retornar (opcional)",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="job_cancel",
        description="Cancela um job em background que ainda está na fila ou rodando.",
        module="jobs",
        execution="server-side",
        timeout=15,
        parameters={
            "job_id": ToolParameter(type="string", description="ID do job", required=True),
        },
    ),
]

JOB_TOOL_NAMES = frozenset(t.name for t in JOB_TOOLS)
