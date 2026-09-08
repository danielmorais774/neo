---
name: produtos
description: 'Catálogo de produtos da loja: cadastrar, consultar preço e estoque, atualizar. Use quando falarem de produto, preço, estoque ou pedido de cliente.'
keywords: [produto, produtos, preço, preco, estoque, catálogo, catalogo, caneca, camiseta]
kind: tool_backed
group: cadastros
discovery: keyword
datasets: [produtos]
screens: [{dataset: produtos, titulo: Produtos}]
enabled: true
version: 1
---

# Catálogo de produtos

## Quando usar
- Cadastrar produto novo, consultar preço/estoque, atualizar estoque.

## Como fazer
1. Consultar: `data_list(dataset="produtos", busca="...")`. Para filtro exato use `filtro`.
2. Cadastrar: `data_create(dataset="produtos", campos={...})`. Preço aceita "49,90".
3. Atualizar: `data_update(dataset="produtos", id=..., campos={"estoque": 7})`.
4. Excluir: `data_delete` SEM confirmar primeiro (vem preview), confirme com o usuário, só então `confirmar=true`.

## Regras
- Só afirme que cadastrou/alterou se o retorno trouxer `conferido`.
- Nunca invente preço ou estoque que o usuário não disse — pergunte.
- Para análise (o que mais vendeu, estoque parado) use `sql_query_ro` na tabela do bloco de cadastro acima.
