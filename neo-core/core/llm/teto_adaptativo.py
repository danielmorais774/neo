"""Teto de saída que acompanha o raciocínio real do modelo.

**Por que existe.** O ``z-ai/glm-5.3-flash`` não aceita desligar o raciocínio
("reasoning is mandatory"), então o ``disable_thinking`` das chamadas de saída
curta vira no-op (ver ``OpenRouterProvider._apply_reasoning_pref``). No job
9c9b5bae (08/11) o passo do navegador tinha ``max_tokens`` fixo em 2000 e o
raciocínio passou disso em 6 de 30 passos: 1984 a 2000 tokens pensando, o JSON
cortado nos primeiros 60 caracteres. Nos passos que fecharam, o raciocínio foi
de 29 a 1566. Um número fixo ou corta a cauda, ou fica largo demais para todo
mundo.

**Três peças.**

1. O teto é ``reserva da resposta + orçamento de raciocínio``. A resposta é
   previsível (o maior JSON medido teve 356 tokens); o raciocínio não.
2. O orçamento acompanha o raciocínio medido com o cálculo do timeout do TCP
   (RFC 6298): média móvel + ``desvios`` × desvio móvel, por modelo. Sobe sozinho
   depois de um passo pesado, desce nos leves e reaprende quando o modelo muda.
3. Corte por raciocínio — o raciocínio invadiu a reserva da resposta — repete a
   MESMA chamada com o teto maior (``escada``), antes de virar recusa.

**Teto não é gasto.** O provedor cobra o que o modelo gera. Subir o teto só
custa no passo que de fato raciocina mais — e esse é justamente o que, cortado,
era pago inteiro sem entregar nada (28% do custo do job 9c9b5bae).

O que se aprende fica na memória do processo: um reload do neo-core volta ao
piso, e a escada cobre o recomeço. Os números ficam em ``config/neo.toml``
(seções ``[teto_adaptativo]`` e uma por uso, como ``[navegador.decisao]``).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any

from shared.ajustes import TetoDeUso, ajustes


@dataclass(frozen=True)
class Leitura:
    """O que a resposta do provedor diz sobre o orçamento.

    ``None`` em qualquer campo é "o provedor não disse", nunca "não houve".
    """

    cortado: bool | None
    raciocinio: int | None


def ler_resposta(response: Any) -> Leitura:
    """Lê ``finish_reason`` e tokens de raciocínio sem confiar que existam.

    ``finish_reason`` é o sinal de VERDADE do corte, mas nem todo provedor expõe
    o campo, e em alguns ele é atributo em vez de método. Dublê de teste também
    não tem nenhum dos dois. Ausência vira ``None``.
    """

    def _campo(nome: str) -> Any:
        attr = getattr(response, nome, None)
        try:
            return attr() if callable(attr) else attr
        except Exception:  # noqa: BLE001 — provedor exótico não derruba o passo
            return None

    motivo = _campo("finish_reason")
    raciocinio = _campo("reasoning_tokens")
    return Leitura(
        cortado=str(motivo).strip().lower() == "length" if motivo else None,
        raciocinio=raciocinio if isinstance(raciocinio, int) else None,
    )


@dataclass
class _Estado:
    media: float
    desvio: float


class TetoAdaptativo:
    """O teto de um uso (ex.: ``"navegador.decisao"``), aprendido por modelo."""

    def __init__(self, uso: str) -> None:
        self.uso = uso
        self._estados: dict[str, _Estado] = {}

    def _limites(self) -> TetoDeUso:
        return functools.reduce(getattr, self.uso.split("."), ajustes())

    def teto(self, modelo: str) -> int:
        lim = self._limites()
        estado = self._estados.get(modelo)
        if estado is None:
            return lim.piso
        orcamento = estado.media + ajustes().teto_adaptativo.desvios * estado.desvio
        return int(min(lim.maximo, max(lim.piso, round(orcamento) + lim.reserva_resposta)))

    def corte_por_raciocinio(self, leitura: Leitura, orcamento: int) -> bool:
        """O raciocínio invadiu a reserva da resposta e o texto foi cortado.

        É o corte que mais teto resolve. Corte com o raciocínio baixo é o
        contrário — a RESPOSTA saiu longa — e repetir não muda nada.
        """
        if not leitura.cortado or leitura.raciocinio is None:
            return False
        return orcamento - leitura.raciocinio < self._limites().reserva_resposta

    def observar(self, modelo: str, leitura: Leitura, orcamento: int) -> None:
        if leitura.raciocinio is None:
            return
        cfg = ajustes().teto_adaptativo
        medida = float(leitura.raciocinio)
        if self.corte_por_raciocinio(leitura, orcamento):
            # O valor real é desconhecido e MAIOR que o medido.
            medida *= cfg.fator_corte
        estado = self._estados.get(modelo)
        if estado is None:
            self._estados[modelo] = _Estado(media=medida, desvio=medida / 2)
            return
        estado.desvio += cfg.peso_desvio * (abs(estado.media - medida) - estado.desvio)
        estado.media += cfg.peso_media * (medida - estado.media)

    def escada(self, orcamento: int) -> int | None:
        """O teto da repetição de um passo cortado, ou ``None`` se já está no máximo."""
        maximo = self._limites().maximo
        maior = min(maximo, round(orcamento * ajustes().teto_adaptativo.fator_escada))
        return maior if maior > orcamento else None
