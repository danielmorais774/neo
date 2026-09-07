"""Tools do Mercado Livre — server-side (docs/product-search-plan.md §2).

Duas tools de propósito, em vez de uma com modo: `meli_search` descobre o
produto, `meli_product` abre. O modelo escolhe melhor entre dois nomes claros
do que entre dois valores de um parâmetro.

As descrições dizem em voz alta que NÃO existe preço aqui. Sem isso o modelo
chama a tool, recebe um objeto rico sem `price` e completa o buraco de memória
— foi exatamente assim que a sessão f2b06898 inventou "geralmente abaixo de
R$ 100".
"""

from shared import ToolParameter, ToolSchema

MELI_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="meli_search",
        description=(
            "Busca no CATÁLOGO do Mercado Livre pela API oficial. Devolve os "
            "produtos canônicos (id + nome), que é o único jeito de alcançar o ML "
            "— o site bloqueia web_fetch e o browser. Use para descobrir QUAL é o "
            "produto; depois chame `meli_product` para abrir. "
            "ATENÇÃO: não devolve preço (o ML não libera preço para este app)."
        ),
        module="meli",
        execution="server-side",
        timeout=25,
        parameters={
            "query": ToolParameter(
                type="string",
                description="O produto em poucas palavras, ex: 'cortina blackout pvc 2 metros'",
                required=True,
            ),
            "limit": ToolParameter(
                type="integer",
                description="Quantos produtos (default 8, máx 20)",
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="meli_product",
        description=(
            "Abre um produto do catálogo do Mercado Livre e devolve os dados "
            "REAIS: fotos (URLs, use com [[IMAGE:…]]), atributos (marca, medidas, "
            "material) e as VARIAÇÕES disponíveis — cores e tamanhos de verdade, "
            "nunca deduza essa lista. Aceita o id (MLB32482594) ou uma URL com "
            "'/p/MLB…'. "
            "ATENÇÃO: não devolve preço — o ML responde 403 em anúncio. Para preço, "
            "use uma loja que abra (Amazon via web_fetch) ou diga que não obteve."
        ),
        module="meli",
        execution="server-side",
        timeout=25,
        parameters={
            "product": ToolParameter(
                type="string",
                description=(
                    "Id do produto de catálogo ('MLB32482594') ou a URL do ML. "
                    "Link de ANÚNCIO ('MLB-4081940750') não funciona — o ML bloqueia; "
                    "nesse caso busque o produto por nome com meli_search."
                ),
                required=True,
            ),
        },
    ),
]

MELI_TOOL_NAMES = frozenset(t.name for t in MELI_TOOLS)
