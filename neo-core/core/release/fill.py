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
4. a liberação é **consumida** na substituição — outro job não a usa de novo.

"Uso único" é por TAREFA, não por campo: aprovar CEP e CPF para o formulário e ver
o CPF recusado porque o CEP já gastou a aprovação seria pedir duas vezes a mesma
coisa (sessão 1a605544, 2026-11-01). O filler nasce por job, e as liberações que
ele consumiu valem até o fim dele — para os outros campos da mesma aprovação e no
mesmo domínio, nada além.

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
    #: Recusados SÓ por falta de liberação para este site. É o único motivo que o
    #: Daniel resolve com um clique: o driver para a tarefa e pede
    #: (``NeedsRelease``). "Não está na memória", "ambíguo" e "sem aba" não se
    #: resolvem aprovando nada, e por isso ficam de fora daqui.
    sem_liberacao: list[str] = field(default_factory=list)

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


def campos_do_texto(texto: str) -> list[str]:
    """Os ``«pks:campo»`` de um texto, em ordem e sem repetição.

    É como o job sabe TUDO o que a tarefa vai preencher antes de chegar em cada
    campo — e pede tudo num card só, em vez de um card por campo.
    """
    vistos: list[str] = []
    for achado in PLACEHOLDER_RE.finditer(str(texto or "")):
        campo = achado.group(1).strip().lower()
        if campo and campo not in vistos:
            vistos.append(campo)
    return vistos


#: O nome que o modelo escreve no placeholder nem sempre é a chave do PKS: o
#: formulário pergunta "nome" e a memória guarda `full_name`. Só sinônimos
#: conferidos na memória de produção (2026-11-01) — um chute aqui preencheria
#: o campo com o dado errado.
_SINONIMOS: dict[str, tuple[str, ...]] = {
    "nome": ("full_name", "nome_completo"),
    "nome_completo": ("full_name",),
}


def _cobre(rows: Any, campo: str, host: str) -> Any:
    """A liberação de ``rows`` que cobre este campo neste host, se houver."""
    return next(
        (
            r
            for r in rows
            if any(_campo_de(c) == campo for c in (r.campos or []))
            and domain_matches(r.destino, host)
        ),
        None,
    )


class ReleaseFiller:
    """Traduz ``«pks:campo»`` em valor, com a liberação como autorização."""

    def __init__(self, service: Any, pks: Any) -> None:
        self._service = service
        self._pks = pks
        #: Liberações que ESTE job já consumiu, por id. Ver o docstring do módulo.
        self._em_uso: dict[str, Any] = {}

    async def tem_liberacao(self, *, user_id: UUID, campo: str, url: str) -> bool:
        """Já existe aprovação deste campo para esta aba (ou em uso neste job)?"""
        host = host_of(url)
        if not host:
            return False
        pedido = str(campo or "").strip().lower()
        if _cobre(self._em_uso.values(), pedido, host) is not None:
            return True
        return _cobre(await self._aprovadas(user_id), pedido, host) is not None

    async def disponivel(self, *, user_id: UUID, campo: str) -> str | None:
        """``None`` quando a memória tem o valor, inequívoco; senão, o motivo.

        Serve para não pedir liberação do que não existe: aprovar um campo que a
        memória não tem só adia a recusa para o passo seguinte.
        """
        valor, motivo = await self._valor(user_id, str(campo or "").strip().lower())
        return None if valor is not None else (motivo or f"'{campo}' não está na memória")

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
            candidata = _cobre(self._em_uso.values(), pedido, host)
            ja_consumida = candidata is not None
            if candidata is None:
                candidata = _cobre(aprovadas, pedido, host)
            if candidata is None:
                # O que a memória não tem não se resolve aprovando: sem esta
                # conferência, «pks:endereco» parava a continuação já aprovada
                # pedindo um card que nunca ia nascer, e o nome e o "Enviar"
                # depois dele nunca aconteciam (sessão 9b2e29e2).
                _, faltando = await self._valor(user_id, pedido)
                if faltando:
                    resultado.recusados.append(campo)
                    resultado.motivos.append(
                        f"{faltando} — deixe este campo em branco, siga com os outros e "
                        "diga ao Daniel o que ficou faltando"
                    )
                    return None
                resultado.recusados.append(campo)
                resultado.sem_liberacao.append(pedido)
                resultado.motivos.append(
                    f"não existe liberação aprovada de '{campo}' para {host} — "
                    "a tarefa para aqui e pede ao Daniel"
                )
                return None
            valor, motivo = await self._valor(user_id, pedido)
            if valor is None:
                resultado.recusados.append(campo)
                resultado.motivos.append(motivo or f"não achei '{campo}' na memória")
                return None
            if not ja_consumida:
                try:
                    await self._service.consume(candidata.id)
                except Exception as exc:  # noqa: BLE001 — liberação já usada/expirada
                    resultado.recusados.append(campo)
                    resultado.motivos.append(f"a liberação de '{campo}' não vale mais: {exc}")
                    return None
                self._em_uso[str(candidata.id)] = candidata
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
        # O dono primeiro: o dado pedido é DELE. Pela busca, «pks:nome» nunca
        # chegava em `full_name` (chave genérica não localiza entidade), e
        # «pks:email» empatava com o "E-mail do Neo" — os dois viraram "não está
        # na memória" e o card não nasceu (sessão 9b2e29e2, 2026-11-01).
        do_dono = getattr(self._pks, "primary_user_field", None)
        if do_dono is not None:
            for chave_pks in (campo, *_SINONIMOS.get(campo, ())):
                try:
                    valor = await do_dono(user_id=user_id, key=chave_pks)
                except Exception:  # noqa: BLE001 — cai na busca, como antes
                    logger.exception("[release][fill] leitura do campo do dono falhou")
                    break
                if valor not in (None, ""):
                    return valor, None
        achados: list[tuple[str, Any]] = []
        # A chave pedida primeiro; sinônimo só quando ela não existe — nunca
        # misturados, senão "nome" e `full_name` diferentes virariam ambíguo.
        for chave_pks in (campo, *_SINONIMOS.get(campo, ())):
            try:
                rows = await self._pks.search_entities(
                    user_id=user_id, query=chave_pks, redact=False
                )
            except Exception:  # noqa: BLE001
                logger.exception("[release][fill] busca no PKS falhou")
                return None, "a memória não respondeu"
            for row in rows:
                for chave, valor in (row.get("properties") or {}).items():
                    if chave.strip().lower() == chave_pks and valor not in (None, ""):
                        achados.append((str(row.get("name") or "?"), valor))
            if achados:
                break
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
