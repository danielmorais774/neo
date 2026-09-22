"""Definição da tool `conversation_recall` — server-side, read-only."""

from shared import ToolParameter, ToolSchema

CONVERSATION_RECALL_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="conversation_recall",
        description=(
            "Busca no ARQUIVO de conversas deste usuário — todas as sessões, "
            "todos os canais (desktop, painel, WhatsApp) E TAMBÉM os turnos "
            "anteriores DESTA mesma conversa. Use SEMPRE que ele perguntar se "
            "vocês já falaram de algo ('a gente conversou sobre X?', 'você "
            "lembra quando eu disse...', 'o que eu te falei sobre Y?') e "
            "SEMPRE que ele pedir de volta algo que VOCÊ escreveu ('aquela "
            "análise que você fez', 'a mensagem que você mandou', 'olha no seu "
            "histórico', 'me mostra de novo'). Você NÃO enxerga a conversa "
            "inteira: o histórico que chega até você é uma JANELA dos turnos "
            "recentes, e texto longo pode ter ficado de fora — quando isso "
            "acontece aparece um marcador '[mensagem ... OMITIDA]'. Não achar "
            "no contexto não significa que não existe. NUNCA responda 'não "
            "falamos sobre isso', 'não tem nada registrado' ou 'essa análise "
            "não existe' sem ter chamado esta tool nesta mesma resposta, e "
            "NUNCA substitua o texto pedido por um arquivo do disco. Se o "
            "usuário disser que não era essa conversa e sim uma mais antiga, "
            "chame de novo passando `before`. Se for a conversa certa mas o texto "
            "não apareceu, chame de novo passando `conversa`."
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
            "completo": ToolParameter(
                type="boolean",
                description=(
                    "Passe `true` quando o usuário quiser VER o texto de volta "
                    "('me mostra aquela análise de novo', 'olha a mensagem que "
                    "você escreveu por completo', 'repete a tabela'). Aí as "
                    "mensagens que casarem vêm INTEIRAS, e você entrega o que "
                    "voltou. Sem isso vêm só trechos de ~240 caracteres, que "
                    "servem para dizer SE vocês falaram do assunto, mas não "
                    "para reproduzir o conteúdo — e reescrever de cabeça o que "
                    "veio cortado é inventar."
                ),
                required=False,
            ),
            "conversa": ToolParameter(
                type="string",
                description=(
                    "Opcional. O código de 8 caracteres da conversa (o "
                    "'sessão 7330e40c' que aparece no bloco). Use quando a "
                    "conversa CERTA já apareceu mas o texto que o usuário "
                    "pediu não estava nos trechos — principalmente se veio o "
                    "aviso de que outras mensagens dela casaram e ficaram de "
                    "fora. Aí a busca abre MAIS trechos daquela conversa. "
                    "`before` é para conversas mais antigas; `conversa` é "
                    "para o resto DESTA."
                ),
                required=False,
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
