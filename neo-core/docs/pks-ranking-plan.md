# Ranking do PKS — plano

> Status (2026-09-21): **F1 e F2 implementadas; v2 rodando em SOMBRA em produção**
> (`PKS_RANKER_V2_ENABLED=false`, `PKS_RANKER_V2_SHADOW=true`). F0 parcial.
> F3 (ligar) aguarda a revisão do gabarito pelo Daniel. Ver §11.
> Origem: sessões `d35696d4` e `098211a8` (treino da semana). A quinta-feira
> estava completa no banco e não chegava ao prompt; segunda e terça chegavam
> com exercícios faltando; e a conferência (`search_pks`) contradizia o prompt.
>
> Ideia central: trocar "quem chegou primeiro, até 10" por uma **nota de
> prioridade calculada para cada entidade**, ordenar por ela e encher um
> **orçamento de tokens**. É assim que motor de busca faz: gerar candidatos,
> pontuar cada um com sinais explicáveis, ordenar e cortar por custo. E medir
> contra um gabarito antes de ligar.

---

## §1. O que está errado hoje (medido)

| # | Defeito | Onde | Evidência |
|---|---|---|---|
| D1 | Teto de **10 entidades por contagem**, completado por `updated_at DESC` | `retriever.py` (`max_candidates=5`, `type_cap=10`, `_entities_by_types`) | Em 7 de 7 blocos da sessão `098211a8`, 5 das 10 vagas foram de exercícios soltos da quarta (gravados no mesmo dia), que já aparecem como relação do "Treino de quarta-feira". A quinta (gravada em junho) só entrou quando "quinta" veio nas keywords. |
| D2 | A consulta é só `knowledge_keywords` do classificador (até 6, escolhidas por LLM) | `retriever._keywords` | "lista por exercicios tb" → `treino, semana, exercicios, segunda, terca`. A conversa (as 3 falas anteriores eram sobre a quinta) é ignorada. |
| D3 | **Corte silencioso de relações**: `visible_relations[:8]`, ordenadas por `created_at DESC` | `context_builder.py`, `repository.get_relations` | 4 dos 5 treinos têm mais de 8 relações. A segunda perdeu "Tríceps francês crossover" e a terça perdeu "Rosca Sentado no Banco", **e a resposta do Neo saiu incompleta sem avisar**. |
| D4 | O bloco é **tudo ou nada** no seletor: 1 candidato com 900 tokens de orçamento | `context_selector._select_candidates` | Aumentar o teto de 10 para 20 faria o bloco passar de 900 tokens, e aí ele **sumiria inteiro** do prompt. |
| D5 | `search_pks` usa outra busca (trigram por nome) e **não devolve relações** | `service.search_entities` | Turno 6 da `098211a8`: os 6 exercícios da quinta estavam no prompt, a ferramenta disse que não, e o Neo acreditou na ferramenta. A confissão falsa da `d35696d4` teve a mesma causa. |
| D6 | Sinais que existem e não são usados | `pks_entity` | `embedding vector(384)` nunca preenchido (0 de 144). O "recency boost" do `EntityResolver` é binário (`+0.05` se `last_used_at` existe), e 129 das 144 entidades têm `last_used_at`, então não separa nada. |
| D7 | Dois limiares que podem discordar | `context_selector._score_pks` (0,25) × score do retriever | É a mesma classe de bug da memória de 19/09 ("teto do score 0,213 < corte 0,25"). |

Dados tortos que o ranking não conserta (tratados na F0):
- o "Plano de treino de academia" não tem `has_member → Treino de quinta-feira`;
- o plano antigo "Treino ABCDE de hipertrofia com máquinas" (19/05) ainda tem `has_member → Treino de quarta-feira`.

Tamanho real (workspace do Daniel): **144 entidades, 118 relações**, 15 tipos
(33 exercise, 30 document, 14 person, …). Pequeno o bastante para pontuar TODAS
as entidades a cada turno.

---

## §2. Princípios

1. **Separar candidatos de ranking.** Hoje a ordem de chegada decide quem entra.
   No plano novo, todo candidato recebe nota e só a nota decide.
