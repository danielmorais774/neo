#!/usr/bin/env python3
"""
neo_wake_enroll -- gera o arquivo de referencia ("treina" few-shot) de uma frase.

One-shot: o Rust chama isto depois que voce grava as amostras na UI. Le os WAVs
de uma pasta, calcula os embeddings com o EfficientWord-Net e escreve
`<slug>_ref.json` na pasta de refs. O detector (neo_wake.py) carrega esse json.

IMPORTANTE: NAO usamos o `generate_reference_file` do EfficientWord-Net -- ele tem
um bug no `fixPaddingIssues` (compara `< 16000` em vez de `< window_frames`), entao
gravacoes com ~[16000, 24000) amostras sao devolvidas sem padding e estouram o
assert do audioToVector. Aqui normalizamos cada amostra para exatamente 24000
(1.5s @16k) e chamamos audioToVector direto -- deterministico e a prova do bug.

O JSON gerado e identico ao do EfficientWord-Net: {"embeddings": [[...]], "model_type": "..."}.

Requisito do EfficientWord-Net: **>= 4 amostras** por frase.

Emite no stdout (uma linha JSON):
    {"event": "enrolled", "wakeword": "<slug>", "ref": "<path>", "samples": N}
    {"event": "error", "message": "..."}     # sai com !=0

Config por env (ou argv: samples_dir out_dir slug):
    NEO_WAKE_ENROLL_SAMPLES  pasta com os .wav da frase.
    NEO_WAKE_ENROLL_OUT      pasta de saida dos refs.
    NEO_WAKE_ENROLL_WORD     slug da frase (vira <slug>_ref.json).
"""
import os
import sys
import glob
import json

WINDOW = 24000        # 1.5s @16k -- janela do Resnet50_Arc_loss
MODEL_TYPE = "resnet_50_arc"


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def die(msg) -> None:
    emit({"event": "error", "message": str(msg)})
    sys.exit(1)


def fit_window(x, n=WINDOW):
    """Normaliza para exatamente `n` amostras: corta o excesso no fim, ou
    completa com zeros no fim. A frase fica alinhada ao inicio (o usuario fala
    logo apos clicar)."""
    import numpy as np

    if x.shape[0] >= n:
        return x[:n].astype(np.float32)
    pad = np.zeros(n - x.shape[0], dtype=np.float32)
    return np.concatenate((x.astype(np.float32), pad))


def main() -> None:
    args = sys.argv[1:]
    samples_dir = os.environ.get("NEO_WAKE_ENROLL_SAMPLES") or (args[0] if len(args) > 0 else "")
    out_dir = os.environ.get("NEO_WAKE_ENROLL_OUT") or (args[1] if len(args) > 1 else "")
    slug = os.environ.get("NEO_WAKE_ENROLL_WORD") or (args[2] if len(args) > 2 else "")

    if not samples_dir or not out_dir or not slug:
        die("faltam NEO_WAKE_ENROLL_SAMPLES / _OUT / _WORD (ou argv).")
        return
    if not os.path.isdir(samples_dir):
        die(f"pasta de amostras inexistente: {samples_dir}")
        return

    wavs = sorted(glob.glob(os.path.join(samples_dir, "*.wav")))
    if len(wavs) < 4:
        die(f"o EfficientWord-Net exige >= 4 amostras; encontrei {len(wavs)} em {samples_dir}.")
        return

    try:
        import numpy as np
        import soundfile as sf
        from eff_word_net.audio_processing import Resnet50_Arc_loss
    except Exception as e:
        die(f"EfficientWord-Net/deps ausentes: {e}. Rode o setup.sh do sidecar.")
        return

    model = Resnet50_Arc_loss()

    embeddings = []
    for wav in wavs:
        try:
            x, sr = sf.read(wav, dtype="float32")
            if x.ndim > 1:
                x = x[:, 0]  # mono
            vec = model.audioToVector(fit_window(x))  # (1, 2048)
            embeddings.append(np.squeeze(np.asarray(vec)))
        except Exception as e:
            die(f"falha ao processar {os.path.basename(wav)}: {e!r}")
            return

    emb = np.asarray(embeddings)
    if emb.shape[0] < 4:
        die(f"so consegui processar {emb.shape[0]} amostras (precisa de >=4).")
        return

    os.makedirs(out_dir, exist_ok=True)
    ref = os.path.join(out_dir, f"{slug}_ref.json")
    with open(ref, "w") as f:
        json.dump(
            {"embeddings": emb.astype(float).tolist(), "model_type": MODEL_TYPE},
            f,
        )

    emit({"event": "enrolled", "wakeword": slug, "ref": ref, "samples": int(emb.shape[0])})


if __name__ == "__main__":
    main()
