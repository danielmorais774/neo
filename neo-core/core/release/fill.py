"""Preenchimento escopado: o placeholder vira valor, e só no domínio liberado.

É a F3b do docs/release-request-plan.md, e o mesmo contrato que
``core/credentials/browser.py`` já usa para senha: **o modelo escreve o
placeholder, o executor substitui na hora de digitar**. A diferença é que aqui a
autorização não é um cadastro — é uma Solicitação de Liberação aprovada por
você, de uso único.

Quatro condições para um valor sair. Falhando qualquer uma, o campo não é
preenchido e o agente recebe a razão em português:

1. existe liberação **aprovada** (não pendente, não usada, não vencida) para
   aquele campo;
2. o domínio da aba **casa** com o destino aprovado (apex e subdomínio; nunca
   "qualquer página");
3. o valor existe no PKS e é **inequívoco** (um campo com esse nome, num dono só);
4. a liberação é **consumida** na substituição — a segunda tentativa não vale.

O que isto compra em relação a "o modelo já tem o valor no contexto": a página
hostil pode pedir o que quiser, e não há valor nenhum para colher. É o "nada além
disso" por ausência, não por recusa.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: ``«pks:cep»`` — a mesma marca que o bloco de memória mostra (core/pks/sensitivity.py).
#: Aceita as aspas angulares e o ASCII, porque o modelo às vezes reescreve.
PLACEHOLDER_RE = re.compile(r"(?:«|<<)\s*pks:\s*([a-z0-9_.\- ]+?)\s*(?:»|>>)", re.IGNORECASE)


@dataclass
class FillOutcome:
    """Resultado de uma substituição."""

    texto: str
    substituidos: list[str] = field(default_factory=list)
    recusados: list[str] = field(default_factory=list)
    motivos: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.recusados

    @property
    def tem_placeholder(self) -> bool:
        return bool(self.substituidos or self.recusados)


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(str(url or "")).hostname or "").lower().strip(".")
    except Exception:  # noqa: BLE001
        return ""


def domain_matches(destino: str, host: str) -> bool:
    """O destino aprovado cobre este host? Apex + subdomínio, nada além.

    ``loja.com.br`` casa ``loja.com.br`` e ``www.loja.com.br``; não casa
    ``loja.com.br.atacante.tld`` (o sufixo tem que ser precedido de ponto) nem
    ``outraloja.com.br``.
    """
    alvo = (destino or "").lower().strip().lstrip("*.").strip(".")
    atual = (host or "").lower().strip().strip(".")
    if not alvo or not atual:
        return False
    return atual == alvo or atual.endswith("." + alvo)


class ReleaseFiller:
    """Traduz ``«pks:campo»`` em valor, com a liberação como autorização."""

    def __init__(self, service: Any, pks: Any) -> None:
        self._service = service
        self._pks = pks

    async def resolve(self, *, user_id: UUID, texto: str, url: str) -> FillOutcome:
        if not texto or "pks:" not in texto.lower():
            return FillOutcome(texto=texto)
        host = host_of(url)
        if not host:
            return FillOutcome(
                texto=texto,
                recusados=["(sem aba)"],
                motivos=["não sei em que página estou — não substituo dado sem domínio"],
            )

        aprovadas = await self._aprovadas(user_id)
        resultado = FillOutcome(texto=texto)

        async def _troca(campo: str) -> str | None:
            pedido = campo.strip().lower()
            candidata = next(
                (
                    r
                    for r in aprovadas
                    if any(_campo_de(c) == pedido for c in (r.campos or []))
                    and domain_matches(r.destino, host)
                ),
                None,
            )
            if candidata is None:
                resultado.recusados.append(campo)
                resultado.motivos.append(
                    f"não existe liberação aprovada de '{campo}' para {host} — "
                    "peça com solicitar_dado e espere o Daniel aprovar"
                )
                return None
            valor, motivo = await self._valor(user_id, pedido)
            if valor is None:
                resultado.recusados.append(campo)
                resultado.motivos.append(motivo or f"não achei '{campo}' na memória")
                return None
            try:
                await self._service.consume(candidata.id)
            except Exception as exc:  # noqa: BLE001 — liberação já usada/expirada
                resultado.recusados.append(campo)
                resultado.motivos.append(f"a liberação de '{campo}' não vale mais: {exc}")
                return None
            resultado.substituidos.append(campo)
            logger.warning(
                "[release][fill] '%s' preenchido em %s pela liberação %s",
                campo, host, candidata.id,
            )
            return valor

        # Substituição sequencial (o regex é async-hostil): resolve um por um.
        saida = texto
        for achado in list(PLACEHOLDER_RE.finditer(texto)):
            campo = achado.group(1)
            valor = await _troca(campo)
            if valor is not None:
                saida = saida.replace(achado.group(0), valor, 1)
        resultado.texto = saida
        return resultado

    # ── internos ─────────────────────────────────────────────────────────

    async def _aprovadas(self, user_id: UUID) -> list[Any]:
        try:
            return await self._service.list_approved(user_id)
        except Exception:  # noqa: BLE001
            logger.exception("[release][fill] falha ao listar liberações aprovadas")
            return []

    async def _valor(self, user_id: UUID, campo: str) -> tuple[Any, str | None]:
        """Valor do campo no PKS, sem redação. Ambíguo → recusa, não chuta."""
        if self._pks is None:
            return None, "não tenho acesso à memória para resolver o valor"
        try:
            rows = await self._pks.search_entities(
                user_id=user_id, query=campo, redact=False
            )
        except Exception:  # noqa: BLE001
            logger.exception("[release][fill] busca no PKS falhou")
            return None, "a memória não respondeu"
        achados: list[tuple[str, Any]] = []
        for row in rows:
            for chave, valor in (row.get("properties") or {}).items():
                if chave.strip().lower() == campo and valor not in (None, ""):
                    achados.append((str(row.get("name") or "?"), valor))
        if not achados:
            return None, f"'{campo}' não está na memória"
        if len({str(v) for _, v in achados}) > 1:
            donos = ", ".join(sorted({n for n, _ in achados}))
            return None, (
                f"'{campo}' existe em mais de um lugar ({donos}) — diga de quem é "
                "antes de preencher"
            )
        return achados[0][1], None


def _campo_de(rotulo: str) -> str:
    """``pks:cep`` → ``cep``."""
    return str(rotulo).split(":", 1)[-1].strip().lower()
