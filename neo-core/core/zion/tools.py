"""As três tools do Neo para ler o Zion (docs/zion-integracao-plan.md §7.2).

O Zion é a memória de TRABALHO do Daniel: os projetos (com a pasta de cada um
no Mac), e o que os agentes dele (Claude Code, Codex) aprenderam e deixaram
pendente em cada um. O PKS continua sendo a memória PESSOAL. As descriptions
dizem isso, porque é o modelo que escolhe entre as duas.

Só leitura, por construção: o neo-core fala com o ``neo-zion``, que roda com
``--read-only`` e a pasta montada ``:ro``. Este arquivo nem conhece tool de
escrita do Zion.

Liga/desliga em ``[zion] consulta_habilitada`` (``config/neo.toml``), lido a
cada turno. Desligada, as tools somem do cérebro e do roteador
(:func:`visivel`) e o executor recusa.

Registro nos DOIS lugares (gotcha do cofre e do e-mail): registry + executor no
``api/main.py`` e a entrada no ``cognitive_tools_for``. O executor fala os dois
contratos (``execute`` e ``execute_tool_call``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from core.zion.cliente import ClienteZion, ZionIndisponivel, ZionRecusou
from shared import ToolCall, ToolParameter, ToolResult, ToolSchema

logger = logging.getLogger(__name__)

_FRONTEIRA = (
    "O Zion é TRABALHO (projetos, decisões, falhas, planos); a vida pessoal do Daniel "
    "fica no PKS (search_pks)."
)

ZION_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="zion_overview",
        description=(
            "Abre o Zion, a memória de TRABALHO do Daniel, onde os agentes dele (Claude "
            "Code, Codex) guardam o que sabem de cada projeto. SEM `domain`: a lista de "
            "projetos (domains), com a pasta de cada um no Mac dele e o remote do git — "
            "para 'quais projetos eu tenho', 'onde fica o projeto X'. COM `domain` (o slug "
            "que a lista devolve): o resumo daquele projeto — regras, falhas abertas, "
            "planos e em que fase estão, sessões para retomar, memórias recentes. "
            + _FRONTEIRA
            + " Só leitura."
        ),
        router_summary=(
            "Memória de trabalho do Daniel (Zion): projetos, pasta de cada um no Mac, "
            "regras, falhas, planos"
        ),
        module="zion",
        execution="server-side",
        timeout=30,
        parameters={
            "domain": ToolParameter(
                type="string",
                description=(
                    "Slug do projeto, como a lista devolve (ex.: 'neo'). Omita para "
                    "listar os projetos."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="zion_search",
        description=(
            "Busca no Zion, a memória de TRABALHO do Daniel: memórias (decisões, "
            "armadilhas, regras, procedimentos, referências, estado), planos, tarefas, "
            "falhas e sessões dos agentes. Para 'o que decidimos sobre Y', 'teve falha "
            "com Z', 'qual é a regra de W', 'em que pé está o plano P'. O resultado traz "
            "`confidence`; se não for 'high', tente outras palavras ou abra um id com "
            "zion_get. Os campos `next`/`omitted` citam as tools do próprio Zion "
            "(memory_search, record_get, record_related): aqui elas são zion_search e "
            "zion_get. " + _FRONTEIRA
        ),
        router_summary="Busca decisões, regras, falhas e planos dos projetos do Daniel no Zion",
        module="zion",
        execution="server-side",
        timeout=30,
        parameters={
            "query": ToolParameter(
                type="string",
                description="O que procurar, em palavras-chave.",
                required=True,
            ),
            "domain": ToolParameter(
                type="string",
                description="Slug do projeto, para buscar só nele (opcional).",
                required=False,
            ),
            "kinds": ToolParameter(
                type="array",
                description=(
                    "Filtro de tipo (opcional): memory, plan, task, failure, session, ou "
                    "um tipo de memória (decision, gotcha, rule, procedure, reference, state)."
                ),
                required=False,
                items={"type": "string"},
            ),
        },
    ),
    ToolSchema(
        name="zion_get",
        description=(
            "Abre um registro do Zion pelo id que apareceu numa busca ou num resumo "
            "(mem_…, pln_…, fai_…, tsk_…, ses_…), ou um projeto pelo slug. Volta "
            "compacto: o corpo de um plano escrito num .md do repo fica no arquivo, e o "
            "campo `omitted` diz o que foi cortado. Só leitura."
        ),
        router_summary="Abre um registro do Zion pelo id (mem_, pln_, fai_, tsk_, ses_)",
        module="zion",
        execution="server-side",
        timeout=30,
        parameters={
            "id": ToolParameter(
                type="string",
                description="O id exato, como apareceu num resultado do Zion. Nunca invente.",
                required=True,
            ),
        },
    ),
]

ZION_TOOL_NAMES = frozenset(t.name for t in ZION_TOOLS)


def consulta_ligada() -> bool:
    """``[zion] consulta_habilitada`` do neo.toml. Sem ajustes legíveis, desligada."""
    try:
        from shared.ajustes import ajustes

        return bool(ajustes().zion.consulta_habilitada)
    except Exception:  # noqa: BLE001 — na dúvida, a memória dele fica fechada
        return False


def visivel(nome: str) -> bool:
    """Esta tool pode aparecer neste turno? (as do Zion só com a consulta ligada)"""
    return nome not in ZION_TOOL_NAMES or consulta_ligada()


def _texto(valor: Any) -> str:
    return str(valor or "").strip()


def traduzir(nome: str, argumentos: dict[str, Any] | None, *, limite_busca: int) -> tuple[str, dict[str, Any]]:
    """(tool do Zion, argumentos) para uma tool do Neo. Levanta ``ValueError`` com a correção."""
    args = argumentos or {}
    if nome == "zion_overview":
        dominio = _texto(args.get("domain"))
        return ("context_get", {"domain": dominio}) if dominio else ("domain_list", {})
    if nome == "zion_search":
        query = _texto(args.get("query"))
        if not query:
            raise ValueError("diga o que procurar em `query`.")
        saida: dict[str, Any] = {"query": query, "limit": limite_busca}
        dominio = _texto(args.get("domain"))
        if dominio:
            saida["domain"] = dominio
        kinds = args.get("kinds")
        if isinstance(kinds, str):
            kinds = [k for k in kinds.replace(";", ",").split(",")]
        if isinstance(kinds, list):
            limpos = [_texto(k) for k in kinds if _texto(k)]
            if limpos:
                saida["kinds"] = limpos
        return "memory_search", saida
    if nome == "zion_get":
        ident = _texto(args.get("id"))
        if not ident:
            raise ValueError("passe em `id` um id que apareceu num resultado do Zion.")
        return "record_get", {"id": ident}
    raise ValueError(f"tool desconhecida: {nome}")


def caber(valor: Any, max_chars: int) -> dict[str, Any]:
    """O resultado do Zion como resultado de tool, dentro do teto de caracteres.

    Erro do próprio Zion (``{"error": código, "message": …}``, ex. id que não
    existe) vira ``ok: false`` com a mensagem dele, que já diz o que fazer.
    """
    if isinstance(valor, dict) and isinstance(valor.get("error"), str) and "message" in valor:
        return {"ok": False, "error": f"{valor['error']}: {valor.get('message')}"[:600]}
    texto = json.dumps(valor, ensure_ascii=False)
    if len(texto) <= max_chars:
        return {"ok": True, "zion": valor}
    return {
        "ok": True,
        "zion_cortado": texto[:max_chars],
        "aviso": (
            f"resultado cortado em {max_chars} de {len(texto)} caracteres. Peça algo mais "
            "específico (domain, kinds) ou abra um id com zion_get."
        ),
    }


class ZionToolExecutor:
    """Executa ``zion_overview``/``zion_search``/``zion_get`` contra o ``neo-zion``.

    O cliente é refeito quando a URL ou o prazo mudam no neo.toml. Nunca levanta:
    Zion fora do ar vira ``{"ok": false, "error": "a memória do Zion não respondeu: …"}``.
    """

    def __init__(self, *, transport: Any = None) -> None:
        self._transport = transport
        self._cliente: ClienteZion | None = None

    def _cliente_para(self, url: str, timeout_s: float) -> ClienteZion:
        atual = self._cliente
        if atual is None or atual.url != url or atual.timeout_s != timeout_s:
            atual = ClienteZion(url, timeout_s=timeout_s, transport=self._transport)
            self._cliente = atual
        return atual

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from shared.ajustes import ajustes

        cfg = ajustes().zion
        if not cfg.consulta_habilitada:
            return {
                "ok": False,
                "error": "a consulta ao Zion está desligada ([zion] consulta_habilitada no neo.toml).",
            }
        try:
            tool_zion, args = traduzir(tool_name, arguments, limite_busca=cfg.limite_busca)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            valor = await self._cliente_para(cfg.url, cfg.timeout_s).chamar(tool_zion, args)
        except ZionIndisponivel as exc:
            logger.warning("[zion] %s → %s: %s", tool_name, tool_zion, exc)
            return {"ok": False, "error": f"a memória do Zion não respondeu: {exc}"[:500]}
        except ZionRecusou as exc:
            return {"ok": False, "error": f"o Zion recusou a chamada: {exc}"[:500]}
        except Exception as exc:  # noqa: BLE001 — nunca derruba o turno
            logger.exception("[zion] %s falhou", tool_name)
            return {"ok": False, "error": f"falha ao consultar o Zion: {exc}"[:400]}
        return caber(valor, cfg.max_chars)

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        result = await self.execute(tool_call.name, tool_call.arguments or {})
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            result=result,
            error=None if result.get("ok") else str(result.get("error") or "")[:500],
            execution_time=asyncio.get_event_loop().time() - start,
        )
