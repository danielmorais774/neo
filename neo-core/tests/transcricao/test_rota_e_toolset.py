"""A aba Transcrição do neo-panel e a regra "a tool só entra no toolset com chave"."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.routes import transcricao_config as rota
from core.authority.risk import FONTE_EXTERNA, LEITURA_PRIVADA, classes_of
from core.transcricao import TRANSCRICAO_TOOLS, config, groq
from core.transcricao import cota as cota_mod
from tests.transcricao.conftest import FakeDB


@pytest.fixture
def painel(settings, monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(cota_mod, "_cota", None)
    rota.set_dependencies(db, tool_montada=True)
    yield db
    rota.set_dependencies(None, tool_montada=False)


@pytest.mark.asyncio
async def test_get_mostra_origem_final_e_padroes(painel):
    estado = await rota.get_config()
    assert estado["key"] == {"source": "env", "last4": "abcd", "stored": False, "broken": False, "can_store": True}
    assert estado["so_gratuita"] is True
    assert estado["modelo"] == "whisper-large-v3-turbo"
    assert estado["cota"]["audio_hora"]["limite"] == 7170  # 7200 - folga de 30
    assert estado["limites"]["arquivo_max_mb"] == 25
    assert "gsk_" not in str(estado)


@pytest.mark.asyncio
async def test_gravar_chave_testa_no_groq_antes(painel, monkeypatch):
    vistas = []

    async def listar(chave, **kw):
        vistas.append(chave)
        return [{"id": "whisper-large-v3-turbo", "dono": "OpenAI", "ativo": True}]

    monkeypatch.setattr(groq, "listar_modelos", listar)
    estado = await rota.update_key(rota.ChaveUpdate(api_key=" gsk_nova_00001111 "))
    assert vistas == ["gsk_nova_00001111"]
    assert estado["key"]["source"] == "painel" and estado["key"]["last4"] == "1111"

    estado = await rota.delete_key()
    assert estado["key"]["source"] == "env"


@pytest.mark.asyncio
async def test_chave_recusada_nao_e_gravada(painel, monkeypatch):
    async def listar(chave, **kw):
        raise groq.GroqErro("chave", "o Groq recusou a chave (HTTP 401): Invalid API Key", status=401)

    monkeypatch.setattr(groq, "listar_modelos", listar)
    with pytest.raises(HTTPException) as e:
        await rota.update_key(rota.ChaveUpdate(api_key="gsk_ruim"))
    assert e.value.status_code == 400 and "não passou no teste" in e.value.detail
    assert config.origem_da_chave() == "env"


@pytest.mark.asyncio
async def test_opcoes_valem_na_hora(painel):
    estado = await rota.update_config(rota.OpcoesUpdate(so_gratuita=False, modelo="whisper-large-v3"))
    assert estado["so_gratuita"] is False and estado["modelo"] == "whisper-large-v3"
    assert config.so_gratuita() is False
    with pytest.raises(HTTPException):
        await rota.update_config(rota.OpcoesUpdate(modelo="rm -rf /"))


@pytest.mark.asyncio
async def test_modelos_filtra_whisper(painel):
    from tests.transcricao.conftest import FakeGroq

    falso = FakeGroq()
    modelos = await groq.listar_modelos("gsk_x", transport=falso.transport)
    assert [m["id"] for m in modelos] == ["whisper-large-v3", "whisper-large-v3-turbo"]


def test_tool_e_fonte_externa_e_leitura_privada():
    assert classes_of("neo_transcrever") == frozenset({FONTE_EXTERNA, LEITURA_PRIVADA})


def _servico_falso(tem_tool: bool):
    from core.workbench import WORKBENCH_TOOLS

    nomes = {t.name for t in WORKBENCH_TOOLS} | ({"neo_transcrever"} if tem_tool else set())
    return SimpleNamespace(
        agent=SimpleNamespace(tool_registry=SimpleNamespace(exists=lambda n: n in nomes)),
        job_manager=None,
    )


def test_toolset_so_tem_a_tool_com_chave(settings):
    from modules.desktop.ai_service import AIService

    nomes = {t.name for t in AIService._agent_action_tools(_servico_falso(True))}
    assert "neo_transcrever" in nomes and "neo_bash" in nomes

    settings.groq_api_key = None
    nomes = {t.name for t in AIService._agent_action_tools(_servico_falso(True))}
    assert "neo_transcrever" not in nomes and "neo_bash" in nomes


def test_toolset_sem_sandbox_nao_tem_a_tool(settings):
    from modules.desktop.ai_service import AIService

    nomes = {t.name for t in AIService._agent_action_tools(_servico_falso(False))}
    assert "neo_transcrever" not in nomes


def test_classificador_nao_ve_a_tool():
    """Prefixo neo_: só o cérebro chama (ele sabe onde o arquivo está)."""
    from core.router.intent_router import server_tool_can_run

    assert not server_tool_can_run("neo_transcrever", execution="server-side")


def test_schema_tem_o_nome_literal_e_nenhum_parametro_obrigatorio():
    (tool,) = TRANSCRICAO_TOOLS
    assert tool.name == "neo_transcrever" and tool.module == "transcricao"
    assert not any(p.required for p in tool.parameters.values())
