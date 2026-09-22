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
            "usuário que vai avisar quando concluir. NÃO fique esperando com neo_bash. "
            "Se a tarefa tem um passo DEPOIS (montar, formatar, entregar), descreva-o em "
            "`then`: ao fim do job você continua sozinho e termina a entrega."
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
            "then": ToolParameter(
                type="string",
                description=(
                    "Opcional: o que VOCÊ vai fazer quando o job terminar com sucesso, "
                    "escrito como ordem completa (ex.: 'leia /workspace/ocr/x/transcricao.txt "
                    "e entregue ao usuário um .txt com remetente, horário e mensagem'). "
                    "Com isto você continua sozinho ao fim do job e entrega pronto — sem o "
                    "usuário precisar pedir de novo."
                ),
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
    # Leitura delegada de documentos (docs/deep-read-plan.md).
    ToolSchema(
        name="deep_read",
        description=(
            "LEITURA DELEGADA de documentos grandes ou de VÁRIOS documentos (PDF, DOCX, PPTX, "
            "planilha, TXT/MD/HTML) que já estão no seu sandbox. Subagentes leem CADA documento "
            "por inteiro, em paralelo, procurando exatamente o que você pedir; cada um vira "
            "`notas/<documento>.md` (análise + citações literais conferidas no texto, com página) "
            "e todos entram num `sintese.md` com quadro comparativo. Nada disso passa pelo seu "
            "contexto nem pelo seu caderno: você recebe só a pasta e o status. Use SEMPRE que a "
            "tarefa for analisar, comparar ou extrair algo de documentos longos (dezenas de "
            "páginas) ou de mais de dois arquivos — não `neo_read_file`/`cat`/`grep` no texto "
            "inteiro, que enche seu contexto, trunca e perde partes. Antes: liste a pasta de ORIGEM "
            "(a que o usuário citou) e garanta que TODOS os documentos dela estão no sandbox (sobem com "
            "request_user_file stage=true). Chame UMA vez, com todos, e avise o usuário que vai "
            "continuar sozinho ao terminar. Faltou algo numa leitura que já existe? Use `extend` — "
            "não abra outra. Leva de ~30 s a alguns minutos."
        ),
        module="jobs",
        execution="server-side",
        timeout=60,
        parameters={
            "files": ToolParameter(
                type="array",
                items={"type": "string"},
                description=(
                    "TODOS os documentos da tarefa: caminhos no seu sandbox "
                    "(ex.: '/workspace/files/Plano_X.pdf') ou file_ids do catálogo — o original, não "
                    ".txt extraído antes. Nunca caminho do disco do usuário (/Users/...): traga antes "
                    "com request_user_file. Com `extend`, só os documentos NOVOS."
                ),
                required=False,
            ),
            "questions": ToolParameter(
                type="array",
                items={"type": "string"},
                description=(
                    "O que procurar em CADA documento, um critério por item, específico "
                    "(ex.: 'propostas que aumentam a carga tributária: impostos novos, alíquotas "
                    "maiores, fim de isenções'). Uma por critério que o USUÁRIO pediu, com as palavras "
                    "dele — não troque por eixos genéricos. Viram as seções das notas. Até 12. Com "
                    "`extend`, só as perguntas NOVAS (as antigas são mantidas)."
                ),
                required=False,
            ),
            "goal": ToolParameter(
                type="string",
                description="Objetivo da análise em uma frase (ex.: 'comparar os relatórios anuais 2026'). Opcional com `extend`.",
                required=False,
            ),
            "title": ToolParameter(
                type="string",
                description="Título curto da leitura para o usuário (vira o nome da pasta). Opcional com `extend`.",
                required=False,
            ),
            "source_dir": ToolParameter(
                type="string",
                description=(
                    "A pasta do disco do USUÁRIO de onde os documentos vieram, quando ele citou uma "
                    "(ex.: '/Users/daniel/Documents/contratos'). A leitura é conferida contra a listagem "
                    "real dela — liste com run_command antes. 'nenhuma' se a pasta citada não é a origem."
                ),
                required=False,
            ),
            "excluded": ToolParameter(
                type="array",
                items={"type": "string"},
                description="Documentos da source_dir que o USUÁRIO mandou deixar de fora (nomes). Não use para esconder falta.",
                required=False,
            ),
            "extend": ToolParameter(
                type="string",
                description=(
                    "Para CORRIGIR ou AMPLIAR uma leitura que já existe ('faltou o X', 'inclui Y', "
                    "'analisa também Z'): o job_id ou a pasta /workspace/analises/... dela. Mesma pasta, "
                    "mesmas perguntas, só o novo é lido, e a conclusão já entregue é ATUALIZADA."
                ),
                required=False,
            ),
            "instructions": ToolParameter(
                type="string",
                description=(
                    "Opcional: critério de julgamento do usuário — o que conta e o que não conta "
                    "(ex.: 'promessa sem fonte de custeio conta como irrealista; diagnóstico não é proposta')."
                ),
                required=False,
            ),
            "then": ToolParameter(
                type="string",
                description=(
                    "O que VOCÊ faz quando a leitura terminar, como ordem completa (ex.: 'leia a "
                    "sintese.md desta leitura, escreva a conclusão comparativa em "
                    "/workspace/conclusao-planos-2026.md citando as páginas das notas e entregue com "
                    "file_save'). A pasta exata chega junto do resultado. Sem isto, o usuário só é "
                    "avisado e a conclusão espera ele pedir."
                ),
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
            "Executa a tarefa num navegador na MÁQUINA DO USUÁRIO. Por padrão SEM "
            "JANELA (headless): não atrapalha nada do que ele está fazendo, e usa a "
            "conexão de internet dele. Com janela — na tela dele, vendo cada passo e "
            "com botão PARE — só quando ele quiser VER. "
            "PREFIRA ESTA a `browser_task` quando o site recusa o servidor: o "
            "navegador do servidor sai por um IP de datacenter fora do Brasil, e WAF "
            "de CDN (403/Access Denied) e buscadores (CAPTCHA) recusam esse IP. Se um "
            "`web_fetch` voltou com `blocked_origin`, este é o próximo passo — e "
            "repetir no servidor não é. Use também quando: (a) ele pediu o navegador "
            "dele; (b) ele nomeou a MÁQUINA dele como o lugar ('no meu macbook', "
            "'nesse pc'); (c) o resultado só existe se ele VIR ou OUVIR — tocar música "
            "ou vídeo, deixar uma página aberta na frente dele (aí é com janela, e não "
            "é preferência: o servidor não tem tela nem som). "
            "NÃO use para baixar arquivo cuja URL você já tem — `web_fetch` diz se o "
            "arquivo está acessível, e navegador é o pior jeito de pegar binário. "
            "NÃO use quando o `web_fetch` disse `not_found`: a URL está errada e "
            "navegador nenhum conserta endereço. Demora de 30s a minutos — você é "
            "avisado ao terminar."
        ),
        module="jobs",
        execution="server-side",
        timeout=30,
        parameters={
            "visible": ToolParameter(
                type="boolean",
                description=(
                    "true = abrir na TELA do usuário (ele vê cada passo). Só quando "
                    "ele quiser acompanhar, ou quando o resultado depende de ele ver "
                    "ou ouvir. O padrão (false) roda sem janela na mesma máquina — "
                    "mesmo IP, sem atrapalhar o que ele está fazendo."
                ),
                required=False,
            ),
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
