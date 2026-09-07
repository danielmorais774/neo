# Plano: Agente de Compras Pessoal (comparador multi-loja de um usuário só)

**Data:** 2026-08-08 · **Revisado:** 2026-08-12 (escopo corrigido — ver §13)
**Status:** PLANO — nada implementado
**Depende de:** skills library (`docs/skills-library-plan.md`, em prod),
PKS write-on-demand (`docs/pks-write-on-demand-plan.md`, em prod),
ProactiveDispatcher, JobManager (só para o cron do vigia).

Objetivo: "acha uma furadeira" → o Neo consulta as lojas **onde eu compro**,
aplica o que ele **já sabe de mim** (CEP, voltagem, marcas, forma de pagamento),
calcula o preço real com frete e Pix, e responde com 3–5 opções **mais um
veredito temporal**: comprar agora ou esperar.

---

## 1. Escopo (o que este produto é e o que não é)

| | |
|---|---|
| Usuários | **1** (eu) |
| Volume | poucas buscas por semana; dezenas de vigias ativos no máximo |
| Latência aceitável | ~8 s (era 2 s no plano anterior — deixou de ser critério) |
| Cobertura | **não é** o diferencial. Não quero 40 lojas, quero as 5 onde compro |
| Diferencial real | o agente **já me conhece**: não pergunta CEP, não mostra 220V, não sugere marca que eu recusei, e sabe se R$ 420 é um preço bom |

Essa linha muda quase todas as decisões técnicas. Um comparador multi-usuário
paga caro por cobertura, latência e desambiguação em escala. Um agente pessoal
paga por **contexto**. Todo componente abaixo que existia só para servir escala
foi cortado (§13).

Consequência prática: **quase tudo que era "problema difícil" some.** Casar
produtos entre 40 lojas com 200 títulos exige cascata de matching; casar ~50
ofertas de 4 lojas conhecidas cabe num dicionário em memória.

---

## 2. A pergunta que originou o plano: navegador headless?

**Não — e com o escopo novo a camada some do plano inteiro.**

O headless só existia aqui para uma loja: Shopee. Sem Shopee (§13), não sobra
nenhum alvo que resista a HTTP puro. Fica registrado o porquê, caso o assunto
volte:

| | HTTP + JSON | Headless (Playwright/browser-use) |
|---|---|---|
| Latência por loja | 200–800 ms | 8–40 s |
| RAM por busca | ~5 MB | 300–700 MB (Chromium) |
| Estabilidade | quebra quando o schema muda | quebra quando o CSS muda (mais frequente) |
| Dados obtidos | EAN, reputação, parcelas — tipados | o que der para raspar do HTML |

Subir Chromium para pedir dados que existem num endpoint JSON é pagar 100× em
CPU para receber um resultado *pior*. Camadas que ficam:

```
N1  API oficial / pública       → ML API, VTEX catalog
N2  JSON interno do frontend    → __NEXT_DATA__, XHR que o próprio site chama
N3  HTML + JSON-LD              → schema.org/Product embutido na página
N5  Agregador (rede de segurança) → SerpApi google_shopping, Lomadee
```

N4 (headless) foi removida. Se um dia eu passar a comprar na Shopee, reabrir.

---

## 3. O que já existe e vamos reusar

- **PKS + `pks_remember`/`pks_forget`** (em prod) — é onde o perfil de compra
  mora (§5). Não precisa de tabela nova de preferência.
- **Skills library** — a skill `compras` liga por keyword +
  `tool_choice=required` (o gotcha da `financas`: sem isso a ativação é
  não-determinística).
- **`modules/web_search`** — molde de backend plugável e cliente HTTP com
  timeout já resolvidos.
- **`core/gateway`** (`policy.py`, `redact.py`, `audit.py`) — onde as chaves de
  API (token do ML, Cosmos, SerpApi) moram sem nenhum LLM ver o valor.
