"""Guarda de valor afirmado — a resposta destaca um valor que o arquivo publicado não tem.

session-20cb02b6, turno 4: "Acadêmico **Carlos Daniel Morais**, R.A. **12345678-9**,
Prazo **29/11/2026 até 23:59**" com a célula do R.A. vazia na verificação do próprio
Neo. A guarda lê o blob do catálogo (sem LLM) e devolve um round extra.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.files.conferencia import ausentes, texto_do_arquivo, valores_afirmados

RESPOSTA_T4 = (
    "Remontado do jeito certo. O `questoes_preenchido.docx` agora tem:\n"
    "- **Cabeçalho** (na tabela do template): Acadêmico **Carlos Daniel Morais**, R.A. "
    "**12345678-9**, Prazo **29/11/2026 até 23:59**\n"
    "- **As 10 questões** com enunciado completo e a tua resposta marcada com `[ X ]`."
)


def _docx(caminho: Path, *paragrafos: str, cabecalho: str = "") -> Path:
    corpo = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragrafos)
    with zipfile.ZipFile(caminho, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="w"><w:body>{corpo}</w:body></w:document>')
        if cabecalho:
            z.writestr("word/header1.xml", f'<w:hdr xmlns:w="w"><w:p><w:r><w:t>{cabecalho}</w:t></w:r></w:p></w:hdr>')
        z.writestr("[Content_Types].xml", "<Types/>")
    return caminho


def test_so_valor_concreto_conta_como_afirmado():
    assert valores_afirmados(RESPOSTA_T4) == ["Carlos Daniel Morais", "12345678-9", "29/11/2026 até 23:59"]
    assert valores_afirmados("Pronto, **feito**. Salvei `relatorio.md` em `/workspace/x`.") == []


def test_texto_do_docx_inclui_corpo_e_cabecalho(tmp_path):
    arq = _docx(tmp_path / "a.docx", "Acadêmico: Carlos Daniel Morais", "R.A.", cabecalho="Prazo: 29/11")
    texto = texto_do_arquivo(arq, "a.docx")
    assert "Acadêmico: Carlos Daniel Morais" in texto and "Prazo: 29/11" in texto
    assert ausentes(valores_afirmados(RESPOSTA_T4), texto) == ["12345678-9", "29/11/2026 até 23:59"]
    assert texto_do_arquivo(tmp_path / "a.docx", "foto.png") is None


def test_comparacao_ignora_acento_caixa_e_espaco(tmp_path):
    arq = _docx(tmp_path / "b.docx", "PRAZO: 29/11/2026   ATE 23:59")
    assert ausentes(["29/11/2026 até 23:59"], texto_do_arquivo(arq, "b.docx")) == []


# ── no laço ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resposta_com_valor_que_o_arquivo_nao_tem_ganha_round_de_conferencia(tmp_path):
    from core.cognitive import cognitive_tools_for
    from shared import ToolCall
    from shared.types import Message, MessageRole
    from tests.test_cognitive_tool_loop import _FakeExecutor, _Resp, _ScriptedProvider, _svc

    blob = _docx(tmp_path / "blob", "Acadêmico: Carlos Daniel Morais", "R.A.")
    svc = _svc()
    svc.file_store = SimpleNamespace(
        get=_async(SimpleNamespace(name="questoes_preenchido.docx", sha256="abc")),
        blob_path=lambda sha: blob,
    )
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="file_save", arguments={"path": "/workspace/files/q.docx"})]),
        _Resp(content=RESPOSTA_T4),
        _Resp(content="Conferi: o R.A. NÃO entrou na tabela. Vou corrigir."),
    ])
    messages = [Message(role=MessageRole.USER, content="faz isso")]
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=messages, req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()),
        tool_executor=_FakeExecutor({"file_id": str(uuid4()), "name": "questoes_preenchido.docx", "size_bytes": 1}),
        slog=None, trace=None,
    )
    assert out.startswith("Conferi: o R.A. NÃO entrou")
    pare = next(m.content for m in messages if "PARE: sua resposta diz" in (m.content or ""))
    assert "«12345678-9»" in pare and "Carlos Daniel Morais" not in pare


@pytest.mark.asyncio
async def test_sem_arquivo_publicado_ou_formato_ilegivel_nao_acusa(tmp_path):
    from tests.test_cognitive_tool_loop import _svc

    svc = _svc()
    svc.file_store = SimpleNamespace(get=_async(SimpleNamespace(name="x.pdf", sha256="a")),
                                     blob_path=lambda sha: tmp_path / "nada")
    assert await svc._valores_ausentes_no_arquivo(RESPOSTA_T4, []) is None
    assert await svc._valores_ausentes_no_arquivo(RESPOSTA_T4, [{"file_id": "1", "name": "x.pdf"}]) is None


def _async(valor):
    async def f(*_a, **_k):
        return valor
    return f
