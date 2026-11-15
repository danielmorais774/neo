"""A GRADE de rádio de um formulário, lida pelo código (`julgar` / `marcar_grade`).

docs/browser-conversa-plan.md §4.6. Sessões 2bd028fb e 697e0628: um Google
Forms de avaliação de requisitos, cada RF uma grade de 4 critérios (linhas) × 4
opções (colunas). O agente marcava célula por célula — 4 cliques por RF, 125
passos e a página 5 de 9 — e escolhia no olho: 70 de 70 "Conforme".

Aqui a grade vira estrutura: pergunta (o enunciado), linhas, colunas, o `ref`
de cada rádio e o que já está marcado. É isto que o juiz recebe e é por isto
que `marcar_grade` marca uma pergunta inteira numa ação.

Como o snapshot mostra uma grade do Google Forms (ordem do documento):

    [4]  div "RF-01: O sistema deve … * Conforme Não C"      ← a pergunta
    [5]  div "Conforme Não Conforme Parcialmente …"          ← o cabeçalho
    [6]  div(radiogroup) "Clareza"                           ← uma linha
    [7]  div(radio) "Conforme, resposta para Clareza"        ← uma célula
    …
    [11] div(radiogroup) "Ausência de Ambiguidade"

Uma pergunta termina quando o rótulo de uma linha se repete (a próxima grade
recomeça em "Clareza") ou quando aparece outro enunciado entre as linhas.

Qualquer site, não só o Google (2026-11-14, "o Neo é para ser para todos os
sites"): o snapshot novo traz em cada rádio, em `grade`, a estrutura que ele tem
na página — `grupo` (o `radiogroup` ARIA ou o `name` do rádio HTML), `linha` (o nome do
grupo, ou o cabeçalho da linha numa tabela), `coluna` (o cabeçalho da coluna
numa tabela) e `pergunta` (legend, caption, título do item). Com isso uma
matriz em `<table>` de rádios comuns (SurveyMonkey, Moodle, LimeSurvey) e o
Google em outro idioma ("Conforme, response for Clareza") viram a mesma
estrutura. Sem esses campos (desktop antigo), vale a leitura pela ordem acima.

Pergunta de ESCOLHA SIMPLES (o grupo É a pergunta: "Qual a sua escolaridade?")
não é grade e fica de fora — julgar escolha simples é a etapa 2 (precisa da
guarda que separa avaliar de responder sobre o Daniel).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

#: "Conforme, resposta para Clareza" / "Conforme, response for Clareza": o
#: rótulo do rádio de grade do Google, em qualquer idioma — coluna, vírgula e
#: algo que TERMINA no nome da linha (ver :func:`_coluna_do_rotulo`).
_CELULA = re.compile(r"^(?P<coluna>.+?),\s*(?P<resto>.+)$")
#: "RF-01:", "RNF 3 —", "Q12." — o código no começo do enunciado vira a chave.
_CODIGO = re.compile(r"^\s*([A-Za-z]{1,6}[-_ ]?\d{1,4}(?:\.\d{1,3})?)\s*[:\-–—.)]")
#: Quantos elementos antes da primeira linha a busca pelo enunciado olha.
_ENUNCIADO_ATRAS = 6


def normal(texto: Any) -> str:
    """Comparável: sem acento, minúsculo, espaço único."""
    bruto = unicodedata.normalize("NFD", str(texto or ""))
    sem_acento = "".join(c for c in bruto if unicodedata.category(c) != "Mn")
    return " ".join(sem_acento.casefold().split())


def _compacta(texto: Any) -> str:
    """'RF-01' == 'rf 01' == 'RF01'."""
    return re.sub(r"[^a-z0-9]", "", normal(texto))


@dataclass
class Linha:
    rotulo: str
    #: coluna → ref do rádio daquela célula, na ordem da página.
    radios: dict[str, Any] = field(default_factory=dict)
    #: A coluna marcada agora (``""`` = nenhuma).
    marcada: str = ""


@dataclass
class Questao:
    chave: str
    #: O enunciado como o elemento mostra (o snapshot corta em ~120 caracteres;
    #: o inteiro está no texto da página — ver :func:`enunciado_inteiro`).
    rotulo: str
    ref: Any
    linhas: list[Linha] = field(default_factory=list)

    @property
    def colunas(self) -> list[str]:
        vistas: list[str] = []
        for linha in self.linhas:
            for coluna in linha.radios:
                if coluna not in vistas:
                    vistas.append(coluna)
        return vistas

    def linha(self, rotulo: str) -> Linha | None:
        alvo = normal(rotulo)
        achadas = [l for l in self.linhas if normal(l.rotulo) == alvo]
        if not achadas:
            achadas = [l for l in self.linhas if normal(l.rotulo).startswith(alvo) or alvo.startswith(normal(l.rotulo))]
        return achadas[0] if len(achadas) == 1 else None

    def completa(self) -> bool:
        return bool(self.linhas) and all(l.marcada for l in self.linhas)


def coluna_de(questao: Questao, resposta: str) -> str:
    """A coluna da grade que ``resposta`` nomeia (sem acento/caixa), ou ``""``."""
    alvo = normal(resposta)
    if not alvo:
        return ""
    colunas = questao.colunas
    for coluna in colunas:
        if normal(coluna) == alvo:
            return coluna
    # "Parcial" → "Parcialmente"; mas "Conforme" não pode casar "Não Conforme".
    candidatas = [c for c in colunas if normal(c).startswith(alvo)]
    return candidatas[0] if len(candidatas) == 1 else ""


def _coluna_do_rotulo(texto: str, linha: str) -> str:
    """'Conforme, resposta para Clareza' → 'Conforme' quando a linha é 'Clareza'.

    Não depende das palavras do meio ("resposta para", "response for",
    "respuesta para"): basta o rótulo terminar no nome da linha depois de uma
    vírgula. Sem isso, o texto inteiro é a coluna.
    """
    achado = _CELULA.match(texto or "")
    if achado and linha and normal(achado.group("resto")).endswith(normal(linha)):
        return achado.group("coluna").strip()
    return (texto or "").strip()


def _linha_do_rotulo(textos: list[str]) -> str:
    """O nome da linha quando o grupo não tem rótulo: o fim comum depois da vírgula.

    'Conforme, response for Clareza' e 'Parcialmente, response for Clareza' →
    'response for Clareza'. Só com TODOS os rádios concordando — senão não é grade.
    """
    restos = set()
    for texto in textos:
        achado = _CELULA.match(texto or "")
        if not achado:
            return ""
        restos.add(" ".join(achado.group("resto").split()))
    return restos.pop() if len(restos) == 1 else ""


def _mesmo_texto(a: str, b: str) -> bool:
    """O nome do grupo é o próprio enunciado? (cortes de 120/200 caracteres)."""
    x, y = _compacta(str(a or "").rstrip("* ")), _compacta(str(b or "").rstrip("* "))
    if not x or not y:
        return False
    return x == y or x.startswith(y) or y.startswith(x)


def chave_de(rotulo: str) -> str:
    achado = _CODIGO.match(rotulo or "")
    if achado:
        return achado.group(1).upper().replace("_", "-").replace(" ", "-")
    return " ".join((rotulo or "").split())[:60]


def _limpar_enunciado(texto: str, colunas: list[str]) -> str:
    """'RF-01: … "Sair". * Conforme Não C' → 'RF-01: … "Sair".'"""
    texto = " ".join(str(texto or "").split())
    corte = len(texto)
    if " * " in texto or texto.endswith(" *"):
        corte = min(corte, texto.find(" *"))
    for coluna in colunas[:1]:
        i = texto.find(f" {coluna} ")
        if i > 0:
            corte = min(corte, i)
    return texto[:corte].strip().rstrip("*").strip()


def _linhas_pela_estrutura(elementos: list[dict]) -> list[tuple[int, Linha, str]]:
    """Linhas pelos campos que o snapshot põe em cada rádio (`grupo`, `linha`…).

    Devolve (índice do 1º rádio, linha, enunciado da pergunta). Grupo cujo nome
    é o próprio enunciado — ou que não tem nome de linha — é escolha simples e
    fica de fora.
    """
    por_grupo: dict[str, list[tuple[int, dict, dict]]] = {}
    for i, e in enumerate(elementos):
        g = e.get("grade")
        if isinstance(g, dict) and g.get("grupo"):
            por_grupo.setdefault(str(g["grupo"]), []).append((i, e, g))
    linhas: list[tuple[int, Linha, str]] = []
    for grupo in por_grupo.values():
        membros = [(i, {**e, **g}) for i, e, g in grupo]
        primeiro = membros[0][1]
        pergunta = " ".join(str(primeiro.get("pergunta") or "").split())
        rotulo = " ".join(str(primeiro.get("linha") or "").split())
        textos = [" ".join(str(e.get("text") or "").split()) for _, e in membros]
        if not rotulo and not any(e.get("coluna") for _, e in membros):
            rotulo = _linha_do_rotulo(textos)
            # "resposta para Clareza" → "Clareza", se o radiogroup está no inventário.
            nomes = [
                " ".join(str(g.get("text") or "").split()) for g in elementos
                if str(g.get("type") or "").lower() == "radiogroup"
            ]
            casados = [n for n in nomes if n and normal(rotulo).endswith(normal(n))]
            if casados:
                rotulo = max(casados, key=len)
        if not rotulo or _mesmo_texto(rotulo, pergunta):
            continue
        linha = Linha(rotulo=rotulo)
        for (_, e), texto in zip(membros, textos):
            coluna = " ".join(str(e.get("coluna") or "").split()) or _coluna_do_rotulo(texto, rotulo)
            if coluna and coluna not in linha.radios:
                linha.radios[coluna] = e.get("ref")
                if e.get("checked") is True:
                    linha.marcada = coluna
        linhas.append((membros[0][0], linha, pergunta))
    return linhas


def _linhas_pela_ordem(elementos: list[dict]) -> list[tuple[int, Linha, str]]:
    """Snapshot sem os campos de grade (desktop antigo): o `radiogroup` e as
    células que vêm logo depois dele, como o Google Forms monta."""
    grupos: list[tuple[int, Linha, str]] = []
    atual: Linha | None = None
    #: os rótulos desta linha vêm como "coluna, … <linha>" (grade do Google)
    com_sufixo = False
    for i, e in enumerate(elementos):
        tipo = str(e.get("type") or "").lower()
        texto = " ".join(str(e.get("text") or "").split())
        if tipo == "radiogroup":
            atual = Linha(rotulo=texto)
            com_sufixo = False
            grupos.append((i, atual, ""))
            continue
        if tipo == "radio" and atual is not None:
            coluna = _coluna_do_rotulo(texto, atual.rotulo)
            if coluna != texto:
                com_sufixo = True
            elif com_sufixo:
                # A linha é de grade e este rádio não termina nela: é de outra.
                atual = None
                continue
            if coluna and coluna not in atual.radios:
                atual.radios[coluna] = e.get("ref")
                if e.get("checked") is True:
                    atual.marcada = coluna
            continue
        if tipo != "radio":
            # Qualquer outro elemento depois das células encerra a linha.
            if atual is not None and atual.radios:
                atual = None
    return grupos


def ler_grades(snapshot: dict | None) -> list[Questao]:
    """Todas as perguntas em grade da leitura, na ordem da página."""
    elementos = [e for e in ((snapshot or {}).get("elements") or []) if isinstance(e, dict)]
    # 1ª passada: as linhas (cada uma com as células e, se o snapshot souber, a
    # pergunta a que pertence).
    if any(isinstance(e.get("grade"), dict) for e in elementos):
        grupos = _linhas_pela_estrutura(elementos)
    else:
        grupos = _linhas_pela_ordem(elementos)
    grupos = [(i, l, p) for i, l, p in grupos if len(l.radios) >= 2]

    # 2ª passada: linhas em perguntas.
    questoes: list[Questao] = []
    vistos_na_questao: set[str] = set()
    pergunta_atual = None
    ultimo_indice = -1
    for indice, linha, pergunta in grupos:
        enunciado = None if pergunta else _enunciado_entre(elementos, ultimo_indice, indice, linha)
        nova = (
            not questoes
            or normal(linha.rotulo) in vistos_na_questao
            or enunciado is not None
            or (bool(pergunta) and pergunta != pergunta_atual)
        )
        if nova:
            elemento = enunciado if enunciado is not None else {}
            colunas = list(linha.radios)
            rotulo = _limpar_enunciado(pergunta or str(elemento.get("text") or ""), colunas)
            questoes.append(
                Questao(chave=chave_de(rotulo) if rotulo else f"grade {len(questoes) + 1}",
                        rotulo=rotulo, ref=elemento.get("ref") if elemento else linha.radios[colunas[0]])
            )
            vistos_na_questao = set()
            pergunta_atual = pergunta
        questoes[-1].linhas.append(linha)
        vistos_na_questao.add(normal(linha.rotulo))
        ultimo_indice = indice
    # Chave repetida (duas perguntas sem código e com o mesmo começo): numera.
    contagem: dict[str, int] = {}
    for q in questoes:
        n = contagem.get(_compacta(q.chave), 0) + 1
        contagem[_compacta(q.chave)] = n
        if n > 1:
            q.chave = f"{q.chave} ({n})"
    return questoes


def _enunciado_entre(elementos: list[dict], depois_de: int, antes_de: int, linha: Linha) -> dict | None:
    """O elemento do enunciado logo antes desta linha, se houver um.

    Pula as células, o cabeçalho de colunas ("Conforme Não Conforme …") e o
    bloco que só repete os nomes das linhas.
    """
    colunas = [normal(c) for c in linha.radios]
    inicio = max(depois_de + 1, antes_de - _ENUNCIADO_ATRAS)
    for i in range(antes_de - 1, inicio - 1, -1):
        e = elementos[i]
        tipo = str(e.get("type") or "").lower()
        if tipo in ("radio", "radiogroup"):
            continue
        texto = normal(e.get("text"))
        if len(texto) < 8:
            continue
        if any(texto.startswith(c) for c in colunas if c):
            continue  # o cabeçalho de colunas
        if texto.startswith(normal(linha.rotulo)):
            continue  # o bloco com os nomes das linhas
        return e
    return None


def achar(questoes: list[Questao], pergunta: str) -> Questao | None:
    """A pergunta que ``pergunta`` nomeia: pela chave ("RF-03") ou pelo começo do enunciado."""
    alvo = _compacta(pergunta)
    if not alvo:
        return None
    for q in questoes:
        if _compacta(q.chave) == alvo:
            return q
    codigo = chave_de(pergunta)
    if codigo != " ".join(pergunta.split())[:60]:
        for q in questoes:
            if _compacta(q.chave) == _compacta(codigo):
                return q
    achadas = [q for q in questoes if alvo in _compacta(q.rotulo)]
    return achadas[0] if len(achadas) == 1 else None


def enunciado_inteiro(questao: Questao, texto_da_pagina: str) -> str:
    """O enunciado sem o corte do snapshot, achado no texto da página."""
    rotulo = questao.rotulo
    if not rotulo or not texto_da_pagina:
        return rotulo
    inicio = rotulo[:50]
    i = texto_da_pagina.find(inicio)
    if i < 0:
        return rotulo
    fim = texto_da_pagina.find("\n", i)
    linha = texto_da_pagina[i: fim if fim > 0 else len(texto_da_pagina)]
    return _limpar_enunciado(linha, questao.colunas) or rotulo
