"""Catálogo de LLM: semente fiel ao mapa antigo, vitrine do chat, router e CRUD.

O CRUD roda contra SQL de verdade (SQLite em memória, unique e cascade ativos)
através de um adaptador que dá à sessão síncrona a cara assíncrona que o store
usa — sem dependência nova. O router é montado com settings falsas: nenhuma
chamada de rede.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from core.llm import catalog, catalog_seed
from core.llm.catalog import Catalog, ModelSpec, ProviderSpec
from core.llm.compatible_provider import OpenAICompatibleProvider
from core.llm.router import LLMRouter
from db.models.llm_catalog import LLMModel, LLMProvider
from modules.llm_catalog import store
from modules.llm_catalog.store import CatalogError

FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _seed_catalog():
    catalog.install(catalog.build_seed_catalog())
    yield
    catalog.install(catalog.build_seed_catalog())


# ── semente ──────────────────────────────────────────────────────────────────


def test_semente_preserva_o_mapa_antigo():
    """Todo id que o router.py conhecia continua com o mesmo dono e a mesma visão."""
    for model, provider in catalog_seed.MODEL_TO_PROVIDER.items():
        assert catalog.provider_for_model(model) == provider, model
        assert catalog.supports_vision(model) is (model in catalog_seed.VISION_MODELS), model
    for provider, model in catalog_seed.DEFAULT_MODEL_BY_PROVIDER.items():
        assert catalog.default_model_for(provider) == model


def test_todo_modelo_do_seletor_existe_no_mapa():
    """A trava contra a divergência: o seletor oferecia um Claude que o core não conhecia."""
    for provider, models in catalog_seed.SELECTOR.items():
        for model_id, _label, _desc in models:
            assert catalog_seed.MODEL_TO_PROVIDER.get(model_id) == provider, model_id
    assert "claude-3-5-sonnet-20241022" not in catalog.current().model_by_id


def test_modelo_desconhecido_nao_tem_opiniao_sobre_visao():
    assert catalog.supports_vision("vendor/digitado-a-mao") is None


# ── vitrine do chat ──────────────────────────────────────────────────────────


def test_vitrine_so_tem_provider_com_chave_e_modelo_do_seletor():
    payload = catalog.selector_payload(["openrouter", "deepseek"], "openrouter")
    names = [p["name"] for p in payload["providers"]]
    assert names == ["deepseek", "openrouter"], "ordem do catálogo, sem quem não tem chave"
    ids = [m["id"] for m in payload["providers"][1]["models"]]
    assert ids[0] == "x-ai/grok-4.5"
    assert "deepseek/deepseek-v4-flash" not in ids, "id legado roteia mas não aparece"
    # Sem modelo pedido vale o default_model do provider, não o 1º (Grok 4.5, o caro).
    assert payload["default"] == {"provider": "openrouter", "model": "google/gemini-3.1-flash-lite"}


def test_vitrine_usa_o_modelo_do_cerebro_quando_esta_nela():
    pin = "deepseek/deepseek-v4.1-flash@fireworks"
    assert catalog.selector_payload(["openrouter"], "openrouter", pin)["default"] == {
        "provider": "openrouter", "model": pin,
    }
    # Modelo do cérebro fora da vitrine (id legado) → default_model do provider.
    legado = catalog.selector_payload(["openrouter"], "openrouter", "deepseek/deepseek-v4-flash")
    assert legado["default"]["model"] == "google/gemini-3.1-flash-lite"


def test_vitrine_cai_no_primeiro_quando_o_default_nao_tem_chave():
    payload = catalog.selector_payload(["groq"], "openrouter")
    assert payload["default"] == {"provider": "groq", "model": "llama-3.3-70b-versatile"}
    assert payload["providers"][0]["description"].startswith("Llama-3.3 70B, Llama-3.1 8B Fast")


def test_vitrine_vazia_sem_provider():
    assert catalog.selector_payload([], "openrouter") == {"providers": [], "default": None}


# ── router ───────────────────────────────────────────────────────────────────


def _settings(**keys):
    base = dict(
        default_provider="openrouter",
        default_model="",
        ollama_base_url="http://ollama:11434",
        openrouter_base_url="https://openrouter.ai/api/v1",
        neo_cred_key=FERNET_KEY,
    )
    for field in catalog.ENV_KEY_FIELD.values():
        base[field] = None
    base.update(keys)
    return SimpleNamespace(**base)


def _router(settings) -> LLMRouter:
    r = LLMRouter.__new__(LLMRouter)
    r.settings = settings
    r._providers = {}
    r._fingerprints = {}
    r.rebuild()
    return r


def test_router_so_registra_quem_tem_chave():
    r = _router(_settings(openrouter_api_key="sk-or-env"))
    assert set(r.list_available_providers()) == {"openrouter", "ollama"}
    assert r.get_provider("openrouter").api_key == "sk-or-env"


def test_chave_do_banco_vence_a_do_env_e_so_recria_quem_mudou():
    r = _router(_settings(openrouter_api_key="sk-or-env", deepseek_api_key="sk-ds"))
    deepseek_antes = r.get_provider("deepseek")
    r.rebuild({"openrouter": "sk-or-painel"})
    assert r.get_provider("openrouter").api_key == "sk-or-painel"
    assert r.get_provider("deepseek") is deepseek_antes, "config igual mantém a instância"


def _install_extra_provider(**kw):
    seed = catalog.build_seed_catalog()
    extra = ProviderSpec(position=99, **kw)
    catalog.install(
        Catalog(
            providers=seed.providers + (extra,),
            models=seed.models + (ModelSpec(id="vendor/modelo-x", provider=kw["name"]),),
            source="db",
        )
    )


def test_provider_cadastrado_compativel_com_openai():
    _install_extra_provider(
        name="together",
        label="Together",
        adapter="openai_compatible",
        base_url="https://api.together.xyz/v1",
        default_model="vendor/modelo-x",
    )
    r = _router(_settings())
    r.rebuild({"together": "tg-key"})
    provider = r.get_provider("together")
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "vendor/modelo-x"
    assert r.get_provider_for_model("vendor/modelo-x") is provider
    assert r.get_default_model("together") == "vendor/modelo-x"


def test_nome_de_fabrica_com_outro_adapter_nao_herda_a_chave_do_env():
    seed = catalog.build_seed_catalog()
    sequestrado = tuple(
        ProviderSpec(
            name="openai",
            label="OpenAI?",
            adapter="openai_compatible",
            base_url="https://outro.servidor/v1",
        )
        if p.name == "openai"
        else p
        for p in seed.providers
    )
    catalog.install(Catalog(providers=sequestrado, models=seed.models, source="db"))
    r = _router(_settings(openai_api_key="sk-openai-de-verdade"))
    # openai_compatible não exige chave: registra, mas com o enchimento — nunca a do .env.
    assert r.get_provider("openai").client.api_key != "sk-openai-de-verdade"


def test_provider_desativado_some_do_router():
    seed = catalog.build_seed_catalog()
    off = tuple(
        ProviderSpec(**{**p.__dict__, "enabled": False}) if p.name == "ollama" else p
        for p in seed.providers
    )
    catalog.install(Catalog(providers=off, models=seed.models, source="db"))
    r = _router(_settings())
    assert "ollama" not in r.list_available_providers()
    assert "ollama" not in catalog.known_providers()


# ── CRUD contra SQL de verdade ───────────────────────────────────────────────


class _AsyncShim:
    """A API assíncrona de sessão que o store usa, sobre uma sessão síncrona."""

    def __init__(self, session) -> None:
        self._s = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        self._s.close()

    def add(self, obj) -> None:
        self._s.add(obj)

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def get(self, model, pk):
        return self._s.get(model, pk)

    async def flush(self) -> None:
        self._s.flush()

    async def commit(self) -> None:
        self._s.commit()

    async def delete(self, obj) -> None:
        self._s.delete(obj)


class _SqliteDB:
    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        SQLModel.metadata.create_all(
            self.engine, tables=[LLMProvider.__table__, LLMModel.__table__]
        )
        self._factory = sessionmaker(self.engine, expire_on_commit=False)
        # Os slots (aba Modelos) e o classificador moram em `user_preference`.
        self.prefs: dict[tuple[str, str, str], str] = {}

    def get_session(self):
        return _AsyncShim(self._factory())

    async def get_preferences(self, *, user_id=None, service=None, category=None):
        return [
            SimpleNamespace(key=k, value=v)
            for (svc, cat, k), v in self.prefs.items()
            if svc == service and cat == category
        ]

    async def upsert_preference(self, *, service, category, key, value, user_id=None, source_message=None):
        self.prefs[(service, category, key)] = value


class _RouterStub:
    def __init__(self) -> None:
        self.keys: dict[str, str] | None = None

    def rebuild(self, keys) -> None:
        self.keys = dict(keys)

    def list_available_providers(self) -> list[str]:
        return list(self.keys or {})


@pytest.fixture
def env(monkeypatch):
    settings = _settings(openrouter_api_key="sk-or-env")
    monkeypatch.setattr(store, "get_settings", lambda: settings)
    uses: list[dict] = []

    async def _usage(_db):
        return list(uses)

    monkeypatch.setattr(store, "usage", _usage)
    db, router = _SqliteDB(), _RouterStub()
    return SimpleNamespace(db=db, router=router, uses=uses, settings=settings)


def _model_row(provider: str, model_id: str) -> str:
    for m in catalog.current().models_by_provider[provider]:
        if m.id == model_id:
            return m.row_id
    raise AssertionError(model_id)


@pytest.mark.asyncio
async def test_banco_vazio_recebe_a_semente(env):
    cat = await store.refresh(env.db, env.router)
    assert cat.source == "db"
    assert len(cat.providers) == len(catalog_seed.PROVIDERS)
    assert len(cat.models) == len(catalog.build_seed_catalog().models)
    assert catalog.provider_for_model("deepseek/deepseek-v4.1-flash@fireworks") == "openrouter"
    # Segunda subida não semeia de novo.
    await store.refresh(env.db, env.router)
    assert len(catalog.current().models) == len(cat.models)


@pytest.mark.asyncio
async def test_tabela_ausente_segue_com_a_semente_e_bloqueia_gravacao(env, monkeypatch):
    async def _boom(_db):
        raise RuntimeError('relation "llm_provider" does not exist')

    monkeypatch.setattr(store, "_read", _boom)
    cat = await store.refresh(env.db, env.router)
    assert cat.source == "seed" and env.router.keys is None
    with pytest.raises(CatalogError) as err:
        await store.create_provider(env.db, env.router, {"name": "x", "adapter": "openai"})
    assert err.value.status == 503 and "alembic upgrade head" in err.value.message


@pytest.mark.asyncio
async def test_chave_cifrada_chega_decifrada_no_router_e_nunca_no_payload(env):
    await store.refresh(env.db, env.router)
    await store.set_key(env.db, env.router, "openrouter", "sk-or-painel-1234")
    assert env.router.keys == {"openrouter": "sk-or-painel-1234"}
    with env.db._factory() as s:
        raw = s.get(LLMProvider, "openrouter").api_key_encrypted
    assert b"sk-or-painel" not in raw, "no banco só o ciphertext"
    payload = store.admin_payload(env.router, [])
    openrouter = next(p for p in payload["providers"] if p["name"] == "openrouter")
    assert openrouter["key"] == {
        "source": "db", "hint": "1234", "stored": True, "broken": False, "env_var": "OPENROUTER_API_KEY",
    }
    assert "sk-or-painel" not in str(payload)
    # Apagar a chave do banco devolve a vez ao .env.
    await store.set_key(env.db, env.router, "openrouter", None)
    payload = store.admin_payload(env.router, [])
    openrouter = next(p for p in payload["providers"] if p["name"] == "openrouter")
    assert openrouter["key"]["source"] == "env"


@pytest.mark.asyncio
async def test_chave_que_nao_decifra_e_sinalizada(env):
    await store.refresh(env.db, env.router)
    await store.set_key(env.db, env.router, "groq", "gsk-antiga-9999")
    env.settings.neo_cred_key = Fernet.generate_key().decode()  # chave do cofre trocada
    await store.refresh(env.db, env.router)
    assert "groq" not in env.router.keys
    groq = next(p for p in store.admin_payload(env.router, [])["providers"] if p["name"] == "groq")
    assert groq["key"]["broken"] is True and groq["key"]["source"] is None


@pytest.mark.asyncio
async def test_cadastra_provider_e_modelo_e_aparece_na_vitrine(env):
    await store.refresh(env.db, env.router)
    await store.create_provider(
        env.db, env.router,
        {"name": "together", "label": "Together", "adapter": "openai_compatible",
         "base_url": "https://api.together.xyz/v1"},
    )
    await store.create_model(
        env.db, env.router, "together",
        {"model_id": "meta/llama-x", "label": "Llama X", "in_selector": True, "vision": True},
    )
    await store.set_key(env.db, env.router, "together", "tg-12345678")
    assert catalog.provider_for_model("meta/llama-x") == "together"
    assert catalog.supports_vision("meta/llama-x") is True
    vitrine = catalog.selector_payload(env.router.list_available_providers(), "openrouter")
    together = next(p for p in vitrine["providers"] if p["name"] == "together")
    assert together["models"] == [
        {"id": "meta/llama-x", "label": "Llama X", "description": "", "vision": True}
    ]


@pytest.mark.asyncio
async def test_validacoes_de_cadastro(env):
    await store.refresh(env.db, env.router)
    with pytest.raises(CatalogError, match="exige um base_url"):
        await store.create_provider(env.db, env.router, {"name": "local", "adapter": "openai_compatible"})
    with pytest.raises(CatalogError, match="nome inválido"):
        await store.create_provider(env.db, env.router, {"name": "Com Espaço", "adapter": "openai"})
    with pytest.raises(CatalogError) as dup:
        await store.create_provider(env.db, env.router, {"name": "groq", "adapter": "groq"})
    assert dup.value.status == 409
    with pytest.raises(CatalogError, match="já está cadastrado no provider 'openai'"):
        await store.create_model(env.db, env.router, "groq", {"model_id": "gpt-4o"})
    with pytest.raises(CatalogError, match="não é um modelo do provider"):
        await store.update_provider(env.db, env.router, "groq", {"default_model": "gpt-4o"})


@pytest.mark.asyncio
async def test_nada_em_uso_e_apagado_nem_desligado(env):
    await store.refresh(env.db, env.router)
    env.uses.append({"label": "Cérebro — agente principal", "provider": "openrouter",
                     "model": "x-ai/grok-4.5"})
    with pytest.raises(CatalogError) as err:
        await store.delete_provider(env.db, env.router, "openrouter")
    assert err.value.status == 409 and "Cérebro" in err.value.message
    with pytest.raises(CatalogError):
        await store.update_provider(env.db, env.router, "openrouter", {"enabled": False})
    with pytest.raises(CatalogError):
        await store.delete_model(env.db, env.router, _model_row("openrouter", "x-ai/grok-4.5"))
    with pytest.raises(CatalogError, match="modelo padrão"):
        await store.delete_model(
            env.db, env.router, _model_row("openrouter", "google/gemini-3.1-flash-lite")
        )


@pytest.mark.asyncio
async def test_apagar_provider_leva_os_modelos(env):
    await store.refresh(env.db, env.router)
    await store.delete_provider(env.db, env.router, "groq")
    assert "groq" not in catalog.known_providers()
    assert catalog.provider_for_model("llama-3.1-8b-instant") is None
    with env.db._factory() as s:
        assert s.query(LLMModel).filter(LLMModel.provider == "groq").count() == 0


@pytest.fixture(autouse=True)
def _slots_sem_cache():
    from modules.intent_config import store as intent_store
    from modules.model_config import store as model_store

    model_store.invalidate_cache()
    intent_store.invalidate_cache()
    yield
    model_store.invalidate_cache()
    intent_store.invalidate_cache()


@pytest.mark.asyncio
async def test_corrigir_id_em_uso_leva_o_slot_junto(env):
    """O caso de 06/11: id do Hugging Face no slot do navegador → corrigir dava 409."""
    import json

    from modules.model_config import store as model_store

    await store.refresh(env.db, env.router)
    await store.create_model(env.db, env.router, "openrouter", {"model_id": "google/gemma-4-E4B-it"})
    await model_store.set_model_config(env.db, "browser_local", "openrouter", "google/gemma-4-E4B-it")
    env.uses.append({"label": "Navegador local — passo a passo", "provider": "openrouter",
                     "model": "google/gemma-4-E4B-it"})

    await store.update_model(env.db, env.router, _model_row("openrouter", "google/gemma-4-E4B-it"),
                             {"model_id": "google/gemma-4-26b-a4b-it"})

    assert catalog.provider_for_model("google/gemma-4-26b-a4b-it") == "openrouter"
    assert catalog.provider_for_model("google/gemma-4-E4B-it") is None
    slot = json.loads(env.db.prefs[("neo-core", "model_config", "browser_local")])
    assert slot == {"provider": "openrouter", "model": "google/gemma-4-26b-a4b-it"}
    assert (await model_store.get_model_config(env.db, "browser_local"))["model"] == "google/gemma-4-26b-a4b-it"


@pytest.mark.asyncio
async def test_reordenar_e_renomear_modelo(env):
    await store.refresh(env.db, env.router)
    ids = [m.row_id for m in catalog.current().models_by_provider["deepseek"]]
    await store.reorder_models(env.db, env.router, "deepseek", list(reversed(ids)))
    primeiro = catalog.current().models_by_provider["deepseek"][0]
    assert primeiro.row_id == ids[-1]
    # Renomear o padrão do provider leva o padrão junto — e os slots que estavam
    # no default `deepseek-flash` (sem linha no banco) ganham a linha com o novo.
    await store.update_model(env.db, env.router, _model_row("deepseek", "deepseek-flash"),
                             {"model_id": "deepseek-flash-2"})
    assert catalog.default_model_for("deepseek") == "deepseek-flash-2"
    from modules.model_config import store as model_store

    assert (await model_store.get_model_config(env.db, "memory_planner"))["model"] == "deepseek-flash-2"


# ── HTTP: as rotas reais, num app só com elas ────────────────────────────────


@pytest.fixture
def client(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import desktop as desktop_routes
    from api.routes import llm_catalog as routes

    llm = _router(env.settings)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.include_router(desktop_routes.router, prefix="/api/v1")
    routes.set_dependencies(env.db, llm)
    monkeypatch.setattr(desktop_routes, "_ai_service", SimpleNamespace(llm=llm))

    async def _brain(_db, role):
        assert role == "cognitive"
        return {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash@fireworks"}

    monkeypatch.setattr("modules.model_config.store.get_model_config", _brain)
    import asyncio

    asyncio.run(store.refresh(env.db, llm))
    return TestClient(app)


def test_http_cadastro_completo_ate_a_vitrine(client):
    cat = client.get("/api/v1/llm/catalog").json()
    assert cat["source"] == "db" and cat["cred_key"] is True
    assert {a["key"] for a in cat["adapters"]} >= {"openai_compatible", "openrouter"}

    r = client.post("/api/v1/llm/providers", json={
        "name": "local", "label": "vLLM da rede", "adapter": "openai_compatible",
        "base_url": "http://192.168.4.50:8001/v1",
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/v1/llm/providers/local/models", json={
        "model_id": "qwen3-32b", "label": "Qwen3 32B", "in_selector": True,
    })
    local = next(p for p in r.json()["providers"] if p["name"] == "local")
    assert local["available"] is True, "openai_compatible sem chave registra (servidor local)"
    assert local["key"]["source"] == "none"
    row_id = local["models"][0]["id"]

    r = client.put(f"/api/v1/llm/models/{row_id}", json={"description": "na GPU do PC gamer"})
    assert r.status_code == 200
    r = client.put("/api/v1/llm/providers/local", json={"default_model": "qwen3-32b"})
    assert next(p for p in r.json()["providers"] if p["name"] == "local")["default_model"] == "qwen3-32b"

    vitrine = client.get("/api/v1/desktop/model-catalog").json()
    local_chat = next(p for p in vitrine["providers"] if p["name"] == "local")
    assert local_chat["models"] == [
        {"id": "qwen3-32b", "label": "Qwen3 32B", "description": "na GPU do PC gamer", "vision": False}
    ]
    assert vitrine["default"] == {
        "provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash@fireworks",
    }, "sem escolha salva o chat começa no cérebro da aba Modelos"


def test_http_chave_entra_e_nao_sai(client):
    r = client.put("/api/v1/llm/providers/groq/key", json={"api_key": "gsk-segredo-abcd"})
    assert r.status_code == 200
    assert "gsk-segredo" not in r.text
    groq = next(p for p in r.json()["providers"] if p["name"] == "groq")
    assert groq["key"]["source"] == "db" and groq["key"]["hint"] == "abcd" and groq["available"]
    r = client.delete("/api/v1/llm/providers/groq/key")
    groq = next(p for p in r.json()["providers"] if p["name"] == "groq")
    assert groq["key"]["source"] is None and groq["available"] is False


def test_http_erros_viram_status(client, env):
    assert client.delete("/api/v1/llm/providers/nao-existe").status_code == 404
    assert client.post("/api/v1/llm/providers", json={"name": "x", "adapter": "?"}).status_code == 400
    env.uses.append({"label": "Cérebro", "provider": "openrouter", "model": "x-ai/grok-4.5"})
    r = client.delete("/api/v1/llm/providers/openrouter")
    assert r.status_code == 409 and "Cérebro" in r.json()["detail"]


@pytest.mark.asyncio
async def test_http_catalogo_le_o_banco_quando_a_migracao_chega_depois(env, monkeypatch):
    """Subiu sem as tabelas → semente; migrou → abrir a aba já pega o banco."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from api.routes import llm_catalog as routes

    real_read = store._read

    async def _sem_tabela(_db):
        raise RuntimeError('relation "llm_provider" does not exist')

    monkeypatch.setattr(store, "_read", _sem_tabela)
    llm = _router(env.settings)
    await store.refresh(env.db, llm)
    assert catalog.current().source == "seed"

    monkeypatch.setattr(store, "_read", real_read)  # "rodou o alembic upgrade head"
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    routes.set_dependencies(env.db, llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        body = (await http.get("/api/v1/llm/catalog")).json()
    assert body["source"] == "db" and body["error"] is None


def test_http_ordem_nao_e_confundida_com_nome(client):
    """`PUT /providers/order` vem antes de `PUT /providers/{name}` no router."""
    names = [p["name"] for p in client.get("/api/v1/llm/catalog").json()["providers"]]
    r = client.put("/api/v1/llm/providers/order", json={"ids": list(reversed(names))})
    assert r.status_code == 200
    assert [p["name"] for p in r.json()["providers"]] == list(reversed(names))
