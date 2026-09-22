#!/usr/bin/env python3
"""Conversa em balões gravada rolando (WhatsApp, Telegram…) → mensagens.

Usado pelo ocr_video.py (modo chat). Só ffmpeg, tesseract, numpy e Pillow — o que
existe no sandbox (sem scipy/opencv).

Por que não o OCR da tela inteira (session-1fcaa440: 14 entradas, contra 188 linhas
de uma leitura com modelo de visão): o remetente não está no texto, está no LADO e
na COR do balão; o horário do canto sai ilegível na tela reduzida; e juntar o texto
de quadros vizinhos por semelhança duplica e engole mensagens. Aqui cada balão é
localizado na imagem, ganha posição GLOBAL na conversa (pela rolagem medida) e é
lido uma vez, no quadro em que aparece melhor.

Etapas (cada uma grava o que produziu em DIR/chat e é pulada ao retomar):
1. reconhecimento: quadros pequenos a 4/s. A área que muda é o painel da conversa, a
   correlação do perfil das linhas dá a rolagem, e sai um quadro-chave a cada ~45%
   de painel rolado ou 3 s de tela mudando;
2. quadros-chave em resolução nativa, só o painel;
3. balões: cores por frequência e lado → máscara → abertura → componentes →
   separação pelo recuo dos cantos; o alinhamento dá o remetente. Mais: mídia,
   citação, texto centralizado (data/aviso) e área tapada por sobreposição;
4. junção: o mesmo balão visto em vários quadros vira um grupo, lido no melhor
   exemplar (horário por votação); datas, botões e reparo dos horários pela ordem.

Os limiares em pixels foram medidos no WhatsApp do macOS gravado em tela Retina e
escalam por `u` = altura típica de linha de texto ÷ GLIFO_REF.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

RECON_FPS = 4
RECON_LARGURA = 800
#: Novo quadro-chave a cada fração do painel rolada: sobra sobreposição para todo
#: balão aparecer inteiro em algum quadro…
CHAVE_ROLAGEM = 0.45
#: …ou a cada tantos segundos com a tela mudando sem rolar (mensagem chegando,
#: sobreposição abrindo), medido como diferença média de luma contra o último.
CHAVE_SEGUNDOS = 3
CHAVE_MUDANCA = 2.0
#: Altura mediana (px) das faixas de texto no vídeo em que os limiares foram medidos.
GLIFO_REF = 26.0
#: Distância máxima (por canal) de um pixel ao tom do balão para contar como balão.
#: Medido no WhatsApp: o botão de resposta rápida fica a ~14 do cinza do balão (com
#: 10 o botão sumia) e a caixa da citação a ~10 do verde — por isso a citação NÃO
#: sai da máscara, sai da cor mediana das linhas (`achar_citacao`).
DIST_BALAO = 20
#: Menos balões que isto no vídeo inteiro: não é conversa, vai para o OCR de texto.
MIN_BALOES = 4
#: Quantos quadros-chave em RGB nativo ficam na memória durante a leitura (~12 MB
#: cada numa tela Retina; o sandbox tem 1 GB).
IMAGENS_EM_MEMORIA = 6


def log(msg: str) -> None:
    print(msg, flush=True)


def mmss(segundos: float) -> str:
    total = int(round(segundos))
    return f"{total // 60:02d}:{total % 60:02d}"


# ── Geometria (pura — coberta por tests/skills/test_ocr_video_chat.py) ───────


def _px(valor: float, u: float) -> int:
    return max(1, int(round(valor * u)))


def spans(on) -> list[tuple[int, int]]:
    """Sequências de True como (início, fim exclusivo)."""
    a = np.asarray(on, dtype=np.int8).ravel()
    d = np.diff(np.concatenate(([0], a, [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))


def luma(rgb):
    rgb = rgb.astype(np.float32)
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


def horiz_close(mask, k: int):
    """Fechamento só na horizontal: emenda a moldura fina que o texto interrompe."""
    m = mask.astype(np.int32)
    largura = 2 * k + 1
    cs = np.pad(np.cumsum(np.pad(m, ((0, 0), (k, k))), axis=1), ((0, 0), (1, 0)))
    dil = (cs[:, largura:] - cs[:, :-largura]) > 0
    d = np.pad(dil.astype(np.int32), ((0, 0), (k, k)), constant_values=1)
    cs2 = np.pad(np.cumsum(d, axis=1), ((0, 0), (1, 0)))
    return (cs2[:, largura:] - cs2[:, :-largura]) == largura


def max_run_rows(mask):
    d = np.diff(np.pad(mask.astype(np.int8), ((0, 0), (1, 1))), axis=1)
    out = np.zeros(mask.shape[0], dtype=np.int32)
    for y in range(mask.shape[0]):
        s = np.flatnonzero(d[y] == 1)
        if s.size:
            out[y] = int((np.flatnonzero(d[y] == -1) - s).max())
    return out


def _janela(mask, r: int):
    k = 2 * r + 1
    m = np.pad(mask.astype(np.int32), r)
    sat = np.pad(m, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return sat[k:, k:] - sat[:-k, k:] - sat[k:, :-k] + sat[:-k, :-k], k * k


def box_erode(mask, r: int):
    soma, n = _janela(mask, r)
    return soma == n


def box_dilate(mask, r: int):
    soma, _ = _janela(mask, r)
    return soma > 0


def components(mask) -> list[tuple[int, int, int, int, int]]:
    """Regiões conectadas de um mask booleano: [(x0, y0, x1, y1, area)].

    Rotulagem por sequências horizontais (RLE) + union-find: cada linha vira poucas
    sequências, e uma sequência herda o rótulo das da linha de cima que ela toca.
    """
    d = np.diff(np.pad(mask.astype(np.int8), ((0, 0), (1, 1))), axis=1)
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    runs = []
    prev: list[tuple[int, int, int]] = []
    for y in range(mask.shape[0]):
        starts = np.flatnonzero(d[y] == 1)
        ends = np.flatnonzero(d[y] == -1)
        cur = []
        j = 0
        for x0, x1 in zip(starts.tolist(), ends.tolist()):
            while j < len(prev) and prev[j][1] <= x0:
                j += 1
            lab = None
            k = j
            while k < len(prev) and prev[k][0] < x1:
                raiz = find(prev[k][2])
                if lab is None:
                    lab = raiz
                elif raiz != lab:
                    parent[raiz] = lab
                k += 1
            if lab is None:
                lab = len(parent)
                parent.append(lab)
            cur.append((x0, x1, lab))
            runs.append((y, x0, x1, lab))
        prev = cur
    caixas: dict[int, list[int]] = {}
    for y, x0, x1, lab in runs:
        r = find(lab)
        b = caixas.get(r)
        if b is None:
            caixas[r] = [x0, y, x1, y + 1, x1 - x0]
        else:
            b[0] = min(b[0], x0)
            b[1] = min(b[1], y)
            b[2] = max(b[2], x1)
            b[3] = max(b[3], y + 1)
            b[4] += x1 - x0
    return [tuple(b) for b in caixas.values()]


def split_by_notches(fill, ya: int, yb: int, xa: int, xb: int, u: float = 1.0) -> list[tuple[int, int]]:
    """Separa balões colados pelo recuo dos cantos arredondados na borda.

    Dois balões seguidos do mesmo lado quase não têm vão; o que denuncia a fronteira
    é a borda recuar por poucas linhas (o canto) na esquerda ou na direita. Recuo
    LONGO é só largura diferente. Pedaço mais baixo que uma linha de texto volta para
    o vizinho: o botão fixo "rolar para baixo" por cima da borda também recorta, e
    fatiava um balão de uma linha em 38+24px.
    """
    seg = fill[ya:yb, xa:xb]
    h, w = seg.shape
    tem = seg.any(axis=1)
    if h < 60 * u or not tem.any():
        return [(ya, yb)]
    esq = np.where(tem, seg.argmax(axis=1), w)
    dir_ = np.where(tem, w - 1 - seg[:, ::-1].argmax(axis=1), 0)
    e_ref, d_ref = np.median(esq[tem]), np.median(dir_[tem])
    recuo = max(2.0, 4 * u)
    notch = (esq - e_ref >= recuo) | (d_ref - dir_ >= recuo)
    cortes = [ya + (a + b) // 2 for a, b in spans(notch) if 2 <= b - a <= 24 * u and a > 10 * u and b < h - 10 * u]
    limites = [ya] + cortes + [yb]
    pedacos = [[limites[i], limites[i + 1]] for i in range(len(limites) - 1)]
    i = 0
    while len(pedacos) > 1 and i < len(pedacos):
        a, b = pedacos[i]
        if b - a >= 50 * u:
            i += 1
            continue
        vizinho_baixo_menor = i + 1 < len(pedacos) and (
            pedacos[i + 1][1] - pedacos[i + 1][0] < pedacos[i - 1][1] - pedacos[i - 1][0]
        )
        if i == 0 or vizinho_baixo_menor:
            pedacos[i + 1][0] = a
        else:
            pedacos[i - 1][1] = b
        pedacos.pop(i)
    return [(a, b) for a, b in pedacos]


def row_profiles(fr, bg: float):
    m = (np.abs(fr - bg) > 60).astype(np.float32)
    return np.stack([b.mean(axis=1) for b in np.array_split(m, 8, axis=1)], axis=1)


def best_shift(a, b, lo: int, hi: int) -> tuple[int, float]:
    """Deslocamento d que melhor alinha a[d:] com b (conteúdo subiu d linhas)."""
    h = a.shape[0]
    best = (1e9, 0)
    for d in range(lo, hi + 1):
        aa, bb = (a[d:], b[: h - d]) if d >= 0 else (a[: h + d], b[-d:])
        if aa.shape[0] < h * 0.3:
            continue
        e = float(np.abs(aa - bb).mean())
        if e < best[0]:
            best = (e, d)
    return best[1], best[0]


def painel(act, frac: float = 0.2) -> tuple[int, int, int, int]:
    """Caixa (x0, y0, x1, y1) da maior faixa contínua de atividade nas duas direções."""
    h, w = act.shape
    if float(act.max()) <= 0:
        return 0, 0, w, h
    col, row = act.mean(axis=0), act.mean(axis=1)
    x0, x1 = max(spans(col > col.max() * frac), key=lambda s: s[1] - s[0])
    y0, y1 = max(spans(row > row.max() * frac), key=lambda s: s[1] - s[0])
    return x0, y0, x1, y1


def estimar_unidade(L, bgl: float) -> float:
    """Escala dos limiares: altura mediana das faixas de texto ÷ GLIFO_REF (em ¼)."""
    alturas = [b - a for a, b in spans((np.abs(L - bgl) > 60).sum(axis=1) >= 3) if 6 <= b - a <= 120]
    if len(alturas) < 5:
        return 1.0
    return min(2.5, max(0.5, round(float(np.median(alturas)) / GLIFO_REF * 4) / 4))


def _cor_do_balde(k):
    return np.array([k // 4096, (k // 64) % 64, k % 64]) * 6 + 3


def analisar_cores(rgb) -> dict | None:
    """Fundo, cor do balão de ENTRADA (lado esquerdo) e de SAÍDA (direito).

    Cor = balde de 6 níveis. Balão = cor frequente, longe do fundo, que forma faixas
    chapadas largas; o lado sai da posição média. O preenchimento varia entre tons
    vizinhos (compressão), então cada lado é um GRUPO de tons da paleta. None quando
    não há cor de balão nenhuma.
    """
    sub = rgb[::3, ::3].reshape(-1, 3)
    q = sub // 6
    chave = q[:, 0] * 4096 + q[:, 1] * 64 + q[:, 2]
    vals, counts = np.unique(chave, return_counts=True)
    ordem = np.argsort(-counts)
    bg = _cor_do_balde(vals[ordem[0]])
    bgl = float(0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2])
    u = estimar_unidade(luma(rgb), bgl)
    total = int(counts.sum())
    topo = ordem[:30]
    cands: list[dict] = []
    for i in ordem[1:30]:
        c = _cor_do_balde(vals[i])
        if int(np.abs(c - bg).max()) < 18 or any(np.abs(c - t["c"]).max() < 14 for t in cands):
            continue
        vizinha = min((int(np.abs(c - _cor_do_balde(vals[j])).max()) for j in topo if vals[j] != vals[i]), default=24)
        tol = max(4, min(12, vizinha // 2))
        mask = np.abs(rgb - c).max(axis=2) <= tol
        xs = np.nonzero(mask)[1]
        cands.append({
            "c": c, "n": int(counts[i]),
            "flat": float((max_run_rows(mask[::4]) >= 60 * u).mean()),
            "x": float(xs.mean() / mask.shape[1]) if xs.size else 0.5,
        })
    boas = [t for t in cands if t["flat"] >= 0.05 and t["n"] >= 0.01 * total]
    esq = max((t for t in boas if t["x"] < 0.5), key=lambda t: t["n"] * t["flat"], default=None)
    dir_ = max((t for t in boas if t["x"] >= 0.5), key=lambda t: t["n"] * t["flat"], default=None)
    if esq is None and dir_ is None:
        return None
    paleta = [_cor_do_balde(vals[j]) for j in ordem[:12]]
    for t in (esq, dir_):
        if t is not None and not any(np.array_equal(t["c"], c) for c in paleta):
            paleta.append(t["c"])

    def grupo(t):
        if t is None:
            return []
        return [i for i, c in enumerate(paleta) if np.abs(c - t["c"]).max() <= 9 and np.abs(c - bg).max() > 14]

    return {
        "fundo": [int(v) for v in bg], "bgl": bgl, "u": u,
        "paleta": [[int(v) for v in c] for c in paleta],
        "entrada": [int(v) for v in esq["c"]] if esq else None,
        "saida": [int(v) for v in dir_["c"]] if dir_ else None,
        "grupo_entrada": grupo(esq), "grupo_saida": grupo(dir_),
    }


def rotular(rgb, paleta):
    """Cada pixel vai para a cor MAIS PRÓXIMA da paleta: o corte entre o cinza do
    balão e o do fundo cai no meio do caminho sozinho, sem tolerância fixa."""
    melhor = np.full(rgb.shape[:2], 10 ** 6, dtype=np.int32)
    rot = np.zeros(rgb.shape[:2], dtype=np.int16)
    for i, c in enumerate(paleta):
        dist = np.abs(rgb - np.asarray(c)).max(axis=2)
        menor = dist < melhor
        melhor[menor] = dist[menor]
        rot[menor] = i
    return rot, melhor


def moldura(bruto, xa: int, ya: int, xb: int, yb: int, u: float = 1.0) -> bool:
    """Anexo emoldurado: faixa fina da cor do balão nos QUATRO lados, miolo de outra cor.

    Pilha de balões de larguras diferentes também tem caixa envolvente pouco
    preenchida, mas a coluna da borda oposta ao lado não fica cheia de cima a baixo.
    """
    reg = bruto[ya:yb, xa:xb]
    f, m = _px(14, u), _px(30, u)
    lados = (reg[:f].mean(axis=1).max(), reg[-f:].mean(axis=1).max(),
             reg[:, :f].mean(axis=0).max(), reg[:, -f:].mean(axis=0).max())
    miolo = reg[m:-m, m:-m].mean() if reg.shape[0] > 80 * u and reg.shape[1] > 80 * u else 1.0
    return min(lados) >= 0.8 and miolo < 0.35


def achar_citacao(rgb, xa: int, ya: int, xb: int, yb: int, fill, u: float = 1.0) -> list[int] | None:
    """Faixa [topo, fim) da citação de resposta, relativa ao topo do balão.

    A citação é uma caixa de OUTRA cor logo no topo do balão. O tom é próximo demais
    do balão para a máscara de cor (fica a ~10 do verde, dentro do limiar que o botão
    precisa), então cada linha é julgada pela cor MEDIANA — o texto é minoria na
    linha: >=40px de linhas com mediana diferente do balão, começando nos primeiros
    ~34px e terminando antes do fim.
    """
    if fill is None or yb - ya <= 90 * u or xb - xa <= 200 * u:
        return None
    inset = _px(14, u)
    medianas = np.median(rgb[ya:yb, xa + inset:xb - inset], axis=1)
    baixo = np.abs(medianas - np.asarray(fill)).max(axis=1) > 6
    limite = min(int(34 * u), len(baixo))
    topo = _px(4, u)
    while topo < limite and not baixo[topo]:
        topo += 1
    if topo >= limite:
        return None
    fim = topo
    while fim < len(baixo) and baixo[fim]:
        fim += 1
    if fim - topo >= 40 * u and fim < len(baixo) - 20 * u:
        return [int(topo), int(fim)]
    return None


def centralizados(L, bgl: float, bolhas: list[dict], W: int, H: int, u: float = 1.0) -> list[list[int]]:
    """Separador de data ("quarta-feira") e aviso do sistema: texto CENTRALIZADO direto
    sobre o fundo, fora de qualquer balão. A pílula é quase da cor do fundo e não sai
    na máscara de cor — sai pela tinta.

    O rótulo de data flutuante fica colado no topo e não conta: ele se repete em todo
    quadro e poria datas no meio do dia errado.
    """
    tinta = np.abs(L - bgl) > 60
    f = _px(6, u)
    for b in bolhas:
        xa, ya, xb, yb = b["box"]
        tinta[max(0, ya - f):yb + f, max(0, xa - f):xb + f] = False
    faixas: list[list[int]] = []
    for a, b in spans(tinta.sum(axis=1) >= 3):
        if faixas and a - faixas[-1][1] <= 20 * u:  # linhas do mesmo aviso
            faixas[-1][1] = b
        else:
            faixas.append([a, b])
    caixas = []
    for a, b in faixas:
        if not 10 * u <= b - a <= 160 * u or a < 110 * u:
            continue
        grupos: list[list[int]] = []
        for c0, c1 in spans(tinta[a:b].any(axis=0)):
            if grupos and c0 - grupos[-1][1] < 60 * u:
                grupos[-1][1] = c1
            else:
                grupos.append([c0, c1])
        for c0, c1 in grupos:
            if c1 - c0 >= 30 * u and abs((c0 + c1) / 2 / W - 0.5) <= 0.06:
                px, py = int(24 * u), int(12 * u)
                caixas.append([int(max(0, c0 - px)), int(max(0, a - py)), int(min(W, c1 + px)), int(min(H, b + py))])
    return caixas


def segmentar_quadro(rgb, cores: dict) -> list[dict]:
    """Balões de um quadro-chave: [{kind, cor, box, citacao, preench, corte}].

    kind: entrada | saida (encosta à esquerda/direita), centro (data, aviso, cartão),
    cabecalho (colado no topo), midia_entrada | midia_saida, outro (não encosta em
    lado nenhum: algo por cima da conversa, como a Central de Controle).
    """
    H, W = rgb.shape[:2]
    u = cores["u"]
    rot, dist = rotular(rgb, cores["paleta"])
    m_in = np.isin(rot, cores["grupo_entrada"]) & (dist <= DIST_BALAO)
    m_out = np.isin(rot, cores["grupo_saida"]) & (dist <= DIST_BALAO)
    L = luma(rgb)
    bgl = cores["bgl"]
    bolhas: list[dict] = []
    midias: list[tuple[int, int, int, int]] = []

    def dentro_de_midia(xa, ya, xb, yb) -> bool:
        f = 12 * u
        return any(xa >= m[0] - f and xb <= m[2] + f and ya >= m[1] - f and yb <= m[3] + f for m in midias)

    def midia(xa, ya, xb, yb, area, cor):
        esq, dir_ = xa <= 0.07 * W, xb >= 0.93 * W
        if esq == dir_:
            return
        lado = "entrada" if esq else "saida"
        midias.append((xa, ya, xb, yb))
        bolhas.append({"kind": "midia_" + lado, "cor": cor or lado, "box": [int(xa), int(ya), int(xb), int(yb)],
                       "citacao": None, "preench": round(area / max(1, (xb - xa) * (yb - ya)), 2),
                       "corte": ya <= 2 or yb >= H - 2})

    # Foto/vídeo SEM moldura: a miniatura ocupa o balão inteiro e é um bloco bem mais
    # claro que o fundo (medido: 100% dos pixels >25 de luma acima do fundo; fundo e
    # balão de entrada, 0%). A abertura larga some com o texto fino.
    r_m = _px(8, u)
    claro = box_dilate(box_erode((np.abs(L - bgl) > 25) & ~(m_in | m_out), r_m), r_m)
    for xa, ya, xb, yb, area in components(claro):
        w, h = xb - xa, yb - ya
        if w >= 300 * u and h >= 200 * u and area / (w * h) >= 0.6:
            midia(xa, ya, xb, yb, area, None)

    for lado, bruto in (("entrada", m_in), ("saida", m_out)):
        if not bruto.any():
            continue
        # Anexo com moldura fina da cor do balão: a abertura apagaria a moldura, então
        # ele vem da máscara bruta.
        for xa, ya, xb, yb, area in components(horiz_close(bruto, 2)):
            w, h = xb - xa, yb - ya
            if (w > 300 * u and h > 200 * u and area / (w * h) < 0.3
                    and moldura(bruto, xa, ya, xb, yb, u) and not dentro_de_midia(xa, ya, xb, yb)):
                midia(xa, ya, xb, yb, area, lado)
        r = _px(3, u)
        mask = box_dilate(box_erode(bruto, r), r)
        for xa, ya, xb, yb, _area in components(mask):
            if xb - xa < 40 * u or yb - ya < 24 * u or dentro_de_midia(xa, ya, xb, yb):
                continue
            for sa, sb in split_by_notches(mask, ya, yb, xa, xb, u):
                if sb - sa < 24 * u:
                    continue
                esq, dir_ = xa <= 0.07 * W, xb >= 0.93 * W
                if abs((xa + xb) / 2 / W - 0.5) < 0.1 and not esq and not dir_:
                    kind = "cabecalho" if sa < 70 * u else "centro"
                elif dir_ and not esq:
                    kind = "saida"
                elif esq and not dir_:
                    kind = "entrada"
                else:
                    kind = "outro"
                citacao = achar_citacao(rgb, xa, sa, xb, sb, cores.get(lado), u) if kind in ("entrada", "saida") else None
                preench = float(bruto[sa:sb, xa:xb].mean())
                # Balão de verdade é quase todo da própria cor; painel translúcido por
                # cima ou pedaço de miniatura não é. A citação baixa o preenchimento.
                if kind in ("entrada", "saida") and preench < 0.45 and not citacao:
                    continue
                bolhas.append({"kind": kind, "cor": lado, "box": [int(xa), int(sa), int(xb), int(sb)],
                               "citacao": citacao, "preench": round(preench, 2), "corte": sa <= 2 or sb >= H - 2})

    for caixa in centralizados(L, bgl, bolhas, W, H, u):
        bolhas.append({"kind": "centro", "cor": "fundo", "box": caixa, "citacao": None, "preench": 0.0,
                       "corte": caixa[1] <= 2 or caixa[3] >= H - 2})
    bolhas.sort(key=lambda b: (b["box"][1], b["box"][0]))
    return bolhas


def cobertura(bolhas: list[dict], u: float = 1.0) -> tuple[float, float, float, float] | None:
    """Área tapada por algo POR CIMA da conversa (Central de Controle, notificação):
    vários blocos que não encostam em lado nenhum nem ficam no centro."""
    outros = [b["box"] for b in bolhas if b["kind"] == "outro"]
    if len(outros) < 3:
        return None
    f = 40 * u
    return (min(b[0] for b in outros) - f, min(b[1] for b in outros) - f,
            max(b[2] for b in outros) + f, max(b[3] for b in outros) + f)


def cruza(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _nota(b: dict) -> tuple:
    """Melhor exemplar de um balão: inteiro na tela, mais alto, mais largo (sem nada
    tapando a borda)."""
    xa, ya, xb, yb = b["box"]
    return (not b["corte"], yb - ya, xb - xa)


def agrupar(cand: list[dict], u: float = 1.0) -> list[dict]:
    """O mesmo balão visto em vários quadros → um grupo.

    Mesmo lado, uma das bordas horizontais batendo e sobreposição vertical (na
    posição global) de mais da metade do menor.
    """
    tol = 14 * u
    grupos: list[dict] = []
    for c in sorted(cand, key=lambda c: c["gy0"]):
        alvo = None
        for g in reversed(grupos[-15:]):
            r = g["rep"]
            if g["lado"] != c["lado"] or (abs(r["box"][0] - c["box"][0]) > tol and abs(r["box"][2] - c["box"][2]) > tol):
                continue
            ov = min(g["gy1"], c["gy1"]) - max(g["gy0"], c["gy0"]) + 6 * u
            if ov > 0.5 * min(g["gy1"] - g["gy0"], c["gy1"] - c["gy0"]):
                alvo = g
                break
        if alvo is None:
            grupos.append({"lado": c["lado"], "gy0": c["gy0"], "gy1": c["gy1"], "rep": c, "membros": [c]})
            continue
        alvo["membros"].append(c)
        alvo["gy0"], alvo["gy1"] = min(alvo["gy0"], c["gy0"]), max(alvo["gy1"], c["gy1"])
        if _nota(c) > _nota(alvo["rep"]):
            alvo["rep"] = c
    return grupos


def marcar_botoes(grupos: list[dict], u: float = 1.0) -> None:
    """Botão de resposta rápida ("Sim", "Toque para ver"): bloco colado embaixo de um
    balão de entrada, mesma largura, quase sem tinta e mais alto que uma linha."""
    ultimo: dict[str, dict] = {}
    for g in sorted(grupos, key=lambda g: g["gy0"]):
        ant = ultimo.get(g["lado"])
        r = g["rep"]
        altura = r["box"][3] - r["box"][1]
        if (ant is not None and g["lado"] == "entrada" and g["gy0"] - ant["gy1"] <= 10 * u
                and 75 * u <= altura <= 100 * u and r["preench"] >= 0.9
                and abs(r["box"][0] - ant["rep"]["box"][0]) <= 10 * u
                and abs(r["box"][2] - ant["rep"]["box"][2]) <= 10 * u):
            g["botao"] = True
        ultimo[g["lado"]] = g


def local_hora(reg, fill, u: float = 1.0) -> tuple[int, int, int, int] | None:
    """Caixa (x0, y0, x1, y1) do horário dentro do recorte do canto do balão.

    O horário é o grupo de colunas com tinta mais à direita da ÚLTIMA faixa de texto,
    separado do texto por um vão largo. Recortar uma largura fixa pegava o fim da
    frase junto, e o OCR de dígitos transformava letra em número.
    """
    tinta = np.abs(reg - np.asarray(fill)).max(axis=2) > 40
    juntas: list[list[int]] = []
    for a, b in spans(tinta.mean(axis=1) > 0.004):
        if juntas and a - juntas[-1][1] <= 6 * u:
            juntas[-1][1] = b
        else:
            juntas.append([a, b])
    if not juntas:
        return None
    r0, r1 = juntas[-1]
    if r1 - r0 < 8 * u:
        return None
    grupos: list[list[int]] = []
    for a, b in spans(tinta[r0:r1].any(axis=0)):
        if grupos and a - grupos[-1][1] < 12 * u:
            grupos[-1][1] = b
        else:
            grupos.append([a, b])
    for a, b in reversed(grupos):
        if 30 * u <= b - a <= 150 * u:
            return a, r0, b, r1
    return None


# ── Texto, horários e montagem (puro) ─────────────────────────────────────────

DIAS = re.compile(
    r"^(segunda|terça|terca|quarta|quinta|sexta)(-?feira)?$|^(sábado|sabado|domingo|hoje|ontem)$"
    r"|^(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|yesterday)$"
    r"|^\d{1,2}/\d{1,2}(/\d{2,4})?$|^\d{1,2}de[a-zç]+(de\d{4})?$",
    re.I,
)
NUM = "0-9oOqgsSlIaBz"
MAPA_NUM = str.maketrans({"o": "0", "O": "0", "q": "9", "g": "9", "s": "5", "S": "5",
                          "l": "1", "I": "1", "a": "8", "B": "8", "z": "2"})
HORA_FIM = re.compile(rf"\s*([{NUM}]{{1,2}})[:.,]([{NUM}]{{2}})[^\w\n]{{0,2}}\s*\S{{0,3}}\s*$")
#: Trocas de dígito que o tesseract faz no horário pequeno do canto.
CONFUSAO = {"0": "896", "1": "74", "3": "85", "4": "1", "5": "8693", "6": "508", "7": "1", "8": "0356", "9": "058"}


def eh_data(texto: str) -> bool:
    return bool(DIAS.match(re.sub(r"[^a-zà-ú0-9/\-]", "", (texto or "").lower())))


def hora_do_texto(ultima_linha: str | None) -> str | None:
    """Horário colado no fim da última linha lida do corpo ("… normal 09:4O")."""
    m = HORA_FIM.search(ultima_linha or "")
    if not m:
        return None
    h, mm = m.group(1).translate(MAPA_NUM), m.group(2).translate(MAPA_NUM)
    return f"{int(h):02d}:{mm}" if h.isdigit() and mm.isdigit() else None


def hora_valida(h: str | None) -> bool:
    return bool(h) and bool(re.fullmatch(r"\d{2}:\d{2}", h)) and int(h[:2]) < 24 and int(h[3:]) < 60


def _variantes(h: str) -> list[str]:
    digs = [i for i, ch in enumerate(h) if ch.isdigit()]
    out = set()
    for i in digs:
        for troca in CONFUSAO.get(h[i], ""):
            v = h[:i] + troca + h[i + 1:]
            out.add(v)
            for j in digs:
                if j > i:
                    for t2 in CONFUSAO.get(v[j], ""):
                        out.add(v[:j] + t2 + v[j + 1:])
    return [v for v in out if hora_valida(v)]


def _minutos(h: str) -> int:
    return int(h[:2]) * 60 + int(h[3:])


def reparar_horas(itens: list[dict]) -> None:
    """Horários só crescem DENTRO de um dia (a data separa os blocos).

    Âncora = horário com 2+ votos que respeita a ordem (maior subsequência não
    decrescente). O resto escolhe, entre as próprias leituras e as trocas de dígito,
    a que cabe entre as âncoras vizinhas, mais perto da anterior; sem nenhuma que
    caiba, fica sem horário (melhor que um errado). Muda `hora` e marca `reparada`.
    """
    blocos: list[list[dict]] = [[]]
    for it in itens:
        if it["tipo"] == "data":
            blocos.append([])
        elif it["tipo"] in ("entrada", "saida"):
            blocos[-1].append(it)
    for bloco in blocos:
        cand = [i for i, it in enumerate(bloco) if hora_valida(it.get("hora")) and it.get("votos", 0) >= 2]
        seqs: list[list[int]] = []
        for i in cand:
            base = max((s for s in seqs if _minutos(bloco[s[-1]]["hora"]) <= _minutos(bloco[i]["hora"])), key=len, default=[])
            seqs.append(base + [i])
        ancoras = set(max(seqs, key=len, default=[]))
        for idx, it in enumerate(bloco):
            if idx in ancoras:
                continue
            ant = next((bloco[j]["hora"] for j in range(idx - 1, -1, -1) if j in ancoras), None)
            prox = next((bloco[j]["hora"] for j in range(idx + 1, len(bloco)) if j in ancoras), None)
            opcoes = {v for v in it.get("leituras", []) if hora_valida(v)}
            if hora_valida(it.get("hora")):
                opcoes.add(it["hora"])
            for v in list(opcoes):
                opcoes.update(_variantes(v))
            cabem = [v for v in opcoes if (ant is None or v >= ant) and (prox is None or v <= prox)]
            original = it.get("hora")
            if cabem:
                ref = ant or prox
                it["hora"] = min(cabem, key=lambda v: (abs(_minutos(v) - _minutos(ref)) if ref else 0, v))
            elif ant or prox:
                it["hora"] = None
            if it["hora"] != original:
                it["reparada"] = True


def _letras(texto: str) -> int:
    return sum(ch.isalpha() for ch in texto or "")


def qualidade_ok(texto: str, vistos: int = 2) -> bool:
    sem_espaco = re.sub(r"\s", "", texto or "")
    letras = _letras(sem_espaco)
    razao = letras / max(1, len(sem_espaco))
    if vistos >= 2:
        return letras >= 2 and razao >= 0.75
    # Visto num quadro só (balão cortado na borda, esmaecendo): precisa parecer texto
    # de verdade — "a . .. ta as ava º a a a a" tem letras, mas não palavras.
    palavras = re.findall(r"[A-Za-zÀ-ú]+", texto or "")
    return (letras >= 4 and razao >= 0.6 and any(len(p) >= 4 for p in palavras)
            and sum(map(len, palavras)) / len(palavras) >= 3)


def _palavras(texto: str) -> set[str]:
    return set(re.findall(r"[a-zà-ú]{3,}", (texto or "").lower()))


def parecidas(a: str, b: str) -> bool:
    """Mesma mensagem lida duas vezes: palavras em comum OU caracteres quase iguais
    ("bom dia sunorte" × "bom dia, suporte" só divide 2 de 3 palavras)."""
    pa, pb = _palavras(a), _palavras(b)
    if len(pa) >= 2 and len(pb) >= 2 and len(pa & pb) / min(len(pa), len(pb)) >= 0.7:
        return True
    ca, cb = re.sub(r"[^a-z0-9à-ú]", "", (a or "").lower()), re.sub(r"[^a-z0-9à-ú]", "", (b or "").lower())
    return min(len(ca), len(cb)) >= 8 and SequenceMatcher(None, ca, cb).ratio() >= 0.85


def limpar_fim(texto: str) -> str:
    """Tira sobra de ícone lida como pontuação solta no fim da linha ("… opções. ,")."""
    return "\n".join(re.sub(r"(\s+[^\w\s]{1,2})+$", "", linha) for linha in (texto or "").split("\n")).strip()


def citacao_limpa(linhas, contato: str) -> str:
    """A 1ª linha da citação é o NOME de quem escreveu a original ("Dell Support - WA",
    "Você"): o remetente da resposta já está no cabeçalho dela."""
    if isinstance(linhas, str):
        linhas = [linhas]
    linhas = [l for l in (linhas or []) if l.strip()]
    if len(linhas) > 1:
        primeira = re.sub(r"[^a-z0-9]", "", linhas[0].lower())
        for nome in (contato, "Você", "Voce"):
            alvo = re.sub(r"[^a-z0-9]", "", (nome or "").lower())
            if alvo and SequenceMatcher(None, primeira, alvo).ratio() >= 0.75:
                linhas = linhas[1:]
                break
    return " ".join(linhas)


def montar(lidos: list[dict], u: float = 1.0) -> tuple[list[dict], list[str]]:
    """Leituras dos grupos → itens em ordem (mensagens, datas, avisos) + descartes."""
    itens: list[dict] = []
    descartes: list[str] = []
    for it in sorted(lidos, key=lambda i: i["g"]["gy0"]):
        g, texto = it["g"], (it.get("texto") or "").strip()
        n = len(g["membros"])
        base = {"hora": it.get("hora"), "votos": it.get("votos", 0), "leituras": list(it.get("leituras") or []),
                "cortada": bool(it.get("cortada")), "citacao": it.get("citacao"),
                "gy0": g["gy0"], "gy1": g["gy1"], "vistos": n, "k": g["rep"]["k"]}
        if it.get("botao"):
            itens.append({**base, "tipo": "botao", "texto": texto or "?"})
        elif g["lado"] == "centro":
            if eh_data(texto):
                itens.append({**base, "tipo": "data", "texto": re.sub(r"^[^\wÀ-ú]+|[^\wÀ-ú]+$", "", texto)})
            elif qualidade_ok(texto, n) and len(texto.split()) <= 60:
                # o ícone do aviso (cadeado, "i") sai como dígito/símbolo solto no começo
                itens.append({**base, "tipo": "aviso", "texto": re.sub(r"^[\d\W]{1,3}\s+(?=[A-ZÀ-Ú])", "", texto)})
            else:
                descartes.append(texto)
        elif g["rep"]["kind"].startswith("midia_"):
            itens.append({**base, "tipo": g["lado"], "texto": texto or "[imagem]", "midia": True})
        elif qualidade_ok(texto, n):
            itens.append({**base, "tipo": g["lado"], "texto": texto})
        else:
            descartes.append(texto)

    # aviso que só repete o texto de uma mensagem (balão tapado lido como centro) sai
    palavras_msgs = [_palavras(i["texto"]) for i in itens if i["tipo"] in ("entrada", "saida") and not i.get("midia")]

    def repete(texto: str) -> bool:
        w = _palavras(texto)
        return bool(w) and any(len(w & m) / len(w) >= 0.5 for m in palavras_msgs)

    itens = [i for i in itens if not (i["tipo"] == "aviso" and repete(i["texto"]))]

    # a mesma mensagem lida duas vezes (borda coberta num quadro): fica a mais completa
    limpos: list[dict] = []
    for it in itens:
        dup = None
        if it["tipo"] in ("entrada", "saida") and not it.get("midia"):
            for ant in reversed(limpos[-4:]):
                if ant["tipo"] != it["tipo"] or ant.get("midia") or it["gy0"] - ant["gy1"] > 80 * u:
                    continue
                if parecidas(it["texto"], ant["texto"]):
                    dup = ant
                    break
        if dup is None:
            limpos.append(it)
            continue
        if len(it["texto"]) > len(dup["texto"]):
            dup["texto"], dup["cortada"] = it["texto"], it["cortada"]
        dup["leituras"] = dup["leituras"] + it["leituras"]
        if it["votos"] > dup["votos"]:
            dup["hora"], dup["votos"] = it["hora"], it["votos"]
        dup["gy1"] = max(dup["gy1"], it["gy1"])

    juntos: list[dict] = []
    for it in limpos:
        if it["tipo"] == "botao":
            ant = next((j for j in reversed(juntos) if j["tipo"] == "entrada"), None)
            if ant is not None and it["gy0"] - ant["gy1"] <= 300 * u:
                rotulo = re.sub(r"^\W*[a-z]?\W+(?=\w{2})|\W+$", "", it["texto"])
                ant.setdefault("botoes", []).append(rotulo or it["texto"])
            continue
        juntos.append(it)

    # data sem mensagem até a próxima data (rótulo flutuante lido no 1º quadro) sai
    final: list[dict] = []
    for i, it in enumerate(juntos):
        if it["tipo"] == "data":
            prox = next((j for j in juntos[i + 1:] if j["tipo"] in ("data", "entrada", "saida")), None)
            if prox is None or prox["tipo"] == "data":
                continue
        final.append(it)
    reparar_horas(final)
    return final, descartes


def formatar(itens: list[dict], contato: str, segundos_k: list[float]) -> tuple[str, list[dict]]:
    """Itens → texto de mensagens.txt + registros de mensagens.jsonl."""
    linhas: list[str] = []
    registros: list[dict] = []
    for it in itens:
        t = mmss(segundos_k[it["k"]]) if 0 <= it.get("k", -1) < len(segundos_k) else None
        if it["tipo"] in ("data", "aviso"):
            linhas.append(f"\n== {it['texto']} ==" if it["tipo"] == "data" else f"[aviso] {it['texto']}")
            registros.append({"tipo": it["tipo"], "texto": it["texto"], "t_video": t})
            continue
        quem = "Você" if it["tipo"] == "saida" else contato
        cit = citacao_limpa(it["citacao"], contato) if it.get("citacao") else None
        cab = quem
        if cit:
            cab += f' (respondendo a "{cit[:80]}")'
        if it.get("cortada"):
            cab += ' [cortada em "Ler mais"]'
        corpo = it["texto"].replace("\n", "\n  ")
        botoes = f"\n  [botões: {' / '.join(it['botoes'])}]" if it.get("botoes") else ""
        linhas.append(f"[{it['hora'] or '??:??'}] {cab}: {corpo}{botoes}")
        registros.append({
            "tipo": "mensagem", "de": "voce" if it["tipo"] == "saida" else "contato",
            "hora": it["hora"], "hora_lida": bool(it["hora"]) and it.get("votos", 0) >= 2 and not it.get("reparada"),
            "texto": it["texto"], "citacao": cit, "botoes": it.get("botoes") or [],
            "cortada": bool(it.get("cortada")), "midia": bool(it.get("midia")), "t_video": t,
        })
    return "\n".join(linhas).strip() + "\n", registros


# ── Etapas com efeito (ffmpeg, tesseract, disco) ─────────────────────────────


class Leitor:
    """tesseract com cache em disco (chave = imagem + argumentos): retomar o job não
    relê balão nenhum. A imagem vai por stdin, sem arquivo temporário."""

    def __init__(self, store: Path, lang: str) -> None:
        self.lang = lang
        self.cache: dict[str, str] = {}
        if store.exists():
            for linha in store.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(linha)
                    self.cache[item["k"]] = item["v"]
                except (ValueError, KeyError, TypeError):
                    continue  # linha cortada por um job morto no meio da escrita
        self._trava = threading.Lock()
        self._fh = store.open("a", encoding="utf-8")

    def close(self) -> None:
        self._fh.close()

    def _rodar(self, img: Image.Image, args: list[str]) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        dados = buf.getvalue()
        chave = hashlib.sha1(dados + "\0".join(args).encode()).hexdigest()
        with self._trava:
            if chave in self.cache:
                return self.cache[chave]
        proc = subprocess.run(
            ["tesseract", "stdin", "stdout", *args], input=dados, capture_output=True, timeout=120,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
        if proc.returncode != 0:
            raise RuntimeError(f"tesseract falhou: {proc.stderr.decode('utf-8', 'replace').strip()[-300:]}")
        saida = proc.stdout.decode("utf-8", "replace")
        with self._trava:
            self.cache[chave] = saida
            self._fh.write(json.dumps({"k": chave, "v": saida}, ensure_ascii=False) + "\n")
            self._fh.flush()
        return saida

    def texto(self, img: Image.Image) -> list[str]:
        """Linhas (modo bloco), só as de confiança média >= 30."""
        tsv = self._rodar(img, ["-l", self.lang, "--psm", "6", "tsv"])
        linhas: dict[tuple, list] = {}
        ordem = []
        for i, row in enumerate(tsv.splitlines()):
            c = row.split("\t")
            if i == 0 or len(c) < 12 or c[0] != "5" or not c[11].strip():
                continue
            try:
                conf = float(c[10])
            except ValueError:
                continue
            chave = (c[2], c[3], c[4])
            if chave not in linhas:
                linhas[chave] = []
                ordem.append(chave)
            linhas[chave].append((c[11].strip(), conf))
        saida = []
        for chave in ordem:
            confs = [x for _, x in linhas[chave] if x >= 0]
            if confs and sum(confs) / len(confs) >= 30:
                saida.append(" ".join(p for p, _ in linhas[chave]))
        return saida

    def linha(self, img: Image.Image) -> str:
        return self._rodar(img, ["-l", self.lang, "--psm", "7"]).strip()

    def hora(self, img: Image.Image) -> str | None:
        txt = self._rodar(img, ["--psm", "7", "-c", "tessedit_char_whitelist=0123456789:"])
        achados = re.findall(r"(\d{1,2}):(\d{2})", txt)
        return f"{int(achados[-1][0]):02d}:{achados[-1][1]}" if achados else None


def margem(img: Image.Image) -> Image.Image:
    return ImageOps.expand(img, border=16, fill=255)


def preparar(img_rgb: Image.Image, escuro: bool, escala: int = 1) -> Image.Image:
    g = img_rgb.convert("L")
    if escuro:
        g = Image.eval(g, lambda v: 255 - v)
    if escala != 1:
        g = g.resize((g.width * escala, g.height * escala), Image.LANCZOS)
    return g


def otsu(img: Image.Image) -> Image.Image:
    a = np.asarray(img, dtype=np.uint8)
    hist = np.bincount(a.ravel(), minlength=256).astype(np.float64)
    total, soma = a.size, float(np.dot(np.arange(256), hist))
    wb = sb = 0.0
    melhor, limiar = -1.0, 128
    for t in range(256):
        wb += hist[t]
        if wb == 0 or wb == total:
            continue
        sb += t * hist[t]
        mb, mf = sb / wb, (soma - sb) / (total - wb)
        var = wb * (total - wb) * (mb - mf) ** 2
        if var > melhor:
            melhor, limiar = var, t
    return img.point(lambda v: 0 if v <= limiar else 255)


def _ffmpeg(entrada: Path, vf: str, saida: Path, duracao: float, rotulo: str, extra: tuple = ()) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(entrada), "-vf", vf,
           *extra, "-progress", "pipe:1", "-nostats", str(saida)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proximo = 0.25
    assert proc.stdout is not None
    for linha in proc.stdout:
        # `out_time_ms` é em MICROssegundos no ffmpeg (bug histórico do nome).
        if duracao <= 0 or not linha.startswith(("out_time_us=", "out_time_ms=")):
            continue
        try:
            feito = int(linha.split("=", 1)[1]) / 1e6 / duracao
        except ValueError:
            continue
        if feito >= proximo:
            log(f"[{rotulo}] {min(100, int(feito * 100))}%")
            while proximo <= feito:
                proximo += 0.25
    erro = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg falhou ({rotulo}): {erro.strip()[-500:]}")


def _cinza(path: Path):
    with Image.open(path) as img:
        return np.asarray(img.convert("L"), dtype=np.float32)


def reconhecer(video: Path, trab: Path, meta: dict, crop) -> dict | None:
    """Painel da conversa, rolagem entre quadros e quadros-chave, em quadros pequenos.

    Lê um quadro por vez (duas passadas): empilhar 2 min a 4/s já passaria de 1 GB.
    """
    rdir = trab / "r"
    shutil.rmtree(rdir, ignore_errors=True)
    rdir.mkdir(parents=True)
    filtros = [f"crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}"] if crop else []
    filtros += [f"fps={RECON_FPS}", f"scale={RECON_LARGURA}:-2", "format=gray"]
    _ffmpeg(video, ",".join(filtros), rdir / "r_%05d.png", float(meta.get("duration") or 0), "reconhecer")
    paths = sorted(rdir.glob("r_*.png"))
    if not paths:
        return None
    src_w = crop[2] if crop else int(meta["width"])
    src_h = crop[3] if crop else int(meta["height"])

    act = ant = None
    for p in paths:
        f = _cinza(p)
        if ant is not None:
            d = np.abs(f - ant)
            act = d if act is None else act + d
        ant = f
    hh, ww = ant.shape
    sx, sy = src_w / ww, src_h / hh
    x0, y0, x1, y1 = painel(act) if act is not None else (0, 0, ww, hh)
    h = y1 - y0

    dys: list[int] = []
    keys, acc = [0], 0
    ref = perfil_ant = f = None
    bg = None
    for i, p in enumerate(paths):
        f = _cinza(p)[y0:y1, x0:x1]
        if bg is None:
            bg = float(np.median(f))
        perfil = row_profiles(f, bg)
        if perfil_ant is None:
            ref = f
        else:
            d = best_shift(perfil_ant, perfil, -int(h * 0.65), int(h * 0.65))[0]
            dys.append(d)
            acc += d
            parado = i - keys[-1] >= RECON_FPS * CHAVE_SEGUNDOS
            if abs(acc) >= CHAVE_ROLAGEM * h or (parado and float(np.abs(f - ref).mean()) > CHAVE_MUDANCA):
                keys.append(i)
                acc = 0
                ref = f
        perfil_ant = perfil
    if len(paths) > 1 and keys[-1] != len(paths) - 1 and (acc or float(np.abs(f - ref).mean()) > CHAVE_MUDANCA):
        keys.append(len(paths) - 1)
    # A borda do painel achada pela atividade corta o balão cinza (quase da cor do
    # fundo, muda pouco): 10 px de quadro pequeno de folga à esquerda, 2 à direita.
    box = [max(0, int(round((x0 - 10) * sx))), int(round(y0 * sy)),
           min(src_w, int(round((x1 + 2) * sx))), min(src_h, int(round(y1 * sy)))]
    shutil.rmtree(rdir, ignore_errors=True)
    log(f"[reconhecer] {len(paths)} quadros · painel {box} · rolagem total {int(sum(abs(d) for d in dys) * sy)}px · "
        f"{len(keys)} quadros-chave")
    return {"box": box, "keys": keys, "dys": dys, "escala": [sx, sy], "quadros": len(paths)}


def extrair_chaves(video: Path, trab: Path, recon: dict, crop, duracao: float) -> list[Path]:
    kdir = trab / "k"
    keys = recon["keys"]
    existentes = sorted(kdir.glob("k_*.png")) if kdir.exists() else []
    if len(existentes) == len(keys):
        return existentes
    shutil.rmtree(kdir, ignore_errors=True)
    kdir.mkdir(parents=True)
    bx0, by0, bx1, by1 = recon["box"]
    ox, oy = (crop[0], crop[1]) if crop else (0, 0)
    sel = "+".join(f"eq(n,{i})" for i in keys)
    vf = f"fps={RECON_FPS},select='{sel}',crop={bx1 - bx0}:{by1 - by0}:{ox + bx0}:{oy + by0}"
    _ffmpeg(video, vf, kdir / "k_%05d.png", duracao, "quadros-chave", extra=("-fps_mode", "vfr"))
    paths = sorted(kdir.glob("k_*.png"))
    if len(paths) != len(keys):
        raise RuntimeError(f"esperava {len(keys)} quadros-chave, o ffmpeg gerou {len(paths)}")
    log(f"[quadros-chave] {len(paths)} em resolução nativa {bx1 - bx0}x{by1 - by0}")
    return paths


def posicoes_globais(kpaths: list[Path], keys: list[int], dys: list[int], escala_y: float) -> list[int]:
    """Topo de cada quadro-chave na conversa inteira: a estimativa do reconhecimento,
    refinada correlacionando os quadros-chave vizinhos em meia resolução."""
    G = [0]
    anterior = None
    for i, p in enumerate(kpaths):
        with Image.open(p) as img:
            meia = np.asarray(img.convert("L").resize((img.width // 2, img.height // 2)), dtype=np.float32)
        perfil = row_profiles(meia, float(np.median(meia)))
        if anterior is not None:
            est = int(round(sum(dys[keys[i - 1]:keys[i]]) * escala_y / 2))
            d, _ = best_shift(anterior, perfil, est - 40, est + 40)
            G.append(G[-1] + d * 2)
        anterior = perfil
    return G


def ler_contato(video: Path, trab: Path, recon: dict, crop, leitor: Leitor, escuro: bool, u: float) -> str:
    """Nome do contato: a faixa do cabeçalho logo acima do painel, depois do avatar."""
    bx0, by0, bx1, _ = recon["box"]
    ox, oy = (crop[0], crop[1]) if crop else (0, 0)
    alto = int(min(90 * u, by0))
    largura = int(min(900 * u, bx1 - bx0 - 80 * u))
    if alto < 20 or largura < 100:
        return "Contato"
    destino = trab / "cabecalho.png"
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", "0.5", "-i", str(video),
         "-frames:v", "1", "-vf", f"crop={largura}:{alto}:{ox + bx0 + int(80 * u)}:{oy + by0 - alto}", str(destino)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not destino.exists():
        return "Contato"
    with Image.open(destino) as img:
        lido = leitor.linha(preparar(img.convert("RGB"), escuro, 2))
    destino.unlink(missing_ok=True)
    lido = re.sub(r"^[^\wÀ-ú+]+|[^\w)]+$", "", lido).strip()
    return lido if len(lido) >= 3 and qualidade_ok(lido) else "Contato"


def ler_grupo(g: dict, img_k, leitor: Leitor, escuro: bool, cores: dict, W: int) -> dict:
    """OCR de um balão no melhor exemplar; horário por votação entre os exemplares."""
    u = cores["u"]
    r = g["rep"]
    xa, ya, xb, yb = r["box"]
    img = img_k(r["k"])
    lido: dict = {"g": g, "texto": "", "hora": None, "votos": 0, "leituras": [], "cortada": False, "citacao": None}

    if g["lado"] == "centro":
        cinza = preparar(img.crop((xa, ya, xb, yb)), escuro, 2)
        if yb - ya > 70 * u:  # aviso de várias linhas, cartão
            texto = " ".join(leitor.texto(margem(cinza)))
        else:
            texto = leitor.linha(margem(cinza))
            if not eh_data(texto):
                alt = leitor.linha(margem(otsu(cinza)))
                if eh_data(alt) or len(alt) > len(texto):
                    texto = alt
        lido["texto"] = texto.strip()
        return lido

    if g.get("botao"):
        # o ícone fica à esquerda e o rótulo, centralizado: corta as laterais
        inset = int(60 * u) if xb - xa > 200 * u else int(10 * u)
        recorte = img.crop((xa + inset, ya + int(8 * u), xb - inset, yb - int(8 * u)))
        lido["texto"] = re.sub(r"^\W+|\W+$", "", leitor.linha(margem(otsu(preparar(recorte, escuro, 2)))))
        lido["botao"] = True
        return lido

    midia = r["kind"].startswith("midia_")
    linhas: list[str] = []
    if midia:
        canto = img.crop((xa + int(6 * u), max(ya, yb - int(56 * u)), min(xb, xa + int(220 * u)), yb - int(4 * u)))
        dur = leitor.hora(margem(otsu(preparar(canto, escuro, 3))))
        lido["texto"] = f"[vídeo {int(dur[:2])}:{dur[3:]}]" if dur else "[imagem]"
    else:
        topo = ya
        if r.get("citacao"):
            q0, q1 = r["citacao"]
            i = int(14 * u)
            lido["citacao"] = leitor.texto(margem(preparar(img.crop((xa + i, ya + q0, xb - i, ya + q1)), escuro))) or None
            topo = ya + q1
        corpo = img.crop((xa, topo, xb, yb))
        linhas = leitor.texto(margem(preparar(corpo, escuro)))
        if _letras("".join(linhas)) < 4:
            # corpo curto ("Não", "Ok"): o modo bloco devolve lixo; tenta uma linha só
            alt = leitor.linha(margem(otsu(preparar(corpo, escuro, 2))))
            if _letras(alt) > _letras("".join(linhas)):
                linhas = [alt]
    hora_texto = hora_do_texto(linhas[-1]) if linhas else None
    if linhas:
        linhas[-1] = HORA_FIM.sub("", linhas[-1]).rstrip()

    leituras: list[str] = []
    fill = cores.get(r["cor"]) if r["cor"] in ("entrada", "saida") else None
    for m in sorted(g["membros"], key=_nota, reverse=True)[:4]:
        mxa, mya, mxb, myb = m["box"]
        imgm = img_k(m["k"])
        # Saída termina nos tiques de leitura, à direita do horário. Quando algo fixo
        # tapa a borda (o botão "rolar para baixo"), os tiques já não aparecem.
        coberto = g["lado"] == "saida" and mxb < W - 16 * u
        dx = int(4 * u) if coberto else int(46 * u) if g["lado"] == "saida" else int(14 * u)
        cx0, cy0 = mxa + int(10 * u), max(mya, myb - int(64 * u))
        cx1, cy1 = max(mxa + int(11 * u), mxb - dx), myb - int(6 * u)
        achado = None
        if fill is not None and not midia and cy1 - cy0 > 12 * u:
            achado = local_hora(np.asarray(imgm.crop((cx0, cy0, cx1, cy1)), dtype=np.int32), fill, u)
        if achado:
            a, r0, b, r1 = achado
            p = int(6 * u)
            canto = imgm.crop((cx0 + a - p, cy0 + r0 - p, cx0 + b + p, cy0 + r1 + p))
        else:
            canto = imgm.crop((max(mxa, mxb - int(240 * u)), max(mya, myb - int(50 * u)),
                               max(mxa + 1, mxb - dx), max(mya + 1, myb - int(4 * u))))
        cinza = preparar(canto, escuro, 3)
        for binaria in (cinza.point(lambda v: 0 if v < 170 else 255), otsu(cinza)):
            h = leitor.hora(margem(binaria))
            if hora_valida(h):
                leituras.append(h)
    if hora_valida(hora_texto):
        leituras.append(hora_texto)
    hora, votos = Counter(leituras).most_common(1)[0] if leituras else (None, 0)

    if not midia:
        texto = "\n".join(l for l in linhas if l.strip()).strip()
        lido["cortada"] = bool(re.search(r"\bler mais\s*$", texto, re.I))
        texto = re.sub(r"\n?\s*ler mais\s*$", "", texto, flags=re.I).strip()
        lido["texto"] = limpar_fim(texto)
    lido.update(hora=hora, votos=votos, leituras=leituras)
    return lido


def executar(video: Path, out: Path, meta: dict, *, lang: str = "por", workers: int = 2, crop=None,
             keep_frames: bool = False, forcar: bool = False) -> dict | None:
    """Pipeline do modo chat. Devolve o resumo, ou None quando o vídeo não é conversa
    em balões (o chamador cai no OCR de texto corrido)."""
    inicio = time.monotonic()
    trab = out / "chat"
    trab.mkdir(parents=True, exist_ok=True)
    duracao = float(meta.get("duration") or 0)

    recon_path = trab / "recon.json"
    if recon_path.exists():
        recon = json.loads(recon_path.read_text(encoding="utf-8"))
        log(f"[chat] retomando: reconhecimento pronto ({len(recon['keys'])} quadros-chave)")
    else:
        recon = reconhecer(video, trab, meta, crop)
        if recon is None:
            log("[chat] o ffmpeg não gerou quadro nenhum")
            return None
        recon_path.write_text(json.dumps(recon), encoding="utf-8")

    seg_path = trab / "baloes.json"
    if seg_path.exists():
        seg = json.loads(seg_path.read_text(encoding="utf-8"))
        log("[chat] retomando: balões já localizados")
    else:
        kpaths = extrair_chaves(video, trab, recon, crop, duracao)
        with Image.open(kpaths[len(kpaths) // 2]) as img:
            rgb = np.asarray(img.convert("RGB")).astype(np.int32)
        H, W = rgb.shape[:2]
        cores = analisar_cores(rgb)
        quadros: list[list[dict]] = []
        if cores is not None:
            log(f"[cores] fundo {cores['fundo']} · entrada {cores['entrada']} · saída {cores['saida']} · escala {cores['u']:g}")
            for kp in kpaths:
                with Image.open(kp) as img:
                    rgb = np.asarray(img.convert("RGB")).astype(np.int32)
                bolhas = segmentar_quadro(rgb, cores)
                quadros.append(bolhas)
                log(f"[balões] {kp.name}: {dict(Counter(b['kind'] for b in bolhas))}")
        seg = {"cores": cores, "quadros": quadros, "W": W, "H": H}
        seg_path.write_text(json.dumps(seg), encoding="utf-8")

    cores = seg["cores"]
    tipos = Counter(b["kind"] for q in seg["quadros"] for b in q)
    esquerda = tipos["entrada"] + tipos["midia_entrada"]
    direita = tipos["saida"] + tipos["midia_saida"]
    conversa = cores is not None and esquerda + direita >= MIN_BALOES and esquerda > 0 and direita > 0
    if not conversa and not (forcar and cores is not None and esquerda + direita > 0):
        log(f"[chat] não parece conversa em balões ({esquerda} à esquerda, {direita} à direita)")
        if not keep_frames:
            shutil.rmtree(trab / "k", ignore_errors=True)
        return None

    u, W = cores["u"], seg["W"]
    escuro = cores["bgl"] < 100
    kpaths = extrair_chaves(video, trab, recon, crop, duracao)
    G = posicoes_globais(kpaths, recon["keys"], recon["dys"], recon["escala"][1])
    log(f"[juntar] posição de cada quadro-chave na conversa: {G}")

    cand: list[dict] = []
    tapados = 0
    for k, bolhas in enumerate(seg["quadros"]):
        tampa = cobertura(bolhas, u)
        for b in bolhas:
            if b["kind"] in ("cabecalho", "outro"):
                continue
            if tampa and cruza(b["box"], tampa):
                tapados += 1
                continue
            cand.append({**b, "k": k, "lado": b["kind"].replace("midia_", ""),
                         "gy0": G[k] + b["box"][1], "gy1": G[k] + b["box"][3]})
    grupos = agrupar(cand, u)
    marcar_botoes(grupos, u)
    log(f"[juntar] {len(cand)} balões vistos ({tapados} debaixo de sobreposição) → {len(grupos)} balões únicos")

    imagens: OrderedDict[int, Image.Image] = OrderedDict()
    trava = threading.Lock()

    def img_k(k: int) -> Image.Image:
        with trava:
            if k in imagens:
                imagens.move_to_end(k)
                return imagens[k]
        with Image.open(kpaths[k]) as aberta:
            img = aberta.convert("RGB")
        with trava:
            imagens[k] = img
            while len(imagens) > IMAGENS_EM_MEMORIA:
                imagens.popitem(last=False)
        return img

    leitor = Leitor(trab / "ocr_cache.jsonl", lang)
    try:
        lidos: list[dict] = [None] * len(grupos)  # type: ignore[list-item]
        t_ocr = time.monotonic()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futuros = {pool.submit(ler_grupo, g, img_k, leitor, escuro, cores, W): i for i, g in enumerate(grupos)}
            passo = max(1, len(grupos) // 10)
            for n, futuro in enumerate(as_completed(futuros), 1):
                lidos[futuros[futuro]] = futuro.result()
                if n % passo == 0 or n == len(grupos):
                    log(f"[ler] {n}/{len(grupos)} balões · {time.monotonic() - t_ocr:.0f}s")
        contato = ler_contato(video, trab, recon, crop, leitor, escuro, u)
    finally:
        leitor.close()

    itens, descartes = montar(lidos, u)
    texto, registros = formatar(itens, contato, [k / RECON_FPS for k in recon["keys"]])
    cabecalho = (
        f"# Conversa com {contato} — OCR por balão do vídeo {video.name}\n"
        "# [HORA] REMETENTE: mensagem · \"== dia ==\" separa os dias · [??:??] = horário ilegível · "
        "[aviso] = aviso do sistema\n\n"
    )
    arquivos = {"mensagens": str(out / "mensagens.txt"), "mensagens_jsonl": str(out / "mensagens.jsonl"),
                "descartadas": str(out / "descartadas.txt")}
    (out / "mensagens.txt").write_text(cabecalho + texto, encoding="utf-8")
    (out / "mensagens.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in registros), encoding="utf-8"
    )
    (out / "descartadas.txt").write_text("".join(f"{d}\n" for d in descartes if d.strip()), encoding="utf-8")
    if not keep_frames:
        shutil.rmtree(trab / "k", ignore_errors=True)

    msgs = [r for r in registros if r["tipo"] == "mensagem"]
    resumo = {
        "contato": contato,
        "mensagens": len(msgs),
        "datas": sum(1 for r in registros if r["tipo"] == "data"),
        "avisos": sum(1 for r in registros if r["tipo"] == "aviso"),
        "sem_hora": sum(1 for r in msgs if not r["hora"]),
        "horas_deduzidas": sum(1 for r in msgs if r["hora"] and not r["hora_lida"]),
        "quadros_chave": len(recon["keys"]),
        "baloes_vistos": len(cand),
        "baloes_unicos": len(grupos),
        "tapados_por_sobreposicao": tapados,
        "descartados": len(descartes),
        "arquivos": arquivos,
        "segundos_chat": int(time.monotonic() - inicio),
    }
    log(f"[chat] {resumo['mensagens']} mensagens · {resumo['datas']} datas · {resumo['avisos']} avisos · "
        f"{resumo['sem_hora']} sem horário · contato {contato!r}")
    return resumo
