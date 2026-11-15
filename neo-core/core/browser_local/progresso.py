"""Progresso da tarefa, montado pelo CÓDIGO com o que a página e o navegador confirmam.

**Por que não é o modelo que anota.** O caderno (`driver.notes`) é escrito pelo
modelo, e progresso escrito pelo modelo falhou dos dois jeitos nos 20 diários da
VPS (medido em 2026-11-08):

- o job f43e2f02 respondeu as 36 perguntas do 12axes (41 cliques confirmados
  "→ SELECIONADO") e não anotou NADA — o caderno chegou vazio;
- o job 50227807, sem lugar para o progresso, escreveu-o dentro das notas, 23
  vezes ("P1..P29 já anotadas. P30: …").

As duas coisas de que o progresso precisa já chegam prontas, sem modelo no meio:
o CONTADOR da página ("Questão 7 de 10", "Pergunta 14 de 36 39% concluído" —
presente nos 13 questionários medidos) diz em qual item a tarefa está e quantos
são; e o device lê o controle DEPOIS do clique ("→ SELECIONADO"), que diz qual
alternativa ficou marcada.

**Marcada não é respondida.** No Portal a marcação só vale depois de
"Responder" (que salva e já pula para a questão seguinte). Respondida é:

- um clique de confirmação depois de marcar (`_CONFIRMA`);
- a página trocar de item logo depois da marcação (o 12axes avança sozinho);
- para quem só confere ou extrai, a alternativa JÁ marcada com o botão de
  confirmação desabilitado — é como o Portal mostra uma resposta salva (antes de
  marcar, o "Responder" também vem desabilitado, mas aí não há marcada).

**Sem contador na página** (decisão do Daniel, 2026-11-08: a opção "c"), o
total vem do PEDIDO ("são 10 questões") e o código conta as marcações que o
navegador confirmou: "3 de 10 marcadas". Não diz o número de cada questão, mas a
contagem é real. Sem contador e sem total no pedido, não há bloco: número
inventado é pior do que nenhum.

O cuidado da contagem é não contar duas vezes a mesma questão. Duas marcações
seguidas na mesma página, sem envio nem troca de página entre elas, podem ser a
mesma questão remarcada ou duas questões de um formulário de página única. O
snapshot seguinte decide pelo próprio rádio: marcar B desmarca A quando os dois
são do mesmo grupo; se A continua marcada, são questões diferentes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

#: O contador do SITE. Só os substantivos medidos nos diários: "Questão N de N
#: Responder Finalizar" (Portal), "Pergunta N de N N% concluído" e "Identificando
#: seu arquétipo N de N" (12axes). "Passo N de M" fica de fora de propósito: é
#: como o próprio driver escreve o orçamento dele, e é como cadastro e checkout
#: numeram etapas que não são perguntas.
_CONTADOR = re.compile(
    r"\b(quest[aã]o|pergunta|question|exerc[ií]cio|arqu[eé]tipo)\s+(\d{1,3})\s*(?:de|of|/)\s*(\d{1,3})\b",
    re.IGNORECASE,
)

#: Botão que ENVIA a resposta marcada. "Finalizar" fica de fora: encerra a prova
#: inteira, e não é um envio de UMA resposta.
_CONFIRMA = re.compile(
    r"^\s*(responder|confirmar|enviar resposta|salvar resposta|pr[oó]xim[ao]|avan[cç]ar|"
    r"continuar|next|submit|check)\b",
    re.IGNORECASE,
)

#: O total que o PEDIDO declara, para a página que não numera as questões.
#: Medido nas tarefas dos 20 diários: "quiz de 36 perguntas", "atividade de 10
#: questões", "Short 36 questions" — e o Daniel escreve sem acento ("sao 10
#: questoes"). "alternativas" fica de fora: "5 alternativas" não é o tamanho da prova.
_TOTAL_DO_PEDIDO = re.compile(
    r"\b(\d{1,3})\s+(?:quest(?:õ|o)es|perguntas|questions|exerc[ií]cios)\b", re.IGNORECASE
)

#: Mais controles marcados do que isto numa tela não é a resposta de UMA questão:
#: é uma página de preferências.
_MARCADAS_MAX = 6

#: Rótulo de alternativa no caderno. O corpo da nota é barato, mas a linha tem de
#: caber numa leitura de olho.
_ROTULO_MAX = 300

#: Número no começo de uma nota de questão: "Q3 —", "Questão 3", "Pergunta 14",
#: "P1:". "PRODUTO 4" não casa (depois do "p" tem de vir o número).
_NUMERO_DA_NOTA = re.compile(
    r"^\s*(?:\*\*)?\s*(?:q|quest[aã]o|pergunta|p)\s*\.?\s*(\d{1,3})\b", re.IGNORECASE
)


def numero_da_nota(nota: str) -> int | None:
    """O número da questão no começo da nota, se houver."""
    m = _NUMERO_DA_NOTA.match(nota or "")
    return int(m.group(1)) if m else None


def total_do_pedido(*textos: str) -> int | None:
    """O primeiro "N questões" do pedido — o do Daniel antes do que o cérebro escreveu."""
    for texto in textos:
        m = _TOTAL_DO_PEDIDO.search(texto or "")
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return None


def _chave(serie: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", serie).encode("ascii", "ignore").decode()
    return sem_acento.casefold()


def _textos(snapshot: dict) -> list[str]:
    """O texto da PÁGINA, na ordem em que a tela mostra: nunca o prompt do agente."""
    main = snapshot.get("main")
    return [
        str(snapshot.get("tela") or ""),
        str(main.get("text") or "") if isinstance(main, dict) else "",
        str(snapshot.get("resto") or ""),
        str(snapshot.get("text") or ""),
    ]


def ler_contador(snapshot: dict) -> tuple[str, str, int, int] | None:
    """``(chave da série, nome como a página escreve, número, total)`` ou ``None``.

    A primeira série achada, na ordem tela → principal → resto → corpo. Uma série
    com DOIS números diferentes na mesma página não conta: é uma lista, não o
    contador de onde a tarefa está.
    """
    valores: dict[str, set[tuple[int, int]]] = {}
    nomes: dict[str, str] = {}
    for texto in _textos(snapshot):
        for m in _CONTADOR.finditer(texto):
            numero, total = int(m.group(2)), int(m.group(3))
            if not 1 <= numero <= total:
                continue
            chave = _chave(m.group(1))
            nomes.setdefault(chave, m.group(1).capitalize())
            valores.setdefault(chave, set()).add((numero, total))
    for chave, achados in valores.items():
        if len(achados) == 1:
            numero, total = next(iter(achados))
            return chave, nomes[chave], numero, total
    return None


def _limpo(rotulo: str) -> str:
    texto = " ".join(str(rotulo or "").split())
    return texto if len(texto) <= _ROTULO_MAX else texto[:_ROTULO_MAX].rstrip() + "…"


def _mesmo_rotulo(a: str, b: str) -> bool:
    """O device e o snapshot escrevem o mesmo controle com pequenas diferenças."""
    a, b = _chave(a)[:40], _chave(b)[:40]
    n = min(len(a), len(b))
    return n > 0 and a[:n] == b[:n]


def _marcadas(snapshot: dict) -> list[str]:
    """O que a página mostra marcado agora (rádio/caixa), sem repetir.

    O Portal manda cada alternativa duas vezes — o `<label>` com o texto e o rádio
    sem texto — e as duas vêm `[marcado]`; a sem texto não diz nada.
    """
    vistos: list[str] = []
    for item in snapshot.get("elements") or []:
        if item.get("checked") is not True or item.get("chrome"):
            continue
        texto = _limpo(item.get("text") or "")
        if texto and not any(_mesmo_rotulo(texto, v) for v in vistos):
            vistos.append(texto)
    return vistos if len(vistos) <= _MARCADAS_MAX else []


def _confirmacao_desabilitada(snapshot: dict) -> bool:
    return any(
        item.get("disabled") and _CONFIRMA.search(str(item.get("text") or ""))
        for item in snapshot.get("elements") or []
    )


def _faixas(numeros: list[int]) -> str:
    """[1, 2, 3, 5, 7, 8] → "1–3, 5, 7–8"."""
    partes: list[str] = []
    ordenados = sorted(set(numeros))
    i = 0
    while i < len(ordenados):
        j = i
        while j + 1 < len(ordenados) and ordenados[j + 1] == ordenados[j] + 1:
            j += 1
        partes.append(str(ordenados[i]) if i == j else f"{ordenados[i]}–{ordenados[j]}")
        i = j + 1
    return ", ".join(partes)


@dataclass
class ItemDoProgresso:
    numero: int
    #: O rótulo do que ficou marcado, como o navegador o devolveu.
    marcada: str = ""
    #: ``marquei`` (clique confirmado), ``já estava`` (a página já mostrava) ou
    #: ``na página`` (a página mostra outra coisa que não a que eu marquei).
    como: str = ""
    respondida: bool = False


@dataclass
class Marcacao:
    """Uma questão SEM número: só o que o navegador confirmou ter marcado."""

    rotulo: str
    #: O `ref` e a página do controle marcado: é por eles que o snapshot seguinte
    #: vê se a marcação continua de pé.
    ref: int | None
    url: str
    respondida: bool = False
    #: Veio logo depois de outra, sem envio nem troca de página entre as duas:
    #: pode ser a mesma questão remarcada. O snapshot seguinte decide.
    talvez_remarcacao: bool = False


@dataclass
class Serie:
    nome: str
    total: int
    itens: dict[int, ItemDoProgresso] = field(default_factory=dict)

    def respondidas(self) -> list[int]:
        return [n for n, i in self.itens.items() if i.respondida]

    def so_marcadas(self) -> list[int]:
        return [n for n, i in self.itens.items() if i.marcada and not i.respondida]

    def abertas(self) -> list[int]:
        return [n for n, i in self.itens.items() if not i.marcada]

    def nao_vistas(self) -> list[int]:
        """Números do contador por onde a tarefa não passou. NÃO é "sem resposta":
        o job 90b58ca3 começou na pergunta 16 porque outro job já tinha
        respondido as 15 primeiras, e esta tarefa nunca as viu."""
        return [n for n in range(1, self.total + 1) if n not in self.itens]


class Progresso:
    """Uma tabela por série do site ("Questão", "Pergunta", "Arquétipo")."""

    def __init__(self) -> None:
        self.series: dict[str, Serie] = {}
        #: Onde a página está agora: ``(chave da série, número)``.
        self.atual: tuple[str, int] | None = None
        #: A última ação foi a marcação DESTE item: se a página trocar de item no
        #: próximo snapshot, foi a marcação que a fez avançar (resposta aceita).
        self._marcou_agora: tuple[str, int] | None = None
        #: Para a página SEM contador: o total que o pedido declara e as
        #: marcações confirmadas, em ordem.
        self.total_pedido: int | None = None
        self.marcacoes: list[Marcacao] = []
        #: Desde a última marcação houve envio ou troca de página: a próxima
        #: marcação é de outra questão.
        self._fronteira = True
        self._marcou_agora_sem_numero = False

    def __bool__(self) -> bool:
        return bool(self.series) or self._sem_contador()

    def _sem_contador(self) -> bool:
        """Contagem pelo pedido: só sem contador nenhum no job e com total declarado."""
        return not self.series and bool(self.total_pedido) and bool(self.marcacoes)

    def _item(self, onde: tuple[str, int] | None) -> ItemDoProgresso | None:
        if onde is None or onde[0] not in self.series:
            return None
        return self.series[onde[0]].itens.setdefault(onde[1], ItemDoProgresso(onde[1]))

    # ── o que acontece ──────────────────────────────────────────────────

    def ver(self, snapshot: dict) -> None:
        """Cada snapshot: em que item a página está e o que ela já mostra marcado."""
        contador = ler_contador(snapshot)
        agora = (contador[0], contador[2]) if contador else None
        if self._marcou_agora is not None and agora != self._marcou_agora:
            item = self._item(self._marcou_agora)
            if item is not None and item.marcada:
                item.respondida = True
            self._marcou_agora = None
        # Sem contador (tela de resultado, outra página), nada do que for marcado
        # aqui é de uma questão numerada.
        self.atual = agora
        if contador is None:
            if self.marcacoes and not self.series:
                self._ver_sem_numero(snapshot)
            return
        chave, nome, numero, total = contador
        serie = self.series.setdefault(chave, Serie(nome=nome, total=total))
        # O total pode mudar no meio: o 12axes passou de "arquétipo 6 de 6" para
        # "7 de 7" quando a resposta abriu uma pergunta a mais.
        serie.total = total
        item = serie.itens.setdefault(numero, ItemDoProgresso(numero))
        marcadas = _marcadas(snapshot)
        if not marcadas:
            return
        rotulo = "; ".join(marcadas)
        if not item.marcada:
            item.marcada, item.como = rotulo, "já estava"
        elif not _mesmo_rotulo(item.marcada, rotulo):
            # A página é a verdade: o que está marcado agora não é o que eu
            # marquei (ou o que ela mostrava antes).
            item.marcada, item.como, item.respondida = rotulo, "na página", False
        if _confirmacao_desabilitada(snapshot):
            # Marcada E botão de envio desabilitado: a resposta está salva (antes
            # de marcar ele também vem desabilitado, mas aí não há marcada; logo
            # depois de marcar, sem enviar, ele vem habilitado).
            item.respondida = True

    def _ver_sem_numero(self, snapshot: dict) -> None:
        """Página sem contador: a última marcação ainda vale, e a página mudou?"""
        elementos = {i.get("ref"): i for i in snapshot.get("elements") or []}

        def no_lugar(m: Marcacao) -> dict | None:
            """O controle da marcação, se ele ainda está na página com o mesmo rótulo."""
            el = elementos.get(m.ref)
            if el is None or not _mesmo_rotulo(str(el.get("text") or ""), m.rotulo):
                return None
            return el

        ultima = self.marcacoes[-1]
        if ultima.talvez_remarcacao and len(self.marcacoes) > 1:
            ultima.talvez_remarcacao = False
            anterior = no_lugar(self.marcacoes[-2])
            if anterior is not None and anterior.get("checked") is False:
                # Marcar esta DESMARCOU a anterior: mesmo grupo de rádio, a
                # mesma questão com outra resposta. Fica uma só.
                del self.marcacoes[-2]
        if self._fronteira:
            return
        url = str(snapshot.get("url") or "")
        el = no_lugar(ultima)
        trocou = (
            bool(url and ultima.url and url != ultima.url)
            or (ultima.ref is not None and el is None)
            # O controle continua lá, desmarcado, sem ninguém ter clicado nele:
            # a página foi para a questão seguinte, com as mesmas alternativas.
            or (el is not None and el.get("checked") is False)
        )
        if trocou:
            self._fronteira = True
            if self._marcou_agora_sem_numero:
                ultima.respondida = True
            self._marcou_agora_sem_numero = False

    def _marcou_sem_numero(self, rotulo: str, ref: int | None, url: str) -> None:
        if self.series or not self.total_pedido:
            return
        if self.marcacoes and not self._fronteira:
            ultima = self.marcacoes[-1]
            if ref is not None and ref == ultima.ref:
                # O mesmo controle de novo: nada muda (nem desfaz um envio).
                self._marcou_agora_sem_numero = True
                return
            nova = Marcacao(rotulo=rotulo, ref=ref, url=url, talvez_remarcacao=True)
        else:
            nova = Marcacao(rotulo=rotulo, ref=ref, url=url)
        self.marcacoes.append(nova)
        self._fronteira = False
        self._marcou_agora_sem_numero = True

    def marcou(self, rotulo: str, *, ref: int | None = None, url: str = "") -> None:
        """O device confirmou o controle MARCADO depois do clique."""
        item = self._item(self.atual)
        if item is None:
            if self.atual is None:
                self._marcou_sem_numero(_limpo(rotulo), ref, url)
            return
        rotulo = _limpo(rotulo)
        # Clicar de novo na MESMA alternativa não desfaz o envio: no job
        # 9c9b5bae o modelo voltou à Q1 (achando que era a Q9) e clicou na
        # alternativa que já estava salva. Trocar de alternativa desfaz, até a
        # nova confirmação.
        if not (item.respondida and _mesmo_rotulo(item.marcada, rotulo)):
            item.respondida = False
        item.marcada, item.como = rotulo, "marquei"
        self._marcou_agora = self.atual

    def clicou(self, rotulo: str) -> None:
        """Clique que não marcou nada: se for o botão de envio, a marcada foi enviada."""
        envio = bool(_CONFIRMA.search(str(rotulo or "")))
        item = self._item(self.atual)
        if item is not None and item.marcada and envio:
            item.respondida = True
        self._marcou_agora = None
        if self.atual is None and self.marcacoes and not self._fronteira and envio:
            self.marcacoes[-1].respondida = True
            self._fronteira = True
        self._marcou_agora_sem_numero = False

    # ── como aparece ────────────────────────────────────────────────────

    def rotulo_atual(self) -> str:
        """"Questão 7" — para prender a uma questão o que a visão leu nela."""
        if self.atual is None or self.atual[0] not in self.series:
            return ""
        return f"{self.series[self.atual[0]].nome} {self.atual[1]}"

    def _placar_sem_numero(self) -> str:
        enviadas = sum(1 for m in self.marcacoes if m.respondida)
        return (
            f"{len(self.marcacoes)} de {self.total_pedido} questões marcadas "
            f"({enviadas} com envio confirmado)"
        )

    def resumo(self) -> str:
        """Uma linha: "Questão 7 de 10 · 6 respondidas, 1 só marcada"."""
        if self._sem_contador():
            return self._placar_sem_numero()
        partes: list[str] = []
        for chave, serie in self.series.items():
            onde = (
                f"{serie.nome} {self.atual[1]} de {serie.total}"
                if self.atual and self.atual[0] == chave
                else f"{serie.nome}: {serie.total} no total"
            )
            conta = f"{len(serie.respondidas())} respondida(s)"
            marcadas = len(serie.so_marcadas())
            if marcadas:
                conta += f", {marcadas} só marcada(s)"
            partes.append(f"{onde} · {conta}")
        return " | ".join(partes)

    def linhas(self, *, ao_vivo: bool = False) -> list[str]:
        """O bloco do caderno: o placar por série e uma linha por questão vista."""
        if self._sem_contador():
            saida = [
                f"Progresso — {self._placar_sem_numero()}. A página não numera as "
                "questões: o total é o do pedido, a contagem é do que o navegador "
                "confirmou marcado."
            ]
            for n, m in enumerate(self.marcacoes, 1):
                estado = "✓ respondida" if m.respondida else "◐ marcada, envio não confirmado"
                saida.append(f"- {n}ª marcação {estado} — marquei «{m.rotulo}»")
            return saida
        saida: list[str] = []
        for chave, serie in self.series.items():
            respondidas = serie.respondidas()
            cabeca = f"Progresso — {serie.nome}: {len(respondidas)} de {serie.total} respondidas"
            marcadas = serie.so_marcadas()
            if marcadas:
                cabeca += f", {len(marcadas)} marcada(s) sem envio confirmado"
            if ao_vivo and self.atual and self.atual[0] == chave:
                cabeca += f" · agora na {serie.nome} {self.atual[1]}"
            saida.append(cabeca)
            for numero in sorted(serie.itens):
                item = serie.itens[numero]
                saida.append(f"- {serie.nome} {numero} {self._estado(item)}")
            faltam = serie.nao_vistas()
            if faltam:
                saida.append(f"- Não vistas nesta tarefa: {_faixas(faltam)}")
        return saida

    @staticmethod
    def _estado(item: ItemDoProgresso) -> str:
        if not item.marcada:
            return "○ aberta, nada marcado"
        como = {
            "marquei": "marquei",
            "já estava": "já estava marcada",
            "na página": "a página mostra marcada",
        }.get(item.como, item.como)
        if item.respondida:
            return f"✓ respondida — {como} «{item.marcada}»"
        # "Envio não confirmado", e não "não enviada": no job 50227807 a última
        # escolha fez a página carregar o resultado e a tarefa acabou antes de
        # ela trocar — pode ter ido, só não há prova.
        return f"◐ marcada, envio não confirmado — {como} «{item.marcada}»"

    def para_o_prompt(self) -> str:
        """O placar compacto para o agente: números, não a lista inteira."""
        if self._sem_contador():
            return (
                "SEU PROGRESSO (registrado por mim, com o que o navegador confirmou — NÃO "
                "anote isto no caderno): a página não numera as questões; o pedido fala em "
                f"{self.total_pedido}, e até agora {self._placar_sem_numero()}."
            )
        if not self.series:
            return ""
        partes: list[str] = []
        for chave, serie in self.series.items():
            linha = f"{serie.nome} — {serie.total} no total."
            if serie.respondidas():
                linha += f" Respondidas: {_faixas(serie.respondidas())}."
            if serie.so_marcadas():
                linha += f" Marcadas, envio não confirmado: {_faixas(serie.so_marcadas())}."
            if serie.abertas():
                linha += f" Abertas sem nada marcado: {_faixas(serie.abertas())}."
            if serie.nao_vistas():
                linha += f" Ainda não vistas nesta tarefa: {_faixas(serie.nao_vistas())}."
            if self.atual and self.atual[0] == chave:
                item = serie.itens.get(self.atual[1])
                linha += f" Você está na {serie.nome} {self.atual[1]}"
                linha += f", com «{item.marcada[:120]}» marcada." if item and item.marcada else "."
            partes.append(linha)
        return (
            "SEU PROGRESSO (registrado por mim, com o contador da página e o que o "
            "navegador confirmou — NÃO anote isto no caderno):\n" + "\n".join(partes)
        )


def ordenar_por_numero(notas: list[str]) -> list[str]:
    """As notas na ordem das questões, quando o caderno é de questões numeradas.

    A ordem do caderno é a da COLETA, e a coleta começa onde a aba estava: no job
    f7794968 a página abriu na Questão 3 e o arquivo saiu Q3, Q1, Q2, Q4… Nota sem
    número acompanha a numerada que veio antes dela (a leitura do gráfico da Q7
    fica junto da Q7). Com menos de 3 numeradas, a ordem fica como está.
    """
    numeradas = sum(1 for n in notas if numero_da_nota(n) is not None)
    if numeradas < 3:
        return list(notas)
    chaves: list[tuple[int, int]] = []
    atual = -1
    for i, nota in enumerate(notas):
        numero = numero_da_nota(nota)
        if numero is not None:
            atual = numero
        chaves.append((atual, i))
    return [nota for _, nota in sorted(zip(chaves, notas), key=lambda par: par[0])]
