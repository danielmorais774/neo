# neo-voice

Serviço de **voz** do neo (STT + TTS). O **Mac** orquestra e serve o painel web;
o **PC gamer** (RTX 5070 Ti / Blackwell) roda os dois motores na GPU:
- **STT**: Parakeet TDT 0.6B v3 (ONNX) — porta 5005.
- **TTS**: Qwen3-TTS 0.6B CustomVoice (torch) — porta 5006.

```
  ┌──────────── Mac (:5002) ───────────┐      LAN       ┌──────── PC gamer (Windows) ────────┐
  │ neo-voice service + painel web      │               │ engine-stt (:5005)  Parakeet v3    │
  │  • painel 2 abas (Ouvir / Falar)    │ ───────────▶ │   onnx-asr + onnxruntime-gpu       │
  │  • STT: VAD Silero + segmentação    │               │ engine-tts (:5006)  Qwen3-TTS      │
  │  • TTS: proxy texto→voz             │ ◀─────────── │   torch cu128 (bf16, sdpa)         │
  └─────────────────────────────────────┘               │   RTX 5070 Ti                      │
                                                         └────────────────────────────────────┘
```

## Por que essa arquitetura
- O Mac (Apple Silicon) não tem CUDA; os modelos rodam muito mais rápido na RTX.
- Motores desacoplados: qualquer cliente do neo manda áudio/texto e recebe texto/voz.
- STT mantém o contrato do antigo `neo-transcribe-voice` (WS `/ws/audio`, PCM int16 16 kHz).

## engine-stt/ — roda no PC gamer (192.168.4.185)
Stack: `onnx-asr` + `onnxruntime-gpu 1.23.2` + wheels `nvidia-*` (CUDA 12.9 / cuDNN 9,
com kernels Blackwell sm_120). O servidor adiciona `site-packages/nvidia/*/bin` ao
DLL search path em runtime para o CUDA EP inicializar no Windows.

Instalação (já feita em `C:\Users\dmdan\neo-stt-engine`):
```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Subir o engine:
```powershell
# Em primeiro plano (console interativo, mostra logs):
powershell -ExecutionPolicy Bypass -File run_engine.ps1

# OU destacado (sobrevive ao fechar a sessao SSH; usa GPU; loga em engine.log):
powershell -ExecutionPolicy Bypass -File engine_ctl.ps1 start
powershell -ExecutionPolicy Bypass -File engine_ctl.ps1 status   # RODANDO/PARADO + tail
powershell -ExecutionPolicy Bypass -File engine_ctl.ps1 stop