2. **Nota explicável.** Cada entidade tem as notas parciais registradas. No log
   dá para ler *por que* a quinta ficou de fora, como no `explain` do
   Elasticsearch.
3. **Orçamento em tokens, não em contagem.** O bloco nunca passa do orçamento,
   então nunca é descartado inteiro.
4. **Lista não se corta em silêncio.** Relação de contenção (os exercícios de
   um treino) aparece completa, ou com "+N — use search_pks".
5. **Uma função só.** O bloco do prompt e a `search_pks` usam o mesmo ranking.
6. **Medir antes de ligar.** Primeiro gabarito e linha de base, depois modo
   sombra, e só então ligar.

---

## §3. Arquitetura — 5 estágios

```
 mensagem + keywords + 3 falas anteriores
          │
 E1  CONSULTA ──────────── termos com peso (atual 1.0, anteriores 0.6^j)
          │
 E2  CANDIDATOS ────────── o workspace inteiro (144), 3 queries em lote
          │                (entidades, fields, relações), sem N+1
 E3  NOTA ──────────────── 6 sinais normalizados em [0,1] → soma ponderada
          │                + propagação pelo grafo
 E4  AGRUPAMENTO ───────── folha coberta pelo contêiner vira sub-item dele
          │
 E5  EMPACOTAMENTO ─────── maior nota primeiro, 3 níveis de detalhe,
                           nunca passa do orçamento, lista completa ou "+N"
```

### E1 — Consulta

- `q0` = mensagem atual ∪ `knowledge_keywords` (peso 1,0). As keywords
  continuam ajudando, mas deixam de ser a consulta inteira (D2).
- `q1..q3` = as 3 falas anteriores da sessão, **do usuário e do Neo**, com peso
  `0,6^j` (0,6; 0,36; 0,22). "confere aí" e "lista por exercícios tb" só fazem
  sentido com a conversa.
- Normalização: `normalize_token` (sem acento), hífen quebra ("quinta-feira" →
  "quinta feira"), stopwords pt, e um *stemmer* leve de plural pt
  (exercícios→exercicio, séries→serie, -ões→ão, -ais→al). Sinônimos pela
  `OntologyResolver`, que já existe, mais uma tabela de dias da semana
  pt↔en (os fields gravam `weekday: thursday`).

### E2 — Candidatos

Carrega o workspace inteiro (sem `deleted_at`, sem `sensitivity=secret`) em 3
queries: entidades, fields e relações vivas. Com 144 entidades isso é
milissegundos, e **já é mais barato que hoje** (hoje são 2 queries por
candidato). Guarda de escala: acima de 3000 entidades, pré-filtra por trigram +
tipo + vizinhança de 1 salto.

### E3 — Nota de prioridade

Cada entidade `e` tem um **documento** com campos de pesos diferentes, como o
`multi_match` de um buscador: nome ×3, aliases ×2, valores de fields ×1, nomes
dos vizinhos ×0,5, tipo (pt e en).

| Sinal | Definição | Por quê |
|---|---|---|
| `f_nome` | 1,0 se o nome ou um alias inteiro aparece em `q0`; 0,8 se todos os tokens *distintivos* do nome (IDF alto) aparecem | "treino de quarta" tem que ganhar de tudo |
| `f_lex` | BM25 de `q0` contra o documento (k1=1,2, b=0,75), com IDF calculado sobre as 144 entidades. Saturado: `s/(s+κ)` | "treino" aparece em 6 nomes (IDF baixo) e "quinta" em 1 (IDF alto): o termo raro pesa mais, e é isso que separa os dias |
| `f_conv` | `max_j 0,6^j · lex(q_j, e)` nas 3 falas anteriores | a quinta estava na fala anterior do próprio Neo |
| `f_grafo` | `max` sobre os vizinhos `n` de `evidência(n) · α(relação) / √grau(n)` | "ombro" acha o grupo muscular Ombro → `targets` → Treino de quinta; "treino" acha o plano → `has_member` → os 5 dias |
| `f_tipo` | a consulta nomeia um tipo (via ontologia): tipo exato 1,0; mesma família, contêiner (routine/workout) 0,8; mesma família, folha (exercise) 0,3 | "treino" quer os dias, não 33 exercícios soltos |
| `f_prior` | `0,6·confiança + 0,4·2^(−dias desde updated_at / 60)` | desempate fraco. **Nunca dominante**: foi `updated_at` como ordem principal que causou o D1 |
| `f_sem` | cosseno do embedding (reservado, peso 0 até existir embedding; ver F4) | paráfrase ("computador de jogos" → "PC Gamer") |

