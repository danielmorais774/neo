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
        name="deep_research",
        description=(
            "Pesquisa PROFUNDA na web em segundo plano (várias buscas + leitura de "
            "fontes + relatório com citações). Demora minutos — você é avisado ao "
            "terminar; diga ao usuário que vai avisar. Use SÓ quando o usuário pedir "
            "explicitamente uma pesquisa a fundo/relatório; para pergunta pontual/"
            "rápida use web_search."
        ),
        module="jobs",
        execution="server-side",
        timeout=30,
        parameters={
            "topic": ToolParameter(
                type="string",
                description="Tópico da pesquisa (claro e auto-contido)",
                required=True,
            ),
            "instructions": ToolParameter(
                type="string",
                description=(
                    "Direcionamento extra do usuário, COPIADO fielmente: exigências "
                    "(nº mínimo de referências, seções obrigatórias), ângulo, período, "
                    "formato. É contra isso que o relatório é validado no fim."
                ),
                required=False,
            ),
            "depth": ToolParameter(
                type="string",
                description=(
                    "Profundidade: quick (~5min, resposta enxuta, sem revisão), "
                    "standard (~20min, padrão), deep (~45min, tema amplo). Se o "
                    "usuário exigir número mínimo de referências, seções específicas "
                    "ou 'a fundo/completo', use standard ou deep — o quick não tem "
                    "ciclo de revisão para cumprir requisito."
                ),
                enum=["quick", "standard", "deep"],
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="browser_task",
        description=(
            "Executa uma tarefa num BROWSER real (Chromium no servidor) EM SEGUNDO "
            "PLANO: navegar, buscar, clicar, preencher, ler páginas com JavaScript. "
            "Demora de 30s a minutos — você é avisado ao terminar; diga ao usuário "
            "que vai avisar. A sessão é PERSISTENTE: logins ficam salvos e a página "
            "onde a última tarefa parou continua aberta — para continuar algo ('agora "
            "ordena por preço'), mande a nova ordem e mencione no context o que já "
            "foi feito. Use para sites interativos; para pergunta pontual use "
            "web_search e para ler uma página estática use web_fetch."
        ),
        module="jobs",
        execution="server-side",
        timeout=30,
        parameters={
            "task": ToolParameter(
                type="string",
                description=(
                    "A tarefa em linguagem natural, clara e auto-contida (site, "
                    "objetivo, critérios). O subagente não vê a conversa — inclua "
                    "tudo que ele precisa."
                ),
                required=True,
            ),
            "title": ToolParameter(
                type="string",
                description="Título curto do job para o usuário (ex: 'Buscar voos FOR→GRU')",
                required=True,
            ),
            "context": ToolParameter(
                type="string",
                description=(
                    "Contexto de continuação (opcional): o que a tarefa anterior "
                    "desta sessão fez/deixou na tela, se o usuário está continuando "
                    "algo (ex: 'a busca de teclados até R$300 já está aberta')."
                ),
                required=False,
            ),
            "session": ToolParameter(
                type="string",
                description=(
                    "Nome da sessão de browser (opcional; default é a sessão "
                    "persistente do usuário). Só use outro nome se o usuário pedir "
                    "um contexto separado."
                ),
                required=False,
            ),
            "max_steps": ToolParameter(
                type="integer",
                description="Teto de passos do subagente (opcional; default 40).",
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
