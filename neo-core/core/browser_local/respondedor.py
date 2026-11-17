"""Respondedor de formulário: a memória responde cada pergunta antes de o campo ser preenchido.

O job de navegador não tinha como pedir dado. Quem respondia o formulário era o
cérebro, de uma vez e ANTES do job, com o bloco de memória do turno — ranqueado
pela mensagem do Daniel, que era "preencha e envie". Na sessão e752a399
(2026-11-01) esse bloco trouxe só o treino de antebraço; segunda, quarta,
quinta e sexta saíram inventados, com séries inventadas, e o formulário foi
enviado assim. Perguntado só pelo treino de sexta, o mesmo Neo acertava.

Aqui quem pergunta é o job, com o rótulo LITERAL de cada campo, em três etapas:

1. **evidência por pergunta, sem LLM** (``PKSService.evidencia_de_formulario``):
   uma busca do ranking v2 por pergunta, mais o bloco do dono (campos redigidos
   e relações). É o piso de "uma pergunta por dia": a busca nunca é agrupada;
2. **agrupamento pela evidência em comum**: perguntas que trouxeram as mesmas
   entidades vão numa chamada só — os cinco dias de treino caem no mesmo plano.
   Quem decide agrupar é a sobreposição da evidência, não o modelo;
3. **uma chamada de LLM por grupo**, que redige cada resposta pronta para o
   campo só com a evidência daquele grupo, e depois o **aterramento**: fonte que
   não está na evidência, marcador que não veio da memória e item sem lastro
   nela são cortados, e o corte vira observação para o Daniel.

Dado protegido não passa por aqui: o bloco do dono já chega com ``«pks:email»``
no lugar do e-mail, e a resposta devolve o MARCADOR. Quem o troca pelo valor é o
preenchimento, e só com liberação (``core/release/fill.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from core.documents.prompts import parse_json_object

logger = logging.getLogger(__name__)

#: Perguntas por consulta. Um formulário com mais que isso é paginado, e o
#: agente consulta página por página.
MAX_PERGUNTAS = 20
_PERGUNTA_MAX = 300
#: Evidência por chamada (estimada em caracteres/4). Os 8 campos do formulário
#: de treino somam ~1.300 tokens de evidência: cabem numa chamada só.
_ORCAMENTO_TOKENS = 6000
_CONCORRENCIA = 3
#: Uma resposta de lista longa (o treino de um dia) tem ~250 tokens.
_TOKENS_POR_PERGUNTA = 350
_TETO_TOKENS = 4000
_TIMEOUT_S = 90

_MARCADOR_RE = re.compile(r"«\s*pks:[^»]+»")
_CABECALHO_RE = re.compile(r"^###\s+(.+?)\s+\[[^\]]*\]", re.MULTILINE)

#: Palavras que o modelo escreve para FORMATAR a resposta e que a memória não
#: precisa ter ("4 séries de 12 repetições"). Não contam no aterramento.
_PALAVRAS_DE_FORMATO = frozenset({
    "series", "serie", "repeticoes", "repeticao", "reps", "sets", "vezes", "cada",
    "para", "como", "mais", "menos", "entre", "sobre", "pelo", "pela", "pelos",
    "pelas", "este", "esta", "esse", "essa", "isso", "isto", "tambem", "quando",
    "onde", "porque", "muito", "pouco", "todos", "todas", "nenhum", "nenhuma",
    "dias", "semana", "treino", "treinos",
})
#: Acima disto o item é prosa, não lista: exigir TODAS as palavras na memória
#: recusaria qualquer frase redigida. Prosa passa com a maioria das palavras e
#: chega ao Daniel marcada para conferência.
_ITEM_DE_LISTA_MAX_PALAVRAS = 6
_PROSA_MIN_RAZAO = 0.6


class LLMDoRespondedor(Protocol):
    async def complete(self, system: str, user: str, *, max_tokens: int) -> str: ...


#: ``perguntas -> {"dono": str, "perguntas": [[(chave, texto), ...], ...]}``
Evidencia = Callable[[list[str]], Awaitable[dict]]


@dataclass
class Resposta:
    """O que a memória responde a UMA pergunta do formulário."""

    pergunta: str
    #: Texto pronto para digitar no campo. ``None`` = a memória não tem.
    resposta: str | None = None
    fontes: list[str] = field(default_factory=list)
    faltou: str = ""
    observacao: str = ""

    def para_o_agente(self) -> str:
        """A linha que o agente do navegador lê antes de preencher."""
        if self.resposta is None:
            motivo = self.faltou or "a memória não tem"
            return f"«{self.pergunta}» → (sem resposta: {motivo}) — deixe este campo em branco"
        extras = [f"da memória: {', '.join(self.fontes)}"] if self.fontes else []
        if self.faltou:
            extras.append(f"faltou: {self.faltou}")
        if self.observacao:
            extras.append(f"atenção: {self.observacao}")
        return f"«{self.pergunta}» → {self.resposta}" + (f"  ({'; '.join(extras)})" if extras else "")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, dado: dict) -> "Resposta | None":
        if not isinstance(dado, dict) or not str(dado.get("pergunta") or "").strip():
            return None
        resposta = dado.get("resposta")
        return cls(
            pergunta=str(dado["pergunta"]).strip(),
            resposta=str(resposta) if resposta not in (None, "") else None,
            fontes=[str(f) for f in (dado.get("fontes") or []) if f],
            faltou=str(dado.get("faltou") or ""),
            observacao=str(dado.get("observacao") or ""),
        )


def chave_da_pergunta(pergunta: str) -> str:
    """A mesma pergunta escrita com outra caixa, acento ou asterisco é a mesma."""
    return comparavel(str(pergunta or "").replace("*", " "))


def comparavel(texto: str) -> str:
    """Texto para comparar: minúsculo, sem acento, espaços colapsados."""
    return " ".join(_normalizar(texto).split())


_SISTEMA = """Você preenche um formulário EM NOME do Daniel com o que a memória dele diz — e só com isso.

