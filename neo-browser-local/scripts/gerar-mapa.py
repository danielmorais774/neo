#!/usr/bin/env python3
"""Gera `neo-map.generated.js` a partir do Rust — a extensão NÃO tem cópia própria.

O inventário de elementos mora como string JS dentro de
`neo-desktop-tauri/src-tauri/src/services/browser_local.rs`, porque quem o injeta
é o desktop, por CDP. O inspetor visual da extensão precisa do MESMO mapeamento:
um inspetor que enxerga diferente do driver não testa nada, ele mente.

Por isso aqui não há reimplementação — há extração. Rode este script depois de
mexer no `js_snapshot` e recarregue a extensão.

    python3 scripts/gerar-mapa.py

`--check` sai com código 1 se o arquivo gerado estiver desatualizado (serve para
CI ou para um hook, sem reescrever nada).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RUST = AQUI.parents[1] / "neo-desktop-tauri/src-tauri/src/services/browser_local.rs"
SAIDA = AQUI.parent / "neo-map.generated.js"

CABECALHO = """// GERADO POR scripts/gerar-mapa.py — NÃO EDITE À MÃO.
//
// Fonte: neo-desktop-tauri/src-tauri/src/services/browser_local.rs (`js_snapshot`).
// Este é, literalmente, o mesmo código que o neo-core injeta por CDP a cada
// passo de uma tarefa. O inspetor visual (content-inspect.js) usa ele para
// desenhar na tela EXATAMENTE o que o agente enxerga — se divergisse, o
// inspetor viraria um segundo mapeador para manter, e o teste deixaria de valer.
//
// Roda no mundo ISOLADO do content script: o `window.__neo` daqui é outro
// objeto, sem relação com o do mundo principal onde o driver trabalha.

globalThis.__neoMapear = function (MAX, CHARS) {
"""

RODAPE = """
};
"""


def extrair() -> str:
    fonte = RUST.read_text(encoding="utf-8")
    helpers = re.search(r'const JS_HELPERS: &str = r#"(.*?)"#;', fonte, re.S)
    if not helpers:
        raise SystemExit("não achei JS_HELPERS no browser_local.rs")

    i = fonte.index("fn js_snapshot(")
    j = fonte.index('r#"', i)
    k = fonte.index('"#', j)
    js = fonte[j + 3 : k]

    # Desfaz o escape do `format!` e troca os dois placeholders por parâmetros.
    js = js.replace("{{", "\x00").replace("}}", "\x01")
    js = js.replace("{helpers}", helpers.group(1))
    js = js.replace("{max}", "MAX").replace("{chars}", "CHARS")
    js = js.replace("\x00", "{").replace("\x01", "}")

    if "{" + "max" + "}" in js or re.search(r"\{[a-z_]+\}", js):
        sobrou = re.findall(r"\{[a-z_]+\}", js)
        raise SystemExit(f"placeholder do format! não tratado: {sorted(set(sobrou))}")

    # O último valor da expressão é o snapshot; dentro de uma função precisa de
    # `return`. O corpo é `<helpers>;\n(() => {...})()`.
    corte = js.rindex("\n(() =>")
    return f"{js[:corte]}\n  return {js[corte:].strip()};"


def main() -> int:
    novo = CABECALHO + extrair() + RODAPE
    checar = "--check" in sys.argv
    atual = SAIDA.read_text(encoding="utf-8") if SAIDA.exists() else ""
    if novo == atual:
        print(f"{SAIDA.name}: em dia")
        return 0
    if checar:
        print(f"{SAIDA.name} DESATUALIZADO — rode: python3 scripts/gerar-mapa.py", file=sys.stderr)
        return 1
    SAIDA.write_text(novo, encoding="utf-8")
    print(f"{SAIDA.name}: {len(novo)} bytes gerados")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
