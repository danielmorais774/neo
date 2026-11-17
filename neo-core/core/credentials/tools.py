"""O cofre visto pelo cérebro — catálogo sem segredo.

Até aqui o cofre tinha UM consumidor: o broker do gateway, resolvendo o
``credential_ref`` que a *conexão* já apontava. Ninguém escolhia nada, e por isso
a ``description`` era um campo morto — não existia o momento de julgar "qual
credencial serve para esta tarefa".

``vault_list`` cria esse momento. O cérebro vê **rótulo, tipo, campos e
descrição** — o suficiente para decidir — e **nunca o segredo**: para usar uma
credencial ele cita o RÓTULO para quem sabe decifrar (o runner do browser, as
tools de email), e o valor viaja por baixo, fora do prompt.

É a mesma divisão do ``sensitive_data`` do browser-use: o modelo manipula um
nome, o executor manipula o valor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from shared import ToolCall, ToolParameter, ToolResult, ToolSchema

logger = logging.getLogger(__name__)

VAULT_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="vault_list",
        description=(
            "Lista as credenciais guardadas no cofre do usuário — rótulo, tipo, "
            "usuário/endereço e a descrição de para que serve. NUNCA devolve a "
            "senha/token (isso é impossível por design). Use ANTES de uma tarefa "
            "que precisa de login ('entra na minha conta do X', 'preciso de um "
            "email') para descobrir se existe credencial e QUAL é a certa; depois "
            "passe o RÓTULO dela para quem vai usar (ex.: browser_task com "
            "credentials=['github-pessoal']). Para `browser_local_task` não precisa: "
            "a tarefa acha sozinha o login do cofre pelo DOMÍNIO do site quando a "
            "aba chega nele. Nunca peça senha no chat."
        ),
        module="vault",
        execution="server-side",
        timeout=15,
        parameters={
            "kind": ToolParameter(
                type="string",
                description=(
                    "Filtra por tipo (opcional): password (login de site), email "
                    "(caixa de email), token, api_key, basic, cookie, cert, other."
                ),
                required=False,
            ),
            "query": ToolParameter(
                type="string",
                description=(
                    "Filtra por texto no rótulo/descrição/campos (opcional). Ex.: "
                    "'github', 'email', 'banco'."
                ),
                required=False,
            ),
        },
    ),
]

VAULT_TOOL_NAMES = frozenset(t.name for t in VAULT_TOOLS)


class VaultToolExecutor:
    """Executa ``vault_list``. Global (o cofre não é por usuário) — registrado
    como module executor "vault" no bootstrap, junto com o store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        start = asyncio.get_event_loop().time()
        args = tool_call.arguments or {}
        try:
            result = await self._list(args)
            ok = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vault] %s falhou: %s", tool_call.name, exc)
            result = {"ok": False, "error": str(exc)[:300]}
            ok = False
        return ToolResult(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            result=result,
            error=None if ok else str(result.get("error") or "")[:300],
            execution_time=asyncio.get_event_loop().time() - start,
        )

    async def _list(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._store is None:
            return {
                "ok": False,
                "error": "o cofre está indisponível (NEO_CRED_KEY não configurada na VPS).",
            }
        kind = str(args.get("kind") or "").strip().lower() or None
        query = str(args.get("query") or "").strip().lower() or None
        infos = [i for i in await self._store.list() if not kind or i.kind == kind]

        def _casa(info: Any) -> bool:
            haystack = " ".join(
                [info.label, info.kind, info.description or "", str(info.fields or "")]
            ).lower()
            return query in haystack

        achadas = [i for i in infos if _casa(i)] if query else infos
        # Filtro sem resultado NÃO é cofre vazio. Na sessão a20f4722 o cérebro
        # procurou "appdemo", a conta do AppDemo está guardada pelo DOMÍNIO
        # (sistema-exemplo.vercel.app), veio `count: 0`, e ele respondeu "o
        # cofre está vazio" e pediu e-mail e senha no chat. Quando o texto não
        # casa, devolve o mesmo que a chamada sem filtro e diz por quê.
        sem_casar = bool(query) and not achadas and bool(infos)
        if sem_casar:
            achadas = infos
        out = [
            {
                "label": info.label,
                "kind": info.kind,
                "description": info.description,
                # Campos NÃO secretos: usuário do login, endereço da caixa, domínio.
                **{k: v for k, v in (info.fields or {}).items()},
            }
            for info in achadas
        ]
        note = (
            "O segredo NUNCA aparece aqui. Para usar uma destas, cite o 'label' "
            "para a tool que executa (browser_task credentials=[...], email_*); "
            "quem decifra é o servidor, no momento do uso. `browser_local_task` "
            "acha sozinha o login pelo DOMÍNIO do site: não precisa citar nada. "
            "Nunca peça e-mail ou senha no chat."
        )
        if sem_casar:
            note = (
                f"Nenhuma credencial tem «{query}» no texto, mas o cofre NÃO está vazio: "
                f"estas são todas as {len(infos)}"
                + (f" do tipo {kind}" if kind else "")
                + ". Conta de site criada pelo Neo leva o DOMÍNIO no rótulo e no campo "
                "`domain`, não o nome do sistema. " + note
            )
        return {
            "ok": True,
            "count": len(out),
            **({"filtro_sem_resultado": query} if sem_casar else {}),
            "credentials": out,
            "note": note,
        }
