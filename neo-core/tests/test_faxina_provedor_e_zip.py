"""Faxina do /workspace, filtro do OpenInference e a receita do zip em partes (14/11).

- Faxina: nada apagava o sandbox; o Sintel (session-34788907) deixou 1,6 GB.
- OpenInference: 20–40 s por rodada do cérebro (sessions b3ccdf21 e 10988735).
  O Daniel quis tirá-lo SEM fixar provedor.
- Zip em partes: o cérebro fez `split` + um zip por pedaço; `zip -s` corrompe ao
  juntar (testado no sandbox e no Mac); volumes `7z -tzip -v` funcionam.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.llm.openrouter_provider import OpenRouterProvider
from core.workbench import faxina
from core.workbench.faxina import Regras, comando, ler_saida, passar
from modules.desktop.ai_service import _WORKBENCH_INSTRUCTION


# ── filtro de provedor ───────────────────────────────────────────────────────

def test_sem_pin_o_openinference_sai_do_roteamento():
    kwargs: dict = {}
    OpenRouterProvider._apply_provider_ignore(kwargs)
    assert kwargs["extra_body"]["provider"] == {"ignore": ["open-inference"]}


def test_nao_fixa_provedor_nenhum():
    kwargs: dict = {}
    OpenRouterProvider._apply_provider_ignore(kwargs)
    pref = kwargs["extra_body"]["provider"]
    assert "order" not in pref and "only" not in pref and "allow_fallbacks" not in pref


def test_pin_explicito_fica_como_esta():
    kwargs: dict = {}
    modelo = OpenRouterProvider._split_provider_pin("minimax/minimax-m3.1-flash@nebius", kwargs)
    OpenRouterProvider._apply_provider_ignore(kwargs)
    assert modelo == "minimax/minimax-m3.1-flash"
    assert kwargs["extra_body"]["provider"] == {"order": ["nebius"], "allow_fallbacks": False}


def test_ignore_do_chamador_e_somado_e_o_resto_do_extra_body_fica():
    kwargs = {"extra_body": {"reasoning": {"enabled": False}, "provider": {"ignore": ["baidu"]}}}
    OpenRouterProvider._apply_provider_ignore(kwargs)
    assert kwargs["extra_body"]["provider"]["ignore"] == ["baidu", "open-inference"]
    assert kwargs["extra_body"]["reasoning"] == {"enabled": False}
    OpenRouterProvider._apply_provider_ignore(kwargs)  # idempotente
    assert kwargs["extra_body"]["provider"]["ignore"] == ["baidu", "open-inference"]


def test_lista_vazia_no_neo_toml_nao_mexe(monkeypatch):
    import core.llm.openrouter_provider as mod

    monkeypatch.setattr(mod, "_provedores_ignorados", lambda: ())
    kwargs: dict = {}
    OpenRouterProvider._apply_provider_ignore(kwargs)
    assert kwargs == {}


@pytest.mark.asyncio
async def test_complete_manda_o_ignore_para_a_api(monkeypatch):
    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    enviado: dict = {}

    async def _super_complete(self, *, messages, tools=None, model=None, **kwargs):
        enviado.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        "core.llm.openai_provider.OpenAIProvider.complete", _super_complete, raising=True
    )
    from shared.types import Message, MessageRole

    try:
        await provider.complete(
            messages=[Message(role=MessageRole.USER, content="oi")],
            model="deepseek/deepseek-v4.1-flash-1017",
        )
    except Exception:  # noqa: BLE001 — só interessa o que foi montado para a API
        pass
    assert enviado.get("extra_body", {}).get("provider", {}).get("ignore") == ["open-inference"]


# ── faxina ───────────────────────────────────────────────────────────────────

def test_comando_padrao_so_apaga_grande_parado_e_pasta_vazia():
    cmd = comando(Regras(grande_mb=20, grande_horas=24, anexos_horas=0))
    assert "-size +20M -mmin +1440" in cmd
    assert "-not -path '/workspace/.jobs/*'" in cmd
    assert "/workspace/files -xdev" not in cmd       # regra dos anexos desligada
    assert "-type d -empty" in cmd
    # `-prune` com `-delete` não funciona no GNU find (o -delete liga -depth).
    assert "-prune" not in cmd


def test_anexos_so_quando_ligado():
    cmd = comando(Regras(grande_mb=20, grande_horas=24, anexos_horas=72))
    assert "find /workspace/files -xdev -type f -mmin +4320" in cmd


def test_padrao_do_neo_toml_deixa_os_anexos_quietos():
    regras = faxina.regras_do_ajuste()
    assert regras is not None
    assert regras.anexos_horas == 0
    assert "/workspace/files -xdev" not in comando(regras)


def test_le_o_que_foi_apagado():
    saida = (
        "grande\t681285470\t/workspace/sintel/Sintel.2010.720p.mkv.zip\n"
        "pasta\t0\t/workspace/sintel\n"
        "find: '/workspace/x': Permission denied\n"
        "lixo sem tab\n"
    )
    assert ler_saida(saida) == [
        ("grande", 681285470, "/workspace/sintel/Sintel.2010.720p.mkv.zip"),
        ("pasta", 0, "/workspace/sintel"),
    ]


class _Sandbox:
    def __init__(self, stdout="", stderr="", explode=False):
        self.comandos: list[tuple[str, int | None]] = []
        self._out, self._err, self._explode = stdout, stderr, explode

    async def run(self, command, *, timeout=None, **kw):
        self.comandos.append((command, timeout))
        if self._explode:
            raise RuntimeError("container fora do ar")
        return SimpleNamespace(exit_code=0, stdout=self._out, stderr=self._err)


@pytest.mark.asyncio
async def test_passada_soma_o_que_apagou():
    sandbox = _Sandbox("grande\t1048576\t/workspace/a.zip\ngrande\t2097152\t/workspace/b.mkv\n")
    res = await passar(sandbox, Regras(20, 24, 0))
    assert res.bytes == 3 * 1048576
    assert [c for _, _, c in res.apagados] == ["/workspace/a.zip", "/workspace/b.mkv"]
    assert sandbox.comandos[0][1] == faxina.TIMEOUT_S


@pytest.mark.asyncio
async def test_passada_nunca_levanta():
    res = await passar(_Sandbox(explode=True), Regras(20, 24, 0))
    assert res.apagados == [] and "fora do ar" in res.erro


@pytest.mark.asyncio
async def test_loop_respeita_desligada(monkeypatch):
    sandbox = _Sandbox()
    monkeypatch.setattr(faxina, "regras_do_ajuste", lambda: None)
    dormidas: list[float] = []

    async def _sleep(s):
        dormidas.append(s)
        if len(dormidas) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(faxina.asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await faxina.faxina_loop(sandbox, atraso_inicial_s=1)
    assert sandbox.comandos == []
    assert dormidas[0] == 1 and dormidas[1] == 6 * 3600


@pytest.mark.asyncio
async def test_loop_passa_e_dorme_o_intervalo(monkeypatch):
    sandbox = _Sandbox()
    dormidas: list[float] = []

    async def _sleep(s):
        dormidas.append(s)
        if len(dormidas) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(faxina.asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await faxina.faxina_loop(sandbox, atraso_inicial_s=0)
    assert len(sandbox.comandos) == 1


# ── receita do zip em partes ────────────────────────────────────────────────

def test_prompt_do_sandbox_ensina_o_zip_em_partes():
    assert "7z a -tzip -mx=0 -v<N>m" in _WORKBENCH_INSTRUCTION
    assert "NÃO use `zip -s`" in _WORKBENCH_INSTRUCTION
    assert "cat saida.zip.0* > saida.zip" in _WORKBENCH_INSTRUCTION
    assert "faxina automática" in _WORKBENCH_INSTRUCTION
