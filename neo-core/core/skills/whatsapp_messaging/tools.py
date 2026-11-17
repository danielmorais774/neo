"""Tool schemas for the whatsapp_messaging skill."""

from shared.types import ToolSchema, ToolParameter

WHATSAPP_MESSAGING_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="whatsapp_send_message",
        description=(
            "Envia uma mensagem de texto para o contato do WhatsApp. Em modo "
            "personal, o envio passa por aprovação humana via "
            "whatsapp_request_confirmation antes de sair."
        ),
        module="whatsapp_messaging",
        execution="server-side",
        parameters={
            "text": ToolParameter(
                type="string",
                description="Texto da mensagem a enviar",
                required=True,
            ),
            "recipient": ToolParameter(
                type="string",
                description=(
                    "Destinatário: JID completo (ex: 5511988217222@c.us), número "
                    "com código do país, ou apelido do contato. Se omitido, usa o "
                    "contato resolvido na missão atual."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="whatsapp_request_confirmation",
        description=(
            "Pausa a missão e pede aprovação ao Daniel antes de continuar. "
            "Use SEMPRE para mensagem pessoal (mostrando rascunho + destinatário + "
            "telefone) e para decisões comerciais sensíveis (preço alto, endereço, "
            "pagamento)."
        ),
        module="whatsapp_messaging",
        execution="server-side",
        parameters={
            "question": ToolParameter(
                type="string",
                description="Pergunta/rascunho a apresentar ao Daniel para aprovação",
                required=True,
            ),
            "draft_text": ToolParameter(
                type="string",
                description="Texto da mensagem que será enviada se aprovada",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="whatsapp_complete_mission",
        description="Finaliza a missão com um resumo. Use quando o objetivo foi alcançado.",
        module="whatsapp_messaging",
        execution="server-side",
        parameters={
            "summary": ToolParameter(
                type="string",
                description="Resumo do que foi feito",
                required=True,
            ),
            "details": ToolParameter(
                type="string",
                description="Detalhes opcionais (preço, ETA, endereço)",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="whatsapp_fail_mission",
        description="Marca a missão como falhou com uma razão.",
        module="whatsapp_messaging",
        execution="server-side",
        parameters={
            "reason": ToolParameter(
                type="string",
                description="Motivo pelo qual a missão falhou",
                required=True,
            ),
        },
    ),
    ToolSchema(
        name="whatsapp_lookup_contact",
        description=(
            "Busca um contato pelo apelido na agenda do WhatsApp do Daniel. "
            "Retorna jid, name, phone_e164. Aceita 'minha mãe', 'mae', 'pai' etc."
        ),
        module="whatsapp_messaging",
        execution="server-side",
        parameters={
            "label": ToolParameter(
                type="string",
                description="Apelido/nome do contato",
                required=True,
            ),
        },
    ),
    ToolSchema(
        name="whatsapp_list_contacts",
        description=(
            "Lista contatos do WhatsApp do Daniel. Use quando o apelido do "
            "destinatário for ambíguo (ex: 'João' com vários Joãos), ou para "
            "obter candidatos antes de pedir desambiguação ao Daniel."
        ),
        module="whatsapp_messaging",
        execution="server-side",
        parameters={
            "query": ToolParameter(
                type="string",
                description="Termo de busca opcional para filtrar contatos",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="whatsapp_notify_user",
        description=(
            "Cria uma notificação para o Daniel quando precisar de atenção dele "
            "(ex: contato respondeu algo importante e a missão precisa de input). "
            "Não pausa a missão."
        ),
        module="whatsapp_messaging",
        execution="server-side",
        parameters={
            "title": ToolParameter(
                type="string",
                description="Título curto da notificação",
                required=True,
            ),
            "body": ToolParameter(
                type="string",
                description="Conteúdo da notificação",
                required=True,
            ),
        },
    ),
]
