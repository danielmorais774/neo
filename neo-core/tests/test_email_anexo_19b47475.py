"""Email com foto → PDF: os três defeitos da sessão 19b47475 (14/11/2026).

O Daniel mandou "Foto para PDF" (só a imagem, sem texto) para a caixa do Neo às
23:23 BRT e pediu o PDF. Em 8 turnos o Neo não entregou nada e culpou o envio:

1. **Busca cega.** O ``SINCE`` do IMAP ia com a data UTC (15-Nov) e o Gmail
   datou a mensagem em 14/11: duas buscas de 30 min voltaram vazias com o email
   lá; as de 24 h e 7 dias acharam.
2. **Anexo invisível.** ``_body_text`` descarta anexo de propósito e nada mais o
   mostrava: o Neo disse "anexo grande demais, manda pelo botão normal".
3. **Achado que some.** O agente do SERVER_TOOL achou o email no meio do laço,
   mas só o texto final ia para o histórico. Nos turnos 7 e 8 o Neo afirmou que
   a mensagem "não trouxe assunto nem remetente".
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from core.authority import risk
from core.mailbox import client
from core.mailbox.tools import EMAIL_TOOL_NAMES, MailboxToolExecutor
from core.session.session_manager import _render_tool_trace_note
from modules.desktop.ai_service import AIService, _server_tool_trace, _summarize_tool_call
from shared import IntentType, ToolCall
from shared.config import get_settings
from shared.types import AgentStep, RoutingDecision, ToolResult

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200 + b"\xff\xd9"


def _foto_para_pdf(*, inline: bool = False) -> bytes:
    """A mensagem real, no formato do Gmail: corpo vazio + a foto."""
    msg = EmailMessage()
    msg["From"] = "Daniel <daniel@example.com>"
    msg["To"] = "neo@example.com"
    msg["Subject"] = "Foto para PDF"
    msg["Date"] = "Sat, 14 Nov 2026 23:23:38 -0300"
    msg.set_content("")
    if inline:
        # Foto arrastada para o corpo: multipart/related, sem nome, só Content-ID.
        msg.add_alternative("<div><img src='cid:foto1'></div>", subtype="html")
        html = msg.get_payload()[1]
        html.add_related(JPEG, maintype="image", subtype="jpeg", cid="<foto1>")
    else:
        msg.add_attachment(JPEG, maintype="image", subtype="jpeg", filename="IMG_1234.jpg")
    return msg.as_bytes()


class _Conn:
    """IMAP falso: guarda o SEARCH e devolve a mensagem no FETCH."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.search: tuple = ()

    def select(self, folder, readonly=True):
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.search = args
            return "OK", [b"138"]
        return "OK", [(b"138 (FLAGS () BODY[] {1})", self.raw)]

    def logout(self):
        return None


# ── 1. a busca pede um dia antes ────────────────────────────────────────────
def test_since_pede_um_dia_antes_do_dia_utc():
    conn = _Conn(_foto_para_pdf())
    # Início da janela às 23:24 BRT de 14/11 = 02:24 UTC de 15/11: o código
    # antigo pedia SINCE 15-Nov e o Gmail, que datou a mensagem em 14/11, não a dava.
    since = datetime(2026, 11, 15, 2, 24, tzinfo=timezone.utc)
    client._search_recent(conn, since, unread_only=False)
    assert conn.search[-1] == 'SINCE "14-Nov-2026"'


def test_janela_curta_acha_o_email_das_23h23():
    """O corte fino pelo header continua: a mensagem de 02:23 UTC entra numa
    janela que começa 01:54 UTC, e a hora sai no fuso do Neo, não do container."""
    conn = _Conn(_foto_para_pdf())
    since = datetime(2026, 11, 15, 1, 54, tzinfo=timezone.utc)
    [resumo] = client._fetch_summaries(conn, [b"138"], since=since, limit=5)
    d = resumo.to_dict()
    assert d["assunto"] == "Foto para PDF"
    assert d["quando"] == "14/11 23:23"
    assert d["anexos"] == ["IMG_1234.jpg"]
    assert d["previa"] == ""


def test_corte_pelo_header_ainda_descarta_o_que_e_velho():
    conn = _Conn(_foto_para_pdf())
    since = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)
    assert client._fetch_summaries(conn, [b"138"], since=since, limit=5) == []


# ── 2. anexo aparece e pode ser pego ────────────────────────────────────────
def test_anexo_e_listado_com_indice_nome_tipo_e_tamanho():
    import email as _email

    msg = _email.message_from_bytes(_foto_para_pdf())
    [a] = client._attachments_summary(msg)
    assert a == {"indice": 1, "nome": "IMG_1234.jpg", "tipo": "image/jpeg", "tamanho_bytes": len(JPEG)}


def test_foto_colada_no_corpo_tambem_e_anexo():
    """Gmail manda a foto arrastada como inline num multipart/related, sem nome."""
    import email as _email

    msg = _email.message_from_bytes(_foto_para_pdf(inline=True))
    [a] = client._attachments_summary(msg)
    assert a["tipo"] == "image/jpeg"
    assert a["nome"].startswith("anexo-1.") and a["nome"].endswith(("jpg", "jpeg"))


