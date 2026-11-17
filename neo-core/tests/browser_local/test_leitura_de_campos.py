"""Sessão c4316486: o texto de um campo de formulário é o VALOR dele.

A descrição da experiência (um textarea preenchido pelo JS do site) aparecia
cortada em 80 caracteres na linha do elemento, o `extract` no ref devolvia "não
tem texto nenhum" e a busca não olhava os campos — e o trecho a trocar estava
depois do corte. O agente falhou duas tarefas seguidas.
"""

from __future__ import annotations

import pytest

from core.browser_local.driver import BrowserLocalDriver

DESCRICAO = "Desenvolvimento full stack ponta a ponta de um produto de fidelidade para postos"


def test_linha_do_campo_avisa_que_o_valor_continua():
    linha = BrowserLocalDriver._element_line(
        {"ref": 13, "tag": "textarea", "text": "Escreva uma descrição", "value": DESCRICAO, "valor_total": 412}
    )
    assert 'o valor tem 412 caracteres — `extract {"ref": 13}` lê inteiro' in linha
    curto = BrowserLocalDriver._element_line({"ref": 3, "tag": "input", "text": "Empresa", "value": "Acme"})
    assert "caracteres" not in curto


class _Pagina:
    def __init__(self):
        self.chamadas = []

    async def act(self, action, args, timeout):
        self.chamadas.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return {"url": "https://x.test/exp", "title": "Exp", "ready": "complete", "stop": False,
                    "elements": [{"ref": 13, "tag": "textarea", "text": "Escreva", "value": DESCRICAO, "valor_total": 412}],
                    "text": "Descrição"}
        if action == "browser.extract":
            if args.get("busca"):
                return {"ok": True, "busca": True, "achados": 1, "total": 2282,
                        "text": "campo [13] «Descrição» (412 caracteres): …catracas e RH. Referência técnica do time. Stack…"}
            return {"ok": True, "campo": True, "label": "Descrição", "total": 412,
                    "text": DESCRICAO + " … Referência técnica do time. Stack: Node.js."}
        return {"ok": True}


@pytest.mark.asyncio
async def test_extract_no_campo_e_busca_nos_valores_chegam_ao_modelo():
    from tests.browser_local.test_conversa import _driver

    driver, ctx = _driver(
        [
            {"action": "extract", "args": {"busca": "Referência técnica do time"}},
            {"action": "extract", "args": {"ref": 13}},
            {"action": "done", "args": {"answer": "ok"}},
        ],
        _Pagina(),
    )
    await driver.run("trocar um trecho da descrição", start_url="https://x.test/exp")
    prompts = [c["messages"][-1].content for c in ctx.provider.calls]
    assert "campo [13] «Descrição»" in prompts[1]
    assert "valor de «Descrição» (412 caracteres), inteiro:" in prompts[2]
    assert "Referência técnica do time" in prompts[2]


@pytest.mark.asyncio
async def test_campo_de_senha_nunca_e_lido():
    from tests.browser_local.test_conversa import _driver

    class _Senha(_Pagina):
        async def act(self, action, args, timeout):
            if action == "browser.extract":
                return {"ok": True, "campo": True, "senha": True, "label": "Senha", "text": ""}
            return await super().act(action, args, timeout)

    driver, ctx = _driver(
        [{"action": "extract", "args": {"ref": 13}}, {"action": "done", "args": {"answer": "ok"}}], _Senha()
    )
    await driver.run("x", start_url="https://x.test/exp")
    assert "«Senha» é campo de SENHA: o valor não é lido" in ctx.provider.calls[1]["messages"][-1].content