- **`modules/finance/money.py`** — parsing de dinheiro BR já resolvido.
- **JobManager + ProactiveDispatcher** — usados **só** pelo cron do vigia (§7).
  A busca em si é síncrona.
- **Postgres + alembic** — histórico de preço, watches, cache de frete.

**O gap:** não existe nada de produto/preço no repo hoje. É greenfield.

---

## 4. Aquisição de dados

> ⚠️ As linhas abaixo são hipótese de trabalho. A **F0 é uma sonda** que mede
> cada uma e produz a tabela real — política de API e anti-bot mudam a cada
> trimestre, e não quero construir sobre chute meu.

### 4.1 Lojas (só onde eu compro)

| Loja | Camada | Como | Risco |
|---|---|---|---|
| **Mercado Livre** | N1 | API oficial `api.mercadolibre.com/sites/MLB/search?q=`. App dev grátis; hoje o search exige token OAuth (era aberto até ~2023). Devolve preço, frete grátis, parcelas, reputação do vendedor, EAN às vezes. | Rate limit; mudança de política |
| **Lojas VTEX** (Casas Bahia, Ponto, Fast Shop, Leroy, Carrefour…) | N1 | `GET /api/catalog_system/pub/products/search?ft=furadeira&_from=0&_to=49` — público, JSON completo **com EAN**, sellers, `Installments`, `ListPrice` vs `Price`. **Uma implementação serve N lojas.** | Header `resources` obrigatório em algumas; paginação limitada a 50 |
| **Magazine Luiza** | N2 | Next.js: `__NEXT_DATA__` na página de busca traz o catálogo tipado. | Migração de framework quebra |

**Descoberta de VTEX é automática:** dado um domínio, bater no endpoint de
catálogo e ver se responde 200 com JSON. Se sim, a loja está coberta sem
escrever adapter novo — melhor retorno por linha de código do plano todo. É por
isso que "só as lojas onde eu compro" não é uma limitação cara: adicionar uma
loja VTEX à minha lista custa uma linha de config.

**Shopee e Amazon estão fora** — não compro lá hoje, e eram as duas únicas
justificativas para headless, proxy residencial e caminho assíncrono. Se eu
passar a comprar, reabrir: Shopee tem o caminho legítimo da **Affiliate Open
API** (GraphQL, AppID+secret), e Amazon a **PA-API 5.0** (exige afiliado com
vendas qualificadas). Nenhum dos dois precisa de browser — precisa de cadastro.

### 4.2 Fontes auxiliares (não são lojas)

| Fonte | Para quê | Ressalva registrada |
|---|---|---|
| **Bluesoft Cosmos** (`api.cosmos.bluesoft.com.br/gtins/{ean}.json`) | Catálogo GTIN/EAN gratuito. Dado um EAN vindo do VTEX, devolve marca/modelo canônicos → **melhora o dedupe por regex** (§6.2) sem inventar heurística. | **Rate limit agressivo (429)**. Usar só para EANs que o dedupe não resolveu sozinho, e memoizar em `gtin_cache` — um EAN consultado uma vez nunca é reconsultado. |
| **Lomadee — API de Ofertas** | Cobre 300+ varejistas; serve como rede de segurança quando os adapters próprios voltam vazio, e os links são de afiliado. | **O ToS proíbe carga full e varredura.** Só consulta pontual por categoria/produto, disparada por uma busca minha. Proibido: crawler, snapshot de catálogo, job periódico varrendo. O vigia (§7) **não** pode usar Lomadee como fonte de re-checagem diária — só os adapters próprios. |
| **SerpApi** (`engine=google_shopping&gl=br&hl=pt-br`) | Ofertas normalizadas de dezenas de lojas numa chamada. Free tier pequeno dá e sobra para F0/F1. | **Rede de segurança, não base.** Custo por busca e menos campos (sem EAN, sem reputação). Se virar caminho principal, o plano está errado. |

