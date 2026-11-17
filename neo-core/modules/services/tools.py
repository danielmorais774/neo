"""Tools do Service Gateway expostas ao cognitivo (docs/service-gateway-dsl-plan.md §3.3).

``service_list``/``service_docs`` são leitura pura (descoberta). ``service_request``
é gated pela POLÍTICA da conexão (não pela presença da tool): leitura costuma
passar direto, escrita cai em confirmação. ``service_confirm`` executa uma
escrita já aprovada pelo usuário.

Regra de ouro para o modelo: o host e a credencial são resolvidos pelo gateway.
Você NUNCA informa URL absoluta, token, header de Authorization nem cookie.
"""

from shared import ToolParameter, ToolSchema

SERVICE_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="service_list",
        description=(
            "Lista os serviços HTTP cadastrados que você pode chamar via service_request "
            "(Home Assistant, GitHub, etc.), com um resumo da política de cada um. Use "
            "ANTES de service_request para descobrir o slug correto do serviço e o que é "
            "permitido. Não inventa serviço fora desta lista."
        ),
        module="services",
        execution="server-side",
        timeout=10,
        parameters={},
    ),
    ToolSchema(
        name="service_docs",
        description=(
            "Mostra a documentação/política de um serviço cadastrado (docs_url, o que é "
            "leitura livre, o que exige confirmação, o que é bloqueado). Use para montar o "
            "path e o body corretos antes de chamar service_request."
        ),
        module="services",
        execution="server-side",
        timeout=10,
        parameters={
            "service": ToolParameter(
                type="string",
                description="Slug do serviço (ex.: 'homeassistant'), como aparece em service_list",
                required=True,
            ),
            "query": ToolParameter(
                type="string",
                description="Opcional: o que você procura (ex.: 'ligar luz')",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="service_request",
        description=(
            "Executa UMA requisição HTTP contra um serviço cadastrado quando não existe "
            "tool dedicada para o que o usuário pediu. Informe apenas: service (slug), "
            "method, path RELATIVO (ex.: /api/states) e, quando houver, query/body. O "
            "gateway resolve host, autenticação e certificados — NUNCA passe URL absoluta, "
            "token ou header de Authorization. Leitura (GET) costuma rodar direto; escrita "
            "pode voltar como 'pending_confirmation' — nesse caso confirme com o usuário e "
            "chame service_confirm. O resultado traz o status HTTP REAL: só afirme sucesso "
            "se status for 2xx (nunca presuma)."
        ),
        module="services",
        execution="server-side",
        timeout=40,
        parameters={
            "service": ToolParameter(
                type="string",
                description="Slug do serviço cadastrado (veja service_list)",
                required=True,
            ),
            "method": ToolParameter(
                type="string",
                description="Método HTTP",
                enum=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                required=True,
            ),
            "path": ToolParameter(
                type="string",
                description="Path RELATIVO começando com '/' (ex.: /api/services/light/turn_on). Sem host, sem querystring aqui.",
                required=True,
            ),
            "query": ToolParameter(
                type="object",
                description="Opcional: parâmetros de querystring como objeto ({\"page\": \"2\"})",
                required=False,
            ),
            "headers": ToolParameter(
                type="object",
                description="Opcional: headers complementares (ex.: Accept). NÃO inclua Authorization/Cookie — são recusados.",
                required=False,
            ),
            "body": ToolParameter(
                type="object",
                description="Opcional: corpo JSON da requisição (para POST/PUT/PATCH)",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="service_confirm",
        description=(
            "Executa uma requisição que voltou como 'pending_confirmation', DEPOIS que o "
            "usuário aprovou explicitamente. Passe o confirmation_id recebido. Se o usuário "
            "não aprovou, não chame. A confirmação expira em 5 minutos e é de uso único."
        ),
        module="services",
        execution="server-side",
        timeout=40,
        parameters={
            "confirmation_id": ToolParameter(
                type="string",
                description="ID devolvido no campo confirmation_id de um service_request pendente",
                required=True,
            ),
        },
    ),
]

SERVICE_TOOL_NAMES = frozenset(t.name for t in SERVICE_TOOLS)
