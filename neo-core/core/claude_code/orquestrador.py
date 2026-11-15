"""O orquestrador do Claude Code — um só, para os dois corpos.

Recebe uma tarefa, lança rodadas no corpo, acompanha o stream, e ao fim de cada
rodada decide: aceitar, continuar, perguntar ao Daniel ou desistir
(docs/claude-code-delegation-plan.md §2.2 e §6).

O estado inteiro mora num dict que o runner grava no ``spec_json`` do job a cada
mudança. É isso que deixa reatar depois de um reinício do core (salvar ``.py`` =
reiniciar): ``run_id`` preenchido quer dizer "rodada em voo", e quem volta relê o
stream do começo — ele é pequeno, e reler reconstrói as recusas e a cota sem ter
de guardá-las à parte.

Este arquivo não conhece banco, device nem LLMRouter: tudo entra por ganchos, e
os testes usam um corpo falso que devolve streams gravados na F0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from core.claude_code.corpo import Corpo, CorpoIndisponivel, PedidoDeRodada
from core.claude_code.stream import Cota, Fim, LeitorDeStream, Recusa

logger = logging.getLogger(__name__)

#: O relatório que toda rodada termina entregando (`--json-schema`). É o que
#: tira a decisão do fim da rodada do texto livre.
RELATORIO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["concluido", "precisa_de_voce", "travou"]},
        "resumo": {"type": "string"},
        "arquivos": {"type": "array", "items": {"type": "string"}},
        "pergunta": {"type": "string"},
    },
    "required": ["status", "resumo", "arquivos"],
}

CONTRATO = (
    "Você está trabalhando a pedido do Neo, o assistente pessoal do Daniel. O Neo "
    "acompanha esta sessão de fora: ele não vê a sua tela e o Daniel não está olhando "
    "o terminal. Trabalhe só na pasta atual. Se uma ação for negada por permissão, não "
    "insista nem tente contornar por outro caminho: siga com o resto e conte no resumo — "
    "o Neo pergunta ao Daniel e libera na rodada seguinte. Ao terminar, ou se precisar de "
    "uma decisão do Daniel, ou se travar, entregue o relatório estruturado: `status` "
    "(concluido, precisa_de_voce ou travou), `resumo` do que fez e do que falta, "
    "`arquivos` que criou ou mudou (caminhos relativos à pasta) e, se precisar dele, a "
    "`pergunta`."
)

RETOMAR = (
    "Você foi interrompido no meio da tarefa. Confira o que já existe nesta pasta e "
    "continue de onde parou, sem refazer o que já está feito."
)

#: Prefixo do erro do `claude.start` quando o Daniel clica em Recusar no Mac
#: (services/claude_cli.rs). É decisão dele, não falha.
RECUSADO = "RECUSADO:"


@dataclass
class Tarefa:
    objetivo: str
    contexto: str = ""
    entregaveis: list[str] = field(default_factory=list)
    pasta: str = ""
    modelo: str = "sonnet"
    esforco: str = ""
    max_rodadas: int = 3
    max_turnos: int = 60
    #: A primeira instrução, quando o job é continuação ("o Daniel respondeu…").
    instrucao_inicial: str = ""
    #: `allowedTools` liberadas pelo Daniel nesta tarefa.
    liberadas: list[str] = field(default_factory=list)
    titulo: str = ""


@dataclass
class Limites:
    intervalo_s: float = 3.0
    rodada_s: float = 3600.0
    parado_s: float = 900.0
    fora_do_ar_s: float = 6 * 3600.0
    max_retomadas: int = 2


@dataclass
class Decisao:
    acao: str  # aceitar | continuar | desistir
    instrucao: str = ""
    motivo: str = ""


@dataclass
class Desfecho:
    status: str  # completed | failed | waiting | timeout
    resumo: str
    relatorio: dict[str, Any] | None = None
    recusas: list[Recusa] = field(default_factory=list)
    pergunta: str = ""
    session_id: str = ""
    cwd: str = ""
    rodadas: int = 0
    custo_usd: float = 0.0
    cota: Cota | None = None

    @property
    def arquivos(self) -> list[str]:
        return [str(a) for a in (self.relatorio or {}).get("arquivos") or [] if str(a).strip()]


class Supervisor(Protocol):
    async def decidir(
        self, tarefa: Tarefa, fim: Fim, historico: list[dict], rodada: int
    ) -> Decisao: ...


@dataclass
class Ganchos:
    salvar: Callable[[dict], Awaitable[None]]
    progresso: Callable[[str], Awaitable[None]]
    #: Todas as mensagens do Daniel para esta tarefa, na ordem; o orquestrador
    #: guarda quantas já leu (`mensagens_lidas`) e nunca apaga nenhuma.
    mensagens: Callable[[], Awaitable[list[dict]]]


def regra_de_permissao(recusa: Recusa) -> str | None:
    """A `allowedTools` que libera ESTA ação e só ela. ``None`` = não se libera daqui.

    Bash vira a regra do comando exato. Escrita fora da pasta não se libera: no
    corpo local a pasta é a cerca que o Daniel confirmou no Mac.
    """
    if recusa.ferramenta == "Bash":
        comando = " ".join(str(recusa.entrada.get("command") or "").split())
        if not comando or "(" in comando or ")" in comando:
            return None
        return f"Bash({comando})"
    if recusa.ferramenta in ("WebFetch", "WebSearch"):
        return recusa.ferramenta
    return None


class Orquestrador:
    def __init__(
        self,
        *,
        corpo: Corpo,
        tarefa: Tarefa,
        estado: dict[str, Any],
        ganchos: Ganchos,
        supervisor: Supervisor | None = None,
        limites: Limites | None = None,
        tag: str = "",
        relogio: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.corpo = corpo
        self.tarefa = tarefa
        self.estado = estado
        self.ganchos = ganchos
        self.supervisor = supervisor
        self.limites = limites or Limites()
        self.tag = tag or "tarefa"
        self._agora = relogio
        self._dormir = dormir
        self.estado.setdefault("rodada", 0)
        self.estado.setdefault("historico", [])
        self.estado.setdefault("custo", 0.0)
        self.estado.setdefault("mensagens_lidas", 0)
        self.estado.setdefault("retomadas", 0)
        self.estado.setdefault("cwd", tarefa.pasta)

    @property
    def run_atual(self) -> str:
        return str(self.estado.get("run_id") or "")

    # ------------------------------------------------------------------

    async def executar(self) -> Desfecho:
        saude = await self.corpo.saude()
        if not saude.ok:
            return self._desfecho("failed", saude.motivo or "o Claude Code não está disponível")

        if not self.run_atual and not self.estado.get("proxima"):
            if int(self.estado["rodada"]) > 0:
                # O core reiniciou ENTRE duas rodadas, antes de gravar a próxima.
                self.estado["proxima"] = {"texto": RETOMAR}
            elif self.tarefa.instrucao_inicial:
                self.estado["proxima"] = {"texto": self.tarefa.instrucao_inicial, "do_daniel": True}
            else:
                self.estado["proxima"] = {"texto": self._prompt_inicial()}
        while True:
            if not self.run_atual:
                try:
                    await self._lancar(dict(self.estado.pop("proxima", None) or {"texto": RETOMAR}))
                except CorpoIndisponivel as exc:
                    if str(exc).startswith(RECUSADO):
                        return self._desfecho("cancelled", "Você recusou no Mac; o Claude não começou.")
                    return self._desfecho("failed", f"Não consegui começar a rodada do Claude: {exc}")
            motivo, leitor = await self._acompanhar()
            fim = leitor.fim
            run_id = self.run_atual
            self.estado["run_id"] = ""
            await self.ganchos.progresso(f"Claude: {_fim_da_rodada(self.estado['rodada'], motivo, leitor)}")
            if fim is not None:
                self.estado["custo"] = float(self.estado["custo"]) + fim.custo_usd
                self.estado["historico"].append({
                    "rodada": self.estado["rodada"],
                    "status": (fim.relatorio or {}).get("status") or ("erro" if fim.erro else "sem relatório"),
                    "resumo": str((fim.relatorio or {}).get("resumo") or fim.texto)[:300],
                })
            await self.ganchos.salvar(self.estado)
            await self.corpo.limpar(run_id)

            desfecho, proxima = await self._depois_da_rodada(motivo, leitor)
            if desfecho is not None:
                desfecho.cota = leitor.cota or desfecho.cota
                return desfecho
            self.estado["proxima"] = proxima
            await self.ganchos.salvar(self.estado)

    async def _lancar(self, proxima: dict[str, Any]) -> None:
        primeira = not self.estado.get("sessao_iniciada")
        self.estado["rodada"] = int(self.estado["rodada"]) + 1
        if proxima.get("do_daniel"):
            self.estado["rodadas_do_daniel"] = int(self.estado.get("rodadas_do_daniel") or 0) + 1
        run_id = f"{self.tag}-r{self.estado['rodada']}"
        prompt = str(proxima.get("texto") or "") or (self._prompt_inicial() if primeira else RETOMAR)
        pedido = PedidoDeRodada(
            run_id=run_id,
            cwd=str(self.estado.get("cwd") or self.tarefa.pasta),
            prompt=prompt,
            flags=self._flags(primeira),
            confirmar={
                "tarefa": str(self.estado.get("tarefa_id") or self.tag),
                "titulo": self.tarefa.titulo or self.tarefa.objetivo[:120],
                "objetivo": self.tarefa.objetivo[:600],
            },
        )
        try:
            lancamento = await self.corpo.lancar(pedido)
        except CorpoIndisponivel:
            # Não começou, então não conta: o resumo do job 40852ae7 dizia
            # "1 rodada(s)" sem nenhuma ter rodado.
            self.estado["rodada"] = int(self.estado["rodada"]) - 1
            if proxima.get("do_daniel"):
                self.estado["rodadas_do_daniel"] = int(self.estado.get("rodadas_do_daniel") or 0) - 1
            raise
        self.estado.update(
            run_id=run_id,
            cwd=lancamento.cwd or pedido.cwd,
            sessao_iniciada=True,
            instrucao=prompt[:2000],
        )
        await self.ganchos.salvar(self.estado)
        await self.ganchos.progresso(
            f"Claude: ▶ rodada {self.estado['rodada']} em {self.estado['cwd']} "
            f"({self.tarefa.modelo or 'sonnet'})"
        )

    def _flags(self, primeira: bool) -> list[str]:
        sessao = str(self.estado["session_id"])
        flags = [
            "-p", "--output-format", "stream-json", "--verbose",
            *(["--session-id", sessao] if primeira else ["--resume", sessao]),
            "--max-turns", str(self.tarefa.max_turnos),
            "--model", self.tarefa.modelo or "sonnet",
            "--json-schema", json.dumps(RELATORIO_SCHEMA, ensure_ascii=False),
            "--append-system-prompt", CONTRATO,
            "--strict-mcp-config",
        ]
        if self.tarefa.esforco:
            flags += ["--effort", self.tarefa.esforco]
        # Por último: `--allowedTools` é variádica e engoliria o que viesse depois.
        return flags + self.corpo.flags_de_permissao(list(self.tarefa.liberadas))

    def _prompt_inicial(self) -> str:
        partes = [f"# Tarefa\n\n{self.tarefa.objetivo.strip()}"]
        if self.tarefa.contexto.strip():
            partes.append(f"## Contexto (do Neo)\n\n{self.tarefa.contexto.strip()}")
        if self.tarefa.entregaveis:
            itens = "\n".join(f"- {e}" for e in self.tarefa.entregaveis if str(e).strip())
            partes.append(f"## Entregáveis\n\n{itens}")
        return "\n\n".join(partes)

    # ------------------------------------------------------------------

    async def _acompanhar(self) -> tuple[str, LeitorDeStream]:
        """Lê a rodada até o fim. Devolve o motivo da parada e o leitor."""
        leitor = LeitorDeStream(cwd=str(self.estado.get("cwd") or ""))
        run_id = self.run_atual
        offset = 0
        inicio = ultimo_byte = self._agora()
        fora_desde: float | None = None
        while True:
            try:
                leitura = await self.corpo.ler(run_id, offset)
            except CorpoIndisponivel as exc:
                if not exc.fora_do_ar:
                    logger.warning("[claude] leitura da rodada %s falhou: %s", run_id, exc)
                    return "corpo", leitor
                agora = self._agora()
                if fora_desde is None:
                    fora_desde = agora
                    await self.ganchos.progresso(
                        f"Claude: o Mac saiu do ar às {datetime.now():%H:%M}; espero voltar"
                    )
                if agora - fora_desde > self.limites.fora_do_ar_s:
                    return "fora_do_ar", leitor
                # O relógio da rodada não corre com o Mac dormindo: o processo
                # também está parado.
                inicio += self.limites.intervalo_s * 5
                ultimo_byte += self.limites.intervalo_s * 5
                await self._dormir(self.limites.intervalo_s * 5)
                continue
            fora_desde = None
            offset = leitura.offset
            for nota in leitor.alimentar(leitura.pedaco):
                await self.ganchos.progresso(f"Claude: {nota}")
            agora = self._agora()
            if leitura.pedaco:
                ultimo_byte = agora
            if leitor.fim is not None:
                return "fim", leitor
            if not leitura.vivo and not leitura.pedaco:
                return "morreu", leitor
            if agora - inicio > self.limites.rodada_s:
                await self._parar(run_id)
                return "tempo", leitor
            if agora - ultimo_byte > self.limites.parado_s:
                await self._parar(run_id)
                return "parado", leitor
            if any(m.get("interromper") for m in await self._mensagens_novas(consumir=False)):
                await self._parar(run_id)
                return "interrompido", leitor
            await self._dormir(self.limites.intervalo_s)

    async def _parar(self, run_id: str) -> None:
        try:
            await self.corpo.parar(run_id)
        except CorpoIndisponivel as exc:
            logger.warning("[claude] não consegui parar a rodada %s: %s", run_id, exc)

    async def _mensagens_novas(self, *, consumir: bool) -> list[dict]:
        todas = await self.ganchos.mensagens()
        lidas = int(self.estado.get("mensagens_lidas") or 0)
        novas = [m for m in todas[lidas:] if isinstance(m, dict) and str(m.get("texto") or "").strip()]
        if consumir:
            self.estado["mensagens_lidas"] = len(todas)
        return novas

    # ------------------------------------------------------------------

    async def _depois_da_rodada(
        self, motivo: str, leitor: LeitorDeStream
    ) -> tuple[Desfecho | None, dict[str, Any] | None]:
        """O que fazer agora. ``(desfecho, None)`` encerra; ``(None, próxima)`` segue."""
        fim = leitor.fim
        n = int(self.estado["rodada"])

        if motivo == "interrompido":
            novas = await self._mensagens_novas(consumir=True)
            await self.ganchos.salvar(self.estado)
            return None, {"texto": _do_daniel(novas), "do_daniel": True}
        if motivo == "fora_do_ar":
            return self._desfecho(
                "waiting",
                "O Mac ficou fora do ar por tempo demais no meio da tarefa do Claude. "
                "Quando ele voltar, peça para continuar.",
            ), None
        if motivo == "corpo":
            return self._desfecho("failed", "Perdi o contato com o Claude Code no Mac no meio da rodada."), None
        if motivo in ("tempo", "parado"):
            causa = "passou do tempo máximo da rodada" if motivo == "tempo" else "ficou parado sem sinal de vida"
            return self._desfecho("timeout", f"O Claude {causa} e eu parei a rodada {n}.", leitor=leitor), None
        if fim is None:
            # Morreu sem `result` (kill, reinício do Mac). A sessão está gravada no
            # disco dele, e `--resume` continua de onde parou (medido na F0).
            if int(self.estado["retomadas"]) >= self.limites.max_retomadas:
                return self._desfecho(
                    "failed", "O Claude parou no meio da rodada mais de uma vez e eu desisti de retomar."
                ), None
            self.estado["retomadas"] = int(self.estado["retomadas"]) + 1
            return None, {"texto": RETOMAR, "retomada": True}

        if fim.limite_atingido or (fim.erro and leitor.cota is not None and leitor.cota.estourou):
            volta = _hora(leitor.cota.reset_5h if leitor.cota else None)
            return self._desfecho(
                "waiting",
                f"A cota da sua assinatura do Claude acabou{f' — volta às {volta}' if volta else ''}. "
                "Quando voltar, peça para continuar.",
                fim=fim, leitor=leitor,
            ), None
        if fim.erro:
            if fim.erro_api in (401, 403):
                return self._desfecho(
                    "waiting",
                    "O login do Claude Code no Mac foi recusado. Rode `claude` no terminal, "
                    "faça `/login` e peça para continuar.",
                    fim=fim, leitor=leitor,
                ), None
            return self._desfecho("failed", f"A rodada {n} do Claude terminou em erro: {fim.texto[:500]}",
                                  fim=fim, leitor=leitor), None

        # Mensagem do Daniel que chegou durante a rodada vale mais que o supervisor.
        novas = await self._mensagens_novas(consumir=True)
        if novas:
            await self.ganchos.salvar(self.estado)
            return None, {"texto": _do_daniel(novas), "do_daniel": True}

        relatorio = fim.relatorio or {}
        status = str(relatorio.get("status") or "")
        if fim.recusas or status == "precisa_de_voce":
            pergunta = str(relatorio.get("pergunta") or "").strip()
            return self._desfecho("waiting", self._texto_da_pausa(fim, pergunta), fim=fim, leitor=leitor,
                                  pergunta=pergunta), None

        decisao = await self._decidir(fim)
        if decisao.acao == "aceitar":
            return self._desfecho("completed", str(relatorio.get("resumo") or fim.texto), fim=fim, leitor=leitor), None
        if decisao.acao == "continuar" and decisao.instrucao.strip():
            if self._rodadas_automaticas() >= self.tarefa.max_rodadas:
                return self._desfecho(
                    "waiting",
                    f"O Claude fez {n} rodada(s) e ainda não terminou. Falta: {decisao.motivo or decisao.instrucao}. "
                    "Quer que ele continue?",
                    fim=fim, leitor=leitor,
                ), None
            await self.ganchos.progresso(f"Claude: pedi mais uma rodada — {decisao.motivo or decisao.instrucao}"[:300])
            return None, {"texto": decisao.instrucao}
        return self._desfecho(
            "failed",
            f"O Claude não conseguiu terminar: {decisao.motivo or relatorio.get('resumo') or fim.texto}"[:1500],
            fim=fim, leitor=leitor,
        ), None

    async def _decidir(self, fim: Fim) -> Decisao:
        status = str((fim.relatorio or {}).get("status") or "")
        if self.supervisor is not None:
            try:
                return await self.supervisor.decidir(
                    self.tarefa, fim, list(self.estado["historico"]), int(self.estado["rodada"])
                )
            except Exception as exc:  # noqa: BLE001 — sem supervisor vale o relatório
                logger.warning("[claude] supervisor falhou (%s); decido pelo relatório", exc)
        if status == "travou":
            return Decisao("desistir", motivo=str((fim.relatorio or {}).get("resumo") or ""))
        return Decisao("aceitar")

    def _rodadas_automaticas(self) -> int:
        """Rodadas deste job que o supervisor puxou sozinho (as do Daniel não contam)."""
        return (
            int(self.estado["rodada"])
            - int(self.estado.get("rodadas_do_daniel") or 0)
            - int(self.estado.get("retomadas") or 0)
        )

    def _texto_da_pausa(self, fim: Fim, pergunta: str) -> str:
        partes = []
        if fim.recusas:
            partes.append(
                "O Claude parou porque precisa da sua permissão para algumas ações "
                "(listadas abaixo). Se você liberar, ele continua de onde parou."
            )
        if pergunta:
            partes.append(f"O Claude perguntou: {pergunta}")
        resumo = str((fim.relatorio or {}).get("resumo") or "").strip()
        if resumo:
            partes.append(f"Até aqui: {resumo}")
        return "\n\n".join(partes) or "O Claude parou esperando você."

    def _desfecho(
        self,
        status: str,
        resumo: str,
        *,
        fim: Fim | None = None,
        leitor: LeitorDeStream | None = None,
        pergunta: str = "",
    ) -> Desfecho:
        return Desfecho(
            status=status,
            resumo=resumo.strip(),
            relatorio=fim.relatorio if fim else None,
            recusas=list(fim.recusas) if fim else [],
            pergunta=pergunta,
            session_id=str(self.estado.get("session_id") or ""),
            cwd=str(self.estado.get("cwd") or ""),
            rodadas=int(self.estado.get("rodada") or 0),
            custo_usd=float(self.estado.get("custo") or 0.0),
            cota=leitor.cota if leitor else None,
        )


class SupervisorLLM:
    """O supervisor: uma chamada de LLM, sem tools, que lê o relatório da rodada.

    Só é chamado quando o Claude diz que terminou ou que travou — recusa e
    pergunta já viram pausa sem precisar de modelo.
    """

    _SISTEMA = (
        "Você supervisiona uma tarefa que o Neo delegou ao Claude Code. Leia o objetivo, os "
        "entregáveis e o relatório da última rodada e decida: `aceitar` (o objetivo e os "
        "entregáveis estão cumpridos), `continuar` (falta algo concreto que o Claude pode fazer "
        "sozinho — escreva a instrução da próxima rodada, curta e específica) ou `desistir` "
        "(não há como seguir sem o Daniel). Não invente requisito que o objetivo não pede. "
        'Responda só JSON: {"acao": "aceitar|continuar|desistir", "instrucao": "...", "motivo": "..."}'
    )

    def __init__(self, completar: Callable[[str, str, int], Awaitable[str]]) -> None:
        self._completar = completar

    async def decidir(self, tarefa: Tarefa, fim: Fim, historico: list[dict], rodada: int) -> Decisao:
        relatorio = fim.relatorio or {}
        usuario = json.dumps(
            {
                "objetivo": tarefa.objetivo,
                "entregaveis": tarefa.entregaveis,
                "rodada": rodada,
                "historico": historico[-5:],
                "relatorio": {
                    "status": relatorio.get("status"),
                    "resumo": str(relatorio.get("resumo") or fim.texto)[:3000],
                    "arquivos": (relatorio.get("arquivos") or [])[:30],
                },
            },
            ensure_ascii=False,
        )
        bruto = await self._completar(self._SISTEMA, usuario, 400)
        dados = json.loads(_so_json(bruto))
        acao = str(dados.get("acao") or "").strip().lower()
        if acao not in ("aceitar", "continuar", "desistir"):
            raise ValueError(f"ação desconhecida do supervisor: {acao!r}")
        return Decisao(acao, str(dados.get("instrucao") or ""), str(dados.get("motivo") or ""))


def _fim_da_rodada(n: Any, motivo: str, leitor: LeitorDeStream) -> str:
    """Uma linha para o diário e a faixa do chat: como a rodada acabou."""
    fim = leitor.fim
    if fim is None:
        texto = {
            "tempo": "passou do tempo", "parado": "ficou parado", "interrompido": "interrompida por você",
            "fora_do_ar": "o Mac ficou fora do ar", "corpo": "perdi o contato com o Mac",
        }.get(motivo, "parou sem relatório")
        return f"■ rodada {n}: {texto}"
    status = str((fim.relatorio or {}).get("status") or ("erro" if fim.erro else "sem relatório"))
    partes = [f"■ rodada {n}: {status}", f"{fim.turnos} turnos"]
    if fim.recusas:
        partes.append(f"{len(fim.recusas)} ação(ões) sem permissão")
    if leitor.cota is not None and leitor.cota.janela_5h is not None:
        partes.append(f"cota {round(leitor.cota.janela_5h * 100)}% da janela de 5 h")
    return " · ".join(partes)


def _do_daniel(mensagens: list[dict]) -> str:
    texto = "\n".join(f"- {str(m.get('texto')).strip()}" for m in mensagens)
    return f"O Daniel mandou isto para você, pelo Neo:\n{texto}\n\nSiga a orientação dele e continue."


def _hora(epoch: int | None) -> str:
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _so_json(texto: str) -> str:
    texto = (texto or "").strip()
    inicio, fim = texto.find("{"), texto.rfind("}")
    return texto[inicio: fim + 1] if inicio >= 0 and fim > inicio else texto