def test_corpo_de_texto_nao_vira_anexo_mas_txt_anexado_vira():
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg.set_content("olá, segue a lista")
    msg.add_attachment("a;b\n1;2\n", subtype="csv", filename="lista.csv")
    nomes = [a["nome"] for a in client._attachments_summary(msg)]
    assert nomes == ["lista.csv"]


def test_nome_de_anexo_nunca_vira_caminho():
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg.set_content("")
    msg.add_attachment(JPEG, maintype="image", subtype="jpeg", filename="../../etc/passwd.jpg")
    [a] = client._attachments_summary(msg)
    assert a["nome"] == "passwd.jpg"


def test_attachment_sync_devolve_os_bytes_e_recusa_indice_que_nao_existe(monkeypatch):
    monkeypatch.setattr(client, "_connect", lambda **_kw: _Conn(_foto_para_pdf()))
    kw = dict(host="h", port=993, address="neo@x", password="p", uid="138", folder="INBOX")
    got = client._attachment_sync(index=1, **kw)
    assert got["dados"] == JPEG and got["nome"] == "IMG_1234.jpg"
    fora = client._attachment_sync(index=3, **kw)
    assert "dados" not in fora and len(fora["anexos"]) == 1


def _info(fields):
    return SimpleNamespace(id="c1", label="email-do-neo", kind="email", fields=fields)


class _Cofre:
    async def list_by_kind(self, kind):
        return [_info({})]

    async def resolve(self, label):
        return _info({"address": "neo@x.com", "imap_host": "imap.x.com"}), "app-pass"

    async def touch(self, _id):
        return None


class _Catalogo:
    def __init__(self) -> None:
        self.kw: dict[str, Any] = {}
        self.data = b""

    async def ingest_bytes(self, data, **kw):
        self.data, self.kw = data, kw
        return SimpleNamespace(id=uuid4(), name=kw["name"], size_bytes=len(data), mime=kw.get("mime"))


def _anexo_falso(n_anexos: int = 1):
    async def _fake(**kw):
        anexos = [
            {"indice": i, "nome": f"f{i}.jpg", "tipo": "image/jpeg", "tamanho_bytes": 9}
            for i in range(1, n_anexos + 1)
        ]
        base = {"de": "Daniel", "assunto": "Foto para PDF", "anexos": anexos}
        if 1 <= kw["index"] <= n_anexos:
            return {**base, "nome": f"f{kw['index']}.jpg", "tipo": "image/jpeg", "dados": JPEG}
        return base

    return _fake


@pytest.mark.asyncio
async def test_email_anexo_guarda_no_catalogo_e_devolve_o_file_id(monkeypatch):
    monkeypatch.setattr("core.mailbox.tools.get_attachment", _anexo_falso())
    cat = _Catalogo()
    res = await MailboxToolExecutor(_Cofre(), get_file_store=lambda: cat).execute_tool_call(
        ToolCall(id="1", name="email_anexo", arguments={"id": "138"})
    )
    assert res.result["ok"] is True and res.result["file_id"]
    assert cat.data == JPEG
    assert cat.kw["origin"] == "email" and cat.kw["retention"] == "temp"
    assert cat.kw["source"]["assunto"] == "Foto para PDF"
    assert "file_stage" in res.result["note"]
    assert "app-pass" not in str(res.result)


@pytest.mark.asyncio
async def test_email_anexo_com_varios_pede_qual(monkeypatch):
    monkeypatch.setattr("core.mailbox.tools.get_attachment", _anexo_falso(3))
    res = await MailboxToolExecutor(_Cofre(), get_file_store=lambda: _Catalogo()).execute_tool_call(
        ToolCall(id="1", name="email_anexo", arguments={"id": "138"})
    )
    assert res.result["ok"] is False and len(res.result["anexos"]) == 3
    ok = await MailboxToolExecutor(_Cofre(), get_file_store=lambda: _Catalogo()).execute_tool_call(
        ToolCall(id="1", name="email_anexo", arguments={"id": "138", "anexo": 2})
    )
    assert ok.result["ok"] is True and ok.result["name"] == "f2.jpg"


@pytest.mark.asyncio
async def test_email_anexo_sem_anexo_ou_sem_catalogo_explica(monkeypatch):
    monkeypatch.setattr("core.mailbox.tools.get_attachment", _anexo_falso(0))
    sem_anexo = await MailboxToolExecutor(_Cofre(), get_file_store=lambda: _Catalogo()).execute_tool_call(
        ToolCall(id="1", name="email_anexo", arguments={"id": "138"})
    )
    assert sem_anexo.result["ok"] is False and "não tem anexo" in sem_anexo.result["error"]
    sem_catalogo = await MailboxToolExecutor(_Cofre()).execute_tool_call(
        ToolCall(id="1", name="email_anexo", arguments={"id": "138"})
    )
    assert sem_catalogo.result["ok"] is False and "NEO_FILES_ENABLED" in sem_catalogo.result["error"]


