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


# ── Sessão 0e2fcc86 (2026-09-18): 11 idas ao disco, zero arquivo gravado ────
#
# "cria 3 arquivos .md com resumo desses arquivos". O turno morreu com "Parei
# depois de 10 ações seguidas na sua máquina neste pedido, pra não virar laço" —
# mas NENHUM comando tinha se repetido literalmente. Três defeitos em cadeia:
#
# 1. a janela de saídas (`_ACTUATION_STEPS_KEPT`=6) é menor que o teto de rodadas.
#    Da 8ª em diante o modelo parava de ver os primeiros passos e voltava a pedir
#    o que já tinha pedido — enquanto a guarda, que lembra de TODOS, o matava;
# 2. a guarda comparava string exata, e as rodadas 10 e 11 tinham 3 dos 4 alvos
#    iguais com a string diferente: passou batido;
# 3. leitura e escrita dividiam um teto de 10. Ler 7 arquivos e escrever 3 é
#    impossível assim — o crédito acabou antes da primeira escrita.

_A = "/Users/daniel/Desktop/teste-de-analise"
_R10 = (
    f'ls -la {_A}/*.md {_A}/*.pdf {_A}/*.csv {_A}/*.json 2>/dev/null; echo "---"; '
    f"ls -la {_A}/dados/tratados/txt/ {_A}/dados/tratados/txt_limpo/ 2>/dev/null"
)
_R11 = f"ls -la {_A}/*.md {_A}/*.csv {_A}/*.json {_A}/fontes/ 2>/dev/null"


def _passos(n: int, *, tool: str = "run_command") -> list[dict]:
    return [
        {"command": f"ls -la /pasta/{i}", "ok": True, "output": f"saida<{i}>", "tool": tool}
        for i in range(1, n + 1)
    ]


def test_indice_mostra_os_passos_que_sairam_da_janela():
    """O conserto do defeito 1: a saída velha sai, o COMANDO velho fica."""
    from modules.desktop.ai_service import _ACTUATION_STEPS_KEPT

    passos = _passos(10)
    note = _render_actuation_steps(passos)

    assert "Índice de tudo que já rodou" in note
    for i in range(1, 11):
        assert f"ls -la /pasta/{i}" in note, f"o passo {i} sumiu do índice"
    # A saída detalhada continua só dos últimos — o índice é barato, a saída não.
    assert "saida<1>" not in note, "saída antiga não devia voltar"
    assert "saida<10>" in note
    fora = 10 - _ACTUATION_STEPS_KEPT
    assert f"As saídas dos passos 1–{fora} já foram usadas" in note


def test_indice_nao_aparece_enquanto_tudo_cabe_na_janela():
    """Turno curto não paga nada: o índice seria a mesma lista duas vezes."""
    note = _render_actuation_steps(_passos(3))
    assert "Índice de tudo que já rodou" not in note
    assert "### Passo 3:" in note


def test_numeracao_do_detalhe_e_absoluta():
    """Passo 7 tem que se chamar 7 — senão o índice e o detalhe divergem."""
    note = _render_actuation_steps(_passos(8))
    assert "### Passo 8:" in note
    assert "### Passo 1:" not in note, "o 7º passo não pode se chamar 'Passo 1'"


def test_quase_repeticao_pega_o_que_a_igualdade_exata_deixava_passar():
    from modules.desktop.ai_service import (
        _near_repeated_actuation,
        _repeated_actuation,
    )

    steps = [{"command": f"ls -la {_A}/"}, {"command": _R10}]

    assert _repeated_actuation(steps, _R11) is False, "as strings são diferentes"
    assert _near_repeated_actuation(steps, _R11) == [2], "3 de 4 alvos já respondidos"


def test_quase_repeticao_nao_barra_pedido_novo():
    from modules.desktop.ai_service import _near_repeated_actuation

    steps = [{"command": f"ls -la {_A}/"}, {"command": _R10}]

    # Caminho que nunca apareceu → é pergunta nova, tem que passar.
    assert _near_repeated_actuation(steps, f"cat {_A}/RESUMO_EXECUTIVO.md") == []
    assert _near_repeated_actuation(steps, f"ls -la {_A}/fichas/ {_A}/anexos/") == []
    # Verbo diferente no mesmo caminho lê outra coisa (listar ≠ ler).
    assert _near_repeated_actuation([{"command": "ls -la /a/b"}], "cat /a/b") == []
    # Comando sem alvo nenhum: só a igualdade literal conta.
    assert _near_repeated_actuation(steps, "df -h") == []


def test_opcao_que_muda_a_resposta_nao_e_repeticao():
    """Medido no replay da 0e2fcc86: sem olhar as flags, `ls -laR X/` depois de
    `ls -la X/` era acusado de repetir — e o -R é justamente informação nova."""
    from modules.desktop.ai_service import _near_repeated_actuation

    steps = [{"command": f"ls -la {_A}/dados/"}]
    assert _near_repeated_actuation(steps, f"ls -laR {_A}/dados/") == []
    assert _near_repeated_actuation(steps, f"grep -r politica {_A}/dados/") == []
    # Mas reordenar as mesmas flags não engana: `-al` é `-la`.
    assert _near_repeated_actuation(steps, f"ls -al {_A}/dados/") == [1]