```
evidência(e) = max(f_nome, f_lex, f_conv, f_sem)          # evidência DIRETA

nota(e) = 0,30·f_nome + 0,25·f_lex + 0,15·f_conv
        + 0,15·f_grafo + 0,10·f_tipo + 0,05·f_prior       # pesos iniciais
```

- **α da propagação**: contenção (`has_member`, `has_exercise`, `part_of`,
  `contains`) 0,9 do contêiner para o membro e 0,8 do membro para o contêiner;
  `targets`/`uses` 0,6; o resto 0,4. Um salto só.
- **Amortecimento por grau** (`/√grau`): "Daniel Morais" tem 39 relações e não
  pode puxar as 39 só porque o nome dele apareceu.
- **Por que saturação e não dividir pelo máximo:** dividir pelo melhor da
  consulta dá 1,0 a *alguém* mesmo quando nada casa ("quanto é 2+2?"). A
  saturação mantém nota baixa quando a evidência é fraca.
- **Limiar `τ_min`** (inicial 0,12): abaixo dele a entidade não entra, mesmo com
  orçamento sobrando. Só tipo + prior somam no máximo 0,15, então a "família
  de tipo" sozinha não inunda o bloco.
- Os pesos, `κ` e `τ_min` são **calibrados no gabarito** (§6), não no olho.

**Exemplo** (turno "lista por exercicios tb", valores aproximados):

| Entidade | f_nome | f_lex | f_conv | f_grafo | f_tipo | f_prior | nota |
|---|---|---|---|---|---|---|---|
| Treino de quinta-feira | 0 | 0,30 | 0,48 (fala anterior: "Quinta \| Ombro") | 0,30 | 1,0 | 0,6 | **~0,32** |
| Leg Press (folha) | 0 | 0,25 | 0 | 0,45 | 0,3 | 0,9 | ~0,20 → agrupada na quarta (E4) |

Hoje a quinta fica de fora. Com a nota, ela passa as folhas da quarta e
entra no bloco.

### E4 — Agrupamento por contenção

Uma folha cujo contêiner também é candidato (nota ≥ `τ_min`) **não ocupa vaga
própria**. Ela entra no grupo do contêiner, e a nota do grupo vira
`max(nota do contêiner, notas das folhas)`.
- Pergunta "quanto faço de leg press?": a folha Leg Press tem nota alta, o grupo
  "Treino de quarta-feira" sobe junto, e a folha aparece **dentro** do dia.
- Folha com fields próprios (séries, carga) vira sub-linha do pai:
  `  - Leg Press: 4x12, 80 kg`.
- Folha sem contêiner candidato continua sozinha.

Isso resolve o D1 pela raiz: as 5 folhas da quarta ocupavam vaga para repetir o
que o treino da quarta já mostrava.

### E5 — Empacotamento no orçamento

- Ordem: as globais obrigatórias (`health_state`, `restriction`, por segurança)
  primeiro, depois os grupos por nota.
- Cada grupo é renderizado no maior nível que couber no que sobra:
  **completo** (fields + relações) → **compacto** (fields + relações numa
  linha por tipo: `has_exercise: A, B, C`) → **mínimo** (nome e tipo).
- Relações de contenção saem **completas**. Passando de 20 itens:
  "+N — `search_pks` traz o resto". As outras relações são ordenadas pela nota
  do vizinho, não por `created_at`. Isso acaba com o D3.
