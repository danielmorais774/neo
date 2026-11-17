# neo-voice (orquestrador de voz) — STT + TTS proxy. Roda na VPS Contabo,
# alcancando as engines do PC gamer pelo tunnel ZeroTier. Inclui fallbacks que
# rodam no proprio container: STT->OpenRouter (nuvem) e TTS->Kokoro (local, CPU).
FROM python:3.11-slim

# libsndfile1 -> soundfile (PCM<->WAV); libgomp1 -> torch (Silero VAD);
# espeak-ng -> G2P do kokoro-onnx p/ pt-BR (fallback de TTS); curl -> baixar o modelo.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 libgomp1 espeak-ng curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch + torchaudio CPU-only: o silero-vad depende dos dois. Instala AMBOS do
# mesmo indice CPU p/ casarem a ABI (senao o requirements puxa um torchaudio do
# PyPI incompativel e o _torchaudio.so nao carrega). Tambem evita as wheels CUDA.
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu

COPY service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# kokoro-onnx (fallback de TTS local via ONNX Runtime — bem mais rapido na CPU que
# o kokoro/torch). Nao depende de torch/transformers.
RUN pip install --no-cache-dir kokoro-onnx

# Baixa os arquivos do modelo onnx p/ dentro da imagem (offline em runtime).
ENV KOKORO_MODELS_DIR=/app/models
RUN mkdir -p /app/models \
 && curl -fsSL -o /app/models/kokoro-v1.0.onnx \
      https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx \
 && curl -fsSL -o /app/models/voices-v1.0.bin \
      https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# Pre-baixa o Silero VAD e aquece o kokoro-onnx (carrega a sessao ONNX + espeak).
RUN python -c "from silero_vad import load_silero_vad; load_silero_vad(onnx=False)" \
 && python -c "from kokoro_onnx import Kokoro; k=Kokoro('/app/models/kokoro-v1.0.onnx','/app/models/voices-v1.0.bin'); k.create('teste de voz do neo', voice='pf_dora', lang='pt-br')"

# Codigo do service (inclui web/ do painel; o server resolve web/ por __file__).
COPY service/ ./

EXPOSE 5002

# bind ja e 0.0.0.0:NEO_STT_PORT (default 5002) dentro do proprio server.
CMD ["python", "neo_voice_server.py"]
