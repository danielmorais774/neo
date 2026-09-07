"""Cliente da API oficial do Mercado Livre (app `client_credentials`).

Por que este módulo existe: medido em 2026-08-19, o ML é o único grande varejo
BR que o neo-core não alcança de jeito nenhum sem app oficial — a API sem token
dá 403, o HTML redireciona pra ``/gz/account-verification`` (inclusive de IP
residencial) e o browser real do ``neo-browser-agent`` cai em loop de reCAPTCHA.

**O que este token NÃO dá: PREÇO.** Medido endpoint a endpoint com o app do
Daniel (docs/product-search-plan.md §2):

| Endpoint                          | client_credentials |
|-----------------------------------|--------------------|
| ``/oauth/token``                  | ✅ 200 (6h, scope read) |
| ``/products/search``              | ✅ 200 |
| ``/products/{id}``                | ✅ 200 — fotos, atributos, **pickers (cor/tamanho)** |
| ``/highlights/MLB/category/{c}``  | ✅ 200 |
| ``/sites/MLB/categories``         | ✅ 200 |
| ``/sites/MLB/search``             | ❌ 403 forbidden |
| ``/items/{id}`` e ``/items?ids=`` | ❌ 403 access_denied (**preço mora aqui**) |

Ou seja: dá pra dizer com certeza QUAL é o produto, QUAIS cores/tamanhos existem
e MOSTRAR a foto — que é exatamente o que o Neo inventou na sessão f2b06898 —
mas o preço tem que vir de outra fonte (Amazon via ``web_fetch``) ou ficar
declarado como desconhecido. Nunca deduzir.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.mercadolibre.com"
_SITE = "MLB"  # Brasil
_TIMEOUT = 20.0

# `/p/MLB32482594` (catálogo, 8 dígitos) vs `MLB-4081940750` (anúncio, 9+).
# A distinção importa: catálogo responde 200, anúncio responde 403 — e o erro
# precisa dizer isso, senão o modelo tenta de novo achando que foi falha de rede.
_CATALOG_ID_RE = re.compile(r"/p/(MLB\d{6,8})\b", re.I)
_ITEM_ID_RE = re.compile(r"\bMLB-?(\d{9,})\b", re.I)
_BARE_ID_RE = re.compile(r"^\s*(MLB)-?(\d{6,})\s*$", re.I)


class MeliError(RuntimeError):
    """Falha de credencial/permissão — carrega texto pronto pro modelo ler."""


@dataclass
class _Token:
    value: str = ""
    expires_at: float = 0.0

    def valid(self) -> bool:
        # 60 s de folga: token que vence no meio do voo vira 401 confuso.
        return bool(self.value) and time.time() < (self.expires_at - 60)


@dataclass
class MeliClient:
    app_id: str
    client_secret: str
    _token: _Token = field(default_factory=_Token)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.client_secret)

    async def _access_token(self) -> str:
        """Token de app, cacheado. O ML devolve 6h de validade."""
        if self._token.valid():
            return self._token.value
        async with self._lock:
            if self._token.valid():  # outra corrotina renovou enquanto esperava
                return self._token.value
            if not self.configured:
                raise MeliError(
                    "Mercado Livre não configurado (ML_APP_ID/ML_CLIENT_SECRET "
                    "ausentes no .env da raiz)."
                )
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.post(
                    f"{_API}/oauth/token",
                    headers={
                        "accept": "application/json",
                        "content-type": "application/x-www-form-urlencoded",
                    },
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.app_id,
                        "client_secret": self.client_secret,
                    },
                )
            if r.status_code != 200:
                raise MeliError(
                    f"Mercado Livre recusou as credenciais (HTTP {r.status_code}). "
                    "Confira ML_APP_ID/ML_CLIENT_SECRET."
                )
            data = r.json()
            self._token = _Token(
                value=str(data.get("access_token") or ""),
                expires_at=time.time() + float(data.get("expires_in") or 0),
            )
            logger.info("[meli] token renovado (expira em %ss)", data.get("expires_in"))
            return self._token.value

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{_API}{path}",
                params=params or {},
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code == 403:
            raise MeliError(
                f"O Mercado Livre bloqueia {path} para este app (403). "
                "Preço de anúncio não está liberado — use outra fonte."
            )
        if r.status_code == 404:
            raise MeliError(f"Não achei isso no Mercado Livre ({path}).")
        if r.status_code != 200:
            raise MeliError(f"Mercado Livre respondeu HTTP {r.status_code} em {path}.")
        return r.json()

    # ── API pública do módulo ────────────────────────────────────────────

    @staticmethod
    def parse_reference(raw: str) -> tuple[str, str]:
        """Descobre o que o usuário/modelo passou: ``("catalog"|"item", id)``.

        Anúncio (``MLB-4081940750``) é reconhecido de propósito, mesmo sabendo
        que vai dar 403: o erro específico ("é anúncio, não catálogo") é o que
        faz o modelo mudar de fonte em vez de repetir a chamada.
        """
        s = (raw or "").strip()
        m = _CATALOG_ID_RE.search(s)
        if m:
            return "catalog", m.group(1).upper()
        m = _ITEM_ID_RE.search(s)
        if m:
            return "item", "MLB" + m.group(1)
        m = _BARE_ID_RE.match(s)
        if m:
            digits = m.group(2)
            return ("item" if len(digits) >= 9 else "catalog"), "MLB" + digits
        raise MeliError(
            "Não reconheci um id/link do Mercado Livre nisso. Esperado algo como "
            "'MLB32482594' ou uma URL com '/p/MLB…'."
        )

    async def search_catalog(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Busca no CATÁLOGO (produto canônico), que é o que o token abre."""
        data = await self._get(
            "/products/search",
            {"status": "active", "site_id": _SITE, "q": query, "limit": max(1, min(limit, 20))},
        )
        out: list[dict[str, Any]] = []
        for p in data.get("results") or []:
            out.append(
                {
                    "product_id": p.get("id"),
                    "name": p.get("name"),
                    "family_name": p.get("family_name"),
                    "domain": p.get("domain_id"),
                }
            )
        return out

    async def product(self, product_id: str) -> dict[str, Any]:
        """Detalhe do produto de catálogo: fotos, atributos e variações."""
        p = await self._get(f"/products/{product_id}")
        attrs = {
            a.get("id"): a.get("value_name")
            for a in (p.get("attributes") or [])
            if a.get("value_name")
        }
        # `pickers` é a lista REAL de variações (cor, tamanho) — o dado que o Neo
        # alucinou na sessão f2b06898 ("branco, bege, cinza, marrom, preto").
        variations: dict[str, list[str]] = {}
        for pk in p.get("pickers") or []:
            labels = [
                str(x.get("picker_label"))
                for x in (pk.get("products") or [])
                if x.get("picker_label")
            ]
            if labels:
                variations[str(pk.get("picker_id") or "?")] = labels
        photos = [
            str(pic.get("secure_url") or pic.get("url"))
            for pic in (p.get("pictures") or [])
            if (pic.get("secure_url") or pic.get("url"))
        ]
        return {
            "product_id": p.get("id"),
            "name": p.get("name"),
            "family_name": p.get("family_name"),
            "photos": photos,
            "variations": variations,
            "attributes": attrs,
            # Sempre presente e sempre None: sem este campo explícito o modelo
            # preenche o silêncio com um valor plausível.
            "price": None,
            "price_note": (
                "O Mercado Livre não libera preço para este app (403 em /items). "
                "NÃO invente valor: ou busque o preço noutra loja que abra "
                "(Amazon via web_fetch), ou diga que não conseguiu o preço do ML."
            ),
        }
