"""Autoridade do turno + portão em sombra (docs/release-request-plan.md, F0)."""

from __future__ import annotations

import pytest

from core.authority import (
    NEGAR,
    PERMITIR,
    PRINCIPAL_ADMIN,
    PRINCIPAL_AGENDA,
    PRINCIPAL_EXTERNO,
    SOLICITAR,
    TurnAuthority,
    bind_authority,
    canonical_url,
    classes_of,
    evaluate,
    gate_tool_call,
    is_external_source,
    is_sink,
    known,
    principal_from_metadata,
)
from core.authority.risk import ATUACAO, FONTE_EXTERNA, LEITURA_PRIVADA, REVELACAO


# ── tabela de risco ──────────────────────────────────────────────────────

def test_fonte_externa_contamina_e_sink_e_sink():
    assert is_external_source("web_fetch")
    assert is_external_source("email_message")
    assert not is_external_source("search_pks")
    assert is_sink("email_send")
    assert is_sink("run_command")
    assert not is_sink("get_weather")
    assert not is_sink("search_pks")  # ler o PKS não entrega nada a ninguém


def test_navegador_e_as_tres_coisas_ao_mesmo_tempo():
    classes = classes_of("browser_local_task")
    assert {FONTE_EXTERNA, REVELACAO, ATUACAO} <= classes


def test_web_fetch_revela_porque_o_dado_cabe_no_query_string():
    assert REVELACAO in classes_of("web_fetch")


def test_prefixo_separa_leitura_de_escrita():
    assert classes_of("fin_balances") == frozenset({LEITURA_PRIVADA})
    assert classes_of("fin_add_transaction") == frozenset({ATUACAO})
    assert classes_of("data_list") == frozenset({LEITURA_PRIVADA})
    assert classes_of("data_delete") == frozenset({ATUACAO})
    assert classes_of("schedule_list") == frozenset()
    assert classes_of("schedule_cancel") == frozenset({ATUACAO})


def test_tool_desconhecida_cai_no_fail_safe():
    assert not known("tool_que_nao_existe_ainda")
    assert classes_of("tool_que_nao_existe_ainda") == frozenset({ATUACAO})
    assert is_sink("tool_que_nao_existe_ainda")


# ── procedência de URL ───────────────────────────────────────────────────

def test_canonical_url_normaliza_host_e_tira_pontuacao_final():
    assert canonical_url("HTTPS://CDN.Loja.com/Foto.jpg") == "https://cdn.loja.com/Foto.jpg"
    assert canonical_url("https://loja.com/p?x=1),") == "https://loja.com/p?x=1"


def test_query_string_faz_parte_da_identidade_da_url():
    """`?d=<segredo>` não pode passar por ser 'a mesma URL' da foto."""
    a = TurnAuthority()
    a.note_tool_result("meli_product", {"foto": "https://cdn.loja.com/f.jpg"})
    assert a.url_has_provenance("https://cdn.loja.com/f.jpg")
    assert not a.url_has_provenance("https://cdn.loja.com/f.jpg?d=01234-567")


def test_url_do_resultado_tem_procedencia_url_inventada_nao():
    a = TurnAuthority()
    a.note_tool_result("web_fetch", "veja em https://loja.com.br/dp/123 o preço")
    assert a.url_has_provenance("https://loja.com.br/dp/123")
    assert not a.url_has_provenance("https://atacante.tld/p.png")


def test_url_escrita_pelo_usuario_tambem_tem_procedencia():
    a = TurnAuthority()
    a.note_user_text("abre https://loja.com.br/dp/9 pra mim")
    assert a.url_has_provenance("https://loja.com.br/dp/9")


def test_resultado_de_fonte_externa_contamina_o_turno():
    a = TurnAuthority()
    assert not a.tainted
    a.note_tool_result("web_fetch", "conteúdo")
    assert a.tainted and a.taint_sources == ["web_fetch"]


def test_leitura_privada_nao_contamina():
    a = TurnAuthority()
    a.note_tool_result("search_pks", {"cep": "01234-567"})
    assert not a.tainted


# ── principal ────────────────────────────────────────────────────────────

def test_principal_default_e_admin():
    assert principal_from_metadata(None) == (PRINCIPAL_ADMIN, None)
    assert principal_from_metadata({"client": "neo-desktop"})[0] == PRINCIPAL_ADMIN


def test_agenda_vem_do_metadata_do_scheduler():
    assert principal_from_metadata({"source": "schedule", "unattended": True})[0] == PRINCIPAL_AGENDA
    assert principal_from_metadata({"unattended": True})[0] == PRINCIPAL_AGENDA


