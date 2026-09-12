"""As chamadas utilitárias do CLIENT_TOOL usam o slot do painel, não o env.

Em 2026-09-12, produção (app.log, 02:37:02 UTC):

    POST https://api.deepseek.com/chat/completions "402 Payment Required"
    Failed to format result with AI: 402 - Insufficient Balance

Três chamadas nasceram com `self.llm.get_provider()` pelado, que resolve no
`DEFAULT_PROVIDER` do env (`deepseek`, a carteira zerada — crédito na OpenRouter
não cobre quem bate em api.deepseek.com direto). As três falhavam e degradavam em
silêncio: `_format_tool_result` despejava a saída CRUA do comando no chat (e no
histórico, virando a única memória do Neo sobre o que leu), `_check_followup_needed`
nunca encadeava o 2º passo, e o analisador de erro nunca diagnosticava nada.
"""

from __future__ import annotations

import pytest

from modules.desktop.ai_service import AIService
from shared.config import get_settings


class _Resp:
    def get_content(self) -> str:
        return "resposta formatada"


class _Provider:
    model = "provider-default"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, messages, model=None, temperature=0.0, max_tokens=0, **kwargs):
        self.calls.append({"model": model, "kwargs": kwargs})
        return _Resp()


class _LLM:
    """Espelha o LLMRouter: provider nomeado, e o default quando não vem nome."""

    def __init__(self, painel: _Provider, env: _Provider) -> None:
        self.painel, self.env = painel, env
        self.pedidos: list[str | None] = []

    def get_provider(self, name=None):
        self.pedidos.append(name)
        return self.painel if name == "openrouter" else self.env


def _svc(llm: _LLM) -> AIService:
    svc = AIService.__new__(AIService)
    svc.llm = llm
    svc.settings = get_settings()
    svc.db = object()
    return svc


@pytest.fixture
def painel_openrouter(monkeypatch):
    """Painel com o slot `cognitive` na OpenRouter, com pin de endpoint."""
    import modules.model_config as mc

    async def _fake(db, role, **kw):
        assert role == "cognitive"
        return {"provider": "openrouter", "model": "deepseek/deepseek-v4-flash@baidu"}

    monkeypatch.setattr(mc, "get_model_config", _fake)


@pytest.mark.asyncio
async def test_utility_llm_resolve_pelo_painel(painel_openrouter):
    painel, env = _Provider(), _Provider()
    provider, model = await _svc(_LLM(painel, env))._utility_llm()
    assert provider is painel, "tem que ir pra OpenRouter, não pro default do env"
    assert model == "deepseek/deepseek-v4-flash@baidu", "o pin do painel tem que sobreviver"


@pytest.mark.asyncio
async def test_format_tool_result_manda_o_modelo_do_slot(painel_openrouter):
    painel, env = _Provider(), _Provider()
    out = await _svc(_LLM(painel, env))._format_tool_result("quais chaves?", "ls ~/.ssh", "a\nb")
    assert out == "resposta formatada", "sem 402, a saída crua não vaza pro chat"
    assert env.calls == [], "o provider do env (carteira zerada) não pode ser tocado"
    assert painel.calls[0]["model"] == "deepseek/deepseek-v4-flash@baidu"
    # No OpenRouter o raciocínio vem ligado sozinho, é cobrado do MESMO teto de
    # tokens e sai ANTES do texto — era a outra metade das respostas vazias.
    assert painel.calls[0]["kwargs"].get("disable_thinking") is True


@pytest.mark.asyncio
async def test_followup_e_analise_de_erro_tambem_saem_do_env(painel_openrouter):
    painel, env = _Provider(), _Provider()
    svc = _svc(_LLM(painel, env))
    await svc._check_followup_needed("quais chaves?", "ls ~/.ssh", "a\nb")
    assert painel.calls and env.calls == []
    # Teto curto (200/500 tokens) só sobrevive com o raciocínio desligado.
    assert all(c["kwargs"].get("disable_thinking") is True for c in painel.calls)


@pytest.mark.asyncio
async def test_painel_ilegivel_cai_no_default_sem_levantar(monkeypatch):
    """Preferência quebrada degrada pro comportamento antigo — nunca derruba a chamada."""
    import modules.model_config as mc

    async def _boom(db, role, **kw):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(mc, "get_model_config", _boom)
    painel, env = _Provider(), _Provider()
    provider, model = await _svc(_LLM(painel, env))._utility_llm()
    assert provider is env and model is None
