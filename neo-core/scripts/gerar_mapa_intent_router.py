"""Gera docs/mapa-intent-router.svg, o mapa visual do intent router.

SVG estático, sem dependências. Mudou o roteador? Edite os textos aqui e rode:

    python3 scripts/gerar_mapa_intent_router.py

O SVG é embutido em docs/mapa-tecnico-neo-core.md (§4).
"""

from __future__ import annotations

import sys
from html import escape
from pathlib import Path

W = 1000
M = 40  # margem
CW = W - 2 * M  # largura útil

FONT = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'SF Mono', Menlo, Consolas, monospace"

INK = "#1F2328"
QUIET = "#5B616B"
FAINT = "#8A9099"
LINE = "#E2DED6"
BG = "#FAF8F4"
CARD = "#FFFFFF"

TRILHO = {
    "brain": ("#6D4AFF", "#F2EEFF"),
    "server": ("#0E8A7E", "#E4F5F2"),
    "client": ("#2563EB", "#E8EFFF"),
    "ha": ("#B86E00", "#FFF4DE"),
    "wpp": ("#1A9150", "#E4F6EA"),
    "music": ("#C2387A", "#FCEAF2"),
    "warn": ("#C2412D", "#FDEDEA"),
}

avisos: list[str] = []
out: list[str] = []


def largura(texto: str, tam: float, peso: int = 400, mono: bool = False) -> float:
    fator = 0.61 if mono else (0.575 if peso >= 600 else 0.54)
    return len(texto) * tam * fator


def text(x, y, s, tam=13, cor=INK, peso=400, anchor="start", mono=False, caixa=None, extra=""):
    if caixa is not None and largura(s, tam, peso, mono) > caixa:
        avisos.append(f"estoura {largura(s, tam, peso, mono):.0f}>{caixa:.0f}: {s!r}")
    fam = MONO if mono else FONT
    out.append(
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{fam}" font-size="{tam}" '
        f'font-weight="{peso}" fill="{cor}" text-anchor="{anchor}"{extra}>{escape(s)}</text>'
    )


def rect(x, y, w, h, fill=CARD, stroke=LINE, rx=14, sw=1, extra=""):
    out.append(
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>'
    )


def chip(x, y, s, cor, tint, tam=11.5, mono=True):
    w = largura(s, tam, 500, mono) + 16
    rect(x, y, w, 22, fill=tint, stroke=cor, rx=11, sw=0.8)
    text(x + 8, y + 15.5, s, tam=tam, cor=cor, peso=600, mono=mono)
    return w


def seta(x1, y1, x2, y2, cor=FAINT, tracejada=False, marcador="seta"):
    dash = ' stroke-dasharray="5 5"' if tracejada else ""
    out.append(
        f'<path d="M{x1} {y1} L{x2} {y2}" stroke="{cor}" stroke-width="1.6" fill="none"'
        f'{dash} marker-end="url(#{marcador})"/>'
    )


def secao(y, num, titulo):
    out.append(f'<circle cx="{M + 11}" cy="{y - 5}" r="11" fill="{INK}"/>')
    text(M + 11, y - 0.5, num, tam=12, cor="#FFFFFF", peso=700, anchor="middle")
    text(M + 30, y, titulo, tam=15, peso=700)


def exemplo(x, y, w, frase, nota, cor):
    """Uma linha de exemplo: a frase entre aspas e, embaixo, para onde vai."""
    out.append(f'<rect x="{x}" y="{y}" width="3" height="34" rx="1.5" fill="{cor}"/>')
    text(x + 12, y + 13, f"“{frase}”", tam=13, peso=500, caixa=w - 14)
    text(x + 12, y + 30, nota, tam=11.5, cor=QUIET, caixa=w - 14)


# ── cabeçalho ────────────────────────────────────────────────────────────────
y = 58
text(M, y, "Intent router do Neo", tam=28, peso=750)
text(M, y + 28, "Como cada mensagem escolhe o trilho, e por que quase tudo cai no cérebro",
     tam=15, cor=QUIET, caixa=CW)

# ── 1. o caminho da decisão ─────────────────────────────────────────────────
y = 140
secao(y, "1", "O caminho da decisão")

