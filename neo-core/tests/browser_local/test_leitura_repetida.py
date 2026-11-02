"""A mesma leitura devolvida cinco vezes (job 6633351e).

Pedido: "tenho algum produto pra chegar?". A resposta certa entrou no caderno no
passo 2, aos 5 segundos. O job morreu no teto de 25 passos, 115s depois, com
estado `failed` — e a resposta que chegou ao usuário trazia CINCO cópias do menu
de atalhos de teclado da Amazon.

O motor: `extract` sem `ref` mirava `document.body`, que sai em ordem de
documento, então os 1500 caracteres eram sempre a moldura do site. O texto tinha
mais de 200 caracteres, então ia sozinho para o caderno. E a trava de releitura
que já existia pergunta *"mesma TELA?"* — como havia clique e rolagem entre um
`extract` e outro, a tela mudava e a trava se limpava. Cinco telas diferentes,
uma leitura só.

Três promessas, uma por camada:
  1. leitura repetida não entra no caderno de novo (o caderno vai para o usuário);
  2. o modelo é AVISADO de que releu, com a saída junto;
  3. insistir na terceira é recusado.

O alvo do `extract` sem `ref` é do lado do device (`js_extract` no Rust, provado
em `neo-desktop-tauri/scripts/snapshot-lab`).
"""

from __future__ import annotations

import json

import pytest

from core.browser_local.driver import BrowserLocalDriver


class _Resposta:
    def __init__(self, content: str) -> None:
        self._content = content

    def get_content(self) -> str:
        return self._content


class _Provider:
    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)

    async def complete(self, **kwargs):
        passo = (
            self.steps.pop(0)
            if self.steps
            else {"action": "give_up", "args": {"reason": "sem roteiro"}}
        )
        return _Resposta(json.dumps(passo))


class _LLM:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get_provider_for_model(self, model: str):
        return self.provider


#: O lixo real: os primeiros 1500 caracteres do `body` de qualquer página da
#: Amazon, em ordem de documento.
MOLDURA = (
    "Ir para\nConteúdo principal\nAtalhos do teclado\nPesquisar\nopt\n+\n/\n"
    "Carrinho\nshift\n+\nopt\n+\nC\nPágina inicial\nshift\n+\nopt\n+\nH\n"
    "Seus Pedidos\nshift\n+\nopt\n+\nO\nMostrar/ocultar atalhos\n"
    "Para se mover entre os produtos, use as setas do teclado.\n"
) * 2


def _tela(marker: str):
    return {
        "url": f"https://www.amazon.com.br/gp/css/order-history#{marker}",
        "title": "Seus pedidos",
        "ready": "complete",
        "stop": False,
        "elements": [{"ref": 0, "tag": "button", "text": f"Rastrear {marker}"}],
        "text": f"Seus pedidos ({marker})",
    }


def _driver(steps, *, leituras=None, **kwargs):
    """``leituras``: o que cada `browser.extract` devolve, em ordem."""
    fila_texto = list(leituras or [])
    pedidos: list[tuple[str, dict]] = []
    telas = [_tela(c) for c in "abcdefgh"]

    async def act(action, args, timeout):
        pedidos.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 7}
        if action == "browser.snapshot":
            return telas.pop(0) if len(telas) > 1 else telas[0]
        if action == "browser.extract":
            item = fila_texto.pop(0) if fila_texto else {"text": MOLDURA}
            return {"ok": True, **item}
        if action == "browser.screenshot":
            return {"ok": True, "data": "FOTO", "marks": True}
        return {"ok": True}

    driver = BrowserLocalDriver(
        act=act,
        llm=_LLM(_Provider(steps)),
        model="texto",
        vision_model=None,
        **kwargs,
    )
    driver._pedidos = pedidos  # type: ignore[attr-defined]
    return driver


def _detalhe(result, acao: str, n: int = 0):
    passos = [s for s in result.steps if s.action == acao]
    return passos[n].detail or ""


@pytest.mark.asyncio
async def test_leitura_repetida_nao_entra_duas_vezes_no_caderno():
    """O caderno é o que vai para o USUÁRIO.

    O critério era só o tamanho, então o mesmo bloco de moldura entrou cinco
    vezes e a resposta saiu com cinco cópias dele.
    """
    driver = _driver(
        [
            {"action": "extract", "args": {}},
            {"action": "scroll", "args": {"delta": 700}},
            {"action": "extract", "args": {}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        leituras=[{"text": MOLDURA}, {"text": MOLDURA}],
    )

    result = await driver.run("o que está a caminho?")

    assert len(driver.notes) == 1, f"a mesma leitura entrou {len(driver.notes)}x no caderno"
    # O caderno é colado no resumo: uma anotação, um marcador.
    assert result.summary.count("\n- ") == 1, "o resumo entregou a mesma leitura mais de uma vez"


@pytest.mark.asyncio
async def test_leitura_repetida_e_dita_ao_modelo_com_a_saida_junto():
    """Silêncio aqui é o que fazia ele tentar de novo.

    Cinco leituras idênticas chegavam ao histórico como cinco blocos de texto
    iguais, sem nada dizendo que eram iguais.
    """
    driver = _driver(
        [
            {"action": "extract", "args": {}},
            {"action": "scroll", "args": {"delta": 700}},
            {"action": "extract", "args": {}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        leituras=[
            {"text": MOLDURA, "label": "Moldura do site"},
            {"text": MOLDURA, "label": "Moldura do site"},
        ],
    )

    result = await driver.run("o que está a caminho?")

    primeira = _detalhe(result, "extract", 0)
    segunda = _detalhe(result, "extract", 1)
    assert "Moldura do site" in primeira, "o resultado diz de ONDE leu"
    assert "MESMO TEXTO" in segunda
    assert "`ref`" in segunda, "a recusa/aviso aponta a saída, nunca só barra"


@pytest.mark.asyncio
async def test_terceira_leitura_identica_e_recusada():
    """Mudar de tela deixou de ser desculpa para reler a mesma coisa."""
    driver = _driver(
        [
            {"action": "extract", "args": {}},
            {"action": "scroll", "args": {"delta": 700}},
            {"action": "extract", "args": {}},
            {"action": "scroll", "args": {"delta": 700}},
            {"action": "extract", "args": {}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        leituras=[{"text": MOLDURA}] * 3,
    )

    result = await driver.run("o que está a caminho?")

    lidas = [a for a, _ in driver._pedidos if a == "browser.extract"]
    assert len(lidas) == 2, f"a terceira tinha que ser recusada antes de sair ({len(lidas)})"
    recusa = next(s for s in result.steps if s.args.get("recusado"))
    assert "DUAS VEZES o mesmo texto" in (recusa.detail or "")


@pytest.mark.asyncio
async def test_leitura_nova_continua_indo_ao_caderno():
    """A trava é da REPETIÇÃO, não do `extract`.

    O auto-caderno existe porque uma leitura substancial que morre no histórico
    é uma tarefa perdida (job 6785cf70) — isso continua valendo.
    """
    outro = "PEDIDO 000-000 — Produto Exemplo 113g — Chega segunda — R$ 49,86. " * 4
    driver = _driver(
        [
            {"action": "extract", "args": {}},
            {"action": "scroll", "args": {"delta": 700}},
            {"action": "extract", "args": {}},
            {"action": "done", "args": {"answer": "pronto"}},
        ],
        leituras=[{"text": MOLDURA}, {"text": outro}],
    )

    await driver.run("o que está a caminho?")

    assert len(driver.notes) == 2, "duas leituras diferentes, duas anotações"
    assert any("Produto Exemplo" in n for n in driver.notes)
