"""Os quatro defeitos da sessão 3733f3ae (2026-09-12, 03:12–03:15 UTC).

Diferente da ea040f16: aqui nenhum turno chegou ao núcleo cognitivo — todos
correram pelo caminho de tiro só do classificador. As falhas:

1. caminho com espaço escrito SEM aspas (2×) → o shell parte no meio;
2. a recuperação determinística devolve "nenhuma ação" e o turno vira despejo
   de erro cru ("Não consegui executar `cat …`. Erro: No such file or directory");
3. o validador do plano devolveu o `decision` ANINHADO → nenhum branch casou, o
   plano se deu por completo no passo 1 de 2 e o passo "ler o e-mail" nunca rodou;
4. o formatador só via a ÚLTIMA mensagem → "tenta de outra froma" foi respondido
   ao pé da letra, e a análise pedida dois turnos antes nunca veio.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.planning.plan_manager import PlanManager
from modules.desktop.ai_service import AIService, _quote_known_paths
from shared.config import get_settings

_PASTA = "/Users/daniel/Downloads/Conversa do WhatsApp com Dell Support - WA"


def _svc() -> AIService:
    svc = AIService.__new__(AIService)
    svc.settings = get_settings()
    svc._session_disk_evidence = {}
    return svc


# ── 1. Aspas em caminho com espaço ──────────────────────────────────────────


def test_quota_caminho_conhecido_e_o_resto_da_trilha():
    """O sufixo sem espaço (`/00-…/CORPO-DO-EMAIL.txt`) entra junto nas aspas."""
    cmd = f"cat {_PASTA}/00-ENVIAR-AO-PROCON/CORPO-DO-EMAIL.txt"
    out = _quote_known_paths(cmd, [_PASTA])
    assert out == f'cat "{_PASTA}/00-ENVIAR-AO-PROCON/CORPO-DO-EMAIL.txt"'


def test_hifen_solto_no_nome_da_pasta_nao_engana():
    """A pasta tem " - " no nome: heurística de "pare no primeiro -flag" cortaria errado."""
    cmd = f'find {_PASTA}/00-ENVIAR-AO-PROCON -iname "*.txt" -exec cat {{}} \\;'
    out = _quote_known_paths(cmd, [_PASTA])
    assert out.startswith(f'find "{_PASTA}/00-ENVIAR-AO-PROCON"')
    assert '-iname "*.txt"' in out, "as flags seguintes ficam intactas"


def test_caminho_ja_citado_nao_ganha_aspas_duplicadas():
    cmd = f'ls -la "{_PASTA}"'
    assert _quote_known_paths(cmd, [_PASTA]) == cmd


def test_caminho_desconhecido_nao_e_tocado():
    """Só mexemos no que a sessão já viu funcionar — o resto pode depender do corte."""
    cmd = "echo a b c && ls /tmp/x y"
    assert _quote_known_paths(cmd, [_PASTA]) == cmd


def test_known_paths_vem_do_working_dir_e_dos_comandos_que_rodaram():
    svc = _svc()
    sid = uuid4()
    svc._record_disk_evidence(sid, f'ls -la "{_PASTA}"', "00-ENVIAR-AO-PROCON\n")
    assert _PASTA in svc._known_paths(sid)
    assert "/tmp/com espaco" in svc._known_paths(sid, {"working_dir": "/tmp/com espaco"})


def test_fix_command_quoting_end_to_end():
    """O caso exato do turno 5: turno 1 citou a pasta, turno 5 esqueceu as aspas."""
    svc = _svc()
    sid = uuid4()
    svc._record_disk_evidence(sid, f'ls -la "{_PASTA}"', "00-ENVIAR-AO-PROCON\n")
    quebrado = f"cat {_PASTA}/00-ENVIAR-AO-PROCON/CORPO-DO-EMAIL.txt"
    assert svc._fix_command_quoting(quebrado, sid) == (
        f'cat "{_PASTA}/00-ENVIAR-AO-PROCON/CORPO-DO-EMAIL.txt"'
    )


def test_sem_evidencia_o_comando_passa_intacto():
    svc = _svc()
    cmd = f"cat {_PASTA}/x.txt"
    assert svc._fix_command_quoting(cmd, uuid4()) == cmd


# ── 2. Escalar ao cérebro em vez de despejar o erro ─────────────────────────


def test_so_shell_escala(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc.settings, "neo_brain_shell_enabled", True, raising=False)
    assert svc._pode_escalar_ao_cerebro("run_command", False) is True
    assert svc._pode_escalar_ao_cerebro("execute_command", False) is True
    assert svc._pode_escalar_ao_cerebro("save_generated_file", False) is False, (
        "tool nomeada tem tratamento próprio; o cérebro não conserta 404 de catálogo"
    )


def test_nao_escala_duas_vezes(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc.settings, "neo_brain_shell_enabled", True, raising=False)
    assert svc._pode_escalar_ao_cerebro("run_command", True) is False


def test_flag_desligada_mantem_o_comportamento_antigo(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc.settings, "neo_brain_shell_enabled", False, raising=False)
    assert svc._pode_escalar_ao_cerebro("run_command", False) is False


# ── 3. decision aninhado do validador de plano ──────────────────────────────


class _Step:
    def __init__(self, status="pending"):
        self.status = status


def test_decision_aninhado_e_achatado():
    """O JSON real que matou o passo 2/2 na sessão 3733f3ae."""
    parsed = {
        "validation": {"success": True},
        "decision": {
            "decision": "continue",
            "reason": "O arquivo foi identificado e agora deve ser lido.",
            "refined_step": {"tool_name": "run_command", "tool_arguments": {"command": "cat x"}},
        },
    }
    out = PlanManager._flatten_decision(parsed, _Step())
    assert out["decision"] == "continue"
    assert out["refined_step"]["tool_arguments"]["command"] == "cat x", (
        "o refined_step correto não pode ser jogado fora junto"
    )


def test_decision_invalido_com_passo_pendente_vira_continue():
    out = PlanManager._flatten_decision({"decision": "seguir_em_frente"}, _Step())
    assert out["decision"] == "continue", "falha segura é seguir o plano, não declarar vitória"


def test_decision_invalido_sem_passo_pendente_vira_finish():
    assert PlanManager._flatten_decision({"decision": None}, None)["decision"] == "finish"


def test_decision_valido_passa_intacto():
    out = PlanManager._flatten_decision({"decision": "finish", "reason": "ok"}, None)
    assert out["decision"] == "finish" and out["reason"] == "ok"


# ── 4. O formatador enxerga a conversa ──────────────────────────────────────


class _Resp:
    def get_content(self):
        return "análise do texto"


class _Captura:
    model = "m"

    def __init__(self):
        self.messages = None

    async def complete(self, messages, model=None, temperature=0.0, max_tokens=0, **kw):
        self.messages = messages
        return _Resp()


@pytest.mark.asyncio
async def test_formatador_recebe_o_pedido_original(monkeypatch):
    """"tenta de outra froma" só faz sentido com a pergunta de dois turnos antes."""
    import modules.model_config as mc

    prov = _Captura()

    async def _cfg(db, role, **kw):
        return {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash"}

    monkeypatch.setattr(mc, "get_model_config", _cfg)

    svc = _svc()
    svc.db = object()
    svc.llm = type("L", (), {"get_provider": lambda self, n=None: prov})()

    await svc._format_tool_result(
        "tenta de outra froma", "find ...", "conteúdo do e-mail...",
        history=[
            {"role": "user", "content": "ler os .txt e me diz o que acha do texto que fiz"},
            {"role": "assistant", "content": "Não consegui executar find..."},
            {"role": "user", "content": "tenta de outra froma"},
        ],
    )
    prompt = prov.messages[-1].content
    assert "o que acha do texto que fiz" in prompt, "o pedido real tem que chegar ao formatador"
    assert "o pedido que vale é o ORIGINAL" in prompt


@pytest.mark.asyncio
async def test_formatador_sem_historico_continua_funcionando(monkeypatch):
    import modules.model_config as mc

    prov = _Captura()

    async def _cfg(db, role, **kw):
        return {"provider": "openrouter", "model": "m"}

    monkeypatch.setattr(mc, "get_model_config", _cfg)
    svc = _svc()
    svc.db = object()
    svc.llm = type("L", (), {"get_provider": lambda self, n=None: prov})()

    out = await svc._format_tool_result("quais chaves?", "ls ~/.ssh", "a\nb")
    assert out == "análise do texto"
    assert "Conversa até aqui" not in prov.messages[-1].content
