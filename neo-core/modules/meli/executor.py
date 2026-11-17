"""Executor das tools do Mercado Livre."""

from __future__ import annotations

import logging
from typing import Any

from modules.meli.client import MeliClient, MeliError

logger = logging.getLogger(__name__)


class MeliExecutor:
    """Roda `meli_search` / `meli_product` contra a API oficial.

    Erro NUNCA vira exceção crua: volta como ``{"ok": False, "error": ...}`` com
    texto que o modelo consegue agir em cima (trocar de fonte), porque o valor
    aqui é justamente não deixar buraco pra ele preencher com invenção.
    """

    def __init__(self, client: MeliClient) -> None:
        self._client = client

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if tool_name == "meli_search":
                query = str(arguments.get("query") or "").strip()
                if not query:
                    return {"ok": False, "error": "query vazia."}
                limit = arguments.get("limit")
                results = await self._client.search_catalog(
                    query, int(limit) if limit else 8
                )
                return {
                    "ok": True,
                    "results": results,
                    "note": (
                        "Sem preço: o ML não libera preço para este app. Para abrir "
                        "um destes e ver fotos/cores, chame meli_product com o product_id."
                    ),
                }

            if tool_name == "meli_product":
                raw = str(arguments.get("product") or "").strip()
                if not raw:
                    return {"ok": False, "error": "product vazio."}
                kind, ident = MeliClient.parse_reference(raw)
                if kind == "item":
                    return {
                        "ok": False,
                        "error": (
                            f"'{ident}' é um ANÚNCIO, e o Mercado Livre bloqueia anúncio "
                            "para este app (403). Busque o produto por nome com "
                            "meli_search e abra o resultado de catálogo."
                        ),
                    }
                data = await self._client.product(ident)
                return {"ok": True, **data}

            return {"ok": False, "error": f"Tool desconhecida: {tool_name}"}

        except MeliError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 — tool nunca derruba o turno
            logger.exception("[meli] %s falhou", tool_name)
            return {"ok": False, "error": f"Falha ao falar com o Mercado Livre: {e}"}