Você recebe as perguntas do formulário (o texto literal de cada campo) e a MEMÓRIA dele em blocos "### Nome [tipo]". O primeiro bloco é o próprio Daniel.

Para cada pergunta devolva:
- "resposta": o texto PRONTO para digitar no campo, no formato que a pergunta pede. Vários itens: separados por "; ". Pergunta com opções listadas: só o texto exato da(s) opção(ões), separadas por "; ".
- "fontes": os nomes EXATOS dos blocos de onde a resposta saiu (o que vem depois de "### ", sem o [tipo]).
- "faltou": o que a pergunta pede e a memória não tem (ex.: "séries e repetições de 4 dos 6 exercícios"). Vazio se nada faltou.
- "observacao": o que o Daniel precisa conferir (ex.: dois treinos marcados para o mesmo dia). Vazio se nada.

Regras:
1. NADA de fora da memória: não complete, não melhore, não sugira, não use conhecimento geral. Exercício, número, nome ou opção que não está escrito num bloco NÃO entra.
2. Detalhe que falta não se inventa: responda com o que existe e diga em "faltou". Exercício sem séries vai só com o nome.
3. Dia da semana: o campo `weekday` e o nome do bloco dizem de que dia ele é (monday=segunda, tuesday=terça, wednesday=quarta, thursday=quinta, friday=sexta, saturday=sábado, sunday=domingo). Mais de um bloco para o mesmo dia: inclua todos e diga em "observacao".
4. «pks:campo» está NO LUGAR de um dado protegido. Se a resposta é esse dado, devolva o marcador exatamente como está escrito — nunca um valor.
5. A memória não responde a pergunta: "resposta": null, e diga em "faltou" o que faltou.
6. Séries e repetições: escreva "3x12-15" (séries x repetições), colado no nome do exercício.