Toda oferta vinda de N5 (Lomadee/SerpApi) é oferta de **loja fora da minha
lista** e cai na regra da §6.4.

---

## 5. O perfil de compra — componente de primeira classe

Este é o diferencial do produto, então não é um filtro pendurado no fim: é um
objeto carregado **uma vez por busca** e consultado em **quatro pontos
distintos** do pipeline.

### 5.1 Onde mora

No **PKS / memória do Neo**, namespace `compras.*`. Duas naturezas:

| Parte | Como entra | Exemplos |
|---|---|---|
| **Declarada** (estruturada, muda raramente) | eu declaro uma vez, no setup | `cep`, `voltagem=127V`, `lojas=[...]`, `pagamento=pix`, `teto_parcelas=6x sem juros`, `reputacao_minima` |
| **Preferência** (texto livre, evolui) | eu edito quando **rejeito** algo — "não me mostra Mondial de novo" vira uma linha | `marcas_preferidas`, `marcas_rejeitadas`, `notas` ("prefiro pagar mais e receber em 3 dias") |

**Nada de aprendizado estatístico de preferência.** Com poucas buscas por
semana não existe sinal para inferir nada — um modelo treinado em 12 cliques
inventa preferência em vez de descobrir. O caminho é o explícito: eu falo, o
Neo grava via `pks_remember`, eu leio e corrijo quando estiver errado. Barato,
inspecionável, e corrigível numa frase.

⚠️ **Fronteira com o `ledger_guard`:** `teto_parcelas` e `pagamento=pix` são
*preferência de compra*, não saldo — podem ir ao PKS. Valor de fatura, limite
de cartão e saldo continuam **proibidos** no PKS ([[neo-pks-ledger-boundary]]).
A skill `compras` nunca escreve dinheiro; se precisar saber se cabe no
orçamento, pergunta à skill `financas`.

### 5.2 Os quatro pontos de atuação

**(1) QueryPlanner — expande a query com o que já sabe.**
`"furadeira"` não vira uma busca por "furadeira". Vira:

```
query_expandida: "furadeira de impacto"
filtros_duros:   voltagem ∈ {127V, bivolt}        ← nunca me mostre 220V
                 marca ∉ {marcas_rejeitadas}       ← corte, não penalidade
faixas:          potencia ∈ [500W, 800W]           ← do histórico declarado
bônus:           marca ∈ {Bosch, Makita}           ← peso, não corte
```

O ganho não é a busca ficar melhor — é eu não ter que digitar isso toda vez, e
não receber resultado inútil que eu descartaria à mão. Marca rejeitada e
voltagem errada são **corte duro** (a oferta some); marca preferida é **bônus**
no ranker (§6.3).

**(2) Fan-out — seleciona quais adapters chamar.**
`compras.lojas` é a lista de adapters ativos. Não é filtro pós-busca: as lojas
fora da lista **não são chamadas**, o que economiza latência, quota e o risco de
bater em API que eu nem uso. O agregador N5 só entra quando os adapters próprios
voltam com menos de K resultados úteis (K≈3) — rede de segurança, não default.

**(3) PriceResolver — CEP fixo transforma frete de estimativa em fato.**
Este é o ganho mais concreto de ser um usuário só: o CEP nunca muda, então o
frete de uma loja para uma faixa de peso é **quase estático**.

```
shipping_quote_cache:  (store, weight_band) → { cost, days, captured_at }
weight_band:           <1kg | 1–3kg | 3–10kg | 10–30kg | >30kg
TTL:                   7 dias   (CEP fixo ⇒ a resposta muda de mês, não de hora)
```

Efeito: o frete deixa de ser **uma chamada de simulação por oferta** (50
chamadas por busca, lentas e rate-limitadas) e vira **um punhado de hits de
cache**. Em miss, simular de verdade — mas só para as **top-K ofertas** (K≈8)
depois da ordenação preliminar por preço de produto, nunca para as 50. Regra de
frete grátis acima de limiar (ML, VTEX) é atributo da loja no cache, não
consulta.