- O bloco **nunca passa** de `context_budget_user_pks` (900) → nunca é
  descartado inteiro (D4). Mesma técnica do `_fit_working_index` do caderno.
- **Um limiar só (D7):** `_score_pks` do seletor passa a devolver 1,0 quando o
  ranking incluiu algo e 0 quando não incluiu. Quem decide relevância é o
  `τ_min` do ranking; o 0,25 do seletor deixa de competir com ele.

Estimativa para a semana inteira: 5 treinos completos + o plano ≈ 500 tokens,
cabe com folga.

---

## §4. `search_pks` com o mesmo ranking

- A consulta é o argumento da ferramenta + as 3 falas anteriores.
- Devolve os top-5 **grupos** com fields **e relações** (lista de contenção
  completa), mais a nota e os sinais que a compuseram.
- Resultado: "confere aí" mostra os 6 exercícios da quinta, igual ao prompt.

---

## §5. Observabilidade (o "explain")

- Session log, em todo turno com PKS: seção `### PKS — ranking` com as 15
  melhores entidades, as notas parciais, a nota final e a decisão (`entrou`,
  `agrupada em X`, `nível compacto`, `sem orçamento`, `abaixo do limiar`).
- Painel **Memória › Contexto** (`/context/prompt-preview`): a mesma tabela.
  Ele já reproduz o turno real, então é só expor o `explain`.

---

## §6. Avaliação — gabarito, métricas e calibragem

É o que torna isso "busca precisa" e não mais um ajuste no olho.

**Corpus disponível:** 721 logs de sessão no container (228 com bloco de PKS
injetado) + 2276 mensagens do usuário no Postgres.

**Gabarito (`tests/pks_ranking/golden.yaml`, ~60 consultas):**
- ~15 de falhas conhecidas: os 10 turnos de `d35696d4` e `098211a8`, a música de
  `5102534a`, os dispositivos de `384236e9`…;
- ~30 amostradas dos 228 turnos reais, estratificadas por tema (treino,
  dispositivos, pessoas, projetos, preferências);
- ~15 **negativas** (pedidos que não precisam de PKS: "quanto é 2+2", "toca X",
  "abre o arquivo…").
- Cada caso leva a **janela de conversa** (as 3 falas anteriores do log), sem a
  qual o `f_conv` não pode ser medido.
- Rótulos graduados por entidade: **2** necessária, **1** útil, **0**
  irrelevante. Eu (Claude) rascunho lendo o PKS e o turno; **o Daniel revisa**.
  O gabarito é dele, não do modelo.

**Harness (`scripts/pks_ranking_bench.py`):** roda o ranking atual e o novo
**só leitura**, dentro do container (`docker exec neo-core python …`), contra o
banco real. Sem LLM, determinístico, menos de 1 minuto. Sai JSON com as
métricas e o diff por consulta.

**Métricas:**

| Métrica | O que mede | Aceite |
|---|---|---|
| **Recall@orçamento** (principal) | fração das entidades "2" que chegaram ao bloco FINAL | ≥ 0,90 |
| nDCG@10 | qualidade da ordem antes do empacotamento | > linha de base |
| Completude de lista | contêineres renderizados com toda a contenção (hoje: 1 de 5 treinos) | 100% |
| Ruído | % dos tokens do bloco gastos com entidades "0" | < linha de base |
| Negativos | bloco vazio / não incluído | ≥ 90% |
| Latência do retrieve | p95 | ≤ 50 ms |

**Calibragem:** busca em grade nos 6 pesos + `κ` + `τ_min` (alguns milhares de
combinações, segundos de CPU), separando 70% para calibrar e 30% para validar,
senão os pesos decoram o gabarito. Escolhe o maior Recall@orçamento com a
restrição de negativos ≥ 90%.

---

## §7. Fases

