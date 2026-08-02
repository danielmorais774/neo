"""Shell na máquina do usuário sob iniciativa do CÉREBRO (neo_brain_shell_enabled).

O buraco que isto fecha (sessão 69feab87): "quantas chaves ssh tenho" rodou um
comando e respondeu; o follow-up "Quais? lista os nomes" caiu no núcleo cognitivo
(o classificador devolveu prosa em vez de JSON) e lá o cérebro NÃO tinha como
tocar o terminal — respondeu "a ferramenta de terminal não está disponível nesta
sessão, roda isso aí e me manda a saída". O usuário teve que mandar "então roda e
me diz" pra conseguir a resposta que já era pra ter vindo.

Cobre, isolado de LLM/gRPC:
- a tool entrar (e sair) do toolset cognitivo conforme a flag;
- `run_command` do cérebro virando o MESMO command_request (com aprovação) do
  fluxo CLIENT_TOOL, com o estado de retomada guardado;
- as guardas (comando vazio, caminho do sandbox);
- a retomada com a saída real — inclusive no erro, que volta pro cérebro em vez
  de virar desculpa;
- o descarte do estado quando o usuário NEGA a permissão;
- `_carries_json` do roteador, que decide a retentativa de reparo.

Molde de tests/files/test_actuation.py (métodos exercitados direto, sem __init__).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from modules.desktop.ai_service import AIService, ClientActuationRequested, IntentType


class _FakeSessions:
    def __init__(self) -> None:
        self.added: list[dict] = []

    async def add_message(self, **kw):
        self.added.append(kw)

    async def get_history(self, *a, **kw):
        return []


def _svc() -> AIService:
    svc = AIService.__new__(AIService)
    svc.file_store = None
    svc.sessions = _FakeSessions()
    svc._session_attachments = {}
    svc._pending_actuations = {}
    svc._pending_traces = {}
    svc._live_sessions = set()
    svc._image_capable_sessions = set()
    svc._session_registers = {}
    return svc


def _spy_reenter(svc, recorder):
    """_reenter_cognitive real puxa LLM+DB; aqui só grava a nota de retomada."""

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace):
        recorder.append({"extra_note": extra_note, "user_message": user_message, "pend": pend})
        from modules.desktop.ai_service import AIServiceResponse

        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
            message="[reentrada]", is_final=True,
        )

    svc._reenter_cognitive = _reenter


async def _collect(agen) -> list:
    return [x async for x in agen]


# ── Toolset do cérebro ──────────────────────────────────────────────────────


def test_run_command_entra_no_toolset_cognitivo_com_a_flag(monkeypatch):
    from core.cognitive import read_tools

    names = {t.name for t in read_tools.cognitive_tools_for(uuid4())}
    assert "run_command" in names, "com a flag ligada (default) o cérebro precisa ver o terminal"


def test_run_command_some_do_toolset_com_a_flag_off(monkeypatch):
    from core.cognitive import read_tools
    from shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "neo_brain_shell_enabled", False, raising=False)
    names = {t.name for t in read_tools.cognitive_tools_for(uuid4())}
    assert "run_command" not in names


def test_tool_desmente_a_desculpa_de_terminal_indisponivel():
    """A descrição é o que o modelo lê: precisa dizer que o terminal EXISTE."""
    from modules.desktop.tools import DESKTOP_TOOLS, SHELL_ACTUATION_TOOL_NAMES

    schema = next(t for t in DESKTOP_TOOLS if t.name == "run_command")
    assert "run_command" in SHELL_ACTUATION_TOOL_NAMES
    desc = schema.description.lower()
    assert "sempre disponível" in desc
    assert "colar a saída" in desc or "cole" in desc


# ── Emissão: run_command do cérebro → command_request ───────────────────────


@pytest.mark.asyncio
async def test_shell_do_cerebro_vira_command_request_com_estado_de_retomada():
    svc = _svc()
    sid = uuid4()
    car = ClientActuationRequested(
        "run_command", {"command": "ls -la ~/.ssh", "reason": "Vou olhar a pasta."}, "tc1"
    )
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="quais chaves ssh eu tenho?", user_id=None,
        metadata={"client": "neo-desktop"}, trace=None, slog=None,
    ))

    reqs = [r for r in out if r.command_request]
    assert len(reqs) == 1, "shell do cérebro passa pela MESMA aprovação do CLIENT_TOOL"
    assert reqs[0].command_request["command"] == "ls -la ~/.ssh"
    # Nada de tool_request: o trilho é o de comando, não o de tool nomeada.
    assert not any(r.tool_request for r in out)

    pend = svc._pending_actuations[str(sid)]
    assert pend["kind"] == "shell"
    assert pend["command"] == "ls -la ~/.ssh"
    assert pend["user_message"] == "quais chaves ssh eu tenho?"


@pytest.mark.asyncio
async def test_comando_vazio_nao_e_despachado():
    svc = _svc()
    sid = uuid4()
    car = ClientActuationRequested("run_command", {"command": "   "}, "tc2")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="dá uma olhada aí", user_id=None,
        metadata={}, trace=None, slog=None,
    ))
    assert not any(r.command_request for r in out)
    assert out[-1].is_final
    assert str(sid) not in svc._pending_actuations


@pytest.mark.asyncio
async def test_comando_citando_workspace_e_bloqueado():
    """Sandbox do cérebro ≠ disco do usuário — mesma trava do _handle_client_tool."""
    svc = _svc()
    sid = uuid4()
    car = ClientActuationRequested("run_command", {"command": "cp /workspace/x.csv ~/Desktop"}, "tc3")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="joga esse csv na área de trabalho", user_id=None,
        metadata={}, trace=None, slog=None,
    ))
    assert not any(r.command_request for r in out)
    assert out[-1].is_final
    assert "sandbox" in (out[-1].message or "").lower()
    assert str(sid) not in svc._pending_actuations


# ── Retomada: a saída volta pro cérebro ─────────────────────────────────────


@pytest.mark.asyncio
async def test_saida_do_comando_volta_pro_cerebro_nao_pro_formatador():
    svc = _svc()
    sid = uuid4()
    seen: list[dict] = []
    _spy_reenter(svc, seen)

    pend = {"kind": "shell", "command": "ls -la ~/.ssh", "user_message": "quais chaves ssh?", "depth": 0}
    out = await _collect(svc._resume_after_command(
        sid, success=True, result="id_ed25519_github\nid_ed25519_github.pub\npc_gamer\npc_gamer.pub",
        error=None, user_message="quais chaves ssh?", pend=pend, slog=None, trace=None,
    ))

    assert len(seen) == 1
    note = seen[0]["extra_note"]
    assert "SAÍDA DO COMANDO" in note
    assert "id_ed25519_github" in note, "a saída REAL tem que chegar no cérebro"
    assert "ls -la ~/.ssh" in note
    assert out[-1].is_final


@pytest.mark.asyncio
async def test_erro_do_comando_tambem_volta_pro_cerebro():
    """Falha vira diagnóstico do cérebro, não 'não consegui, roda você'."""
    svc = _svc()
    sid = uuid4()
    seen: list[dict] = []
    _spy_reenter(svc, seen)

    pend = {"kind": "shell", "command": "free -h", "user_message": "quanta memória livre?", "depth": 0}
    await _collect(svc._resume_after_command(
        sid, success=False, result=None, error="command not found: free",
        user_message="quanta memória livre?", pend=pend, slog=None, trace=None,
    ))

    note = seen[0]["extra_note"]
    assert "FALHOU" in note
    assert "command not found: free" in note
    assert "run_command" in note, "o cérebro deve ser instruído a tentar de novo, não a desistir"


@pytest.mark.asyncio
async def test_process_tool_result_so_retoma_com_estado_de_shell():
    """Sem marca `kind=shell`, um execute_command comum segue no fluxo genérico.

    É por isso que `run_command` mora em _SHELL_ACTUATION_TOOLS e não dentro de
    _CLIENT_ACTUATION_TOOLS: o nome sozinho colidiria com o CLIENT_TOOL do
    classificador, que também devolve resultado de `run_command`/`execute_command`.
    """
    from modules.desktop.ai_service import _CLIENT_ACTUATION_TOOLS, _SHELL_ACTUATION_TOOLS

    assert "run_command" not in _CLIENT_ACTUATION_TOOLS
    assert "run_command" in _SHELL_ACTUATION_TOOLS

    svc = _svc()
    sid = uuid4()
    # Só há estado quando o CÉREBRO pediu; o caminho do classificador não grava nada.
    assert svc._pending_actuations.get(str(sid)) is None


# ── Permissão negada ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_negar_permissao_descarta_o_estado_de_retomada():
    """Senão o estado fica pendurado e sequestra o próximo comando da sessão."""
    svc = _svc()
    sid = uuid4()

    class _FakeSession:
        id = sid
        user_id = None

    async def _get_or_create_session(**kw):
        return _FakeSession()

    svc.sessions.get_or_create_session = _get_or_create_session
    svc._pending_actuations[str(sid)] = {"kind": "shell", "command": "ls ~"}

    out = await _collect(svc.process_command_approval(
        session_id=sid, command_id="c1", approved=False, command="ls ~", user_message="lista aí",
    ))
    assert out[-1].is_final
    assert str(sid) not in svc._pending_actuations


# ── Roteador: detectar prosa e reparar ──────────────────────────────────────


def test_carries_json_distingue_classificacao_de_prosa():
    from core.router.intent_router import IntentRouter

    assert IntentRouter._carries_json('{"intent": "CLIENT_TOOL", "confidence": 1.0}') is True
    assert IntentRouter._carries_json('```json\n{"intent": "DIRECT_RESPONSE"}\n```') is True
    assert IntentRouter._carries_json('Segue: {"intent": "CLIENT_TOOL"} pronto') is True
    # O caso real da sessão 69feab87: o classificador respondeu ao usuário.
    assert IntentRouter._carries_json(
        "Aqui estão os arquivos encontrados em `/Users/daniel/.ssh`:\n\n"
        "id_ed25519\nid_ed25519.pub\n\n**Resumo:** 19 arquivos no total"
    ) is False
    assert IntentRouter._carries_json("") is False


# ── Turno infinito: run_command sem comando (sessão 9f7abeec) ───────────────


@pytest.mark.asyncio
async def test_run_command_sem_comando_nunca_e_despachado():
    """A trava que fecha o travamento infinito.

    Cadeia real da sessão 9f7abeec: guard de grounding zera `raw_command` e força
    needs_plan → `generate_plan` devolve None (LLM vazio) → o fluxo caía no
    despacho genérico e mandava `run_command` com `{}` pro desktop. O desktop não
    tem o que executar e NUNCA devolve tool_result: "Executando" pra sempre, até a
    conexão cair. Nada pode sair daqui sem comando dentro.
    """
    from shared import IntentType as _IT
    from shared.types import RoutingDecision

    svc = _svc()
    svc.planner = None
    sid = uuid4()
    decision = RoutingDecision(
        intent=_IT.CLIENT_TOOL, confidence=1.0, tool_name="run_command",
        tool_arguments={}, raw_command=None, needs_plan=False,
    )
    out = await _collect(svc._handle_client_tool(
        sid, "quais chaves de ssh eu tenho?", decision, trace=None, slog=None,
        user_id=None, metadata={"os_platform": "macos", "home_dir": "/Users/daniel"},
    ))
    assert not any(r.tool_request for r in out), "nada pode ir pro desktop sem comando"
    assert not any(r.command_request for r in out)
    assert out[-1].is_final, "o turno tem que FECHAR, não ficar pendurado"


def test_dispatch_would_hang_cobre_os_dois_caminhos():
    """A checagem é uma função só porque existem DOIS despachos de client tool
    (direto e passo de plano) e os dois já travaram um turno de verdade —
    9f7abeec pelo direto, e8e5664b pelo plano."""
    from modules.desktop.ai_service import _dispatch_would_hang

    assert _dispatch_would_hang("run_command", {}) is True
    assert _dispatch_would_hang("run_command", None) is True
    assert _dispatch_would_hang("run_command", {"command": "   "}) is True
    assert _dispatch_would_hang("run_command", {"command": "ls -la"}) is False
    assert _dispatch_would_hang("run_command", {"cmd": "df -h"}) is False
    # Outras tools têm contrato próprio — não é essa checagem que as valida.
    assert _dispatch_would_hang("save_generated_file", {}) is False


def test_dispatch_would_hang_barra_placeholder_nao_refinado():
    """Placeholder de passo dependente não pode ir pro shell como comando literal.

    O prompt do planner manda pôr "[SERÁ GERADO COM BASE NOS ARQUIVOS LIDOS]" no
    passo que depende do anterior; quem troca pelo comando real é o
    `advance_and_refine`. Quando ele falha (vazio do DeepSeek), o fallback é
    "continua com o passo como está" — e o placeholder era despachado de verdade
    (visto rodando na sessão ad49f58b, depois que o passo de plano voltou a
    executar no cliente). Roda, dá "command not found", e o plano fecha com lixo.
    """
    from modules.desktop.ai_service import _dispatch_would_hang

    for placeholder in (
        "[SERÁ GERADO COM BASE NOS ARQUIVOS LIDOS]",
        "[será gerado depois]",
        "[comando definido com base no resultado anterior]",
        "<conteúdo baseado no arquivo>",
    ):
        assert _dispatch_would_hang("run_command", {"command": placeholder}) is True, placeholder

    # Comando real que por acaso começa com colchete continua passando — a trava
    # é pro placeholder do planner, não pra sintaxe de shell.
    assert _dispatch_would_hang("run_command", {"command": "[ -f ~/.zshrc ] && echo ok"}) is False


def test_plano_com_passo_sem_comando_e_invalidado():
    """`_validate_plan` só olhava descrição/contagem: um passo `run_command` com
    tool_arguments={} passava e era despachado (sessão e8e5664b)."""
    from core.planning.plan_manager import ExecutionPlan, PlanManager, PlanStep

    pm = PlanManager.__new__(PlanManager)
    furado = ExecutionPlan(goal="ler a chave", steps=[
        PlanStep(index=0, description="Listar ~/.ssh", tool_name="run_command", tool_arguments={}),
    ])
    assert pm._validate_plan(furado) is False

    bom = ExecutionPlan(goal="ler a chave", steps=[
        PlanStep(index=0, description="Listar ~/.ssh", tool_name="run_command",
                 tool_arguments={"command": "ls -la ~/.ssh"}, raw_command="ls -la ~/.ssh"),
    ])
    assert pm._validate_plan(bom) is True


def test_passo_dependente_pode_comecar_sem_comando():
    """Passo dependente tem o comando montado depois, pelo refine_step, com a
    saída do passo anterior — invalidar o plano por isso mataria a cascata."""
    from core.planning.plan_manager import ExecutionPlan, PlanManager, PlanStep

    pm = PlanManager.__new__(PlanManager)
    plano = ExecutionPlan(goal="ler a chave do pc gamer", steps=[
        PlanStep(index=0, description="Listar ~/.ssh", tool_name="run_command",
                 tool_arguments={"command": "ls -la ~/.ssh"}, raw_command="ls -la ~/.ssh"),
        PlanStep(index=1, description="Exibir a .pub encontrada", tool_name="run_command",
                 tool_arguments={}, depends_on_previous=True),
    ])
    assert pm._validate_plan(plano) is True
