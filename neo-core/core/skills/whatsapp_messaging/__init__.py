"""WhatsApp Messaging skill manifest."""

from core.skills.manifest import SkillManifest
from shared.types import IntentType

whatsapp_messaging_manifest = SkillManifest(
    name="whatsapp_messaging",
    description=(
        "Mensagens via WhatsApp — envia mensagens pessoais para contatos do usuário "
        "(família, amigos) e também trata pedidos comerciais (delivery, restaurante, "
        "farmácia, mercado). Resolve contatos por apelido (ex: 'minha mãe') buscando "
        "na agenda do WhatsApp."
    ),
    keywords=[
        "whatsapp", "wpp", "zap", "mensagem", "mensagens", "manda", "mandar",
        "envia", "enviar", "texto", "contato", "telefone", "celular",
        "mae", "mãe", "pai", "irmao", "irmão", "irma", "irmã", "esposa", "marido",
        "amigo", "amiga", "namorada", "namorado",
        "pizza", "delivery", "restaurante", "pedido", "comida", "domicilio",
        "farmácia", "mercado", "encomenda", "domino", "ifood", "rappi",
    ],
    intent_type=IntentType.WHATSAPP_MESSAGING,
    tool_names=[
        "whatsapp_send_message",
        "whatsapp_request_confirmation",
        "whatsapp_complete_mission",
        "whatsapp_fail_mission",
        "whatsapp_lookup_contact",
        "whatsapp_list_contacts",
        "whatsapp_notify_user",
    ],
    handoff_line="enviar mensagem de WhatsApp (passa por confirmação).",
    pipeline_factory="core.skills.whatsapp_messaging.pipeline:WhatsAppMessagingPipeline",
)

whatsapp_messaging_manifest.register()
