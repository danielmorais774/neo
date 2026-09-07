"""DESKTOP_TOOLS só pode listar tools que o neo-desktop-tauri EXECUTA de
verdade. Sessão 2afe5753: o classificador escolheu `list_directory` — uma
tool que estava no catálogo (com "USE SEMPRE" na descrição) mas nunca teve
implementação nenhuma no cliente (nem Rust, nem React) — o ToolRequest saiu e
nunca voltou, travando o turno pelos 300s do timeout do client.

Se algum dia uma dessas tools for implementada de verdade (ver
`save_generated_file` em ai_agent.rs como referência), tire ela desta lista
de proibidas ao mesmo tempo em que a devolve pro DESKTOP_TOOLS.
"""

from modules.desktop.tools import DESKTOP_TOOLS

# `write_file` saiu desta lista em 2026-08-30, seguindo a receita do docstring:
# ganhou execução real ANTES de voltar ao catálogo — `write_text_file` em
# commands/mod.rs, despachada em ai_agent.rs, devolvendo ToolResult com o mesmo
# envelope saved/conflict do save_generated_file.
_KNOWN_DEAD_TOOLS = {
    "list_directory",
    "read_file",
    "create_directory",
    "delete_path",
    "search_files",
    "get_system_info",
}


def test_desktop_tools_catalog_has_no_unimplemented_client_tools():
    names = {t.name for t in DESKTOP_TOOLS}
    dead_but_listed = names & _KNOWN_DEAD_TOOLS
    assert not dead_but_listed, (
        f"{dead_but_listed} está no catálogo mas não tem execução real no "
        "neo-desktop-tauri — vai travar 300s na primeira vez que o "
        "classificador escolher. Implemente em ai_agent.rs primeiro."
    )


def test_desktop_tools_catalog_only_has_working_tools():
    names = {t.name for t in DESKTOP_TOOLS}
    # request_user_file É executado de verdade (intercept em ai_agent.rs, reusando
    # pick_attachment_files + upload_path_to_store) — plano "arquivos sob iniciativa
    # do cérebro". Não é dead tool: tem handler, devolve ToolResult.
    assert names == {
        "run_command",
        "save_generated_file",
        "request_user_file",
        "write_file",
    }
