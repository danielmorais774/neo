"""``tool_find`` e ``tool_run`` — a porta única do agregador (§2.3 do plano).

``tool_find`` é leitura pura sobre o índice: devolve ``nome — resumo`` sem pagar
o schema de ninguém. É o que torna achável uma tool que mora em qualquer canto
do código.

``tool_run`` é o dispatcher genérico: uma função, um nome, funciona. É o
**fallback**, não o caminho padrão — sem schema tipado o provider não valida
nada e o modelo inventa argumento (histórico deste repo: ``entity_id`` da skill
HA, ``file_path`` do classificador, as 7 client tools fantasma). Por isso o
executor valida contra o schema do binding e devolve a assinatura inteira no
erro, e ``tool_find`` já entrega a assinatura junto do resultado.
"""

from shared import ToolParameter, ToolSchema

TOOL_INDEX_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="tool_find",
        description=(
            "Procura uma ferramenta no índice do Neo pelo nome ou pelo assunto e "
            "devolve a assinatura dela. Use quando souber QUE existe algo capaz de "
            "fazer o que o usuário pediu mas não souber o nome exato, ou antes de "
            "chamar tool_run. Não inventa ferramenta: só lista o que está registrado."
        ),
        router_summary="procura uma ferramenta pelo assunto e mostra a assinatura",
        module="tool_index",
        execution="server-side",
        timeout=10,
        parameters={
            "busca": ToolParameter(
                type="string",
                description="Nome parcial ou assunto (ex.: 'produto', 'fatura', 'whatsapp').",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="tool_run",
        description=(
            "Executa uma ferramenta do índice pelo nome único, passando os argumentos "
            "em `args`. Use SÓ quando a ferramenta não estiver disponível diretamente "
            "nesta conversa — se ela já aparece na sua lista de funções, chame-a "
            "direto (é validada e mais confiável). Confira a assinatura com tool_find "
            "antes: argumento errado aqui não é corrigido pelo sistema."
        ),
        router_summary="executa uma ferramenta do índice pelo nome",
        module="tool_index",
        execution="server-side",
        timeout=120,
        parameters={
            "nome": ToolParameter(
                type="string",
                description="Nome exato da ferramenta, como veio de tool_find.",
                required=True,
            ),
            "args": ToolParameter(
                type="object",
                description="Argumentos da ferramenta, no formato da assinatura dela.",
                required=False,
            ),
        },
    ),
]

TOOL_INDEX_TOOL_NAMES = frozenset(t.name for t in TOOL_INDEX_TOOLS)
