"""Desktop module tools - definições das tools do neo-desktop.

``list_directory``, ``read_file``, ``write_file``, ``create_directory``,
``delete_path``, ``search_files`` e ``get_system_info`` foram REMOVIDAS deste
catálogo (2026-07-24): estavam registradas — inclusive com descrição "USE
SEMPRE esta ferramenta em vez de run_command" — mas nunca tiveram NENHUMA
implementação no neo-desktop-tauri (nem Rust nem React; só apareciam como
rótulo cosmético no PlanTracker). Toda vez que o classificador escolhia
qualquer uma delas, o ToolRequest saía pro cliente e nunca voltava —
resultado: 300s de timeout garantido, e isso vinha se repetindo em sessões
reais (2afe5753: "relatório de finanças... na minha área de trabalho" →
list_directory → travou). Até serem implementadas de verdade (mesmo padrão de
``save_generated_file``: interceptar em ai_agent.rs, executar, devolver
ToolResult), NÃO devem voltar pro catálogo — `run_command` já cobre listar
(`ls`), ler (`cat`), escrever (`echo >`/`tee`), criar diretório (`mkdir`) e
apagar (`rm`) de ponta a ponta, comprovadamente.
"""

from shared import ToolParameter, ToolSchema

# Tools disponíveis para o neo-desktop
DESKTOP_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="run_command",
        description=(
            "Executa qualquer comando shell no terminal do usuário. "
            "Use esta ferramenta sempre que precisar: rodar scripts, instalar pacotes, "
            "verificar processos (ps, top), espaço em disco (df -h, du), "
            "variáveis de ambiente (env), versões de software (python --version, node -v), "
            "gerenciar serviços, compilar código, mover/copiar arquivos via shell, "
            "ou qualquer outra operação que requeira o terminal do usuário. "
            "O terminal está SEMPRE disponível: você chama, o usuário aprova, e a saída "
            "REAL volta pra você responder. Nunca diga que não tem acesso ao terminal e "
            "nunca peça pro usuário rodar o comando e colar a saída — rode você."
        ),
        # O classificador só precisa saber QUANDO cair aqui; o "como usar" (e o
        # "nunca diga que não tem terminal", que é regra de comportamento do
        # cérebro) continua na description, que vai como schema da função.
        router_summary="Executa um comando shell no terminal do usuário.",
        module="desktop",
        execution="client-side",
        parameters={
            "command": ToolParameter(
                type="string",
                description="Comando shell completo a executar (ex: 'ls -la ~/Downloads', 'df -h', 'python --version')",
                required=True,
            ),
            "working_dir": ToolParameter(
                type="string",
                description="Diretório de trabalho (padrão: home do usuário)",
                required=False,
            ),
            "timeout": ToolParameter(
                type="integer",
                description="Timeout em segundos (padrão: 30)",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="save_generated_file",
        description=(
            "Baixa um arquivo que o Neo já gerou/catalogou (via file_save no sandbox) e "
            "salva DIRETO num local do disco do usuário. USE SEMPRE esta ferramenta — nunca "
            "run_command/cp/mv/curl — quando o usuário pedir pra 'colocar', 'salvar' ou "
            "'baixar' localmente um arquivo que o Neo criou: o cliente (não o modelo) resolve "
            "a URL certa do catálogo e escreve o arquivo; um comando shell nunca alcança o "
            "arquivo (ele existe só no sandbox remoto, não no disco do usuário, e nenhum "
            "caminho ou domínio inventado por você vai funcionar). NUNCA sobrescreve às cegas: "
            "se já existir um arquivo no destino, ela NÃO grava e devolve os dados do arquivo "
            "existente (nome, tamanho, data) com status='conflict' — aí VOCÊ decide (só quando "
            "for obviamente seguro, ex.: o arquivo que você acabou de gerar nesta conversa) ou "
            "PERGUNTA ao usuário citando nome/tamanho/data antes de reenviar com overwrite=true."
        ),
        # A regra "sem file_id no histórico não é CLIENT_TOOL" e o mapa de
        # `destination` já estão no prompt do classificador; a política de
        # colisão/overwrite é do cérebro e fica só na description.
        router_summary=(
            "Entrega no disco do usuário um arquivo que o cérebro já publicou "
            "no catálogo (exige um file_id real do histórico)."
        ),
        module="desktop",
        execution="client-side",
        parameters={
            "file_id": ToolParameter(
                type="string",
                description="O file_id devolvido pela tool file_save do sandbox",
                required=True,
            ),
            "file_name": ToolParameter(
                type="string",
                description="Nome do arquivo (com extensão) a salvar",
                required=True,
            ),
            "destination": ToolParameter(
                type="string",
                description=(
                    "Onde salvar. Atalhos conhecidos: 'desktop' (área de trabalho — padrão "
                    "quando o usuário não especifica), 'downloads', 'documents', 'home'. Ou um "
                    "caminho literal da máquina do usuário (ex: '~/Projetos/financas' ou "
                    "'/Users/daniel/Desktop/relatorios') se ele citar uma pasta específica."
                ),
                required=False,
            ),
            "overwrite": ToolParameter(
                type="boolean",
                description=(
                    "Só passe true DEPOIS de um status='conflict' (arquivo já existia) e você "
                    "ter certeza ou o usuário ter confirmado a sobrescrita. Padrão false: na "
                    "primeira tentativa NUNCA mande true — deixe a ferramenta detectar colisão."
                ),
                required=False,
            ),
        },
    ),
    # ── Pull: o núcleo cognitivo PEDE um arquivo do disco do usuário ──
    # (docs/... plano "arquivos sob iniciativa do cérebro"). Client-actuating:
    # NÃO roda in-process — o loop cognitivo levanta ClientActuationRequested e o
    # desktop executa (abre o picker OU sobe o caminho citado), devolvendo o
    # file_id do File Store. Gated por neo_file_actuation_enabled em read_tools.
    ToolSchema(
        name="request_user_file",
        description=(
            "Pede ao usuário um arquivo do computador DELE e o traz pro SEU catálogo "
            "(File Store) — use quando você precisa de um arquivo que só existe na máquina "
            "dele (uma planilha, um PDF, um script) e ele NÃO anexou. Depois que chegar, você "
            "recebe um file_id e lê por fatia (file_read) ou materializa no sandbox (file_stage) "
            "pra processar. NÃO é pra arquivos que o usuário já anexou (esses já estão no "
            "catálogo) nem pra arquivos que VOCÊ gerou (esses saem por file_save/download). "
            "Prefira mode='pick' (o usuário escolhe na hora, seguro). Só use mode='path' se o "
            "usuário citou LITERALMENTE um caminho nesta conversa — NUNCA invente um caminho a "
            "partir do nome do arquivo."
        ),
        module="desktop",
        execution="client-side",
        parameters={
            "mode": ToolParameter(
                type="string",
                description=(
                    "'pick' (padrão): abre o seletor de arquivos nativo pro usuário escolher. "
                    "'path': sobe um caminho EXATO que o usuário citou nesta conversa."
                ),
                required=False,
                enum=["pick", "path"],
            ),
            "path": ToolParameter(
                type="string",
                description=(
                    "Só com mode='path': o caminho literal que o usuário citou (ex: "
                    "'~/Desktop/vendas.csv'). Nunca invente — se não foi citado, use mode='pick'."
                ),
                required=False,
            ),
            "reason": ToolParameter(
                type="string",
                description="Frase curta dita ao usuário sobre por que você precisa do arquivo.",
                required=False,
            ),
            "stage": ToolParameter(
                type="boolean",
                description=(
                    "true quando você vai MANIPULAR o arquivo (rodar script, processar) — assim "
                    "ele já é materializado no seu /workspace e você recebe o caminho pronto pro "
                    "neo_bash. false (padrão) quando só vai LER o conteúdo (file_read)."
                ),
                required=False,
            ),
        },
    ),
    # ── Escrita direta no disco do usuário, sem shell ──
    # Existiu no catálogo até 2026-08-02 e foi REMOVIDA por ser schema sem
    # executor: o ToolRequest saía e nunca voltava, travando o turno pelos 300s
    # do timeout do cliente (sessão 2afe5753). Voltou em 2026-08-30 COM o
    # handler em ai_agent.rs (`write_text_file` em commands/mod.rs) — a ordem
    # que o teste do catálogo exige.
    ToolSchema(
        name="write_file",
        description=(
            "Escreve conteúdo de texto num arquivo no computador DO USUÁRIO, em uma "
            "chamada. USE ESTA em vez de `run_command` com `cat <<EOF`/`echo >` sempre "
            "que for gravar texto que você mesmo escreveu: pelo shell o conteúdo vira "
            "heredoc, e aspas, crase, cifrão ou a palavra EOF dentro do texto corrompem "
            "o arquivo sem erro nenhum. Serve pra criar script, anotação, .env, config, "
            "csv, markdown. NÃO serve pra entregar arquivo que você gerou no seu sandbox "
            "e já publicou no catálogo — esse é o `save_generated_file` (que carrega os "
            "bytes originais, inclusive binário). Nunca sobrescreve às cegas: se o "
            "arquivo já existir com mode='write', devolve status='conflict' com os dados "
            "do que está lá pra você decidir ou perguntar."
        ),
        module="desktop",
        execution="client-side",
        parameters={
            "path": ToolParameter(
                type="string",
                description=(
                    "Caminho ABSOLUTO ou começando com '~/' (ex: '~/Desktop/notas.md'). "
                    "Caminho relativo é recusado — o diretório de trabalho do app não é o "
                    "do usuário. Só escreva onde o usuário pediu; não invente pasta."
                ),
                required=True,
            ),
            "content": ToolParameter(
                type="string",
                description="O texto a gravar, exatamente como deve ficar no arquivo.",
                required=True,
            ),
            "mode": ToolParameter(
                type="string",
                description=(
                    "'write' (padrão) substitui o arquivo; 'append' acrescenta ao fim. "
                    "'append' nunca dá conflito — acrescentar não destrói o que existe."
                ),
                required=False,
                enum=["write", "append"],
            ),
            "overwrite": ToolParameter(
                type="boolean",
                description=(
                    "Só passe true DEPOIS de um status='conflict' e de ter certeza (ou de o "
                    "usuário confirmar). Na primeira tentativa NUNCA mande true — deixe a "
                    "ferramenta detectar a colisão."
                ),
                required=False,
            ),
        },
    ),
]

