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
            "web_search e para ler uma página estática use web_fetch. Este browser "
            "roda numa VPS: NÃO tem tela nem caixa de som, e o usuário não vê nem "
            "ouve NADA do que acontece nele. Se o resultado só existe se ele VIR ou "
            "OUVIR (tocar música/vídeo, deixar uma aba aberta na frente dele, mostrar "
            "algo acontecendo), ou se ele citou o navegador ou a MÁQUINA dele ('no meu "
            "navegador', 'no meu Chrome', 'no meu macbook', 'nesse pc'), a tarefa é "
            "impossível aqui — use browser_local_task."
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
            "credentials": ToolParameter(
                type="string",
                description=(
                    "Rótulo(s) de credencial do COFRE que a tarefa vai precisar, "
                    "separados por vírgula (ex.: 'github-pessoal'). Use quando o site "
                    "exigir login ou um email: chame vault_list antes para ver o que "
                    "existe e escolher pela descrição. O subagente NUNCA vê a senha — "
                    "recebe um placeholder que só funciona no domínio certo. Se não "
                    "houver credencial para o site, não invente: diga ao usuário."
                ),
                required=False,
            ),
            "screenshots": ToolParameter(
                type="string",
                description=(
                    "Prints da tela: 'auto' (padrão — tira só se der erro, "
                    "automaticamente), 'always' (tira também quando dá certo; use "
                    "SEMPRE que o usuário pedir para VER a tela/o print/como ficou/"
                    "a prova do que você fez) ou 'never'. Os prints aparecem no "
                    "chat sozinhos quando a tarefa termina — não precisa fazer nada."
                ),
                enum=["auto", "always", "never"],
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="browser_local_task",
        description=(
            "Executa a tarefa no navegador REAL da máquina do usuário: na tela dele, "
            "com ele vendo cada passo e um botão PARE na página. Use nos TRÊS casos: "
            "(a) ele pediu o navegador dele ('no meu navegador', 'no meu Chrome', "
            "'quero acompanhar/ver acontecendo'); (b) ele nomeou a MÁQUINA dele como o "
            "lugar ('no meu macbook', 'nesse pc', 'aqui na minha máquina'); (c) o "
            "resultado só existe se ele VIR ou OUVIR — tocar música ou vídeo, deixar "
            "uma página aberta na frente dele, mostrar algo acontecendo (se houver "
            "tool específica para o pedido, ela vem antes de qualquer navegador). "
            "No caso (c) "
            "não é questão de preferência: o browser do servidor não tem tela nem som, "
            "e lá a tarefa é impossível. Fora esses três, o default continua sendo "
            "browser_task, que roda no servidor sem atrapalhar ninguém. Demora de 30s "
            "a minutos — você é avisado ao terminar; diga ao usuário que ele pode "
            "acompanhar e que você avisa no fim."
        ),
        module="jobs",
        execution="server-side",
        timeout=30,
        parameters={
            "task": ToolParameter(
                type="string",
                description=(
                    "A tarefa em linguagem natural, clara e auto-contida (site, "
                    "objetivo, critério de pronto). Quem executa não vê a conversa."
                ),
                required=True,
            ),
            "title": ToolParameter(
                type="string",
                description="Título curto do job para o usuário (ex: 'Emitir NFSe').",
                required=True,
            ),
            "start_url": ToolParameter(
                type="string",
                description=(
                    "URL onde começar (opcional). Só passe uma que o usuário citou "
                    "ou que você tem certeza — não invente endereço de resultado."
                ),
                required=False,
            ),
            "context": ToolParameter(
                type="string",
                description=(
                    "Contexto de continuação (opcional): o que já foi feito antes "
                    "nesta mesma tarefa."
                ),
                required=False,
            ),
            "max_steps": ToolParameter(
                type="integer",
                description="Teto de passos (opcional; padrão 25).",
                required=False,
            ),
            "confirm_sensitive_ok": ToolParameter(
                type="boolean",
                description=(
                    "true APENAS quando o usuário já autorizou explicitamente, nesta "
                    "conversa, uma ação que não desfaz (comprar, pagar, enviar, "
                    "excluir). Sem isso a tarefa para e pergunta antes de clicar."
                ),
                required=False,
            ),
            "continue_job": ToolParameter(
                type="string",
                description=(
                    "OBRIGATÓRIO quando uma tarefa anterior PAROU esperando "
                    "confirmação e o usuário acabou de autorizar: passe o job_id que "
                    "veio no aviso '⏸ PARADO ESPERANDO VOCÊ' e MAIS NADA (nem task, "
                    "nem title). A tarefa original, a aba onde ela parou e a "
                    "permissão daquele clique específico vêm todas de lá. NÃO "
                    "reescreva a tarefa: o agente já tinha chegado no lugar certo, e "
                    "recomeçar do zero perde o caminho andado."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="browser_screenshot",
        description=(
            "Tira um print AGORA da tela do browser do servidor e mostra no chat, "
            "sem rodar tarefa nova. Use quando o usuário quiser VER o que está na "
            "tela do browser neste momento ('me mostra', 'tira um print', 'como "
            "está aí?'), inclusive enquanto uma browser_task ainda roda. Só "
            "funciona se a sessão estiver aberta (alguma tarefa de browser "
            "recente); se estiver fria, a resposta diz isso."
        ),
        module="jobs",
        execution="server-side",
        timeout=45,
        parameters={
            "session": ToolParameter(
                type="string",
                description=(
                    "Nome da sessão de browser (opcional; default é a sessão "
                    "persistente do usuário — a mesma da browser_task)."
                ),
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