@pytest.mark.asyncio
async def test_quase_repetido_volta_pro_cerebro_em_vez_de_rodar_ou_morrer():
    """Nem despacha (pagaria uma ida pelo mesmo dado) nem mata o turno (tem 1 alvo novo)."""
    svc = _svc()
    sid = uuid4()
    seen: list[dict] = []
    _spy_reenter(svc, seen)

    steps = [{"command": f"ls -la {_A}/", "ok": True, "output": "...", "tool": "run_command"},
             {"command": _R10, "ok": True, "output": "...", "tool": "run_command"}]
    car = ClientActuationRequested("run_command", {"command": _R11}, "tc")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="cria os 3 .md", user_id=None,
        metadata={}, trace=None, slog=None, depth=2, steps=steps,
    ))

    assert not any(r.command_request for r in out), "não podia ter despachado"
    assert seen, "tinha que voltar pro cérebro"
    assert "ESSE COMANDO NÃO RODOU" in seen[0]["extra_note"]
    assert "peça SÓ o que falta" in seen[0]["extra_note"]
    # O empurrão vira passo: é ele que aparece no índice e que limita as voltas.
    assert seen[0]["steps"][-1]["kind"] == "nudge"
    assert seen[0]["steps"][-1]["ok"] is False


@pytest.mark.asyncio
async def test_terceira_volta_no_mesmo_ponto_responde_com_o_que_ja_tem():
    """O empurrão é bom duas vezes; na terceira é laço: não despacha, e a resposta
    sai das saídas que o turno já tem (session-1bbdd7c5), sem tool nenhuma."""
    from modules.desktop.ai_service import _MAX_REPEAT_NUDGES

    svc = _svc()
    recebido = []

    async def formatar(user_message, command, output, **kw):
        recebido.append(output)
        return "Os 3 .md estão na pasta."

    svc._format_tool_result = formatar
    steps = [{"command": _R10, "ok": True, "output": "RESUMO.md\nFICHAS.md", "tool": "run_command"}]
    steps += [
        {"command": f"ls -la {_A}/*.md {_A}/*.json {_A}/*.csv {_A}/extra{i}/",
         "ok": False, "kind": "nudge", "tool": "run_command", "error": "x"}
        for i in range(_MAX_REPEAT_NUDGES)
    ]
    car = ClientActuationRequested("run_command", {"command": _R11}, "tc")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=uuid4(), user_message="cria os 3 .md", user_id=None,
        metadata={}, trace=None, slog=None, depth=3, steps=steps,
    ))

    assert not any(r.command_request for r in out)
    assert out[-1].is_final
    assert out[-1].message == "Os 3 .md estão na pasta."
    assert "RESUMO.md" in recebido[0]


@pytest.mark.asyncio
async def test_terceira_volta_no_mesmo_ponto_encerra():
    """Sem saída aproveitável no turno, a terceira volta fecha com a parada."""
    from modules.desktop.ai_service import _MAX_REPEAT_NUDGES

    svc = _svc()
    steps = [{"command": _R10, "ok": True, "output": "", "tool": "run_command"}]
    # Cada volta com uma string DIFERENTE: repetir letra por letra já cai na guarda
    # de igualdade, que é outra porta. O que se mede aqui é o reembaralhamento.
    steps += [
        {"command": f"ls -la {_A}/*.md {_A}/*.json {_A}/*.csv {_A}/extra{i}/",
         "ok": False, "kind": "nudge", "tool": "run_command", "error": "x"}
        for i in range(_MAX_REPEAT_NUDGES)
    ]
    car = ClientActuationRequested("run_command", {"command": _R11}, "tc")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=uuid4(), user_message="cria os 3 .md", user_id=None,
        metadata={}, trace=None, slog=None, depth=3, steps=steps,
    ))

    assert out[-1].is_final
    assert "terceira volta no mesmo ponto" in out[-1].message


@pytest.mark.asyncio
async def test_ler_muito_nao_gasta_o_credito_de_escrever():
    """O defeito 3: 10 leituras matavam o turno antes do primeiro write_file."""
    from modules.desktop.ai_service import _MAX_READ_ACTUATIONS

    svc = _svc()
    leituras = [
        {"command": f"read_user_file /a/{i}.md", "ok": True, "output": "x",
         "tool": "read_user_file"}
        for i in range(_MAX_READ_ACTUATIONS)
    ]
    car = ClientActuationRequested(
        "write_file", {"path": "/Users/daniel/Desktop/r.md", "content": "oi"}, "tc"
    )
    out = await _collect(svc._emit_client_actuation(
        car, session_id=uuid4(), user_message="cria os 3 .md", user_id=None,
        metadata={}, trace=None, slog=None, depth=len(leituras), steps=leituras,
    ))

    assert any(r.tool_request for r in out), "a escrita tem orçamento próprio"


@pytest.mark.asyncio
async def test_teto_de_leitura_diz_que_foi_garimpo_e_nao_laco():
    """O defeito 4: a mensagem acusava repetição quando o que acabou foi crédito."""
    from modules.desktop.ai_service import _MAX_READ_ACTUATIONS

    svc = _svc()
    leituras = _passos(_MAX_READ_ACTUATIONS)
    car = ClientActuationRequested("run_command", {"command": "ls -la /outra/pasta"}, "tc")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=uuid4(), user_message="cria os 3 .md", user_id=None,
        metadata={}, trace=None, slog=None, depth=len(leituras), steps=leituras,
    ))

    assert out[-1].is_final
    assert "leituras/listagens" in out[-1].message
    assert "laço" not in out[-1].message, "não houve laço nenhum"
    # A lista de passos usa numeração absoluta.
    assert f"{_MAX_READ_ACTUATIONS}. `ls -la /pasta/{_MAX_READ_ACTUATIONS}`" in out[-1].message
