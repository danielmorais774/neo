"""
engine_monitor.py -- tabela em RAM da alcancabilidade real de cada engine local.

Problema que resolve: STT/TTS tem um provider PRIMARIO (engine local no PC gamer, IP
da LAN) e um de FALLBACK (OpenRouter p/ STT, Kokoro p/ TTS). Antes, cada request
DESCOBRIA na hora que o primario estava caido pagando o connect/read timeout, e so
entao caia no fallback -- lento e repetido. Pior no cenario WSL/portproxy, onde a
porta aceita TCP mas o backend esta morto (o /tts trava ate o read timeout).

Solucao: um poller em BACKGROUND pinga o /health de cada engine num intervalo curto
e mantem um estado up/down/unknown por engine nesta tabela (em RAM, construida no
start). O caminho de request LE a tabela em O(1): se o engine esta DOWN, vai direto
pro fallback (0ms, sem esperar timeout); se UP, usa o local. Resultados de requests
reais tambem realimentam a tabela na hora (sinal forte, sincrono), entao a deteccao
de uma queda e imediata quando ha trafego, e o poller cobre os periodos ociosos.

Uso (no neo_voice_server):

    from engine_monitor import EngineHealthMonitor, EngineState, UP, DOWN, UNKNOWN
    monitor = EngineHealthMonitor(http_client, [EngineState("stt", url, "stt", ...)], ...)
    monitor.start()                     # dispara o poller (num startup async)
    if monitor.should_try("stt"): ...   # leitura O(1) no caminho de request
    monitor.record_request("stt", ok)   # realimenta com o resultado real
"""
from __future__ import annotations

import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

log = logging.getLogger("neo-voice.monitor")

UP, DOWN, UNKNOWN = "up", "down", "unknown"


@dataclass
class EngineState:
    """Estado de um engine na tabela. `health_key`/`url_key` sao as chaves com que
    esse engine aparece no JSON do /health do neo-voice (p/ o /health servir a
    tabela em cache preservando o shape que o painel espera)."""
    name: str            # nome logico usado pelo caminho de request (ex "stt", "f5")
    url: str             # base url do engine (ex http://192.168.4.185:5005)
    group: str           # "stt" | "tts"
    health_key: str = ""  # chave do corpo no /health (ex "engine", "tts", "coqui")
    url_key: str = ""     # chave da url no /health (ex "engine_url", "tts_engine_url")

    status: str = UNKNOWN
    consecutive_fails: int = 0
    last_ok_ts: float = 0.0
    last_check_ts: float = 0.0
    last_change_ts: float = 0.0
    latency_ms: Optional[float] = None
    last_error: Optional[str] = None
    body: Optional[dict] = None   # ultimo /health bem-sucedido (passthrough p/ o painel)

    def as_dict(self) -> dict:
        now = time.time()
        return {
            "name": self.name,
            "url": self.url,
            "group": self.group,
            "status": self.status,
            "reachable": self.status == UP,
            "consecutive_fails": self.consecutive_fails,
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms is not None else None,
            "last_ok_age_s": round(now - self.last_ok_ts, 1) if self.last_ok_ts else None,
            "last_check_age_s": round(now - self.last_check_ts, 1) if self.last_check_ts else None,
            "since_s": round(now - self.last_change_ts, 1) if self.last_change_ts else None,
            "last_error": self.last_error,
        }


