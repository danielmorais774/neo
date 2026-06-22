"""Desktop module tools - definições das tools do neo-desktop."""

from shared import ToolParameter, ToolSchema

# Tools disponíveis para o neo-desktop
DESKTOP_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="list_directory",
        description="Lista conteúdo de um diretório. USE SEMPRE esta ferramenta em vez de 'ls' via run_command. Retorna nome, tipo, tamanho e data de cada item.",
        module="desktop",
        execution="client-side",
        parameters={
            "path": ToolParameter(
                type="string",
                description="Caminho do diretório a listar",
                required=True,
            ),
            "recursive": ToolParameter(
                type="boolean",
                description="Se deve listar recursivamente",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="read_file",
        description="Lê conteúdo de um arquivo. USE SEMPRE esta ferramenta em vez de 'cat' via run_command. Retorna o conteúdo completo do arquivo. Possui tratamento de erros embutido.",
        module="desktop",
        execution="client-side",
        parameters={
            "path": ToolParameter(
                type="string",
                description="Caminho do arquivo",
                required=True,
            ),
            "encoding": ToolParameter(
                type="string",
                description="Encoding do arquivo (padrão: utf-8)",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="write_file",
        description="Escreve conteúdo em um arquivo. USE SEMPRE esta ferramenta em vez de 'echo >' ou 'tee' via run_command. Cria diretórios pais automaticamente se não existirem.",
        module="desktop",
        execution="client-side",
        parameters={
            "path": ToolParameter(
                type="string",
                description="Caminho do arquivo",
                required=True,
            ),
            "content": ToolParameter(
                type="string",
                description="Conteúdo a escrever",
                required=True,
            ),
            "mode": ToolParameter(
                type="string",
                description="Modo: 'write' (sobrescrever) ou 'append'",
                enum=["write", "append"],
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="create_directory",
        description="Cria um novo diretório",
        module="desktop",
        execution="client-side",
        parameters={
            "path": ToolParameter(
                type="string",
                description="Caminho do diretório a criar",
                required=True,
            ),
            "recursive": ToolParameter(
                type="boolean",
                description="Criar diretórios pais se necessário",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="delete_path",
        description="Remove arquivo ou diretório",
        module="desktop",
        execution="client-side",
        parameters={
            "path": ToolParameter(
                type="string",
                description="Caminho a remover",
                required=True,
            ),
            "recursive": ToolParameter(
                type="boolean",
                description="Remover diretório recursivamente",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="run_command",
        description=(
            "Executa qualquer comando shell no terminal do usuário. "
            "Use esta ferramenta sempre que precisar: rodar scripts, instalar pacotes, "
            "verificar processos (ps, top), espaço em disco (df -h, du), "
            "variáveis de ambiente (env), versões de software (python --version, node -v), "
            "gerenciar serviços, compilar código, mover/copiar arquivos via shell, "
            "ou qualquer outra operação que requeira o terminal do usuário."
        ),
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
        name="search_files",
        description="Busca arquivos por padrão (nome). USE SEMPRE esta ferramenta em vez de 'find' via run_command para encontrar arquivos.",
        module="desktop",
        execution="client-side",
        parameters={
            "pattern": ToolParameter(
                type="string",
                description="Padrão de busca (glob ou regex)",
                required=True,
            ),
            "directory": ToolParameter(
                type="string",
                description="Diretório raiz da busca",
                required=True,
            ),
            "max_results": ToolParameter(
                type="integer",
                description="Máximo de resultados",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="get_system_info",
        description="Obtém informações do sistema",
        module="desktop",
        execution="client-side",
        parameters={
            "info_type": ToolParameter(
                type="string",
                description="Tipo de info: 'os', 'cpu', 'memory', 'disk', 'network'",
                enum=["os", "cpu", "memory", "disk", "network", "all"],
                required=False,
            ),
        },
    ),
]

# Media actuation tools (open_url / launch_uri / media_command) are intentionally
# NOT in DESKTOP_TOOLS: they must never be agent-selectable, or the LLM picks
# launch_uri and INVENTS a spotify: URI (→ "música não encontrada"). They are
# dispatched ONLY by _handle_music after a real Spotify/YouTube search, executed by
# name on the desktop (neo-desktop-tauri: is_media_tool) and confirmed by name in
# process_tool_result (_MUSIC_ACTUATION_TOOLS). See neo-music-feature.
