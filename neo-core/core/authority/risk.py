"""Classificação de risco das tools — a tabela é a definição ÚNICA.

Por que uma tabela e não um campo no ``ToolSchema``: o campo obrigaria a mexer
nos 27 arquivos de ``tools.py`` para uma informação que precisa ser lida num
lugar só (o portão). Pior, um schema novo nasceria sem classe e o default
silencioso decidiria a segurança. Aqui o default é explícito e auditável: tool
desconhecida cai em :data:`DESCONHECIDA` e o portão trata como atuação.

Mesma razão do ``server_tool_can_run`` (``core/router/intent_router.py``): duas
definições de "o que esta tool é" divergem no primeiro nome novo, e divergem em
silêncio. Ao registrar tool nova, classifique aqui — o teste
``tests/authority/test_risk_cobre_o_registry.py`` falha quando existe tool
declarada que a tabela não conhece.

As quatro classes (uma tool pode ter mais de uma):

``fonte_externa``
    Traz texto de terceiro para dentro do contexto. É o que CONTAMINA o turno.
``leitura_privada``
    Lê dado do usuário. É o que dá o que vazar.
``revelacao``
    Entrega texto a um terceiro (e-mail, mensagem, formulário, query string).
``atuacao``
    Muda o mundo — disco, casa, agenda, memória, dinheiro.
"""

from __future__ import annotations

FONTE_EXTERNA = "fonte_externa"
LEITURA_PRIVADA = "leitura_privada"
REVELACAO = "revelacao"
ATUACAO = "atuacao"

#: Tool que a tabela não conhece. O portão trata como atuação (fail-safe).
DESCONHECIDA = frozenset({ATUACAO})

_F = FONTE_EXTERNA
_L = LEITURA_PRIVADA
_R = REVELACAO
_A = ATUACAO

#: nome exato -> classes. Comentários marcam as decisões que não são óbvias.
_BY_NAME: dict[str, frozenset[str]] = {
    # ── fontes externas puras ────────────────────────────────────────────
    "web_search": frozenset({_F}),
    "deep_research": frozenset({_F}),
    "meli_search": frozenset({_F}),
    "meli_product": frozenset({_F}),
    "browser_screenshot": frozenset({_F}),
    # web_fetch também REVELA: o dado cabe no query string de um GET.
    "web_fetch": frozenset({_F, _R}),
    # Os dois navegadores leem a web E digitam nela — o pior par que existe.
    # O local ainda por cima roda na máquina do usuário.
    "browser_task": frozenset({_F, _R, _A}),
    "browser_local_task": frozenset({_F, _R, _A}),
    # Caixa do Neo: o conteúdo é de terceiro (fonte externa) e a caixa é dele
    # (leitura privada). As duas coisas ao mesmo tempo.
    "email_inbox": frozenset({_F, _L}),
    "email_message": frozenset({_F, _L}),
    # Baixa o anexo para o catálogo: lê conteúdo de fora, não age para fora.
    "email_anexo": frozenset({_F, _L}),
    # Arquivo de terceiro tem texto de terceiro dentro (PDF com injection).
    "deep_read": frozenset({_F, _L}),
    # Claude Code delegado: lê web e arquivos (o relatório que volta é fonte
    # externa) e atua — no corpo local, na máquina do usuário, com rede livre.
    # O portão NEGA em turno contaminado/agenda/de fora (gate.py).
    "claude_task": frozenset({_F, _A}),
    "claude_reply": frozenset({_A}),
    # As sessões do Claude do Daniel: ler é dado dele (o que pediu, o que a
    # sessão respondeu); entregar um pedido faz um agente agir na máquina dele.
    "claude_sessoes": frozenset({_L}),
    "claude_enviar": frozenset({_A}),
    "file_read": frozenset({_F, _L}),
    # A fala transcrita de um vídeo de terceiro é texto de terceiro (injection
    # falada). O áudio vai ao Groq, mas o destino é fixo e escolhido pelo dono no
    # painel: não é um canal que o conteúdo controla, então não conta revelação.
    "neo_transcrever": frozenset({_F, _L}),
    # Lê a busca do YouTube e as miniaturas (texto e imagem de terceiro). Não
    # baixa nem escreve nada fora do sandbox.
    "neo_video_candidatos": frozenset({_F}),
    # O que o modelo de visão lê na página renderizada pode ser texto de terceiro
    # (o PDF anexado com injection vira imagem e volta como descrição).
    "file_olhar": frozenset({_F, _L}),
    "read_user_file": frozenset({_F, _L}),
    # ── leitura privada ──────────────────────────────────────────────────
    "search_pks": frozenset({_L}),
    # pks_reveal põe um dado de identificação NO CONTEXTO — é o passo que
    # precede qualquer vazamento. Num turno seu e limpo é rotina; num turno
    # que leu página/e-mail, é uma liberação. Por isso conta como revelação.
    "pks_reveal": frozenset({_L, _R}),
    "conversation_recall": frozenset({_L}),
    "wm_read": frozenset({_L}),
    "sql_query_ro": frozenset({_L}),
    "vault_list": frozenset({_L}),
    "file_list": frozenset({_L}),
    "file_info": frozenset({_L}),
    "get_user_location": frozenset({_L}),
    "list_connected_devices": frozenset({_L}),
    # ── revelação ────────────────────────────────────────────────────────
    "email_send": frozenset({_R}),
    "whatsapp_send_message": frozenset({_R}),
    "whatsapp_list_contacts": frozenset({_L}),
    "whatsapp_lookup_contact": frozenset({_L}),
    # service_request fala com um serviço de fora COM a credencial do usuário:
    # revela e atua ao mesmo tempo. service_confirm executa o que ficou pendente.
    "service_request": frozenset({_R, _A}),
    "service_confirm": frozenset({_R, _A}),
    "service_list": frozenset({_L}),
    "service_docs": frozenset({_L}),
    # ── atuação ──────────────────────────────────────────────────────────
    "run_command": frozenset({_A}),
    "neo_bash": frozenset({_A}),
    "neo_bash_background": frozenset({_A}),
    "neo_write_file": frozenset({_A}),
    "neo_read_file": frozenset({_F, _L}),
    "neo_list_files": frozenset({_L}),
    "write_file": frozenset({_A}),
    "edit_user_file": frozenset({_A}),
    "delegate_file_edit": frozenset({_A}),
    "save_generated_file": frozenset({_A}),
    "request_user_file": frozenset({_A, _L}),
    "file_save": frozenset({_A}),
    "file_stage": frozenset({_A}),
    "file_offer": frozenset({_A}),
    "file_set_retention": frozenset({_A}),
    "ha_call_service": frozenset({_A}),
    "ha_rename_entity": frozenset({_A}),
    "ha_get_state": frozenset({_L}),
    "ha_list_entities": frozenset({_L}),
    "device_control": frozenset({_A}),
    "device_power": frozenset({_A}),
    "pks_remember": frozenset({_A}),
    "pks_forget": frozenset({_A}),
    "wm_write": frozenset({_A}),
    "config_set": frozenset({_A}),
    "config_get": frozenset(),
    "config_list": frozenset(),
    "tool_run": frozenset({_A}),  # dispatcher genérico: vale o pior caso
    "tool_find": frozenset(),
    "job_cancel": frozenset({_A}),
    # Instrui o agente que está mexendo no navegador DELE: é atuação.
    "job_falar": frozenset({_A}),
    "job_list": frozenset(),
    "job_status": frozenset(),
    "job_logs": frozenset(),
    "event_subscribe": frozenset({_A}),
    "event_unsubscribe": frozenset({_A}),
    "event_list": frozenset(),
    "whatsapp_complete_mission": frozenset({_A}),
    "whatsapp_fail_mission": frozenset({_A}),
    "whatsapp_notify_user": frozenset(),  # notifica o DONO, não um terceiro
    "whatsapp_request_confirmation": frozenset(),  # pergunta ao dono
    # A saída de emergência do portão NÃO pode ter fechadura: solicitar_dado
    # só cria um pedido para VOCÊ decidir — não entrega nada a ninguém.
    "solicitar_dado": frozenset(),
    # ── neutras ──────────────────────────────────────────────────────────
    "get_weather": frozenset(),
    "voice_list": frozenset(),
    "voice_get": frozenset(),
    "skill_list": frozenset(),
    "skill_read": frozenset(),
}