etapas = [
    ("Mensagem", None, ["chat, voz, WhatsApp,", "agenda ou fim de job,", "com histórico", "e contexto"]),
    ("Atalhos", "pulam o classificador", ["pergunta pendente,", "missão de WhatsApp,", "Claude esperando,", "force_intent"]),
    ("Guardas", "antes do LLM", ["regex de alta precisão:", "treino, “lembra que…”,", "condicional, música,", "“alexa, …” → cérebro"]),
    ("Classificador", "LLM flash, JSON", ["hoje MiniMax m3.1", "JSON com intent,", "onde, needs_plan,", "speech_act, confidence"]),
    ("Vieses", "código, depois do LLM", ["bias_to_brain", "bias_local_browser", "bias_claude_code", "afeto → cérebro", "+ agenda: sem o Mac"]),
]
gap, by, bh = 24, y + 22, 176
bw = (CW - 4 * gap) / 5
for i, (nome, sub, linhas) in enumerate(etapas):
    bx = M + i * (bw + gap)
    destaque = nome == "Vieses"
    rect(bx, by, bw, bh, fill=CARD, stroke=TRILHO["brain"][0] if destaque else LINE, sw=1.4 if destaque else 1)
    text(bx + 14, by + 28, nome, tam=15, peso=700, caixa=bw - 28)
    if sub:
        text(bx + 14, by + 46, sub, tam=11.5, cor=FAINT, peso=500, caixa=bw - 28)
    for j, l in enumerate(linhas):
        mono = nome == "Vieses" and not l.startswith("+")
        text(bx + 14, by + 74 + j * 20, l, tam=11.5 if mono else 12.5, cor=INK if mono else QUIET,
             mono=mono, caixa=bw - 24)
    text(bx + bw - 14, by + 28, str(i), tam=12, cor=FAINT, peso=600, anchor="end")
    if i < len(etapas) - 1:
        seta(bx + bw + 4, by + bh / 2, bx + bw + gap - 4, by + bh / 2)

# fallback: sai do classificador
fx = M + 3 * (bw + gap) + bw / 2
fy = by + bh + 34
seta(fx, by + bh + 2, fx, fy - 2, cor=TRILHO["warn"][0], tracejada=True, marcador="seta-warn")
rect(M, fy, 3 * (bw + gap) + bw + 24, 58, fill=TRILHO["warn"][1], stroke=TRILHO["warn"][0], sw=1)
text(M + 16, fy + 23, "Se o provedor do classificador cai: fallback por palavra-chave",
     tam=13, peso=650, cor=TRILHO["warn"][0], caixa=640)
text(M + 16, fy + 43, "Os vieses rodam, mas o bias_to_brain não: HA (0,6) e WhatsApp (0,7) disparam direto",
     tam=12, cor=INK, caixa=640)

# saída principal: do último bloco para os trilhos
ox = M + 4 * (bw + gap) + bw / 2
seta(ox, by + bh + 2, ox, fy + 82, cor=INK)
text(ox - 10, fy + 34, "intent", tam=12, cor=QUIET, peso=600, anchor="end")
text(ox - 10, fy + 50, "decidido", tam=12, cor=QUIET, peso=600, anchor="end")

# ── 2. os trilhos ────────────────────────────────────────────────────────────
y = fy + 112
secao(y, "2", "Os trilhos, com exemplos")

# cérebro
cor, tint = TRILHO["brain"]
cy, ch = y + 22, 286
rect(M, cy, CW, ch, fill=CARD, stroke=cor, sw=1.6)
rect(M, cy, CW, 64, fill=tint, stroke="none", rx=14)
out.append(f'<rect x="{M}" y="{cy + 50}" width="{CW}" height="14" fill="{tint}"/>')
text(M + 22, cy + 30, "DIRECT_RESPONSE", tam=18, peso=750, cor=cor, mono=True)
text(M + 22 + largura("DIRECT_RESPONSE", 18, 750, True) + 12, cy + 30, "· o cérebro", tam=18, peso=650, cor=cor)
text(M + 22, cy + 51, "Padrão na dúvida. Laço de LLM com todas as tools, PKS, web e sandbox.",
     tam=13, cor=INK, caixa=CW - 200)
chip(W - M - 22 - (largura("padrão na dúvida", 11.5, 500, False) + 16), cy + 20, "padrão na dúvida",
     cor, CARD, mono=False)

