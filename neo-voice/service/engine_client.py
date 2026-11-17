"""
Cliente do neo-stt engine (Parakeet v3 rodando no PC gamer).

O Mac orquestra; a transcricao pesada acontece na RTX 5070 Ti via rede.

    from engine_client import EngineClient
    c = EngineClient("http://192.168.4.185:5005")
    print(c.health())
    print(c.transcribe_pcm(pcm_int16_bytes, sample_rate=16000))
    print(c.transcribe_file("audio.wav"))
"""
from __future__ import annotations

import os
import httpx


DEFAULT_ENGINE_URL = os.environ.get("NEO_STT_ENGINE_URL", "http://192.168.4.185:5005")


class EngineClient:
    def __init__(self, base_url: str = DEFAULT_ENGINE_URL, api_key: str | None = None,
                 timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("NEO_STT_APIKEY") or None
        self._client = httpx.Client(timeout=timeout)

    def _headers(self, extra: dict | None = None) -> dict:
        h = dict(extra or {})
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    def health(self) -> dict:
        r = self._client.get(f"{self.base_url}/health", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def transcribe_pcm(self, pcm_int16: bytes, sample_rate: int = 16000) -> dict:
        """Envia PCM int16 mono bruto e retorna {text, audio_seconds, infer_ms, ...}."""
        r = self._client.post(
            f"{self.base_url}/transcribe",
            content=pcm_int16,
            headers=self._headers({
                "Content-Type": "application/octet-stream",
                "X-Sample-Rate": str(sample_rate),
            }),
        )
        r.raise_for_status()
        return r.json()

    def transcribe_file(self, path: str) -> dict:
        """Envia um arquivo de audio (wav/flac/ogg)."""
        with open(path, "rb") as f:
            r = self._client.post(
                f"{self.base_url}/transcribe",
                files={"file": (os.path.basename(path), f, "audio/wav")},
                headers=self._headers(),
            )
        r.raise_for_status()
        return r.json()

    def close(self):
        self._client.close()


if __name__ == "__main__":
    import sys
    c = EngineClient(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENGINE_URL)
    print("health:", c.health())
    for path in sys.argv[2:]:
        print(f"{path}:", c.transcribe_file(path))
