"""Conferências da deep_read antes de gastar minutos lendo (docs/deep-read-plan.md §10).

Todas vêm da sessão ad3be3bf: o Foxtrot ficou de fora porque o Neo inventariou a
cópia velha do /workspace e não a pasta do usuário; os planos foram lidos a partir
de .txt extraídos numa sessão anterior (sem páginas); as perguntas trocaram
"regulamentação" por nove eixos genéricos; e a correção abriu uma leitura
nova em vez de acrescentar à existente.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.jobs.deep_read_tool import parse_listing, user_dirs
from core.jobs.executor import JobToolExecutor
from db.models.agent_job import AgentJob
from shared import ToolCall
from tests.documents.test_deep_read_job import _JM, _PathSandbox
from tests.test_jobs import _FakeDB

PASTA = "/Users/daniel/Documents/analises/relatorios-anuais-2026"
PEDIDO = (
    f"nessa pasta {PASTA} tem varios relatórios anuais, vamos analisar eles e montar uma conclusao em um novo "
    "aquivo .md, vamoa analisa aumento de carga tributaria, regulamentação e metas irrealistas"
)
LS = f"""$ ls -la "{PASTA}/"
total 58528
drwxr-xr-x@  9 daniel  staff       288 15 set 19:06 .
drwxr-xr-x  10 daniel  staff       320 15 set 22:44 ..
-rw-r--r--@  1 daniel  staff   4927342 15 set 19:04 Relatorio_Alfa.pdf
-rw-r--r--@  1 daniel  staff  20260103 15 set 19:04 Relatorio_Bravo.pdf
-rw-r--r--@  1 daniel  staff      3222 15 set 19:06 FONTES.md
-rw-r--r--@  1 daniel  staff   1189907 15 set 19:04 Relatorio_Charlie.pdf
-rw-r--r--@  1 daniel  staff   1691345 15 set 19:04 Relatorio_Delta.pdf
-rw-r--r--@  1 daniel  staff    882639 15 set 19:04 Relatorio_Echo.pdf
-rw-r--r--@  1 daniel  staff   1000707 15 set 19:04 Relatorio_Foxtrot.pdf"""
PLANOS = ["Relatorio_Alfa", "Relatorio_Bravo", "Relatorio_Charlie", "Relatorio_Delta", "Relatorio_Echo", "Relatorio_Foxtrot"]
PERGUNTAS = ["aumento de carga tributária", "regulamentação", "metas irrealistas"]


class _DB(_FakeDB):
    def __init__(self, pedidos=()):
        super().__init__()
        self.pedidos = list(pedidos)

    async def fetch_session_messages(self, session_id, limit=50, roles=None, **kw):
        return [SimpleNamespace(content=p) for p in self.pedidos][-limit:]

    async def list_agent_jobs(self, user_id=None, *, statuses=None, limit=20):
        out = await super().list_agent_jobs(user_id, statuses=statuses, limit=1000)
        return sorted(out, key=lambda j: j.created_at or datetime.min, reverse=True)[:limit]


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "files").mkdir()
    for nome in PLANOS:
        (tmp_path / "files" / f"{nome}.pdf").write_text("x")
    for nome in PLANOS[:5]:
        (tmp_path / "files" / f"{nome}.txt").write_text("velho")
    return tmp_path


def _ex(ws, db, *, evidencia=(LS,), checker=None, sessao=None, usuario=None, store=None):
    return JobToolExecutor(
        db=db, job_manager=_JM(_PathSandbox(ws)), user_id=usuario or uuid4(), session_id=sessao or uuid4(),
        user_message=PEDIDO, disk_evidence=lambda: list(evidencia), criteria_checker=checker, file_store=store,
    )


async def _call(ex, **args):
    return (await ex.execute_tool_call(ToolCall(id="1", name="deep_read", arguments=args))).result


def _args(nomes, **extra):
    return {
        "files": [f"/workspace/files/{n}" for n in nomes], "questions": PERGUNTAS,
        "goal": "comparar os planos", "title": "Planos 2026", **extra,
    }


def test_pasta_citada_e_listagem_sao_reconhecidas():
    assert user_dirs([PEDIDO]) == [PASTA]
    assert user_dirs(["salva o arquivo /Users/daniel/Desktop/x.md"]) == []
    nomes = [n for n, _ in parse_listing([LS], PASTA + "/")]
    assert "Relatorio_Foxtrot.pdf" in nomes and "FONTES.md" in nomes and "." not in nomes


@pytest.mark.asyncio
async def test_pasta_citada_exige_source_dir(ws):
    db = _DB([PEDIDO])
    out = await _call(_ex(ws, db), **_args([f"{n}.txt" for n in PLANOS[:5]]))
    assert out["ok"] is False and "source_dir" in out["error"] and not db.jobs


@pytest.mark.asyncio
async def test_sem_listagem_da_pasta_manda_listar(ws):
    db = _DB([PEDIDO])
    out = await _call(_ex(ws, db, evidencia=()), **_args([f"{n}.pdf" for n in PLANOS], source_dir=PASTA))
    assert out["ok"] is False and "ls -la" in out["error"]


@pytest.mark.asyncio
async def test_documento_da_pasta_fora_da_leitura_e_recusado_com_o_nome(ws):
    """O caso exato do teste: 5 planos lidos, o Foxtrot não."""
    db = _DB([PEDIDO])
    out = await _call(_ex(ws, db), **_args([f"{n}.txt" for n in PLANOS[:5]], source_dir=PASTA))
    assert out["ok"] is False and out["faltando"] == ["Relatorio_Foxtrot.pdf"] and not db.jobs


@pytest.mark.asyncio
async def test_todos_os_documentos_passam_e_indice_pequeno_nao_e_exigido(ws):
    db = _DB([PEDIDO])
    out = await _call(_ex(ws, db), **_args([f"{n}.pdf" for n in PLANOS], source_dir=PASTA))
    assert out["ok"] is True, out  # FONTES.md (3 KB) não é documento a ler


@pytest.mark.asyncio
async def test_excluded_deixa_de_fora_so_o_que_foi_nomeado(ws):
    db = _DB([PEDIDO])
    out = await _call(
        _ex(ws, db), **_args([f"{n}.pdf" for n in PLANOS[:5]], source_dir=PASTA, excluded=["Relatorio_Foxtrot.pdf"])
    )
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_txt_extraido_antes_e_trocado_pelo_pdf_original(ws):
    db = _DB()
    ex = JobToolExecutor(db=db, job_manager=_JM(_PathSandbox(ws)), user_id=uuid4(), session_id=uuid4())
    out = await _call(ex, **_args(["Relatorio_Charlie.txt", "Relatorio_Charlie.pdf", "Relatorio_Echo.txt"]))
    assert out["ok"] is True
    job = next(iter(db.jobs.values()))
    assert job.spec_json["files"] == [
        {"path": "/workspace/files/Relatorio_Charlie.pdf"}, {"path": "/workspace/files/Relatorio_Echo.pdf"},
    ]


@pytest.mark.asyncio
async def test_criterio_do_usuario_sem_pergunta_e_recusado(ws):
    visto = {}

    async def checker(pedidos, goal, perguntas, instrucoes):
        visto["pedidos"] = pedidos
        return ["regulamentação"]

    db = _DB([PEDIDO])
    out = await _call(
        _ex(ws, db, checker=checker),
        **{**_args([f"{n}.pdf" for n in PLANOS], source_dir=PASTA), "questions": ["PROPOSTAS ECONÔMICAS", "SEGURANÇA PÚBLICA"]},
    )
    assert out["ok"] is False and out["criterios_faltando"] == ["regulamentação"] and not db.jobs
    assert visto["pedidos"] == [PEDIDO]


@pytest.mark.asyncio
async def test_verificador_fora_do_ar_nao_bloqueia(ws):
    async def checker(*a):
        raise RuntimeError("402")

    out = await _call(_ex(ws, _DB([PEDIDO]), checker=checker), **_args([f"{n}.pdf" for n in PLANOS], source_dir=PASTA))
    assert out["ok"] is True


# ── extensão ────────────────────────────────────────────────────────────────


class _Store:
    async def get(self, file_id):
        return SimpleNamespace(
            status="available", name="analise-completa-planos-2026.md",
            meta_json={"source": {"workspace_path": "/workspace/analise-completa-planos-2026.md"}},
        )


async def _leitura_base(db, sessao, usuario, *, status="completed"):
    base = await db.create_agent_job(AgentJob(
        user_id=usuario, session_id=sessao, kind="deep_read", title="Leitura: Planos 2026", status=status,
        created_at=datetime(2026, 9, 17, 1, 19),
        spec_json={
            "goal": "comparar os planos", "questions": PERGUNTAS, "source_dir": PASTA,
            "files": [{"path": f"/workspace/files/{n}.pdf"} for n in PLANOS[:5]],
            "out_dir": "/workspace/analises/planos-2026-2c0ec2bd",
        },
    ))
    await db.create_agent_job(AgentJob(
        user_id=usuario, session_id=sessao, kind="scheduled_prompt", title="Continuação", status="completed",
        created_at=datetime(2026, 9, 17, 1, 23),
        spec_json={"continuation_of": str(base.id)},
        extra_metadata={"files": [{"file_id": "a6be6066-17ce-4fdd-805f-fd246ab2728b", "name": "analise-completa-planos-2026.md"}]},
    ))
    return base


@pytest.mark.asyncio
async def test_faltou_o_foxtrot_acrescenta_na_mesma_leitura_e_aponta_a_conclusao(ws):
    sessao, usuario = uuid4(), uuid4()
    db = _DB([PEDIDO, "eu nao vi falar do foxtrot na sua analise"])
    base = await _leitura_base(db, sessao, usuario)

    out = await _call(
        _ex(ws, db, sessao=sessao, usuario=usuario, store=_Store()),
        extend=str(base.id), files=["/workspace/files/Relatorio_Foxtrot.pdf"],
        then="atualize a conclusão com o Foxtrot",
    )

    assert out["ok"] is True, out
    novo = next(j for j in db.jobs.values() if j.kind == "deep_read" and j.id != base.id)
    spec = novo.spec_json
    assert spec["out_dir"] == "/workspace/analises/planos-2026-2c0ec2bd"
    assert spec["extend_of"] == str(base.id)
    assert spec["questions"] == PERGUNTAS
    assert [f["path"].rsplit("/", 1)[-1] for f in spec["files"]] == [f"{n}.pdf" for n in PLANOS]
    assert spec["entregas_anteriores"][0]["path"] == "/workspace/analise-completa-planos-2026.md"
    assert "ATUALIZADA" in out["note"] and "/workspace/analise-completa-planos-2026.md" in out["note"]
    assert novo.title.startswith("Leitura (ampliada)")


@pytest.mark.asyncio
async def test_estender_sem_nada_novo_manda_ler_a_sintese(ws):
    sessao, usuario = uuid4(), uuid4()
    db = _DB([PEDIDO])
    base = await _leitura_base(db, sessao, usuario)
    out = await _call(
        _ex(ws, db, sessao=sessao, usuario=usuario), extend="/workspace/analises/planos-2026-2c0ec2bd/sintese.md",
        files=["/workspace/files/Relatorio_Charlie.pdf"],
    )
    assert out["ok"] is False and "nada novo" in out["error"]


@pytest.mark.asyncio
async def test_nao_estende_leitura_que_ainda_roda(ws):
    sessao, usuario = uuid4(), uuid4()
    db = _DB([PEDIDO])
    base = await _leitura_base(db, sessao, usuario, status="running")
    out = await _call(_ex(ws, db, sessao=sessao, usuario=usuario), extend=str(base.id), files=["Relatorio_Foxtrot.pdf"])
    assert out["ok"] is False and "rodando" in out["error"]


@pytest.mark.asyncio
async def test_continuacao_da_leitura_ampliada_manda_atualizar_a_conclusao_e_cobrir_todos():
    from core.jobs.manager import JobManager

    db = _DB()
    jm = JobManager(db=db, sandbox=None, tick_interval=1)
    job = await db.create_agent_job(AgentJob(
        user_id=uuid4(), session_id=uuid4(), kind="deep_read", title="Leitura (ampliada): Planos 2026",
        status="completed",
        spec_json={
            "questions": PERGUNTAS,
            "files": [{"path": f"/workspace/files/{n}.pdf"} for n in PLANOS],
            "out_dir": "/workspace/analises/planos-2026-2c0ec2bd",
            "entregas_anteriores": [{"path": "/workspace/analise-completa-planos-2026.md", "name": "x", "file_id": "a"}],
        },
    ))
    assert await jm._enqueue_continuation(job, "escreva a conclusão em /workspace/analise-com-foxtrot.md", {"status": "completed", "result_summary": "ok"})
    texto = next(j for j in db.jobs.values() if j.kind == "scheduled_prompt").spec_json["action"]["texto"]
    assert "TODOS os 6 documentos" in texto and "Relatorio_Foxtrot.pdf" in texto
    assert "ATUALIZAÇÃO" in texto and "/workspace/analise-completa-planos-2026.md" in texto
    assert "MESMO caminho" in texto
    # A ordem do cérebro continua lá; o contrato vem depois e prevalece.
    assert texto.index("analise-com-foxtrot.md") < texto.index("Contrato desta leitura")


@pytest.mark.asyncio
async def test_continuacao_de_outro_job_nao_ganha_contrato():
    from core.jobs.manager import JobManager

    db = _DB()
    jm = JobManager(db=db, sandbox=None, tick_interval=1)
    job = await db.create_agent_job(AgentJob(kind="sandbox_task", title="ocr", status="completed", spec_json={"command": "x"}))
    assert await jm._enqueue_continuation(job, "entregue", {"status": "completed", "result_summary": "ok"})
    texto = next(j for j in db.jobs.values() if j.kind == "scheduled_prompt").spec_json["action"]["texto"]
    assert "Contrato desta leitura" not in texto


@pytest.mark.asyncio
async def test_verificador_de_criterios_so_acusa_o_que_ficou_sem_pergunta():
    import json as _json

    from modules.desktop.ai_service import AIService

    class _Prov:
        async def complete(self, **kw):
            self.kw = kw
            return SimpleNamespace(get_content=lambda: _json.dumps({"criterios": [
                {"criterio": "aumento de carga tributaria", "pergunta": 1},
                {"criterio": "regulamentação", "pergunta": None},
                {"criterio": "metas irrealistas", "pergunta": 2},
            ]}))

    prov = _Prov()
    svc = AIService.__new__(AIService)

    async def _utility():
        return prov, "m"

    svc._utility_llm = _utility
    faltando = await svc._deep_read_criteria_gap([PEDIDO], "comparar", ["economia", "metas"], "")
    assert faltando == ["regulamentação"]
    assert prov.kw["disable_thinking"] is True and prov.kw["temperature"] == 0.0
