# Avaliações

Toda mudança de comportamento no Neo que dependia de um modelo foi decidida com uma medição. Este arquivo reúne essas medições: o que foi medido, como, com quantas amostras e o resultado.

**O que estes números são e o que não são.** São medições de engenharia, feitas para decidir uma mudança: amostras pequenas (5 a 59 casos), quase sempre com o **request real** de produção reenviado ao mesmo modelo. Não são benchmark. Cada linha diz o `n`, para o leitor pesar o resultado.

**Regra que vale para todas:** cada tabela compara **uma coisa só**. Ou é o mesmo modelo antes e depois de uma mudança no sistema, ou são modelos diferentes na mesma tarefa.

---

## 1. Antes e depois (mesmo modelo, uma mudança)

| Mudança | Antes | Depois | n e método | Data |
|---|---|---|---|---|
| Prompt do classificador sem repetição | 29.972 caracteres por mensagem, 43/46 | **18.720 (−37%), 43/46**, zero decisão diferente | golden set de 46 casos, A/B, gpt-4o-mini | 01/08/2026 |
| Classificador com JSON truncado pelo raciocínio (deepseek-v4-flash) | 13/46 caíam no fallback por palavra-chave | **44/46, zero truncagens** | mesmo golden set | 01/08/2026 |
| Turno do cérebro: stream por etapa, sem raciocínio, pin de provedor | 71,5 s; 1º token em 13–40 s | **28,0 s (−61%); 1º token em 2,3–3,4 s** | replay dos mesmos 6 rounds de um turno real | 11/09/2026 |
| Pin de provedor no OpenRouter (deepseek-v4-flash) | 21,7 tok/s com roteamento livre | **74–80 tok/s** com provedor fixo | 4 chamadas por braço, mesmo prompt | 11/09/2026 |
| Cache de prefixo com provedor estável | 0 tokens cacheados (trocando de provedor) | **24.576 de 24.723** tokens de entrada cacheados | mesmo turno | 11/09/2026 |
| Navegador lendo 3 anúncios: abas em `paralelo` | 21 passos, 71 s | **5 passos, 39 s**, mesmo veredito | 2 tarefas reais equivalentes | 02/11/2026 |
| Raciocínio cortando o passo do navegador (GLM-5.3-flash): teto na fonte | ~10 s por corte; 28% do custo do job sem entregar nada | **1–67 tokens de raciocínio, ~2 s** | 6 reenvios do payload real, 5 provedores | 08/11/2026 |
| Aviso no resultado quando um download de URL inventada falha | 0/5 vão buscar a URL certa | **5/5** chamam `web_search` | A/B com o request real e as 68 tools | 14/11/2026 |
| Descrição da ferramenta que delega o formulário | delegou 0/16 | **10/16** | A/B com o request real | 02/11/2026 |
| A mesma regra em outra posição: no prompt de sistema × logo depois da mensagem | 2–3/8 | **8/8** | A/B, mesma frase | 31/10/2026 |
| Guarda de "toca <música>" no roteador | `coloca/bota/põe` disparavam em pedidos que não eram música | **12/12 acertos, 0 falso positivo** | 1.865 mensagens reais de usuário | 07/09/2026 |

## 2. Modelos lado a lado (mesma tarefa)

### Classificador de intenção, 30 pedidos reais de produção (corpo completo do request)

| Modelo | p50 | p90 | máx | Problema encontrado |
|---|---|---|---|---|
| GLM-5.3-flash (com teto de raciocínio) | 3,4 s | 11,7 s | 19,6 s | um provedor ignora o teto de raciocínio |
| **MiniMax m3.1-flash, provedor fixo** (em produção) | **2,5 s** | **4,2 s** | 6,5 s | 1/30 voltou vazio; o código repete a chamada |
| Haiku 5.5 | 2,6 s | 4,9 s | 6,8 s | em 19/30 respondeu ao usuário em vez de classificar |

14/11/2026. Com o MiniMax, o golden set atual (59 casos) fica em **57/59**. As 2 falhas já falhavam com o prompt anterior, em 3 de 3 repetições.

### Juiz de formulário: Haiku × Sonnet, mesma página real, 2 rodadas cada

| Modelo | Concorda consigo mesmo entre rodadas | "Conforme" (de 24) |
|---|---|---|
| Haiku | 20/24 | 15 e 19 (mais leniente) |
| Sonnet | 18/24 | 8 e 9 |

12/11/2026. Os dois derrubaram o "CONFIRMADO" falso do agente, que era o defeito que o juiz veio resolver.

## 3. Como reproduzir

| Medição | Onde |
|---|---|
| Classificador (golden set) | `neo-core/scripts/eval_router.py` + `neo-core/tests/router/golden_intents.yaml` (`--ab` compara duas versões do prompt; um run em que o provedor falhou é marcado **inválido**, porque o fallback por palavra-chave "concordaria" com tudo) |
| Portão de mídia do roteador | `neo-core/scripts/eval_midia_gate.py` |
| Ranking da memória (PKS) | `neo-core/scripts/pks_ranking_bench.py` (o gabarito é feito de conversas reais e fica fora do repositório) |
| Recuperação de conversa | `neo-core/scripts/eval_conversation_recall.py` |
| Guardas sobre a prosa do modelo | rodar o predicado novo e o antigo sobre as mensagens reais e olhar os dois deltas (o que deixou de pegar tem que ser só falso positivo) |

O método do A/B: extrair do diário da sessão o payload exato que foi ao modelo, aplicar a mudança e reenviar ao mesmo modelo e provedor, 5 a 16 vezes por braço. Custa centavos e mede a mudança com o contexto real, não com um exemplo inventado.

## 4. Próximas suítes

| Suíte | O quê | Métricas |
|---|---|---|
| Classificador, vários modelos | o golden set de 59 casos × 5–6 modelos × 3 repetições | acerto, p50/p90, custo por 1.000 classificações, % no fallback |
| Navegação | 6 a 8 tarefas fixas num site de demonstração com resposta conhecida, × 4–5 modelos de driver × 3 repetições | sucesso conferido por código, passos, tempo, tokens, custo |
| Ponta a ponta | GAIA nível 1 (`neo-core/scripts/eval_gaia.py`) | acerto, "errou confiante" × "não sei", custo por pergunta |
