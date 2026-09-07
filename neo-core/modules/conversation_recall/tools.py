"""Definição da tool `conversation_recall` — server-side, read-only."""

from shared import ToolParameter, ToolSchema

CONVERSATION_RECALL_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="conversation_recall",
        description=(
            "Busca no ARQUIVO de conversas passadas deste usuário — todas as "
            "sessões, todos os canais (desktop, painel, WhatsApp). Use SEMPRE "
            "que ele perguntar se vocês já falaram de algo ('a gente conversou "
            "sobre X?', 'você lembra quando eu disse...', 'o que eu te falei "
            "sobre Y?') ou quando precisar do que foi dito em OUTRA conversa. "
            "Você só tem no contexto as mensagens desta sessão — as antigas só "
            "existem aqui dentro. NUNCA responda 'não falamos sobre isso' ou "
            "'não tem nada registrado' sem ter chamado esta tool nesta mesma "
            "resposta. Se o usuário disser que não era essa conversa e sim uma "
            "mais antiga, chame de novo passando `before`."
        ),
        # Classificar não é usar: a política ("nunca negue sem buscar") é do
        # cérebro e fica na description, não no resumo do router.
        router_summary="Busca conversas antigas do usuário por assunto.",
        module="conversation_recall",
        execution="server-side",
        timeout=15,
        parameters={
            "query": ToolParameter(
                type="string",
                description=(
                    "SÓ o assunto, sem as palavras da pergunta. "
                    "'a gente conversou sobre a frança?' → 'frança'. "
                    "'o que eu te falei do meu treino de perna?' → 'treino perna'. "
                    "Não escreva 'conversa', 'lembra', 'sobre' — isso é a pergunta, "
                    "não o assunto."
                ),
                required=True,
            ),
            "before": ToolParameter(
                type="string",
                description=(
                    "Opcional, ISO-8601. Só conversas ANTERIORES a esta data. Use "
                    "quando o usuário disser que não foi essa e sim uma mais "
                    "antiga: passe o valor de `proxima_busca_before` que veio no "
                    "resultado anterior."
                ),
                required=False,
            ),
        },
    ),
]
