"""A CONFERÊNCIA visual: a pergunta fechada de cada gatilho e a leitura da resposta.

docs/browser-visao-confirmacao-plan.md §3 e §9. A visão não descreve a tela:
ela confere o que o código já sabe (o valor digitado, os campos do formulário,
o botão de enviar) e responde um JSON curto. Medido em 17/11: pergunta fechada
com JSON lê igual à aberta, ~30% mais rápida, e o recorte de um campo custa
US$ 0,00007.

A DOM continua valendo para estado (marcado, valor do campo): a imagem
acrescenta o que a DOM não vê — erro em vermelho, campo coberto, texto cortado,
página de erro, prévia de foto.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

SISTEMA = (
    "Você confere prints de páginas web para um agente que está preenchendo um "
    "formulário. Responda SÓ o JSON pedido, sem texto fora dele. Se não dá para ver "
    "algo no print, use null — nunca chute. Mensagem de erro é texto de validação "
    "visível (vermelho, 'obrigatório', 'inválido'); placeholder cinza não é erro."
)


@dataclass
class Conferencia:
    gatilho: str
    #: True = confere · False = diverge · None = não deu para ver.
    ok: bool | None
    #: Uma linha para o prompt e a timeline ("✓ «Cargo» mostra …").
    linha: str
    #: O que está errado, quando `ok is False` (vai para o passo que causou).
    divergencia: str = ""


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t).strip().casefold()


def _mesmo_valor(visto: str, esperado: str) -> bool | None:
    v, e = _norm(visto), _norm(esperado)
    if not e:
        return None
    if not v:
        return False
    # Texto longo: a visão devolve só o começo (até `_COMECO` letras).
    if len(e) > _LONGO:
        e = e[: len(v)] if len(v) >= 12 else e
    # Campo estreito corta o texto na tela: o começo igual vale.
    return v == e or e.startswith(v.rstrip(".… ")) and len(v) >= 12 or v.startswith(e)


#: Valor maior que isto vai para a pergunta só pelo começo, e a visão devolve só
#: o começo do que vê. Ecoar 600 caracteres estourou o teto da resposta e o JSON
#: chegou cortado (sessão 69020f7a, conferência do G3).
_LONGO = 120
_COMECO = 80


def _esperado(valor: str) -> str:
    v = str(valor or "")
    return v if len(v) <= _LONGO else f"{v[:_COMECO]}… (texto longo: confira só o começo)"


def _codigo(gatilho: str) -> str:
    """'G4+G7' → 'G4' (o mais forte manda na pergunta)."""
    partes = gatilho.split("+")
    for forte in ("G3", "G4", "G8", "G2", "G5", "G7", "G1", "G1b"):
        if forte in partes:
            return forte
    return partes[0]


def pergunta(gatilho: str, contexto: dict) -> str:
    """A pergunta fechada do gatilho. `contexto` vem do driver (valor digitado,
    campos do formulário, botão)."""
    g = _codigo(gatilho)
    if g == "G2":
        campo = contexto.get("campo") or "o campo"
        if contexto.get("marcado") is not None:
            estado = "MARCADA" if contexto["marcado"] else "DESMARCADA"
            alvo = f'A caixa/opção «{campo}» deve estar {estado}.'
        elif contexto.get("valor"):
            alvo = f'O campo «{campo}» deve mostrar «{_esperado(contexto["valor"])}».'
        else:
            alvo = f'O campo «{campo}» foi preenchido (o valor não importa aqui).'
        return (
            f"{alvo} JSON: "
            f'{{"visto":"<as primeiras {_COMECO} letras do que o campo mostra>","confere":true|false|null,'
            '"erro_visivel":"<texto do erro>"|null,"coberto":true|false}'
        )
    if g == "G3":
        campos = contexto.get("campos") or []
        lista = "; ".join(
            f'«{c["rotulo"]}» = «{_esperado(c["valor"])}»' if c.get("valor") else f'«{c["rotulo"]}» preenchido'
            for c in campos[:12]
        ) or "(nenhum campo listado)"
        return (
            f'Antes de clicar em «{contexto.get("botao") or "enviar"}». Confira: {lista}. JSON: '
            '{"campos":[{"rotulo":"...","visto":"<até 80 letras do começo>","confere":true|false|null}],'
            '"erros_visiveis":["..."],"botao_habilitado":true|false|null}'
        )
    if g == "G4":
        return (
            f'Acabou de clicar em «{contexto.get("botao") or "enviar"}». O que a página mostra? JSON: '
            '{"resultado":"sucesso"|"erro"|"formulario_aberto"|"incerto",'
            '"mensagem":"<texto de sucesso ou erro visível>"|null}'
        )
    if g == "G8":
        return (
            "Acabou de enviar um arquivo (foto/documento) a um campo. JSON: "
            '{"previa_apareceu":true|false|null,"passo_extra":"<recortar/confirmar/salvar, se pedido>"|null,'
            '"erro_visivel":"..."|null}'
        )
    if g == "G5":
        return (
            "Um painel/modal acabou de abrir. JSON: "
            '{"painel":"<o que ele é, em poucas palavras>","campos":["..."],"botoes":["..."],'
            '"erro_visivel":"..."|null}'
        )
    if g == "G7":
        return (
            "A página acabou de mudar. JSON: "
            '{"pagina":"<que página é, em poucas palavras>",'
            '"bloqueio":"login"|"captcha"|"erro"|"popup"|null,"detalhe":"..."|null}'
        )
    # G1 / G1b: a linha de base do formulário.
    return (
        "Linha de base antes de editar este formulário. JSON: "
        '{"erros_visiveis":["..."],"algo_cobrindo":"<popup/banner/menu na frente>"|null}'
    )


def _json(texto: str) -> dict | None:
    t = str(texto or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    inicio, fim = t.find("{"), t.rfind("}")
    if inicio < 0 or fim <= inicio:
        return None
    try:
        dado = json.loads(t[inicio : fim + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return dado if isinstance(dado, dict) else None


def _txt(v: object, n: int = 80) -> str:
    return str(v or "").strip()[:n]


def interpretar(gatilho: str, contexto: dict, resposta: str) -> Conferencia:
    """A resposta da visão vira veredito + uma linha. O VEREDITO é do código
    (compara o visto com o esperado), não do `confere` que a visão disse."""
    g = _codigo(gatilho)
    dado = _json(resposta)
    if dado is None:
        return Conferencia(gatilho, None, f"{gatilho}: a visão não respondeu no formato")

    if g == "G2":
        campo = contexto.get("campo") or "campo"
        erro = _txt(dado.get("erro_visivel"))
        if dado.get("coberto") is True:
            return Conferencia(gatilho, False, f"⚠ «{campo}» está COBERTO por outra coisa no print",
                               f"o campo «{campo}» aparece coberto no print")
        if erro:
            return Conferencia(gatilho, False, f"⚠ «{campo}»: aparece o erro «{erro}»",
                               f"perto de «{campo}» aparece o erro «{erro}»")
        visto = _txt(dado.get("visto"), 120)
        esperado = contexto.get("valor")
        if contexto.get("marcado") is not None or not esperado:
            ok = dado.get("confere") if isinstance(dado.get("confere"), bool) else None
        else:
            ok = _mesmo_valor(visto, str(esperado))
        if ok is False:
            return Conferencia(gatilho, False, f"⚠ «{campo}» mostra «{visto}», e o digitado foi «{_txt(esperado)}»",
                               f"no print, «{campo}» mostra «{visto}» (cortado ou não entrou?)")
        if ok is None:
            return Conferencia(gatilho, None, f"? «{campo}»: não deu para ver no print")
        return Conferencia(gatilho, True, f"✓ «{campo}» mostra «{visto or _txt(esperado)}»")

    if g == "G3":
        erros = [_txt(e) for e in (dado.get("erros_visiveis") or []) if _txt(e)]
        esperado = {_norm(c["rotulo"]): c for c in contexto.get("campos") or []}
        ruins: list[str] = []
        for c in dado.get("campos") or []:
            if not isinstance(c, dict):
                continue
            meu = esperado.get(_norm(c.get("rotulo") or ""))
            if meu and meu.get("valor"):
                if _mesmo_valor(_txt(c.get("visto"), 120), meu["valor"]) is False:
                    ruins.append(f"«{meu['rotulo']}» mostra «{_txt(c.get('visto'))}»")
            elif c.get("confere") is False:
                ruins.append(f"«{_txt(c.get('rotulo'))}» não confere")
        if dado.get("botao_habilitado") is False:
            ruins.append(f"o botão «{contexto.get('botao') or 'enviar'}» está desabilitado")
        if erros or ruins:
            o_que = "; ".join(ruins + [f"erro «{e}»" for e in erros])
            return Conferencia(gatilho, False, f"⚠ antes de enviar: {o_que}", f"antes de enviar, o print mostra: {o_que}")
        return Conferencia(gatilho, True, f"✓ antes de enviar: {len(esperado)} campo(s) conferem, sem erro na tela")

    if g == "G4":
        res = _txt(dado.get("resultado"), 30)
        msg = _txt(dado.get("mensagem"))
        if res == "erro":
            return Conferencia(gatilho, False, f"⚠ depois de enviar: erro «{msg}»", f"o site mostrou erro ao enviar: «{msg}»")
        if res == "formulario_aberto":
            return Conferencia(gatilho, False, "⚠ depois de enviar: o formulário continua aberto",
                               "depois de enviar, o formulário continua aberto no print (não salvou?)")
        if res == "sucesso":
            return Conferencia(gatilho, True, "✓ depois de enviar: sucesso" + (f" — «{msg}»" if msg else ""))
        return Conferencia(gatilho, None, "? depois de enviar: o print não mostra resultado claro")

    if g == "G7":
        bloqueio = _txt(dado.get("bloqueio"), 20)
        pagina = _txt(dado.get("pagina"))
        if bloqueio and bloqueio != "null":
            return Conferencia(gatilho, False, f"⚠ página nova com {bloqueio}: {_txt(dado.get('detalhe')) or pagina}",
                               f"a página nova mostra {bloqueio}")
        return Conferencia(gatilho, True, f"✓ página nova: {pagina or 'carregou'}")

    if g == "G8":
        erro = _txt(dado.get("erro_visivel"))
        extra = _txt(dado.get("passo_extra"))
        if erro:
            return Conferencia(gatilho, False, f"⚠ depois do upload: erro «{erro}»", f"o upload mostrou erro: «{erro}»")
        if dado.get("previa_apareceu") is True:
            return Conferencia(gatilho, True, "✓ upload: a prévia apareceu" + (f"; falta «{extra}»" if extra else ""))
        return Conferencia(gatilho, None, "? upload: não deu para ver a prévia" + (f"; o site pede «{extra}»" if extra else ""))

    if g == "G5":
        erro = _txt(dado.get("erro_visivel"))
        painel = _txt(dado.get("painel"))
        botoes = ", ".join(_txt(b, 30) for b in (dado.get("botoes") or [])[:6] if _txt(b, 30))
        linha = f"painel «{painel or 'aberto'}»" + (f" — botões: {botoes}" if botoes else "")
        if erro:
            return Conferencia(gatilho, False, f"⚠ {linha}; erro «{erro}»", f"o painel mostra erro: «{erro}»")
        return Conferencia(gatilho, True, f"✓ {linha}")

    # G1 / G1b
    erros = [_txt(e) for e in (dado.get("erros_visiveis") or []) if _txt(e)]
    cobrindo = _txt(dado.get("algo_cobrindo"))
    if cobrindo and cobrindo != "null":
        return Conferencia(gatilho, False, f"⚠ antes de editar: «{cobrindo}» na frente do formulário",
                           f"tem «{cobrindo}» na frente do formulário")
    if erros:
        return Conferencia(gatilho, None, "• o formulário já tinha erro antes de editar: " + "; ".join(erros[:3]))
    return Conferencia(gatilho, True, "✓ formulário sem erro antes de editar")


def valores_salvos_na_pagina(snapshot: dict, campos: list[dict]) -> list[str]:
    """Depois de enviar: os valores digitados que a DOM mostra (o print do G4 pode
    não pegar o item salvo — a página rola; job cb0374f1). Só valor de ≥ 6 letras
    conta (número curto casa com qualquer coisa)."""
    pagina = _norm(json.dumps(snapshot, ensure_ascii=False))
    achados: list[str] = []
    for c in campos:
        valor = str(c.get("valor") or "")
        if len(_norm(valor)) >= 6 and _norm(valor)[:60] in pagina:
            achados.append(str(c.get("rotulo") or "campo"))
    return achados
