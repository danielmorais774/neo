# Por que o Neo não reencontrava a própria resposta

**Caso:** sessão `7330e40c-1327-4ac2-a9ca-11502ffa3c0a` (19/09/2026).
**Diagnóstico:** não foi limite do LLM. A sessão inteira tinha **14.498 tokens**
— cabe folgada em qualquer modelo que usamos. O texto nunca foi entregue ao
modelo.

## O que aconteceu

Na interação 6 o Neo produziu a "Reanálise Completa": mensagem `e5d78d60`,
12.811 chars = **3.982 tokens**, salva no banco com embedding, íntegra.

Da interação 8 em diante ela ficou **matematicamente inalcançável**. Nas 13
interações seguintes o usuário pediu esse texto de volta de nove jeitos
diferentes. O Neo respondeu que ele não existia, depois passou a ler arquivos
do disco e a apresentá-los como "a mensagem original que eu postei no chat" —
inclusive gravando no disco a análise ERRADA (a primeira, de 01:51) sob o nome
`reanalise-completa-brasileiro-privado.md`.

Cinco travas em série, nenhuma delas do modelo.

---

## §1 — A camada RETRIEVED era inalcançável por construção

`core/memory/hybrid_search.py`, `shared/config.py`

O RRF cru vale `1/(K+rank+1)`. Com `K=60`, o melhor caso possível (rank 0) é
`1/61 = 0,0164`. Com os pesos default:

```
teto = 0,5·0,0164 + 0,3·0,0164 + 0,2·1,0 = 0,2131
context_relevance_threshold                = 0,25
```

**O máximo possível era menor que o corte.** Nenhuma memória recuperada podia
entrar no prompt, nunca, para nenhum usuário. Medido nesta sessão: **295
candidatos, 295 descartes `below_threshold`, zero aprovados.**

Segundo efeito: com o RRF esmagado em 0,016 e o temporal valendo 0,2 inteiros,
**94% da nota era recência** e a relevância virava ruído — o resultado nº 1 era
sempre a mensagem que o usuário tinha acabado de mandar.

**Correção:** normalizar o RRF por `(K+1)` (rank 0 → 1,0), devolvendo o
`final_score` a um 0..1 real em que os pesos significam o que dizem significar.
Junto, `exclude_session_id`: esta camada é memória ENTRE conversas, e a de agora
já vai inteira pelo histórico.

Medido depois: 4/4 acima do threshold, scores 0,68–0,70, zero vazamento da
sessão atual.

## §2 — O orçamento de histórico era menor que uma resposta do próprio Neo

`core/session/session_manager.py`, `modules/desktop/ai_service.py`

Dois defeitos somados:

1. **`max_tokens_budget=3000` cravado no código** contra uma mensagem de 3.982
   tokens: ela não cabia **nem sozinha**.
2. **`break`, não `skip`.** Ao topar com a mensagem grande, a varredura parava —
   e levava junto toda a conversa mais antiga, não só a mensagem grande.

Conferido contra o log: na interação 13 a janela tinha 10 mensagens somando
2.676 tokens; a próxima (560) daria 3.236 > 3.000 → corte. Para a reanálise ser
alcançável ali, o orçamento precisaria de ≥ 7.257 tokens.

Havia um terceiro defeito, mais silencioso: o bloco de **resgate semântico
dentro da sessão** (`fetch_similar_session_messages`) ficava atrás de
`if current_embedding is not None`, e o único chamador de produção passava
`None` cravado — porque o embedding da mensagem só nasce depois do `add_message`
que roda em paralelo com o `get_history`. **Nunca rodou uma vez em produção.**

**Correção:**
- tetos viram settings: `history_limit_messages=40`,
  `history_max_tokens_budget=24000`;
- a mensagem que não cabe vira um **marcador de omissão** (`history_max_elisions`)
  e a varredura continua para trás. O marcador custa ~40 tokens e avisa o modelo
  de que falta algo ali — era o silêncio que o fazia concluir que a mensagem
  nunca existiu;
- o `ai_service` faz um **segundo passe** com o embedding em mãos, ressuscitando
  o resgate semântico (piso da sessão baixou de >30 para 12 mensagens).