**(4) Ranker — pesos meus, não a média de ninguém.**

```
score = w_preco · norm(1/total_landed)
      + w_prazo · norm(1/shipping_days)
      + w_marca · (marca ∈ preferidas)
      + w_reput · norm(seller_score)

corte duro: seller_score < compras.reputacao_minima  ⇒ oferta DESCARTADA
```

Os pesos vêm do PKS, não de constantes no código. Se eu prefiro pagar R$ 30 a
mais para receber em 3 dias em vez de 12, isso é `w_prazo` alto — e é uma frase
minha, não um A/B test. A reputação mínima é **corte, não penalidade**: preço
excelente de vendedor com 2 avaliações não é uma opção ruim, é ruído.

---

## 6. Arquitetura

```
        skill "compras" → tool product_search(query)
                                  │
                      ┌───────────▼────────────┐
                      │  PerfilDeCompra        │ ← PKS  compras.*
                      │  (carregado 1× por     │   CEP, voltagem, lojas,
                      │   busca)               │   marcas, pesos, pagamento
                      └───────────┬────────────┘
                                  │  (1) expande + filtros duros
                      ┌───────────▼────────────┐
                      │  QueryPlanner          │
                      └───────────┬────────────┘
                                  │  (2) seleciona adapters
                      ┌───────────▼────────────┐
                      │  Orchestrator          │  asyncio.gather
                      │  fan-out AO VIVO       │  timeout POR adapter (5 s)
                      └───────────┬────────────┘  resultado PARCIAL
                                  │               circuit breaker por adapter
              ┌───────────────────┼───────────────────┐
        ┌─────▼─────┐      ┌──────▼──────┐     ┌──────▼───────┐
        │  ml_api   │      │ vtex(host)  │     │  agregador   │  só se os
        │           │      │  ×N lojas   │     │  (N5)        │  próprios
        └─────┬─────┘      └──────┬──────┘     └──────┬───────┘  vierem <3
              └───────────────────┼───────────────────┘
                      ┌───────────▼────────────┐
                      │  Normalizer            │  título → marca/modelo/specs
                      │  + Dedupe EM MEMÓRIA   │  EAN, senão marca+modelo regex
                      └───────────┬────────────┘  (~50 ofertas, dict, O(n))
                                  │  (3) CEP fixo + shipping_quote_cache
                      ┌───────────▼────────────┐
                      │  PriceResolver         │  → total_landed
                      └───────────┬────────────┘     (produto + frete, no Pix)
                                  │  (4) pesos do perfil + cortes duros
                      ┌───────────▼────────────┐
                      │  Ranker                │
                      └───────────┬────────────┘
                                  │
                      ┌───────────▼────────────┐
                      │  offer_price_history   │  grava SEMPRE, desde a F1
                      └───────────┬────────────┘
                                  │
                      ┌───────────▼────────────┐
                      │  VeredictoTemporal     │  compara com a série histórica
                      └───────────┬────────────┘
                                  ▼
              3–5 ofertas  +  "R$ 420 hoje; mediana de 60d = R$ 389 → espera"
```

Tudo síncrono, uma passada, sem job. O caminho assíncrono foi removido junto com
a camada headless que era sua única razão de existir (§13).

### 6.1 Fan-out ao vivo — e por que NÃO um crawler com índice local

Está correto para este volume e não deve ser trocado. Um índice local exigiria
manter catálogo fresco de milhões de SKUs para servir ~5 buscas por semana — e
o campo que mais importa (preço) é justamente o que envelhece mais rápido. Seria
infraestrutura sem dono: muito para manter, errada quando consultada. O fan-off
ao vivo custa ~2 s, está sempre correto, e a única coisa que persistimos é
justamente o que ganha valor com o tempo (a série de preço, §7).

O que **continua** valendo mesmo com volume baixo:

