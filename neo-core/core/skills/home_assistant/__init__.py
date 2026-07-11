"""Home Assistant skill manifest."""

from core.skills.manifest import SkillManifest
from shared.types import IntentType

home_assistant_manifest = SkillManifest(
    name="home_assistant",
    description=(
        "Casa inteligente via Home Assistant — controla luzes, tomadas, clima/termostato, "
        "persianas, cenas, scripts e automações; e consulta estados de dispositivos "
        "('acende a luz da sala', 'qual a temperatura do quarto?', 'ativa o modo cinema')."
    ),
    keywords=[
        "luz", "luzes", "lampada", "lâmpada", "acende", "acender", "apaga", "apagar",
        "tomada", "interruptor", "termostato", "clima", "ar condicionado", "aquecedor",
        "cena", "modo cinema", "persiana", "cortina", "home assistant", "automacao",
        "automação", "script", "casa", "quarto", "sala", "cozinha", "garagem",
    ],
    intent_type=IntentType.HOME_ASSISTANT,
    tool_names=[
        "ha_list_entities",
        "ha_get_state",
        "ha_call_service",
        "ha_rename_entity",
    ],
    handoff_line="controlar a casa (luzes, tomadas, clima, persianas, cenas).",
    pipeline_factory="core.skills.home_assistant.pipeline:HomeAssistantPipeline",
)

home_assistant_manifest.register()
