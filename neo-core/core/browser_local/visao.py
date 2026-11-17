"""Gatilhos dos prints de CONFIRMAÇÃO (docs/browser-visao-confirmacao-plan.md).

Quem decide QUANDO fotografar é o código, igual em todo job — não o modelo, que
troca de ideia de um passo para o outro. Este módulo é lógica pura: recebe a
ação e os snapshots e devolve os disparos. O driver tira o print e guarda.

| gatilho | quando | print |
|---|---|---|
| G1 | antes do 1º campo editado num formulário (url + painel aberto) | tela inteira |
| G1b | antes de cada campo seguinte, se a tela mudou desde o último print | tela inteira |
| G2 | depois de cada campo editado (no snapshot seguinte) | recortado no campo |
| G3 | antes de enviar (clique em Salvar/Enviar/Concluir…) | tela inteira |
| G4 | depois de enviar | tela inteira |
| G5 | um painel/modal abriu depois da ação | recortado no painel |
| G7 | a página mudou de endereço | tela inteira |
| G8 | depois de um upload | tela inteira |

Dedupe: tela inteira na MESMA tela (fingerprint) do último print inteiro não se
repete — o "depois" do campo N costuma ser o "antes" do campo N+1. No mesmo
momento, vários disparos de tela inteira viram UM print com os motivos somados,
e o recortado sai junto só quando acrescenta (campo/painel específico).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Ações que EDITAM um campo. Clique só conta quando o alvo é marcável.
_EDITA = frozenset({"fill", "select", "upload"})
_CLIQUES = frozenset({"click", "click_text"})


@dataclass
class Disparo:
    gatilho: str
    motivo: str
    #: Retângulo em px de CSS ([x, y, w, h]) para o print recortado; None = tela inteira.
    recorte: list[float] | None = None
    #: O que a conferência precisa saber (o valor digitado, o botão de enviar…).
    contexto: dict = field(default_factory=dict)
    #: Print guiado pelo NÚMERO do elemento: o desktop mede o campo na página na
    #: hora (e o traz para a vista), em vez de usar a caixa de um snapshot.
    alvo_ref: int | None = None


@dataclass
class EstadoDaVisao:
    """O que a visão de confirmação já fez nesta tarefa."""

    prints: int = 0
    formularios: set[str] = field(default_factory=set)
    #: Fingerprint da tela no último print de TELA INTEIRA.
    ultimo_inteiro: str | None = None
    #: A ação que acabou de rodar, para os disparos do "depois".
    pendente: dict | None = None
    avisou_teto: bool = False


def _elemento(snapshot: dict, ref: Any) -> dict | None:
    for item in snapshot.get("elements") or []:
        if isinstance(item, dict) and item.get("ref") == ref:
            return item
    return None


def _caixa(item: dict | None) -> list[float] | None:
    box = (item or {}).get("box")
    if isinstance(box, list) and len(box) == 4:
        try:
            x, y, w, h = (float(v) for v in box)
        except (TypeError, ValueError):
            return None
        if w > 0 and h > 0:
            return [x, y, w, h]
    return None


def _mesmo_campo(snapshot: dict, pendente: dict) -> dict | None:
    """O campo editado no snapshot NOVO, pela identidade, não pelo número.

    Preencher pode rolar a página e renumerar tudo: na sessão 69020f7a a
    descrição era o [28] antes do `fill` e o [18] depois — e o [28] virou o
    seletor de mês. O print "depois do campo" recortava o elemento errado.
    """
    tag, texto = pendente.get("tag"), pendente.get("texto")
    candidatos = [
        e for e in snapshot.get("elements") or []
        if isinstance(e, dict) and e.get("tag") == tag and str(e.get("text") or "") == texto
    ]
    valor = str(pendente.get("valor") or "")[:40]
    if valor and len(candidatos) > 1:
        com_valor = [e for e in candidatos if str(e.get("value") or "").startswith(valor)]
        if com_valor:
            candidatos = com_valor
    if len(candidatos) == 1:
        return candidatos[0]
    mesmo_ref = _elemento(snapshot, pendente.get("ref"))
    if mesmo_ref in candidatos:
        return mesmo_ref
    return None


def _url(snapshot: dict) -> str:
    return str(snapshot.get("url") or "").split("#")[0]


def _rotulo(item: dict | None, args: dict) -> str:
    texto = str((item or {}).get("text") or args.get("texto") or "").strip()
    return texto[:60] or f"campo {args.get('ref')}"


def campo_editado(action: str, args: dict, snapshot: dict) -> dict | None:
    """O elemento que esta ação EDITA (fill/select/upload, ou clique em caixa/rádio)."""
    if action in _EDITA:
        return _elemento(snapshot, args.get("ref")) or {"text": str(args.get("ref") or "")}
    if action == "click":
        item = _elemento(snapshot, args.get("ref"))
        if item is not None and item.get("checked") is not None:
            return item
    return None


def rotulo_de_envio(action: str, args: dict, snapshot: dict, envio: re.Pattern) -> str | None:
    """O rótulo do botão, quando a ação é ENVIAR (Salvar, Enviar, Concluir…)."""
    if action not in _CLIQUES:
        return None
    if action == "click":
        texto = str((_elemento(snapshot, args.get("ref")) or {}).get("text") or "")
    else:
        texto = str(args.get("texto") or "")
    texto = texto.strip()
    return texto[:60] if texto and envio.search(texto) else None


def _chave_do_formulario(snapshot: dict, painel: str | None) -> str:
    return f"{_url(snapshot)}|{painel or ''}"


def recorte_do_campo(caixa: list[float], margem: float) -> list[float]:
    """O campo, o rótulo acima e a faixa abaixo (onde aparece o erro)."""
    x, y, w, h = caixa
    topo = max(0.0, y - 40.0)
    esquerda = max(0.0, x - 24.0)
    return [esquerda, topo, w + (x - esquerda) + 24.0, h + (y - topo) + margem]


def antes_da_acao(
    estado: EstadoDaVisao,
    action: str,
    args: dict,
    snapshot: dict,
    *,
    fingerprint: str,
    painel: str | None,
    envio: re.Pattern,
) -> list[Disparo]:
    """Os prints de ANTES (G1, G1b, G3) e a anotação do que esperar depois."""
    disparos: list[Disparo] = []
    item = campo_editado(action, args, snapshot)
    envio_rotulo = rotulo_de_envio(action, args, snapshot, envio)
    if item is not None:
        chave = _chave_do_formulario(snapshot, painel)
        if chave not in estado.formularios:
            estado.formularios.add(chave)
            disparos.append(Disparo("G1", f"início do formulário (antes de «{_rotulo(item, args)}»)"))
        elif fingerprint != estado.ultimo_inteiro:
            disparos.append(Disparo("G1b", f"antes de «{_rotulo(item, args)}»"))
    if envio_rotulo:
        disparos.append(Disparo("G3", f"antes de enviar («{envio_rotulo}»)"))
    valor = args.get("value") if action == "fill" else args.get("option") if action == "select" else None
    # Marcador de dado pessoal/cofre: o valor real só existe na hora de digitar.
    if valor is not None and ("«pks:" in str(valor) or "«cofre:" in str(valor) or "«senha:" in str(valor)):
        valor = None
    if envio_rotulo:
        disparos[-1].contexto["botao"] = envio_rotulo
    estado.pendente = {
        "valor": None if valor is None else str(valor),
        "action": action,
        "tag": (item or {}).get("tag"),
        "texto": str((item or {}).get("text") or ""),
        "campo": _rotulo(item, args) if item is not None else None,
        "caixa": _caixa(item) if item is not None else None,
        "ref": args.get("ref"),
        "envio": envio_rotulo,
        "url": _url(snapshot),
    }
    return disparos


def _marcado_pelo_retorno(detalhe: str) -> bool | None:
    """O estado da caixa depois do clique, pelo retorno do device (sem snapshot novo)."""
    if "DESMARCADO" in detalhe or "NÃO ficou selecionado" in detalhe:
        return False
    if "SELECIONADO" in detalhe or "continua MARCADO" in detalhe:
        return True
    return None


def logo_depois(estado: EstadoDaVisao, detalhe: str) -> Disparo | None:
    """G2 LOGO DEPOIS da ação, antes do próximo snapshot — antes de a página (ou o
    agente) rolar. Guiado pelo número que a ação usou: o desktop mede o campo na
    hora. Sem número (``click_text``) fica para o snapshot seguinte."""
    pendente = estado.pendente
    if not pendente or pendente.get("campo") is None or pendente.get("action") == "upload":
        return None
    ref = pendente.get("ref")
    if not isinstance(ref, int):
        return None
    pendente["g2_ja"] = True
    marcado = _marcado_pelo_retorno(str(detalhe or "")) if pendente.get("action") in _CLIQUES else None
    return Disparo(
        "G2", f"depois de «{pendente['campo']}»", None,
        contexto={"campo": pendente["campo"], "valor": pendente.get("valor"), "marcado": marcado},
        alvo_ref=ref,
    )


def depois_da_acao(
    estado: EstadoDaVisao,
    snapshot: dict,
    *,
    painel_novo: dict | None,
    margem: float,
) -> list[Disparo]:
    """Os prints de DEPOIS, no snapshot seguinte à ação (G2, G4, G5, G7, G8)."""
    pendente, estado.pendente = estado.pendente, None
    if not pendente:
        return []
    disparos: list[Disparo] = []
    if pendente.get("envio"):
        disparos.append(Disparo("G4", f"depois de enviar («{pendente['envio']}»)", contexto={"botao": pendente["envio"]}))
    if _url(snapshot) and pendente.get("url") and _url(snapshot) != pendente["url"]:
        disparos.append(Disparo("G7", f"página nova: {_url(snapshot)[:120]}"))
    if pendente.get("action") == "upload":
        disparos.append(Disparo("G8", "depois do upload (prévia/recorte do site)"))
    if painel_novo is not None:
        caixa = _caixa(painel_novo)
        rotulo = str(painel_novo.get("rotulo") or "painel")[:60]
        disparos.append(Disparo("G5", f"abriu o painel «{rotulo}»", caixa))
    if pendente.get("campo") is not None and pendente.get("action") != "upload" and not pendente.get("g2_ja"):
        # A caixa de ANTES pode ter andado (a página reflui ao preencher): a do
        # snapshot novo vale mais, quando o elemento ainda existe.
        novo = _mesmo_campo(snapshot, pendente)
        # Sem achar o campo, nada de caixa velha (a página pode ter rolado): o
        # print sai inteiro, e a conferência pergunta pelo rótulo.
        caixa = _caixa(novo)
        marcado = (novo or {}).get("checked") if pendente.get("action") in _CLIQUES else None
        disparos.append(Disparo(
            "G2", f"depois de «{pendente['campo']}»",
            recorte_do_campo(caixa, margem) if caixa else None,
            contexto={"campo": pendente["campo"], "valor": pendente.get("valor"), "marcado": marcado},
        ))
    return disparos


def juntar(disparos: list[Disparo], *, fingerprint: str, estado: EstadoDaVisao) -> list[Disparo]:
    """No mesmo momento: UM print inteiro (motivos somados) + os recortados.

    Tela inteira repetida na mesma fingerprint do último print inteiro não sai.
    Recorte sem caixa vira motivo do print inteiro.
    """
    inteiros = [d for d in disparos if d.recorte is None and d.alvo_ref is None]
    recortes = [d for d in disparos if d.recorte is not None or d.alvo_ref is not None]
    saida: list[Disparo] = []
    if inteiros and fingerprint != estado.ultimo_inteiro:
        contexto: dict = {}
        for d in inteiros:
            contexto.update(d.contexto)
        saida.append(Disparo(
            "+".join(dict.fromkeys(d.gatilho for d in inteiros)),
            " · ".join(d.motivo for d in inteiros),
            contexto=contexto,
        ))
    elif inteiros and not saida:
        # Mesma tela do último print inteiro: os G3/G4 ainda merecem o print
        # (é a prova do que foi enviado), os outros não.
        fortes = [d for d in inteiros if d.gatilho in ("G3", "G4")]
        if fortes:
            contexto = {}
            for d in fortes:
                contexto.update(d.contexto)
            saida.append(Disparo(
                "+".join(d.gatilho for d in fortes), " · ".join(d.motivo for d in fortes),
                contexto=contexto,
            ))
    saida.extend(recortes)
    return saida