- **timeout por adapter** (5 s) e teto total ~8 s;
- **resultado parcial** — VTEX fora do ar não pode zerar a resposta;
- **circuit breaker** — 3 falhas seguidas desligam o adapter por 15 min. Com
  poucas buscas por semana isso não é performance, é **diagnóstico**: um adapter
  aberto por muito tempo é o sinal de que a loja mudou o schema.

**Adapter é uma interface só:**

```python
class StoreAdapter(Protocol):
    name: str
    timeout_s: float
    async def search(self, q: PlannedQuery) -> list[RawOffer]: ...
```

Adicionar loja = um arquivo em `modules/shopping/adapters/` (ou uma linha de
config, se for VTEX).

### 6.2 Dedupe em memória (substitui a cascata de matching)

Dentro do resultado de **uma** busca (~50 ofertas), duas passadas num dicionário:

1. **EAN igual** → mesma oferta. VTEX entrega EAN; ML às vezes.
2. **Senão, `marca + modelo normalizado`** por regex: `"Bosch GSB 550 RE 550W
   127V"` → `bosch|gsb550re`. Dicionário de marcas + regex de código de modelo
   (`[A-Z]{2,4}\s*\d{2,4}\s*[A-Z]{0,3}` → minúsculas, sem espaço).
3. Não casou? Fica como oferta separada. **Um falso negativo aqui custa uma
   linha duplicada na resposta — não um bug de dados.**

Sem `pg_trgm`, sem embedding, sem LLM de desempate: são ferramentas de
desambiguação em escala, e 50 ofertas de 4 lojas conhecidas não têm escala.
Quando o EAN existe mas a marca/modelo do título estão sujos, o **Cosmos**
(§4.2) resolve com uma consulta memoizada — mais barato e mais correto que
qualquer heurística nossa.

Armadilha que permanece: variação (110v/220v, cor, kit com maleta) **não é o
mesmo produto** para efeito de preço. A voltagem já é corte duro do perfil, o
que elimina a metade mais perigosa do problema de graça.

### 6.3 Preço-verdade (é onde comparador brasileiro mente)

O mesmo dinheiro aparece como "R$ 249" e "R$ 199". Resolver:

- **Pix vs cartão** — desconto à vista é regra, não exceção;
- **Frete real até o meu CEP** (§5.2, ponto 3) — muda o vencedor com frequência;
- **Parcelamento** — "12× sem juros" ≠ 12× com juros; o perfil tem teto;
- **Cupom / preço só no app** — capturável em VTEX (`teasers`), nem sempre.

Chave de ordenação: **`total_landed`** = produto + frete, à vista no Pix. Os
outros campos ficam preservados para exibição. **Nunca ordenar pelo preço de
vitrine.**

### 6.4 Loja fora da minha lista

Ofertas de lojas que não estão em `compras.lojas` (só chegam via N5) **não
entram por padrão**. Entram apenas se forem **materialmente melhores** que a
melhor oferta da minha lista:

```
total_landed ≤ melhor_da_lista × 0,90   E   diferença ≥ R$ 30
```

As duas condições juntas evitam "vitória" de R$ 3 numa compra de R$ 40. Quando
entra, vem **sinalizada** — não misturada no ranking como se fosse conhecida:

> ⚠ **Loja nova pra você:** Leroy Merlin — R$ 361 (R$ 59 mais barata que a
> melhor do ML). Nunca comprei aí. Quer que eu adicione à sua lista?

O aceite vira `pks_remember` na lista de lojas. É assim que a lista cresce: por
evidência de preço e decisão minha, não por cobertura automática.

### 6.5 Formato da resposta