def test_externo_e_declarado_por_quem_sabe_a_origem():
    principal, ref = principal_from_metadata(
        {"principal": "externo", "principal_ref": "fulano@x.com"}
    )
    assert (principal, ref) == (PRINCIPAL_EXTERNO, "fulano@x.com")


# ── decisão do portão ────────────────────────────────────────────────────

def test_turno_seu_e_limpo_passa():
    a = TurnAuthority()
    assert evaluate("email_send", a).efeito == PERMITIR


def test_turno_seu_contaminado_pede_liberacao():
    a = TurnAuthority()
    a.note_tool_result("web_fetch", "página")
    d = evaluate("email_send", a)
    assert d.efeito == SOLICITAR and "web_fetch" in d.motivo


def test_leitura_passa_mesmo_contaminado():
    """Contaminado não é proibido: só saída passa pelo portão."""
    a = TurnAuthority()
    a.note_tool_result("web_fetch", "página")
    assert evaluate("search_pks", a).efeito == PERMITIR
    assert evaluate("get_weather", a).efeito == PERMITIR


def test_pedido_externo_pede_liberacao_mesmo_sem_contaminacao():
    a = TurnAuthority(principal=PRINCIPAL_EXTERNO, principal_ref="fulano@x.com")
    d = evaluate("email_send", a)
    assert d.efeito == SOLICITAR and "fulano@x.com" in d.motivo


def test_agenda_nao_atua_sozinha():
    a = TurnAuthority(principal=PRINCIPAL_AGENDA)
    assert evaluate("service_request", a).efeito == SOLICITAR


def test_agendar_prompt_com_conteudo_de_terceiro_e_negado():
    """A injeção não pode se reagendar — isto é não, não é card."""
    a = TurnAuthority()
    a.note_tool_result("email_message", "corpo do e-mail")
    assert evaluate("schedule_create", a, {"acao_tipo": "prompt"}).efeito == NEGAR
    # Lembrete de texto fixo continua podendo pedir liberação, não é negado.
    assert evaluate("schedule_create", a, {"acao_tipo": "notify"}).efeito == SOLICITAR


def test_fora_de_turno_nao_ha_o_que_verificar():
    assert evaluate("neo_bash", None).efeito == PERMITIR


# ── modo sombra ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sombra_permite_mas_preserva_o_motivo(monkeypatch):
    monkeypatch.setenv("NEO_RELEASE_GATE_MODE", "shadow")
    from shared.config import get_settings

    get_settings.cache_clear()
    a = TurnAuthority()
    a.note_tool_result("web_fetch", "página")
    token = bind_authority(a)
    try:
        d = await gate_tool_call("email_send")
        assert d.efeito == PERMITIR
        assert "solicitar" in d.motivo  # o motivo real sobrevive para o relatório
    finally:
        from core.authority.turn import _current

        _current.reset(token)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_enforce_barra(monkeypatch):
    monkeypatch.setenv("NEO_RELEASE_GATE_MODE", "enforce")
    from shared.config import get_settings

    get_settings.cache_clear()
    a = TurnAuthority(principal=PRINCIPAL_EXTERNO, principal_ref="x@y")
    token = bind_authority(a)
    try:
        assert (await gate_tool_call("email_send")).efeito == SOLICITAR
    finally:
        from core.authority.turn import _current

        _current.reset(token)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_sem_autoridade_instalada_o_portao_nao_atrapalha():
    assert (await gate_tool_call("neo_bash")).efeito == PERMITIR


@pytest.mark.asyncio
async def test_mesma_chamada_nao_e_contada_duas_vezes(monkeypatch):
    """O trilho cognitivo delega ao global: sem dedupe, a sombra contaria o dobro."""
    monkeypatch.setenv("NEO_RELEASE_GATE_MODE", "shadow")
    from shared.config import get_settings

    get_settings.cache_clear()
    a = TurnAuthority()
    a.note_tool_result("web_fetch", "página")
    token = bind_authority(a)
    try:
        primeira = await gate_tool_call("email_send", None, "call-1")
        segunda = await gate_tool_call("email_send", None, "call-1")  # mesmo id
        outra = await gate_tool_call("email_send", None, "call-2")    # chamada de verdade
        assert "solicitar" in primeira.motivo
        assert segunda.motivo == "já avaliado neste turno"
        assert "solicitar" in outra.motivo
    finally:
        from core.authority.turn import _current

        _current.reset(token)
        get_settings.cache_clear()
