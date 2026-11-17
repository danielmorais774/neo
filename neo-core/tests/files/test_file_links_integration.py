"""Conciliação de arquivos no AIService e no roteador (docs/file-links-plan.md).

Cada teste é um pedaço da sessão f33632b4, interações 5–10.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.files.ledger import build_views, render_block, resolve_doc_key, versao_provavel
from core.router.intent_router import IntentRouter
from modules.desktop.ai_service import AIService, AIServiceResponse
from shared.types import IntentType, RoutingDecision

CONC = "conclusao-custos-regulacao-metas.md"
A = "414166c4-a33e-4cb8-8f9d-68dbc7bf4862"
L = "6b22eab5-fe16-4a4d-a65f-1131e7c65ef7"
TESTE_DIR = "/Users/daniel/Desktop/teste-de-analise"


class _Ledger:
    """FileLedger em memória: mesma resolução de documento, sem banco."""

    def __init__(self):
        self.rows: list[SimpleNamespace] = []
        self.t = datetime(2026, 9, 16, 20, 0)

    async def events(self, session_id, user_id=None, limit=400):
        return [r for r in self.rows if r.session_id == str(session_id) or (user_id and r.user_id == str(user_id))]

    async def record(self, *, user_id, session_id, action, name="", host_path=None, sandbox_path=None,
                     file_id=None, sha256=None, size_bytes=None, status="ok", note="", versao_de=None):
        eventos = await self.events(session_id, user_id)
        if action == "saved" and not versao_de:
            versao_de = versao_provavel(eventos, sandbox_path=sandbox_path, session_id=session_id)
        chave = resolve_doc_key(eventos, action=action, host_path=host_path, sandbox_path=sandbox_path,
                                file_id=file_id, versao_de=versao_de)
        if action == "delivered" and host_path and name and name.rsplit(".", 1)[-1] != host_path.rsplit(".", 1)[-1]:
            status = "wrong_content"
        self.t += timedelta(minutes=1)
        row = SimpleNamespace(
            session_id=str(session_id), user_id=str(user_id) if user_id else None, doc_key=chave, action=action, name=name or "", host_path=host_path,
            sandbox_path=sandbox_path, file_id=file_id, sha256=sha256, size_bytes=size_bytes, status=status,
            created_at=self.t,
        )
        self.rows.append(row)
        return row

    async def views(self, session_id, user_id=None):
        return build_views(await self.events(session_id, user_id), session_id=session_id)

    async def block(self, session_id, user_id=None, anchor_text=None):
        return render_block(await self.views(session_id, user_id), anchor_text=anchor_text)


class _Store:
    def __init__(self, files):
        self.files = {f.id: f for f in files}
        self.owners: list[tuple] = []

    async def get(self, file_id):
        return self.files.get(str(file_id))

    async def assign_owner(self, file_id, *, user_id, session_id):
        self.owners.append((str(file_id), user_id, session_id))

    async def list_files(self, **kw):
        return []


def _arquivo(fid, name, sha, mime):
    return SimpleNamespace(id=fid, name=name, sha256=sha, mime=mime, size_bytes=1000, status="available", created_at=datetime(2026, 9, 16))


def _svc(ledger=None, store=None):
    svc = object.__new__(AIService)
    svc.db = object()
    svc.file_store = store or _Store([
        _arquivo(A, CONC, "aaa", "text/markdown"),
        _arquivo(L, "Relatorio_Charlie.pdf", "lll", "application/pdf"),
    ])
    svc._file_ledger_obj = ledger or _Ledger()
    svc._session_seen_files = {}
    return svc


SESSAO = uuid.uuid4()
USUARIO = uuid.uuid4()


async def _passos_ate_o_pdf_puxado(svc):
    await svc._record_file_link(
        {"tool": "file_save", "ok": True, "name": CONC, "file_id": A, "size_bytes": 8138, "path": f"/workspace/{CONC}"},
        user_id=USUARIO, session_id=SESSAO,
    )
    await svc._record_file_link(
        {"tool": "save_generated_file", "ok": True, "path": f"/Users/daniel/Desktop/{CONC}", "file_id": A},
        user_id=USUARIO, session_id=SESSAO,
    )
    await svc._record_file_link(
        {"tool": "request_user_file", "ok": True, "name": "Relatorio_Charlie.pdf", "file_id": L, "sha256": "lll",
         "host_path": "/Users/daniel/Documents/analises/relatorios-anuais-2026/Relatorio_Charlie.pdf",
         "path": "/workspace/files/Relatorio_Charlie.pdf"},
        user_id=USUARIO, session_id=SESSAO,
    )


@pytest.mark.asyncio
async def test_passos_do_turno_viram_movimentos_do_registro_e_file_save_ganha_dono():
    svc = _svc()
    await _passos_ate_o_pdf_puxado(svc)
    rows = svc._file_ledger_obj.rows
    assert [r.action for r in rows] == ["saved", "delivered", "pulled", "staged"]
    assert svc.file_store.owners == [(A, USUARIO, SESSAO)]
    assert rows[1].sha256 == "aaa" and rows[1].doc_key == rows[0].doc_key
    assert rows[3].doc_key == rows[2].doc_key != rows[0].doc_key


@pytest.mark.asyncio
async def test_script_escrito_no_sandbox_nao_vira_documento_mas_editar_a_copia_vira():
    svc = _svc()
    await _passos_ate_o_pdf_puxado(svc)
    await svc._record_file_link({"tool": "neo_write_file", "ok": True, "path": "/workspace/analisar.py"},
                                user_id=USUARIO, session_id=SESSAO)
    await svc._record_file_link({"tool": "neo_write_file", "ok": True, "path": f"/workspace/{CONC}"},
                                user_id=USUARIO, session_id=SESSAO)
    assert [r.action for r in svc._file_ledger_obj.rows][-1] == "edited_sandbox"
    assert len(svc._file_ledger_obj.rows) == 5


@pytest.mark.asyncio
async def test_rm_so_registra_caminho_conhecido():
    svc = _svc()
    await _passos_ate_o_pdf_puxado(svc)
    await svc._record_file_link(
        {"tool": "run_command", "ok": True, "command": f'rm -f "/Users/daniel/Desktop/{CONC}" /tmp/lixo.txt'},
        user_id=USUARIO, session_id=SESSAO,
    )
    apagados = [r for r in svc._file_ledger_obj.rows if r.action == "deleted_host"]
    assert [r.host_path for r in apagados] == [f"/Users/daniel/Desktop/{CONC}"]


@pytest.mark.asyncio
async def test_pdf_com_nome_de_md_e_trocado_pelo_documento_certo():
    """Interação 5: file_id do Relatorio_Charlie.pdf + file_name da conclusão."""
    svc = _svc()
    await _passos_ate_o_pdf_puxado(svc)
    certo, recusa = await svc._delivery_identity(await svc.file_store.get(L), CONC, SESSAO)
    assert recusa is None and certo.id == A


@pytest.mark.asyncio
async def test_sem_documento_com_esse_nome_a_entrega_e_recusada_com_o_motivo():
    svc = _svc()
    certo, recusa = await svc._delivery_identity(await svc.file_store.get(L), "relatorio.md", SESSAO)
    assert certo is None and "Relatorio_Charlie.pdf" in recusa and "relatorio.md" in recusa


@pytest.mark.asyncio
async def test_classificador_entrega_o_arquivo_certo_na_pasta_pedida():
    svc = _svc()
    await _passos_ate_o_pdf_puxado(svc)
    svc.settings = SimpleNamespace(neo_file_actuation_enabled=False)
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL, tool_name="save_generated_file", needs_plan=False,
        tool_arguments={"file_id": L, "file_name": CONC, "destination": TESTE_DIR},
    )
    respostas = [r async for r in svc._handle_client_tool(
        session_id=SESSAO, user_message="coloque nesse diretorio /Users/daniel/Desktop/teste-de-analise", decision=decision,
    )]
    import json

    pedido = next(r.tool_request for r in respostas if r.tool_request)
    argumentos = json.loads(pedido["arguments"])
    assert argumentos["file_id"] == A and argumentos["file_name"] == CONC


@pytest.mark.asyncio
async def test_classificador_sem_como_trocar_desvia_para_o_cerebro_com_o_registro():
    svc = _svc()
    await _passos_ate_o_pdf_puxado(svc)
    capturado = {}

    async def _cerebro(session_id, user_message, history, decision, trace, sys_context, **kw):
        capturado["decision"], capturado["ctx"] = decision, sys_context
        yield AIServiceResponse(intent=IntentType.DIRECT_RESPONSE, session_id=session_id, message="ok", is_final=True)

    svc._handle_direct_response = _cerebro
    svc._build_system_context = lambda md: "base"
    decision = RoutingDecision(
        intent=IntentType.CLIENT_TOOL, tool_name="save_generated_file", needs_plan=False,
        tool_arguments={"file_id": L, "file_name": "outro-nome.md", "destination": TESTE_DIR},
    )
    respostas = [r async for r in svc._handle_client_tool(session_id=SESSAO, user_message="coloca lá", decision=decision)]
    assert all(r.tool_request is None for r in respostas)
    assert "entrega descartada" in capturado["decision"].explanation
    assert "ARQUIVOS DESTA CONVERSA" in capturado["ctx"]


@pytest.mark.asyncio
async def test_sobrescrever_copia_propria_vale_mesmo_depois_de_reload():
    svc = _svc()
    await _passos_ate_o_pdf_puxado(svc)
    svc._session_seen_files = {}  # o reload do core zerou a RAM
    assert await svc._may_overwrite(SESSAO, f"/Users/daniel/Desktop/{CONC}", {})
    assert not await svc._may_overwrite(SESSAO, "/Users/daniel/Desktop/outro.md", {})


@pytest.mark.asyncio
async def test_bloco_do_registro_chega_ao_contexto():
    svc = _svc()
    await _passos_ate_o_pdf_puxado(svc)
    bloco = await svc._file_links_block(SESSAO)
    assert f"catálogo file_id={A}" in bloco and "Relatorio_Charlie.pdf" in bloco


# ── roteador ────────────────────────────────────────────────────────────────


def _rota(tool, args=None, raw=None, speech="imperative"):
    return RoutingDecision(intent=IntentType.CLIENT_TOOL, tool_name=tool, tool_arguments=args or {},
                           raw_command=raw, speech_act=speech, needs_plan=False)


def test_adiciona_e_entrega_vai_para_o_cerebro():
    d = IntentRouter._reject_classifier_file_authoring(
        _rota("save_generated_file", {"file_id": L, "file_name": CONC, "destination": TESTE_DIR}),
        "adiciona ao arquivo final e coloque nesse diretorio /Users/daniel/Desktop/teste-de-analise",
    )
    assert d.intent is IntentType.DIRECT_RESPONSE


def test_entrega_simples_continua_com_o_classificador():
    d = IntentRouter._reject_classifier_file_authoring(
        _rota("save_generated_file", {"file_id": A, "file_name": CONC, "destination": "desktop"}),
        "salva na minha área de trabalho",
    )
    assert d.intent is IntentType.CLIENT_TOOL


def test_heredoc_que_inventa_markdown_no_mac_vai_para_o_cerebro():
    comando = (
        "python3 << 'EOF'\ntexto = '''# Conclusão: Aumento Tributário'''\n"
        "open('/Users/daniel/Desktop/teste-de-analise/x.md','w').write(texto)\nEOF"
    )
    d = IntentRouter._reject_classifier_file_authoring(_rota("run_command", raw=comando), "baixe ele para mim e coloque lá")
    assert d.intent is IntentType.DIRECT_RESPONSE


def test_texto_ditado_pelo_usuario_ainda_pode_ir_por_shell():
    d = IntentRouter._reject_classifier_file_authoring(
        _rota("run_command", raw='echo "comprar pão" >> ~/lista.txt'), 'escreve "comprar pão" no fim da minha lista.txt'
    )
    assert d.intent is IntentType.CLIENT_TOOL


def test_botao_de_download_vai_para_o_cerebro():
    d = IntentRouter._reject_classifier_file_authoring(_rota("run_command", raw="ls ~/Documents"), "me mostra o botao de download")
    assert d.intent is IntentType.DIRECT_RESPONSE


def test_comando_de_leitura_nao_e_afetado():
    d = IntentRouter._reject_classifier_file_authoring(
        _rota("run_command", raw='ls -la "/Users/daniel/Documents/x" 2>&1 | head -60'), "lista a pasta"
    )
    assert d.intent is IntentType.CLIENT_TOOL


# ── file_offer ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_offer_devolve_o_arquivo_existente_sem_publicar_de_novo():
    from core.files.executor import FilesExecutor

    class _S:
        async def get(self, file_id):
            return _arquivo(A, CONC, "aaa", "text/markdown") if file_id == A else None

    ex = FilesExecutor(_S())
    ok = await ex.execute("file_offer", {"file_id": A})
    assert ok["offered"] is True and ok["file_id"] == A and ok["name"] == CONC
    assert (await ex.execute("file_offer", {"file_id": L}))["error"] == "not_found"
