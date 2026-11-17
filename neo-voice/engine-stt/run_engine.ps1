# Sobe o neo-stt engine (Parakeet TDT 0.6B v3, GPU) no PC gamer.
# Uso:  powershell -ExecutionPolicy Bypass -File run_engine.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Quantizacao do modelo: o repo onnx tem fp32 (default) e int8.
# fp32 cabe folgado nos 16GB da RTX e da melhor precisao; deixe vazio.
# Para economizar disco/VRAM use $env:NEO_STT_QUANT = "int8".
if (-not $env:NEO_STT_PORT)  { $env:NEO_STT_PORT  = "5005" }

& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\neo_stt_engine.py"
