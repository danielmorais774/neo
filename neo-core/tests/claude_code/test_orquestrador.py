"""O orquestrador contra um corpo de mentira que devolve rodadas REAIS da F0.

O corpo falso fala o contrato do ``core/claude_code/corpo.py``: cada ``lancar``
consome a próxima fixture, e ``ler`` entrega o stream em pedaços, como o
``claude.read`` do desktop faria. É o mesmo teste que o corpo do servidor (F2)
vai ter que passar — se precisar mudar o orquestrador para ele, a interface
estava errada.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.claude_code.corpo import CorpoIndisponivel, Lancamento, Leitura, Saude
from core.claude_code.orquestrador import (
    RETOMAR,
    Decisao,
    Ganchos,
    Limites,
    Orquestrador,
    Tarefa,
    regra_de_permissao,
)

FIX = Path(__file__).parent / "fixtures"


class CorpoFalso:
    nome = "falso"

    def __init__(self, rodadas: list[str], *, saude: Saude | None = None, pedacos: int = 3) -> None:
        self.rodadas = [(FIX / r).read_text() if r.endswith(".jsonl") else r for r in rodadas]
        self._saude = saude or Saude(ok=True, logado=True, metodo="claude.ai")
        self.pedacos = pedacos
        self.lancados: list = []
        self.parados: list[str] = []
        self.limpos: list[str] = []
        self.streams: dict[str, str] = {}
        self.leituras: dict[str, int] = {}
        self.falhas_de_leitura: list[CorpoIndisponivel] = []

    async def saude(self) -> Saude:
        return self._saude

    async def lancar(self, pedido):
        self.lancados.append(pedido)
        self.streams[pedido.run_id] = self.rodadas.pop(0)
        return Lancamento(run_id=pedido.run_id, cwd=pedido.cwd or "/home/neo/proj", pid=123)

    async def ler(self, run_id: str, offset: int) -> Leitura:
        if self.falhas_de_leitura:
            raise self.falhas_de_leitura.pop(0)
        texto = self.streams[run_id]
        passo = max(1, len(texto) // self.pedacos + 1)
        pedaco = texto[offset: offset + passo]
        novo = offset + len(pedaco)
        self.leituras[run_id] = self.leituras.get(run_id, 0) + 1
        vivo = run_id not in self.parados and novo < len(texto)
        return Leitura(pedaco=pedaco, offset=novo, vivo=vivo)

    async def parar(self, run_id: str) -> None:
        self.parados.append(run_id)

    async def recolher(self, cwd, caminhos):
        return []

    async def limpar(self, run_id: str) -> None:
        self.limpos.append(run_id)

    def flags_de_permissao(self, liberadas):
        flags = ["--permission-mode", "acceptEdits", "--permission-prompts", "none"]
        return flags + (["--allowedTools", *liberadas] if liberadas else [])


class Relogio:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def dormir(self, s: float) -> None:
        self.t += s


def _orq(corpo, *, tarefa: Tarefa | None = None, estado: dict | None = None, supervisor=None,
         mensagens: list | None = None, limites: Limites | None = None):
    salvos: list[dict] = []
    notas: list[str] = []
    caixa = mensagens if mensagens is not None else []
    relogio = Relogio()

    async def salvar(e):
        salvos.append(dict(e))

    async def progresso(n):
        notas.append(n)

    async def ler_mensagens():
        return list(caixa)

    orq = Orquestrador(
        corpo=corpo,
        tarefa=tarefa or Tarefa(objetivo="criar hello.py e rodar", pasta="/home/neo/proj"),
        estado=estado if estado is not None else {"session_id": "sess-1", "tarefa_id": "t1"},
        ganchos=Ganchos(salvar=salvar, progresso=progresso, mensagens=ler_mensagens),
        supervisor=supervisor,
        limites=limites or Limites(intervalo_s=1.0, parado_s=60, rodada_s=600, fora_do_ar_s=30),
        tag="job12345",
        relogio=relogio,
        dormir=relogio.dormir,
    )
    return orq, salvos, notas, caixa


@pytest.mark.asyncio
async def test_rodada_concluida_vira_completed_e_limpa():
    corpo = CorpoFalso(["auto_concluido.jsonl"])
    orq, salvos, notas, _ = _orq(corpo)
    desfecho = await orq.executar()
    assert desfecho.status == "completed"
    assert "funcionaram" in desfecho.resumo
    assert desfecho.rodadas == 1
    assert corpo.limpos == ["job12345-r1"]
    flags = corpo.lancados[0].flags
    assert flags[flags.index("--session-id") + 1] == "sess-1"
    assert "--resume" not in flags
    assert flags[-4:] == ["--permission-mode", "acceptEdits", "--permission-prompts", "none"]
    assert "# Tarefa" in corpo.lancados[0].prompt
    assert corpo.lancados[0].confirmar["tarefa"] == "t1"
    assert any("escrevendo `hello.py`" in n for n in notas)
    assert salvos[-1]["run_id"] == ""


@pytest.mark.asyncio
async def test_recusa_vira_pausa_com_as_acoes_e_as_regras():
    corpo = CorpoFalso(["accept_negado.jsonl"])
    orq, *_ = _orq(corpo)
    desfecho = await orq.executar()
    assert desfecho.status == "waiting"
    assert "permissão" in desfecho.resumo
    regras = [regra_de_permissao(r) for r in desfecho.recusas]
    assert "Bash(python3 hello.py)" in regras
    # escrita fora da pasta não se libera daqui
    assert None in regras


@pytest.mark.asyncio
async def test_rodada_morta_e_retomada_pela_mesma_sessao():
    corpo = CorpoFalso(["morto_no_meio.jsonl", "retomada.jsonl"])
    orq, salvos, _, _ = _orq(corpo)
    desfecho = await orq.executar()
    assert desfecho.status == "completed"
    assert desfecho.rodadas == 2
    segunda = corpo.lancados[1]
    assert segunda.prompt == RETOMAR
    assert segunda.flags[segunda.flags.index("--resume") + 1] == "sess-1"
    assert salvos[-1]["retomadas"] == 1


@pytest.mark.asyncio
async def test_reatar_rodada_em_voo_nao_relanca():
    corpo = CorpoFalso([])
    corpo.streams["job12345-r1"] = (FIX / "auto_concluido.jsonl").read_text()
    estado = {"session_id": "sess-1", "sessao_iniciada": True, "rodada": 1, "run_id": "job12345-r1",
              "cwd": "/home/neo/proj"}
    orq, *_ = _orq(corpo, estado=estado)
    desfecho = await orq.executar()
    assert desfecho.status == "completed"
    assert corpo.lancados == []
    assert desfecho.rodadas == 1


@pytest.mark.asyncio
async def test_core_reiniciado_entre_rodadas_retoma_em_vez_de_repetir_a_tarefa():
    corpo = CorpoFalso(["retomada.jsonl"])
    estado = {"session_id": "sess-1", "sessao_iniciada": True, "rodada": 1, "run_id": "",
              "cwd": "/home/neo/proj"}
    orq, *_ = _orq(corpo, estado=estado)
    await orq.executar()
    assert corpo.lancados[0].prompt == RETOMAR


class SupervisorFixo:
    def __init__(self, *decisoes: Decisao) -> None:
        self.decisoes = list(decisoes)

    async def decidir(self, tarefa, fim, historico, rodada):
        return self.decisoes.pop(0)


@pytest.mark.asyncio
async def test_supervisor_pede_mais_uma_rodada():
    corpo = CorpoFalso(["auto_concluido.jsonl", "auto_concluido.jsonl"])
    sup = SupervisorFixo(Decisao("continuar", "adicione um teste"), Decisao("aceitar"))
    orq, *_ = _orq(corpo, supervisor=sup)
    desfecho = await orq.executar()
    assert desfecho.status == "completed"
    assert corpo.lancados[1].prompt == "adicione um teste"
    assert "--resume" in corpo.lancados[1].flags


@pytest.mark.asyncio
async def test_teto_de_rodadas_automaticas_pergunta_ao_daniel():
    corpo = CorpoFalso(["auto_concluido.jsonl"])
    sup = SupervisorFixo(Decisao("continuar", "mais coisa", motivo="faltam testes"))
    tarefa = Tarefa(objetivo="x", pasta="/home/neo/proj", max_rodadas=1)
    orq, *_ = _orq(corpo, supervisor=sup, tarefa=tarefa)
    desfecho = await orq.executar()
    assert desfecho.status == "waiting"
    assert "faltam testes" in desfecho.resumo


@pytest.mark.asyncio
async def test_mensagem_do_daniel_com_interromper_para_e_vira_proxima_rodada():
    corpo = CorpoFalso(["morto_no_meio.jsonl", "auto_concluido.jsonl"], pedacos=10)
    caixa = [{"texto": "use Postgres em vez de SQLite", "interromper": True}]
    orq, salvos, _, _ = _orq(corpo, mensagens=caixa)
    desfecho = await orq.executar()
    assert corpo.parados == ["job12345-r1"]
    assert "use Postgres em vez de SQLite" in corpo.lancados[1].prompt
    assert desfecho.status == "completed"
    assert salvos[-1]["rodadas_do_daniel"] == 1
    assert salvos[-1]["mensagens_lidas"] == 1


@pytest.mark.asyncio
async def test_sem_login_nao_lanca():
    corpo = CorpoFalso([], saude=Saude(ok=False, motivo="o Claude Code do Mac não está logado"))
    orq, *_ = _orq(corpo)
    desfecho = await orq.executar()
    assert desfecho.status == "failed"
    assert "logado" in desfecho.resumo
    assert corpo.lancados == []


@pytest.mark.asyncio
async def test_mac_fora_do_ar_espera_e_segue():
    corpo = CorpoFalso(["auto_concluido.jsonl"])
    corpo.falhas_de_leitura = [CorpoIndisponivel("offline", fora_do_ar=True)] * 3
    orq, _, notas, _ = _orq(corpo)
    desfecho = await orq.executar()
    assert desfecho.status == "completed"
    assert sum("saiu do ar" in n for n in notas) == 1


@pytest.mark.asyncio
async def test_mac_fora_do_ar_por_tempo_demais_pausa():
    corpo = CorpoFalso(["auto_concluido.jsonl"])
    corpo.falhas_de_leitura = [CorpoIndisponivel("offline", fora_do_ar=True)] * 20
    orq, *_ = _orq(corpo)
    desfecho = await orq.executar()
    assert desfecho.status == "waiting"
    assert "fora do ar" in desfecho.resumo


@pytest.mark.asyncio
async def test_cota_estourada_pausa_com_a_hora_da_volta():
    estouro = (
        '{"type":"system","subtype":"init","session_id":"sess-1"}\n'
        '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","unifiedWindows":'
        '{"five_hour":{"utilization":1.0,"resetsAt":1794079200}}}}\n'
        '{"type":"result","subtype":"error","is_error":true,"api_error_status":429,'
        '"result":"limit","session_id":"sess-1","num_turns":0}\n'
    )
    corpo = CorpoFalso([estouro])
    orq, *_ = _orq(corpo)
    desfecho = await orq.executar()
    assert desfecho.status == "waiting"
    assert "cota" in desfecho.resumo and "volta às" in desfecho.resumo


@pytest.mark.asyncio
async def test_rodada_parada_sem_sinal_e_interrompida():
    parado = '{"type":"system","subtype":"init","session_id":"sess-1"}\n'

    class CorpoParado(CorpoFalso):
        async def ler(self, run_id, offset):
            texto = self.streams[run_id]
            return Leitura(pedaco=texto[offset:], offset=len(texto), vivo=True)

    corpo = CorpoParado([parado])
    orq, *_ = _orq(corpo)
    desfecho = await orq.executar()
    assert desfecho.status == "timeout"
    assert corpo.parados == ["job12345-r1"]


@pytest.mark.asyncio
async def test_recusa_no_dialogo_do_mac_cancela_sem_erro():
    class CorpoRecusado(CorpoFalso):
        async def lancar(self, pedido):
            raise CorpoIndisponivel("RECUSADO: o Daniel recusou no Mac")

    orq, *_ = _orq(CorpoRecusado([]))
    desfecho = await orq.executar()
    assert desfecho.status == "cancelled"
    assert "recusou" in desfecho.resumo


def test_regra_de_permissao():
    from core.claude_code.stream import Recusa

    assert regra_de_permissao(Recusa("Bash", {"command": "npm  install"})) == "Bash(npm install)"
    assert regra_de_permissao(Recusa("Bash", {"command": "echo $(whoami)"})) is None
    assert regra_de_permissao(Recusa("WebFetch", {"url": "https://x"})) == "WebFetch"
    assert regra_de_permissao(Recusa("Write", {"file_path": "/etc/x"})) is None


@pytest.mark.asyncio
async def test_rodada_que_nao_comecou_nao_conta():
    """Job 40852ae7: o resumo dizia "1 rodada(s)" e nenhuma tinha rodado."""
    class CorpoSemConfirmacao(CorpoFalso):
        async def lancar(self, pedido):
            raise CorpoIndisponivel("ninguém permitiu no Mac em 3 minutos")

    orq, *_ = _orq(CorpoSemConfirmacao([]))
    desfecho = await orq.executar()
    assert desfecho.status == "failed"
    assert "ninguém permitiu no Mac" in desfecho.resumo
    assert desfecho.rodadas == 0
