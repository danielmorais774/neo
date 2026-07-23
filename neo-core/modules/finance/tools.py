"""Tools fin_* — gerenciamento de finanças pessoais (skill ``financas``).

Backing da skill tool_backed ``financas``. NÃO entram no read set default do
cognitivo: só aparecem via tool gating, depois que o modelo leu a skill (que
ensina o fluxo brasileiro: cartão/fatura/parcelamento, Pix, categorias, etc.).
Prefixo ``fin_`` está em ``_SKILL_OWNED_TOOL_PREFIXES`` do router — o
classificador nunca roteia direto para elas.
"""

from shared import ToolParameter, ToolSchema

FINANCE_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="fin_add_transaction",
        description=(
            "Registra uma despesa ou receita no extrato do usuário. Crédito com "
            "installments>1 vira compra parcelada (cria o plano e as N parcelas, "
            "uma por fatura). Valores em reais (aceita '45,90')."
        ),
        module="finance",
        execution="server-side",
        timeout=15,
        parameters={
            "kind": ToolParameter(
                type="string", enum=["despesa", "receita"],
                description="Tipo do lançamento.", required=True,
            ),
            "amount": ToolParameter(
                type="string",
                description="Valor em reais (ex: '45,90', '1.234,56' ou número).",
                required=True,
            ),
            "description": ToolParameter(
                type="string", description="Descrição curta (ex: 'mercado do mês').",
                required=True,
            ),
            "category": ToolParameter(
                type="string",
                description="Categoria por nome (busca aproximada nas do usuário).",
                required=False,
            ),
            "payment_method": ToolParameter(
                type="string",
                enum=["pix", "debito", "credito", "boleto", "dinheiro", "ted",
                      "transferencia", "vale", "outro"],
                description="Método de pagamento.", required=False,
            ),
            "account": ToolParameter(
                type="string",
                description="Conta por nome (obrigatória exceto no crédito; usa a padrão se omitida).",
                required=False,
            ),
            "card": ToolParameter(
                type="string",
                description="Cartão de crédito por nome (obrigatório quando payment_method=credito).",
                required=False,
            ),
            "date": ToolParameter(
                type="string",
                description="Data (AAAA-MM-DD ou DD/MM/AAAA). Padrão: hoje.",
                required=False,
            ),
            "merchant": ToolParameter(
                type="string", description="Estabelecimento (ex: 'Assaí', 'iFood').",
                required=False,
            ),
            "installments": ToolParameter(
                type="integer",
                description="Número de parcelas (só crédito). Padrão 1 (à vista).",
                required=False,
            ),
            "notes": ToolParameter(
                type="string", description="Observações livres.", required=False,
            ),
            "create_category": ToolParameter(
                type="boolean",
                description="true para criar a categoria caso não exista (confirme com o usuário antes).",
                required=False,
            ),
            "confirm": ToolParameter(
                type="boolean",
                description=(
                    "Só use depois que a tool RECUSAR por duplicata E o usuário "
                    "confirmar que é um segundo lançamento de verdade."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="fin_transfer",
        description=(
            "Transferência entre contas do usuário (inclui saque/depósito com a "
            "conta 'Carteira') OU pagamento de fatura de cartão (informe card e "
            "opcionalmente invoice_month). Não é despesa: não conta nos gastos."
        ),
        module="finance",
        execution="server-side",
        timeout=15,
        parameters={
            "amount": ToolParameter(
                type="string",
                description=(
                    "Valor em reais. OBRIGATÓRIO na transferência entre contas. No "
                    "pagamento de fatura, OMITA quando o usuário não disser o valor "
                    "('paguei a fatura') — o sistema paga o total em aberto. NUNCA "
                    "invente um valor."
                ),
                required=False,
            ),
            "from_account": ToolParameter(
                type="string", description="Conta de origem (nome).", required=True,
            ),
            "to_account": ToolParameter(
                type="string",
                description="Conta de destino (nome) — para transferência entre contas.",
                required=False,
            ),
            "card": ToolParameter(
                type="string",
                description="Cartão (nome) — para pagamento de fatura.",
                required=False,
            ),
            "invoice_month": ToolParameter(
                type="string",
                description=(
                    "Qual fatura pagar: AAAA-MM OU relativo ('proximo','atual',"
                    "'anterior'). Padrão (omitido): a aberta mais antiga. Se o usuário "
                    "disser um mês ('a de agosto','adianta o próximo'), PREENCHA — dá "
                    "para adiantar fatura futura com saldo em aberto."
                ),
                required=False,
            ),
            "date": ToolParameter(
                type="string", description="Data (padrão: hoje).", required=False,
            ),
            "description": ToolParameter(
                type="string", description="Descrição opcional.", required=False,
            ),
            "confirm": ToolParameter(
                type="boolean",
                description=(
                    "Só use depois que a tool RECUSAR (duplicata ou valor acima da "
                    "fatura) E o usuário confirmar explicitamente que é isso mesmo."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="fin_statement",
        description=(
            "Extrato: lista lançamentos com filtros (período, conta, cartão, "
            "categoria, método, texto) + totais do recorte. Retorna ids curtos "
            "para correção via fin_edit_transaction. Padrão: mês atual."
        ),
        module="finance",
        execution="server-side",
        timeout=15,
        parameters={
            "start_date": ToolParameter(
                type="string", description="Início (AAAA-MM-DD). Padrão: dia 1 do mês atual.",
                required=False,
            ),
            "end_date": ToolParameter(
                type="string", description="Fim (AAAA-MM-DD). Padrão: hoje.", required=False,
            ),
            "account": ToolParameter(
                type="string", description="Filtrar por conta (nome).", required=False,
            ),
            "card": ToolParameter(
                type="string", description="Filtrar por cartão (nome).", required=False,
            ),
            "category": ToolParameter(
                type="string", description="Filtrar por categoria (inclui as filhas).",
                required=False,
            ),
            "kind": ToolParameter(
                type="string",
                enum=["despesa", "receita", "transf_saida", "transf_entrada",
                      "pagamento_fatura"],
                description="Filtrar por tipo.", required=False,
            ),
            "payment_method": ToolParameter(
                type="string", description="Filtrar por método (pix, credito, ...).",
                required=False,
            ),
            "search": ToolParameter(
                type="string", description="Busca em descrição/estabelecimento.",
                required=False,
            ),
            "limit": ToolParameter(
                type="integer", description="Máx. de linhas (padrão 30, teto 100).",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="fin_card_invoice",
        description=(
            "Fatura do cartão: itens do mês (compras + parcelas), total, status "
            "de pagamento, vencimento, limite disponível e parcelas futuras "
            "comprometidas."
        ),
        module="finance",
        execution="server-side",
        timeout=15,
        parameters={
            "card": ToolParameter(
                type="string", description="Cartão (nome).", required=True,
            ),
            "month": ToolParameter(
                type="string",
                description="Mês da fatura (AAAA-MM). Padrão: fatura atual.",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="fin_balances",
        description=(
            "Visão geral: saldo de cada conta, fatura aberta de cada cartão e "
            "resumo do mês (receitas, despesas, resultado). Use para 'quanto "
            "tenho?', 'como tô esse mês?'."
        ),
        module="finance",
        execution="server-side",
        timeout=15,
        parameters={},
    ),
    ToolSchema(
        name="fin_insights",
        description=(
            "Análise dos gastos: por categoria (com variação vs período "
            "anterior), top estabelecimentos, por método de pagamento, média "
            "diária, evolução mensal, comprometimento futuro (parcelas + "
            "recorrências) e orçamentos. Padrão: mês atual."
        ),
        module="finance",
        execution="server-side",
        timeout=20,
        parameters={
            "start_date": ToolParameter(
                type="string", description="Início (AAAA-MM-DD). Padrão: dia 1 do mês atual.",
                required=False,
            ),
            "end_date": ToolParameter(
                type="string", description="Fim (AAAA-MM-DD). Padrão: hoje.", required=False,
            ),
            "compare": ToolParameter(
                type="boolean",
                description="Comparar com o período anterior de mesma duração (padrão true).",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="fin_manage",
        description=(
            "Cadastro de finanças: criar/alterar/arquivar/listar contas, cartões, "
            "categorias, orçamentos e recorrências (assinaturas/contas fixas). "
            "fields é um JSON com os campos da entidade (ver skill)."
        ),
        module="finance",
        execution="server-side",
        timeout=15,
        parameters={
            "entity": ToolParameter(
                type="string",
                enum=["account", "card", "category", "budget", "recurring"],
                description="Tipo de cadastro.", required=True,
            ),
            "action": ToolParameter(
                type="string", enum=["create", "update", "archive", "list"],
                description="Ação.", required=True,
            ),
            "name": ToolParameter(
                type="string",
                description=(
                    "Nome OU id (8+ caracteres) do item alvo (update/archive). Use o "
                    "id quando houver dois itens com o mesmo nome — é a única forma de "
                    "dizer QUAL deles (o `list` mostra os ids)."
                ),
                required=False,
            ),
            "fields": ToolParameter(
                type="string",
                description=(
                    "JSON com os campos. Ex. card: {\"name\":\"Nubank\","
                    "\"limit\":\"5000\",\"closing_day\":28,\"due_day\":7}."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="fin_edit_transaction",
        description=(
            "Corrige ou exclui um lançamento pelo id (aceita o id curto do "
            "fin_statement, ou 'ultimo' para o mais recente). Exclusão de parcela "
            "pode ser só a parcela ou o plano inteiro (scope=plano)."
        ),
        module="finance",
        execution="server-side",
        timeout=15,
        parameters={
            "transaction_id": ToolParameter(
                type="string",
                description="Id (curto ou completo) ou 'ultimo'. Use target se não tiver o id.",
                required=False,
            ),
            "target": ToolParameter(
                type="string",
                description=(
                    "JSON descrevendo o lançamento quando você NÃO tem o id — NUNCA "
                    "invente um id nem recrie um lançamento para poder apagá-lo. "
                    "Campos: kind (despesa|receita|pagamento_fatura), card, account, "
                    "invoice_month (AAAA-MM), amount, date, description. Ex.: desfazer "
                    'o pagamento da fatura → {"kind":"pagamento_fatura","card":"Nubank",'
                    '"invoice_month":"2026-07"}. Se casar com vários, a tool lista.'
                ),
                required=False,
            ),
            "action": ToolParameter(
                type="string", enum=["update", "delete"],
                description="Ação.", required=True,
            ),
            "preview": ToolParameter(
                type="boolean",
                description=(
                    "true = NÃO executa: devolve o PLANO (o que será apagado e o efeito "
                    "em saldo/fatura). Use antes de qualquer exclusão que o usuário não "
                    "tenha detalhado, mostre o plano e peça confirmação."
                ),
                required=False,
            ),
            "fields": ToolParameter(
                type="string",
                description=(
                    "JSON com o que mudar (update): amount, description, category, "
                    "date, merchant, account, card, payment_method, notes."
                ),
                required=False,
            ),
            "scope": ToolParameter(
                type="string", enum=["lancamento", "plano"],
                description="Para delete de parcela: só este lançamento ou o plano todo.",
                required=False,
            ),
        },
    ),
]
