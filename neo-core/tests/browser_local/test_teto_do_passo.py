"""O passo do navegador com o teto adaptativo — o caso do job 9c9b5bae.

O `glm-5.3-flash` exige raciocínio, e o raciocínio sai do mesmo `max_tokens`.
Em 6 de 30 passos ele usou o teto inteiro e o JSON voltou com 60 caracteres;
a recusa mandava "encurtar o answer" e o modelo acreditou.
"""

from __future__ import annotations

import json

import pytest

from core.browser_local import driver as driver_mod
from core.browser_local.driver import BrowserLocalDriver
from core.llm.teto_adaptativo import TetoAdaptativo
from shared import ajustes as ajustes_mod

#: O cru real do passo 2 do job 9c9b5bae.
_CORTADO = '{"thought": "O wait_for \'Iniciar\' falhou porque o question'


class _Resposta:
    def __init__(self, content: str, motivo: str, raciocinio: int) -> None:
        self._content, self._motivo, self._raciocinio = content, motivo, raciocinio

    def get_content(self) -> str:
        return self._content

    def finish_reason(self) -> str:
        return self._motivo

    def reasoning_tokens(self) -> int:
        return self._raciocinio


class _Provider:
    """Simula um modelo que raciocina ``pensa`` tokens antes de responder."""

    def __init__(self, roteiro: list[tuple[int, dict]]) -> None:
        self.roteiro = list(roteiro)
        self.tetos: list[int] = []

    async def complete(self, **kw):
        teto = kw["max_tokens"]
        self.tetos.append(teto)
        pensa, passo = self.roteiro[0] if self.roteiro else (0, {"action": "give_up", "args": {"reason": "fim"}})
        if pensa + 100 > teto:
            # Cortado: o mesmo passo volta na próxima chamada, com o mesmo pensar.
            return _Resposta(_CORTADO, "length", min(pensa, teto))
        if self.roteiro:
            self.roteiro.pop(0)
        return _Resposta(json.dumps(passo), "stop", pensa)


class _LLM:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get_provider_for_model(self, _model):
        return self.provider


@pytest.fixture(autouse=True)
def _limpo(tmp_path, monkeypatch):
    monkeypatch.setenv("NEO_AJUSTES", str(tmp_path / "nao-existe.toml"))
    monkeypatch.setattr(ajustes_mod, "_cache", None)
    # Aprendizado novo a cada teste: o de produção é do processo inteiro.
    monkeypatch.setattr(driver_mod, "_TETO_DECISAO", TetoAdaptativo("navegador.decisao"))


def _driver(provider) -> BrowserLocalDriver:
    async def act(action, args, timeout):
        if action == "browser.claim":
            return {"tab_ref": 1}
        if action == "browser.snapshot":
            return {
                "url": "https://exemplo.com/", "title": "x", "ready": "complete",
                "stop": False, "elements": [], "text": "x", "main": None,
            }
        return {"ok": True}

    return BrowserLocalDriver(act=act, llm=_LLM(provider), model="glm", trace=None)


@pytest.mark.asyncio
async def test_raciocinio_que_come_o_teto_repete_o_passo_com_folga():
    provider = _Provider([(2500, {"action": "done", "args": {"answer": "pronto"}})])
    result = await _driver(provider).run("tarefa")
    # 2000 cortou (2500 pensando); 4000 coube — sem passo perdido nem recusa.
    assert provider.tetos == [2000, 4000]
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_resposta_longa_cortada_nao_repete():
    """Cortado com pouco raciocínio é a RESPOSTA longa: mais teto não muda nada."""

    class _Longo(_Provider):
        async def complete(self, **kw):
            self.tetos.append(kw["max_tokens"])
            if len(self.tetos) == 1:
                return _Resposta('{"thought": "x", "action": "click", "args": {"re', "length", 100)
            return _Resposta(json.dumps({"action": "done", "args": {"answer": "ok"}}), "stop", 50)

    provider = _Longo([])
    await _driver(provider).run("tarefa")
    assert provider.tetos[:2] == [2000, 2000]


@pytest.mark.asyncio
async def test_corte_depois_da_escada_vira_recusa_que_culpa_o_raciocinio():
    """Uma repetição por passo; se cortar de novo, a recusa diz a causa certa.

    O passo seguinte já sai com o teto aprendido (o máximo, depois de dois
    cortes), e aí o mesmo pensar de 5000 tokens cabe.
    """
    provider = _Provider([(5000, {"action": "done", "args": {"answer": "ok"}})])
    drv = _driver(provider)
    recusas: list[str] = []
    original = drv._log

    def _espiao(event, *args, **kwargs):
        if event == "refused":
            recusas.append(args[0])
        return original(event, *args, **kwargs)

    drv._log = _espiao  # type: ignore[method-assign]
    result = await drv.run("tarefa")
    assert provider.tetos == [2000, 4000, 6000]
    assert len(recusas) == 1 and "RACIOCÍNIO" in recusas[0]
    assert "answer" not in recusas[0]
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_o_teto_aprende_entre_passos():
    pesado = (1800, {"action": "wait", "args": {"ms": 10}})
    provider = _Provider([pesado, pesado, (100, {"action": "done", "args": {"answer": "ok"}})])
    await _driver(provider).run("tarefa")
    assert provider.tetos[0] == 2000
    assert provider.tetos[1] > 2000