col_w = (CW - 44 - 28) / 2
exemplos_cerebro = [
    [
        ("como você está hoje?", "conversa · onde=nenhum, persona a 0,9"),
        ("pega na Wikipédia os 10 países mais populosos em xlsx", "sandbox + file_save · onde=sandbox, 1ª rodada a 0,3"),
        ("o que eu treino na sexta?", "dado pessoal: vem do PKS, sem tool"),
        ("cotação do dólar hoje", "notícia, cotação, esporte: ele busca na web sozinho"),
    ],
    [
        ("lembra que meu CEP mudou", "diretiva de memória: a guarda pega antes do LLM"),
        ("se eu costumo dormir às 23h, apaga a luz", "condicional: o cérebro lê o PKS e aciona o HA por handoff"),
        ("no meu navegador, preenche esse formulário", "bias_local_browser → browser_local_task"),
        ("pede pro Claude criar um script de backup", "bias_claude_code → claude_task"),
    ],
]
for c, coluna in enumerate(exemplos_cerebro):
    ex = M + 22 + c * (col_w + 28)
    for r, (frase, nota) in enumerate(coluna):
        exemplo(ex, cy + 84 + r * 50, col_w, frase, nota, cor)

# faixa: vieses devolvem ao cérebro
fy2 = cy + ch + 18
rect(M, fy2, CW, 46, fill=tint, stroke="none", rx=12)
seta(M + 30, fy2 + 36, M + 30, fy2 + 8, cor=cor, marcador="seta-brain")
text(M + 50, fy2 + 19, "Os vieses devolvem ao cérebro o que não é ordem clara para uma skill",
     tam=13, peso=650, cor=cor, caixa=CW - 70)
text(M + 50, fy2 + 37,
     "não é imperativo ou confiança < 0,75 · “no meu navegador” · cita “claude” · o Neo está irritado",
     tam=12, cor=INK, caixa=CW - 70)
seta(W - M - 30, fy2 + 36, W - M - 30, fy2 + 8, cor=cor, marcador="seta-brain")


def cartao(x, y0, w, h, chave, nome, sub, exs, rodape=None):
    cor, tint = TRILHO[chave]
    rect(x, y0, w, h, fill=CARD, stroke=LINE)
    out.append(f'<rect x="{x}" y="{y0}" width="{w}" height="5" rx="2.5" fill="{cor}"/>')
    out.append(f'<rect x="{x + 1}" y="{y0 + 2}" width="{w - 2}" height="12" fill="{CARD}"/>')
    out.append(f'<path d="M{x + 14} {y0} H{x + w - 14}" stroke="{cor}" stroke-width="4" stroke-linecap="round"/>')
    text(x + 18, y0 + 34, nome, tam=15, peso=750, cor=cor, mono=True, caixa=w - 36)
    text(x + 18, y0 + 54, sub, tam=12, cor=QUIET, caixa=w - 36)
    for r, (frase, nota) in enumerate(exs):
        exemplo(x + 18, y0 + 72 + r * 48, w - 36, frase, nota, cor)
    if rodape:
        out.append(f'<path d="M{x + 18} {y0 + h - 40} H{x + w - 18}" stroke="{LINE}"/>')
        for k, l in enumerate(rodape):
            text(x + 18, y0 + h - 22 + k * 16, l, tam=11.5, cor=QUIET, caixa=w - 36)


r1 = fy2 + 46 + 22
cartao(M, r1, 360, 270, "server", "SERVER_TOOL", "Tool rápida que roda no neo-core", [
    ("como vai estar o tempo amanhã?", "get_weather"),
    ("quais vozes você tem?", "voice_list"),
    ("troca a voz do TTS", "config_set tts.voice"),
    ("tira um print do browser", "browser_screenshot, do servidor"),
])
cartao(M + 380, r1, CW - 380, 270, "client", "CLIENT_TOOL", "Só o que exige o Mac dele. Três tools e nenhuma outra", [
    ("quanto espaço livre tenho no disco?", "run_command no Mac, aprovação pela política"),
    ("salva aquela planilha no Desktop", "save_generated_file com o file_id do histórico"),
    ("manda aquele pdf da nota fiscal que tá no Downloads", "needs_plan=true: o plano faz ls/find antes de assumir o nome"),
    ("grava esse texto em ~/notas.txt", "write_file, só com o texto ditado nesta mensagem"),
])

