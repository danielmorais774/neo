#!/usr/bin/env python3
"""OCR de vídeo de tela — quadros → OCR → transcrição sem linhas repetidas.

Feito para gravação de chat rolando (serve também para slides ou documento na
tela). Roda no sandbox do Neo: ffmpeg, tesseract (por/eng), Pillow e numpy já
estão na imagem. É RETOMÁVEL: rodar de novo com o mesmo --out pula o que já foi
feito.

Uso:
  ocr_video.py VIDEO --out DIR [--modo auto|chat|texto] [--fps 1] [--width 1600]
               [--crop X,Y,W,H] [--lang por] [--psm 4] [--workers 2] [--keep-frames]

Dois modos (--modo auto escolhe sozinho):
- chat (chat_ocr.py): conversa em BALÕES (WhatsApp, Telegram…). Cada balão é
  localizado, lido uma vez e sai com remetente (lado/cor), horário do canto, data,
  citação, botões e anexos. Em DIR:
    mensagens.txt     "[HORA] REMETENTE: mensagem", "== dia ==" entre os dias
    mensagens.jsonl   um objeto por item: tipo, de, hora, hora_lida, texto, citacao…
    descartadas.txt   pedaços que o OCR não leu direito (confira se falta algo)
- texto: OCR da tela inteira + junção das linhas (slides, documento, chat sem
  balões). É o caminho abaixo; em DIR:
  transcricao.txt   linhas únicas, na ordem em que apareceram, com [mm:ss] do vídeo
  linhas.jsonl      um objeto por linha: {"t", "texto"}
  interface.txt     linhas FIXAS da tela (cabeçalho, rodapé, barra lateral) tiradas
                    da transcrição — confira se nada de conteúdo foi parar ali
  descartadas.txt   linhas vistas num quadro só (pedaço cortado na borda da rolagem)
  resultado.json    o resumo; a última linha do stdout é "RESULT {o mesmo json}"

Por que cada etapa existe (sessão 45bd5ad0, vídeo 3420×2224 a 60 fps):
- reduzir para ~1600 px em cinza: o tesseract caiu de >20 s para ~1 s por quadro;
- descartar quadro repetido: vídeo parado não paga OCR de novo;
- tirar a interface fixa ANTES de juntar: o rodapé "Digite uma mensagem" em todo
  quadro viraria âncora falsa e engoliria as mensagens novas;
- gravar o OCR quadro a quadro: um job interrompido não perde o que já rodou.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

#: Linha com menos que isto (em caracteres de chave) não ancora a junção:
#: "ok" e "kkk" se repetem de verdade numa conversa.
ANCHOR_MIN_KEY = 8
SIMILAR_RATIO = 0.86
#: Fração dos quadros distintos em que uma linha precisa aparecer para ser
#: considerada interface fixa.
CHROME_SHARE = 0.6
CHROME_MIN_FRAMES = 4
MIN_LINE_CONF = 45.0
#: Diferença média (0–255) entre miniaturas abaixo da qual o quadro é "o mesmo".
#: Uma mensagem nova de uma linha já muda mais que isso; ruído de compressão, não.
SAME_FRAME_DIFF = 1.0
MERGE_WINDOW = 150
#: Suporte mediano (em quantos quadros a linha típica aparece) abaixo do qual a
#: rolagem é rápida demais para o filtro de linha cortada — e para o próprio OCR a
#: este fps, que pode ter pulado mensagens entre um quadro e outro.
ROLAGEM_LENTA_MIN_SUPORTE = 3


def log(msg: str) -> None:
    print(msg, flush=True)


# ── Lógica pura (coberta por tests/skills/test_ocr_video.py) ─────────────────


def parse_crop(raw: str | None) -> tuple[int, int, int, int] | None:
    if not raw:
        return None
    partes = [p.strip() for p in raw.split(",")]
    if len(partes) != 4 or not all(p.isdigit() for p in partes):
        raise ValueError("--crop precisa ser X,Y,W,H em pixels inteiros do vídeo original")
    x, y, w, h = (int(p) for p in partes)
    if w <= 0 or h <= 0:
        raise ValueError("--crop com largura ou altura zero")
    return x, y, w, h


def build_filter(fps: float, width: int, crop: tuple[int, int, int, int] | None) -> str:
    etapas = []
    if crop:
        x, y, w, h = crop
        etapas.append(f"crop={w}:{h}:{x}:{y}")
    etapas.append(f"fps={fps:g}")
    # Nunca amplia: vídeo pequeno esticado só fica borrado para o OCR.
    etapas.append(f"scale='min(iw,{width})':-2")
    etapas.append("format=gray")
    return ",".join(etapas)


def norm_key(texto: str) -> str:
    """Chave de comparação: sem acento, sem caixa, só letras e dígitos."""
    decomposto = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", sem_acento.casefold())


def similar(a: str, b: str) -> bool:
    """Mesma linha lida por OCR em dois quadros (tolera ruído de leitura).

    Os DÍGITOS precisam bater exatamente: "Maria 14:07" e "Maria 14:08" diferem em
    um caractere de quarenta e seriam "a mesma linha" pela razão de semelhança —
    e horário, valor e número são justamente o que distingue uma mensagem da outra.
    """
    if a == b:
        return True
    if not a or not b:
        return False
    if re.sub(r"\D", "", a) != re.sub(r"\D", "", b):
        return False
    maior = max(len(a), len(b))
    if maior < 4 or abs(len(a) - len(b)) > 0.3 * maior:
        return False
    return SequenceMatcher(None, a, b).ratio() >= SIMILAR_RATIO


def same_frame(a, b, limiar: float = SAME_FRAME_DIFF) -> bool:
    """Duas miniaturas (arrays numpy int16) mostram a mesma tela?

    Diferença de pixels e não hash perceptual: um hash de 64 bits é cego a texto
    que rolou — a miniatura de uma tela de chat é quase igual antes e depois da
    rolagem, e o quadro com mensagens novas seria jogado fora.
    """
    if a.shape != b.shape:
        return False
    return float(abs(a - b).mean()) < limiar


def distinct_frames(thumbs: list, limiar: float = SAME_FRAME_DIFF) -> list[int]:
    """Índices dos quadros que mudaram em relação ao último MANTIDO.

    Comparar com o último mantido (e não com o anterior) faz a rolagem lenta
    acumular diferença até contar como mudança, em vez de sumir aos pouquinhos.
    """
    mantidos: list[int] = []
    ultimo = None
    for i, miniatura in enumerate(thumbs):
        if ultimo is None or not same_frame(ultimo, miniatura, limiar):
            mantidos.append(i)
            ultimo = miniatura
    return mantidos


def tsv_to_lines(tsv: str, min_conf: float = MIN_LINE_CONF) -> list[str]:
    """Linhas de texto a partir da saída TSV do tesseract, na ordem de leitura.

    Descarta a linha cuja confiança média fica abaixo de ``min_conf``: é o
    ruído de ícone, avatar e borda de balão que o OCR "lê" como letras.
    """
    palavras: dict[tuple[int, int, int], list[tuple[str, float]]] = {}
    ordem: list[tuple[int, int, int]] = []
    for i, row in enumerate(tsv.splitlines()):
        cols = row.split("\t")
        if i == 0 or len(cols) < 12 or cols[0] != "5":
            continue
        texto = cols[11].strip()
        if not texto:
            continue
        try:
            conf = float(cols[10])
            chave = (int(cols[2]), int(cols[3]), int(cols[4]))
        except ValueError:
            continue
        if chave not in palavras:
            palavras[chave] = []
            ordem.append(chave)
        palavras[chave].append((texto, conf))
    linhas: list[str] = []
    for chave in ordem:
        itens = palavras[chave]
        confs = [c for _, c in itens if c >= 0]
        media = sum(confs) / len(confs) if confs else 0.0
        texto = " ".join(p for p, _ in itens).strip()
        if media >= min_conf and norm_key(texto):
            linhas.append(texto)
    return linhas


def find_chrome(
    frames_lines: list[list[str]],
    share: float = CHROME_SHARE,
    min_frames: int = CHROME_MIN_FRAMES,
) -> dict[str, str]:
    """Linhas que aparecem em quase todo quadro distinto: interface, não conversa.

    Devolve ``chave -> texto de exemplo``. Com poucos quadros não decide nada —
    três telas não bastam para separar rodapé de mensagem.
    """
    total = len(frames_lines)
    if total < min_frames:
        return {}
    contagem: dict[str, int] = {}
    exemplo: dict[str, str] = {}
    for linhas in frames_lines:
        vistas = set()
        for linha in linhas:
            chave = norm_key(linha)
            if chave and chave not in vistas:
                vistas.add(chave)
                contagem[chave] = contagem.get(chave, 0) + 1
                exemplo.setdefault(chave, linha.strip())
    return {k: exemplo[k] for k, n in contagem.items() if n / total >= share}


def _canonical(itens: list[tuple[str, str]], tail_keys: list[str]) -> list[str]:
    """Troca a chave de cada linha do quadro pela chave equivalente já vista.

    O SequenceMatcher só alinha tokens IGUAIS; o OCR lê "Olá" num quadro e "Ola"
    no seguinte. Linha sem par vira um token que nunca casa.
    """
    canon: list[str] = []
    for _, chave in itens:
        par = next((t for t in reversed(tail_keys) if similar(chave, t)), None)
        canon.append(par if par is not None else "\0" + chave)
    return canon


def _best_block(tail_keys: list[str], canon: list[str]) -> tuple[int, int, int] | None:
    """O trecho em comum mais PESADO entre o fim da transcrição e o quadro.

    Pesado (soma do tamanho das linhas), não o último: o rodapé repetido casa no
    fim de todo quadro, e ancorar nele descartaria tudo o que veio antes dele.
    """
    if not tail_keys:
        return None
    melhor: tuple[int, int, int] | None = None
    peso_melhor = 0
    matcher = SequenceMatcher(None, tail_keys, canon, autojunk=False)
    for bloco in matcher.get_matching_blocks():
        if bloco.size == 0:
            continue
        peso = sum(len(tail_keys[bloco.a + i]) for i in range(bloco.size))
        if peso >= ANCHOR_MIN_KEY and peso >= peso_melhor:
            melhor, peso_melhor = (bloco.a, bloco.b, bloco.size), peso
    return melhor


def _fresh(itens: list[tuple[str, str]], tail_keys: list[str]) -> list[tuple[str, str]]:
    """Tira o que já está em ``tail_keys`` — só linha longa; curta se repete de verdade.

    Quem chama passa só o LADO da transcrição onde as linhas vão entrar, não a
    janela inteira: mensagem longa que se repete de verdade ("Esta mensagem foi
    apagada", texto encaminhado duas vezes) sumiria se fosse comparada com tudo.
    """
    return [
        (texto, chave)
        for texto, chave in itens
        if len(chave) < ANCHOR_MIN_KEY or not any(similar(chave, t) for t in tail_keys)
    ]


def merge_frames(
    frames: list[tuple[float, list[str]]],
    chrome: set[str] | dict[str, str] | frozenset = frozenset(),
    window: int = MERGE_WINDOW,
) -> list[dict]:
    """Junta as linhas de quadros sucessivos numa transcrição sem repetição.

    Cada quadro é alinhado ao fim da transcrição: o que vem DEPOIS do trecho em
    comum é novo (rolou para baixo) e entra logo após ele; duas ou mais linhas
    desconhecidas ANTES do trecho são conteúdo revelado rolando para cima e entram
    antes dele. Uma linha solta antes do trecho é quase sempre a linha cortada
    pela borda da tela, e é descartada.
    """
    saida: list[dict] = []
    for t, linhas in frames:
        itens = [(linha.strip(), norm_key(linha)) for linha in linhas]
        itens = [(texto, chave) for texto, chave in itens if chave and chave not in chrome]
        if not itens:
            continue
        inicio = max(0, len(saida) - window)
        tail_keys = [s["key"] for s in saida[inicio:]]
        bloco = _best_block(tail_keys, _canonical(itens, tail_keys))
        if bloco is None:
            saida.extend({"t": t, "texto": texto, "key": chave} for texto, chave in _fresh(itens, tail_keys))
            continue
        a, b, tamanho = bloco
        # Abaixo do trecho em comum só é "repetido" o que a transcrição JÁ tem
        # abaixo dele (o rodapé velho, um quadro adiantado); acima, idem para cima.
        depois = _fresh(itens[b + tamanho:], tail_keys[a + tamanho:])
        antes = _fresh(itens[:b], tail_keys[:a])
        pos = inicio + a + tamanho
        saida[pos:pos] = [{"t": t, "texto": texto, "key": chave} for texto, chave in depois]
        if len(antes) >= 2:
            pos = inicio + a
            saida[pos:pos] = [{"t": t, "texto": texto, "key": chave} for texto, chave in antes]
    return [{"t": s["t"], "texto": s["texto"]} for s in saida]


def drop_single_frame_lines(
    transcricao: list[dict],
    frames: list[tuple[float, list[str]]],
    chrome: set[str] | dict[str, str] | frozenset = frozenset(),
    min_support: int = 2,
) -> tuple[list[dict], list[dict], float]:
    """Tira a linha vista em UM quadro só — quase sempre um pedaço cortado na borda.

    Numa conversa rolando devagar, uma linha de verdade fica visível por vários
    quadros seguidos. O pedaço cortado pela borda da área de rolagem — que o OCR
    lê como "Nanial Morais 14:na" ou "tam" — aparece num quadro só, porque no
    seguinte a linha inteira já está lá. Medido no E2E sintético: 34 de 162 linhas
    eram isso. O primeiro e o último quadro ficam fora da regra: o que só aparece
    neles não teve chance de aparecer duas vezes.

    **Adaptativo, porque o mesmo sinal engana na rolagem rápida:** se a linha
    TÍPICA aparece em menos de ``ROLAGEM_LENTA_MIN_SUPORTE`` quadros, linha real
    também aparece num quadro só, e o filtro jogaria conversa fora. Aí ele não
    descarta nada e devolve o suporte mediano para o chamador avisar que é hora de
    rodar com mais quadros por segundo.

    Devolve ``(mantidas, descartadas, suporte_mediano)``.
    """
    if min_support <= 1 or len(frames) < 3 or not transcricao:
        return transcricao, [], 0.0
    fixas = set(chrome)
    chaves_por_quadro = [{norm_key(l) for l in linhas} - fixas for _, linhas in frames]
    suportes = []
    for item in transcricao:
        chave = norm_key(item["texto"])
        suportes.append(sum(
            1 for chaves in chaves_por_quadro
            if chave in chaves or any(similar(chave, k) for k in chaves)
        ))
    mediana = float(statistics.median(suportes))
    if mediana < ROLAGEM_LENTA_MIN_SUPORTE:
        return transcricao, [], mediana
    bordas = (frames[0][0], frames[-1][0])
    mantidas: list[dict] = []
    descartadas: list[dict] = []
    for item, suporte in zip(transcricao, suportes):
        if suporte < min_support and item["t"] not in bordas:
            descartadas.append(item)
        else:
            mantidas.append(item)
    return mantidas, descartadas, mediana


def mmss(segundos: float) -> str:
    total = int(round(segundos))
    return f"{total // 60:02d}:{total % 60:02d}"


# ── Etapas com efeito (ffmpeg, tesseract, disco) ─────────────────────────────


def probe(video: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
            "-of", "json", str(video),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe falhou: {proc.stderr.strip()[-300:]}")
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    num, _, den = str(stream.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(fps, 2),
        "duration": float((data.get("format") or {}).get("duration") or 0.0),
    }


def extract(video: Path, frames_dir: Path, fps: float, width: int, crop, duration: float) -> int:
    frames_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(video), "-vf", build_filter(fps, width, crop),
        "-progress", "pipe:1", "-nostats",
        str(frames_dir / "f_%05d.png"),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proximo = 0.1
    assert proc.stdout is not None
    for linha in proc.stdout:
        # `out_time_ms` é em MICROssegundos no ffmpeg (bug histórico do nome).
        if not linha.startswith(("out_time_us=", "out_time_ms=")) or duration <= 0:
            continue
        try:
            feito = int(linha.split("=", 1)[1]) / 1e6 / duration
        except ValueError:
            continue
        if feito >= proximo:
            log(f"[extrair] {min(100, int(feito * 100))}%")
            while proximo <= feito:
                proximo += 0.1
    erro = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg falhou: {erro.strip()[-500:]}")
    return len(list(frames_dir.glob("f_*.png")))


def thumb(path: Path, largura: int = 160):
    import numpy as np
    from PIL import Image

    with Image.open(path) as img:
        w, h = img.size
        alto = max(1, round(h * largura / max(1, w)))
        return np.asarray(img.convert("L").resize((largura, alto)), dtype=np.int16)


def ocr_one(path: Path, lang: str, psm: int, min_conf: float) -> list[str]:
    proc = subprocess.run(
        ["tesseract", str(path), "stdout", "-l", lang, "--psm", str(psm), "tsv"],
        capture_output=True, text=True, timeout=300,
        # Um processo por núcleo: o paralelismo é o pool de threads abaixo, e o
        # OpenMP do tesseract brigando por 1,5 CPU só deixa tudo mais lento.
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract falhou em {path.name}: {proc.stderr.strip()[-300:]}")
    return tsv_to_lines(proc.stdout, min_conf)


def load_ocr(store: Path) -> dict[str, list[str]]:
    feitos: dict[str, list[str]] = {}
    if not store.exists():
        return feitos
    for linha in store.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(linha)
        except ValueError:
            continue  # linha cortada por um job morto no meio da escrita
        feitos[str(item.get("frame"))] = list(item.get("linhas") or [])
    return feitos


def ocr_frames(paths: list[Path], store: Path, *, lang: str, psm: int, workers: int, min_conf: float) -> dict[str, list[str]]:
    feitos = load_ocr(store)
    pendentes = [p for p in paths if p.name not in feitos]
    if len(pendentes) < len(paths):
        log(f"[ocr] retomando: {len(paths) - len(pendentes)} quadros já lidos")
    if not pendentes:
        return feitos
    trava = threading.Lock()
    inicio = time.monotonic()
    passo = max(1, len(pendentes) // 20)
    with store.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=workers) as pool:
        futuros = {pool.submit(ocr_one, p, lang, psm, min_conf): p for p in pendentes}
        for n, futuro in enumerate(as_completed(futuros), 1):
            path = futuros[futuro]
            linhas = futuro.result()
            with trava:
                feitos[path.name] = linhas
                fh.write(json.dumps({"frame": path.name, "linhas": linhas}, ensure_ascii=False) + "\n")
                fh.flush()
            if n % passo == 0 or n == len(pendentes):
                decorrido = time.monotonic() - inicio
                taxa = n / decorrido if decorrido > 0 else 0.0
                falta = (len(pendentes) - n) / taxa if taxa > 0 else 0.0
                log(f"[ocr] {n}/{len(pendentes)} quadros · {taxa:.1f}/s · faltam ~{int(falta)}s")
    return feitos


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OCR de vídeo de tela sem linhas repetidas")
    ap.add_argument("video")
    ap.add_argument("--out", required=True, help="pasta de saída (retomável)")
    ap.add_argument("--modo", choices=("auto", "chat", "texto"), default="auto",
                    help="chat = conversa em balões; texto = OCR de texto corrido; auto tenta chat primeiro")
    ap.add_argument("--fps", type=float, default=1.0, help="quadros por segundo de vídeo (padrão 1)")
    ap.add_argument("--width", type=int, default=1600, help="largura máxima do quadro para o OCR")
    ap.add_argument("--crop", default=None, help="X,Y,W,H em pixels do vídeo ORIGINAL")
    ap.add_argument("--lang", default="por", help="idiomas do tesseract (ex.: por+eng)")
    ap.add_argument("--psm", type=int, default=4, help="modo de página do tesseract")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--min-conf", type=float, default=MIN_LINE_CONF)
    ap.add_argument("--min-support", type=int, default=2,
                    help="em quantos quadros uma linha precisa aparecer (1 desliga o filtro)")
    ap.add_argument("--keep-frames", action="store_true", help="não apaga os quadros no fim")
    args = ap.parse_args(argv)

    video = Path(args.video)
    if not video.is_file():
        log(f"ERRO: vídeo não encontrado: {video}")
        return 2
    try:
        crop = parse_crop(args.crop)
    except ValueError as exc:
        log(f"ERRO: {exc}")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frames_dir = out / "quadros"
    ocr_store = out / "ocr.jsonl"
    estado_path = out / "estado.json"
    resultado_path = out / "resultado.json"
    inicio = time.monotonic()

    params = {
        "video": str(video.resolve()), "tamanho": video.stat().st_size,
        "fps": args.fps, "width": args.width, "crop": args.crop,
        "lang": args.lang, "psm": args.psm, "min_conf": args.min_conf, "modo": args.modo,
    }
    estado = json.loads(estado_path.read_text(encoding="utf-8")) if estado_path.exists() else {}
    if estado.get("params") != params:
        shutil.rmtree(frames_dir, ignore_errors=True)
        shutil.rmtree(out / "chat", ignore_errors=True)
        ocr_store.unlink(missing_ok=True)
        estado = {"params": params}
    elif estado.get("concluido") and resultado_path.exists():
        log("[ok] já concluído com estes parâmetros — nada a refazer")
        log("RESULT " + resultado_path.read_text(encoding="utf-8").strip())
        return 0

    meta = probe(video)
    log(f"[probe] {meta['duration']:.1f}s · {meta['width']}x{meta['height']} · {meta['fps']:g} fps")

    if args.modo != "texto":
        # Import tardio: numpy/Pillow só existem no sandbox, e os testes da lógica
        # pura deste arquivo rodam sem eles.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import chat_ocr

        resumo = chat_ocr.executar(
            video, out, meta, lang=args.lang, workers=max(1, args.workers), crop=crop,
            keep_frames=args.keep_frames, forcar=args.modo == "chat",
        )
        if resumo is not None:
            resultado = {
                "modo": "chat", "video": str(video), "duracao_s": round(meta["duration"], 1),
                **resumo, "saida": str(out), "segundos": int(time.monotonic() - inicio),
            }
            resultado_path.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
            estado["concluido"] = True
            estado_path.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
            log("RESULT " + json.dumps(resultado, ensure_ascii=False))
            return 0
        if args.modo == "chat":
            log("ERRO: nenhum balão de conversa encontrado — rode com --modo texto")
            return 3
        log("[modo] sem balões de conversa — OCR de texto corrido")

    if not estado.get("extraido") or not any(frames_dir.glob("f_*.png")):
        shutil.rmtree(frames_dir, ignore_errors=True)
        ocr_store.unlink(missing_ok=True)
        extract(video, frames_dir, args.fps, args.width, crop, meta["duration"])
        estado["extraido"] = True
        estado_path.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
    paths = sorted(frames_dir.glob("f_*.png"))
    log(f"[extrair] {len(paths)} quadros a {args.fps:g} por segundo de vídeo")

    mantidos = distinct_frames([thumb(p) for p in paths])
    alvo = [paths[i] for i in mantidos]
    log(f"[repetidos] {len(paths)} → {len(alvo)} quadros com mudança")

    feitos = ocr_frames(
        alvo, ocr_store, lang=args.lang, psm=args.psm,
        workers=max(1, args.workers), min_conf=args.min_conf,
    )
    linhas_por_quadro = [feitos.get(p.name, []) for p in alvo]
    chrome = find_chrome(linhas_por_quadro)
    frames = [((int(p.stem.split("_")[1]) - 1) / args.fps, feitos.get(p.name, [])) for p in alvo]
    transcricao = merge_frames(frames, chrome)
    transcricao, descartadas, suporte = drop_single_frame_lines(
        transcricao, frames, chrome, args.min_support
    )
    rolagem_rapida = 0 < suporte < ROLAGEM_LENTA_MIN_SUPORTE
    log(
        f"[juntar] {len(transcricao)} linhas únicas · {len(chrome)} de interface removidas · "
        f"{len(descartadas)} vistas num quadro só · linha típica em {suporte:g} quadros"
    )
    if rolagem_rapida:
        log(
            f"[aviso] rolagem rápida: a linha típica aparece em só {suporte:g} quadro(s) a "
            f"{args.fps:g} fps — mensagens podem ter passado entre dois quadros. Rode de novo "
            f"com --fps {args.fps * 2:g}."
        )

    arquivos = {
        "transcricao": str(out / "transcricao.txt"),
        "linhas": str(out / "linhas.jsonl"),
        "interface": str(out / "interface.txt"),
        "descartadas": str(out / "descartadas.txt"),
    }
    (out / "transcricao.txt").write_text(
        "".join(f"[{mmss(item['t'])}] {item['texto']}\n" for item in transcricao), encoding="utf-8"
    )
    (out / "linhas.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in transcricao), encoding="utf-8"
    )
    (out / "interface.txt").write_text("".join(f"{texto}\n" for texto in chrome.values()), encoding="utf-8")
    (out / "descartadas.txt").write_text(
        "".join(f"[{mmss(item['t'])}] {item['texto']}\n" for item in descartadas), encoding="utf-8"
    )

    if not args.keep_frames:
        shutil.rmtree(frames_dir, ignore_errors=True)
        log("[limpeza] quadros apagados (o OCR de cada um fica em ocr.jsonl)")

    resultado = {
        "modo": "texto", "video": str(video), "duracao_s": round(meta["duration"], 1),
        "quadros": len(paths), "quadros_com_mudanca": len(alvo),
        "linhas": len(transcricao), "interface_removida": len(chrome),
        "descartadas": len(descartadas), "suporte_mediano": suporte,
        "rolagem_rapida": rolagem_rapida,
        "saida": str(out), "arquivos": arquivos,
        "segundos": int(time.monotonic() - inicio),
    }
    resultado_path.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    estado["concluido"] = True
    estado_path.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
    log("RESULT " + json.dumps(resultado, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
