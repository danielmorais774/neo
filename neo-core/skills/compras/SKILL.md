---
name: compras
description: >
  Buscar PRODUTO e comparar preço ("quanto custa", "é barato?", frete, oferta).
  Obriga a ABRIR o anúncio antes de citar preço — nunca por snippet de busca.
  Não serve para achar arquivo.
# ATENÇÃO ao mexer aqui: 1 keyword basta pra autocarregar a skill, e ela gruda
# por 5 turnos (_SKILL_STICKY_TURNS). Verbo genérico de busca NÃO entra — ver
# "Por que as keywords são estreitas" no fim deste arquivo.
keywords: [produto, produtos, comprar, compra, compras, preco, preço, precos, preços, quanto custa, quanto ta, quanto tá, custa, onde compro, onde comprar, barato, barata, baratas, baratos, oferta, ofertas, promocao, promoção, promocoes, promoções, desconto, anuncio, anúncio, anuncios, anúncios, loja, lojas, mercado livre, mercadolivre, shopee, amazon, magalu, magazine luiza, aliexpress, frete, parcela, parcelado, vale a pena]
kind: tool_backed
register: oficina
tools: [web_search, web_fetch, meli_search, meli_product, browser_task]
enabled: true
version: 1
---

# Busca de produto e comparação de preço

## Quando usar
- "quanto custa X", "pesquisa uma cortina barata", "onde compro isso".
- "tem oferta de X?", "qual o frete", "é barato?", "vale a pena?".
- NÃO use para cotação de ação/moeda nem para notícia sobre uma marca.
- NÃO use para procurar ARQUIVO (disco, Drive, anexo) — isso é `run_command`
  ou `browser_local_task`, e não tem nada a ver com comprar.

## A regra que existe por causa de um erro real
Numa sessão de produção o Neo respondeu preço de cortina lendo SÓ o snippet do
Google, disse "geralmente abaixo de R$ 100" sem nunca abrir anúncio nenhum, e
depois **inventou uma lista de cores** de um anúncio da Shopee que ele não leu.
Nada disso era verdade.

**Snippet de busca não é preço.** `web_search` serve para DESCOBRIR candidatos.
O preço, a variação (cor/tamanho), o frete e a foto só valem se saírem de uma
página que você ABRIU nesta conversa.

## Como fazer
1. **Descobrir** — `web_search` com o produto + a restrição do usuário
   (tamanho, marca, teto de preço). 1–2 buscas bastam; mais que isso é sinal de
   que a consulta está ruim, não de que falta resultado.
2. **Abrir os candidatos** — no Mercado Livre use `meli_search` + `meli_product`
   (web_fetch não passa lá). Nas outras lojas, `web_fetch` na URL de **PRODUTO**,
   não na de listagem/busca. Alvo: 3–5 páginas.
   - URL de listagem (`/busca`, `/s?k=`, `lista.…`, `/c/…`) devolve texto inútil.
     A do produto tem id no caminho: Amazon `/dp/XXXX`, Shopee `…-i.<loja>.<item>`.
3. **Extrair de cada página aberta**: preço à vista, preço de lista (se houver),
   parcelamento, frete, vendedor/nota, medidas, e a URL da FOTO principal.
4. **Responder** com 3–5 opções ordenadas pelo preço real (produto + frete),
   dizendo de cada uma por que está ali ("mais barata com frete", "melhor
   avaliada"). Mostre a foto do topo com `[[IMAGE:<url da foto>]]` — a URL tem
   que ter aparecido num resultado de tool, nunca deduzida.

## Mercado Livre: use as tools `meli_*`, nunca web_fetch
O site do ML bloqueia `web_fetch` e o browser. A porta é a API oficial:
1. `meli_search(query)` → produtos do catálogo (id + nome).
2. `meli_product(product_id)` → **fotos** (URLs prontas pra `[[IMAGE:…]]`),
   atributos (marca, medidas, material) e **`variations`** — a lista real de
   cores/tamanhos. É esta a resposta certa para "quais as cores?".

**As tools `meli_*` NÃO devolvem preço** (`price: null`). O ML não libera preço
para este app. Então: nunca escreva um preço do ML. Ou você pega o preço numa
loja que abre, ou diz que não conseguiu o preço do ML. Um valor "plausível" aqui
é mentira.

Link de ANÚNCIO (`MLB-4081940750`) não abre nem pela API — busque o produto pelo
nome com `meli_search`.

## O que está bloqueado hoje (medido em 2026-08-19)
`web_fetch` **não** abre Shopee, Magalu nem as VTEX (Casas Bahia, Leroy, Fast
Shop, Centauro, Carrefour): 403 ou muro de verificação. **Funcionam:** Amazon BR
(`/dp/…`) e lojas próprias/Shopify — e o ML só pelas `meli_*`.

- Para PREÇO verificado, a Amazon é hoje a fonte que abre: `web_fetch` numa URL
  `/dp/…` devolve o valor real.
- Se o usuário pedir explicitamente uma loja bloqueada, tente `browser_task`
  (browser real, roda em segundo plano e te avisa depois; diga isso a ele).
  Shopee costuma barrar o browser também.
- Quando não deu para abrir, **diga a verdade**: "esse preço é do resultado de
  busca, não abri o anúncio" ou "a Shopee bloqueou". Nunca preencha o buraco
  com um valor plausível.

## Regras
- Nunca cite preço, cor, medida ou disponibilidade que você não leu numa página
  aberta neste turno ou num anterior desta conversa.
- Nunca invente lista de variação ("normalmente tem branco, bege, cinza"). Se
  não abriu o anúncio, a resposta é que não sabe — e o que você vai fazer.
- Link só depois de conferir que ele abre e é do produto certo.
- Diferencie preço à vista/Pix de parcelado; frete muda o vencedor.

## Por que as keywords são estreitas
Elas já foram largas — tinham `achar`, `acha`, `procura`, `procurar`, `link`,
`modelo`, `marca`, `cor`, `cores`, `tamanho`, `medida`, `variação`. Em produção
(sessões `c35e576f` e `1e7c5943`) o pedido **"procura por um app-release"**, que
era um APK no Google Drive, casou `procura` e ligou esta skill. Como uma keyword
basta para autocarregar e a skill fica ativa por mais 5 turnos mesmo sem casar
nada, ela dominou seis turnos seguidos de uma conversa que não tinha nada de
compra: o Neo saiu procurando o arquivo com `mdfind` no disco local e listando
`~/Downloads` em vez de abrir o Drive, e o usuário teve que corrigir o rumo duas
vezes ("era no drive", "era para baixa esse arquivo do drive").

**Regra:** keyword aqui é palavra de COMPRA (preço, loja, frete, oferta,
anúncio), nunca verbo genérico de busca. Um pedido de produto sem nenhuma
palavra de compra ("acha uma furadeira") deixa de autocarregar e volta ao
caminho normal — catálogo + `skill_read`, que continua funcionando. Perder uma
ativação automática custa um round; ligar a skill errada custa a conversa
inteira.
