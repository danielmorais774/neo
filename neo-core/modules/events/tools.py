"""Tools ``event_*`` (docs/playlists-skill-plan.md §7.7).

Três tools, gated pelo ``skill_read`` da skill que declara ``eventos:`` — mesmo
mecanismo das ``data_*``. O prefixo ``event_`` entra em
``_SKILL_OWNED_TOOL_PREFIXES``: o classificador nunca as vê nem roteia para elas,
que é o que impede o bypass do cérebro (o mesmo que fazia a skill HA alucinar
``entity_id``).

A regra que define o subsistema está na descrição do ``event_subscribe``, e é
repetida no SKILL.md: **inscrição só existe grudada no que acabou de ser
tocado**. Sem aba e sem faixa, a tool recusa — é isso que impede um "toca um
AC/DC" de deixar gatilho armado para trás.
"""

from shared import ToolParameter, ToolSchema

EVENT_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="event_subscribe",
        description=(
            "Liga um gatilho declarado na skill à faixa concreta que você está "
            "colocando para tocar: quando ELA terminar, o Neo despacha a próxima da "
            "fila sozinho. Chame junto com o início da fila, passando `faixa` = o "
            "link/id da primeira música (a que você acabou de tirar da fila). NUNCA "
            "use para 'ficar de olho em música' em geral — sem faixa concreta a tool "
            "recusa, e um pedido avulso ('toca um AC/DC') não deve armar nada. "
            "`gatilho` é '<skill>.<nome>' como declarado em `eventos:` "
            "(ex.: 'playlists.fila_musica'). `alvo` é opcional: sem ele o Neo usa a "
            "aba onde ele mesmo está tocando."
        ),
        module="events",
        execution="server-side",
        timeout=20,
        parameters={
            "gatilho": ToolParameter(
                type="string",
                description="'<skill>.<nome do gatilho>' declarado em eventos:.",
                required=True,
            ),
            "alvo": ToolParameter(
                type="object",
                description=(
                    'Onde observar: {"site": "youtube", "tab_id": 42}. Omita para usar '
                    "a aba onde o Neo acabou de tocar."
                ),
                required=False,
            ),
            "faixa": ToolParameter(
                type="string",
                description=(
                    "Link ou id da faixa que dispara o gatilho ao terminar — a que "
                    "você está mandando tocar agora. Omita só se já estiver tocando "
                    "(aí o Neo usa a faixa da aba atual)."
                ),
                required=False,
            ),
            "minutos": ToolParameter(
                type="number",
                description="Validade da inscrição em minutos (padrão 120, teto 480).",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="event_unsubscribe",
        description=(
            "Desliga gatilho(s) armado(s) — 'para a fila', 'chega de tocar sozinho'. "
            "Sem argumento nenhum, encerra TODAS as inscrições do usuário."
        ),
        module="events",
        execution="server-side",
        timeout=20,
        parameters={
            "id": ToolParameter(
                type="string", description="Id da inscrição (de event_list).", required=False,
            ),
            "gatilho": ToolParameter(
                type="string",
                description="'<skill>.<nome>' — encerra todas as inscrições desse gatilho.",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="event_list",
        description=(
            "Mostra o que está armado agora: gatilho, aba observada, faixa esperada, "
            "quantos disparos, quando expira. Use para responder 'a fila ainda está "
            "rodando?' e antes de inscrever de novo."
        ),
        module="events",
        execution="server-side",
        timeout=15,
        parameters={
            "incluir_encerradas": ToolParameter(
                type="boolean",
                description="Inclui as já encerradas (histórico curto). Padrão false.",
                required=False,
            ),
        },
    ),
]

EVENT_TOOL_NAMES = frozenset(t.name for t in EVENT_TOOLS)