| Fase | O que | Muda produção? | Esforço |
|---|---|---|---|
| **F0** Dados | `has_member` plano → quinta; apagar o `has_member` do plano ABCDE → quarta (e a entidade, se for só resíduo). Soft delete + `pks_audit_log`, como as 6 relações de hoje | sim, dado (pedir OK) | 10 min |
| **F1** Gabarito + linha de base | golden.yaml + harness; medir o retriever **atual** | não | 1 dia |
| **F2** Ranking em sombra | `core/pks/ranking.py` (funções puras: consulta, sinais, nota, agrupamento, empacotamento), `ContextBuilder` com níveis, histórico chegando ao retriever. Flag desligada + sombra: roda os dois, injeta o velho, loga `explain` e diff (`entraria`/`sairia`) | só log | 2 dias |
| **F3** Ligar | depois do aceite da §6 e de 2–3 dias de sombra revisados; `search_pks` unificada (§4) | sim | 0,5 dia + observação |
| **F4** Semântica (opcional) | = F0/F1 do `memory-semantic-v3-plan.md` (embeddings e5 multilíngue) entrando como `f_sem`. **Só se o gabarito mostrar falha de paráfrase** que o léxico + conversa + grafo não pegam | sim | 3–5 dias |

**Encanamento da F2:**
- `PromptContextSelector.build_prompt` ganha `recent_messages`;
  `_handle_direct_response`, o SERVER_TOOL e o `preview_prompt` já têm o
  histórico e passam adiante;
- `PKSRetriever.retrieve` ganha `recent_messages` e, com a flag ligada, delega
  ao `ranking.py`;
- o `ContextBuilder` recebe os grupos já empacotados (sem `[:8]`).

**Testes:** unidade pura no `ranking.py`, sem banco: fixture com as 144
entidades exportadas (nomes, tipos, relações). Casos obrigatórios: os 3 turnos
de hoje (lista da semana com os 5 dias completos; "confere aí" com os 6 da
quinta; "treino de quarta" sem folhas soltas), orçamento nunca estourado, lista
completa ou "+N", negativo com bloco vazio.

---

## §8. Configuração

| Setting | Default | Fase |
|---|---|---|
| `pks_ranker_v2_enabled` | `False` | F2/F3 |
| `pks_ranker_v2_shadow` | `True` | F2 |
| `pks_rank_weights` (dict dos 6 pesos) | os da §3 até a calibragem | F2 |
| `pks_rank_min_score` (`τ_min`) | 0,12 → calibrado | F2 |
| `pks_rank_history_turns` | 3 | F2 |
| `pks_rank_list_cap` | 20 | F2 |

`context_budget_user_pks` (900) continua sendo o orçamento; o ranking passa a
respeitá-lo por dentro.

---

## §9. O que NÃO fazer

- **Não** subir o teto de 10 para 20: estoura os 900 tokens e o bloco some
  inteiro (D4).
- **Não** usar `updated_at` nem `last_used_at` como ordem principal. O
  `last_used_at` ainda cria um círculo vicioso: quem entra é tocado e volta a
  entrar.
- **Não** treinar modelo de ranking (LTR): 60 consultas não bastam. A soma
  ponderada explicável é o certo para este volume.
- **Não** colocar MMR/diversidade agora: o caso medido é contenção, não
  redundância. Só entra se o gabarito pedir.
- **Não** cortar lista em silêncio. Ou completa, ou "+N".
- **Não** ligar sem o gabarito revisado pelo Daniel.

## §10. Relação com o `memory-semantic-v3-plan.md`

Este plano substitui a F1.3 de lá ("fusão por RRF no retriever"): o ranking
daqui **é** o ponto de fusão, e a semântica vira mais um sinal (`f_sem`) em vez
de um segundo ranking fundido por RRF. A F0 de lá (embeddings) continua válida
e vira a F4 daqui. O gabarito da §6 é o começo da F6 de lá (benchmark
contínuo).


---

## §11. Implementação (2026-09-21)

### O que existe