Duas camadas: **resumo compacto** (3–5 ofertas, cada uma com o *porquê* — "mais
barata com frete", "chega quinta", "único bivolt") e o **veredito temporal**
(§7). Em modo live isso importa ainda mais — ninguém escuta 40 ofertas
([[neo-live-mode-flag]]).

---

## 7. O vigia de preço — a feature central (não um acessório)

**Eu quase nunca preciso comprar agora. Preciso comprar bem.** Uma busca que
devolve as 5 melhores ofertas de hoje responde a pergunta errada; a pergunta
certa é *"esse preço é bom?"*. E essa não se responde com cobertura nem com
latência — só com **série temporal**. É por isso que o vigia deixou de ser a
última fase e virou o eixo do produto.

> "Furadeira Bosch GSB 550: **R$ 420** hoje no ML.
>  Mediana de 60 dias: **R$ 389**. Mínimo: R$ 355 (há 3 semanas).
>  **Recomendo esperar** — esse preço está no quartil superior."

Isso vale mais que qualquer busca rápida, e nenhum comparador me dá porque
nenhum sabe há quanto tempo eu estou de olho *neste* produto.

**Duas formas:**

| Forma | Como | Onde aparece |
|---|---|---|
| **Veredito passivo** | toda busca compara o resultado com `offer_price_history` e classifica: `bom_preco` / `mediano` / `caro` / `sem_historico` | no fim de toda resposta de busca |
| **Vigia ativo** (`price_watch`) | "me avisa se a Bosch cair de 350" ou "me avisa quando for um bom preço" | alerta proativo no chat / WhatsApp |

**Mecânica do vigia ativo:**

- Job periódico no JobManager, 1×/dia por watch ativo. Volume é trivial (dezenas
  de watches × 1 busca = nada). **Só adapters próprios** — Lomadee é proibida
  aqui pelo ToS (§4.2).
- Regra `abaixo_de` (preço alvo declarado) ou `bom_preco` (≤ p25 dos últimos 90
  dias). A segunda só arma depois de ~30 dias de histórico.
- Anti-spam: `last_notified_at` + só re-notifica se melhorou ≥5% desde o último
  alerta.
- Canal: ProactiveDispatcher (chat) e/ou o endpoint `/notify` do WhatsApp, que
  já existe.

**`sem_historico` é uma resposta honesta e obrigatória.** No primeiro mês o
agente vai dizer "não sei se está bom, só tenho 4 dias de série" — e isso é
melhor que fingir um veredito. É exatamente por isso que a **F1 grava
`offer_price_history` mesmo sem ninguém ler**: a F3 sem 60 dias de série nasce
muda. O custo é uma tabela de append; o benefício é a feature principal já
nascer com dados.

---

## 8. Modelo de dados

```
product_offer          store, store_sku, url, title_raw, image_url,
                       price_list, price_cash, price_pix,
                       installments_n, installments_value, installments_free,
                       shipping_cost, shipping_free, shipping_days,
                       seller_name, seller_score, rating, reviews_n,
                       ean, brand, model_norm, total_landed, captured_at
                       └─ SEM canonical_id: o dedupe é em memória (§6.2)

offer_price_history    store, store_sku, total_landed, captured_at
                       └─ chave estável (store, store_sku). É a série que
                          sustenta todo o §7. Append desde a F1.

price_watch            alvo (query_norm | store+store_sku), regra
                       (abaixo_de | bom_preco), target_price, channel,
                       active, last_price, last_notified_at

shipping_quote_cache   (store, weight_band) → cost, days, captured_at
                       └─ CEP fixo ⇒ quase estático. TTL 7 dias.

gtin_cache             ean → brand, model, description, fetched_at      [F2]
                       └─ só memoiza o Cosmos para não bater no 429.
                          NÃO agrega ofertas, não é product_canonical.

product_query          query_norm, perfil_snapshot, resultado_json, created_at
                       └─ NÃO é cache de performance (com 5 buscas/semana ele
                          nunca acertaria). É reprodutibilidade: por que o
                          agente recomendou X naquele dia, com qual perfil.
```

O perfil **não** tem tabela: mora no PKS (§5.1).

---

## 9. Anti-bloqueio e legalidade

- Preferir sempre API oficial e **programa de afiliado** — resolve legitimidade
  e ainda monetiza. Vale para ML e Lomadee.
- **Respeitar o ToS do Lomadee literalmente**: consulta pontual disparada por
  busca minha. Sem carga full, sem varredura, sem uso no cron do vigia.
- Rate limit por domínio (token bucket), backoff em 429/503, circuit breaker por
  adapter (§6.1). O Cosmos exige isso de verdade — memoizar é obrigatório, não
  otimização.
- Respeitar `robots.txt` em N2/N3; User-Agent identificável.
- Sem burlar captcha, sem proxy para contornar bloqueio explícito. Se uma loja
  recusar sistematicamente, ela sai da lista — não vira alvo técnico.
- Volume de 1 usuário com poucas buscas por semana é, por si só, a melhor
  defesa: nenhum destes limites deve sequer ser roçado.

---

## 10. Fases

| Fase | Entrega | Como sei que funcionou (verificável) |
|---|---|---|
| **F0 — Sonda** (1 dia) | Script isolado que bate em ML, 4 lojas VTEX, Magalu, Cosmos, Lomadee e SerpApi. Mede: responde JSON? exige token/cadastro? quantos campos úteis (EAN? reputação? parcelas?)? bloqueia? Gera a tabela real do §4. | Uma tabela preenchida **com resposta salva por fonte** (curl → arquivo), incluindo o veredito sobre o token do ML e o cadastro do Lomadee. Zero linha baseada em suposição minha. |
| **F1 — Núcleo + perfil** | `modules/shopping/`, adapters `ml_api` + `vtex`, Orchestrator (fan-out, timeout, parcial, circuit breaker), tool `product_search`, skill `compras`, **perfil aplicado nos 4 pontos** (§5.2), grava `offer_price_history`. | "acha uma furadeira" devolve ofertas reais de ML + ≥2 VTEX em <8 s, **já sem nenhum 220V e sem nenhuma marca da minha lista de rejeitadas**, e `offer_price_history` tem linhas novas ao fim da busca. Derrubar um adapter de propósito ainda devolve resposta. |
| **F2 — Preço-verdade** | PriceResolver (`total_landed` = Pix + frete real com CEP fixo + parcelas), `shipping_quote_cache`, dedupe em memória (§6.2), `gtin_cache`/Cosmos, Ranker com pesos do PKS, regra de loja nova (§6.4). | O mesmo produto aparece **uma vez só**; a ordenação é por `total_landed` e não pelo preço de vitrine; a busca faz **≤2 simulações de frete** (o resto vem do cache); mudar um peso no PKS muda a ordem da resposta. |
| **F3 — Vigia de preço** | `price_watch` + cron diário no JobManager + alerta proativo (chat/WhatsApp) + veredito temporal em toda busca. | Um watch dispara alerta com preço e link; e uma busca comum responde "mediana de 60 d = R$ X, espera" **usando a série gravada desde a F1** — não um número inventado. Sem histórico, diz `sem_historico`. |

Não há F4/F5. O produto está completo em F3.

---

## 11. Ideias futuras (não são fases)

- **Aba Compras no neo-panel** — histórico, gráfico de preço de 90 dias, gerir
  watches. Valor real, mas o agente funciona inteiro sem UI: a resposta vem no
  chat. Vira fase quando eu sentir falta, não antes.
- **Links de afiliado** (ML, Lomadee) — monetização; irrelevante para um usuário.
- **Shopee / Amazon** — reabrir **só** se eu passar a comprar lá. Caminho: API
  de afiliado dos dois (cadastro), não headless.
- **Mais lojas VTEX** — praticamente grátis (uma linha de config); adicionar por
  evidência da regra §6.4, não por cobertura.

---

## 12. Riscos

1. **Token do ML** — se o cadastro travar, a F1 perde a loja mais importante e
   sobra VTEX. Mitigação: a F0 valida isso **primeiro**, antes de qualquer
   código.
2. **Manutenção de adapter** — loja muda schema e o adapter passa a devolver
   zero. Com poucas buscas por semana eu posso levar semanas para perceber.
   Mitigação: alerta quando um adapter devolve **zero resultados** (sinal de
   quebra, não de "não achou") e teste de contrato com resposta gravada no CI.
   Este risco é maior aqui do que num produto de alto volume — pouco tráfego
   esconde defeito.
3. **Série de preço curta** — o §7 só fica bom depois de ~60 dias. Mitigação:
   gravar desde a F1 e o agente dizer `sem_historico` sem constrangimento.
4. **Perfil errado é pior que perfil ausente** — um corte duro equivocado
   (voltagem, marca rejeitada) **esconde** a oferta certa e eu nunca fico
   sabendo. Mitigação: a resposta informa quantas ofertas foram cortadas e por
   qual regra ("12 ocultadas: 220V"), e `pks_forget` desfaz numa frase.
5. **Fronteira com o `ledger_guard`** — a skill `compras` fala de preço o tempo
   todo e não pode escrever dinheiro no PKS. Mitigação: só preferência vai ao
   PKS; valores vivem em `product_offer`/`offer_price_history`
   ([[neo-pks-ledger-boundary]]).
6. **ToS do Lomadee** — uso indevido (varredura, cron) derruba o cadastro.
   Mitigação: a fonte é chamada **apenas** no caminho de busca interativa, com
   guarda explícita no código do vigia.

---

## 13. O que foi removido nesta revisão (para eu revisar antes de aceitar)

- **Tabela `product_canonical` + cascata de matching (EAN → modelo → trigram →
  embedding → LLM)** — era desambiguação em escala; com ~50 ofertas de 4 lojas
  conhecidas por busca, um dicionário em memória (EAN, senão marca+modelo por
  regex) resolve, e um falso negativo custa uma linha duplicada, não um bug.
- **Shopee** — não compro lá; era a única loja que justificava anti-bot, proxy
  residencial e manutenção pesada. Nota de reabertura mantida em §11.
- **Amazon BR** — não compro lá; a PA-API exige afiliado com vendas
  qualificadas e o resto era scraping hostil por um retorno que eu não uso.
- **Camada N4 (headless browser)** — sem Shopee/Amazon não sobrou nenhum alvo
  que resista a HTTP+JSON; era 40× a latência e 100× a RAM para dados piores.
- **Job `product_scan`** — existia só para hospedar o browser da Shopee, que
  saiu.
- **Busca de duas velocidades (rápida + assíncrona)** — sem N4 não há nada lento
  para separar; toda fonte que sobrou responde em menos de 1 s.
- **F5 (painel + links de afiliado)** — não é fase de um agente de um usuário só:
  o painel é conveniência sobre uma resposta que já chega no chat, e afiliado é
  monetização sem público. Movidos para §11.
- **Meta de latência de 2 s** — deixou de ser critério; com poucas buscas por
  semana, 8 s é indistinguível de 2 s, e a folga compra frete real em vez de
  estimado.

**Reduzido, não removido:**

- **`product_query`** — deixou de ser cache de performance (com 5 buscas por
  semana nunca acertaria) e virou registro de reprodutibilidade: qual perfil
  produziu qual recomendação, para eu auditar.

**Mantido de propósito, apesar do volume baixo:**

- **Fan-out ao vivo com `asyncio.gather`, timeout por adapter, resultado parcial
  e circuit breaker** — correto para este volume; não trocar por crawler/índice
  local, que seria manter catálogo de milhões de SKUs para servir ~5 buscas por
  semana (infraestrutura sem dono, e com o campo mais volátil — preço — sempre
  velho). O circuit breaker aqui vale mais como **diagnóstico** de adapter
  quebrado do que como proteção de carga.
- **`offer_price_history` desde a F1** — é o insumo da feature central (§7); sem
  gravar desde o começo, a F3 nasce sem série e sem valor.
