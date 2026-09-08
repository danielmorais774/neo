"""Barramento de eventos — as guardas que evitam o bug caro (§7 do plano).

O que é testado aqui é exatamente o que dá defeito MUDO em produção:

- **sem inscrição, nada acontece** — o requisito número um: "toca um AC/DC" não
  pode deixar gatilho armado nem gerar requisição;
- **evento repetido/atrasado não avança a fila** — o reconnect da extensão
  reenvia snapshot, e sem dedup a fila pula três músicas de uma vez;
- **faixa inesperada pausa em vez de brigar** — o usuário trocou de música na
  mão; a fila não pode sequestrar o player de volta;
- **inscrição no vazio é recusada** — sem aba e sem faixa, `event_subscribe`
  não grava (é a defesa que não depende do bom senso do LLM);
- **o link sai da linha do cadastro**, nunca do payload do evento.

Sem Postgres: registry e DatasetService são dublês em memória. O que se prova
aqui é a REGRA, e é ela que a suíte precisa guardar.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from core.events.registry import Subscription
from core.events.router import EventRouter
from core.events.runners import RunnerContext, proxima_da_fila
from core.events.schema import EventTrigger, TriggerError, parse_triggers
from modules.events.executor import EventsExecutor
from modules.events.tools import EVENT_TOOL_NAMES

USER = uuid.uuid4()

TRIGGER_RAW = {
    "nome": "fila_musica",
    "quando": ["faixa_terminando", "faixa_terminou"],
    "canal": "musica",
    "faz": "proxima_da_fila",
    "fila": "fila",
    "ordem": "posicao asc",
    "filtro": {"estado": "pendente"},
    "campo_link": "link",
    "campo_titulo": "musica_titulo",
    "campo_estado": "estado",
    "marca": {"despachada": "tocando", "anterior": "tocada"},
    "encerra_quando": "fila_vazia",
}


# ---------------------------------------------------------------------------
# Dublês
# ---------------------------------------------------------------------------


class _Row:
    """Linha crua do event_subscription (o que o registry lê do banco)."""

    def __init__(self, **kw):
        agora = datetime.utcnow()
        base = dict(
            id=uuid.uuid4(),
            user_id=USER,
            device_id="mac-daniel",
            channel="musica",
            events=["faixa_terminando", "faixa_terminou"],
            target={"site": "youtube", "tab_id": 42},
            expected_ref="AAA",
            handler_ref="playlists.fila_musica",
            handler_snapshot={},
            state="armada",
            reason="",
            fires=0,
            max_fires=200,
            consecutive_errors=0,
            session_id=None,
            expires_at=agora + timedelta(hours=2),
            last_event_at=None,
            last_event_key="",
            enabled=True,
            created_at=agora,
            updated_at=agora,
        )
        base.update(kw)
        for k, v in base.items():
            setattr(self, k, v)


class FakeSubs:
    """SubscriptionRegistry em memória, com a mesma superfície usada pelo router."""

    def __init__(self, subs: list[Subscription] | None = None):
        self.items = {s.id: s for s in (subs or [])}
        self.deliveries_log: list[dict] = []
        self.created: list[dict] = []

    async def get(self, subscription_id):
        return self.items.get(subscription_id)

    async def list(self, *, user_id=None, device_id=None, channel=None, states=("armada",), limit=50):
        out = []
        for sub in self.items.values():
            if user_id is not None and sub.user_id != user_id:
                continue
            if device_id and sub.device_id != device_id:
                continue
            if channel and sub.channel != channel:
                continue
            if states and sub.state not in states:
                continue
            out.append(sub)
        return out

    async def live_for_event(self, *, device_id, channel, event, target):
        for sub in await self.list(device_id=device_id, channel=channel, states=("armada",)):
            if sub.is_live() and sub.matches(event=event, target=target):
                return sub
        return None

    async def update(self, subscription_id, **fields):
        sub = self.items.get(subscription_id)
        for key, value in fields.items():
            setattr(sub, key, value)
        return sub

    async def end(self, subscription_id, reason):
        return await self.update(subscription_id, state="encerrada", reason=reason)

    async def pause(self, subscription_id, reason):
        return await self.update(subscription_id, state="pausada", reason=reason)

    async def end_many(self, *, user_id=None, channel=None, device_id=None, handler_ref=None, reason=""):
        count = 0
        for sub in list(self.items.values()):
            if handler_ref and sub.handler_ref != handler_ref:
                continue
            if sub.state == "encerrada":
                continue
            sub.state = "encerrada"
            sub.reason = reason
            count += 1
        return count

    async def create(self, **kw):
        self.created.append(kw)
        row = _Row(
            user_id=kw.get("user_id"),
            device_id=kw.get("device_id"),
            channel=kw.get("channel"),
            events=kw.get("events"),
            target=kw.get("target"),
            expected_ref=kw.get("expected_ref"),
            handler_ref=kw.get("handler_ref"),
        )
        sub = Subscription(row)
        self.items[sub.id] = sub
        return sub

    async def log_delivery(self, **kw):
        self.deliveries_log.append(kw)


class FakeLibrary:
    """SkillLibrary reduzida ao que o router usa: get(name).eventos."""

    def __init__(self, eventos):
        self._eventos = eventos

    class _Entry:
        def __init__(self, name, eventos):
            self.name = name
            self.eventos = eventos

    def get(self, name):
        return self._Entry(name, self._eventos) if name == "playlists" else None

    def all(self):
        return [self._Entry("playlists", self._eventos)]


class FakeDatasetService:
    """Só o suficiente para o runner: list_rows + update_row sobre uma lista."""

    def __init__(self, rows):
        self.rows = rows
        self.updates: list[tuple] = []

    async def list_rows(self, dataset, *, user_id, filtro=None, ordem=None, limite=25, **kw):
        filtro = filtro or {}
        matched = [
            r for r in self.rows
            if all(str(r.get(k)) == str(v) for k, v in filtro.items())
        ]
        matched.sort(key=lambda r: r.get("posicao") or 0)
        return {"linhas": matched[:limite], "total": len(matched)}

    async def update_row(self, dataset, *, user_id, row_id, campos, actor=""):
        self.updates.append((str(row_id), campos))
        for row in self.rows:
            if str(row["id"]) == str(row_id):
                row.update(campos)
        return {"ok": True}


class FakeRegistry:
    async def resolve(self, slug, *, user_id=None):
        return object()


def _router(subs, *, service=None, eventos=(TRIGGER_RAW,)):
    return EventRouter(
        db=None,
        registry=subs,
        skill_library=FakeLibrary(list(eventos)),
        dataset_registry=FakeRegistry(),
        dataset_service_factory=(lambda: service) if service else (lambda: None),
    )


# ---------------------------------------------------------------------------
# Contrato do `eventos:` do SKILL.md
# ---------------------------------------------------------------------------


def test_trigger_valido():
    trigger = parse_triggers([TRIGGER_RAW], skill="playlists")[0]
    assert trigger.nome == "fila_musica"
    assert trigger.accepts("faixa_terminou")
    assert not trigger.accepts("porta_abriu")


@pytest.mark.parametrize(
    "mudanca, trecho",
    [
        ({"faz": "roda_qualquer_coisa"}, "faz"),
        ({"quando": ["fim_da_musica"]}, "desconhecido"),
        ({"canal": "email"}, "canal"),
        ({"fila": ""}, "fila"),
        ({"marca": {}}, "campo_estado"),
    ],
)
def test_trigger_invalido_ensina_o_erro(mudanca, trecho):
    """`faz:` só aceita runner que existe — é o que mantém a skill fora do
    território de executar código arbitrário dentro do neo-core."""
    with pytest.raises(TriggerError) as exc:
        parse_triggers([{**TRIGGER_RAW, **mudanca}], skill="playlists")
    assert trecho in str(exc.value)


def test_event_tools_sao_tres():
    assert EVENT_TOOL_NAMES == {"event_subscribe", "event_unsubscribe", "event_list"}


# ---------------------------------------------------------------------------
# A guarda que o Daniel pediu: pedido avulso não dispara nada
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sem_inscricao_o_evento_morre_no_lugar():
    """'toca um AC/DC' → nenhuma inscrição → nada acontece e nada é logado."""
    subs = FakeSubs([])
    resposta = await _router(subs).handle(
        {"device_id": "mac-daniel", "evento": "faixa_terminou", "site": "youtube",
         "tab_id": 7, "video_id": "ACDC123"}
    )
    assert resposta["acao"] == "nada"
    assert subs.deliveries_log == []  # nem trilha: senão vira ruído de toda faixa


@pytest.mark.asyncio
async def test_evento_de_outra_aba_nao_casa():
    sub = Subscription(_Row())
    subs = FakeSubs([sub])
    resposta = await _router(subs).handle(
        {"device_id": "mac-daniel", "evento": "faixa_terminou", "site": "youtube",
         "tab_id": 99, "video_id": "AAA"}
    )
    assert resposta["acao"] == "nada"
    assert sub.state == "armada"  # não pausa: o evento simplesmente não é dela


@pytest.mark.asyncio
async def test_faixa_inesperada_pausa_a_inscricao():
    """O usuário trocou a música na mão — a fila não sequestra o player."""
    sub = Subscription(_Row(expected_ref="AAA"))
    subs = FakeSubs([sub])
    resposta = await _router(subs).handle(
        {"device_id": "mac-daniel", "evento": "faixa_terminou", "site": "youtube",
         "tab_id": 42, "video_id": "OUTRA"}
    )
    assert resposta["acao"] == "nada"
    assert sub.state == "pausada"


@pytest.mark.asyncio
async def test_evento_repetido_nao_avanca_a_fila():
    """Reconnect da extensão reenvia o snapshot; sem dedup a fila pula faixas."""
    sub = Subscription(_Row(expected_ref="AAA", last_event_key="42:AAA"))
    subs = FakeSubs([sub])
    service = FakeDatasetService([
        {"id": uuid.uuid4(), "musica_titulo": "B", "link": "https://youtu.be/BBBBBBBBBBB",
         "estado": "pendente", "posicao": 2},
    ])
    resposta = await _router(subs, service=service).handle(
        {"device_id": "mac-daniel", "evento": "faixa_terminou", "site": "youtube",
         "tab_id": 42, "video_id": "AAA"}
    )
    assert resposta["acao"] == "nada"
    assert service.updates == []  # nada foi marcado


# ---------------------------------------------------------------------------
# O caminho feliz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_despacha_a_proxima_e_rearma():
    tocando_id, proxima_id = uuid.uuid4(), uuid.uuid4()
    service = FakeDatasetService([
        {"id": tocando_id, "musica_titulo": "A", "link": "https://youtu.be/AAAAAAAAAAA",
         "estado": "tocando", "posicao": 1},
        {"id": proxima_id, "musica_titulo": "B", "link": "https://youtu.be/BBBBBBBBBBB",
         "estado": "pendente", "posicao": 2},
    ])
    sub = Subscription(_Row(expected_ref="AAAAAAAAAAA"))
    subs = FakeSubs([sub])

    resposta = await _router(subs, service=service).handle(
        {"device_id": "mac-daniel", "evento": "faixa_terminando", "site": "youtube",
         "tab_id": 42, "video_id": "AAAAAAAAAAA"}
    )

    assert resposta["acao"] == "tocar"
    assert resposta["video_id"] == "BBBBBBBBBBB"
    assert resposta["tab_id"] == 42
    assert resposta["rearmar"]["ref"] == "BBBBBBBBBBB"
    # A que acabou vira "tocada"; a nova, "tocando" — antes de responder.
    estados = {str(r["id"]): r["estado"] for r in service.rows}
    assert estados[str(tocando_id)] == "tocada"
    assert estados[str(proxima_id)] == "tocando"
    # E a inscrição passa a esperar a faixa nova.
    assert sub.expected_ref == "BBBBBBBBBBB"
    assert sub.fires == 1
    assert subs.deliveries_log[-1]["outcome"] == "despachado"


@pytest.mark.asyncio
async def test_fila_vazia_encerra_e_manda_desarmar():
    service = FakeDatasetService([])
    sub = Subscription(_Row(expected_ref="AAA"))
    subs = FakeSubs([sub])
    resposta = await _router(subs, service=service).handle(
        {"device_id": "mac-daniel", "evento": "faixa_terminou", "site": "youtube",
         "tab_id": 42, "video_id": "AAA"}
    )
    assert resposta["acao"] == "desarmar"
    assert sub.state == "encerrada"


@pytest.mark.asyncio
async def test_linha_sem_link_e_pulada_e_marcada():
    """Linha sem link tocável não pode travar a fila nem voltar na rodada seguinte."""
    sem_link, com_link = uuid.uuid4(), uuid.uuid4()
    service = FakeDatasetService([
        {"id": sem_link, "musica_titulo": "sem link", "link": "", "estado": "pendente", "posicao": 1},
        {"id": com_link, "musica_titulo": "B", "link": "https://youtu.be/BBBBBBBBBBB",
         "estado": "pendente", "posicao": 2},
    ])
    trigger = EventTrigger.model_validate(TRIGGER_RAW)
    ctx = RunnerContext(
        trigger=trigger,
        subscription=Subscription(_Row()),
        event={},
        user_id=USER,
        db=None,
        dataset_registry=FakeRegistry(),
        dataset_service=service,
    )
    resultado = await proxima_da_fila(ctx)
    assert resultado.action["video_id"] == "BBBBBBBBBBB"
    assert [r["estado"] for r in service.rows if r["id"] == sem_link] == ["tocada"]


@pytest.mark.asyncio
async def test_teto_de_disparos_encerra():
    sub = Subscription(_Row(fires=200, max_fires=200))
    subs = FakeSubs([sub])
    resposta = await _router(subs).handle(
        {"device_id": "mac-daniel", "evento": "faixa_terminou", "site": "youtube",
         "tab_id": 42, "video_id": "AAA"}
    )
    assert resposta["acao"] == "desarmar"
    assert sub.state == "encerrada"


@pytest.mark.asyncio
async def test_inscricao_expirada_nao_dispara():
    sub = Subscription(_Row(expires_at=datetime.utcnow() - timedelta(minutes=1)))
    subs = FakeSubs([sub])
    resposta = await _router(subs).handle(
        {"device_id": "mac-daniel", "evento": "faixa_terminou", "site": "youtube",
         "tab_id": 42, "video_id": "AAA"}
    )
    assert resposta["acao"] == "nada"


@pytest.mark.asyncio
async def test_gatilho_removido_do_skill_md_encerra_a_inscricao():
    """Receita resolvida do arquivo AGORA: skill apagada não dispara receita velha."""
    sub = Subscription(_Row())
    subs = FakeSubs([sub])
    resposta = await _router(subs, eventos=()).handle(
        {"device_id": "mac-daniel", "evento": "faixa_terminou", "site": "youtube",
         "tab_id": 42, "video_id": "AAA"}
    )
    assert resposta["acao"] == "desarmar"
    assert sub.state == "encerrada"


# ---------------------------------------------------------------------------
# event_subscribe: a recusa que impede inscrição no vazio
# ---------------------------------------------------------------------------


def _executor(subs, media_context=None, device_id="mac-daniel"):
    ex = EventsExecutor(db=None, registry=subs, router=_router(subs))
    ex.set_context(USER, session_id=None, device_id=device_id, media_context=media_context or {})
    return ex


@pytest.mark.asyncio
async def test_subscribe_sem_alvo_recusa():
    with pytest.raises(ValueError) as exc:
        await _executor(FakeSubs([])).execute("event_subscribe", {"gatilho": "playlists.fila_musica"})
    assert "vazio" in str(exc.value)


@pytest.mark.asyncio
async def test_subscribe_herda_alvo_da_aba_que_o_neo_tocou():
    """O modelo não precisa (nem deve) inventar tabId: ele vem do media_context."""
    subs = FakeSubs([])
    media = {"browser_tabs": [
        {"site": "youtube", "tabId": 42, "videoId": "AAAAAAAAAAA", "playing": True},
    ]}
    resultado = await _executor(subs, media).execute(
        "event_subscribe", {"gatilho": "playlists.fila_musica"}
    )
    assert resultado["ok"] is True
    assert resultado["conferido"]["alvo"] == {"site": "youtube", "tab_id": 42}
    assert resultado["conferido"]["faixa_esperada"] == "AAAAAAAAAAA"


@pytest.mark.asyncio
async def test_subscribe_gatilho_inexistente_lista_os_validos():
    subs = FakeSubs([])
    media = {"browser_tabs": [{"site": "youtube", "tabId": 42, "videoId": "AAA", "playing": True}]}
    with pytest.raises(ValueError) as exc:
        await _executor(subs, media).execute("event_subscribe", {"gatilho": "playlists.inventado"})
    assert "playlists.fila_musica" in str(exc.value)


@pytest.mark.asyncio
async def test_reinscrever_substitui_em_vez_de_acumular():
    """Duas inscrições vivas no mesmo gatilho avançariam a fila duas vezes."""
    subs = FakeSubs([])
    media = {"browser_tabs": [{"site": "youtube", "tabId": 42, "videoId": "AAA", "playing": True}]}
    ex = _executor(subs, media)
    await ex.execute("event_subscribe", {"gatilho": "playlists.fila_musica"})
    segundo = await ex.execute("event_subscribe", {"gatilho": "playlists.fila_musica"})
    assert segundo["substituidas"] == 1
    vivas = [s for s in subs.items.values() if s.state == "armada"]
    assert len(vivas) == 1


@pytest.mark.asyncio
async def test_unsubscribe_sem_argumento_encerra_tudo():
    subs = FakeSubs([Subscription(_Row()), Subscription(_Row())])
    resultado = await _executor(subs).execute("event_unsubscribe", {})
    assert resultado["encerradas"] == 2


@pytest.mark.asyncio
async def test_subscribe_aceita_link_e_guarda_o_id_canonico():
    """A fila guarda URL; a extensão reporta videoId. Sem normalizar, a PRIMEIRA
    faixa seria 'inesperada' e a inscrição se pausaria sozinha."""
    subs = FakeSubs([])
    resultado = await _executor(subs).execute(
        "event_subscribe",
        {"gatilho": "playlists.fila_musica", "faixa": "https://youtu.be/AAAAAAAAAAA"},
    )
    assert resultado["conferido"]["faixa_esperada"] == "AAAAAAAAAAA"
    assert resultado["conferido"]["alvo"] == {}  # a aba entra depois, pelo `observar`


@pytest.mark.asyncio
async def test_subscribe_sem_device_recusa():
    """Sem aparelho o evento do fim da faixa não teria como casar."""
    with pytest.raises(ValueError) as exc:
        await _executor(FakeSubs([]), device_id="").execute(
            "event_subscribe", {"gatilho": "playlists.fila_musica", "faixa": "AAA"}
        )
    assert "aparelho" in str(exc.value)
