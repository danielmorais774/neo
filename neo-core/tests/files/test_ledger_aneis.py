"""Registro em dois anéis — a sequência real da session-20cb02b6 (09/11/2026).

Ele anexou o template MAPA de Processos de Software e pediu "coloca o meu R.A e o
meu nome no cabeçalho desse arquivo". O registro, por usuário e 72 h, mostrava sob
"ARQUIVOS DESTA CONVERSA" um TXT de questões de semiótica de OUTRA conversa
(08/11), com a regra "na dúvida, o gerado mais recente". O Neo batizou o anexo de
`questoes.docx`, no turno 2 reabriu o original em branco (jogando fora o turno 1),
e no turno 3 propôs encher o template com aquelas questões.

O que tem de valer agora:
* o anexo é documento desta conversa, de origem "veio dele", e a versão preenchida
  é a versão 2 DELE;
* o TXT de outra conversa não aparece sem ser citado — e citado, aparece com a
  origem dita e é adotado;
* ler o TXT sem ele ter citado devolve um aviso de origem;
* a memória continua: "usa as questões do Portal" encontra o arquivo.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.files.ledger import (
    FileLedger,
    ancorado,
    build_views,
    render_block,
    resolve_doc_key,
    texto_de_ancora,
    versao_provavel,
)

ATUAL = str(uuid.uuid4())
VELHA = str(uuid.uuid4())
USUARIO = str(uuid.uuid4())

ANEXO = "e9745955-ef26-496a-b919-e044f7f26b17"
HASH = "48fc71b7abad642a07276b53f90a95f095ef9a321f5b4cf9f1d8a5f5081fd184.docx"
V1 = "a8830bd8-c6fd-4295-8964-47d5f5db9f21"
V2 = "b2292836-4586-4b0d-8904-0192c379a4b8"
TXT_ID = "54f35c3c-3ebc-4dd6-96ec-f7c314616d87"
TXT_NOME = "questoes-questionario-380129.txt"
TXT_SANDBOX = "/workspace/files/extrair-questoes-do-portal-para-txt-f7794968.txt"
ORIGINAL = "/workspace/files/questoes.docx"
PREENCHIDO = "/workspace/files/questoes_preenchido.docx"

PEDIDO_T1 = (
    "coloca o meu R.A e o meu nome no cabeçalho desse arquivo e me gera outro arquivo "
    "e retorna ele, o meu r.a é 12345678-9"
)


class _Memoria(FileLedger):
    """O `FileLedger` de verdade, com a tabela numa lista."""

    def __init__(self) -> None:
        super().__init__(db=None)
        self.rows: list[SimpleNamespace] = []
        self.t = datetime(2026, 11, 8, 16, 0)

    async def events(self, session_id, *, user_id=None, horizon_hours=72, limit=400):
        return [r for r in self.rows if r.session_id == str(session_id) or (user_id and r.user_id == str(user_id))]

    async def record(self, *, user_id, session_id, action, name="", host_path=None, sandbox_path=None,
                     file_id=None, sha256=None, size_bytes=None, status="ok", note="", versao_de=None):
        eventos = await self.events(session_id, user_id=user_id)
        if action == "saved" and not versao_de:
            versao_de = versao_provavel(eventos, sandbox_path=sandbox_path, session_id=session_id)
        chave = resolve_doc_key(eventos, action=action, host_path=host_path, sandbox_path=sandbox_path,
                                file_id=file_id, versao_de=versao_de)
        self.t += timedelta(minutes=1)
        row = SimpleNamespace(
            session_id=str(session_id), user_id=str(user_id) if user_id else None, doc_key=chave,
            action=action, name=name or (sandbox_path or "").rsplit("/", 1)[-1], host_path=host_path,
            sandbox_path=sandbox_path, file_id=file_id, sha256=sha256, size_bytes=size_bytes,
            status=status, note=note, created_at=self.t,
        )
        self.rows.append(row)
        return row


async def _historico_de_outra_conversa(g: _Memoria) -> None:
    await g.record(user_id=USUARIO, session_id=VELHA, action="saved", name=TXT_NOME,
                   sandbox_path=TXT_SANDBOX, file_id=TXT_ID, size_bytes=17000)
    await g.record(user_id=USUARIO, session_id=VELHA, action="saved", name="revolucao-industrial.pdf",
                   sandbox_path="/workspace/revolucao-industrial/revolucao-industrial.pdf",
                   file_id=str(uuid.uuid4()))


async def _turno_1(g: _Memoria) -> None:
    g.t = datetime(2026, 11, 10, 0, 22)
    await g.record(user_id=USUARIO, session_id=ATUAL, action="attached", name=HASH, file_id=ANEXO,
                   size_bytes=39019)
    await g.record(user_id=USUARIO, session_id=ATUAL, action="staged", name=HASH, file_id=ANEXO,
                   sandbox_path=ORIGINAL)
    # Publicado SEM versao_de, como o modelo fez — o nome liga ao original.
    await g.record(user_id=USUARIO, session_id=ATUAL, action="saved", name="questoes_preenchido.docx",
                   sandbox_path=PREENCHIDO, file_id=V1, size_bytes=34648)


# ── Anel 1: o anexo e as versões dele ────────────────────────────────────────

@pytest.mark.asyncio
async def test_anexo_e_documento_desta_conversa_e_o_txt_de_outra_nao_aparece():
    g = _Memoria()
    await _historico_de_outra_conversa(g)
    await g.record(user_id=USUARIO, session_id=ATUAL, action="attached", name=HASH, file_id=ANEXO)

    bloco = await g.block(ATUAL, USUARIO, anchor_text=PEDIDO_T1)

    assert "## ARQUIVOS DESTA CONVERSA" in bloco
    assert "veio DELE (anexou no chat)" in bloco
    assert f"catálogo file_id={ANEXO}" in bloco
    assert "questoes-questionario" not in bloco and "revolucao" not in bloco
    assert "o gerado mais recente" not in bloco


@pytest.mark.asyncio
async def test_versao_preenchida_e_a_versao_2_do_anexo_com_o_original_a_vista():
    g = _Memoria()
    await _turno_1(g)
    views = await g.views(ATUAL, USUARIO)

    assert len(views) == 1
    doc = views[0]
    assert doc.origin == "anexo" and doc.name == "questoes_preenchido.docx"
    assert doc.current_file_id == V1 and doc.sandbox_path == PREENCHIDO
    assert doc.original_file_id == ANEXO and doc.original_sandbox_path == ORIGINAL
    bloco = render_block(views)
    assert f"versão atual: sandbox {PREENCHIDO} → catálogo file_id={V1}" in bloco
    assert f"original (como ele mandou): catálogo file_id={ANEXO}, cópia no sandbox {ORIGINAL}" in bloco
    assert "versao_de=" in bloco  # a regra de publicar a versão nova ligada


@pytest.mark.asyncio
async def test_versao_de_explicito_liga_mesmo_com_nome_diferente():
    g = _Memoria()
    await _turno_1(g)
    await g.record(user_id=USUARIO, session_id=ATUAL, action="saved", name="MAPA_Carlos_Daniel.docx",
                   sandbox_path="/workspace/files/MAPA_Carlos_Daniel.docx", file_id=V2,
                   versao_de=PREENCHIDO)
    [doc] = await g.views(ATUAL, USUARIO)
    assert doc.current_file_id == V2 and doc.name == "MAPA_Carlos_Daniel.docx"


def test_palpite_pelo_nome_so_decide_com_um_candidato():
    base = datetime(2026, 11, 10)
    ev = [
        {"doc_key": "a", "action": "staged", "sandbox_path": "/workspace/files/questoes.docx",
         "session_id": ATUAL, "created_at": base},
        {"doc_key": "b", "action": "saved", "sandbox_path": "/workspace/files/questoes.docx.bak.docx",
         "session_id": ATUAL, "created_at": base},
    ]
    assert versao_provavel(ev[:1], sandbox_path=PREENCHIDO, session_id=ATUAL) == "/workspace/files/questoes.docx"
    # outra extensão não é versão
    assert versao_provavel(ev[:1], sandbox_path="/workspace/files/questoes_preenchido.pdf", session_id=ATUAL) is None
    # de outra conversa não conta
    assert versao_provavel(ev[:1], sandbox_path=PREENCHIDO, session_id=VELHA) is None
    # dois candidatos é chute
    dois = ev + [{"doc_key": "c", "action": "staged", "sandbox_path": "/workspace/files/questoes_v.docx",
                  "session_id": ATUAL, "created_at": base}]
    assert versao_provavel(dois, sandbox_path="/workspace/files/questoes_v_final.docx", session_id=ATUAL) is None


# ── Anel 2: outras conversas ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_citado_pelo_assunto_aparece_com_a_origem_e_e_adotado():
    """A memória continua: ele pede as questões do Portal e o Neo acha o arquivo."""
    g = _Memoria()
    await _historico_de_outra_conversa(g)
    await _turno_1(g)

    bloco = await g.block(ATUAL, USUARIO, anchor_text="usa as questões do Portal que você extraiu")

    assert "## ARQUIVOS DE OUTRAS CONVERSAS (recentes) — NÃO são desta conversa" in bloco
    assert f"«{TXT_NOME}» — gerado por você no sandbox · de OUTRA conversa" in bloco
    assert "revolucao" not in bloco
    assert [r.action for r in g.rows if r.session_id == ATUAL][-1] == "adopted"
    # Adotado: no turno seguinte é desta conversa, sem precisar ser citado de novo.
    seguinte = await g.block(ATUAL, USUARIO, anchor_text="formata o texto")
    assert "NÃO são desta conversa" not in seguinte
    assert TXT_NOME in seguinte.split("Como usar este registro")[0]


@pytest.mark.asyncio
async def test_conversa_sem_arquivo_ainda_mostra_os_recentes_como_pergunte_qual():
    """A rotação de sessão que motivou as 72 h (f33632b4) continua coberta."""
    g = _Memoria()
    await _historico_de_outra_conversa(g)

    bloco = await g.block(ATUAL, USUARIO, anchor_text="tenta salvar o arquivo lá agora")

    assert "esta ainda não tem nenhum" in bloco
    assert TXT_NOME in bloco and "revolucao-industrial.pdf" in bloco
    assert "confirme com ele qual destes é" in bloco
    assert not any(r.action == "adopted" for r in g.rows)


def test_id_e_palavra_generica_nao_ancoram():
    txt = SimpleNamespace(name=TXT_NOME, aliases={TXT_SANDBOX})
    anexo = SimpleNamespace(name=HASH, aliases=set())
    assert not ancorado(txt, PEDIDO_T1)
    assert not ancorado(txt, "acho que deu um erro pq veio nao preenchido")
    assert not ancorado(anexo, "o arquivo 48fc71b7abad642a07276b53f90a95f095ef9a321f5b4cf9f1d8a5f5081fd184")
    assert ancorado(txt, "aquele questionário")
    assert ancorado(txt, "pega a questão 3 do portal")


def test_faz_isso_cita_o_que_o_neo_acabou_de_perguntar():
    historico = [
        {"role": "user", "content": "acho que deu um erro pq veio nao preenchido"},
        {"role": "assistant", "content": "Quer que eu use as 10 questões do Portal de 08/11 no template?"},
        {"role": "user", "content": "faz isso"},
    ]
    texto = texto_de_ancora("faz isso", historico)
    assert "Portal" in texto
    assert ancorado(SimpleNamespace(name=TXT_NOME, aliases={TXT_SANDBOX}), texto)
    # Mensagem com conteúdo próprio não puxa a fala anterior.
    assert texto_de_ancora("formata o texto das questões", historico) == "formata o texto das questões"


# ── Aviso na hora do uso ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ler_o_txt_de_outra_conversa_traz_aviso_de_origem():
    g = _Memoria()
    await _historico_de_outra_conversa(g)
    await _turno_1(g)

    # O comando real do turno 3: caminho RELATIVO, depois de um `cd`.
    cmd = ("cd /workspace/files && python3 -c \"import docx\" ; echo '--- TXT ---' ; "
           "head -c 2500 extrair-questoes-do-portal-para-txt-f7794968.txt")
    nota = await g.nota_de_origem(ATUAL, USUARIO, textos=[cmd])
    assert nota and nota.startswith("⚠ ORIGEM:") and TXT_NOME in nota and "confirme" in nota

    assert await g.nota_de_origem(ATUAL, USUARIO, file_ids=[TXT_ID]) is not None
    # Listar a pasta e mexer no documento desta conversa não avisam.
    assert await g.nota_de_origem(ATUAL, USUARIO, textos=["cd /workspace/files && ls -la"]) is None
    assert await g.nota_de_origem(ATUAL, USUARIO, textos=[f"python3 x.py {PREENCHIDO}"]) is None


@pytest.mark.asyncio
async def test_depois_de_adotado_nao_avisa_mais():
    g = _Memoria()
    await _historico_de_outra_conversa(g)
    await _turno_1(g)
    await g.block(ATUAL, USUARIO, anchor_text="usa as questões do Portal")
    assert await g.nota_de_origem(ATUAL, USUARIO, textos=[f"cat {TXT_SANDBOX}"]) is None


def test_sem_conversa_informada_tudo_e_desta_como_antes():
    ev = [{"doc_key": "x", "action": "saved", "name": TXT_NOME, "sandbox_path": TXT_SANDBOX,
           "file_id": TXT_ID, "session_id": VELHA, "created_at": datetime(2026, 11, 8)}]
    [v] = build_views(ev)
    assert v.in_session
    assert "## ARQUIVOS DESTA CONVERSA" in render_block([v])


# ── Costuras no AIService ────────────────────────────────────────────────────

def _svc_com(g):
    from modules.desktop.ai_service import AIService

    svc = object.__new__(AIService)
    svc.db = object()
    svc._file_ledger_obj = g

    class _Store:
        async def get(self, file_id):
            return SimpleNamespace(id=file_id, name=HASH, sha256="s", size_bytes=39019)

    svc.file_store = _Store()
    return svc


@pytest.mark.asyncio
async def test_anexo_entra_no_registro_uma_vez_por_conversa():
    g = _Memoria()
    svc = _svc_com(g)
    await svc._record_attachments([ANEXO], user_id=USUARIO, session_id=ATUAL)
    await svc._record_attachments([ANEXO], user_id=USUARIO, session_id=ATUAL)
    assert [(r.action, r.file_id) for r in g.rows] == [("attached", ANEXO)]


@pytest.mark.asyncio
async def test_anotador_so_fala_em_tool_que_le_conteudo():
    g = _Memoria()
    await _historico_de_outra_conversa(g)
    anotar = _svc_com(g)._origin_annotator(USUARIO, ATUAL)
    cmd = {"command": f"head -c 2500 {TXT_SANDBOX.rsplit('/', 1)[-1]}"}
    assert (await anotar("neo_bash", cmd, {"ok": True})).startswith("⚠ ORIGEM")
    assert await anotar("neo_list_files", {"path": "files"}, {"listing": TXT_SANDBOX}) is None
    assert (await anotar("file_stage", {"file_id": TXT_ID}, {"path": TXT_SANDBOX})) is not None


@pytest.mark.asyncio
async def test_aviso_de_origem_chega_no_resultado_que_o_modelo_le():
    from core.cognitive import cognitive_tools_for
    from shared import ToolCall, ToolResult
    from shared.types import Message, MessageRole

    from tests.test_cognitive_tool_loop import _FakeExecutor, _Resp, _ScriptedProvider, _svc

    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="neo_bash", arguments={"command": "cat questoes.txt"})]),
        _Resp(content="Esse TXT é de outra conversa — uso mesmo assim?"),
    ])

    async def anotar(nome, argumentos, resultado):
        return "⚠ ORIGEM: «questoes.txt» é de OUTRA conversa" if nome == "neo_bash" else None

    messages = [Message(role=MessageRole.USER, content="veio não preenchido")]
    await svc._run_cognitive_tool_loop(
        provider=provider, messages=messages, req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid.uuid4()), tool_executor=_FakeExecutor({"ok": True, "stdout": "Q1"}),
        slog=None, trace=None, annotate_result=anotar,
    )
    resultado = next(m.content for m in messages if m.role == MessageRole.USER and "Tool: neo_bash" in (m.content or ""))
    assert "⚠ ORIGEM" in resultado

    async def quebra(*_a):
        raise RuntimeError("registro fora do ar")

    provider2 = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="neo_bash", arguments={"command": "ls"})]),
        _Resp(content="ok"),
    ])
    out = await svc._run_cognitive_tool_loop(
        provider=provider2, messages=[Message(role=MessageRole.USER, content="x")], req_model=None,
        model_name="m", provider_name="p", tools=cognitive_tools_for(uuid.uuid4()),
        tool_executor=_FakeExecutor({"ok": True}), slog=None, trace=None, annotate_result=quebra,
    )
    assert out == "ok"


def test_anexo_docx_aponta_a_skill_so_quando_ela_esta_no_catalogo():
    svc = _svc_com(_Memoria())
    linha = f"- {HASH} — application/vnd.openxmlformats-officedocument.wordprocessingml.document, 38 KB"
    entrada = SimpleNamespace(name="docx")
    svc.skill_library = SimpleNamespace(get=lambda n: entrada if n == "docx" else None, is_available=lambda e: True)
    assert 'skill_read(name="docx")' in svc._dica_de_skill_para_anexos([linha])
    assert svc._dica_de_skill_para_anexos(["- planilha.xlsx — application/vnd.ms-excel"]) is None
    svc.skill_library = SimpleNamespace(get=lambda n: entrada, is_available=lambda e: False)
    assert svc._dica_de_skill_para_anexos([linha]) is None