def test_email_anexo_e_leitura_de_fora_nao_atuacao():
    assert "email_anexo" in EMAIL_TOOL_NAMES
    assert risk.known("email_anexo")
    assert risk.is_external_source("email_anexo") and not risk.is_actuation("email_anexo")


# ── 3. o achado vai para o próximo turno ────────────────────────────────────
_INBOX = {
    "ok": True, "caixa": "neo@x.com", "credencial": "email-do-neo", "janela_min": 1440, "count": 2,
    "mensagens": [
        {"id": "138", "de": "Daniel Morais <daniel@example.com>", "assunto": "Foto para PDF",
         "quando": "14/11 23:23", "previa": "", "nao_lida": True, "anexos": ["IMG_1234.jpg"]},
        {"id": "137", "de": "Conecta", "assunto": "Uma pessoa que você talvez conheça",
         "quando": "14/11 22:00", "previa": "...", "nao_lida": True},
    ],
}


def _tr(result, error=None, cid="a"):
    return ToolResult(tool_call_id=cid, name="x", result=result, error=error, execution_time=0.1)


def test_resumo_do_inbox_guarda_assunto_remetente_id_e_anexos():
    e = _summarize_tool_call("email_inbox", {"minutes": 1440}, _tr(_INBOX))
    m = e["mensagens"][0]
    assert (m["id"], m["assunto"], m["anexos"]) == ("138", "Foto para PDF", ["IMG_1234.jpg"])
    assert "Daniel Morais" in m["de"]


def test_nota_do_proximo_turno_mostra_o_email():
    e = _summarize_tool_call("email_inbox", {}, _tr(_INBOX))
    nota = _render_tool_trace_note({"tool_trace": [e]})
    assert "Foto para PDF" in nota and "id 138" in nota and "IMG_1234.jpg" in nota
    assert "Daniel Morais" in nota


def test_trace_do_server_tool_junta_tools_e_arquivos_publicados():
    passos = [
        AgentStep(
            step_number=1,
            tool_calls=[ToolCall(id="a", name="email_inbox", arguments={"minutes": 1440})],
            tool_results=[_tr(_INBOX, cid="a")],
        ),
        AgentStep(
            step_number=2,
            tool_calls=[ToolCall(id="b", name="file_save", arguments={"path": "/workspace/foto.pdf"})],
            tool_results=[_tr({"file_id": "f-1", "name": "foto.pdf", "size_bytes": 900}, cid="b")],
        ),
    ]
    trace, files = _server_tool_trace(passos)
    assert [e["tool"] for e in trace] == ["email_inbox", "file_save"]
    assert files == [{"file_id": "f-1", "name": "foto.pdf", "size_bytes": 900, "path": "/workspace/foto.pdf"}]


def test_trace_ignora_resultado_ausente_e_aceita_dict():
    passos = [{"tool_calls": [{"id": "z", "name": "email_inbox", "arguments": {}}], "tool_results": []}]
    assert _server_tool_trace(passos) == ([], [])


class _Agent:
    async def execute(self, **_kw):
        return {
            "status": "completed", "final_response": "Job enfileirado.", "iterations": 2,
            "metadata": {},
            "steps": [
                AgentStep(
                    step_number=1,
                    tool_calls=[ToolCall(id="a", name="email_inbox", arguments={})],
                    tool_results=[_tr(_INBOX, cid="a")],
                ),
                AgentStep(
                    step_number=2,
                    tool_calls=[ToolCall(id="b", name="file_save", arguments={"path": "/workspace/foto.pdf"})],
                    tool_results=[_tr({"file_id": "f-1", "name": "foto.pdf", "size_bytes": 900}, cid="b")],
                ),
            ],
        }


class _Sessions:
    def __init__(self) -> None:
        self.saved: dict[str, Any] = {}

    async def add_message(self, **kw):
        self.saved = kw


class _LLM:
    def list_available_providers(self):
        return ["openrouter"]


@pytest.mark.asyncio
async def test_server_tool_salva_a_nota_e_manda_o_botao_de_download():
    svc = AIService.__new__(AIService)
    svc.settings = get_settings()
    svc.db = None
    svc.llm = _LLM()
    svc.pks = None
    svc.agent = _Agent()
    svc.sessions = _Sessions()
    svc._voice_input_sessions = set()
    svc.build_request_executor = lambda **_kw: object()
    svc._server_tool_toolset = lambda escolhida=None: []
    svc._build_server_tool_memory = lambda *_a, **_kw: None
    decision = RoutingDecision(intent=IntentType.SERVER_TOOL, tool_name="email_inbox", needs_memory=False)

    finais = [
        r async for r in svc._handle_server_tool(uuid4(), "qual o ultimo email?", [], decision, metadata={})
        if r.is_final
    ]

    meta = svc.sessions.saved["metadata"]
    assert [e["tool"] for e in meta["tool_trace"]] == ["email_inbox", "file_save"]
    assert meta["files"][0]["file_id"] == "f-1"
    nota = _render_tool_trace_note(meta)
    assert "Foto para PDF" in nota
    assert finais[-1].metadata == {"files": meta["files"]}