class EngineHealthMonitor:
    """Poller em background + tabela de status em RAM. Thread-safe por construcao:
    asyncio e cooperativo e nenhuma transicao (`_apply`) da await no meio, entao
    poller e caminho de request nunca corrompem o estado."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        engines: list[EngineState],
        headers_fn: Optional[Callable[[], dict]] = None,
        *,
        interval: float = 4.0,
        probe_timeout: float = 2.0,
        fail_threshold: int = 2,
    ):
        self._client = client
        self._engines: dict[str, EngineState] = {e.name: e for e in engines}
        self._headers_fn = headers_fn or (lambda: {})
        self.interval = max(0.5, interval)
        self.probe_timeout = max(0.2, probe_timeout)
        # quantas falhas seguidas do POLLER p/ marcar down (absorve um blip isolado).
        # Um request real que falha marca down na hora, sem esperar o threshold.
        self.fail_threshold = max(1, fail_threshold)
        self._task: Optional[asyncio.Task] = None

    # ---- leitura O(1) pelo caminho de request ------------------------------
    def status(self, name: str) -> str:
        e = self._engines.get(name)
        return e.status if e else UNKNOWN

    def is_up(self, name: str) -> bool:
        return self.status(name) == UP

    def is_down(self, name: str) -> bool:
        return self.status(name) == DOWN

    def should_try(self, name: str) -> bool:
        """True se vale tentar o primario. So pula quando SABEMOS que esta DOWN --
        um engine 'unknown' (nunca sondado, ou monitor desligado) nunca e pulado
        injustamente: melhor tentar e cair no fallback do que negar um engine vivo."""
        return self.status(name) != DOWN

    def get(self, name: str) -> Optional[EngineState]:
        return self._engines.get(name)

    def snapshot(self) -> dict:
        return {name: e.as_dict() for name, e in self._engines.items()}

    # ---- transicoes de estado ---------------------------------------------
    def _apply(self, e: EngineState, ok: bool, *, latency: Optional[float] = None,
               err=None, body: Optional[dict] = None, hard: bool = False):
        now = time.time()
        e.last_check_ts = now
        prev = e.status
        if ok:
            e.consecutive_fails = 0
            e.last_ok_ts = now
            e.latency_ms = latency
            e.last_error = None
            if body is not None:
                e.body = body
            e.status = UP
        else:
            e.consecutive_fails += 1
            # str() de um ReadTimeout/ConnectTimeout costuma vir vazio -> usa o nome
            # da excecao p/ o painel/health nao mostrarem erro em branco.
            e.last_error = (str(err) or type(err).__name__) if err else "inalcancavel"
            # request real que falhou (hard) marca down imediatamente; o poller
            # respeita o fail_threshold p/ nao flapar num blip transitorio.
            if hard or e.consecutive_fails >= self.fail_threshold:
                e.status = DOWN
        if e.status != prev:
            e.last_change_ts = now
            detail = (f"{latency:.0f}ms" if (ok and latency is not None) else (e.last_error or ""))
            log.info("engine '%s': %s -> %s (%s)", e.name, prev, e.status, detail)

    def record_request(self, name: str, ok: bool, err=None, latency: Optional[float] = None):
        """Realimenta a tabela com o resultado de um request REAL (STT/TTS). Sinal
        forte e sincrono: uma falha marca down na hora (sem esperar o poller), um
        sucesso marca up (a sintese/transcricao respondeu = engine vivo de fato)."""
        e = self._engines.get(name)
        if e is None:
            return
        self._apply(e, ok, latency=latency, err=err, hard=not ok)

    # ---- poller em background ---------------------------------------------
    async def _probe(self, e: EngineState):
        t0 = time.time()
        try:
            r = await self._client.get(
                f"{e.url}/health",
                headers=self._headers_fn(),
                timeout=httpx.Timeout(self.probe_timeout),
            )
            ok = r.status_code == 200
            body = None
            if ok:
                try:
                    body = r.json()
                except Exception:
                    body = {"status": "ok"}
            self._apply(
                e, ok,
                latency=(time.time() - t0) * 1000 if ok else None,
                err=None if ok else f"HTTP {r.status_code}",
                body=body,
            )
        except Exception as ex:
            self._apply(e, False, err=ex)

    async def _sweep(self):
        # concorrente: um sweep leva ~probe_timeout no pior caso, nao a soma dos engines.
        await asyncio.gather(*(self._probe(e) for e in self._engines.values()))

    async def run(self):
        log.info("monitor de engines iniciado (%d engines, intervalo %.1fs, probe %.1fs, threshold %d)",
                 len(self._engines), self.interval, self.probe_timeout, self.fail_threshold)
        await self._sweep()  # 1a varredura ja no boot -> tabela pronta em ~probe_timeout
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self._sweep()
            except asyncio.CancelledError:
                break
            except Exception as ex:  # nunca deixa o poller morrer por um erro isolado
                log.warning("erro no sweep do monitor: %s", ex)
                try:
                    await asyncio.sleep(self.interval)
                except asyncio.CancelledError:
                    break
        log.info("monitor de engines parado")

    def start(self) -> Optional[asyncio.Task]:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
