"""`save_generated_file` com NOME no lugar do id — resolução contra o catálogo.

Medido na session-b407c213: "salva em algum local na minha maquina e exibe pelo
chat". O classificador preencheu ``file_id='portifolio-naruto.zip'`` — o nome, que é
o que ele vê no histórico, porque o UUID só existe depois do `file_save` do cognitivo.
A trava de id-não-fundamentado barrou (certo: o id era inválido) e o turno terminou em
"peça de novo que eu gero" — com o zip real, `f17db260…`, sentado no catálogo havia
4 minutos.

A resolução por nome não relaxa a trava: o arquivo continua tendo que existir de
verdade. Só deixa de exigir que o classificador adivinhe um UUID.
"""

from datetime import datetime
from uuid import uuid4

import pytest

from modules.desktop.ai_service import AIService


class _FakeStored:
    def __init__(self, name, created_at, session_id=None, size_bytes=10):
        self.id = uuid4()
        self.name = name
        self.created_at = created_at
        self.session_id = session_id
        self.size_bytes = size_bytes


class _FakeStore:
    def __init__(self, files):
        self._files = files
        self.queries = []

    async def list_files(self, *, q=None, limit=50):
        self.queries.append(q)
        return [f for f in self._files if q and q.lower() in (f.name or "").lower()]


def _svc(files):
    svc = object.__new__(AIService)
    svc.file_store = _FakeStore(files)
    return svc


def _dt(minute):
    return datetime(2026, 8, 9, 3, minute, 0)


@pytest.mark.asyncio
async def test_resolve_o_caso_medido():
    zipf = _FakeStored("portifolio-naruto.zip", _dt(15), size_bytes=4435379)
    svc = _svc([zipf])

    got = await svc._resolve_file_by_name("portifolio-naruto.zip", None)

    assert got is zipf


@pytest.mark.asyncio
async def test_nome_duplicado_pega_o_mais_recente_da_sessao():
    """Gerar o mesmo zip duas vezes é rotina. Entregar a versão errada é pior que
    não entregar, então o desempate é explícito: sessão atual, depois recência."""
    sid = uuid4()
    velho_desta_sessao = _FakeStored("portifolio-naruto.zip", _dt(10), session_id=sid)
    novo_desta_sessao = _FakeStored("portifolio-naruto.zip", _dt(15), session_id=sid)
    mais_novo_de_outra = _FakeStored("portifolio-naruto.zip", _dt(20), session_id=uuid4())
    svc = _svc([velho_desta_sessao, mais_novo_de_outra, novo_desta_sessao])

    got = await svc._resolve_file_by_name("portifolio-naruto.zip", sid)

    assert got is novo_desta_sessao


@pytest.mark.asyncio
async def test_sem_sessao_casada_cai_para_o_mais_recente():
    a = _FakeStored("relatorio.pdf", _dt(10), session_id=uuid4())
    b = _FakeStored("relatorio.pdf", _dt(30), session_id=uuid4())
    svc = _svc([a, b])

    assert await svc._resolve_file_by_name("relatorio.pdf", uuid4()) is b


@pytest.mark.asyncio
async def test_casamento_parcial_nao_resolve():
    """`ilike %x%` traz "portifolio-naruto-v2.zip" para a busca de
    "portifolio-naruto.zip". Entregar esse seria entregar o arquivo ERRADO."""
    outro = _FakeStored("portifolio-naruto-v2.zip", _dt(20))
    svc = _svc([outro])

    assert await svc._resolve_file_by_name("portifolio-naruto.zip", None) is None


@pytest.mark.asyncio
async def test_uuid_inexistente_continua_barrado():
    """Id com forma de UUID que não está no catálogo é alucinação — a trava
    original (sessão bd7a304d) tem que continuar fechando nele."""
    svc = _svc([_FakeStored("x.zip", _dt(10))])

    assert await svc._resolve_file_by_name("bd7a304d-0000-4000-8000-000000000000", None) is None
    assert svc.file_store.queries == []   # nem foi ao catálogo


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", ["", "   ", "/workspace/portifolio-naruto.zip"])
async def test_entradas_que_nao_sao_nome_de_arquivo(candidate):
    svc = _svc([_FakeStored("portifolio-naruto.zip", _dt(15))])

    assert await svc._resolve_file_by_name(candidate, None) is None


@pytest.mark.asyncio
async def test_aspas_do_modelo_sao_toleradas():
    zipf = _FakeStored("portifolio-naruto.zip", _dt(15))
    svc = _svc([zipf])

    assert await svc._resolve_file_by_name("'portifolio-naruto.zip'", None) is zipf


@pytest.mark.asyncio
async def test_catalogo_quebrado_nao_derruba_o_turno():
    svc = object.__new__(AIService)

    class _Boom:
        async def list_files(self, **kw):
            raise RuntimeError("db down")

    svc.file_store = _Boom()

    assert await svc._resolve_file_by_name("x.zip", None) is None
