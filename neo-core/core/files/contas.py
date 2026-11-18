"""As contas de uma nota (ou de uma planilha) fecham? E, se não, onde a leitura errou?

Determinístico de propósito, sem LLM: na session-3b19d27a o modelo errou justamente
a aritmética. A visão leu a vodka como "1 × 39,99 = 33,99"; os itens só fecham com
o "Valor a pagar" de 213,74 se o total dela for 39,99 (com 33,99 dão 207,74), e o
modelo escreveu o contrário, gravou 33,99 na planilha e disse "fecha em 213,74".

Duas portas:

* ``conferir(itens, totais)``: a conferência de uma nota já transcrita (a tool
  ``conferir_cupom`` da skill nota-fiscal);
* ``conferir_planilha(caminho)``: a rede de segurança do ``file_save`` de .xlsx.
  Lê o XML do arquivo com a biblioteca padrão (o openpyxl não está no core) e
  calcula ela mesma as fórmulas simples, porque quem gera com openpyxl grava a
  fórmula sem o valor.

O que ela faz, nesta ordem:

1. **Linha:** quantidade × unitário contra o total da linha (tolerância de um
   centavo, arredondando para cima ou truncando, que é o que os PDVs fazem).
2. **Fechamento:** itens − descontos + acréscimos contra o valor a pagar;
   pagamentos − troco contra o valor a pagar; linhas lidas contra a "Qtde total
   de itens".
3. **Dedução:** a diferença do fechamento igual à diferença de uma linha são duas
   provas independentes da mesma leitura errada (confiança alta). Sem isso, testa
   a troca de UM dígito nos totais, com as confusões comuns de OCR (3/8/9, 0/8,
   1/7, 5/6...), e lista as que fecham a conta.
4. **Cobertura:** fecha e a contagem bate → todas as linhas foram lidas. Era a
   certeza que o modelo buscou por 22 rodadas relendo a nota em faixas.
"""

from __future__ import annotations

import ast
import operator
import posixpath
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

CENTAVO = Decimal("0.01")
#: Folga de comparação: meio centavo cobre a representação; arredondamento de
#: item por peso fica na checagem da linha, que aceita um centavo.
_FOLGA = Decimal("0.005")

#: Dígitos que o OCR troca entre si numa nota térmica (mão dupla).
_CONFUSOES: dict[str, str] = {
    "0": "689",
    "1": "47",
    "2": "7",
    "3": "5689",
    "4": "19",
    "5": "36",
    "6": "0358",
    "7": "12",
    "8": "03569",
    "9": "0348",
}


# ── números ─────────────────────────────────────────────────────────────────

