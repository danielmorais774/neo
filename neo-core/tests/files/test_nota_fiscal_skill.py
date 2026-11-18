"""Skill nota-fiscal: tool `conferir_cupom` e o aviso de imagem (session-3b19d27a).

O pedido "passa OCR na notinha do e-mail" foi marcado `midia=tela` (o anexo ainda
estava no e-mail), o aviso mandou ler a skill de vídeo, e a conta da vodka foi
feita de cabeça, errada. Aqui:

* a mensagem real carrega a skill nota-fiscal por keyword (corpo + tool no round 1);
* `conferir_cupom` devolve a correção da vodka com confiança alta;
* o resultado do anexo de IMAGEM, num turno de `midia=tela`, avisa uma vez que não é vídeo.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.authority.risk import classes_of, known
from core.files.executor import FilesExecutor
from core.files.tools import CUPOM_TOOLS, FILE_TOOLS
from core.skills.library import SkillLibrary
from modules.desktop.ai_service import AIService, _AVISO_E_IMAGEM, _chegou_imagem
from shared.config import get_settings

_PEDIDO = (
    "te encaminhei um email com a notinha de supermercado quero que passe um OCR "
    "nela, coloque em uma planilha e me retorne o arquivo para eu baixar."
)

_ITENS_LIDOS = [
    {"descricao": "CERVEJA PILSEN SKOL MULTIPACK C15 LT 269", "qtd": "1", "unitario": "44,99", "total": "44,99"},
    {"descricao": "CERVEJA PILSEN SKOL MULTIPACK C15 LT 269", "qtd": "1", "unitario": "44,99", "total": "44,99"},
    {"descricao": "LINGUICA SUINA PERDIGAO kg", "qtd": "2,168", "unitario": "23,99", "total": "52,01"},
    {"descricao": "GELO CUBO 3kg", "qtd": "1", "unitario": "5,99", "total": "5,99"},
    {"descricao": "GELO CUBO 3kg", "qtd": "1", "unitario": "5,99", "total": "5,99"},
    {"descricao": "ENERGETICO RED WAVE 2L", "qtd": "1", "unitario": "17,99", "total": "17,99"},
    {"descricao": "VODKA NAC SHIRNOFF GF 990ML TRI DESTILADA", "qtd": "1", "unitario": "39,99", "total": "33,99"},
    {"descricao": "COPO VIDRO AMERICANO NADIR 190ML", "qtd": "1", "unitario": "1,79", "total": "1,79"},
]


class _Registry:
    def __init__(self, tools):
        self._t = {t.name: t for t in tools}

    def exists(self, name: str) -> bool:
        return name in self._t

    def get(self, name: str):
        return self._t.get(name)


@pytest.fixture
def lib() -> SkillLibrary:
    library = SkillLibrary(
        root="skills", settings=get_settings(), tool_registry=_Registry([*FILE_TOOLS, *CUPOM_TOOLS]),
    )
    library.load()
    return library


# ── a skill ─────────────────────────────────────────────────────────────────

def test_pedido_da_sessao_carrega_a_skill_com_a_tool(lib):
    assert lib.keyword_match(_PEDIDO) == "nota-fiscal"
    carregada = lib.autoload_for(_PEDIDO)
    assert carregada is not None
    entry, corpo, tools = carregada
    assert entry.name == "nota-fiscal"
    assert [t.name for t in tools] == ["conferir_cupom"]
    assert "conferir_cupom" in corpo and "ocr-video" in corpo


@pytest.mark.parametrize("msg", [
    "me passa o total da nota fiscal que te mandei",
    "faz uma planilha do cupom fiscal",
])
def test_outras_frases_de_nota(lib, msg):
    assert lib.keyword_match(msg) == "nota-fiscal"


@pytest.mark.parametrize("msg", [
    "transcreve esse video do whatsapp",
    "gastei 45 no mercado",          # é da financas
    "tem cupom de desconto pra loja?",
])
def test_nao_sequestra_outros_pedidos(lib, msg):
    assert lib.keyword_match(msg) != "nota-fiscal"


def test_conferir_cupom_esta_fora_do_read_set_default():
    from core.cognitive.read_tools import COGNITIVE_READ_TOOLS

    assert "conferir_cupom" not in {t.name for t in COGNITIVE_READ_TOOLS}


def test_conferir_cupom_tem_classe_de_risco_e_nao_atua():
    assert known("conferir_cupom")
    assert classes_of("conferir_cupom") == frozenset()


# ── a tool ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conferir_cupom_corrige_a_vodka():
    ex = FilesExecutor(store=None)
    r = await ex.execute("conferir_cupom", {
        "itens": _ITENS_LIDOS, "valor_a_pagar": "213,74", "qtde_itens": 8,
        "pagamentos": ["150,00", "63,74"],
    })
    assert r["ok"] is False
    assert r["fecha"] is False
    assert r["todas_as_linhas_lidas"] is True
    assert r["soma_dos_itens"] == "207.74"
    assert r["correcoes"] == [{"item": 7, "de": "33.99", "para": "39.99", "confianca": "alta"}]
    assert "33,99 → 39,99" in r["conferencia"]


@pytest.mark.asyncio
async def test_conferir_cupom_leitura_certa():
    itens = [dict(i) for i in _ITENS_LIDOS]
    itens[6]["total"] = "39,99"
    r = await FilesExecutor(store=None).execute("conferir_cupom", {
        "itens": itens, "valor_a_pagar": "213,74", "qtde_itens": 8, "pagamentos": ["150,00", "63,74"],
    })
    assert r["ok"] is True and r["fecha"] is True and r["todas_as_linhas_lidas"] is True
    assert "correcoes" not in r
    assert "não precisa reler" in r["conferencia"]


@pytest.mark.asyncio
async def test_conferir_cupom_sem_itens_recusa_com_motivo():
    r = await FilesExecutor(store=None).execute("conferir_cupom", {"itens": [{"descricao": "x"}]})
    assert r["error"] == "bad_args"
    assert "sem 'total'" in r["message"]


# ── imagem ≠ vídeo ──────────────────────────────────────────────────────────

_ANEXO = {
    "ok": True, "file_id": "495fb528", "name": "IMG_20260818_223748.jpg",
    "mime": "image/jpeg", "size_bytes": 275518,
}


@pytest.mark.parametrize(
    "nome, resultado, esperado",
    [
        ("email_anexo", _ANEXO, True),
        ("file_stage", {"path": "/workspace/files/notinha-mercado.jpg"}, True),
        ("file_stage", {"path": "/workspace/files/gravacao.mp4"}, False),
        ("email_anexo", {"name": "boleto.pdf", "mime": "application/pdf"}, False),
        ("neo_bash", _ANEXO, False),
        ("email_anexo", "texto", False),
    ],
)
def test_chegou_imagem(nome, resultado, esperado):
    assert _chegou_imagem(nome, resultado) is esperado


def _svc():
    svc = object.__new__(AIService)
    svc.file_store = None
    svc._origin_annotator = lambda u, s: None
    return svc


@pytest.mark.asyncio
async def test_imagem_em_turno_de_video_avisa_uma_vez():
    anotar = _svc()._anotador_do_turno("u", "s", midia="tela")
    assert await anotar("email_anexo", {"id": "145", "anexo": 1}, _ANEXO) == _AVISO_E_IMAGEM
    assert await anotar("file_stage", {}, {"path": "/workspace/files/notinha-mercado.jpg"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("midia", [None, "fala"])
async def test_sem_aviso_de_video_nao_ha_aviso_de_imagem(midia):
    anotar = _svc()._anotador_do_turno("u", "s", midia=midia)
    assert await anotar("email_anexo", {}, _ANEXO) is None
