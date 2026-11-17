"""Tools da agenda — o Neo criando e mexendo nos próprios horários (§9).

Ficam **fora do roteamento direto** (o prefixo ``schedule_`` já está na lista de
tools gated do ``intent_router``): quem agenda é o cognitivo, com a frase inteira
na mão. Rotear direto seria deixar o classificador inventar horário — e horário
inventado só é descoberto quando o despertador toca na hora errada.

A regra de ouro do ``schedule_create``: ``quando`` vem em PORTUGUÊS e quem
resolve é o servidor, que devolve o horário calculado para o Neo **confirmar em
voz alta**. Confirmar o horário resolvido transforma um erro de interpretação em
uma correção de meio segundo.
"""

from shared import ToolParameter, ToolSchema

SCHEDULE_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="schedule_create",
        description=(
            "Agenda uma tarefa para rodar sozinha no futuro (uma vez ou repetindo). "
            "Use quando o usuário pedir lembrete, rotina ou vigia: 'me lembra amanhã "
            "às 15h', 'todo dia útil às 8h me manda o resumo', 'a cada 30 min confere "
            "se o serviço caiu'. ESCOLHA O TIPO DE AÇÃO PELO CUSTO: 'notify' para "
            "aviso de texto fixo (não gasta LLM), 'tool' quando existe uma ferramenta "
            "que faz o trabalho, e 'prompt' só quando na hora for preciso PENSAR — o "
            "prompt é reenviado a você como se o usuário tivesse falado. Depois de "
            "criar, CONFIRME ao usuário o horário que voltou em 'proximo_local'."
        ),
        module="scheduler",
        execution="server-side",
        timeout=20,
        parameters={
            "nome": ToolParameter(
                type="string",
                description="Nome curto da tarefa (ex: 'Resumo da manhã', 'Vigia do TTS')",
                required=True,
            ),
            "quando": ToolParameter(
                type="string",
                description=(
                    "Quando rodar, em português: 'todo dia às 7h', 'toda segunda às 9h', "
                    "'todo dia útil às 8h', 'a cada 30 minutos', 'amanhã às 15h', "
                    "'todo dia 5 às 9h', 'todo fim do mês'. Também aceita cron de 5 campos."
                ),
                required=True,
            ),
            "acao_tipo": ToolParameter(
                type="string",
                description="prompt | tool | notify | job",
                required=True,
                enum=["prompt", "tool", "notify", "job"],
            ),
            "texto": ToolParameter(
                type="string",
                description=(
                    "Para acao_tipo='prompt': o pedido que será reenviado a você na hora. "
                    "Escreva como o usuário escreveria, auto-contido (quem lê não terá o "
                    "contexto desta conversa)."
                ),
                required=False,
            ),
            "tool": ToolParameter(
                type="string",
                description="Para acao_tipo='tool': o nome da ferramenta a disparar.",
                required=False,
            ),
            "arguments": ToolParameter(
                type="object",
                description="Para acao_tipo='tool': os argumentos da ferramenta.",
                required=False,
            ),
            "titulo": ToolParameter(
                type="string",
                description="Para acao_tipo='notify': título do aviso.",
                required=False,
            ),
            "corpo": ToolParameter(
                type="string",
                description="Para acao_tipo='notify': o texto do aviso, já pronto.",
                required=False,
            ),
            "job_kind": ToolParameter(
                type="string",
                description="Para acao_tipo='job': research | sandbox_task | browser_local | browser.",
                required=False,
            ),
            "job_spec": ToolParameter(
                type="object",
                description="Para acao_tipo='job': os argumentos do job (ex: {topic: '...'}).",
                required=False,
            ),
            "canal": ToolParameter(
                type="string",
                description="Onde entregar: chat (padrão), notch, both ou silent.",
                required=False,
                enum=["chat", "notch", "both", "silent"],
            ),
            "so_se_mudar": ToolParameter(
                type="boolean",
                description=(
                    "Só avisa quando o resultado for DIFERENTE do disparo anterior. "
                    "Use em vigia recorrente — é o que evita virar ruído."
                ),
                required=False,
            ),
            "sessao": ToolParameter(
                type="string",
                description=(
                    "Para prompt: 'nova' (padrão), 'propria' (a tarefa acumula a própria "
                    "conversa, útil para comparar com o resultado de ontem) ou 'ativa'."
                ),
                required=False,
                enum=["nova", "propria", "ativa"],
            ),
        },
    ),
    ToolSchema(
        name="schedule_list",
        description=(
            "Lista as tarefas agendadas do usuário (nome, quando roda, próxima execução, "
            "último resultado). Use quando ele perguntar o que está agendado."
        ),
        module="scheduler",
        execution="server-side",
        timeout=15,
        parameters={
            "ativas_apenas": ToolParameter(
                type="boolean",
                description="Só as ligadas (padrão: false, mostra todas).",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="schedule_update",
        description=(
            "Muda uma tarefa agendada já existente: o horário ('quando'), o texto do "
            "prompt, o nome ou o canal. Para ligar/desligar use schedule_pause/resume."
        ),
        module="scheduler",
        execution="server-side",
        timeout=20,
        parameters={
            "id": ToolParameter(type="string", description="ID da tarefa", required=True),
            "quando": ToolParameter(
                type="string", description="Novo horário em português (opcional).", required=False
            ),
            "nome": ToolParameter(type="string", description="Novo nome (opcional).", required=False),
            "texto": ToolParameter(
                type="string", description="Novo texto do prompt/aviso (opcional).", required=False
            ),
            "canal": ToolParameter(
                type="string", description="Novo canal de entrega (opcional).", required=False,
                enum=["chat", "notch", "both", "silent"],
            ),
        },
    ),
    ToolSchema(
        name="schedule_pause",
        description="Desliga uma tarefa agendada sem apagar (ela pode voltar depois).",
        module="scheduler",
        execution="server-side",
        timeout=15,
        parameters={"id": ToolParameter(type="string", description="ID da tarefa", required=True)},
    ),
    ToolSchema(
        name="schedule_resume",
        description="Religa uma tarefa agendada que estava pausada e recalcula o próximo horário.",
        module="scheduler",
        execution="server-side",
        timeout=15,
        parameters={"id": ToolParameter(type="string", description="ID da tarefa", required=True)},
    ),
    ToolSchema(
        name="schedule_cancel",
        description="Apaga uma tarefa agendada de vez. Confirme com o usuário antes.",
        module="scheduler",
        execution="server-side",
        timeout=15,
        parameters={"id": ToolParameter(type="string", description="ID da tarefa", required=True)},
    ),
    ToolSchema(
        name="schedule_run_now",
        description=(
            "Dispara uma tarefa agendada AGORA, fora de hora, sem mexer no horário dela. "
            "Use para testar ('roda aquilo agora pra eu ver')."
        ),
        module="scheduler",
        execution="server-side",
        timeout=15,
        parameters={"id": ToolParameter(type="string", description="ID da tarefa", required=True)},
    ),
]

SCHEDULE_TOOL_NAMES = frozenset(t.name for t in SCHEDULE_TOOLS)