# Tools "client-actuating" que o núcleo cognitivo aciona no meio do turno: elas
# NÃO executam no processo do core — o loop cognitivo levanta
# ClientActuationRequested e o resultado volta pelo round-trip
# tool_request/tool_result (mesmo trilho de save_generated_file/execute_command).
FILE_ACTUATION_TOOL_NAMES: frozenset[str] = frozenset(
    {"request_user_file", "save_generated_file", "write_file"}
)

# Shell no MESMO trilho client-actuating (gated por neo_brain_shell_enabled): o
# núcleo cognitivo chama `run_command`, o loop levanta ClientActuationRequested e
# _emit_client_actuation emite um `command_request` (a MESMA aprovação do fluxo
# CLIENT_TOOL do classificador). O resultado volta como `execute_command` e o turno
# cognitivo é retomado com a saída real em mãos.
SHELL_ACTUATION_TOOL_NAMES: frozenset[str] = frozenset({"run_command"})

# Media actuation tools (open_url / launch_uri / media_command) are intentionally
# NOT in DESKTOP_TOOLS: they must never be agent-selectable, or the LLM picks
# launch_uri and INVENTS a spotify: URI (→ "música não encontrada"). They are
# dispatched ONLY by _handle_music after a real Spotify/YouTube search, executed by
# name on the desktop (neo-desktop-tauri: is_media_tool) and confirmed by name in
# process_tool_result (_MUSIC_ACTUATION_TOOLS). See neo-music-feature.