Responda SÓ com JSON, uma entrada por pergunta, na ordem:
{"respostas": [{"n": 1, "resposta": "...", "fontes": ["..."], "faltou": "", "observacao": ""}]}"""


class Respondedor:
    """Responde as perguntas de um formulário com a memória do Daniel."""

    def __init__(
        self,
        *,
        evidencia: Evidencia,
        llm: LLMDoRespondedor,
        finalidade: str = "",
        orcamento_tokens: int = _ORCAMENTO_TOKENS,
        concorrencia: int = _CONCORRENCIA,
    ) -> None:
        self._evidencia = evidencia
        self._llm = llm
        self._finalidade = str(finalidade or "").strip()[:600]
        self._orcamento = max(500, int(orcamento_tokens))
        self._sem = asyncio.Semaphore(max(1, concorrencia))

    async def responder(self, perguntas: list[str]) -> list[Resposta]:
        unicas: list[str] = []
        vistas: set[str] = set()
        for p in perguntas:
            texto = " ".join(str(p or "").split())[:_PERGUNTA_MAX]
            chave = chave_da_pergunta(texto)
            if texto and chave not in vistas:
                vistas.add(chave)
                unicas.append(texto)
        unicas = unicas[:MAX_PERGUNTAS]
        if not unicas:
            return []
        try:
            ev = await self._evidencia(unicas)
        except Exception:  # noqa: BLE001 — sem memória, nenhuma resposta, nunca chute
            logger.exception("[respondedor] a evidência falhou")
            return [Resposta(p, faltou="a memória não respondeu") for p in unicas]
        dono = str(ev.get("dono") or "")
        # Uma lista por pergunta, nem mais nem menos: evidência desalinhada não
        # pode virar resposta para a pergunta errada (nem índice fora da lista).
        listas = list(ev.get("perguntas") or [])[: len(unicas)]
        listas += [[] for _ in range(len(unicas) - len(listas))]
        lotes = agrupar(listas, self._orcamento, custo_fixo=_tokens(dono))
        respostas: dict[int, Resposta] = {}
        resultados = await asyncio.gather(
            *(self._responder_lote(unicas, listas, lote, dono) for lote in lotes)
        )
        for parte in resultados:
            respostas.update(parte)
        return [respostas.get(i) or Resposta(p, faltou="a memória não respondeu") for i, p in enumerate(unicas)]

    async def _responder_lote(
        self, perguntas: list[str], listas: list[list], lote: list[int], dono: str
    ) -> dict[int, Resposta]:
        blocos: dict[str, str] = {}
        for i in lote:
            for chave, texto in listas[i]:
                blocos.setdefault(str(chave), str(texto))
        memoria = "\n".join(p for p in (dono, *blocos.values()) if p).strip()
        if not memoria:
            return {i: Resposta(perguntas[i], faltou="a memória não tem nada sobre isso") for i in lote}

        linhas = []
        for n, i in enumerate(lote, start=1):
            primeiros = [_nome_do_bloco(t) for _, t in listas[i][:3]]
            dica = f"  (a busca achou primeiro: {'; '.join(x for x in primeiros if x)})" if primeiros else ""
            linhas.append(f"{n}. «{perguntas[i]}»{dica}")
        usuario = "\n\n".join(
            p
            for p in (
                f"O QUE O DANIEL PEDIU: {self._finalidade}" if self._finalidade else "",
                "PERGUNTAS DO FORMULÁRIO:\n" + "\n".join(linhas),
                "MEMÓRIA:\n" + memoria,
            )
            if p
        )
        teto = min(_TETO_TOKENS, _TOKENS_POR_PERGUNTA * len(lote) + 300)
        try:
            async with self._sem:
                bruto = await asyncio.wait_for(
                    self._llm.complete(_SISTEMA, usuario, max_tokens=teto), timeout=_TIMEOUT_S
                )
            dados = parse_json_object(bruto)
        except Exception as exc:  # noqa: BLE001 — falha da chamada = em branco, não chute
            logger.warning("[respondedor] lote de %d pergunta(s) falhou: %s", len(lote), exc)
            return {i: Resposta(perguntas[i], faltou="a consulta à memória falhou") for i in lote}

        itens = dados.get("respostas") if isinstance(dados, dict) else None
        por_n: dict[int, dict] = {}
        for pos, item in enumerate(itens if isinstance(itens, list) else [], start=1):
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("n") or pos)
            except (TypeError, ValueError):
                n = pos
            por_n.setdefault(n, item)

        saida: dict[int, Resposta] = {}
        for n, i in enumerate(lote, start=1):
            item = por_n.get(n)
            if item is None:
                saida[i] = Resposta(perguntas[i], faltou="a consulta não respondeu esta pergunta")
                continue
            saida[i] = aterrar(perguntas[i], item, memoria)
        return saida


def agrupar(listas: list[list], orcamento_tokens: int, *, custo_fixo: int = 0) -> list[list[int]]:
    """Índices das perguntas em lotes: quem divide evidência vai junto.

    Componentes conexos pela evidência em comum (union-find nas chaves das
    entidades) e, depois, componentes pequenos empacotados no mesmo lote até o
    orçamento — "agrupar quando possível". Componente maior que o orçamento é
    fatiado pergunta a pergunta. Toda pergunta cai em exatamente um lote.
    """
    n = len(listas)
    pai = list(range(n))

    def raiz(x: int) -> int:
        while pai[x] != x:
            pai[x] = pai[pai[x]]
            x = pai[x]
        return x

    dono_da_chave: dict[str, int] = {}
    for i, achados in enumerate(listas):
        for chave, _ in achados:
            j = dono_da_chave.setdefault(str(chave), i)
            pai[raiz(i)] = raiz(j)

    componentes: dict[int, list[int]] = {}
    for i in range(n):
        componentes.setdefault(raiz(i), []).append(i)

    def custo(indices: list[int]) -> int:
        vistos: dict[str, str] = {}
        for i in indices:
            for chave, texto in listas[i]:
                vistos.setdefault(str(chave), str(texto))
        return custo_fixo + sum(_tokens(t) for t in vistos.values())

    pedacos: list[list[int]] = []
    for membros in sorted(componentes.values(), key=lambda m: m[0]):
        atual: list[int] = []
        for i in membros:
            if atual and custo(atual + [i]) > orcamento_tokens:
                pedacos.append(atual)
                atual = []
            atual.append(i)
        if atual:
            pedacos.append(atual)

    lotes: list[list[int]] = []
    for pedaco in pedacos:
        for lote in lotes:
            if custo(lote + pedaco) <= orcamento_tokens:
                lote.extend(pedaco)
                break
        else:
            lotes.append(list(pedaco))
    return [sorted(lote) for lote in lotes]


def aterrar(pergunta: str, item: dict, memoria: str) -> Resposta:
    """A resposta do modelo, só com o que tem lastro na memória.

    O modelo foi mandado não inventar; isto confere. Três cortes:

    * **fonte** que não é um bloco da evidência sai — e resposta sem nenhuma
      fonte válida vira "em branco": não há como dizer de onde ela veio;
    * **marcador** ``«pks:…»`` que não está na memória sai: ele vira valor
      real lá no preenchimento, e só a memória pode dizer quais existem;
    * **item da lista** cujas palavras não estão na memória sai. Foi o padrão da
      sessão e752a399: "Desenvolvimento militar", "Stiff", "Mesa flexora" — nomes
      plausíveis que não existiam em lugar nenhum do PKS.
    """
    resposta = Resposta(
        pergunta=pergunta,
        faltou=" ".join(str(item.get("faltou") or "").split())[:300],
        observacao=" ".join(str(item.get("observacao") or "").split())[:300],
    )
    bruto = item.get("resposta")
    texto = " ".join(str(bruto).split()) if bruto not in (None, "", "null") else ""

    cabecalhos = {_normalizar(c): c for c in _CABECALHO_RE.findall(memoria)}
    fontes: list[str] = []
    for f in item.get("fontes") or []:
        # "Treino de sexta-feira [workout]" e "Daniel Morais (você)" são o bloco.
        limpo = re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", str(f)).strip()
        nome = cabecalhos.get(_normalizar(limpo))
        if nome and nome not in fontes:
            fontes.append(nome)
    resposta.fontes = fontes
    if not texto:
        return resposta
    if not fontes:
        resposta.observacao = _juntar(
            resposta.observacao, "a resposta não disse de onde veio na memória — ficou em branco"
        )
        return resposta

    marcadores_validos = {_normalizar(m) for m in _MARCADOR_RE.findall(memoria)}
    for m in _MARCADOR_RE.findall(texto):
        if _normalizar(m) not in marcadores_validos:
            resposta.observacao = _juntar(
                resposta.observacao, f"{m} não existe na memória — ficou em branco"
            )
            return resposta
    if _MARCADOR_RE.fullmatch(texto.strip()):
        resposta.resposta = texto.strip()
        return resposta

    corpus = _normalizar(memoria)
    palavras = set(re.findall(r"[a-z]{4,}", corpus))
    numeros = set(re.findall(r"\d+", corpus))
    mantidos: list[str] = []
    cortados: list[str] = []
    for parte in (p.strip() for p in re.split(r"[;\n]", texto)):
        if not parte:
            continue
        if _tem_lastro(parte, palavras, numeros):
            mantidos.append(parte)
        else:
            cortados.append(parte)
    if cortados:
        resposta.observacao = _juntar(
            resposta.observacao, "tirei o que não está na memória: " + "; ".join(cortados)[:200]
        )
    if mantidos:
        resposta.resposta = "; ".join(mantidos)
    return resposta


def _tem_lastro(parte: str, palavras: set[str], numeros: set[str]) -> bool:
    if _MARCADOR_RE.search(parte):
        return False  # marcador no meio de texto livre: só sozinho, conferido acima
    norm = _normalizar(parte)
    if any(num not in numeros for num in re.findall(r"\d+", norm)):
        return False
    conteudo = [w for w in re.findall(r"[a-z]{4,}", norm) if w not in _PALAVRAS_DE_FORMATO]
    if not conteudo:
        return True
    achadas = sum(1 for w in conteudo if w in palavras)
    if len(conteudo) <= _ITEM_DE_LISTA_MAX_PALAVRAS:
        return achadas == len(conteudo)
    return achadas / len(conteudo) >= _PROSA_MIN_RAZAO


def _nome_do_bloco(texto: str) -> str:
    achado = _CABECALHO_RE.search(str(texto or ""))
    return achado.group(1) if achado else ""


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFD", str(texto or "").lower())
    return "".join(c for c in sem_acento if unicodedata.category(c) != "Mn")


def _tokens(texto: str) -> int:
    return len(str(texto or "")) // 4 + 1


def _juntar(a: str, b: str) -> str:
    return f"{a}; {b}" if a else b


def respostas_de(dados: Any) -> list[Resposta]:
    """Respostas guardadas na sessão da aba (a retomada as herda)."""
    if isinstance(dados, str):
        try:
            dados = json.loads(dados)
        except json.JSONDecodeError:
            return []
    saida = []
    for d in dados if isinstance(dados, list) else []:
        r = Resposta.from_dict(d)
        if r is not None:
            saida.append(r)
    return saida
