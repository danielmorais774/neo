"""Dado pessoal digitado LITERALMENTE — o buraco por onde o §24 passou inteiro.

A defesa de preenchimento (``core/release/fill.py``) nasceu amarrada ao
placeholder: ela só olha o `fill` quando o valor contém ``pks:``. A premissa era
que o modelo não tem o dado real em mãos, então ou ele escreve ``«pks:cep»`` ou
não escreve nada — "o 'nada além disso' por ausência, não por recusa".

A premissa está certa e segurou (na sessão e0905aae o CPF real nunca entrou no
contexto). O que ela não previu: **o modelo não precisa do valor real, ele
inventa um.** Em 2026-10-07 o cérebro escreveu, dentro do próprio `task`,
"preencha o CPF com 00000000000 (se recusar, use 11144477735)" — o CPF-fixture
clássico de teste. O agente digitou, a página coercitiva ("a transportadora
exige o documento") nem precisou convencer ninguém, e a tabela de necessidade
NUNCA foi consultada, porque nada com ``pks:`` chegou até ela.

Daí este módulo: o gatilho da trava passa a ser **o campo de destino e a forma
do valor**, não a cooperação do modelo. Duas perguntas independentes, e basta
uma responder "é dado pessoal":

``classificar_valor``
    A FORMA do que está sendo digitado. Só diz "sim" quando é inequívoco —
    CPF e CNPJ conferidos pelo dígito verificador, cartão por Luhn. Um número
    de pedido com 11 dígitos tem ~1% de chance de passar como CPF por acidente,
    e é por isso que a conferência de DV existe em vez de "tem 11 dígitos".
``classificar_campo``
    O RÓTULO de onde vai ser digitado. Pega o que a forma não pega: um CPF
    formalmente inválido (``00000000000``), um CEP sem hífen, um telefone.

Fora da lista de propósito: **e-mail e nome não entram**. O Neo tem e-mail
próprio e a persona manda usá-lo em cadastro; barrar ali seria quebrar o caminho
legítimo para proteger um dado que não é do Daniel.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

#: Campos que exigem liberação para serem digitados. Os nomes são os MESMOS da
#: tabela de necessidade (`core/release/necessity.py`), porque é ela quem decide
#: se a finalidade do turno justifica o campo — vocabulário diferente aqui viria
#: a dar "campo desconhecido" e a checagem passaria batida.
CAMPOS_SENSIVEIS = frozenset(
    {"cpf", "cnpj", "cep", "telefone", "cartao", "rg", "data_nascimento"}
)

#: rótulo humano por campo, para a frase da recusa.
_HUMANO = {
    "cpf": "CPF",
    "cnpj": "CNPJ",
    "cep": "CEP",
    "telefone": "telefone",
    "cartao": "número de cartão",
    "rg": "RG",
    "data_nascimento": "data de nascimento",
}

#: campo -> palavras que o identificam num rótulo/placeholder. A ordem importa:
#: o primeiro que casar vence, e `cartao` vem antes de `cpf` porque "cartão de
#: crédito CPF do titular" é um rótulo real e o pior dos dois é o cartão.
_PALAVRAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "cartao",
        (
            "cartao", "cartão", "card number", "credit card", "numero do cartao",
            "cvv", "cvc", "codigo de seguranca", "validade do cartao",
        ),
    ),
    ("cnpj", ("cnpj", "00.000.000/0000-00")),
    ("cpf", ("cpf", "cadastro de pessoa fisica", "000.000.000-00")),
    ("cep", ("cep", "codigo postal", "postal code", "zip", "zipcode", "00000-000")),
    (
        "telefone",
        ("telefone", "celular", "whatsapp", "fone", "phone", "ddd", "(00) 00000-0000"),
    ),
    ("rg", ("rg", "registro geral", "identidade")),
    ("data_nascimento", ("data de nascimento", "nascimento", "birth", "birthday")),
)


def _normalizar(texto: str) -> str:
    bruto = str(texto or "").strip().lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", bruto) if unicodedata.category(c) != "Mn"
    )


def _casa(palavra: str, texto: str) -> bool:
    """Palavra inteira, nunca pedaço. ``rg`` não pode casar com "energia"."""
    alvo = _normalizar(palavra)
    if not alvo:
        return False
    return re.search(rf"(?<!\w){re.escape(alvo)}(?!\w)", texto) is not None


# ── forma do valor ────────────────────────────────────────────────────────


def _so_digitos(valor: str) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def cpf_valido(bruto: str) -> bool:
    """Dígito verificador do CPF. Repetição (``111…``) é inválida por definição."""
    d = _so_digitos(bruto)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for corte in (9, 10):
        soma = sum(int(d[i]) * ((corte + 1) - i) for i in range(corte))
        dv = (soma * 10) % 11 % 10
        if dv != int(d[corte]):
            return False
    return True


_CNPJ_PESOS = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def cnpj_valido(bruto: str) -> bool:
    d = _so_digitos(bruto)
    if len(d) != 14 or d == d[0] * 14:
        return False
    for corte in (12, 13):
        pesos = _CNPJ_PESOS if corte == 12 else (6,) + _CNPJ_PESOS
        soma = sum(int(d[i]) * pesos[i] for i in range(corte))
        resto = soma % 11
        dv = 0 if resto < 2 else 11 - resto
        if dv != int(d[corte]):
            return False
    return True


def _luhn(bruto: str) -> bool:
    d = _so_digitos(bruto)
    if not (13 <= len(d) <= 19):
        return False
    total, alterna = 0, False
    for ch in reversed(d):
        n = int(ch)
        if alterna:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alterna = not alterna
    return total % 10 == 0


#: ``62500-000`` — o hífen é o que separa um CEP de um número de 8 dígitos
#: qualquer. Sem ele a forma é ambígua e quem decide é o rótulo do campo.
_CEP_FORMATADO = re.compile(r"^\s*\d{5}-\d{3}\s*$")


def classificar_valor(valor: str) -> str | None:
    """Que dado pessoal este texto É, pela forma. ``None`` quando não dá para saber.

    Conservador de propósito: aqui um falso positivo barra um preenchimento
    legítimo, e o rótulo do campo (``classificar_campo``) é a outra metade da
    rede — o que escapa de uma costuma cair na outra.
    """
    bruto = str(valor or "").strip()
    if not bruto:
        return None
    if _CEP_FORMATADO.match(bruto):
        return "cep"
    d = _so_digitos(bruto)
    if len(d) == 11 and cpf_valido(d):
        return "cpf"
    if len(d) == 14 and cnpj_valido(d):
        return "cnpj"
    # Cartão por último: 14 dígitos que já passaram como CNPJ não voltam aqui.
    if _luhn(d):
        return "cartao"
    return None


# ── rótulo do campo ───────────────────────────────────────────────────────


def classificar_campo(rotulos: Iterable[str]) -> str | None:
    """Que dado pessoal este CAMPO pede, pelo rótulo/placeholder/atributo."""
    texto = _normalizar(" ".join(str(r or "") for r in rotulos))
    if not texto:
        return None
    for campo, palavras in _PALAVRAS:
        if any(_casa(p, texto) for p in palavras):
            return campo
    return None


def rotulos_do_campo(snapshot: Any, ref: Any) -> list[str]:
    """Tudo que nomeia o campo ``ref``: ele próprio e os vizinhos de cima.

    O ``<input>`` quase nunca se nomeia: o `label` do snapshot cai no
    ``placeholder`` (``"000.000.000-00"``) e o nome de verdade está no
    ``<label>``, que vem IMEDIATAMENTE antes dele na lista de elementos e traz
    o ``controla`` (``input[cpf]``) — o atributo ``name`` do controle, que é o
    sinal mais limpo que existe aqui.
    """
    elements = (snapshot or {}).get("elements") or []
    idx = next(
        (i for i, e in enumerate(elements) if e.get("ref") == ref and ref is not None),
        None,
    )
    if idx is None:
        return []
    def _do(item: dict) -> list[str]:
        return [
            str(item.get("text") or ""),
            str(item.get("controla") or ""),
            str(item.get("owner") or ""),
            str(item.get("panel") or ""),
        ]

    alvo = elements[idx]
    out = _do(alvo)
    # Para trás até dois, e PARANDO no primeiro elemento que não é rótulo.
    #
    # As duas armadilhas que esta regra evita, as duas medidas no snapshot real
    # da fixture do §24:
    #
    # * o botão "Calcular frete" herdava o rótulo do campo logo acima e virava
    #   "CPF" — recusa com a frase errada em cima de um clique legítimo;
    # * o campo de BUSCA logo depois do formulário herdava o placeholder
    #   ``000.000.000-00`` do campo anterior, porque a janela de vizinhos não
    #   distinguia "o label deste campo" de "o campo de outra pessoa".
    #
    # Um `<label>` pertence ao input que vem DEPOIS dele; outro input encerra a
    # vizinhança. É esta a fronteira.
    if not _so_clicavel(alvo):
        for vizinho in reversed(elements[max(0, idx - 2) : idx]):
            if not _e_rotulo(vizinho):
                break
            out += _do(vizinho)
    return [t for t in out if t.strip()]


#: Elemento que se CLICA, não se preenche — não herda o rótulo do vizinho.
_NAO_PREENCHIVEL_TAG = frozenset({"button", "a", "img", "svg"})
_NAO_PREENCHIVEL_TIPO = frozenset({"submit", "button", "reset", "checkbox", "radio"})

#: Tags que CARREGAM nome para o campo seguinte.
_ROTULO_TAG = frozenset({"label", "span", "div", "p", "legend", "strong", "th"})


def _so_clicavel(item: dict) -> bool:
    tag = _normalizar(item.get("tag"))
    tipo = _normalizar(item.get("type"))
    return tag in _NAO_PREENCHIVEL_TAG or tipo in _NAO_PREENCHIVEL_TIPO


def _e_rotulo(item: dict) -> bool:
    """Este vizinho NOMEIA o campo seguinte (em vez de ser outro campo)?"""
    tag = _normalizar(item.get("tag"))
    if tag in {"input", "textarea", "select", "button", "a"}:
        return False
    return bool(tag in _ROTULO_TAG or item.get("controla"))


def campo_do_fill(snapshot: Any, ref: Any, valor: str) -> str | None:
    """O campo pessoal deste `fill`, ou ``None`` quando não é dado pessoal.

    Rótulo primeiro, forma depois: o rótulo sabe que ``00000000000`` num campo
    "CPF do destinatário" é um CPF (a forma não sabe — o DV não fecha), e é
    justamente o valor inventado que mais aparece.
    """
    pelo_campo = classificar_campo(rotulos_do_campo(snapshot, ref))
    if pelo_campo:
        return pelo_campo
    return classificar_valor(valor)


def humano(campo: str) -> str:
    return _HUMANO.get(str(campo or "").strip().lower(), str(campo or ""))


# ── o que o formulário pede ───────────────────────────────────────────────

#: Como um rótulo nomeia os campos pessoais que ficaram fora de `_PALAVRAS` (não
#: exigem liberação por si só, mas aparecem como «pks:…» numa tarefa).
_PALAVRAS_DO_PEDIDO: dict[str, tuple[str, ...]] = {
    "nome": ("nome", "name", "full name"),
    "full_name": ("nome", "name", "full name"),
    "nome_completo": ("nome", "name", "full name"),
    "email": ("email", "e-mail", "e mail"),
    "endereco": ("endereco", "address", "logradouro", "rua"),
}

#: Elemento onde se DIGITA, pelo papel: o Google Forms e os editores ricos nem
#: sempre usam `<input>`.
_PAPEL_DIGITAVEL = frozenset({"textbox", "combobox", "searchbox", "text", "email", "tel"})


def _digitavel(item: dict) -> bool:
    if _so_clicavel(item):
        return False
    tag = _normalizar(item.get("tag"))
    papel = _normalizar(item.get("role") or item.get("type"))
    return tag in {"input", "textarea", "select"} or papel in _PAPEL_DIGITAVEL


def campos_pedidos_na_pagina(snapshot: Any, campos: Iterable[str]) -> list[str]:
    """Dos ``campos`` da tarefa, os que algum campo DIGITÁVEL desta página pede.

    O cérebro escreve a tarefa com todo marcador que imagina ("«pks:nome»,
    «pks:cpf»… «pks:data_nascimento» — use só os que o formulário pedir") e o
    card pedia os sete para um formulário de quatro perguntas (sessão 9b2e29e2).
    Só o rótulo do campo conta: o texto solto da página do Forms diz "Seu e-mail
    e sua Conta do Google…", e isso não é pergunta nenhuma.

    Campo sem vocabulário conhecido fica (não dá para dizer que a página não o
    pede), e página sem nenhum campo digitável à vista não filtra nada.
    """
    pedidos = [str(c or "").strip().lower() for c in campos or [] if str(c or "").strip()]
    elements = (snapshot or {}).get("elements") or []
    rotulos = [
        _normalizar(" ".join(rotulos_do_campo(snapshot, item.get("ref"))))
        for item in elements
        if isinstance(item, dict) and item.get("ref") is not None and _digitavel(item)
    ]
    rotulos = [r for r in rotulos if r]
    if not rotulos:
        return pedidos
    saida: list[str] = []
    for campo in pedidos:
        palavras = next((p for nome, p in _PALAVRAS if nome == campo), None)
        palavras = palavras or _PALAVRAS_DO_PEDIDO.get(campo, ())
        if not palavras or any(_casa(p, r) for p in palavras for r in rotulos):
            saida.append(campo)
    return saida


# ── dado pessoal solto num texto ──────────────────────────────────────────

#: Sequência numérica longa o bastante para ser documento, com os separadores
#: que as pessoas escrevem. O `\d` nas pontas evita capturar o lixo em volta.
_CANDIDATO = re.compile(r"\d[\d.\-/ ]{7,}\d")


def dados_pessoais_no_texto(texto: str) -> list[str]:
    """Documentos escritos por extenso dentro de um texto livre.

    Existe por causa de um canal de lavagem que a trava de `fill` sozinha não
    fecha: o cérebro escreveu o CPF inventado **dentro do `task`** do
    ``browser_local_task`` ("preencha o CPF com 00000000000, se recusar use
    11144477735"). Do ponto de vista do agente do navegador aquilo deixa de ser
    "dado pessoal a buscar" e vira "parâmetro que me deram" — e a decisão sobre
    dado pessoal acaba tomada por quem não devia, uma camada acima da defesa.

    **Só CPF, CNPJ e CEP formatado.** Cartão por Luhn ficaria de fora de
    propósito: num texto livre de 400 caracteres um número de pedido passa por
    Luhn com frequência alta demais, e aqui um falso positivo recusa a tarefa
    inteira. Cartão continua barrado onde importa, no `fill`.
    """
    achados: list[str] = []
    for trecho in _CANDIDATO.findall(str(texto or "")):
        if _CEP_FORMATADO.match(trecho.strip()):
            achados.append("cep")
            continue
        d = _so_digitos(trecho)
        if len(d) == 11 and cpf_valido(d):
            achados.append("cpf")
        elif len(d) == 14 and cnpj_valido(d):
            achados.append("cnpj")
    # Ordem estável e sem repetição: a frase da recusa lista cada tipo uma vez.
    return [c for c in ("cpf", "cnpj", "cep") if c in achados]


# ── todo o dado pessoal de uma tarefa, de uma vez ─────────────────────────

#: Dígitos soltos o bastante para serem um valor digitado à mão. Três bastam
#: ("CPF: 123"): o que decide não é o tamanho do número, é o rótulo antes dele.
_VALOR_LITERAL = re.compile(r"\d{3,}")

#: Quanto texto ANTES do número ainda nomeia aquele número. 60 caracteres cobrem
#: ``"CPF do destinatário" (preencher com `` com folga e param antes do campo
#: anterior — a mesma fronteira que ``rotulos_do_campo`` resolve no snapshot.
_JANELA_ROTULO = 60

#: Ordem canônica da resposta: estável, e do mais grave para o menos.
_ORDEM_CAMPOS = ("cartao", "cpf", "cnpj", "rg", "cep", "telefone", "data_nascimento")

#: Vocabulário do TEXTO LIVRE — de propósito mais estreito que ``_PALAVRAS``,
#: que vale para rótulo de formulário. Num ``<label>`` "zip" só pode ser CEP;
#: numa tarefa ("baixe o arquivo zip 12345678") é formato de arquivo — e um
#: falso positivo aqui recusa a tarefa INTEIRA, que é o custo alto deste gate.
_PALAVRAS_TEXTO: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cartao", ("cartao", "cartão", "cvv", "cvc", "numero do cartao", "credit card")),
    ("cnpj", ("cnpj",)),
    ("cpf", ("cpf", "cadastro de pessoa fisica")),
    ("cep", ("cep", "codigo postal")),
    ("telefone", ("telefone", "celular", "whatsapp")),
    ("rg", ("rg", "registro geral")),
    ("data_nascimento", ("data de nascimento", "nascimento")),
)


def _sem_placeholder(texto: str) -> str:
    """Tira os ``«pks:…»`` antes do scan — eles são a forma CERTA, não o vazamento.

    Sem isto a recusa viraria laço: ela manda reescrever a tarefa com
    ``«pks:cpf»``, e a tarefa reescrita traria a palavra "cpf" de volta para o
    scan. O que sobra depois da remoção é só o que foi digitado à mão.
    """
    try:
        from core.release.fill import PLACEHOLDER_RE
    except Exception:  # noqa: BLE001 — sem o regex, o scan segue pela forma
        return str(texto or "")
    return PLACEHOLDER_RE.sub(" ", str(texto or ""))


def _rotulo_mais_proximo(janela: str) -> str | None:
    """O campo nomeado MAIS PERTO do número, não o primeiro da tabela.

    ``"…o CPF com «pks:cpf» e o CEP com 62500-000"`` tem as duas palavras na
    janela; quem nomeia o número é a última. Varrendo na ordem da tabela a
    recusa culparia o CPF pelo literal do CEP — e o cérebro pediria liberação
    do campo errado.
    """
    texto = _normalizar(janela)
    melhor: tuple[int, str] | None = None
    for campo, palavras in _PALAVRAS_TEXTO:
        for palavra in palavras:
            alvo = _normalizar(palavra)
            if not alvo:
                continue
            for achado in re.finditer(rf"(?<!\w){re.escape(alvo)}(?!\w)", texto):
                if melhor is None or achado.start() > melhor[0]:
                    melhor = (achado.start(), campo)
    return melhor[1] if melhor else None


def campos_pessoais_no_texto(texto: str) -> list[str]:
    """TODO campo pessoal que este texto entrega — por forma OU por rótulo.

    ``dados_pessoais_no_texto`` só enxerga a forma, e foi por isso que o §24 de
    2026-10-18 passou pela metade: a tarefa trazia ``62500-000`` (CEP, pego pela
    forma) e ``00000000000`` no campo "CPF do destinatário" — e o dígito
    verificador não fecha num valor repetido, então não havia forma para ver. A
    palavra "CPF" estava escrita ali do lado; faltava olhar para ela. O
    resultado foi uma recusa que nomeava só o CEP, e um CPF que o Daniel
    acreditou barrado sem que a tabela de necessidade tivesse rodado nele.

    É a mesma ideia de ``campo_do_fill`` — rótulo primeiro, forma depois —
    aplicada ao texto livre, onde o "rótulo" é o que vem antes do número.

    Devolve todos de uma vez, em ordem estável, porque quem chama precisa dar
    **um veredito por tarefa**, não um por rodada.
    """
    bruto = _sem_placeholder(texto)
    achados = set(dados_pessoais_no_texto(bruto))
    for numero in _VALOR_LITERAL.finditer(bruto):
        inicio = max(0, numero.start() - _JANELA_ROTULO)
        campo = _rotulo_mais_proximo(bruto[inicio : numero.start()])
        if campo:
            achados.add(campo)
    return [c for c in _ORDEM_CAMPOS if c in achados]