# Porta liberada no firewall (perfis Private/Domain) via:
powershell -ExecutionPolicy Bypass -File open_firewall.ps1
```

> **Notas de deploy no Windows** (aprendidas no setup):
> - O OpenSSH do Windows mata a arvore de processos ao fechar a sessao. Por isso o
>   `engine_ctl.ps1 start` lanca o processo via WMI (`Win32_Process.Create`), que
>   escapa do job do SSH e segue rodando.
> - Nao usar `pythonw.exe` (sob ele `sys.stdout/err` sao `None` e o uvicorn falha);
>   o launcher `run_engine.cmd` usa `python.exe` e redireciona tudo para `engine.log`.
> - O CUDA EP so inicializa se os `site-packages/nvidia/*/bin` estiverem no **PATH**
>   (nao basta `os.add_dll_directory`) — o servidor ja faz isso no import.
> - Autostart no boot (Scheduled Task) **nao** foi configurado — peca se quiser.

Endpoints:
- `GET  /health` → modelo, providers reais, `on_gpu`, nome da GPU.
- `POST /transcribe` → multipart `file` (wav/flac/ogg) **ou** corpo `application/octet-stream`
  com PCM int16 mono (header `X-Sample-Rate`, default 16000). Retorna
  `{text, audio_seconds, infer_ms, rtf, on_gpu}`.
- `WS   /ws/transcribe` → manda chunks PCM int16; manda texto `flush` para fechar a fala
  e receber `{"type":"final","text":...}`.

Validar a GPU isolada:
```powershell
.\.venv\Scripts\python probe_gpu.py samples\chunk_1.wav
```

### Variante CPU — LXC `parakeet-cpu` (192.168.4.230, i9-10900)
Mesmo `neo_stt_engine.py`, sem GPU: o `onnxruntime` CPU roda o Parakeet em
`quantization=int8`. Serve como STT local quando o PC gamer está desligado, evitando
cair no fallback pago da OpenRouter. Deploy em `/opt/neo-stt-cpu`, systemd
`neo-stt-cpu.service`, mesma porta 5005.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-cpu.txt
./engine_ctl.sh start|stop|restart|status     # wrapper systemd, mesma interface do .ps1
```

Pico de ~2.1 GB de RSS. Cerca de 18× mais rápido que tempo real.

**CPU vs GPU, mesmo harness** (POST PCM int16, mediana de 5, `infer_ms` do engine, sem rede).
A comparação honesta é contra a RTX **em int8**, que é a única config correta nas duas pontas:

| áudio | CPU int8 | RTX int8 | CPU/RTX | RTX fp32 (quebrada) |
|---|---|---|---|---|
| 2 s  |  140 ms |  105 ms | 1.3× |  27 ms |
| 5 s  |  259 ms |  178 ms | 1.5× |  35 ms |
| 10 s |  464 ms |  299 ms | 1.6× |  58 ms |
| 15 s |  692 ms |  418 ms | 1.7× |  90 ms |
| 20 s |  909 ms |  528 ms | 1.7× | 115 ms |
| 28 s |  1.30 s |  710 ms | 1.8× | 141 ms |
| 66 s |  3.73 s |  1.58 s | 2.4× | 285 ms |

Na faixa de fala real (2–10 s) o i9 fica **35–165 ms atrás** da RTX — irrelevante ao lado do
LLM e do TTS. A GPU só abre vantagem de verdade em áudio longo (batch/transcrição de arquivo).

A coluna fp32 **não serve de baseline**: além de perder fala (ver aviso abaixo), boa parte da
sua "velocidade" é ela emitindo menos tokens — no áudio de 66 s são 159 caracteres contra 812.
Aos 2 s, onde as três configs devolvem o mesmo texto, o custo real do int8 na RTX aparece:
27 ms → 105 ms (os nós `Memcpy` do CUDA EP).

> ### ⚠ O encoder fp32 é defeituoso — use `NEO_STT_QUANT=int8` (vale para a GPU também)
> O `encoder-model.onnx` **fp32** do repo ONNX perde fala a partir de ~16 s, de forma
> determinística e dependente da duração. Medido com os mesmos arquivos:
>
> | corte | fp32 | int8 |
> |---|---|---|
> | 16 s | **0 chars** | 219 |
> | 17 s | 173 | 245 |
> | 18 s | **0 chars** | 244 |
> | 19 s | **0 chars** | 253 |
> | 21 s | 30 (começa aos ~18 s do áudio) | 275 |
> | 66 s | 159 | 812 |
>
> **Não é da CPU**: a RTX 5070 Ti (CUDA EP, fp32) devolve os mesmos números, texto
> idêntico. Isolado por troca cruzada dos arquivos: o culpado é o **encoder**, não o
> `decoder_joint`. Não há NaN/Inf e `ORT_DISABLE_ALL` não muda nada — é o grafo.
> O int8 do repo é *weight-only* (quantiza só MatMul/Gemm; Conv e ativações seguem
> fp32), então não custa qualidade. A recomendação oficial do `onnx-asr` é a mesma:
> segmentar com VAD acima de 20–30 s.
>
> Na RTX, o int8 custa latência (ORT insere 742 nós `Memcpy` porque o CUDA EP não roda
> os ops quantizados): 2 s → 121 ms e 16 s → 464 ms, contra 30/72 ms do fp32. Ainda
> cabe na cadência de parciais de 600 ms, e devolve a fala em vez de string vazia.

> **Threads**: `NEO_STT_THREADS=8` é o ponto ótimo. Acima disso o hyperthreading atrapalha
> (20 threads ficam ~40 % mais lentas que 8 no áudio curto). Sem a env, o ORT abre uma
> thread por vCPU.

Limite prático como motor de **parciais** ao vivo: a CPU acompanha a cadência de
`VAD_PARTIAL_SEC`=0.6 s até ~13 s de fala acumulada (12 s → 562 ms); a partir de 15 s
a inferência passa de 600 ms e as parciais viram fila. Para finais de fala, tanto faz.

## engine-tts/ — roda no **WSL2 Ubuntu** do PC gamer (`/opt/neo-tts`, distro em `G:\wsl`)
Stack: **torch 2.11 cu128** + `qwen-tts` + `transformers`. Modelo
**Qwen3-TTS-12Hz-0.6B-CustomVoice** (bf16, attn **sdpa**). 9 vozes; `language="Portuguese"`; 24 kHz; porta **5006**.

Por que WSL e não Windows nativo: o **WDDM** do Windows estrangula esse modelo (loop autoregressivo →
overhead de kernel, GPU ~30%). No Windows: rtf ~1.13; no **WSL/Linux: rtf ~0.87** (~25% melhor).
`torch.compile`/flash-attn **não ajudam** (o gargalo é o loop do qwen-tts, não compute). Pré-requisitos
do WSL2: `bcdedit /set hypervisorlaunchtype auto` + VM Platform (2 reboots) e rede **mirrored**
(`%USERPROFILE%\.wslconfig` → `[wsl2] networkingMode=mirrored`) p/ a 5006 do WSL ser alcançável na LAN.

```powershell
# setup (já feito): wsl --install -d Ubuntu; wsl --manage Ubuntu --move G:\wsl\Ubuntu;
#                   wsl ... bash /opt/neo-tts/...  (torch cu128 + qwen-tts)
# subir MANUAL destacado (WMI lança `wsl ... python`; sobrevive ao SSH; loga em /opt/neo-tts/tts.log):
powershell -ExecutionPolicy Bypass -File engine-tts\engine_ctl_wsl.ps1 start|status|stop
```
Endpoints: `GET /health`, `GET /voices`, `POST /tts` (`{text, voice, language, instruct}` → `audio/wav`).
Desempenho: rtf ~0.87; **cada chamada tem custo fixo ~2.5 s**. Por isso o **streaming por frases**
(no serviço do Mac) é o que dá a sensação de rapidez: 1º som em ~2.5 s. O 0.6B **ignora `instruct`**
(o campo Estilo não age); o 1.7B suportaria, mas ficamos no 0.6B por velocidade.

### Coqui XTTS-v2 (2º motor, `/opt/neo-coqui`, porta **5007**) — o rápido
Mesma API do Qwen. **coqui-tts** + **XTTS-v2** (multilíngue PT, **58 vozes** embutidas).
**rtf ~0.14** (gera 6.9 s de áudio em ~1.2 s) → 1º som em ~0.9 s. Roda no WSL em venv **Python 3.11**
(uv; coqui-tts não suporta o 3.14 da distro), **transformers==4.57.3** (5.x quebra o import),
**torchcodec** + `ffmpeg`, e **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** (evita OOM falso do WSL).
```powershell
powershell -ExecutionPolicy Bypass -File engine-tts\engine_ctl_coqui_wsl.ps1 start|status|stop
```
No painel, o **select "Motor"** escolhe entre Qwen, Coqui e Dia. O serviço do Mac roteia por
`backend=qwen|coqui|dia` em `/voices`, `/tts` e `/ws/tts`.

**Clonagem de voz (Coqui *e* Qwen):** `POST /clone?backend=coqui|qwen` (multipart: `file` = áudio de
referência ~6–20s de fala limpa + `name`) cria uma voz `clone_<id>` que aparece no dropdown. Persistida
e recarregada no startup. No painel há um campo de upload + botão **Clonar** (visível p/ Coqui e Qwen).
- **Coqui** (`/opt/neo-coqui/clones/`): `get_conditioning_latents`, rtf ~0.16.
- **Qwen** (`/opt/neo-tts/clones_qwen/`): usa o modelo **Base** (carregado lazy no 1º clone),
  `create_voice_clone_prompt(x_vector_only_mode=True)` + `generate_voice_clone`, rtf ~0.87.

Gotchas: o áudio de ref é lido via `torchaudio.load`→torchcodec (falta libs CUDA no WSL) → ambos engines
fazem monkeypatch `torchaudio.load → soundfile`. Carregar o 2º modelo (Qwen Base) com `device_map='cuda:0'`
bate no OOM falso do WSL → usar `device_map='auto' + max_memory`. Precisa `python-multipart` nos venvs.

## service/ — roda no Mac
- `neo_voice_server.py` — serviço FastAPI. Endpoints:
  - `GET /` — **painel web** com **2 abas** (`web/index.html`): 🎙️ Ouvir (STT) e 🔊 Falar (TTS).
  - `WS /ws/panel` — STT do painel: recebe PCM int16 16 kHz, devolve JSON
    `{type: partial|final|response, text, ...}` (parciais ao vivo).
  - `WS /ws/audio` — STT contrato antigo (`FINAL_TEXT:`/`FINAL_RESPONSE:`), drop-in.
  - `GET /voices` — vozes do TTS (proxy do engine).
  - `POST /tts` — `{text, voice, language, instruct}` → `audio/wav` (proxy, não-streaming).
  - `WS /ws/tts` — **TTS streaming por frases**: quebra o texto, sintetiza cada frase no engine e
    envia o PCM por chunks (1º som rápido). É o que a aba Falar usa.
  - `GET /health` — status do serviço + engines STT e TTS.
- `engine_client.py` — cliente HTTP do engine STT.
- `mic_to_engine.py` — teste do mic do Mac → STT.
- `web/index.html` — o painel (AudioWorklet 16 kHz + WebSocket, autocontido).

### Painel web (2 abas)
```bash
.venv-sys/bin/python neo_voice_server.py     # serve em http://localhost:5002
open http://localhost:5002                    # mic exige contexto seguro (localhost)
```
- **Ouvir (STT)**: clique no mic e fale; o parcial cresce ao vivo (re-transcrição a cada
  `VAD_PARTIAL_SEC`=0.6 s) e cada frase finalizada vira uma linha.
- **Falar (TTS)**: escreva, escolha a voz (e estilo opcional), clique em Falar; o áudio
  é sintetizado na RTX e toca no navegador (Cmd/Ctrl+Enter atalha).

**Cascata de STT**: PC gamer (GPU) → `parakeet-cpu` (LXC) → OpenRouter (**só finais**).
Cada degrau é pulado em 0 ms quando o `engine_monitor` já o marcou `down`, sem pagar o
timeout do host morto. O engine de CPU é local e gratuito, então também atende as
parciais; só a nuvem fica reservada aos finais. A origem de cada transcrição aparece no
log e no campo `source` (`local` / `local-cpu` / `openrouter`).

Config (env):
- `NEO_STT_ENGINE_URL` (default `http://192.168.4.185:5005`)
- `NEO_STT_CPU_ENGINE_URL` (default `http://192.168.4.230:5005`; `""` desliga a camada)
- `NEO_TTS_ENGINE_URL` (default `http://192.168.4.185:5006`)
- `NEO_STT_APIKEY` (opcional)
- `NEO_CORE_URL` (opcional, p/ encaminhar texto ao neo-core)
- VAD: `VAD_SILENCE_SEC` (0.8), `VAD_MIN_SPEECH_SEC` (0.3), `VAD_MAX_UTTER_SEC` (28),
  `VAD_PREROLL_SEC` (0.2 — captura o ataque/onset da fala que o VAD perde).

> `VAD_MAX_UTTER_SEC=28` só é seguro com engines em **int8**. Contra um engine fp32 (ver
> o aviso em engine-stt/), qualquer fala acima de ~16 s volta vazia ou truncada — o teto
> de 28 s está bem acima do ponto de quebra. Com fp32, use ≤ 15.

> **Sobre a cauda da fala**: NÃO apare o silêncio final do segmento — o transducer do
> Parakeet usa esse silêncio (~`VAD_SILENCE_SEC`) para fechar a última palavra. Aparar
> degrada o fim (testado: "Cuba" → "Cubo"/"Google"). A transcrição do áudio inteiro
> (engine direto) é exata; pequenas variações no caminho com VAD são sensibilidade do
> modelo às fronteiras do segmento, mais visível em clipes sintéticos com corte seco.

Subir no Mac:
```bash
cd service
# venv recomendado: criado a partir do python do sistema (Apple-assinado),
# que tem acesso a "Rede Local" do macOS por padrao -> alcança o engine na LAN.
/usr/bin/python3 -m venv .venv-sys
.venv-sys/bin/python -m pip install -r requirements.txt
.venv-sys/bin/python neo_voice_server.py  # painel + STT/TTS em :5002
.venv-sys/bin/python mic_to_engine.py 6   # teste rápido com o microfone
```

> **macOS Local Network**: o macOS bloqueia acesso a IPs da LAN (192.168.x.x) para
> binários sem permissão de "Rede Local", retornando `Errno 65 No route to host`.
> O python do sistema (e venvs criados a partir dele) já têm acesso. Se preferir um
> Python próprio (uv/pyenv/Homebrew), conceda a permissão em
> *System Settings → Privacy & Security → Local Network* (ou rode uma vez no Terminal
> para aparecer o prompt). Diagnóstico: `service/check_local_network.py`.
