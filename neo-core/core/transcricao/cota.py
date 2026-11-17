"""A conta da cota gratuita do Groq, feita pelo Neo ANTES de cada pedido.

O Groq limita por balde que enche sem parar (token bucket), não por janela que
zera. A prova está nos cabeçalhos da conta (15/11/2026): depois de 1 pedido, o
``x-ratelimit-reset-requests`` era 43,2 s, ou seja, 86 400 s ÷ 2000 pedidos/dia; e
3 s de áudio voltavam em 1,5 s, que é 3600 s ÷ 7200 s/hora. Este módulo imita os
quatro baldes (áudio por hora, áudio por dia, pedidos por minuto, pedidos por
dia), com os limites do ``[transcricao.cota]`` no ``neo.toml``.

Só vale com "só a cota gratuita" ligado no painel. É ele que garante "nunca passa
do gratuito": numa conta paga o Groq aceitaria mais, e a conta é cobrada.

Duas fontes de verdade, combinadas pelo lado mais conservador:
- **a conta própria**, gravada no ``user_preference`` (service="neo",
  key="cota") para sobreviver ao ``--reload``, que em prod acontece a cada arquivo
  salvo;
- **os cabeçalhos do Groq**: o que resta de áudio na hora e de pedidos no dia.
  O nível local nunca fica ACIMA do que o Groq diz que resta. O áudio por dia não
  vem em cabeçalho nenhum, então esse balde é só a conta própria.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

KEY_COTA = "cota"

AUDIO_HORA = "audio_hora"
AUDIO_DIA = "audio_dia"
PEDIDOS_MINUTO = "pedidos_minuto"
PEDIDOS_DIA = "pedidos_dia"
BALDES = (AUDIO_HORA, AUDIO_DIA, PEDIDOS_MINUTO, PEDIDOS_DIA)


def _limites_padrao() -> Any:
    from shared.ajustes import ajustes

    return ajustes().transcricao.cota


class Cota:
    """Os quatro baldes, com nível persistido. Um objeto por processo."""

    def __init__(
        self,
        *,
        db: Any = None,
        relogio: Callable[[], float] = time.time,
        limites: Callable[[], Any] = _limites_padrao,
    ) -> None:
        self.db = db
        self._relogio = relogio
        self._limites = limites
        self._lock = asyncio.Lock()
        self._niveis: dict[str, float] | None = None
        self._t: float = 0.0

    # ------------------------------------------------------------------
    # Capacidade e nível
    # ------------------------------------------------------------------

    def capacidades(self) -> dict[str, tuple[float, float]]:
        """balde -> (capacidade, janela em segundos). A folga sai dos de áudio."""
        c = self._limites()
        folga = float(getattr(c, "folga_s", 0) or 0)
        return {
            AUDIO_HORA: (max(0.0, float(c.audio_s_por_hora) - folga), 3600.0),
            AUDIO_DIA: (max(0.0, float(c.audio_s_por_dia) - folga), 86_400.0),
            PEDIDOS_MINUTO: (float(c.pedidos_por_minuto), 60.0),
            PEDIDOS_DIA: (float(c.pedidos_por_dia), 86_400.0),
        }

    async def _carregar(self) -> None:
        if self._niveis is not None:
            return
        caps = self.capacidades()
        niveis = {b: caps[b][0] for b in BALDES}
        t = self._relogio()
        if self.db is not None:
            try:
                from core.transcricao.config import PREF_CATEGORY, PREF_SERVICE

                prefs = await self.db.get_preferences(
                    user_id=None, service=PREF_SERVICE, category=PREF_CATEGORY
                )
                bruto = next((p.value for p in prefs or [] if getattr(p, "key", None) == KEY_COTA), None)
                if bruto:
                    salvo = json.loads(bruto)
                    t = float(salvo.get("t") or t)
                    for b in BALDES:
                        if b in (salvo.get("niveis") or {}):
                            niveis[b] = float(salvo["niveis"][b])
            except Exception as e:  # noqa: BLE001 — sem o salvo, começa cheio
                logger.warning("[transcricao] não li a cota salva: %s", e)
        self._niveis = niveis
        self._t = t

    def _encher(self) -> None:
        """Enche cada balde pelo tempo passado, até a capacidade (que pode ter mudado)."""
        assert self._niveis is not None
        agora = self._relogio()
        dt = max(0.0, agora - self._t)
        for b, (cap, janela) in self.capacidades().items():
            taxa = cap / janela if janela > 0 else 0.0
            self._niveis[b] = min(cap, self._niveis[b] + taxa * dt)
        self._t = agora

    async def _salvar(self) -> None:
        if self.db is None or self._niveis is None:
            return
        from core.transcricao.config import PREF_CATEGORY, PREF_SERVICE

        try:
            await self.db.upsert_preference(
                category=PREF_CATEGORY,
                key=KEY_COTA,
                value=json.dumps({"t": self._t, "niveis": {b: round(v, 3) for b, v in self._niveis.items()}}),
                user_id=None,
                service=PREF_SERVICE,
                source_message="neo_transcrever",
            )
        except Exception as e:  # noqa: BLE001 — a conta em memória segue valendo
            logger.warning("[transcricao] não gravei a cota: %s", e)

    @staticmethod
    def _custos(audio_s: float, pedidos: int) -> dict[str, float]:
        audio = float(math.ceil(max(0.0, audio_s)))
        return {AUDIO_HORA: audio, AUDIO_DIA: audio, PEDIDOS_MINUTO: float(pedidos), PEDIDOS_DIA: float(pedidos)}

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    async def espera(self, audio_s: float, pedidos: int = 1) -> tuple[float, str]:
        """Segundos até caber ``audio_s`` de áudio e ``pedidos`` pedidos, e o balde que manda.

        ``math.inf`` quando o pedido é maior que a capacidade do balde: nunca
        cabe, por mais que se espere.
        """
        async with self._lock:
            await self._carregar()
            self._encher()
            assert self._niveis is not None
            pior, quem = 0.0, ""
            for b, custo in self._custos(audio_s, pedidos).items():
                cap, janela = self.capacidades()[b]
                if custo <= 0:
                    continue
                if custo > cap:
                    return math.inf, b
                falta = custo - self._niveis[b]
                if falta > 0:
                    w = falta / (cap / janela)
                    if w > pior:
                        pior, quem = w, b
            return pior, quem

    async def gastar(self, audio_s: float, pedidos: int = 1) -> None:
        async with self._lock:
            await self._carregar()
            self._encher()
            assert self._niveis is not None
            for b, custo in self._custos(audio_s, pedidos).items():
                self._niveis[b] -= custo
            await self._salvar()

    async def devolver(self, audio_s: float, pedidos: int = 1) -> None:
        """O pedido não chegou a contar no Groq (recusado antes de transcrever)."""
        async with self._lock:
            await self._carregar()
            self._encher()
            assert self._niveis is not None
            caps = self.capacidades()
            for b, custo in self._custos(audio_s, pedidos).items():
                self._niveis[b] = min(caps[b][0], self._niveis[b] + custo)
            await self._salvar()

    async def sincronizar(self, cabecalhos: dict[str, str]) -> None:
        """Puxa o nível para baixo quando o Groq diz que resta menos do que a conta daqui."""
        c = self._limites()
        folga = float(getattr(c, "folga_s", 0) or 0)
        lidos: dict[str, float] = {}
        for balde, nome, desconto in (
            (AUDIO_HORA, "x-ratelimit-remaining-audio-seconds", folga),
            (PEDIDOS_DIA, "x-ratelimit-remaining-requests", 0.0),
        ):
            bruto = (cabecalhos or {}).get(nome)
            try:
                lidos[balde] = float(bruto) - desconto
            except (TypeError, ValueError):
                continue
        if not lidos:
            return
        async with self._lock:
            await self._carregar()
            self._encher()
            assert self._niveis is not None
            mudou = False
            for b, valor in lidos.items():
                if valor < self._niveis[b]:
                    self._niveis[b] = valor
                    mudou = True
            if mudou:
                await self._salvar()

    async def estado(self) -> dict[str, dict[str, float]]:
        """O que resta em cada balde agora (para o painel e para a resposta da tool)."""
        async with self._lock:
            await self._carregar()
            self._encher()
            assert self._niveis is not None
            return {
                b: {"restante": max(0.0, round(self._niveis[b], 1)), "limite": cap, "janela_s": janela}
                for b, (cap, janela) in self.capacidades().items()
            }


#: A conta do processo. A rota do painel e a tool leem a mesma.
_cota: Cota | None = None


def cota_do_processo(db: Any = None) -> Cota:
    global _cota
    if _cota is None:
        _cota = Cota(db=db)
    elif db is not None and _cota.db is None:
        _cota.db = db
    return _cota
