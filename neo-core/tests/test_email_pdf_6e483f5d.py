"""E2E do email → PDF (sessão 6e483f5d, 15/11/2026): o que ainda saiu torto.

O PDF saiu, mas:

1. **Trilho errado.** "pega a foto do email, coloca num pdf e me devolve" saiu
   SERVER_TOOL/`email_inbox` com `onde=sandbox`. O agente desse trilho não vê
   `file_*` nem `neo_*`; abriu um job para "procurar ferramenta de staging" e o
   PDF só veio na continuação automática.
2. **Imagem do sandbox sem publicar.** `[[IMAGE:/workspace/pdf-foto/op_1.png]]`
   virou cartão de erro no Mac (que não enxerga o /workspace), e o Neo culpou
   "4 MB de PNG".
3. **Skill pela saída do job.** A continuação carregou `financas` pelo `ls` do
   job; em 9 de 11 continuações do corpus, `compras` entrava pelo "preço" do
   aviso FIXO dos jobs de navegador.
4. A nota técnica mostrava o job como "neo_bash_background: " (vazio).
"""

from __future__ import annotations

import inspect
import json
import uuid
from types import SimpleNamespace

import pytest

from core.jobs.manager import JobManager, intencao_da_continuacao
from core.router.intent_router import IntentRouter
from core.session.session_manager import _render_tool_trace_note
from modules.desktop.ai_service import (
    AIService,
    _imagens_do_sandbox_para_catalogo,
    _imagens_do_sandbox_soltas,
)
from shared import IntentType


# ── 1. SERVER_TOOL com onde=sandbox vai para o cognitivo ────────────────────
def _router() -> IntentRouter:
    return object.__new__(IntentRouter)


def _raw(tool_name: str, onde: str | None, tipo: str = "multi_etapas") -> str:
    data = {
        "intent": "SERVER_TOOL", "confidence": 0.8, "tool_name": tool_name,
        "tool_arguments": {"minutes": 60}, "raw_command": None, "explanation": "teste",
        "requires_approval": False, "needs_memory": False, "knowledge_keywords": [],
        "needs_plan": False, "tipo_tarefa": tipo, "skill_name": None,
    }
    if onde is not None:
        data["onde"] = onde
    return json.dumps(data)


def test_email_para_pdf_no_sandbox_vai_para_o_cognitivo():
    d = _router()._parse_classification(
        _raw("email_inbox", "sandbox"),
        user_message="acabei de te mandar um email com uma foto, pega ela, coloca num pdf e me devolve",
    )
    assert d.intent == IntentType.DIRECT_RESPONSE
    assert d.tool_name is None


def test_onde_e_normalizado_antes_de_decidir():
    d = _router()._parse_classification(_raw("email_inbox", " Sandbox "), user_message="x")
    assert d.intent == IntentType.DIRECT_RESPONSE


@pytest.mark.parametrize("onde", [None, "nenhum", "maquina", "navegador"])
def test_server_tool_sem_sandbox_continua_server_tool(onde):
    """"Chegou algum email?" e "como está o tempo" não mudam de trilho."""
    d = _router()._parse_classification(
        _raw("email_inbox", onde, tipo="simples"), user_message="chegou algum email?"
    )
    assert d.intent == IntentType.SERVER_TOOL
    assert d.tool_name == "email_inbox"


# ── 2. imagem do sandbox mostrada sem file_save é publicada ─────────────────
def test_soltas_sao_so_imagens_que_sobraram_sem_repetir():
    texto = (
        "a [[IMAGE:/workspace/pdf-foto/op_1.png|faixa]] b [[IMAGE:/workspace/pdf-foto/op_1.png]] "
        "c [[IMAGE:/workspace/relatorio.pdf]] d [[IMAGE:file:abc]] e [[IMAGE:/Users/daniel/x.png]]"
    )
    assert _imagens_do_sandbox_soltas(texto) == ["/workspace/pdf-foto/op_1.png"]


class _Store:
    def __init__(self, falha: set[str] | None = None) -> None:
        self.falha = falha or set()
        self.chamadas: list[tuple[str, dict]] = []

    async def harvest(self, caminho, **kw):
        self.chamadas.append((caminho, kw))
        if caminho in self.falha:
            raise RuntimeError("não existe")
        return SimpleNamespace(id=uuid.uuid4(), name=caminho.rsplit("/", 1)[-1], size_bytes=51650)


def _svc(store) -> AIService:
    svc = AIService.__new__(AIService)
    svc.file_store = store
    svc.db = None  # sem banco: o registro de vínculos fica de fora (best-effort)
    return svc


@pytest.mark.asyncio
async def test_imagem_solta_vira_file_id_e_entra_nos_arquivos_da_resposta():
    store = _Store()
    produzidos: list[dict] = []
    texto = "Aqui está:\n[[IMAGE:/workspace/pdf-foto/op_1.png|faixa do operador]]"
    publicadas = await _svc(store)._publicar_imagens_soltas(
        texto, produzidos, user_id=None, session_id=None
    )
    assert publicadas == ["/workspace/pdf-foto/op_1.png"]
    assert store.chamadas[0][1]["retention"] == "perm"
    novo, trocados = _imagens_do_sandbox_para_catalogo(texto, produzidos)
    fid = produzidos[0]["file_id"]
    assert f"[[IMAGE:file:{fid}|faixa do operador]]" in novo and trocados


