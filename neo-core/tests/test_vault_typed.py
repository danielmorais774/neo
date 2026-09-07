"""Cofre tipado: o `kind` decide os campos, e o cérebro escolhe pela descrição.

Antes disto o tipo era decorativo — qualquer credencial era "um segredo solto",
usuário+senha não cabia no modelo e a `description` não era lida por ninguém.
Os testes cobrem as quatro costuras novas:

1. **spec por tipo** (`kinds.py`): obrigatórios, whitelist de campos, porta int;
2. **catálogo para o cérebro** (`vault_list`): mostra o que decide, nunca o segredo;
3. **cofre → browser** (`sensitive_data`): senha escopada por domínio, e RECUSA
   quando o domínio está em branco (senão a senha valeria em qualquer página);
4. **caixa de email**: resolução da credencial e os erros que explicam o conserto.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.credentials.browser import build_browser_credentials, normalize_domain
from core.credentials.kinds import (
    SPEC_BY_KIND,
    FieldError,
    describe,
    normalize_fields,
)
from core.credentials.tools import VaultToolExecutor
from core.mailbox.tools import MailboxToolExecutor
from shared import ToolCall


def _info(label, kind, fields, description=None):
    return SimpleNamespace(
        id=str(uuid4()), label=label, kind=kind, fields=fields,
        description=description, hint="1234",
    )


class _FakeStore:
    """Cofre em memória: `resolve` devolve (info, segredo) como o real."""

    def __init__(self, entries):
        self.entries = entries  # [(info, secret)]
        self.touched = []

    async def list(self):
        return [i for i, _ in self.entries]

    async def list_by_kind(self, kind):
        return [i for i, _ in self.entries if i.kind == kind]

    async def resolve(self, id_or_label):
        for info, secret in self.entries:
            if id_or_label in (info.label, info.id):
                return info, secret
        return None

    async def touch(self, _id):
        self.touched.append(_id)


# ── 1. spec por tipo ────────────────────────────────────────────────────────
def test_password_requires_username():
    with pytest.raises(FieldError) as e:
        normalize_fields("password", {"domain": "github.com"})
    assert "Usuário" in str(e.value)


def test_email_normalizes_port_to_int():
    out = normalize_fields(
        "email",
        {"address": " neo@gmail.com ", "imap_host": "imap.gmail.com", "imap_port": "993"},
    )
    assert out == {"address": "neo@gmail.com", "imap_host": "imap.gmail.com", "imap_port": 993}


def test_unknown_field_is_refused():
    """A whitelist é o que impede um segundo segredo de entrar EM CLARO."""
    with pytest.raises(FieldError) as e:
        normalize_fields("token", {"senha": "hunter2"})
    assert "não previsto" in str(e.value)


def test_model_kind_list_matches_the_spec():
    """A lista no model é repetida (model não importa de `core`: circular import
    com o alembic). Este teste é o que impede as duas de divergirem."""
    from db.models.credential import CREDENTIAL_KINDS as no_model

    assert set(no_model) == set(SPEC_BY_KIND)


def test_every_kind_has_a_spec_and_describes_itself():
    for kind, spec in SPEC_BY_KIND.items():
        assert spec.secret_label, f"{kind} sem rótulo de segredo"
        assert all(f.name and f.label for f in spec.fields)
    assert describe("password", {"username": "daniel"}) == "usuário: daniel"


# ── 2. catálogo para o cérebro ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_vault_list_shows_choice_material_never_the_secret():
    store = _FakeStore([
        (_info("github-pessoal", "password", {"username": "dan", "domain": "github.com"},
               "minha conta pessoal do GitHub"), "hunter2"),
        (_info("email-do-neo", "email", {"address": "neo@gmail.com"}, "email próprio do Neo"), "app-pass"),
    ])
    res = await VaultToolExecutor(store).execute_tool_call(
        ToolCall(id="1", name="vault_list", arguments={})
    )
    payload = res.result
    assert payload["count"] == 2
    blob = str(payload)
    assert "minha conta pessoal do GitHub" in blob  # a descrição É o insumo da escolha
    assert "dan" in blob and "neo@gmail.com" in blob
    assert "hunter2" not in blob and "app-pass" not in blob  # o segredo nunca


@pytest.mark.asyncio
async def test_vault_list_filters_by_kind_and_query():
    store = _FakeStore([
        (_info("github-pessoal", "password", {"username": "dan"}, "GitHub"), "s1"),
        (_info("email-do-neo", "email", {"address": "neo@gmail.com"}, "email"), "s2"),
    ])
    ex = VaultToolExecutor(store)
    por_tipo = await ex.execute_tool_call(ToolCall(id="1", name="vault_list", arguments={"kind": "email"}))
    assert [c["label"] for c in por_tipo.result["credentials"]] == ["email-do-neo"]
    por_texto = await ex.execute_tool_call(ToolCall(id="2", name="vault_list", arguments={"query": "github"}))
    assert [c["label"] for c in por_texto.result["credentials"]] == ["github-pessoal"]


# ── 3. cofre → browser ──────────────────────────────────────────────────────
def test_domain_normalization_covers_apex_and_subdomains():
    assert normalize_domain("github.com") == "*.github.com"
    assert normalize_domain("HTTPS://Github.com/login") == "https://github.com/login"
    assert normalize_domain("*.interno.local") == "*.interno.local"


@pytest.mark.asyncio
async def test_password_becomes_domain_scoped_sensitive_data():
    store = _FakeStore([
        (_info("github-pessoal", "password", {"username": "dan", "domain": "github.com"},
               "conta do GitHub"), "hunter2"),
    ])
    creds = await build_browser_credentials(store, ["github-pessoal"])
    assert creds.sensitive_data == {
        "*.github.com": {"github_pessoal_user": "dan", "github_pessoal_pass": "hunter2"}
    }
    # O subagente é instruído pelo PLACEHOLDER; o valor não aparece no contexto.
    nota = " ".join(creds.notes)
    assert "<secret>github_pessoal_pass</secret>" in nota
    assert "hunter2" not in nota
    assert not creds.problems


@pytest.mark.asyncio
async def test_password_without_domain_is_refused_with_the_fix():
    """Sem domínio a senha valeria em QUALQUER página — recusa explicada."""
    store = _FakeStore([(_info("banco", "password", {"username": "dan"}), "senha")])
    creds = await build_browser_credentials(store, ["banco"])
    assert creds.sensitive_data == {}
    assert "Domínio de uso" in " ".join(creds.problems)


@pytest.mark.asyncio
async def test_email_gives_address_in_clear_and_never_the_password():
    """Endereço é público (é o que se digita); a senha da caixa não vai ao browser."""
    store = _FakeStore([
        (_info("email-do-neo", "email", {"address": "neo@gmail.com"}, "email do Neo"), "app-pass"),
    ])
    creds = await build_browser_credentials(store, ["email-do-neo"])
    assert creds.sensitive_data == {}
    assert "neo@gmail.com" in " ".join(creds.notes)
    assert "app-pass" not in str(creds)


@pytest.mark.asyncio
async def test_token_is_not_for_forms_and_missing_label_says_so():
    store = _FakeStore([(_info("openrouter", "api_key", {"domain": "openrouter.ai"}), "sk-x")])
    creds = await build_browser_credentials(store, ["openrouter", "nao-existe"])
    assert creds.sensitive_data == {}
    problems = " ".join(creds.problems)
    assert "não serve para preencher formulário" in problems
    assert "não existe credencial 'nao-existe'" in problems


@pytest.mark.asyncio
async def test_no_vault_does_not_explode():
    creds = await build_browser_credentials(None, ["qualquer"])
    assert creds.sensitive_data == {} and creds.problems


# ── 4. caixa de email ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_email_inbox_resolves_the_single_mailbox(monkeypatch):
    store = _FakeStore([
        (_info("email-do-neo", "email",
               {"address": "neo@gmail.com", "imap_host": "imap.gmail.com", "imap_port": 993}), "app-pass"),
    ])
    chamadas = {}

    async def _fake_list_inbox(**kw):
        chamadas.update(kw)
        return [{"id": "42", "de": "site@x.com", "assunto": "Seu código: 998877",
                 "quando": "25/08 10:00", "previa": "código 998877", "nao_lida": True}]

    monkeypatch.setattr("core.mailbox.tools.list_inbox", _fake_list_inbox)
    res = await MailboxToolExecutor(store).execute_tool_call(
        ToolCall(id="1", name="email_inbox", arguments={"minutes": 5})
    )
    assert res.result["ok"] is True
    assert res.result["caixa"] == "neo@gmail.com"
    assert "998877" in str(res.result["mensagens"])
    # a senha de app viaja só até o cliente IMAP
    assert chamadas["password"] == "app-pass" and chamadas["minutes"] == 5
    assert store.touched  # marca uso, como o gateway faz


@pytest.mark.asyncio
async def test_email_inbox_without_mailbox_explains_how_to_fix():
    res = await MailboxToolExecutor(_FakeStore([])).execute_tool_call(
        ToolCall(id="1", name="email_inbox", arguments={})
    )
    assert res.result["ok"] is False
    assert "neo-panel" in res.result["error"]


@pytest.mark.asyncio
async def test_email_inbox_with_two_mailboxes_asks_which():
    store = _FakeStore([
        (_info("caixa-a", "email", {"address": "a@x.com", "imap_host": "h"}), "s1"),
        (_info("caixa-b", "email", {"address": "b@x.com", "imap_host": "h"}), "s2"),
    ])
    res = await MailboxToolExecutor(store).execute_tool_call(
        ToolCall(id="1", name="email_inbox", arguments={})
    )
    assert res.result["ok"] is False
    assert "caixa-a" in res.result["error"] and "caixa-b" in res.result["error"]


@pytest.mark.asyncio
async def test_email_tool_refuses_a_credential_of_another_kind():
    store = _FakeStore([(_info("github", "password", {"username": "dan"}), "s")])
    res = await MailboxToolExecutor(store).execute_tool_call(
        ToolCall(id="1", name="email_inbox", arguments={"credential": "github"})
    )
    assert res.result["ok"] is False and "não é uma caixa de email" in res.result["error"]


@pytest.mark.asyncio
async def test_incomplete_mailbox_points_at_the_missing_field():
    store = _FakeStore([(_info("meia-boca", "email", {"address": "a@x.com"}), "s")])
    res = await MailboxToolExecutor(store).execute_tool_call(
        ToolCall(id="1", name="email_inbox", arguments={"credential": "meia-boca"})
    )
    assert res.result["ok"] is False and "servidor IMAP" in res.result["error"]