#: prefixo -> classes, aplicado quando o nome exato não está na tabela.
#: A ordem importa: o primeiro que casar vence.
_BY_PREFIX: tuple[tuple[str, frozenset[str]], ...] = (
    # Leituras dos módulos user-scoped (o resto do prefixo é escrita).
    ("data_get", frozenset({_L})),
    ("data_list", frozenset({_L})),
    ("data_schema", frozenset({_L})),
    ("fin_balances", frozenset({_L})),
    ("fin_statement", frozenset({_L})),
    ("fin_insights", frozenset({_L})),
    ("fin_card_invoice", frozenset({_L})),
    ("schedule_list", frozenset()),
    # Escritas.
    ("data_", frozenset({_A})),
    ("fin_", frozenset({_A})),
    ("schedule_", frozenset({_A})),
    ("pks_", frozenset({_A})),
    ("wm_", frozenset({_A})),
)


def classes_of(tool_name: str) -> frozenset[str]:
    """Classes de risco de uma tool. Desconhecida → atuação (fail-safe)."""
    nome = (tool_name or "").strip()
    if nome in _BY_NAME:
        return _BY_NAME[nome]
    for prefixo, classes in _BY_PREFIX:
        if nome.startswith(prefixo):
            return classes
    return DESCONHECIDA


def is_external_source(tool_name: str) -> bool:
    """Esta tool traz texto de terceiro? (o que contamina o turno)"""
    return FONTE_EXTERNA in classes_of(tool_name)


def is_private_read(tool_name: str) -> bool:
    return LEITURA_PRIVADA in classes_of(tool_name)


def is_sink(tool_name: str) -> bool:
    """Esta tool entrega para fora ou muda o mundo? (o que o portão guarda)"""
    classes = classes_of(tool_name)
    return REVELACAO in classes or ATUACAO in classes


def is_actuation(tool_name: str) -> bool:
    """Esta tool MUDA algo (disco, casa, agenda, memória, dinheiro)?

    Usado fora do portão para responder "esta skill tem onde GUARDAR o fato?":
    uma skill cujas tools são todas de leitura não é dona do dado, e suprimir a
    escrita do PKS por causa dela perde a mensagem (ver ``skill_owns_data`` em
    ``modules/desktop/ai_service.py``).
    """
    return ATUACAO in classes_of(tool_name)


def known(tool_name: str) -> bool:
    """A tabela conhece este nome de verdade (não caiu no fail-safe)?"""
    nome = (tool_name or "").strip()
    return nome in _BY_NAME or any(nome.startswith(p) for p, _ in _BY_PREFIX)
