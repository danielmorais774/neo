"""O site barrou o sandbox (session-3ed86282, 17/11) e o arquivo que já está no Mac.

O yt-dlp do sandbox levou "Sign in to confirm you're not a bot" três vezes, e o
cérebro caiu num job do navegador com SaveFrom que falhou em 23 passos. O mesmo
yt-dlp no Mac baixou em 3,4 s. O aviso manda para a máquina dele, e o arquivo
de lá é entregue com `[[FILE:…]]` (cartão com "Mostrar no Finder"), não com
`file_save`.
"""

from __future__ import annotations

from types import SimpleNamespace

from modules.desktop.ai_service import (
    _AVISO_ORIGEM_BLOQUEADA,
    _AVISO_ORIGEM_BLOQUEADA_SEM_MAQUINA,
    _LOCAL_FILE_INSTRUCTION,
    LOCAL_FILE_MARKER_PREFIX,
    _aviso_origem_bloqueada,
    _origem_bloqueou,
)

# Saídas reais do diário da session-3ed86282 (aspas tipográficas incluídas).
_YOUTUBE = (
    "[youtube] nRuiacgrY0w: Downloading tv player API JSON\n"
    "ERROR: [youtube] nRuiacgrY0w: Sign in to confirm you’re not a bot. This helps "
    "protect our community. Use --cookies-from-browser or --cookies for the "
    "authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ"
)
_GENERICO = (
    "ERROR: [Instagram] C1x: Requested content is not available, rate-limit reached "
    "or login required. Use --cookies-from-browser or --cookies for the authentication."
)


def _res(stdout: str, stderr: str = "") -> dict:
    return {"ok": True, "exit_code": 0, "stdout": stdout, "stderr": stderr, "command": "yt-dlp …"}


def test_bot_check_do_youtube_dispara():
    assert _origem_bloqueou("neo_bash", _res(_YOUTUBE))


def test_aspas_retas_e_stderr_tambem():
    assert _origem_bloqueou("neo_bash", _res("", "Sign in to confirm you're not a bot"))


def test_pedido_de_cookie_de_outro_extrator_dispara():
    assert _origem_bloqueou("neo_bash", _res(_GENERICO))


def test_download_que_deu_certo_nao_dispara():
    ok = "[download] 100% of 3.73MiB\n/workspace/taca/video.mp4\n"
    assert not _origem_bloqueou("neo_bash", _res(ok))


def test_so_vale_para_o_neo_bash():
    assert not _origem_bloqueou("web_fetch", _res(_YOUTUBE))
    assert not _origem_bloqueou("neo_bash", "texto solto")


def test_com_run_command_o_aviso_manda_para_a_maquina_dele():
    tools = [SimpleNamespace(name="neo_bash"), SimpleNamespace(name="run_command")]
    aviso = _aviso_origem_bloqueada(tools)
    assert aviso is _AVISO_ORIGEM_BLOQUEADA
    assert "`run_command`" in aviso
    assert "--print after_move:filepath" in aviso
    assert LOCAL_FILE_MARKER_PREFIX in aviso
    assert "SaveFrom" in aviso, "o desvio do 3ed86282 tem que estar nomeado"


def test_sem_run_command_o_aviso_nao_promete_rota():
    aviso = _aviso_origem_bloqueada([SimpleNamespace(name="neo_bash")])
    assert aviso is _AVISO_ORIGEM_BLOQUEADA_SEM_MAQUINA
    assert "run_command" not in aviso


def test_instrucao_do_arquivo_local():
    assert LOCAL_FILE_MARKER_PREFIX in _LOCAL_FILE_INSTRUCTION
    assert "Mostrar no Finder" in _LOCAL_FILE_INSTRUCTION
    assert "/workspace" in _LOCAL_FILE_INSTRUCTION, "sandbox continua no file_save"


def test_resposta_do_run_command_sabe_do_cartao_de_arquivo():
    """Caminho CLIENT_TOOL (classificador escolheu `run_command yt-dlp`): a frase
    final é escrita pelo `_format_tool_result`, que precisa conhecer o marcador."""
    import inspect

    from modules.desktop.ai_service import AIService

    fonte = inspect.getsource(AIService._format_tool_result)
    assert "[[FILE:" in fonte and "Mostrar no Finder" in fonte