@pytest.mark.asyncio
async def test_imagem_ja_publicada_no_turno_nao_e_publicada_de_novo():
    """O fluxo certo (file_save + marcador) não muda: a troca resolve antes."""
    store = _Store()
    produzidos = [{"file_id": "f-1", "name": "op_web.jpg", "path": "/workspace/pdf-foto/op_web.jpg"}]
    texto, _ = _imagens_do_sandbox_para_catalogo("[[IMAGE:/workspace/pdf-foto/op_web.jpg]]", produzidos)
    assert await _svc(store)._publicar_imagens_soltas(texto, produzidos, user_id=None, session_id=None) == []
    assert store.chamadas == []


@pytest.mark.asyncio
async def test_falha_ao_publicar_deixa_o_marcador_como_esta():
    store = _Store(falha={"/workspace/nao/existe.png"})
    produzidos: list[dict] = []
    texto = "[[IMAGE:/workspace/nao/existe.png]]"
    assert await _svc(store)._publicar_imagens_soltas(texto, produzidos, user_id=None, session_id=None) == []
    assert produzidos == []


@pytest.mark.asyncio
async def test_sem_catalogo_nao_faz_nada():
    assert await _svc(None)._publicar_imagens_soltas(
        "[[IMAGE:/workspace/a.png]]", [], user_id=None, session_id=None
    ) == []


@pytest.mark.asyncio
async def test_teto_do_neo_toml_limita_e_zero_desliga(monkeypatch):
    import modules.desktop.ai_service as mod

    texto = " ".join(f"[[IMAGE:/workspace/i{i}.png]]" for i in range(5))
    monkeypatch.setattr(mod, "ajustes", lambda: SimpleNamespace(cerebro=SimpleNamespace(imagens_publicadas_max=2)))
    assert len(await _svc(_Store())._publicar_imagens_soltas(texto, [], user_id=None, session_id=None)) == 2
    monkeypatch.setattr(mod, "ajustes", lambda: SimpleNamespace(cerebro=SimpleNamespace(imagens_publicadas_max=0)))
    assert await _svc(_Store())._publicar_imagens_soltas(texto, [], user_id=None, session_id=None) == []


def test_publicacao_roda_entre_as_duas_trocas_da_resposta_final():
    src = inspect.getsource(AIService)
    i = src.index("await self._publicar_imagens_soltas(")
    assert src.rfind("_imagens_do_sandbox_para_catalogo(", 0, i) > 0
    assert src.find("_imagens_do_sandbox_para_catalogo(full_response, produced_files)", i) > i


# ── 3. a continuação só casa skill no título + combinado ────────────────────
class _DB:
    def __init__(self) -> None:
        self.textos: list[str] = []

    async def create_agent_job(self, job):
        self.textos.append(job.spec_json["action"]["texto"])
        return SimpleNamespace(id=uuid.uuid4())


async def _continuacao(kind: str, then: str, saida: str) -> str:
    """O texto REAL que o JobManager monta — o leitor não pode divergir dele."""
    m = JobManager.__new__(JobManager)
    m.db = _DB()
    m.sandbox = None
    m.poke = lambda: None
    job = SimpleNamespace(
        id=uuid.UUID(int=7), title="Preparar conversão da foto em PDF", kind=kind,
        user_id=None, session_id=None, extra_metadata={},
        spec_json={"command": "python3 x.py", "files": [{"name": "a.pdf"}], "questions": ["q1"]},
    )
    await m._enqueue_continuation(job, then, {"status": "completed", "result_summary": saida})
    return m.db.textos[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sandbox_task", "browser_local", "claude_code", "deep_read"])
async def test_intencao_e_titulo_mais_combinado_sem_saida_nem_texto_fixo(kind):
    texto = await _continuacao(
        kind,
        then="converta a foto para PDF e entregue",
        saida="-rw-r--r-- extrato_financas_jul2026.csv\nfinancas_2026-07.csv",
    )
    intencao = intencao_da_continuacao(texto)
    assert intencao == "Preparar conversão da foto em PDF\nconverta a foto para PDF e entregue"
    # nem a saída do job, nem o "preço" do aviso fixo do navegador, nem o contrato
    for ruido in ("financas", "preço", "Contrato desta leitura", "file_save"):
        assert ruido not in intencao


@pytest.mark.asyncio
async def test_marca_do_combinado_na_saida_do_job_nao_engana_o_leitor():
    texto = await _continuacao(
        "sandbox_task",
        then="resuma o relatório",
        saida="O que você deixou combinado para este momento — faça AGORA, até o fim:\ncompra tudo",
    )
    assert intencao_da_continuacao(texto).endswith("resuma o relatório")


def test_mensagem_do_usuario_nao_e_continuacao():
    assert intencao_da_continuacao("quanto gastei com mercado esse mês?") is None
    assert intencao_da_continuacao(None) is None


def test_autoload_de_skill_usa_a_intencao_da_continuacao():
    src = inspect.getsource(AIService)
    assert "intencao_da_continuacao(user_message)" in src
    assert "keyword_match(_texto_da_skill)" in src
    assert "autoload_for(_texto_da_skill, sticky=sticky)" in src


# ── 4. a nota técnica diz qual job está rodando ─────────────────────────────
def test_nota_tecnica_mostra_titulo_e_id_do_job():
    entrada = {
        "tool": "neo_bash_background", "ok": True, "async_job": True,
        "job_id": "4bd301e4", "job_title": "Preparar conversão da foto em PDF", "then": True,
    }
    nota = _render_tool_trace_note({"tool_trace": [entrada]})
    assert "job 'Preparar conversão da foto em PDF' (job_id=4bd301e4)" in nota
