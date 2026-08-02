"""Invariantes do ``ROUTER_SYSTEM_PROMPT``.

O prompt do classificador vai em toda mensagem, então ele é podado de tempos em
tempos. Cada item abaixo é uma REGRA que existe por causa de um bug real (a
sessão está no comentário do ``intent_router.py`` correspondente): o teste falha
se a poda seguinte remover a regra junto com a repetição.

Não é teste de fraseado — cada âncora é o menor pedaço que identifica a regra.
Se a regra for reescrita, atualize a âncora; se ela for REMOVIDA de propósito,
apague a linha aqui e diga por quê no commit.
"""

import re

from core.router.intent_router import ROUTER_SYSTEM_PROMPT as PROMPT

# (nome da regra, âncora que precisa aparecer no prompt)
RULES = [
    ("sandbox vs máquina do usuário", "/workspace"),
    ("ponte File Store / file_id", "file_save"),
    ("entrega de arquivo gerado", "save_generated_file"),
    ("catálogo client-side fechado", "NÃO existem"),
    ("comando não inventa caminho", "NUNCA invente"),
    ("anexo do catálogo não é disco", "ARQUIVOS ANEXADOS NESTA MENSAGEM"),
    ("anexo não vira caminho inventado", "NUNCA invente um caminho"),
    ("imagem inline pelo cérebro", "[[IMAGE:"),
    ("imagem nunca vira base64", "base64"),
    ("evento atual vai pro cérebro", "cotação do dólar"),
    ("agir é o padrão", "qual o comando pra"),
    ("diretiva de memória", "pks_remember"),
    ("dispositivos ao vivo x conhecidos", "list_connected_devices"),
    ("HA não escolhe tool nem entity_id", "entity_id"),
    ("WhatsApp passa apelido cru", "recipient"),
    ("config de voz/engine", "tts.voice"),
    ("ato de fala guia a skill", "speech_act=imperative"),
    ("sondagem antes de assumir nome", "needs_plan"),
    ("cascata conservadora", "pipeline_plan"),
    ("sinais de tom", "register_hint"),
]

# Campos do contrato de saída — o parser (``_parse_classification``) lê todos.
OUTPUT_FIELDS = [
    "speech_act", "intent", "confidence", "tool_name", "tool_arguments",
    "raw_command", "explanation", "requires_approval", "needs_memory",
    "knowledge_keywords", "needs_plan", "tipo_tarefa", "skill_name",
    "pipeline_plan", "gravity", "register_hint",
]

INTENTS = [
    "DIRECT_RESPONSE", "SERVER_TOOL", "CLIENT_TOOL",
    "WHATSAPP_MESSAGING", "HOME_ASSISTANT",
]


def test_every_load_bearing_rule_survives():
    missing = [name for name, anchor in RULES if anchor not in PROMPT]
    assert not missing, f"regras sumiram do prompt: {missing}"


def test_output_contract_is_complete():
    missing = [f for f in OUTPUT_FIELDS if f'"{f}"' not in PROMPT]
    assert not missing, f"campos do JSON de saída ausentes: {missing}"


def test_all_intents_are_offered():
    missing = [i for i in INTENTS if i not in PROMPT]
    assert not missing, f"intents ausentes do prompt: {missing}"


def test_placeholders_are_intact():
    for placeholder in ("{server_tools}", "{client_tools}", "{skills}"):
        assert placeholder in PROMPT, f"placeholder {placeholder} sumiu"


def test_prompt_formats_without_stray_braces():
    """``classify`` chama ``.format()``: todo ``{`` literal precisa estar escapado."""
    rendered = PROMPT.format(server_tools="S", client_tools="C", skills="K")
    assert "{{" not in rendered and "}}" not in rendered


def test_speech_acts_match_the_parser():
    from core.router.intent_router import _SPEECH_ACTS

    for act in _SPEECH_ACTS:
        assert f"`{act}`" in PROMPT, f"ato de fala '{act}' não é explicado no prompt"


def test_prompt_stays_within_budget():
    """Teto de tamanho: o prompt é custo fixo por mensagem.

    Não é um alvo de compressão — é um alarme para quando ele voltar a crescer
    por acúmulo de exemplo/regra repetida (v1 tinha 22.977 chars).
    """
    assert len(PROMPT) < 17_000, f"prompt cresceu para {len(PROMPT):,} chars — revise a repetição"


def test_examples_do_not_repeat_a_message():
    """O mesmo exemplo em duas seções é só custo — v1 repetia 3x 'acende a luz da sala'."""
    quoted = re.findall(r'User: "([^"]{6,})"', PROMPT)
    duplicates = {m for m in quoted if quoted.count(m) > 1}
    assert not duplicates, f"exemplos duplicados no prompt: {duplicates}"
