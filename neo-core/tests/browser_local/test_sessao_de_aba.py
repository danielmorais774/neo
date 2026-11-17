"""Runner: qual aba usar, e o que fica registrado quando a tarefa termina.

A ponte entre um job e o seguinte. Sem ela o driver nunca recebe `tab_ref` e o
executor nunca acha o `pending` para autorizar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.jobs.runner_browser_local import _gravar_sessao, _resolver_aba


def _job(**kw):
    base = dict(id=uuid4(), user_id=uuid4(), spec_json={"task": "apaga o arquivo"},
                extra_metadata={}, kind="browser_local")
    base.update(kw)
    return SimpleNamespace(**base)


def _anterior(*, tab=77, device="device-1", minutos=1, jid=None):
    return SimpleNamespace(
        id=jid or uuid4(),
        kind="browser_local",
        finished_at=datetime.utcnow() - timedelta(minutes=minutos),
        extra_metadata={
            "browser_session": {"device_id": device, "tab_ref": tab,
                                "url": "https://drive.google.com", "pending": ""}
        },
    )


class _DB:
    def __init__(self, jobs=None):
        self.jobs = jobs or []
        self.patches = []

    async def list_agent_jobs(self, user_id, *, statuses=None, limit=20):
        return self.jobs

    async def update_agent_job(self, job_id, **campos):
        self.patches.append((job_id, campos))


# ── qual aba ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spec_manda_mais_que_o_historico():
    aba = await _resolver_aba(_DB([_anterior(tab=77)]), _job(), "device-1", {"tab_ref": 42})
    assert aba == 42, "continuidade explícita do cérebro ganha"


@pytest.mark.asyncio
async def test_herda_a_aba_da_ultima_tarefa_do_mesmo_device():
    aba = await _resolver_aba(_DB([_anterior(tab=77)]), _job(), "device-1", {})
    assert aba == 77


@pytest.mark.asyncio
async def test_aba_de_outro_device_nao_serve():
    """A aba é uma por máquina — herdar do aparelho errado é pior que nada."""
    aba = await _resolver_aba(_DB([_anterior(device="device-2")]), _job(), "device-1", {})
    assert aba is None


@pytest.mark.asyncio
async def test_aba_velha_e_descartada():
    """Passados 15 min o usuário quase certamente usou o navegador para outra
    coisa; aba nova é mais previsível que herdar estado desconhecido."""
    aba = await _resolver_aba(_DB([_anterior(minutos=30)]), _job(), "device-1", {})
    assert aba is None


@pytest.mark.asyncio
async def test_o_proprio_job_nao_conta_como_anterior():
    eu = _job()
    anterior = _anterior(jid=eu.id)
    assert await _resolver_aba(_DB([anterior]), eu, "device-1", {}) is None


@pytest.mark.asyncio
async def test_sem_historico_abre_aba_nova():
    assert await _resolver_aba(_DB([]), _job(), "device-1", {}) is None


@pytest.mark.asyncio
async def test_banco_fora_do_ar_abre_aba_nova():
    class _Morto(_DB):
        async def list_agent_jobs(self, *a, **kw):
            raise RuntimeError("banco fora")

    assert await _resolver_aba(_Morto(), _job(), "device-1", {}) is None


# ── o que fica registrado ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grava_aba_e_clique_pendente():
    db, job = _DB(), _job()
    await _gravar_sessao(db, job, "device-1", SimpleNamespace(
        tab_ref=77, url="https://drive.google.com/drive/trash",
        pending="Excluir definitivamente", status="needs_confirmation",
    ))
    _, campos = db.patches[0]
    sessao = campos["extra_metadata"]["browser_session"]
    assert sessao["tab_ref"] == 77
    assert sessao["pending"] == "Excluir definitivamente"
    assert sessao["device_id"] == "device-1"
    assert sessao["task"] == "apaga o arquivo", "a retomada reusa a tarefa original"


@pytest.mark.asyncio
async def test_tarefa_concluida_tambem_deixa_a_aba():
    """'qual o tamanho?' → 'agora apaga': a segunda continua na mesma aba."""
    db = _DB()
    await _gravar_sessao(db, _job(), "device-1", SimpleNamespace(
        tab_ref=88, url="https://drive.google.com", pending="", status="completed",
    ))
    assert db.patches[0][1]["extra_metadata"]["browser_session"]["tab_ref"] == 88


@pytest.mark.asyncio
async def test_sem_aba_nao_grava_nada():
    db = _DB()
    await _gravar_sessao(db, _job(), "device-1", SimpleNamespace(
        tab_ref=None, url="", pending="", status="failed"))
    assert db.patches == []


@pytest.mark.asyncio
async def test_metadata_existente_e_preservada():
    """`extra_metadata` carrega os prints — sobrescrever perderia as imagens."""
    db, job = _DB(), _job(extra_metadata={"images": ["file:abc"]})
    await _gravar_sessao(db, job, "device-1", SimpleNamespace(
        tab_ref=77, url="u", pending="", status="completed"))
    assert db.patches[0][1]["extra_metadata"]["images"] == ["file:abc"]


@pytest.mark.asyncio
async def test_falha_ao_gravar_nao_derruba_o_job():
    class _Morto(_DB):
        async def update_agent_job(self, *a, **kw):
            raise RuntimeError("banco fora")

    await _gravar_sessao(_Morto(), _job(), "device-1", SimpleNamespace(
        tab_ref=77, url="u", pending="", status="completed"))


@pytest.mark.asyncio
async def test_os_prints_nao_podem_apagar_a_sessao_de_aba():
    """O registro dos prints montava o `extra_metadata` dele a partir da cópia
    EM MEMÓRIA do job — que é de antes desta gravação. Resultado: o print
    apagava a sessão de aba.

    O efeito era invisível job a job e total no conjunto. Medido em produção em
    2026-10-01, nos 16 `browser_local` mais recentes: **todo** job com `images`
    tinha `browser_session` nulo, e todo job com `browser_session` não tinha
    `images`. Virou 100% quando o print do fim passou a existir sempre — e foi
    o que fez a tarefa seguinte abrir aba nova depois de o usuário dizer "na
    aba que já está aberta" (job 23cde731, que então chutou o domínio errado e
    bateu no login).

    Por isso `_gravar_sessao` DEVOLVE o metadata vivo: quem escreve depois parte
    dele.
    """
    db, job = _DB(), _job(extra_metadata={"origem": "chat"})

    metadata = await _gravar_sessao(db, job, "device-1", SimpleNamespace(
        tab_ref=42, url="https://amazon.com.br/pedido", status="failed", pending=""))

    assert metadata["browser_session"]["tab_ref"] == 42
    assert metadata["origem"] == "chat", "o que já existia não pode sumir"

    # É ESTE dicionário que o bloco dos prints usa como base. Com a cópia velha
    # do job, a sessão sumia aqui.
    terminal = {**metadata, "images": [{"file_id": "x"}]}
    assert "browser_session" in terminal, "o print apagou a sessão de aba de novo"
    assert terminal["browser_session"]["tab_ref"] == 42


@pytest.mark.asyncio
async def test_sem_aba_o_metadata_volta_intacto():
    """Sem `tab_ref` não há o que registrar — mas quem chamou ainda precisa do
    metadata para escrever os prints em cima."""
    db, job = _DB(), _job(extra_metadata={"origem": "chat"})

    metadata = await _gravar_sessao(db, job, "device-1", SimpleNamespace(
        tab_ref=None, url="", status="failed", pending=""))

    assert metadata == {"origem": "chat"}
    assert not db.patches, "não tinha o que gravar"