**Custo:** o histórico vai em dois prompts por turno. O classificador já fatia em
`[-6:]`, então o aumento cai só no cognitivo — que é onde precisa cair.

## §3 — `conversation_recall` excluía justamente a sessão atual

`core/memory/conversation_recall.py`, `modules/conversation_recall/`

A tool passava `exclude_session_id=self._session_id`. A reanálise estava na
MESMA sessão. Ainda que fosse chamada, retornaria vazio.

O motivo original é legítimo ("sem isso a própria pergunta casa com a busca e a
resposta fica circular") — mas a cura matou o caso mais comum de todos: pedir de
volta algo que o Neo escreveu hoje, nesta conversa, longe demais para caber na
janela.

**Correção:** `current_session_before`. A sessão atual volta a ser pesquisável,
cortada no timestamp da mensagem que abriu o turno — a pergunta de agora fica
fora (sem circularidade), todo o resto fica ao alcance. O mesmo corte vale para
os trechos. A sessão de agora vem rotulada **ESTA CONVERSA** no bloco.

**E os trechos de 240 chars não bastavam.** Eles respondem "a gente já falou
disso?", mas não "me mostra aquela análise de novo" — com o texto cortado, a
única saída do modelo é reescrever de cabeça, que é a confabulação que se quer
evitar. Daí o parâmetro `completo=true`: as mensagens que casaram vêm inteiras
(as vizinhas seguem curtas), com teto por mensagem e **orçamento somado**
(`conversation_recall_full_total_chars`) para não estourar o prompt.

## §4 — Nenhum gatilho cobria "o que VOCÊ escreveu"

`core/memory/recall_triggers.py` (extraído de `modules/desktop/ai_service.py`)

Os 11 padrões existentes cobriam só o lado do usuário — "eu te falei", "a gente
conversou". As nove frases desta sessão eram todas sobre o que o **Neo** tinha
escrito: "vc me falou aqui pelas mensagens", "tem uma que vc fez uma reanálise
final e me mostrou", "olha no seu histórico de conversa", "olha a mensagem que
vc mesmo escreveu". **Zero casaram** — a tool nunca virou obrigatória.

Dois furos menores no mesmo lugar:
- o padrão de rejeição pedia "não **foi/era** isso" e ele escreveu "não **é**
  esse";
- a continuação só valia **com cursor**, e a primeira rejeição da série acontece
  antes de existir qualquer busca — então não fazia nada.

**Correção:** padrões para o lado do assistente, para referência no passado sem
verbo de fala ("tinha uma tabela do pior para menor pior") e para a rejeição no
presente; e fallback para o nudge de busca quando a rejeição chega sem cursor.

**Medido contra o corpus real** (2.053 mensagens do usuário): **9/9** das frases
da sessão passam a casar, com **2,9%** de disparo no corpus — ver
`tests/test_recall_triggers.py`.

Os padrões saíram do `ai_service` para um módulo próprio porque o
`intent_router` precisa da mesma decisão (§5), e divergir entre os dois reabre o
buraco.

## §5 — O classificador atalhava o pedido antes do cérebro existir

`core/router/intent_router.py`

"pega a última reanálise completa que fizemos e coloca tudo em um arquivo .md"
virou `save_generated_file` apontando um `resumo_fichas.md` de **outra
conversa**, com confiança **0,95** e uma explicação inventada ("o histórico
mostra que este arquivo já existe lá"). O cérebro — o único que sabe chamar
`conversation_recall` — nunca rodou.

O classificador vê no máximo 6 mensagens de histórico. Ele não tem como saber se
"a última reanálise" é um arquivo do catálogo ou um texto de 11 turnos atrás.
Quem sabe é quem pode buscar.

**Correção:** `_reject_recall_shortcut` — `CLIENT_TOOL` de
`save_generated_file`/`write_file` cujo pedido se refere a algo dito antes cai no
cognitivo. Entregar arquivo continua valendo; só depois de o cérebro recuperar o
conteúdo certo.

---

## A camada de prompt: a regra que mentia

`core/agent/context_selector.py`, regra 12

> "Você NÃO tem as conversas antigas no contexto — **só as mensagens desta
> sessão**"

Isso era **falso**. Ele tem uma *janela recente* desta sessão. O prompt garantia
ao modelo uma completude que a implementação não entregava — então, ao não achar
a reanálise, ele concluía logicamente que ela não existia como mensagem e devia
ser um arquivo. A confabulação era induzida.

A regra 12 foi reescrita ("o que chega até você é uma JANELA… não achar algo no
contexto não significa que aquilo não existe") e ganhou a **12b**: texto que você
escreveu no chat e arquivo no disco são coisas diferentes; nunca apresente o
conteúdo de um arquivo como "a mensagem que eu postei".

## Verificação contra a sessão real

| | antes | depois |
|---|---|---|
| mensagens na janela | 10 | 40 |
| janela contém a Reanálise | ❌ | ✅ |
| resgate semântico na sessão | nunca rodou | ✅ |
| `conversation_recall` acha na sessão atual | ❌ (excluída) | ✅ |
| texto devolvido inteiro (`completo=true`) | — | ✅ |
| RETRIEVED acima do threshold | 0/295 | 4/4 |
| gatilhos que casam com as 9 frases | 0/9 | 9/9 |
| `find_similar_sessions` | sempre `[]` (erro engolido) | 5 sessões, sim 0,607 |
| camada `session` no resultado final | impossível (teto 0,246) | 1º lugar, 0,695 |

## §6 — A busca por SESSÃO estava morta em dois níveis

`db/async_client.py`, `core/memory/hybrid_search.py`,
`alembic/versions/a0b1c2d3e4f5_topic_embedding_bytea_to_vector.py`

Achado de passagem ao investigar o §1, e é a mesma família de defeito: dois
problemas empilhados, os dois silenciosos.

**Nível 1 — o banco não conseguia comparar.**
`conversation_session.topic_embedding` estava como **`bytea`**, enquanto
`conversation_message.embedding` é `vector(384)`. Toda query fazia
`topic_embedding <=> CAST(:embedding AS vector)` e morria com
`operator does not exist: bytea <=> vector`. O `except` devolvia `[]` com um
`logger.warning` que parecia "pgvector não instalado" — a busca por sessão nunca
devolveu nada, nos dois caminhos que a usam, e ninguém viu.

As migrations sempre criaram a coluna como `vector`; a tabela em produção nasceu
fora do alembic (o `session_metadata` também diverge — é `jsonb`, a migration
pede `json` — e nenhum dos índices da migration existia). O alembic foi
carimbado por cima.

O dado era recuperável: o caminho de escrita grava
`json.dumps(vetor).encode("utf-8")`, que é exatamente o literal textual do
pgvector. Medido antes de aplicar: **90/90 linhas convertem, todas com 384
dimensões**; erro máximo de `7,4e-09` em 34.560 elementos (arredondamento de
float32, que é o que o pgvector guarda de qualquer jeito).

**Nível 2 — mesmo achando, a sessão não entrava no prompt.**
Sessões entram no dicionário de candidatos com a chave `session-<id>`, mas os
rankings de RRF são indexados por id de MENSAGEM. Toda sessão caía no rank
default 999 e ficava só com o termo temporal: teto de ~0,246 contra o corte de
0,25. Consertar só o banco faria a busca voltar a achar sessão — e ela
continuaria sem conseguir ser usada.

**Correção:**
- migration `a0b1c2d3e4f5` converte `bytea → vector(384)` preservando o dado,
  recria o índice HNSW e é no-op onde a coluna já está certa;
- `find_similar_sessions` passa a detectar o tipo da coluna (o mesmo detector
  que o `update_session` já usava para ESCREVER), funciona nos dois formatos,
  **invalida o cache do tipo em caso de erro** (para se curar se a migration
  rodar com o processo de pé) e loga em **ERROR**, não warning;
- `_apply_scoring` recebe `session_results` e usa a ordem devolvida pelo banco
  como ranking vetorial das sessões.

Verificado em produção: `find_similar_sessions` devolve 5 sessões
(0,607 / 0,586 / 0,582 / …) e a camada `session` passou a aparecer no resultado
final do `hybrid_search`, com o primeiro lugar geral em 0,695.

### Ordem de deploy

A migration troca o tipo da coluna e o processo **cacheia esse tipo em memória**.
Rode `alembic upgrade head` e **reinicie o `neo-core`** em seguida. Se esquecer,
o caminho de leitura se cura sozinho na chamada seguinte (invalida o cache no
erro), mas o `update_session` só acerta depois do restart.

## Pendência conhecida

A tabela `conversation_session` em produção não tem os índices que a migration
`a1b2c3d4e5f6` declara (`status`, `last_activity_at`, `user_id`) — consequência
de ela ter nascido fora do alembic. Com 675 linhas não faz diferença hoje;
vale uma migration de acerto quando alguém for mexer nisso.

---

# Rodada 2 — 2026-09-20: recall entre sessões

A rodada anterior consertou o caso **dentro da mesma sessão**. A sessão
`94805b40` mostrou que o caso **entre sessões** continuava quebrado, e por
motivos diferentes.

O usuário pediu de volta a "Reanálise Completa" (mensagem `e5d78d60`, 12.811
chars, na sessão `7330e40c`) e ditou a ordem do ranking para desambiguar. O Neo
respondeu que aquela versão *"apareceu na conversa mas nunca foi salva em
arquivo"* — as duas afirmações falsas. Varredura do log: **zero** marcadores
exclusivos daquela mensagem (`"Dark Horse"`, `"2,5/10"`, `"Tabela Final"`). Ele
nunca viu o texto; escreveu a resposta por inferência.

Sem a fonte, no turno seguinte ele obedeceu à ordem ditada de memória (errada em
duas posições) e **fabricou as notas** para preencher — Delta recebeu `5,5/10`,
que não existe em versão nenhuma. O arquivo no disco do usuário ficou com dados
inventados sob a assinatura "gerada com base na leitura integral dos arquivos".

## Os dois trilhos falharam por motivos independentes

### 1. O recall cortava os acertos pelos mais ANTIGOS

`_pick_excerpts` montava `hit_idx` varrendo as mensagens em ordem cronológica e
cortava com `hit_idx[:per_session]`. Numa conversa que evoluiu, sobravam as
primeiras versões. Medido na sessão real: **31 acertos, a Reanálise era o 6º**,
com `per_session=4`. Perdeu por duas posições.

Pior: o cabeçalho diz "Mostrando as 3 mais recentes" (verdade para *conversas*,
o oposto do que fazia *dentro* de cada uma), 20 dos 24 acertos sumiam sem
marcador, e o rodapé ainda mandava *"diga que no arquivo não existe"*.

**Corrigido** com `_rank_hit_indices()`: ordena por quantos termos distintos a
mensagem casa e, no empate, pela mais recente. A ordem cronológica volta na
apresentação. Além disso:

- `completo=true` parou de achatar as quebras de linha (`" ".join(split())`
  transformava a tabela markdown numa linha só, e a única saída do modelo era
  reescrevê-la de cabeça — a confabulação que a flag existe para evitar);
- o bloco passou a **avisar quantos acertos ficaram de fora** e a ensinar o novo
  parâmetro `conversa="7330e40c"`, que abre o resto de uma conversa já
  identificada (`before` só andava para conversas mais antigas);
- o rodapé só autoriza negar quando **nada** ficou oculto;
- só o acerto *escolhido* recebe texto integral (antes, uma vizinha que casasse
  o termo queimava o orçamento).

### 2. O embedder trunca em 256 tokens, em silêncio

`embedding_provider` é **local** (`all-MiniLM-L6-v2`), e o modelo tem
`max_seq_length = 256`. O `sentence-transformers` corta sem erro, warning ou
marcador: o vetor de uma mensagem longa representa só a abertura dela.

A Reanálise tem a tabela no offset **9.399** de 12.811 chars — 9x além do que o
vetor enxergava. Resultado medido: **rank 83 de 2.304**, atrás da versão antiga
e superada do mesmo texto (0,5457 contra 0,7071).

Atinge 133 de 4.526 mensagens embeddadas (2,9%) — poucas em contagem, mas são
exatamente as análises longas que o usuário pede de volta.

**Corrigido** com `conversation_message_chunk` (migration `b1c2d3e4f5a6`): cada
pedaço cabe na janela do modelo e ganha vetor próprio. `find_similar_messages`
faz `UNION` dos dois e agrega por `max(similarity)`, devolvendo a **mensagem** —
assinatura e retorno idênticos, nada a jusante mudou.

#### Calibração: por que 500/250

Medir num exemplo só engana. Com **uma** query, `900/450` parecia o melhor
(0,7334) — era alinhamento sortudo da fatia com a tabela. Reindexando o corpus
inteiro em cada config e medindo o rank do alvo contra **5 pedidos reais** que o
usuário fez por essa mensagem (menor é melhor):

| config | rank médio | ranks |
|---|---|---|
| sem chunk | 483,0 | 634 380 903 415 83 |
| 1200/300 | 235,6 | 251 394 171 356 6 |
| 900/450 | 228,8 | 108 390 231 414 1 |
| 650/325 | 163,2 | 178 149 145 335 9 |
| **500/250** | **73,6** | 109 41 116 93 9 |
| 400/200 | 98,2 | 124 40 179 145 3 |
| 300/150 | 60,0 | 46 115 86 48 5 |

O tokenizer gasta **~2,9 chars/token** em pt-BR: 1.200 chars viram ~416 tokens e
**todo** pedaço era truncado de novo pelo modelo — o mesmo defeito um nível
abaixo. 500 chars ≈ 169 tokens, com folga.

`300/150` tem rank médio um pouco melhor, mas o eval cobre o recall de **um
alvo** e não mede ruído; medindo quanto do top-5 de 40 queries aleatórias passa
a ser ocupado por mensagens longas (2,9% do corpus): sem chunk **0,0%**,
`500/250` **9,5%**, `300/150` **12,5%**. Sem ganho comprovado que justifique a
sobre-representação extra, ficou `500/250`.

O `0,0%` é o achado por trás de tudo: antes do chunking, **nenhuma mensagem
longa entrava no top-5 de busca nenhuma**.

### Backfill

    docker exec neo-core python3 scripts/backfill_message_chunks.py [--dry-run] [--force]

Rodado em produção: **1.162 pedaços de 133 mensagens**. O `--force` reindexa o
que já tem pedaços (usado na calibração); `--size`/`--overlap` sobrescrevem a
config.

## Validação

- `conversation_recall` real contra o banco de produção, com o pedido original:
  a Reanálise volta **integral na primeira chamada** (24.866 chars de bloco, 306
  quebras de linha preservadas, tabela verbatim com Alfa em 2º e Bravo em 3º).
- 2.558 testes passando, mais 17 novos em `tests/test_message_chunking.py`.
  As 4 falhas restantes (`tests/birra/*`, `tests/scheduler/test_defaults_off`)
  são config drift pré-existente — confirmado revertendo os arquivos desta
  rodada.

## Pendências

- A busca vetorial melhorou muito mas não é suficiente sozinha para pedidos
  **vagos** ("tem uma que vc fez uma reanálise final e me mostrou"): esses ficam
  na casa do rank 90-116 mesmo com chunking, porque a query não carrega os
  termos do conteúdo. Quem resolve esse caso é o `conversation_recall` (FTS),
  que agora funciona. Os dois trilhos se complementam — nenhum cobre tudo.
- `index_message_chunks` roda em background por mensagem; mensagens gravadas
  fora de `add_message` (se houver) não ganham pedaços até um backfill.

---

# Rodada 3 — 2026-09-20: o desempate estava errado

Sessão `cbff8f81`. O turno 1 **funcionou**: o recall achou a mensagem e o Neo
citou as notas certas (`0,5 / 2,5 / 4,0 / 5,5 / 6,0 / 7,0`). Mas terminou
perguntando em vez de entregar. No turno 2 ("monta a tabela aqui de novo") ele
foi ler o **arquivo do disco** e mostrou as notas fabricadas na sessão anterior
(Alfa 4,0). No turno 3, quando o usuário apontou o erro, ele **negou o 2,5 que
ele mesmo tinha citado dois turnos antes** e voltou para a versão de sexta.

## O defeito principal veio da rodada 2

`_rank_hit_indices` desempatava pela mensagem **mais recente**, com a
justificativa "numa conversa que se corrige, a última é a que vale". Medido com
os termos reais do turno 3 (`reanálise, completa, pior, brasileiro, privado,
tabela`) contra a sessão 7330e40c:

| termos | quando | tamanho | mensagem |
|---|---|---|---|
| 6/6 | 20/09 01:58 | 398 ch | "eu tinha invertido a tabela…" |
| 6/6 | 19/09 22:42 | 5.987 ch | "tá aqui a mensagem original…" |
| 6/6 | 19/09 22:40 | 255 ch | "agora a tabela está corrigida" |
| 6/6 | 19/09 22:37 | 445 ch | "o arquivo já está salvo…" |
| 6/6 | 19/09 02:02 | **12.811 ch** | **a Reanálise Completa** |

Cinco empatadas em 6/6. Pela recência, a Reanálise cai em **5º** — atrás de
quatro recados curtos que eram justamente as tentativas ERRADAS do Neo. Com
`per_session=4`, fica de fora por uma posição.

**O critério que faltava é substância:** quem pede um texto de volta quer o
texto, não a menção a ele. Uma mensagem de 255 chars dizendo "a tabela está
corrigida" não é a tabela. E a intenção já estava codificada no parâmetro:

- `completo=true` (quer VER) → desempata pela mensagem **maior**;
- sem ele (quer SABER se falaram) → desempata pela **mais recente**.

Com isso a Reanálise passa a ser o **primeiro** trecho escolhido, integral.

## O orçamento ia para a conversa errada

`_fill_excerpts` gastava o orçamento dos trechos longos da sessão **mais
recente** para a mais antiga. A sessão mais recente é sempre a de AGORA — cujo
conteúdo o modelo já tem no histórico. No turno 3 isso devolveu ao modelo, como
"memória recuperada", a resposta **errada** que ele mesmo dera dois turnos
antes, e ele a confirmou contra o original.

Agora o gasto é por **relevância** (`best_rank`), com a sessão atual **por
último**. Ela continua elegível (pode ter turnos fora da janela de histórico),
mas é a primeira a ficar sem orçamento, não a última.

Um teto explícito de 20% para a sessão atual foi implementado e **removido**:
medido, não mordia (ela usava 4.134 de 8.000 disponíveis). A ordem de gasto
sozinha resolve; o teto era complexidade sem efeito.

## O gatilho não pegava "monta … de novo"

O padrão exigia o pronome colado ao verbo (`mostra isso de novo`) e deixava
passar `monta a tabela aqui de novo para eu ver`. Novo padrão com âncora dupla
(verbo + artefato **e** "de novo"), que é o que segura o falso positivo:
`monta uma tabela nova com esses dados` e `monta a tabela` continuam sem
disparar. Taxa no corpus real: **2,9% → 3,1%** (2.116 mensagens), todos os
disparos novos legítimos.

## Pendências desta rodada

- **Vazamento de prompt.** A resposta do turno 2 começou com
  `" O sha256 do arquivo está AQUI — reuso na mesma resposta é seguro.\`"` — o
  modelo **emendou no system prompt** em vez de responder. A última mensagem do
  contexto era o system `## ARQUIVO LIDO DO DISCO DO USUÁRIO`, injetado depois
  do resultado da tool. Esse texto foi salvo no banco como resposta do
  assistente e voltou no recall do turno 3. Conserto provável: não deixar um
  bloco system como última mensagem do contexto.
- **O turno 1 perguntou em vez de entregar** ("Quer que eu confira… ou quer que
  eu reescreva?"), contra a regra 8. Se tivesse entregado a tabela ali, os
  turnos 2 e 3 não existiriam.
- **O eco continua possível:** a resposta errada do turno 2 ainda aparece no
  bloco (datada e marcada como ESTA CONVERSA), lado a lado com a verdadeira.
  Agora o modelo ao menos vê as duas; antes via só a errada.
