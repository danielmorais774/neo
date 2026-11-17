"""Tool schemas for the home_assistant skill (server-side)."""

from shared.types import ToolParameter, ToolSchema

HOME_ASSISTANT_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="ha_list_entities",
        description=(
            "Lista entidades do Home Assistant para descobrir o entity_id correto. "
            "Filtre por domínio (light, switch, climate, scene, script, cover, fan, "
            "sensor) e/ou por um texto de busca (apelido/cômodo, ex: 'sala'). "
            "Use SEMPRE antes de controlar/consultar quando não souber o entity_id."
        ),
        module="home_assistant",
        execution="server-side",
        timeout=15,
        parameters={
            "domain": ToolParameter(
                type="string",
                description="Domínio para filtrar (ex: light, switch, climate, scene, script). Opcional.",
                required=False,
            ),
            "query": ToolParameter(
                type="string",
                description="Texto livre para filtrar por apelido/cômodo (ex: 'sala', 'quarto'). Opcional.",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="ha_get_state",
        description=(
            "Retorna o estado atual de uma entidade do Home Assistant "
            "(ligado/desligado, brilho, temperatura, etc). Requer o entity_id exato."
        ),
        module="home_assistant",
        execution="server-side",
        timeout=15,
        parameters={
            "entity_id": ToolParameter(
                type="string",
                description="entity_id exato (ex: light.sala, climate.quarto).",
                required=True,
            ),
        },
    ),
    ToolSchema(
        name="ha_call_service",
        description=(
            "Executa um serviço do Home Assistant — é a ferramenta de AÇÃO. "
            "Controla dispositivos e dispara cenas/scripts/automações. Exemplos: "
            "domain=light service=turn_on entity_id=light.sala data={\"brightness_pct\": 50}; "
            "domain=switch service=turn_off entity_id=switch.tomada; "
            "domain=climate service=set_temperature entity_id=climate.quarto data={\"temperature\": 22}; "
            "domain=scene service=turn_on entity_id=scene.modo_cinema; "
            "domain=script service=turn_on entity_id=script.boa_noite; "
            "domain=automation service=trigger entity_id=automation.x."
        ),
        module="home_assistant",
        execution="server-side",
        timeout=20,
        parameters={
            "domain": ToolParameter(
                type="string",
                description="Domínio do serviço (ex: light, switch, climate, scene, script, automation, cover, fan).",
                required=True,
            ),
            "service": ToolParameter(
                type="string",
                description="Serviço a chamar (ex: turn_on, turn_off, toggle, set_temperature).",
                required=True,
            ),
            "entity_id": ToolParameter(
                type="string",
                description="entity_id alvo. Opcional para serviços sem alvo, obrigatório para a maioria.",
                required=False,
            ),
            "data": ToolParameter(
                type="object",
                description="Parâmetros extras do serviço (ex: {\"brightness_pct\": 50}, {\"temperature\": 22}). Opcional.",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="ha_rename_entity",
        description=(
            "Renomeia (de forma PERSISTENTE) o nome amigável de uma entidade no "
            "Home Assistant — altera o registro da entidade, não só uma exibição "
            "temporária. Ex: renomear 'light.sala' para 'Luz Principal'. Resolva o "
            "entity_id com ha_list_entities antes. Passe name vazio para voltar ao "
            "nome padrão da integração."
        ),
        module="home_assistant",
        execution="server-side",
        timeout=20,
        parameters={
            "entity_id": ToolParameter(
                type="string",
                description="entity_id exato a renomear (ex: light.sala).",
                required=True,
            ),
            "name": ToolParameter(
                type="string",
                description="Novo nome amigável. Vazio (\"\") para resetar ao padrão.",
                required=True,
            ),
        },
    ),
]
