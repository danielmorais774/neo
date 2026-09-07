"""Tools do caderno de trabalho (docs/working-memory-plan.md §5).

Prefixo ``wm_`` é uma decisão, não estilo: as tools NÃO podem se chamar
``note_*``/``anotar_*`` porque "anota isso" é o gatilho do ``pks_remember``. Se
o modelo passar a mandar fato permanente do usuário para cá, o dado morre em 30
dias e a escrita sob demanda no PKS (que custou caro para funcionar) regride.
Daí o prefixo opaco + a negativa explícita nas duas descriptions (§13-R1).

Só duas tools: o ÍNDICE já está no system prompt todo turno, então uma
``wm_list`` só pagaria schema à toa.
"""

from shared import ToolParameter, ToolSchema

WORKING_MEMORY_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="wm_write",
        description=(
            "Grava no SEU caderno de trabalho uma conclusão que você acabou de obter "
            "e que vai fazer falta nos próximos turnos: números já apurados, a "
            "estrutura/formato de um arquivo que você leu, uma decisão tomada, o "
            "caminho de um artefato que você gerou. Chame no MESMO turno em que "
            "concluiu — no turno seguinte o conteúdo bruto (o arquivo lido, a saída "
            "do script) já não está mais no seu contexto, e sem a anotação você teria "
            "que reler tudo ou reprocessar do zero. Escreva a conclusão POR EXTENSO e "
            "auto-contida: quem lê depois não enxerga esta conversa. Use o mesmo "
            "`note` para continuar uma anotação existente (vira uma nova entrada "
            "datada); use um `note` novo para um assunto novo. "
            "NÃO é a memória permanente do usuário: 'lembra que eu prefiro X', 'anota "
            "que meu aniversário é Y', qualquer fato durável sobre ele ou sobre o "
            "mundo vai para `pks_remember`, nunca para cá. Também não é lugar de "
            "despejar o conteúdo de um arquivo — anote a CONCLUSÃO, não os dados."
        ),
        module="working_memory",
        execution="server-side",
        timeout=20,
        parameters={
            "note": ToolParameter(
                type="string",
                description=(
                    "Nome curto e estável da nota, em minúsculas com hífens "
                    "(ex.: 'extrato-nubank-marco'). Reusar o mesmo nome ACRESCENTA "
                    "uma entrada; não crie variações do mesmo assunto."
                ),
                required=True,
            ),
            "content": ToolParameter(
                type="string",
                description=(
                    "A conclusão em markdown, por extenso e auto-contida. Ex.: "
                    "'412 lançamentos em março. Débitos R$ 8.412,10. O CSV usa `;` "
                    "como separador e vírgula decimal; colunas data;descricao;valor.'"
                ),
                required=True,
            ),
            "title": ToolParameter(
                type="string",
                description="Título humano da nota (só usado na criação).",
                required=False,
            ),
            "summary": ToolParameter(
                type="string",
                description=(
                    "UMA linha resumindo o estado atual da nota — é exatamente isto "
                    "que você vê no seu índice em todos os turnos seguintes, então "
                    "escreva o que faria você decidir abrir (ou não) a nota."
                ),
                required=False,
            ),
            "mode": ToolParameter(
                type="string",
                description=(
                    "append (padrão) acrescenta uma entrada datada; replace descarta "
                    "o histórico da nota e recomeça (use só quando o que estava lá "
                    "ficou ERRADO)."
                ),
                enum=["append", "replace"],
                required=False,
            ),
            "status": ToolParameter(
                type="string",
                description=(
                    "active (padrão) mantém a nota no seu índice; done tira do índice "
                    "quando a tarefa acabou (ela continua legível por wm_read)."
                ),
                enum=["active", "done"],
                required=False,
            ),
            "links": ToolParameter(
                type="string",
                description=(
                    "Opcional, separado por vírgula: outras notas ('analise-gastos'), "
                    "arquivos do catálogo ('file:<file_id>') ou artefatos do seu "
                    "sandbox ('path:/workspace/extrato/parse.py'). Escrever [[assim]] "
                    "dentro do content também cria o vínculo."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="wm_read",
        description=(
            "Abre uma anotação do SEU caderno de trabalho pelo nome (os nomes "
            "disponíveis estão entre [[ ]] na seção 'Memória de Trabalho' do seu "
            "contexto). Chame ANTES de reler um arquivo, refazer uma análise ou "
            "recalcular qualquer coisa sobre um trabalho que você já fez — a "
            "conclusão provavelmente já está anotada, com a data em que foi obtida. "
            "Por padrão devolve as últimas entradas; use full=true para o caderno "
            "inteiro. Se um número anotado for antigo e o arquivo pode ter mudado, "
            "confira antes de afirmar."
        ),
        module="working_memory",
        execution="server-side",
        timeout=15,
        parameters={
            "note": ToolParameter(
                type="string",
                description="Nome da nota, como aparece entre [[ ]] no seu índice.",
                required=True,
            ),
            "full": ToolParameter(
                type="boolean",
                description="true devolve o caderno inteiro (padrão: só as últimas entradas).",
                required=False,
            ),
        },
    ),
]

WORKING_MEMORY_TOOL_NAMES = frozenset(t.name for t in WORKING_MEMORY_TOOLS)
