"""file_olhar — o cérebro só lê texto; a página renderizada vai para o modelo de visão.

Na session-20cb02b6 o Neo conferiu o .docx por texto extraído e entregou sem ver que
o logo do cabeçalho tinha sumido. A tool tem de: ler só dentro do /workspace, só
imagem, mandar a imagem de fato ao modelo de visão e devolver texto.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.files.executor import FilesExecutor
from core.files.store import FileStoreError
from shared.types import MessageRole


class _Store:
    def __init__(self, raiz: Path) -> None:
        self.raiz = raiz

    def resolve_workspace_path(self, path: str) -> Path:
        alvo = (self.raiz / path.removeprefix("/workspace/")).resolve()
        if self.raiz.resolve() not in alvo.parents:
            raise FileStoreError("bad_path", f"caminho fora do workspace: {path!r}")
        return alvo


class _Resp:
    def __init__(self, texto: str) -> None:
        self.texto = texto

    def get_content(self) -> str:
        return self.texto


class _Provider:
    def __init__(self, texto: str) -> None:
        self.texto = texto
        self.chamadas: list[dict] = []

    async def complete(self, **kw):
        self.chamadas.append(kw)
        return _Resp(self.texto)


class _LLM:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider
        self.modelos: list[str] = []

    def get_provider_for_model(self, modelo: str):
        self.modelos.append(modelo)
        return self.provider


def _pagina(raiz: Path, nome: str = "pagina-1.png") -> str:
    (raiz / "files").mkdir(parents=True, exist_ok=True)
    (raiz / "files" / nome).write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return f"/workspace/files/{nome}"


@pytest.mark.asyncio
async def test_manda_a_pagina_ao_modelo_de_visao_e_devolve_o_que_ele_viu(tmp_path):
    provider = _Provider("1) logo no topo. 2) Acadêmico: Carlos Daniel Morais; R.A.: VAZIO.")
    llm = _LLM(provider)
    ex = FilesExecutor(_Store(tmp_path), llm=llm)

    r = await ex.execute("file_olhar", {"imagens": [_pagina(tmp_path)], "pergunta": "o R.A. está preenchido?"})

    assert r["ok"] and "R.A.: VAZIO" in r["o_que_se_ve"]
    chamada = provider.chamadas[0]
    usuario = chamada["messages"][1]
    assert usuario.role == MessageRole.USER and usuario.content == "o R.A. está preenchido?"
    assert usuario.images[0].startswith("data:image/png;base64,")
    assert chamada["disable_thinking"] is True and chamada["model"] == llm.modelos[0]


@pytest.mark.asyncio
async def test_recusa_fora_do_workspace_e_o_que_nao_e_imagem(tmp_path):
    ex = FilesExecutor(_Store(tmp_path), llm=_LLM(_Provider("x")))
    fora = await ex.execute("file_olhar", {"imagens": ["/workspace/../etc/passwd.png"], "pergunta": "?"})
    assert fora["error"] == "bad_path"

    (tmp_path / "files").mkdir(parents=True, exist_ok=True)
    (tmp_path / "files" / "saida.docx").write_bytes(b"PK")
    docx = await ex.execute("file_olhar", {"imagens": ["/workspace/files/saida.docx"], "pergunta": "?"})
    assert docx["error"] == "bad_format" and "renderize" in docx["message"]


@pytest.mark.asyncio
async def test_sem_llm_ou_resposta_vazia_diz_por_que(tmp_path):
    sem = await FilesExecutor(_Store(tmp_path)).execute("file_olhar", {"imagens": ["x.png"], "pergunta": "?"})
    assert sem["error"] == "sem_visao"
    vazia = await FilesExecutor(_Store(tmp_path), llm=_LLM(_Provider(""))).execute(
        "file_olhar", {"imagens": [_pagina(tmp_path)], "pergunta": "?"}
    )
    assert vazia["error"] == "visao_vazia"


@pytest.mark.asyncio
async def test_no_maximo_quatro_imagens_por_chamada(tmp_path):
    provider = _Provider("ok")
    ex = FilesExecutor(_Store(tmp_path), llm=_LLM(provider))
    caminhos = [_pagina(tmp_path, f"pagina-{i}.png") for i in range(6)]
    r = await ex.execute("file_olhar", {"imagens": caminhos, "pergunta": "?"})
    assert r["imagens"] == 4 and len(provider.chamadas[0]["messages"][1].images) == 4
