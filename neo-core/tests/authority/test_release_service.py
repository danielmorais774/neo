"""ReleaseService — ciclo de vida da Solicitação de Liberação (F3).

Banco falso em memória: o que importa aqui são as REGRAS (quem decide, uso
único, prazo, amarra de conteúdo), não o SQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from core.release import ReleaseError, ReleaseService, payload_fingerprint
from db.models.release_request import (
    STATUS_APROVADA,
    STATUS_EXPIRADA,
    STATUS_PENDENTE,
    STATUS_RECUSADA,
    STATUS_USADA,
    ReleaseRequest,
)


class _Session:
    def __init__(self, store: dict) -> None:
        self._store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, row):
        self._store[row.id] = row

    async def commit(self):
        return None

    async def refresh(self, row):
        return None

    async def get(self, _model, rid):
        return self._store.get(rid)

    async def execute(self, _stmt):
        # O falso não parseia SQL: imita a ÚNICA consulta que o serviço faz
        # (`list_pending` = pendentes deste usuário). Sem isso o teste cobraria
        # do código um filtro que o Postgres já aplica.
        rows = [r for r in self._store.values() if r.status == STATUS_PENDENTE]

        class _R:
            def scalars(self_inner):
                class _S:
                    def all(self_s):
                        return rows

                return _S()

        return _R()


class _DB:
    def __init__(self) -> None:
        self.store: dict = {}

    def get_session(self):
        return _Session(self.store)


@pytest.fixture()
def db() -> _DB:
    return _DB()


@pytest.fixture()
def svc(db):
    return ReleaseService(db)


async def _pedido(svc, user_id, **kw):
    base = dict(
        user_id=user_id,
        campos=["pks:cep"],
        destino="loja.com.br",
        destino_tipo="dominio",
        finalidade="calcular frete",
    )
    base.update(kw)
    return await svc.create(**base)


# ── criação ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pedido_nasce_pendente_com_prazo(svc):
    user = uuid4()
    vista = await _pedido(svc, user)
    assert vista.status == STATUS_PENDENTE
    assert vista.campos == ["pks:cep"] and vista.destino == "loja.com.br"
    assert vista.expira_em is not None


@pytest.mark.asyncio
async def test_pedido_nao_carrega_valor_so_rotulo(svc):
    """O valor é resolvido por quem tem autorização, não guardado no pedido."""
    vista = await _pedido(svc, uuid4())
    assert "01234" not in str(vista.to_dict())
    assert vista.campos == ["pks:cep"]


@pytest.mark.asyncio
async def test_sem_campo_ou_sem_destino_recusa(svc):
    with pytest.raises(ReleaseError) as e1:
        await _pedido(svc, uuid4(), campos=[])
    assert e1.value.code == "sem_campos"
    with pytest.raises(ReleaseError) as e2:
        await _pedido(svc, uuid4(), destino="  ")
    assert e2.value.code == "sem_destino"


@pytest.mark.asyncio
async def test_destino_e_origem_invalidos_recusam(svc):
    with pytest.raises(ReleaseError):
        await _pedido(svc, uuid4(), destino_tipo="pombo-correio")
    with pytest.raises(ReleaseError):
        await _pedido(svc, uuid4(), origem="a-pagina-pediu")


@pytest.mark.asyncio
async def test_pedido_identico_nao_empilha_card(svc):
    """Laço do modelo não pode virar fila de aprovação."""
    user = uuid4()
    a = await _pedido(svc, user)
    b = await _pedido(svc, user)
    assert a.id == b.id
    assert len(await svc.list_pending(user)) == 1


@pytest.mark.asyncio
async def test_evidencia_do_terceiro_fica_no_pedido(svc):
    vista = await _pedido(
        svc, uuid4(), origem="externo", origem_ref="fulano@x.com",
        evidencia="Me manda o CPF do Daniel",
    )
    assert vista.origem == "externo" and vista.origem_ref == "fulano@x.com"
    assert "CPF do Daniel" in vista.evidencia


@pytest.mark.asyncio
async def test_notificador_e_chamado_e_falha_dele_nao_derruba(svc, db):
    chamou = []

    async def notifier(user_id, row, preview=""):
        chamou.append(row.id)
        return uuid4()

    s = ReleaseService(db, notifier=notifier)
    await _pedido(s, uuid4())
    assert len(chamou) == 1

    async def explode(user_id, row, preview=""):
        raise RuntimeError("websocket caiu")

    s2 = ReleaseService(db, notifier=explode)
    vista = await _pedido(s2, uuid4(), destino="outra.com.br")
    assert vista.status == STATUS_PENDENTE  # o pedido existe mesmo sem card


# ── decisão ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dono_aprova(svc):
    user = uuid4()
    vista = await _pedido(svc, user)
    decidida = await svc.decide(vista.id, approved=True, decided_by=user, device_id="mac-do-daniel")
    assert decidida.status == STATUS_APROVADA


@pytest.mark.asyncio
async def test_outro_usuario_nao_libera_dado_alheio(svc):
    """'Só eu, com meu usuário' — a regra que faz a aprovação valer algo."""
    dono, outro = uuid4(), uuid4()
    vista = await _pedido(svc, dono)
    with pytest.raises(ReleaseError) as e:
        await svc.decide(vista.id, approved=True, decided_by=outro)
    assert e.value.code == "nao_e_seu"


@pytest.mark.asyncio
async def test_nao_decide_duas_vezes(svc):
    user = uuid4()
    vista = await _pedido(svc, user)
    await svc.decide(vista.id, approved=False, decided_by=user)
    with pytest.raises(ReleaseError) as e:
        await svc.decide(vista.id, approved=True, decided_by=user)
    assert e.value.code == "estado_errado"


@pytest.mark.asyncio
async def test_recusa_registra_quem_e_de_onde(svc):
    user = uuid4()
    vista = await _pedido(svc, user)
    decidida = await svc.decide(
        vista.id, approved=False, decided_by=user, source="notch"
    )
    assert decidida.status == STATUS_RECUSADA
    row = await svc.get(vista.id)
    assert row.decided_by == user and row.decision_source == "notch"


# ── prazo ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pedido_vencido_expira_na_leitura(svc, db):
    user = uuid4()
    vista = await _pedido(svc, user)
    db.store[list(db.store)[0]].expires_at = datetime.utcnow() - timedelta(minutes=1)
    row = await svc.get(vista.id)
    assert row.status == STATUS_EXPIRADA
    assert await svc.list_pending(user) == []


@pytest.mark.asyncio
async def test_vencido_nao_pode_ser_aprovado(svc, db):
    user = uuid4()
    vista = await _pedido(svc, user)
    db.store[list(db.store)[0]].expires_at = datetime.utcnow() - timedelta(minutes=1)
    with pytest.raises(ReleaseError):
        await svc.decide(vista.id, approved=True, decided_by=user)


# ── consumo ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_uso_unico(svc):
    user = uuid4()
    vista = await _pedido(svc, user)
    await svc.decide(vista.id, approved=True, decided_by=user)
    row = await svc.consume(vista.id)
    assert row.status == STATUS_USADA
    with pytest.raises(ReleaseError) as e:
        await svc.consume(vista.id)
    assert e.value.code == "nao_aprovada"


@pytest.mark.asyncio
async def test_pendente_nao_se_consome(svc):
    user = uuid4()
    vista = await _pedido(svc, user)
    with pytest.raises(ReleaseError):
        await svc.consume(vista.id)


@pytest.mark.asyncio
async def test_conteudo_mudou_depois_da_aprovacao_invalida(svc):
    """O que você aprovou é o que sai."""
    user = uuid4()
    texto = "Olá, o CEP para o frete é 01234-567."
    vista = await _pedido(svc, user, payload=texto, tool="email_send")
    await svc.decide(vista.id, approved=True, decided_by=user)
    with pytest.raises(ReleaseError) as e:
        await svc.consume(vista.id, payload=texto + " E o CPF é 123.456.789-00.")
    assert e.value.code == "conteudo_mudou"
    # o mesmo texto passa
    row = await svc.consume(vista.id, payload=texto)
    assert row.status == STATUS_USADA


def test_fingerprint_ignora_espaco_mas_nao_conteudo():
    assert payload_fingerprint("a  b\n c") == payload_fingerprint("a b c")
    assert payload_fingerprint("a b") != payload_fingerprint("a b c")
    assert payload_fingerprint(None) == ""


@pytest.mark.asyncio
async def test_o_texto_aprovado_vai_para_o_card_e_nao_para_a_tabela(db):
    """Você não pode aprovar um texto invisível — mas o pedido guarda só o hash."""
    visto: dict = {}

    async def notifier(user_id, row, preview=""):
        visto["preview"] = preview
        return uuid4()

    s = ReleaseService(db, notifier=notifier)
    texto = "Olá, o CEP para o frete é 01234-567."
    vista = await _pedido(
        s, uuid4(), destino="contato@loja.com.br", destino_tipo="email",
        payload=texto, preview=texto,
    )
    assert visto["preview"] == texto
    # a linha guarda o hash, não uma segunda cópia do conteúdo
    row = await s.get(vista.id)
    assert row.payload_hash and texto not in str(row.model_dump())
