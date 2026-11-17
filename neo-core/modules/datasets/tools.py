"""Tools dos cadastros generativos (docs/datasets-crud-plan.md §6).

Seis tools GENÉRICAS parametrizadas por ``dataset`` cobrem 1 ou 100 cadastros —
o precedente é ``service_request(service=…)`` do Service Gateway. A alternativa
(registrar ``produtos_create``, ``produtos_list``, … em runtime) explodiria o
schema no prompt e esbarraria no ``ToolRegistry.register``, que recusa duplicata.

Nenhuma delas entra no read set do cognitivo: são destravadas pelo ``skill_read``
de uma skill que declara ``datasets:`` (tool gating). O prefixo ``data_`` está em
``_SKILL_OWNED_TOOL_PREFIXES`` — o classificador nunca as vê nem roteia para elas,
que é o que impede o bypass do cérebro (o mesmo que fazia a skill HA alucinar
``entity_id``).
"""

from shared import ToolParameter, ToolSchema

_DATASET_PARAM = ToolParameter(
    type="string",
    description=(
        "Slug do cadastro, exatamente como aparece no bloco '### Cadastro `x`' "
        "que você leu na skill (ex.: 'produtos'). Não invente nome."
    ),
    required=True,
)

DATA_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="data_schema",
        description=(
            "Mostra os campos de um cadastro (nome, tipo, obrigatório, opções válidas). "
            "Use quando precisar conferir a estrutura antes de gravar, ou quando o "
            "cadastro não veio descrito na skill que você leu."
        ),
        module="datasets",
        execution="server-side",
        timeout=15,
        parameters={"dataset": _DATASET_PARAM},
    ),
    ToolSchema(
        name="data_list",
        description=(
            "Consulta linhas de um cadastro. Use para responder 'quanto', 'quais', "
            "'tem X?', 'me mostra os Y'. `busca` faz busca textual solta nos campos de "
            "texto; `filtro` é um objeto {campo: valor} para igualdade, ou "
            '{campo: {"op": "gte", "valor": 10}} para comparação (op: eq, ne, lt, lte, '
            'gt, gte, contem, em, nulo). NUNCA passe SQL aqui — para análise livre '
            "(soma, agrupamento) use sql_query_ro."
        ),
        module="datasets",
        execution="server-side",
        timeout=30,
        parameters={
            "dataset": _DATASET_PARAM,
            "busca": ToolParameter(
                type="string",
                description="Texto livre procurado nos campos de busca do cadastro.",
                required=False,
            ),
            "filtro": ToolParameter(
                type="object",
                description=(
                    'Filtro estruturado. Ex.: {"categoria": "caneca"} ou '
                    '{"estoque": {"op": "lte", "valor": 5}}.'
                ),
                required=False,
            ),
            "ordem": ToolParameter(
                type="string",
                description="'<campo> asc|desc' (ex.: 'preco desc'). Omita para o padrão do cadastro.",
                required=False,
            ),
            "limite": ToolParameter(
                type="number", description="Máximo de linhas (padrão 25, teto 200).", required=False,
            ),
            "pagina": ToolParameter(
                type="number", description="Página do resultado, começando em 1.", required=False,
            ),
        },
    ),
    ToolSchema(
        name="data_get",
        description=(
            "Lê UMA linha de um cadastro pelo id (o `id` que veio de data_list ou "
            "data_create). Use quando já sabe qual linha quer."
        ),
        module="datasets",
        execution="server-side",
        timeout=15,
        parameters={
            "dataset": _DATASET_PARAM,
            "id": ToolParameter(type="string", description="UUID da linha.", required=True),
        },
    ),
    ToolSchema(
        name="data_create",
        description=(
            "Cria uma linha num cadastro. `campos` é um objeto {campo: valor} com os "
            "campos do cadastro — valores de dinheiro aceitam formato BR ('49,90'). "
            "O retorno traz `conferido` com a linha RELIDA DO BANCO: só afirme ao "
            "usuário que cadastrou se `conferido` vier preenchido. Nunca invente valor "
            "que o usuário não disse — pergunte."
        ),
        module="datasets",
        execution="server-side",
        timeout=30,
        parameters={
            "dataset": _DATASET_PARAM,
            "campos": ToolParameter(
                type="object",
                description='Valores da linha. Ex.: {"nome": "Caneca preta", "preco": "49,90"}.',
                required=True,
            ),
        },
    ),
    ToolSchema(
        name="data_update",
        description=(
            "Altera uma linha existente. Envie em `campos` SÓ o que muda — o resto "
            "fica como está. O retorno traz `conferido` relido do banco; sem ele, não "
            "afirme que alterou."
        ),
        module="datasets",
        execution="server-side",
        timeout=30,
        parameters={
            "dataset": _DATASET_PARAM,
            "id": ToolParameter(type="string", description="UUID da linha.", required=True),
            "campos": ToolParameter(
                type="object", description="Só os campos que mudam.", required=True,
            ),
        },
    ),
    ToolSchema(
        name="data_delete",
        description=(
            "Exclui uma linha (exclusão reversível). SEM `confirmar` a tool NÃO apaga: "
            "devolve um preview do que sairia — mostre ao usuário e só chame de novo "
            "com confirmar=true depois que ELE confirmar em palavras. `confirmar` não é "
            "seu para decidir."
        ),
        module="datasets",
        execution="server-side",
        timeout=30,
        parameters={
            "dataset": _DATASET_PARAM,
            "id": ToolParameter(type="string", description="UUID da linha.", required=True),
            "confirmar": ToolParameter(
                type="boolean",
                description="true só depois de o usuário confirmar a exclusão.",
                required=False,
            ),
        },
    ),
]