def numero(valor: Any) -> Decimal | None:
    """Decimal de "39,99", "1.234,56", "R$ 213,74", 39.99 ou 2; None se não for número."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, Decimal):
        return valor
    if isinstance(valor, (int, float)):
        try:
            return Decimal(str(valor))
        except InvalidOperation:
            return None
    texto = str(valor).strip().replace("R$", "").replace(" ", "").replace(" ", "")
    if not texto:
        return None
    negativo = texto.startswith("-") or (texto.startswith("(") and texto.endswith(")"))
    texto = texto.strip("-()")
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", texto):
        texto = texto.replace(".", "")
    if not re.fullmatch(r"\d+(\.\d+)?", texto):
        return None
    try:
        n = Decimal(texto)
    except InvalidOperation:
        return None
    return -n if negativo else n


def brl(valor: Decimal | None) -> str:
    """213.74 → "213,74"; 1234.5 → "1.234,50"."""
    if valor is None:
        return "?"
    q = valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)
    inteiro, _, cent = f"{abs(q):,.2f}".partition(".")
    return ("-" if q < 0 else "") + inteiro.replace(",", ".") + "," + cent


def _qtd(valor: Decimal) -> str:
    if valor == valor.to_integral_value():
        return str(int(valor))
    return format(valor.normalize(), "f").replace(".", ",")


# ── a nota ──────────────────────────────────────────────────────────────────

@dataclass
class Item:
    descricao: str
    total: Decimal
    qtd: Decimal | None = None
    unitario: Decimal | None = None
    codigo: str = ""
    #: Onde a linha está, para o aviso apontar ("linha 12" da planilha). Vazio
    #: numa nota: aí o aviso usa a posição na lista.
    onde: str = ""


@dataclass
class Totais:
    valor_a_pagar: Decimal | None = None
    qtde_itens: int | None = None
    pagamentos: list[Decimal] = field(default_factory=list)
    troco: Decimal | None = None
    descontos: Decimal | None = None
    acrescimos: Decimal | None = None


@dataclass
class Correcao:
    item: int              # índice em `itens`
    de: Decimal
    para: Decimal
    confianca: str         # "alta" | "media" | "baixa"
    motivo: str


@dataclass
class Veredito:
    fecha: bool | None                       # None: sem valor a pagar para comparar
    cobre: bool | None                       # todas as linhas lidas?
    soma_itens: Decimal
    diferenca: Decimal | None                # valor a pagar − soma líquida
    linhas: list[tuple[int, Decimal, Decimal]]   # (índice, esperado, lido)
    pagamentos_batem: bool | None
    correcoes: list[Correcao]
    avisos: list[str]
    texto: str

    @property
    def ok(self) -> bool:
        return not self.linhas and self.fecha is not False and self.pagamentos_batem is not False and not self.avisos


def _esperados(item: Item) -> list[Decimal]:
    """O total da linha pelo cálculo: arredondado e truncado (os PDVs fazem os dois)."""
    if item.qtd is None or item.unitario is None:
        return []
    bruto = item.qtd * item.unitario
    return [bruto.quantize(CENTAVO, rounding=ROUND_HALF_UP), bruto.quantize(CENTAVO, rounding=ROUND_DOWN)]


def _linha_bate(item: Item) -> tuple[bool, Decimal | None]:
    esperados = _esperados(item)
    if not esperados:
        return True, None
    if any(abs(e - item.total) <= CENTAVO for e in esperados):
        return True, esperados[0]
    return False, esperados[0]


def _variantes(valor: Decimal) -> Iterable[Decimal]:
    """O valor com UM dígito trocado por um que o OCR confunde com ele."""
    texto = f"{valor.quantize(CENTAVO, rounding=ROUND_HALF_UP):.2f}"
    for i, c in enumerate(texto):
        for troca in _CONFUSOES.get(c, ""):
            novo = texto[:i] + troca + texto[i + 1:]
            try:
                yield Decimal(novo)
            except InvalidOperation:
                continue


def _rotulo(itens: list[Item], i: int) -> str:
    it = itens[i]
    nome = (it.descricao or it.codigo or "item").strip()
    if len(nome) > 40:
        nome = nome[:39] + "…"
    onde = it.onde or f"item {i + 1}"
    return f"{nome} ({onde})"


def conferir(itens: list[Item], totais: Totais) -> Veredito:
    """Confere a nota transcrita e deduz onde a leitura errou. Nunca levanta."""
    avisos: list[str] = []
    linhas: list[tuple[int, Decimal, Decimal]] = []
    for i, it in enumerate(itens):
        bate, esperado = _linha_bate(it)
        if not bate and esperado is not None:
            linhas.append((i, esperado, it.total))

    soma = sum((it.total for it in itens), Decimal("0"))
    liquida = soma - (totais.descontos or 0) + (totais.acrescimos or 0)
    diferenca: Decimal | None = None
    fecha: bool | None = None
    if totais.valor_a_pagar is not None:
        diferenca = totais.valor_a_pagar - liquida
        fecha = abs(diferenca) <= _FOLGA

    pagamentos_batem: bool | None = None
    pago_liquido: Decimal | None = None
    if totais.pagamentos:
        pago_liquido = sum(totais.pagamentos, Decimal("0")) - (totais.troco or 0)
        if totais.valor_a_pagar is not None:
            pagamentos_batem = abs(pago_liquido - totais.valor_a_pagar) <= _FOLGA

    contagem_bate: bool | None = None
    if totais.qtde_itens is not None:
        contagem_bate = totais.qtde_itens == len(itens)
        if not contagem_bate:
            avisos.append(
                f"Foram lidas {len(itens)} linhas de item, e a nota diz "
                f"{totais.qtde_itens} itens: "
                + ("falta ler linha." if len(itens) < totais.qtde_itens else "há linha a mais (lida duas vezes?).")
            )

    correcoes = _deduzir(itens, totais, linhas, diferenca, fecha, pago_liquido, liquida)

    cobre: bool | None = None
    if fecha is not None or contagem_bate is not None:
        explicada = any(c.confianca == "alta" for c in correcoes)
        cobre = bool((fecha or explicada) and contagem_bate is not False)

    texto = _texto(itens, totais, linhas, soma, liquida, diferenca, fecha,
                   pagamentos_batem, pago_liquido, correcoes, avisos, cobre)
    return Veredito(
        fecha=fecha, cobre=cobre, soma_itens=soma, diferenca=diferenca, linhas=linhas,
        pagamentos_batem=pagamentos_batem, correcoes=correcoes, avisos=avisos, texto=texto,
    )


def _deduzir(
    itens: list[Item],
    totais: Totais,
    linhas: list[tuple[int, Decimal, Decimal]],
    diferenca: Decimal | None,
    fecha: bool | None,
    pago_liquido: Decimal | None,
    liquida: Decimal,
) -> list[Correcao]:
    correcoes: list[Correcao] = []
    # Linha que não bate, mas o fechamento bate: o total da linha está certo e
    # quem foi mal lido é o unitário (ou a quantidade).
    if fecha:
        for i, esperado, lido in linhas:
            it = itens[i]
            novo_unit = (lido / it.qtd).quantize(CENTAVO, rounding=ROUND_HALF_UP) if it.qtd else None
            if novo_unit is not None and it.unitario is not None:
                correcoes.append(Correcao(
                    item=i, de=it.unitario, para=novo_unit, confianca="media",
                    motivo="a soma fecha com o total desta linha, então o unitário (ou a quantidade) é que foi mal lido",
                ))
        return correcoes
    if diferenca is None:
        return correcoes

    # Duas provas independentes: a linha erra pelo mesmo valor que falta no fechamento.
    for i, esperado, lido in linhas:
        if abs((esperado - lido) - diferenca) <= _FOLGA:
            correcoes.append(Correcao(
                item=i, de=lido, para=esperado, confianca="alta",
                motivo=(
                    f"a linha erra por {brl(esperado - lido)} e o fechamento também: "
                    "quantidade × unitário dá o total que fecha a nota"
                ),
            ))
    if correcoes:
        return correcoes

    # O valor a pagar é que foi mal lido: os pagamentos batem com os itens.
    if pago_liquido is not None and abs(pago_liquido - liquida) <= _FOLGA:
        return [Correcao(
            item=-1, de=totais.valor_a_pagar or Decimal("0"), para=liquida, confianca="alta",
            motivo="os itens e os pagamentos dão o mesmo valor; o valor a pagar é que foi mal lido",
        )]

    # Troca de um dígito que fecha a conta.
    candidatos: list[Correcao] = []
    for i, it in enumerate(itens):
        for alt in _variantes(it.total):
            if abs((alt - it.total) - diferenca) > _FOLGA:
                continue
            esperados = _esperados(it)
            confere = any(abs(e - alt) <= CENTAVO for e in esperados)
            candidatos.append(Correcao(
                item=i, de=it.total, para=alt,
                confianca="alta" if confere else "media",
                motivo=(
                    "um dígito trocado na leitura"
                    + (", e quantidade × unitário confirma" if confere else "")
                ),
            ))
    if totais.valor_a_pagar is not None:
        for alt in _variantes(totais.valor_a_pagar):
            if abs(alt - liquida) <= _FOLGA:
                candidatos.append(Correcao(
                    item=-1, de=totais.valor_a_pagar, para=alt, confianca="media",
                    motivo="um dígito trocado na leitura do valor a pagar",
                ))
    if len(candidatos) > 1:
        # Várias trocas fecham a conta: nenhuma é prova sozinha, salvo a única
        # confirmada pela própria linha.
        altas = [c for c in candidatos if c.confianca == "alta"]
        if len(altas) == 1:
            return altas
        for c in candidatos:
            c.confianca = "baixa"
    return candidatos


def _texto(
    itens: list[Item],
    totais: Totais,
    linhas: list[tuple[int, Decimal, Decimal]],
    soma: Decimal,
    liquida: Decimal,
    diferenca: Decimal | None,
    fecha: bool | None,
    pagamentos_batem: bool | None,
    pago_liquido: Decimal | None,
    correcoes: list[Correcao],
    avisos: list[str],
    cobre: bool | None,
) -> str:
    partes: list[str] = []
    for i, esperado, lido in linhas:
        it = itens[i]
        partes.append(
            f"{_rotulo(itens, i)}: {_qtd(it.qtd)} × {brl(it.unitario)} = {brl(esperado)}, "
            f"mas o total lido é {brl(lido)} (diferença {brl(esperado - lido)})."
        )
    ajuste = ""
    if totais.descontos:
        ajuste += f" − descontos {brl(totais.descontos)}"
    if totais.acrescimos:
        ajuste += f" + acréscimos {brl(totais.acrescimos)}"
    if totais.valor_a_pagar is not None:
        if fecha:
            partes.append(
                f"Os {len(itens)} itens somam {brl(soma)}"
                + (f"{ajuste} = {brl(liquida)}" if ajuste else "")
                + f": fecha com o valor a pagar ({brl(totais.valor_a_pagar)})."
            )
        else:
            partes.append(
                f"Os {len(itens)} itens somam {brl(soma)}"
                + (f"{ajuste} = {brl(liquida)}" if ajuste else "")
                + ", mas o valor a pagar "
                f"é {brl(totais.valor_a_pagar)} (diferença {brl(diferenca)})."
            )
    if pagamentos_batem is False and pago_liquido is not None:
        partes.append(
            f"Os pagamentos dão {brl(pago_liquido)}"
            + (" (já tirado o troco)" if totais.troco else "")
            + f", diferente do valor a pagar {brl(totais.valor_a_pagar)}."
        )
    partes.extend(avisos)
    for c in correcoes:
        alvo = "Valor a pagar" if c.item < 0 else _rotulo(itens, c.item)
        partes.append(
            f"CORREÇÃO ({c.confianca}): {alvo}: {brl(c.de)} → {brl(c.para)}, porque {c.motivo}."
        )
    if fecha is False and not correcoes and diferenca is not None:
        partes.append(
            f"Nenhuma leitura isolada explica a diferença de {brl(diferenca)}: "
            + ("pode faltar uma linha de item com esse valor, " if diferenca > 0 else "")
            + "releia só as linhas de item e o valor a pagar."
        )
    if cobre:
        partes.append(
            "Todas as linhas de item foram lidas"
            + (f" (bate com 'Qtde total de itens: {totais.qtde_itens}')" if totais.qtde_itens is not None else "")
            + ": não precisa reler a nota."
        )
    if not partes:
        partes.append("Sem valor a pagar nem quantidade de itens para comparar: só as linhas foram conferidas, e batem.")
    return "\n".join(partes)


def de_dicionarios(itens_brutos: Any, totais_brutos: Any) -> tuple[list[Item], Totais, list[str]]:
    """Itens e totais vindos de uma tool (JSON): aceita "39,99", 39.99, "R$ 1.234,56".

    Devolve também os problemas de entrada (linha sem total), para a resposta da
    tool dizer o que ignorou em vez de conferir errado em silêncio.
    """
    problemas: list[str] = []
    itens: list[Item] = []
    for n, bruto in enumerate(itens_brutos or [], start=1):
        if not isinstance(bruto, dict):
            problemas.append(f"item {n}: não é um objeto")
            continue
        total = numero(bruto.get("total"))
        if total is None:
            problemas.append(f"item {n}: sem 'total' numérico")
            continue
        itens.append(Item(
            descricao=str(bruto.get("descricao") or ""),
            codigo=str(bruto.get("codigo") or ""),
            qtd=numero(bruto.get("qtd")),
            unitario=numero(bruto.get("unitario")),
            total=total,
        ))
    t = totais_brutos if isinstance(totais_brutos, dict) else {}
    qtde = numero(t.get("qtde_itens"))
    pagamentos = [p for p in (numero(v) for v in (t.get("pagamentos") or [])) if p is not None]
    totais = Totais(
        valor_a_pagar=numero(t.get("valor_a_pagar")),
        qtde_itens=int(qtde) if qtde is not None else None,
        pagamentos=pagamentos,
        troco=numero(t.get("troco")),
        descontos=numero(t.get("descontos")),
        acrescimos=numero(t.get("acrescimos")),
    )
    return itens, totais, problemas


# ── a planilha ──────────────────────────────────────────────────────────────

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_MAX_BYTES = 10 * 1024 * 1024
_MAX_CELULAS = 50_000
_REF = re.compile(r"\$?([A-Z]{1,3})\$?(\d{1,7})")


def _plano(texto: str) -> str:
    sem = "".join(c for c in unicodedata.normalize("NFKD", texto or "") if not unicodedata.combining(c))
    return " ".join(sem.lower().split())


#: Rótulo de TOTAL da nota: a célula inteira, sem "receitas"/"despesas" junto.
_ROTULO_TOTAL = re.compile(
    r"^(total( geral| da nota| do cupom| a pagar| pago)?|valor (total|a pagar|pago)|soma)\s*(\(r\$\)|r\$)?\s*:?$"
)
_COL_QTD = re.compile(r"\b(qtd|qtde|quant)")
_COL_UNIT = re.compile(r"\bunit|\bpreco|\bvl\.? un\b|\bvalor un")
_COL_TOTAL = re.compile(r"\btotal\b|\bvl\.? item\b|\bvalor item\b")
_COL_DESC = re.compile(r"\bdescri|\bproduto|\bitem\b")


def _col_idx(letras: str) -> int:
    n = 0
    for c in letras:
        n = n * 26 + (ord(c) - 64)
    return n


def _col_letras(idx: int) -> str:
    s = ""
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


@dataclass
class _Celula:
    valor: Any = None      # str | Decimal | None
    formula: str | None = None


def _abas(caminho: Path) -> list[tuple[str, dict[tuple[int, int], _Celula]]]:
    """(nome da aba, {(linha, coluna): célula}) de cada aba do .xlsx."""
    with zipfile.ZipFile(caminho) as z:
        nomes = set(z.namelist())
        compartilhadas: list[str] = []
        if "xl/sharedStrings.xml" in nomes:
            raiz = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in raiz.findall("m:si", _NS):
                compartilhadas.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        alvos: list[tuple[str, str]] = []
        if "xl/workbook.xml" in nomes and "xl/_rels/workbook.xml.rels" in nomes:
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            por_id = {r.get("Id"): r.get("Target") or "" for r in rels}
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            for aba in wb.iter(f"{{{_NS['m']}}}sheet"):
                alvo = por_id.get(aba.get(_NS_REL)) or ""
                alvo = alvo.lstrip("/")
                if not alvo.startswith("xl/"):
                    alvo = posixpath.normpath(posixpath.join("xl", alvo))
                alvos.append((aba.get("name") or alvo, alvo))
        if not alvos:
            alvos = [(n, n) for n in sorted(nomes) if re.match(r"^xl/worksheets/sheet\d+\.xml$", n)]
        saida = []
        total = 0
        for nome, alvo in alvos:
            if alvo not in nomes:
                continue
            raiz = ET.fromstring(z.read(alvo))
            celulas: dict[tuple[int, int], _Celula] = {}
            for c in raiz.iter(f"{{{_NS['m']}}}c"):
                m = _REF.fullmatch(c.get("r") or "")
                if not m:
                    continue
                total += 1
                if total > _MAX_CELULAS:
                    return saida
                tipo = c.get("t") or "n"
                f = c.find("m:f", _NS)
                v = c.find("m:v", _NS)
                cel = _Celula(formula=(f.text or "").strip() if f is not None and f.text else None)
                if tipo == "s" and v is not None and (v.text or "").isdigit():
                    i = int(v.text)
                    cel.valor = compartilhadas[i] if i < len(compartilhadas) else None
                elif tipo == "inlineStr":
                    cel.valor = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
                elif tipo in ("str", "e"):
                    cel.valor = v.text if v is not None else None
                elif tipo == "b":
                    cel.valor = None
                elif v is not None and v.text is not None:
                    # No XML o número vem sempre com PONTO decimal ("2.168" é a
                    # quantidade da linguiça, não 2168): Decimal direto, nunca o
                    # `numero()` de texto em pt-BR.
                    try:
                        cel.valor = Decimal(v.text.strip())
                    except InvalidOperation:
                        cel.valor = None
                celulas[(int(m.group(2)), _col_idx(m.group(1)))] = cel
            saida.append((nome, celulas))
        return saida


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


class _Avaliador:
    """Calcula as fórmulas simples de uma aba: SUM de intervalos e + − × ÷ entre células."""

    def __init__(self, celulas: dict[tuple[int, int], _Celula]) -> None:
        self._c = celulas
        self._cache: dict[tuple[int, int], Decimal | None] = {}

    def valor(self, pos: tuple[int, int], profundidade: int = 0) -> Decimal | None:
        if pos in self._cache:
            return self._cache[pos]
        cel = self._c.get(pos)
        if cel is None:
            return None
        if cel.formula and profundidade < 20:
            self._cache[pos] = None          # ciclo → None
            r = self._formula(cel.formula, profundidade + 1)
            if r is None and isinstance(cel.valor, Decimal):
                r = cel.valor                # fórmula que não sei calcular: o valor guardado
        else:
            r = cel.valor if isinstance(cel.valor, Decimal) else None
        self._cache[pos] = r
        return r

    def _intervalo(self, a: str, b: str, prof: int) -> Decimal | None:
        ma, mb = _REF.fullmatch(a), _REF.fullmatch(b)
        if not ma or not mb:
            return None
        l1, l2 = sorted((int(ma.group(2)), int(mb.group(2))))
        c1, c2 = sorted((_col_idx(ma.group(1)), _col_idx(mb.group(1))))
        if (l2 - l1 + 1) * (c2 - c1 + 1) > 5000:
            return None
        soma = Decimal("0")
        for lin in range(l1, l2 + 1):
            for col in range(c1, c2 + 1):
                v = self.valor((lin, col), prof)
                if v is not None:
                    soma += v
        return soma

    def _formula(self, formula: str, prof: int) -> Decimal | None:
        expr = formula.lstrip("=").replace("$", "").upper()
        if "!" in expr:
            return None                       # referência a outra aba: fora do escopo

        def _sum(m: re.Match) -> str:
            total = Decimal("0")
            for arg in m.group(1).split(","):
                arg = arg.strip()
                if ":" in arg:
                    a, _, b = arg.partition(":")
                    v = self._intervalo(a.strip(), b.strip(), prof)
                else:
                    v = self._ref_ou_num(arg, prof)
                if v is None:
                    raise ValueError(arg)
                total += v
            return f"({total})"

        try:
            expr = re.sub(r"SUM\(([^()]*)\)", _sum, expr)
        except ValueError:
            return None
        if re.search(r"[A-Z]{2,}\(|[A-Z]+\(", expr):
            return None                       # outra função: não calculo
        valores: dict[str, Decimal] = {}

        def _ref(m: re.Match) -> str:
            v = self.valor((int(m.group(2)), _col_idx(m.group(1))), prof)
            if v is None:
                v = Decimal("0")
            nome = f"_v{len(valores)}"
            valores[nome] = v
            return nome

        expr = _REF.sub(_ref, expr)
        try:
            return _calcular(ast.parse(expr, mode="eval").body, valores)
        except (SyntaxError, ValueError, ZeroDivisionError, InvalidOperation, TypeError):
            return None

    def _ref_ou_num(self, arg: str, prof: int) -> Decimal | None:
        m = _REF.fullmatch(arg)
        if m:
            return self.valor((int(m.group(2)), _col_idx(m.group(1))), prof) or Decimal("0")
        return numero(arg)


def _calcular(no: ast.AST, valores: dict[str, Decimal]) -> Decimal:
    if isinstance(no, ast.Constant) and isinstance(no.value, (int, float)):
        return Decimal(str(no.value))
    if isinstance(no, ast.Name) and no.id in valores:
        return valores[no.id]
    if isinstance(no, ast.UnaryOp) and isinstance(no.op, (ast.USub, ast.UAdd)):
        v = _calcular(no.operand, valores)
        return -v if isinstance(no.op, ast.USub) else v
    if isinstance(no, ast.BinOp) and type(no.op) in _OPS:
        return _OPS[type(no.op)](_calcular(no.left, valores), _calcular(no.right, valores))
    raise ValueError("expressão fora do escopo")


def _texto_da(cel: _Celula | None) -> str:
    return cel.valor.strip() if cel is not None and isinstance(cel.valor, str) else ""


def _conferir_aba(nome_aba: str, celulas: dict[tuple[int, int], _Celula]) -> str | None:
    av = _Avaliador(celulas)
    linhas_planilha = sorted({lin for lin, _ in celulas})
    colunas = sorted({col for _, col in celulas})

    # Cabeçalho da tabela de itens: quantidade, unitário e total na mesma linha.
    cab: dict[str, int] | None = None
    linha_cab = 0
    for lin in linhas_planilha:
        achadas: dict[str, int] = {}
        for col in colunas:
            t = _plano(_texto_da(celulas.get((lin, col))))
            if not t:
                continue
            if "qtd" not in achadas and _COL_QTD.search(t):
                achadas["qtd"] = col
            elif "unit" not in achadas and _COL_UNIT.search(t):
                achadas["unit"] = col
            elif "total" not in achadas and _COL_TOTAL.search(t):
                achadas["total"] = col
            elif "desc" not in achadas and _COL_DESC.search(t):
                achadas["desc"] = col
        if {"qtd", "unit", "total"} <= achadas.keys():
            cab, linha_cab = achadas, lin
            break
    if cab is None:
        return None

    def _rotulo_total(lin: int) -> tuple[str, Decimal | None, str] | None:
        """(rótulo, valor, célula) se a linha é de TOTAL."""
        for col in colunas:
            t = _texto_da(celulas.get((lin, col)))
            if t and _ROTULO_TOTAL.match(_plano(t)):
                for c2 in colunas:
                    if c2 <= col:
                        continue
                    v = av.valor((lin, c2))
                    if v is not None:
                        return t, v, f"{_col_letras(c2)}{lin}"
                return t, None, ""
        return None

    itens: list[Item] = []
    fim_da_tabela = linha_cab
    for lin in linhas_planilha:
        if lin <= linha_cab:
            continue
        if _rotulo_total(lin) is not None:
            fim_da_tabela = lin
            break
        total = av.valor((lin, cab["total"]))
        if total is None:
            if itens and not any(celulas.get((lin, c)) for c in colunas):
                fim_da_tabela = lin
                break
            continue
        desc = _texto_da(celulas.get((lin, cab["desc"]))) if "desc" in cab else ""
        itens.append(Item(
            descricao=desc, total=total,
            qtd=av.valor((lin, cab["qtd"])), unitario=av.valor((lin, cab["unit"])),
            onde=f"linha {lin}",
        ))
        fim_da_tabela = lin
    if not itens:
        return None

    # Totais rotulados DEPOIS da tabela (TOTAL, Total pago, Valor a pagar...).
    rotulados: list[tuple[str, Decimal, str]] = []
    for lin in linhas_planilha:
        if lin < fim_da_tabela:
            continue
        r = _rotulo_total(lin)
        if r is not None and r[1] is not None:
            rotulados.append((r[0], r[1], r[2]))

    soma = sum((it.total for it in itens), Decimal("0"))
    # O total que a planilha diz que foi pago/cobrado e não bate com os itens.
    divergentes = [r for r in rotulados if abs(r[1] - soma) > _FOLGA]
    alvo = None
    if divergentes:
        alvo = next((r for r in divergentes if "pag" in _plano(r[0])), divergentes[0])
    veredito = conferir(itens, Totais(valor_a_pagar=alvo[1] if alvo else None))
    if veredito.ok:
        return None

    partes = [f"Conferência da planilha (aba {nome_aba!r}), calculada pelo sistema:"]
    iguais = [r for r in rotulados if abs(r[1] - soma) <= _FOLGA]
    if alvo is not None:
        partes.append(
            f"- os itens somam {brl(soma)}"
            + (f" ({', '.join(f'{r[0]!r} em {r[2]}' for r in iguais)})" if iguais else "")
            + f", mas {alvo[0]!r} em {alvo[2]} = {brl(alvo[1])}."
        )
    partes.extend(
        f"- {linha}" for linha in veredito.texto.splitlines()
        # A soma já foi dita acima, e "não precisa reler a nota" é para a leitura.
        if linha and not linha.startswith(("Os ", "Todas as linhas"))
    )
    partes.append(
        "Corrija o valor na planilha e salve de novo antes de responder; não diga "
        "que as contas fecham enquanto esta conferência acusar diferença."
    )
    return "\n".join(partes)


def conferir_planilha(caminho: Path, nome: str = "") -> str | None:
    """O aviso para o modelo quando a planilha não fecha, ou None. Nunca levanta.

    Só confere aba com tabela de itens (colunas de quantidade, unitário e total):
    uma planilha qualquer com dois "Total" (receitas e despesas) não é nota, e
    acusar ali seria ruído que o modelo aprende a ignorar.
    """
    ext = (nome or caminho.name).rsplit(".", 1)[-1].lower() if "." in (nome or caminho.name) else ""
    if ext not in ("xlsx", "xlsm"):
        return None
    try:
        if caminho.stat().st_size > _MAX_BYTES:
            return None
        avisos = [a for a in (_conferir_aba(n, c) for n, c in _abas(caminho)) if a]
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ValueError, RecursionError):
        return None
    return "\n\n".join(avisos) or None
