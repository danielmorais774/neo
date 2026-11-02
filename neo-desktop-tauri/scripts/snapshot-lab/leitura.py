#!/usr/bin/env python3
"""Prova a LEITURA da tela e a classificação de painéis do `js_snapshot`.

O `conteudo-principal.py` cobre a eleição do bloco principal. Este cobre o que
veio depois do job 45e85695, em que a eleição virou o carrinho e o preço do
produto, aberto na tela, ficou de fora de 15 dos 19 prompts daquelas páginas:

  * `tela` traz o que está na janela, em qualquer forma de página;
  * uma barra presa na borda não toma o lugar do conteúdo, não é "painel aberto"
    e não precisa ser fechada; modal e gaveta de verdade continuam sendo;
  * o painel do Neo não cobre nada (roda o `content-overlay.js` REAL);
  * o teto de elementos guarda o que está na tela, não o começo do DOM;
  * `extract` por página e por busca alcançam o que não coube.

    python3 leitura.py

Sai 1 se qualquer caso falhar.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("lab", AQUI / "conteudo-principal.py")
lab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab)
js = lab.js
EXT = lab.RAIZ / "neo-browser-local/content-overlay.js"

# O mesmo dublê do `testar-painel.py`: content script não roda fora da extensão.
STUB = """
globalThis.chrome = {
  runtime: { lastError: null, onMessage: { addListener: () => {} }, sendMessage: () => {} },
  storage: { local: { get: (_k, cb) => cb({}), set: () => {} } },
};
"""

falhas: list[str] = []


def confere(caso: str, ok: bool, detalhe: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {caso}{' — ' + detalhe if detalhe and not ok else ''}")
    if not ok:
        falhas.append(f"{caso} {detalhe}")


def url(nome: str) -> str:
    return (AQUI / nome).as_uri()


async def corpo(ev, cmd):
    async def abrir(destino: str, w: int, h: int):
        await cmd("Emulation.setDeviceMetricsOverride",
                  {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
        await cmd("Page.navigate", {"url": destino})
        await asyncio.sleep(1.0)

    async def snap():
        return await ev(js("js_snapshot", max=200, chars=3000))

    print("\n■ barra lateral numa janela LARGA, com o painel do Neo por cima")
    await abrir(url("produto-com-barra.html"), 1912, 1000)
    await ev(STUB)
    await ev(EXT.read_text(encoding="utf-8"))
    await ev("postMessage({ __neo_overlay: { active: true, kind: 'passo', text: 'abrindo o produto' } }, '*')")
    await asyncio.sleep(0.3)
    s = await snap()
    tela, els = s.get("tela") or "", s.get("elements") or []
    principal = (s.get("main") or {}).get("label") or ""
    confere("o principal não é o carrinho", "Subtotal" not in principal, principal)
    for dado in ("3.399", "12x de R$ 283,25", "Entrega GRÁTIS", "3 em estoque", "vendido por Amazon"):
        confere(f"a tela traz «{dado}»", dado in tela, tela[:200])
    confere("o carrinho não entra na tela", "Subtotal" not in tela and "54,90" not in tela)
    confere("nada do menu entra na tela", "Todos os departamentos" not in tela)
    confere("opção de <select> não vira texto", "\n1\n2\n3" not in tela)
    cobertos = [e for e in els if e.get("coberto")]
    confere("o painel do Neo não cobre NADA", not cobertos,
            f"{len(cobertos)}: {[(e['text'][:24], e['coberto']) for e in cobertos[:4]]}")
    do_painel = [e for e in els if e.get("menu")]
    confere("item da barra não é item de painel aberto", not do_painel,
            str([e["text"][:24] for e in do_painel[:4]]))
    confere("item da barra sai marcado lateral", any(e.get("lateral") for e in els))
    confere("a barra vai para `barras`", any("Subtotal" in (b.get("rotulo") or "") for b in s.get("barras") or []),
            str(s.get("barras")))
    confere("o que está abaixo da dobra vai para `resto`", "teclado confortável" in (s.get("resto") or ""))

    fotos = s.get("imagens") or []
    confere("a foto principal vai em `imagens`, com o endereço real e não o `data:`",
            any("61abc" in (f.get("url") or "") for f in fotos), str(fotos)[:200])
    confere("selo e ícone não entram", not any("selo.png" in (f.get("url") or "") for f in fotos))

    print("\n■ a mesma página numa janela ESTREITA: o carrinho nem está na tela")
    await abrir(url("produto-com-barra.html"), 1280, 819)
    s = await snap()
    els = s.get("elements") or []
    confere("sem painel, sem barra", not s.get("barras") and not any(e.get("menu") or e.get("lateral") for e in els))
    confere("a tela traz o preço", "3.399" in (s.get("tela") or ""))

    print("\n■ formas de painel")
    await abrir(url("paineis.html") + "?caso=cookie", 1600, 1000)
    s = await snap()
    confere("banner de cookie no rodapé não toma o principal",
            "129,90" in ((s.get("main") or {}).get("text") or ""), str(s.get("main"))[:160])
    confere("banner de cookie continua sendo painel", any(e.get("menu") for e in s["elements"]))
    await abrir(url("paineis.html") + "?caso=filtros", 1600, 1000)
    s = await snap()
    els = s["elements"]
    confere("`role=dialog` em fluxo não é painel aberto", not any(e.get("menu") or e.get("raiz") for e in els),
            str([e["text"][:20] for e in els if e.get("menu")][:4]))
    confere("...e o texto dele continua na leitura", "Marca ASUS" in (s.get("tela") or ""))
    await abrir(url("paineis.html") + "?caso=gaveta", 1600, 1000)
    s = await snap()
    confere("gaveta com fundo não é barra lateral", not s.get("barras"), str(s.get("barras")))
    confere("gaveta com fundo é painel aberto", any(e.get("menu") for e in s["elements"]))
    confere("fundo translúcido não apaga a página da tela", "129,90" in (s.get("tela") or ""))
    await abrir(url("paineis.html") + "?caso=tapume", 1600, 1000)
    s = await snap()
    confere("modal de tela cheia é o principal", "newsletter" in ((s.get("main") or {}).get("text") or "").lower())
    confere("o que está atrás de modal opaco sai da tela", "129,90" not in (s.get("tela") or ""),
            (s.get("tela") or "")[:160])
    confere("...mas não some: vai para `resto`", "129,90" in (s.get("resto") or ""))

    print("\n■ web component, cartão esticado e iframe")
    await abrir(url("sombra.html"), 1600, 1000)
    s = await snap()
    tela, els = s.get("tela") or "", s.get("elements") or []
    confere("texto dentro de shadow DOM", "R$ 1.899,00" in tela, tela[:200])
    confere("botão dentro de shadow DOM vira ref", any(e.get("text") == "Comprar cadeira" for e in els),
            str([e.get("text") for e in els][:8]))
    alvo = next((e for e in els if e.get("text") == "Comprar cadeira"), {})
    confere("...e não sai coberto pelo próprio host", not alvo.get("coberto"), str(alvo.get("coberto")))
    confere("link esticado transparente não apaga o texto do cartão", "R$ 249,90" in tela, tela[:200])
    fr = s.get("iframes") or []
    confere("iframe do mesmo site é lido", any("VOLTA10" in (f.get("texto") or "") for f in fr), str(fr)[:200])
    confere("iframe de outro site é declarado", any(not f.get("legivel") for f in fr), str(fr)[:200])

    print("\n■ o teto de elementos guarda o que está na tela")
    await abrir(url("saturada.html"), 1600, 1000)
    s = await snap()
    textos = [e.get("text") for e in s["elements"]]
    confere("os 6 botões da tela estão na lista", all(f"Comprar item {k}" in textos for k in range(6)))
    confere("a lista respeita o teto", len(textos) <= 200, str(len(textos)))
    confere("o snapshot diz quanto ficou de fora", (s.get("omitidos") or 0) > 20, str(s.get("omitidos")))

    print("\n■ extract alcança o que não coube")
    await abrir(url("produto-com-barra.html"), 1912, 1000)
    await snap()
    p1 = await ev(js("js_ler_pagina", de=0, chars=400))
    confere("a primeira janela sabe que continua", p1.get("cortado") and p1.get("proximo") == 400, str(p1)[:160])
    p2 = await ev(js("js_ler_pagina", de=p1["proximo"], chars=4000))
    confere("a leitura paginada chega no fim da página", "teclado confortável" in p2.get("text", ""))
    confere("e o fim sabe que acabou", not p2.get("cortado") and p2.get("proximo") is None)
    confere("sem menu nenhum no caminho", "Todos os departamentos" not in p1["text"] + p2["text"])
    r = await ev("""(() => { const el = document.getElementById('principal');
      window.__neo.refs.push(el); return window.__neo.refs.length - 1; })()""")
    foto = await ev(js("js_extract", reference=r, chars=4000))
    confere("ler uma imagem devolve o endereço dela", "61abc" in (foto.get("imagem") or ""), str(foto)[:160])
    b = await ev(js("js_buscar", termo=json.dumps("GARANTIA"), chars=4000))
    confere("a busca acha abaixo da dobra, sem acento nem caixa", b.get("achados", 0) >= 1 and "12 meses" in b.get("text", ""), str(b)[:200])
    b = await ev(js("js_buscar", termo=json.dumps("selo.png"), chars=4000))
    confere("a busca acha endereço de imagem", b.get("achados", 0) >= 1 and "imagens.exemplo" in b.get("text", ""), str(b)[:200])
    b = await ev(js("js_buscar", termo=json.dumps("teletransporte"), chars=4000))
    confere("a busca sabe dizer que não tem", b.get("ok") is True and b.get("achados") == 0, str(b)[:200])


async def rodar() -> int:
    await lab._sessao(corpo)
    if falhas:
        print(f"\nFALHOU ({len(falhas)}):")
        for f in falhas:
            print(f"  ✗ {f}")
        return 1
    print("\n✓ leitura e painéis ok")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(rodar()))