r2 = r1 + 270 + 20
w3 = (CW - 40) / 3
cartao(M, r2, w3, 230, "ha", "HOME_ASSISTANT", "Skill da casa. Só imperativo claro", [
    ("acende a luz da sala", "imperativo, confiança ≥ 0,75: dispara"),
    ("ativa o modo cinema", "dispara; a skill acha a entidade"),
    ("se escurecer, liga a luz?", "condicional: vai ao cérebro"),
])
cartao(M + w3 + 20, r2, w3, 230, "wpp", "WHATSAPP_MESSAGING", "Mensagem em nome dele", [
    ("avisa minha mãe que chego tarde", "“minha mãe” é resolvido na agenda"),
    ("pede uma pizza pelo zap", "comercial também é este trilho"),
    ("se o Zé não responder, manda de novo", "condicional: vai ao cérebro"),
])
cartao(M + 2 * (w3 + 20), r2, w3, 230, "music", "MUSIC", "Nunca direto: chega por handoff", [
    ("toca Djavan", "cérebro lê o gosto → [[HANDOFF:music]]"),
    ("pausa a música", "handoff com a ação pausar"),
    ("o que tá tocando?", "status da mídia, sem comando"),
])

# ── 3. campos ────────────────────────────────────────────────────────────────
y = r2 + 230 + 56
secao(y, "3", "Os campos que acompanham o intent")
pw = (CW - 3 * 20) / 4
ph = 176
paineis = [
    ("onde", ["Só muda o cérebro: o aviso", "de sandbox e a 1ª rodada", "a 0,3 em vez de 0,9."], ["sandbox", "maquina", "navegador", "nenhum"]),
    ("needs_plan", ["Só vale no CLIENT_TOOL:", "o PlanManager sonda antes", "de agir. No cérebro, é ignorado."], ["true", "false", "forçado"]),
    ("speech_act + confidence", ["Skill com efeito colateral", "só dispara com imperativo", "e confiança ≥ 0,75."], ["imperative", "question", "conditional"]),
    ("pipeline_plan", ["Cascata declarada de 2 ou", "mais passos com intents", "diferentes, em fila."], ["passo 1", "passo 2", "…"]),
]
for i, (nome, linhas, chips) in enumerate(paineis):
    px = M + i * (pw + 20)
    rect(px, y + 22, pw, ph, fill=CARD, stroke=LINE)
    text(px + 16, y + 50, nome, tam=13.5, peso=700, mono=True, caixa=pw - 32)
    for j, l in enumerate(linhas):
        text(px + 16, y + 74 + j * 18, l, tam=12, cor=QUIET, caixa=pw - 28)
    pos, cx, linha_chips = [], px + 16, 0
    for s in chips:
        wchip = largura(s, 11, 500, True) + 16
        if cx + wchip > px + pw - 12:
            cx, linha_chips = px + 16, linha_chips + 1
        pos.append((cx, linha_chips, s))
        cx += wchip + 6
    base = y + 22 + ph - 14 - (linha_chips + 1) * 26 + 4
    for cx, lc, s in pos:
        chip(cx, base + lc * 26, s, INK, "#F3F1EC", tam=11)

# ── rodapé ───────────────────────────────────────────────────────────────────
y = y + 22 + ph + 28
rect(M, y, CW, 64, fill=INK, stroke="none", rx=14)
text(M + 24, y + 27, "Regra de ouro: na dúvida, DIRECT_RESPONSE.", tam=15, peso=700, cor="#FFFFFF")
text(M + 24, y + 48, "Quem roteia é o intent. Os outros campos só valem dentro do trilho que ele escolheu.",
     tam=13, cor="#D8DCE2", caixa=CW - 48)
H = y + 64 + 36
text(W - M, H - 14, "neo-core · core/router/intent_router.py · 15/11/2026", tam=11, cor=FAINT, anchor="end")

defs = f"""<defs>
  <marker id="seta" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{FAINT}"/></marker>
  <marker id="seta-warn" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{TRILHO['warn'][0]}"/></marker>
  <marker id="seta-brain" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{TRILHO['brain'][0]}"/></marker>
</defs>"""

svg = (
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H:.0f}" width="{W}" height="{H:.0f}" '
    f'role="img" aria-label="Mapa do intent router do Neo">\n{defs}\n'
    f'<rect width="{W}" height="{H:.0f}" rx="18" fill="{BG}"/>\n'
    + "\n".join(out)
    + "\n</svg>\n"
)

destino = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs" / "mapa-intent-router.svg"
destino.write_text(svg, encoding="utf-8")
print(f"ok {destino} {W}x{H:.0f}")
for a in avisos:
    print("AVISO", a)
