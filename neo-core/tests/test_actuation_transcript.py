"""Transcript do turno de client-actuation — o que fecha o laço de comando repetido.

Sessão ea040f16 (2026-09-12), interação 6 — "analisa o arquivo":

    rodada 1 → wc -l "<arquivo>"        → 535
    rodada 2 → wc -l "<arquivo>"        (IDÊNTICO)
    rodada 3 → cat "<arquivo>" | wc -l  → 535
    rodada 4 → cat "<arquivo>"          → estourou _MAX_ACTUATION_DEPTH

Nenhuma resposta ao usuário. Não foi o modelo sendo burro: `_reenter_cognitive`
reconstrói o prompt a partir do histórico do BANCO, que ainda não tem o turno em
curso, e a saída do último comando era colada dentro do system prompt. O array de
mensagens da rodada 2 era idêntico ao da rodada 1 — não havia como saber que o
`wc -l` já tinha rodado.

Cobre, isolado de LLM/gRPC:
- o transcript ACUMULA entre rodadas (e diz explicitamente pra não repetir);
- ele atravessa _emit_client_actuation → _pending_actuations → retomada;
- a saída vai por mensagem no FIM, não dentro do system prompt (cache de prefixo);
- a guarda de fundamentação aceita caminho IMPRESSO por comando aprovado.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from modules.desktop.ai_service import (
    AIService,
    ClientActuationRequested,
    IntentType,
    _render_actuation_steps,
)

_ARQUIVO = (
    "/Users/daniel/Downloads/Conversa do WhatsApp com Dell Support - WA/"
    "00-ENVIAR-AO-PROCON/CORPO-DO-EMAIL.txt"
)


class _FakeSessions:
    def __init__(self, history=None) -> None:
        self.added: list[dict] = []
        self._history = history or []

    async def add_message(self, **kw):
        self.added.append(kw)

    async def get_history(self, *a, **kw):
        return self._history


def _svc(history=None) -> AIService:
    svc = AIService.__new__(AIService)
    svc.file_store = None
    svc.sessions = _FakeSessions(history)
    svc._session_attachments = {}
    svc._pending_actuations = {}
    svc._pending_traces = {}
    svc._live_sessions = set()
    svc._image_capable_sessions = set()
    svc._session_registers = {}
    svc._session_disk_evidence = {}
    return svc


def _spy_reenter(svc, recorder):
    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace, steps=None):
        recorder.append({"pend": pend, "steps": steps or [], "extra_note": extra_note})
        from modules.desktop.ai_service import AIServiceResponse

        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
            message="[reentrada]", is_final=True,
        )

    svc._reenter_cognitive = _reenter


async def _collect(agen) -> list:
    return [x async for x in agen]


# ── O transcript em si ──────────────────────────────────────────────────────


def test_transcript_lista_todos_os_passos_e_probe_repeticao():
    note = _render_actuation_steps([
        {"command": f'wc -l "{_ARQUIVO}"', "ok": True, "output": f"     535 {_ARQUIVO}"},
        {"command": f'cat "{_ARQUIVO}"', "ok": True, "output": "PARA: procon@...\nASSUNTO: ..."},
    ])
    assert "wc -l" in note and "cat " in note, "os dois passos têm que estar visíveis"
    assert "535" in note, "a saída real do passo anterior não pode sumir"
    assert "PARA: procon@..." in note
    assert "Não repita um comando que já está nesta lista" in note


def test_transcript_vazio_nao_gera_mensagem():
    """Turno normal (sem atuação) não paga nada por esta feature."""
    assert _render_actuation_steps([]) == ""


def test_passo_antigo_encolhe_e_o_ultimo_fica_inteiro():
    """Orçamento decrescente: o passo fresco é o dado, os velhos são lembrete."""
    velho = "\n".join(f"linha-velha-{i}" for i in range(300))
    novo = "\n".join(f"linha-nova-{i}" for i in range(150))
    note = _render_actuation_steps([
        {"command": "ls -R /a", "ok": True, "output": velho},
        {"command": "cat /b", "ok": True, "output": novo},
    ])
    assert "linha-velha-299" not in note, "passo antigo deve ser cortado"
    assert "linhas omitidas" in note, "o corte precisa ser anunciado"
    assert "linha-nova-149" in note, "o passo fresco vai inteiro"


def test_passo_que_falhou_aparece_como_falha():
    note = _render_actuation_steps([
        {"command": "free -h", "ok": False, "output": "", "error": "command not found: free"},
    ])
    assert "FALHOU" in note and "command not found: free" in note


# ── A cadeia: rodada N+1 enxerga a rodada N ─────────────────────────────────


@pytest.mark.asyncio
async def test_segunda_rodada_enxerga_o_comando_da_primeira():
    """O caso ea040f16: o cérebro não pode reexecutar o `wc -l` que acabou de rodar."""
    svc = _svc()
    sid = uuid4()
    seen: list[dict] = []
    _spy_reenter(svc, seen)

    # Rodada 1: o cérebro pede `wc -l`, o desktop devolve a saída.
    pend1 = {"kind": "shell", "command": f'wc -l "{_ARQUIVO}"',
             "user_message": "analisa o arquivo", "depth": 0, "steps": []}
    await _collect(svc._resume_after_command(
        sid, success=True, result=f"     535 {_ARQUIVO}", error=None,
        user_message="analisa o arquivo", pend=pend1, slog=None, trace=None,
    ))
    passos1 = seen[0]["steps"]
    assert len(passos1) == 1

    # Rodada 2: o cérebro pede outro comando — o estado guardado tem que carregar
    # o passo anterior, senão a rodada 3 recomeça cega.
    car = ClientActuationRequested("run_command", {"command": f'cat "{_ARQUIVO}"'}, "tc2")
    await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="analisa o arquivo", user_id=None,
        metadata={}, trace=None, slog=None, depth=1, steps=passos1,
    ))
    pend2 = svc._pending_actuations[str(sid)]
    assert len(pend2["steps"]) == 1, "o estado de retomada perdeu o passo anterior"

    # Rodada 3: chega a saída do `cat` — o transcript agora tem os DOIS.
    await _collect(svc._resume_after_command(
        sid, success=True, result="PARA: procon@...", error=None,
        user_message="analisa o arquivo", pend=pend2, slog=None, trace=None,
    ))
    passos2 = seen[1]["steps"]
    assert [p["command"] for p in passos2] == [f'wc -l "{_ARQUIVO}"', f'cat "{_ARQUIVO}"']
    note = _render_actuation_steps(passos2)
    assert "535" in note and "PARA: procon@..." in note


@pytest.mark.asyncio
async def test_saida_nao_vai_mais_para_o_system_context():
    """A saída é a coisa mais volátil do turno; no system prompt ela matava o cache."""
    svc = _svc()
    sid = uuid4()
    seen: list[dict] = []
    _spy_reenter(svc, seen)

    await _collect(svc._resume_after_command(
        sid, success=True, result="saida-secreta-123", error=None,
        user_message="e aí?", pend={"kind": "shell", "command": "ls", "depth": 0},
        slog=None, trace=None,
    ))
    assert seen[0]["extra_note"] is None, "sucesso não precisa de nota; a saída está no transcript"
    assert "saida-secreta-123" in _render_actuation_steps(seen[0]["steps"])


# ── Fundamentação por evidência de disco ────────────────────────────────────


@pytest.mark.asyncio
async def test_caminho_impresso_por_comando_aprovado_fundamenta():
    """`ls -R` aprovado é o disco do usuário respondendo — não é chute do modelo.

    Note a barra DOBRADA: é assim que o `ls -R` imprime o cabeçalho da pasta.
    """
    svc = _svc(history=[{"role": "user", "content": "acessa a pasta do whatsapp"}])
    sid = uuid4()
    assert await svc._file_path_is_grounded(_ARQUIVO, sid, "leia o arquivo") is False

    svc._record_disk_evidence(sid, "ls -R ...", (
        "/Users/daniel/Downloads/Conversa do WhatsApp com Dell Support - WA//00-ENVIAR-AO-PROCON:\n"
        "COMO-ENVIAR.txt\nCORPO-DO-EMAIL.txt\n"
    ))
    assert await svc._file_path_is_grounded(_ARQUIVO, sid, "leia o arquivo") is True


@pytest.mark.asyncio
async def test_homonimo_de_outra_pasta_nao_fundamenta():
    """Nome igual visto em OUTRA pasta não autoriza este caminho."""
    svc = _svc()
    sid = uuid4()
    svc._record_disk_evidence(sid, "ls /Users/daniel/Desktop", "CORPO-DO-EMAIL.txt\n")
    assert await svc._file_path_is_grounded(_ARQUIVO, sid, "leia o arquivo") is False


@pytest.mark.asyncio
async def test_usuario_citando_o_nome_sem_extensao_e_em_caixa_baixa():
    """"ler o corpo-do-email" fundamenta CORPO-DO-EMAIL.txt — era um seletor à toa."""
    svc = _svc(history=[{"role": "user", "content": "ler o corpo-do-email o arquivo txt"}])
    assert await svc._file_path_is_grounded(_ARQUIVO, uuid4(), "analisa pra mim") is True


@pytest.mark.asyncio
async def test_evidencia_e_por_sessao():
    """O que rodou na sessão A não fundamenta caminho na sessão B."""
    svc = _svc()
    a, b = uuid4(), uuid4()
    svc._record_disk_evidence(a, "ls", _ARQUIVO)
    assert await svc._file_path_is_grounded(_ARQUIVO, a, "pega") is True
    assert await svc._file_path_is_grounded(_ARQUIVO, b, "pega") is False