| Peça | Onde |
|---|---|
| Ranking (funções puras: consulta, sinais, nota, agrupamento, empacotamento, explain) | `core/pks/ranking.py` |
| Índice do workspace em cache, invalidado por impressão digital | `PKSRetriever._workspace_graph` + `repository.workspace_fingerprint` |
| v2 / sombra no bloco do turno | `PKSRetriever.retrieve` → `retrieve_ranked` |
| `search_pks` pelo mesmo ranking (flag) e com relações (sempre) | `PKSService._search_ranked`, `search_entities` |
| Falas anteriores até o retriever | `build_prompt(recent_messages=…)`, `AIService._recent_for_pks` (turno e SERVER_TOOL) |
| Um limiar só (D7) | `PromptContextSelector._score_pks` |
| Explain no session log | seção `PKS — ranking` (`AIService._format_pks_rank`) |
| Gabarito (50 turnos reais) | `tests/pks_ranking/golden.yaml` |
| Benchmark / grade / calibragem | `scripts/pks_ranking_bench.py` (`--grid`, `--search N`) |
| Testes | `tests/test_pks_ranking.py` (PKS sintético), `tests/test_pks_ranking_wiring.py` |

### Desvios do plano — todos vieram do gabarito

O primeiro v2 fiel à §3 deu **4/9 negativos** e perdeu "5 dias" e "meus
dispositivos". Cada ajuste abaixo consertou um caso medido:

1. **Frase de nome com tokens crus.** Tirando stopword/token curto antes, o
   apelido "Dia C" virava "dia" e "esse dispositivo" virava "dispositivo":
   "5 dias" e "meus dispositivos" casavam como NOME e o corte relativo
   derrubava o resto. Frase feita só de stopword ("me", "eu") é ignorada.
2. **Identidade × conteúdo.** BM25 separado para nome/apelido/tipo/vizinhos e
   para o texto dos fields; conteúdo vale 0,6. "dois" casava com o texto de um
   dossiê em "quanto é dois mais dois".
3. **Substantivo do tipo no documento** (`_TYPE_WORDS`, lista curta — não os
   apelidos da ontologia, que têm "monitor"/"teclado" para qualquer device).
4. **Stopwords de domínio**: "neo", saudações e verbos de pedido.
5. **O prior não decide** se entra (o limiar é conferido sem ele).
6. **Dinheiro** (`looks_like_money_query`) → só as globais obrigatórias.
7. **Caminho/URL** viram o nome do arquivo antes de tokenizar
   ("/Users/daniel/…" trazia o usuário inteiro).
8. **Sombreamento de frase**: token solto que faz parte de uma frase maior
   casada por outra entidade vale metade ("daniel" em "daniel ferreira") e não
   conta como pedido de tipo ("treino" em "pré-treino").
9. **Hub sem nomes de vizinhos no documento** (> 12 relações): a entidade do
   usuário casava com tudo que ele possui. Tirar de TODOS piorou (os treinos
   ficaram iguais entre si); o corte por grau ficou no meio.
10. **Teto de 220 tokens por entidade** no nível completo; acima disso, compacto.
11. **Toda relação agrupada por tipo** numa linha (não só a contenção).
12. **Corte relativo** (`relative_cutoff` 0,45 da melhor nota) — não estava no
    plano; é o que faz "treino de quarta" trazer só a quarta.
13. **Fuzzy só na mensagem atual** e **índice em cache**: p95 78 → 13–17 ms.

### Resultado (50 casos, PKS real, só leitura)

| métrica | atual | v2 | aceite |
|---|---:|---:|---|
| recall das necessárias | 0,854 | **0,976** (40/41) | ≥ 0,90 ✅ |
| completude de lista | 0,879 | **1,0** | 100% ✅ |
| ruído | 0,619 | **0,526** | < atual ✅ |
| negativos | 8/8 | **8/8** | ≥ 90% ✅ |
| nDCG@10 | 0,584 | **0,84** | > atual ✅ |
| latência p95 | 73–129 ms | **13–17 ms** | ≤ 50 ms ✅ |

**Calibragem:** 300 amostras aleatórias de pesos não acharam nada melhor que os
pesos da §3; a grade `relative_cutoff × min_score` mostra fronteira limpa —
todo ponto com menos ruído perde recall. Ficaram os valores do plano.

### Limites conhecidos