#: Meta-tool: cria/altera a ESTRUTURA de um cadastro. Fica fora de DATA_TOOLS
#: porque é destravada só pela skill `criar-cadastro` — o cérebro não deve poder
#: inventar tabela no meio de uma conversa sobre outra coisa.
DATA_DEFINE_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="data_define",
        description=(
            "Cria um cadastro novo (tabela real no banco + tela no painel) ou "
            "acrescenta campos a um existente. `campos` é uma lista de objetos "
            '{"name","type","label","required","options"}. Tipos: texto, texto_longo, '
            "inteiro, numero, dinheiro, booleano, data, data_hora, opcao (exige "
            "options), multi_opcao, relacao (exige dataset), arquivo, json. "
            "Pergunte ao usuário o que ficou ambíguo ANTES de chamar — mudar tipo de "
            "campo depois não é automático. Só ACRESCENTA campo; remover é pelo painel."
        ),
        module="datasets",
        execution="server-side",
        timeout=60,
        parameters={
            "slug": ToolParameter(
                type="string",
                description="Nome curto em minúsculas com underscore (ex.: 'produtos').",
                required=True,
            ),
            "titulo": ToolParameter(
                type="string", description="Rótulo do cadastro no painel (ex.: 'Produtos').", required=False,
            ),
            "descricao": ToolParameter(
                type="string", description="Uma frase: para que serve este cadastro.", required=False,
            ),
            "campos": ToolParameter(
                type="array", description="Lista de definições de campo.", required=True,
            ),
            "modo": ToolParameter(
                type="string",
                description="'criar' (padrão) ou 'acrescentar' para adicionar campos a um cadastro existente.",
                required=False,
                enum=["criar", "acrescentar"],
            ),
        },
    ),
]

DATA_TOOL_NAMES = frozenset(t.name for t in DATA_TOOLS)
DATA_DEFINE_TOOL_NAMES = frozenset(t.name for t in DATA_DEFINE_TOOLS)
ALL_DATASET_TOOLS = DATA_TOOLS + DATA_DEFINE_TOOLS
