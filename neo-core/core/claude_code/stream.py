"""Leitura do stream-json do Claude Code — a mesma nos dois corpos.

O corpo (Mac ou container) só entrega bytes do ``stream.jsonl`` a partir de um
offset; quem entende o que eles dizem é este arquivo, uma vez só. Formato medido
na F0 com a CLI 2.1.302 (docs/claude-code-delegation-plan.md §13) e gravado em
``tests/claude_code/fixtures/``.

Três regras:

1. **Pedaço não é linha.** O ``claude.read`` corta no teto de bytes, não no fim
   da linha; o resto fica guardado até a próxima leitura.
2. **Evento desconhecido não derruba a tarefa.** No Mac a CLI se atualiza
   sozinha; um tipo novo é contado e ignorado.
3. **O texto cru não sai daqui.** O que vai para o progresso e para o cérebro são
   notas curtas ("editou `x.py`"), nunca o conteúdo dos arquivos nem a saída dos
   comandos — é texto que o Claude leu de terceiros.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Teto de uma nota do DIÁRIO. Era 160 e cortava o que importava: no job
#: 8f83b37c (07/11) os 4 comandos saíram cortados em 100 caracteres, e o 2º
#: terminava em `> RELATORIO.md; rm saida.txt` — uma exclusão que o diário não
#: mostrou. Acima do teto o corte é no MEIO, para o fim do comando aparecer. A
#: faixa do chat continua curta: quem encurta o `progress_note` é o runner.
_NOTA_MAX = 4000
#: A saída de um comando, no diário: começo e fim, com as linhas.
_SAIDA_MAX_LINHAS = 60
_SAIDA_MAX_CHARS = 4000


@dataclass
class Recusa:
    """Ação que o Claude tentou e a permissão negou (pergunta para o Daniel)."""

    ferramenta: str
    entrada: dict[str, Any]
    tool_use_id: str = ""

    def descricao(self) -> str:
        """Como a recusa aparece para o Daniel: o comando ou o arquivo, literal."""
        if self.ferramenta == "Bash":
            return f"rodar `{str(self.entrada.get('command') or '').strip()[:200]}`"
        caminho = self.entrada.get("file_path") or self.entrada.get("path") or self.entrada.get("url")
        if caminho:
            return f"{self.ferramenta} em `{str(caminho)[:200]}`"
        return f"usar {self.ferramenta}"


@dataclass
class Cota:
    """O ``rate_limit_event``: quanto da janela da assinatura já foi usado."""

    estado: str = "allowed"
    janela_5h: float | None = None
    reset_5h: int | None = None
    semana: float | None = None
    reset_semana: int | None = None

    @property
    def estourou(self) -> bool:
        return self.estado not in ("allowed", "allowed_warning")


@dataclass
class Fim:
    """O evento ``result``: o fim de uma rodada."""

    session_id: str
    erro: bool
    texto: str
    relatorio: dict[str, Any] | None
    turnos: int
    custo_usd: float
    motivo: str
    recusas: list[Recusa]
    erro_api: int | None = None

    @property
    def limite_atingido(self) -> bool:
        return self.erro_api == 429


@dataclass
class LeitorDeStream:
    """Consome o stream aos pedaços e guarda o que o orquestrador precisa."""

    cwd: str = ""
    session_id: str = ""
    modelo: str = ""
    modo_permissao: str = ""
    origem_da_chave: str = ""
    cota: Cota | None = None
    fim: Fim | None = None
    recusas: list[Recusa] = field(default_factory=list)
    ultimo_texto: str = ""
    eventos: int = 0
    desconhecidos: int = 0
    _resto: str = ""
    #: tool_use_id -> entrada. O `permission_denied` da hora só traz o id; a
    #: entrada (o comando, o caminho) está no `tool_use` que veio antes.
    _entradas: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: tool_use_id -> nome da ferramenta, para saber de quem é cada `tool_result`.
    _nomes: dict[str, str] = field(default_factory=dict)

    def alimentar(self, pedaco: str) -> list[str]:
        """Processa um pedaço do stream e devolve as notas novas, em ordem."""
        if not pedaco:
            return []
        dados = self._resto + pedaco
        linhas = dados.split("\n")
        self._resto = linhas.pop()
        notas: list[str] = []
        for linha in linhas:
            linha = linha.strip()
            if not linha:
                continue
            try:
                evento = json.loads(linha)
            except ValueError:
                self.desconhecidos += 1
                continue
            if isinstance(evento, dict):
                notas.extend(self._evento(evento))
        return notas

    # ------------------------------------------------------------------

    def _evento(self, ev: dict[str, Any]) -> list[str]:
        self.eventos += 1
        tipo = ev.get("type")
        if ev.get("session_id"):
            self.session_id = str(ev["session_id"])
        if tipo == "system":
            return self._sistema(ev)
        if tipo == "assistant":
            return self._assistente(ev)
        if tipo == "rate_limit_event":
            self._cota(ev.get("rate_limit_info") or {})
            return []
        if tipo == "result":
            self._resultado(ev)
            return []
        if tipo == "user":
            return self._resultados(ev)
        if tipo == "stream_event":
            return []
        self.desconhecidos += 1
        return []

    def _sistema(self, ev: dict[str, Any]) -> list[str]:
        sub = ev.get("subtype")
        if sub == "init":
            self.modelo = str(ev.get("model") or "")
            self.modo_permissao = str(ev.get("permissionMode") or "")
            self.origem_da_chave = str(ev.get("apiKeySource") or "")
            return []
        if sub == "permission_denied":
            tool_use_id = str(ev.get("tool_use_id") or "")
            recusa = Recusa(
                ferramenta=str(ev.get("tool_name") or "?"),
                entrada=dict(ev.get("tool_input") or self._entradas.get(tool_use_id) or {}),
                tool_use_id=tool_use_id,
            )
            self._guardar_recusa(recusa)
            return [_curta(f"sem permissão para {recusa.descricao()}")]
        return []

    def _assistente(self, ev: dict[str, Any]) -> list[str]:
        notas: list[str] = []
        for bloco in (ev.get("message") or {}).get("content") or []:
            if not isinstance(bloco, dict):
                continue
            if bloco.get("type") == "text" and str(bloco.get("text") or "").strip():
                self.ultimo_texto = str(bloco["text"]).strip()
                # O texto INTEIRO: era só a 1ª linha, e o diário existe para o
                # Daniel conferir o que o Claude disse, não um resumo disso.
                notas.append(_curta(f"💬 {self.ultimo_texto}"))
            elif bloco.get("type") == "tool_use":
                if bloco.get("id") and isinstance(bloco.get("input"), dict):
                    self._entradas[str(bloco["id"])] = bloco["input"]
                if bloco.get("id"):
                    self._nomes[str(bloco["id"])] = str(bloco.get("name") or "")
                nota = self._nota_de_ferramenta(str(bloco.get("name") or ""), bloco.get("input") or {})
                if nota:
                    notas.append(_curta(nota))
        return notas

    def _nota_de_ferramenta(self, nome: str, entrada: dict[str, Any]) -> str:
        # No GERÚNDIO: o `tool_use` é o PEDIDO, e o resultado vem depois. "rodou
        # `python3 hello.py`" seguido de "sem permissão para rodar" mentia na
        # faixa do chat (job decc8224, 07/11).
        caminho = self._relativo(str(entrada.get("file_path") or entrada.get("path") or ""))
        if nome == "Write":
            return f"escrevendo `{caminho}`"
        if nome in ("Edit", "MultiEdit", "NotebookEdit"):
            return f"editando `{caminho}`"
        if nome == "Read":
            return f"lendo `{caminho}`"
        if nome == "Bash":
            comando = " ".join(str(entrada.get("command") or "").split())
            return f"rodando `{comando}`"
        if nome in ("Glob", "Grep"):
            return f"procurando `{entrada.get('pattern') or ''}`"
        if nome == "WebSearch":
            return f"pesquisando na web: {entrada.get('query') or ''}"
        if nome == "WebFetch":
            return f"abrindo {entrada.get('url') or ''}"
        if nome in ("Task", "Agent"):
            return "delegando uma parte a um subagente"
        if nome == "StructuredOutput":
            return "escrevendo o relatório final"
        if nome in ("TodoWrite", "TaskCreate", "TaskUpdate"):
            return ""
        return f"usando {nome}" if nome else ""

    def _resultados(self, ev: dict[str, Any]) -> list[str]:
        """O que voltou de cada ferramenta: a saída dos comandos e os erros.

        Nota que começa com ``↳`` e mantém as linhas — o tabuleiro de um jogo da
        velha achatado numa linha não se lê. É do diário: o runner não a põe na
        faixa do chat. Escrever/editar/ler arquivo devolve só "ok" ou o próprio
        arquivo, então só o erro delas entra.
        """
        notas: list[str] = []
        conteudo = (ev.get("message") or {}).get("content")
        if not isinstance(conteudo, list):
            return notas
        negados = {r.tool_use_id for r in self.recusas if r.tool_use_id}
        for bloco in conteudo:
            if not isinstance(bloco, dict) or bloco.get("type") != "tool_result":
                continue
            tool_use_id = str(bloco.get("tool_use_id") or "")
            if tool_use_id in negados:
                continue  # já saiu como "sem permissão para …"
            nome = self._nomes.get(tool_use_id, "")
            texto = _texto_do_resultado(bloco.get("content")).strip("\n")
            erro = bool(bloco.get("is_error"))
            if nome == "Bash":
                linhas = texto.splitlines()
                if not texto.strip() or texto.strip() == "(Bash completed with no output)":
                    notas.append("↳ " + ("erro, sem saída" if erro else "sem saída"))
                    continue
                cabeca = f"↳ {'erro' if erro else 'saída'} ({len(linhas)} {'linha' if len(linhas) == 1 else 'linhas'}):"
                notas.append(f"{cabeca}\n{_saida(linhas)}")
            elif erro and texto.strip():
                notas.append(_curta(f"↳ erro em {nome or 'ferramenta'}: {texto}"))
        return notas

    def _relativo(self, caminho: str) -> str:
        if self.cwd and caminho.startswith(self.cwd.rstrip("/") + "/"):
            return caminho[len(self.cwd.rstrip("/")) + 1:]
        return caminho

    def _cota(self, info: dict[str, Any]) -> None:
        janelas = info.get("unifiedWindows") or {}
        cinco = janelas.get("five_hour") or {}
        semana = janelas.get("seven_day") or {}
        self.cota = Cota(
            estado=str(info.get("status") or "allowed"),
            janela_5h=_num(cinco.get("utilization")),
            reset_5h=_int(cinco.get("resetsAt") or info.get("resetsAt")),
            semana=_num(semana.get("utilization")),
            reset_semana=_int(semana.get("resetsAt")),
        )

    def _resultado(self, ev: dict[str, Any]) -> None:
        for d in ev.get("permission_denials") or []:
            if isinstance(d, dict):
                self._guardar_recusa(Recusa(
                    ferramenta=str(d.get("tool_name") or "?"),
                    entrada=dict(d.get("tool_input") or {}),
                    tool_use_id=str(d.get("tool_use_id") or ""),
                ))
        relatorio = ev.get("structured_output")
        self.fim = Fim(
            session_id=str(ev.get("session_id") or self.session_id),
            erro=bool(ev.get("is_error")),
            texto=str(ev.get("result") or self.ultimo_texto or ""),
            relatorio=relatorio if isinstance(relatorio, dict) else None,
            turnos=int(ev.get("num_turns") or 0),
            custo_usd=float(ev.get("total_cost_usd") or 0.0),
            motivo=str(ev.get("terminal_reason") or ev.get("subtype") or ""),
            recusas=list(self.recusas),
            erro_api=_int(ev.get("api_error_status")),
        )

    def _guardar_recusa(self, recusa: Recusa) -> None:
        # O mesmo tool_use aparece duas vezes: no evento da hora e no `result`.
        for existente in self.recusas:
            if recusa.tool_use_id and existente.tool_use_id == recusa.tool_use_id:
                existente.entrada = existente.entrada or recusa.entrada
                return
        self.recusas.append(recusa)


def _curta(texto: str) -> str:
    """Uma linha; acima do teto, começo e FIM — o fim de um comando é onde mora o `rm`."""
    texto = " ".join(texto.split())
    if len(texto) <= _NOTA_MAX:
        return texto
    fim = _NOTA_MAX // 4
    return f"{texto[: _NOTA_MAX - fim - 3]} … {texto[-fim:]}"


def _texto_do_resultado(conteudo: Any) -> str:
    """O `content` de um `tool_result` vem como texto ou como lista de blocos."""
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        return "\n".join(
            str(b.get("text") or "") for b in conteudo if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _saida(linhas: list[str]) -> str:
    """Começo e fim da saída, dizendo quanto ficou de fora no meio."""
    if len(linhas) > _SAIDA_MAX_LINHAS:
        metade = _SAIDA_MAX_LINHAS // 2
        linhas = linhas[:metade] + [f"… ({len(linhas) - 2 * metade} linhas no meio) …"] + linhas[-metade:]
    texto = "\n".join(linha.rstrip() for linha in linhas)
    if len(texto) > _SAIDA_MAX_CHARS:
        fim = _SAIDA_MAX_CHARS // 3
        texto = f"{texto[: _SAIDA_MAX_CHARS - fim]}\n… (cortado no meio) …\n{texto[-fim:]}"
    return texto


def _num(valor: Any) -> float | None:
    try:
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


def _int(valor: Any) -> int | None:
    try:
        return int(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None
