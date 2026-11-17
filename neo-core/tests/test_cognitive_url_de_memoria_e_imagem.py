"""URL de memória que falhou → aviso para buscar; imagem do sandbox → catálogo.

Session-7ea5427a (Big Buck Bunny): o cérebro baixou de URLs que tirou da memória
(404 no blender.org, 403 no Google) e seguiu chutando. No corpus de produção, 15
vezes em que a URL de memória falhou, 14 seguiram com outro chute, só 2 chegaram
ao `web_search`. Reenvio do request real: 5/5 chutaram de novo sem aviso, 5/5
buscaram com ele.

Session-b3ccdf21 (bandeiras): o PNG foi publicado com `file_save`, mas a resposta
mostrou `[[IMAGE:/workspace/...]]`, que no Mac vira "Não achei o arquivo".
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.authority import TurnAuthority, bind_authority, reset_authority
from core.cognitive import cognitive_tools_for
from modules.desktop.ai_service import (
    AIService,
    _AVISO_URL_DE_MEMORIA,
    _INLINE_IMAGE_INSTRUCTION,
    _download_falhou,
    _imagens_do_sandbox_para_catalogo,
)
from shared import ToolCall, ToolResult
from shared.types import Message, MessageRole


# ── o detector, com as formas do corpus de produção ──────────────────────────

def _r(stdout="", stderr="", exit_code=0, command="curl -sL https://x.org/a.mp4 -o a.mp4"):
    return {"ok": exit_code == 0, "exit_code": exit_code, "stdout": stdout,
            "stderr": stderr, "command": command}


@pytest.mark.parametrize("resultado", [
    _r("404 146\ntotal 12\n-rw-r--r-- 1 neo neo 146 bbb.mp4\n"),                  # curl -w no começo
    _r("403 394\nbbb.mp4: XML 1.0 document, ASCII text\n"),
    _r("== https://raw.githubusercontent.com/x/y.wav\nHTTP/2 404 \r\ncontent-type: text/plain"),
    _r("--- teste ---\nHTTP 403 | 2124 bytes\n"),
    _r("=== 1) CKAN ===\nhttp=403 size=427\n<HTML><HEAD>\n<TITLE>Access Denied</TITLE>"),
    _r("ERRO spinning-jenny.jpg: HTTP Error 403: Forbidden\n"),
    _r("", "Traceback (most recent call last):\nurllib.error.HTTPError: HTTP Error 403: Forbidden", 1,
       "python3 -c 'import urllib.request; urllib.request.urlopen(\"https://x.org\")'"),
    _r('{"status":"error","code":404,"message":"The requested URL returned error: 429"}'),
    _r('=== Naruto ===\n{"status":504,"type":"BadResponseException"}'),
    _r('<!DOCTYPE html>\n<html lang="en">\n<title>Wikimedia Error</title>\n'),
    _r("Please honor our robot policy\n< HTTP/1.1 200 Connection established\r\n< HTTP/2 403 \r\n"),
    _r("", "", 8, "wget -qO /workspace/a.png https://static.wikia.nocookie.net/a.png"),   # wget -q mudo
    _r("", "curl: (6) Could not resolve host: x.org", 6),
])
def test_download_que_nao_veio(resultado):
    assert _download_falhou(resultado)


@pytest.mark.parametrize("resultado", [
    _r("200 61878609\nduration=596.504000\n"),
    # Três URLs de uma vez: uma falhou, mas duas vieram — não é hora de buscar.
    _r("== a\n200 969201\nt.mp4: ISO Media, MP4\n== b\n404 146\nt.mp4: HTML document\n== c\n200 61878609\n"),
    _r("spinning-jenny.jpg: JPEG image data, JFIF standard 1.01 | 74528 bytes"),
    _r("HTTP 200 | 74528 bytes | https://upload.wikimedia.org/a.jpg"),
    # "404" dentro de um id de foto não é status.
    _r("results: 6\n'Naruto' | https://live.staticflickr.com/7331/9404155015_95482cd303.jpg"),
    # Erro de Python que não é de rede.
    _r("", "NameError: name 'pop' is not defined", 1, "python3 parse.py"),
    _r("", "", 1, "ls /workspace/nada"),
    "não é dict",
])
def test_download_que_veio_ou_nao_e_download(resultado):
    assert not _download_falhou(resultado)


# ── no loop ──────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, content="", tool_calls=None):
        self._content = content
        self._tc = tool_calls or []

    def get_content(self):
        return self._content

    def get_tool_calls(self):
        return self._tc


class _Provider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.rodadas: list[list[Message]] = []

    async def complete(self, *, messages, tools=None, **kwargs):
        self.rodadas.append(list(messages))
        return self._responses.pop(0)


class _Executor:
    def __init__(self, resultado, nome="neo_bash"):
        self._resultado = resultado
        self._nome = nome

    async def execute_tool_call(self, tool_call):
        return ToolResult(
            tool_call_id=tool_call.id, name=tool_call.name, result=self._resultado,
            execution_time=0.0,
        )


def _svc():
    svc = object.__new__(AIService)

    class _S:
        cognitive_max_tool_rounds = 5
        default_max_tokens = 1024
        cognitive_stream_enabled = False
        cognitive_reasoning_enabled = False

    svc.settings = _S()
    return svc


_URL = "https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_320x180.mp4"
_COMANDO = f'cd /workspace && curl -sL -o bbb.mp4 "{_URL}" -w "%{{http_code}} %{{size_download}}\\n"'
_404 = {"ok": True, "exit_code": 0, "stdout": "404 146\n", "stderr": "", "command": _COMANDO}
_200 = {"ok": True, "exit_code": 0, "stdout": "200 61878609\n", "stderr": "", "command": _COMANDO}


async def _rodar(resultado, autoridade: TurnAuthority | None, produced=None):
    svc = _svc()
    provider = _Provider([
        _Resp(tool_calls=[ToolCall(id="1", name="neo_bash", arguments={"command": _COMANDO})]),
        _Resp(content="feito"),
    ])
    token = bind_authority(autoridade) if autoridade is not None else None
    try:
        await svc._run_cognitive_tool_loop(
            provider=provider,
            messages=[Message(role=MessageRole.USER, content="baixa o Big Buck Bunny")],
            req_model=None, model_name="m", provider_name="p",
            tools=cognitive_tools_for(uuid4()), tool_executor=_Executor(resultado),
            slog=None, trace=None, produced_files=produced,
        )
    finally:
        if token is not None:
            reset_authority(token)
    return provider.rodadas[-1][-1].content


@pytest.mark.asyncio
async def test_url_de_memoria_que_falhou_ganha_o_aviso():
    resultado = await _rodar(_404, TurnAuthority())
    assert resultado.endswith(_AVISO_URL_DE_MEMORIA)
    assert "Tool: neo_bash\nResult:" in resultado


@pytest.mark.asyncio
async def test_url_que_o_daniel_deu_nao_ganha_aviso():
    autoridade = TurnAuthority()
    autoridade.note_user_text(f"baixa daqui: {_URL}")
    assert _AVISO_URL_DE_MEMORIA not in await _rodar(_404, autoridade)


@pytest.mark.asyncio
async def test_url_vista_numa_busca_nao_ganha_aviso():
    autoridade = TurnAuthority()
    autoridade.note_tool_result("web_search", f"Big Buck Bunny — {_URL}")
    assert _AVISO_URL_DE_MEMORIA not in await _rodar(_404, autoridade)


@pytest.mark.asyncio
async def test_url_de_memoria_que_funcionou_nao_ganha_aviso():
    """As bandeiras do flagcdn vieram de memória e deram certo: chute barato que acerta fica."""
    assert _AVISO_URL_DE_MEMORIA not in await _rodar(_200, TurnAuthority())


@pytest.mark.asyncio
async def test_fora_de_turno_nao_avisa():
    assert _AVISO_URL_DE_MEMORIA not in await _rodar(_404, None)


# ── imagem do sandbox → catálogo ────────────────────────────────────────────

_PUBLICADO = [{
    "file_id": "ad90611b-d904-4a76-b649-6016e6b63ebb",
    "name": "bandeiras_br_ar_uy.png",
    "size_bytes": 95476,
    "path": "/workspace/bandeiras/bandeiras_br_ar_uy.png",
}]


def test_marcador_do_sandbox_vira_o_do_catalogo():
    texto = (
        "Pronto. Brasil, Argentina e Uruguai lado a lado.\n\n"
        "[[IMAGE:/workspace/bandeiras/bandeiras_br_ar_uy.png]]\n\n"
        "O botão de download já está aí."
    )
    novo, trocados = _imagens_do_sandbox_para_catalogo(texto, _PUBLICADO)
    assert "[[IMAGE:file:ad90611b-d904-4a76-b649-6016e6b63ebb]]" in novo
    assert "/workspace" not in novo
    assert trocados == ["/workspace/bandeiras/bandeiras_br_ar_uy.png"]


def test_legenda_fica_e_caminho_com_ponto_casa():
    texto = "[[IMAGE:/workspace/bandeiras/./bandeiras_br_ar_uy.png|as três bandeiras]]"
    novo, _ = _imagens_do_sandbox_para_catalogo(texto, _PUBLICADO)
    assert novo == "[[IMAGE:file:ad90611b-d904-4a76-b649-6016e6b63ebb|as três bandeiras]]"


def test_sem_caminho_casa_pelo_nome_unico():
    publicado = [{"file_id": "f1", "name": "grafico.png"}]
    novo, _ = _imagens_do_sandbox_para_catalogo("[[IMAGE:/workspace/x/grafico.png]]", publicado)
    assert novo == "[[IMAGE:file:f1]]"


def test_nome_repetido_nao_adivinha():
    publicado = [{"file_id": "f1", "name": "grafico.png"}, {"file_id": "f2", "name": "grafico.png"}]
    texto = "[[IMAGE:/workspace/x/grafico.png]]"
    assert _imagens_do_sandbox_para_catalogo(texto, publicado) == (texto, [])


def test_o_que_nao_foi_publicado_ou_nao_e_sandbox_fica_igual():
    texto = (
        "[[IMAGE:/workspace/outro.png]]\n[[IMAGE:/Users/daniel/Desktop/foto.jpg]]\n"
        "[[IMAGE:https://flagcdn.com/w320/br.png]]\n[[IMAGE:file:abc]]"
    )
    assert _imagens_do_sandbox_para_catalogo(texto, _PUBLICADO) == (texto, [])
    assert _imagens_do_sandbox_para_catalogo("sem marcador", _PUBLICADO) == ("sem marcador", [])
    assert _imagens_do_sandbox_para_catalogo(texto, []) == (texto, [])


@pytest.mark.asyncio
async def test_file_save_guarda_o_caminho_do_sandbox():
    svc = _svc()
    provider = _Provider([
        _Resp(tool_calls=[ToolCall(id="1", name="file_save", arguments={
            "path": "/workspace/bandeiras/bandeiras_br_ar_uy.png", "name": "bandeiras_br_ar_uy.png",
        })]),
        _Resp(content="Pronto."),
    ])
    produced: list[dict] = []
    await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="monta")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()),
        tool_executor=_Executor({"file_id": "ad90611b", "name": "bandeiras_br_ar_uy.png",
                                 "size_bytes": 95476}),
        slog=None, trace=None, produced_files=produced,
    )
    assert produced == [{
        "file_id": "ad90611b", "name": "bandeiras_br_ar_uy.png", "size_bytes": 95476,
        "path": "/workspace/bandeiras/bandeiras_br_ar_uy.png",
    }]


def test_prompt_manda_usar_o_catalogo_para_imagem_do_sandbox():
    assert "/workspace/" in _INLINE_IMAGE_INSTRUCTION
    assert "[[IMAGE:file:<file_id>]]" in _INLINE_IMAGE_INSTRUCTION
