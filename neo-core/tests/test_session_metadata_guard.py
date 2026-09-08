"""`session_metadata` torto não pode derrubar a conversa (2026-09-08).

O que aconteceu em produção: a coluna JSONB estava com o **null do JSON** (não o
NULL do SQL, que o COALESCE cobria), e `'null'::jsonb || '{...}'::jsonb` no
Postgres devolve um **array** — `[null, {...}]`. A partir daí TODO turno daquela
sessão morria em `'list' object has no attribute 'get'`, porque cada mensagem
relia a mesma linha. A conversa ficava inutilizável.

Duas defesas, e as duas precisam de teste:
1. o merge no banco só concatena quando o lado esquerdo é objeto;
2. quem LÊ trata qualquer coisa que não seja dict como `{}` — perde-se um
   detalhe, não a conversa.
"""

from types import SimpleNamespace

from modules.desktop.ai_service import _session_meta


def test_metadata_dict_passa_intacto():
    sess = SimpleNamespace(extra_metadata={"pending_clarification": {"round": 2}})
    assert _session_meta(sess)["pending_clarification"]["round"] == 2


def test_metadata_lista_vira_dicionario_vazio():
    """A forma exata que veio do banco: [null, {...}] depois do || do jsonb."""
    sess = SimpleNamespace(extra_metadata=[None, {"music_neo_played": ["abc"]}])
    assert _session_meta(sess) == {}
    # E o uso real não levanta — era isto que derrubava o turno.
    assert _session_meta(sess).get("pending_clarification") is None


def test_metadata_ausente_ou_nula():
    assert _session_meta(SimpleNamespace(extra_metadata=None)) == {}
    assert _session_meta(SimpleNamespace()) == {}
    assert _session_meta(SimpleNamespace(extra_metadata="texto solto")) == {}


def test_merge_so_concatena_sobre_objeto():
    """O SQL do merge precisa do CASE: COALESCE sozinho não cobre o null JSON."""
    import inspect

    from db.async_client import AsyncPostgresClient

    sql = inspect.getsource(AsyncPostgresClient.merge_session_metadata)
    assert "jsonb_typeof(session_metadata) = 'object'" in sql
