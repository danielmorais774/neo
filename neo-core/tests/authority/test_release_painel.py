"""O que a tela de Liberações do painel precisa do core (release-request-plan §11).

Três coisas, e as três já erraram em outro lugar deste sistema:

- **histórico com o prazo varrido**: um pendente vencido listado como "pendente"
  faz alguém esperar por um card que não existe mais;
- **o texto do card vem da notificação**, não de uma segunda redação no painel —
  o que se aprova é um texto específico, amarrado por hash;
- **a origem do PEDIDO** (`nasceu_de_terceiro`) é uma definição só, usada pelo
  card do aparelho e pela tela. Duas cópias divergem no primeiro ajuste.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from core.release import ReleaseService
from core.release.service import ReleaseView, nasceu_de_terceiro
from db.models.release_request import (
    STATUS_APROVADA,
    STATUS_EXPIRADA,
    STATUS_PENDENTE,
    ReleaseRequest,
)


class _Session:
    """Devolve TUDO o que tem — o filtro por dono é do Postgres, não do teste."""

    def __init__(self, rows: list, notifs: list | None = None) -> None:
        self._rows = rows
        self._notifs = notifs or []
        self.commits = 0
        self._proxima = "release"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, row):
        return None

    async def commit(self):
        self.commits += 1

    async def execute(self, stmt):
        texto = str(stmt)
        alvo = self._notifs if "user_notification" in texto else self._rows

        class _R:
            def scalars(self_inner):
                class _S:
                    def all(self_s):
                        return alvo

                return _S()

        return _R()


class _DB:
    def __init__(self, rows, notifs=None) -> None:
        self.session = _Session(rows, notifs)

    def get_session(self):
        return self.session


class _Notif:
    def __init__(self, nid, title, body, payload) -> None:
        self.id = nid
        self.title = title
        self.body = body
        self.payload = payload


def _pedido(**kw) -> ReleaseRequest:
    base = dict(
        id=uuid4(),
        user_id=uuid4(),
        campos=["pks:email"],
        destino="cadastro@portal-regularize.com",
        destino_tipo="email",
        finalidade="regularizar a pendência",
        origem="admin",
        origem_ref=None,
        origem_evidencia="",
        risco="desconhecido",
        risco_motivos=["o turno leu conteúdo de terceiro (colagem)"],
        status=STATUS_PENDENTE,
        tool="pks_reveal",
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    base.update(kw)
    return ReleaseRequest(**base)


# ── a origem do pedido ───────────────────────────────────────────────────

def test_nasceu_de_terceiro_separa_quem_mandou_de_quem_pediu():
    assert nasceu_de_terceiro(_pedido()) is True
    assert nasceu_de_terceiro(_pedido(risco_motivos=[])) is False
    assert nasceu_de_terceiro(_pedido(risco_motivos=["disparo da agenda"])) is False


def test_a_vista_leva_a_origem_e_as_datas_para_a_tela():
    decidido = datetime.utcnow()
    d = ReleaseView.of(_pedido(status=STATUS_APROVADA, decided_at=decidido)).to_dict()
    assert d["nasceu_de_terceiro"] is True
    assert d["decidido_em"] and d["usado_em"] is None
    assert d["status"] == STATUS_APROVADA


# ── histórico ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_historico_marca_o_pendente_vencido_na_passagem():
    vencido = _pedido(expires_at=datetime.utcnow() - timedelta(minutes=1))
    vivo = _pedido()
    decidido = _pedido(status=STATUS_APROVADA)
    db = _DB([vencido, vivo, decidido])
    service = ReleaseService(db)

    rows = await service.list_recent(uuid4())

    assert vencido.status == STATUS_EXPIRADA
    assert vivo.status == STATUS_PENDENTE
    assert decidido.status == STATUS_APROVADA  # decidida não é tocada
    assert len(rows) == 3                      # histórico traz tudo
    assert db.session.commits == 1


# ── o texto do card ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_card_vem_da_notificacao_que_foi_para_o_aparelho():
    nid = uuid4()
    pedido = _pedido(notification_id=nid)
    notif = _Notif(nid, "Liberar email?", "Dado: email\nDestino: x@y.com", {"preview": "o texto que sai"})
    service = ReleaseService(_DB([pedido], [notif]))

    cards = await service.cards_of([pedido])

    assert cards[str(pedido.id)]["preview"] == "o texto que sai"
    assert "Dado: email" in cards[str(pedido.id)]["corpo"]


@pytest.mark.asyncio
async def test_pedido_sem_notificacao_nao_vira_erro():
    """Push pode ter falhado — a tela sem o texto ainda é útil."""
    service = ReleaseService(_DB([_pedido(notification_id=None)], []))
    assert await service.cards_of([_pedido(notification_id=None)]) == {}


@pytest.mark.asyncio
async def test_falha_ao_ler_notificacao_nao_derruba_a_tela():
    class _Quebrado:
        def get_session(self):
            raise RuntimeError("banco fora")

    service = ReleaseService(_Quebrado())
    assert await service.cards_of([_pedido(notification_id=uuid4())]) == {}