- **Paráfrase** ("teclado barato na Amazon" → "Preferência de busca de produtos
  em loja online"): único must perdido. É o caso da F4 (embeddings).
- **Troca de assunto × anáfora:** com a fala IMEDIATAMENTE anterior sobre a
  quarta, "quanto é 2+2" ainda traz a quarta (peso 0,6). O ranking não sabe
  distinguir de "confere aí". Custo limitado; os negativos reais passam.
- **Ruído restante** é quase todo "os outros dias" em conversa sobre a semana.
- **Dado órfão** aparece como está: exercícios sem treino (os da quarta antiga)
  entram soltos quando se pergunta por exercícios.

### Para ligar (F3)

1. Daniel revisa `tests/pks_ranking/golden.yaml` (must/useful de cada caso).
2. Rodar de novo: `docker exec -w /app neo-core python scripts/pks_ranking_bench.py`.
3. Ler a seção `PKS — ranking` de alguns session logs reais (sombra).
4. `.env` da RAIZ: `PKS_RANKER_V2_ENABLED=true` + `docker compose up -d --no-deps neo-core`.
   Reverter = tirar a flag + o mesmo `up -d`.

---

## §12. Orçamento elástico (2026-09-21)

**Pergunta:** o limite de 10 é pouco? Aumentar, ou deixar se ajustar sozinho?

**O teto de 10 só existe no caminho antigo.** No v2, quantas entidades entram é
decidido pela nota (limiar + corte relativo). Nos 2150 turnos reais: mediana 2
grupos, p90 8, p99 **22**, máx 32. O que sobra de limite é o orçamento de tokens.

**Medido (2150 turnos reais, 1221 com bloco):** tokens para caber TUDO que é
relevante — mediana 117, p90 550, p95 703, p99 1169, máx 1739. O fixo de 880
apertava em 2,8% dos turnos. Mas o que ele cortava era quase só cauda: **101
grupos fracos (< 70% da melhor nota) × 14 fortes**. No gabarito, orçamento
acima de 880 não sobe o recall (fica em 0,976) — só o ruído (0,526 → 0,552).

Então "mais orçamento para todos" traz a cauda junto. O ajuste automático tem
que ser pela **força** da relevância, não pela quantidade:

- **base** para todo grupo;
- **extensão até o teto** só para grupo forte (nota ≥ `strong_ratio` × a melhor);
- o seletor aceita o teto quando o v2 está ligado (senão o bloco estendido
  passaria do orçamento e sumiria inteiro — o D4).

| política | recall | completude | ruído | tokens médios | p95 | fortes cortados | cauda a mais |
|---|---:|---:|---:|---:|---:|---:|---:|
| fixo 880 | 0,976 | 1,0 | 0,526 | 206 | 703 | 14 | 0 |
| fixo 1600 | 0,976 | 1,0 | 0,552 | 213 | 703 | 0 | 101 |
| elástico 300→1600 (0,7) | 0,976 | **0,882** | 0,446 | 165 | 412 | 0 | 0 |
| elástico 500→1600 (0,7) | 0,976 | 0,988 | 0,470 | 189 | 495 | 0 | 1 |
| **elástico 600→1600 (0,7)** | **0,976** | **1,0** | **0,479** | **196** | **589** | **0** | 1 |
| elástico 700→1600 (0,7) | 0,976 | 0,988 | 0,514 | 201 | 675 | 0 | 2 |
| elástico 600→1600 (0,6) | 0,976 | 1,0 | 0,484 | 200 | 590 | 0 | 31 |

Escolhido **600 → 1600, forte ≥ 0,7** (`pks_rank_budget_base`,
`pks_rank_budget_max`, `pks_rank_strong_ratio`): mesmo recall, lista completa,
menos ruído e menos token que o fixo, e nenhum grupo forte cortado. Base menor
que 600 quebra lista (o treino entra só com o nome); "forte" em 0,6 deixa a
cauda voltar.

**Sinal para o futuro:** o log `PKS — ranking` já marca o que ficou "sem
orçamento". Se o Neo chamar `search_pks` por uma entidade que o turno cortou,
é medida direta de que o orçamento apertou de verdade — vale contar isso antes
de mexer de novo no teto.
