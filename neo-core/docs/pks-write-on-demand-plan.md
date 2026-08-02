# PKS — Escrita Sob Demanda via Subagente de Memória

**Status:** **TODAS as fases (0–5) EM PRODUÇÃO** (2026-07-24), validadas E2E.
Restam só enhancements (dedup no resolver, expire_relation). Ver §9.
**Origem:** `session-d41ed0fa-1803-429b-affd-395451cebb75` (VPS, `neo-core:/app/logs/`), 2026-07-24.
**Relacionado:** [`memory-semantic-v3-plan.md`](./memory-semantic-v3-plan.md) (complementar, **ainda não implementado** — ver §3).

---

## 1. O que aconteceu (caso real)

Usuário, numa conversa sobre bebida pré-treino sem cafeína (suco de uva + água
com gás), pediu: **"salva essa ideia para quando eu pedir de novo"**.

- **Turno 1:** o classificador de intent roteou como `DIRECT_RESPONSE` /
  `needs_memory: true`, com a explicação *"o Neo salva automaticamente no PKS
  quando o usuário pede para registrar algo. Não precisa de tool."* O
  cognitivo (o LLM que fala com o usuário) só tem `search_pks` — leitura.
  Buscou, não achou nada, e respondeu honestamente que não tinha como salvar.
  Comportamento correto dado o design atual, mas o pedido do usuário **não
  foi cumprido**.
- **Turno 2:** o usuário insistiu ("*tem um workspace do usuário com meu
  nome, tenta de novo*"). Um pipeline em background **escreveu algo no
  PKS** — só que o que foi escrito é lixo: o texto cru da própria mensagem do
  usuário ("voce consegue salvar lá no pós...") virou um item de
  `stable_facts` no perfil `daniel-profile`. A receita da bebida (o conteúdo
  que devia ter sido salvo) nunca entrou.

O resultado é pior que "feature não existe": o perfil do usuário ficou
poluído com uma frase de comando sem sentido, que agora é injetada em **todo
prompt futuro** (`daniel-profile.stable_facts` entra no system prompt de
cada turno).

## 2. Causa raiz (confirmada lendo o código, não só o log)

O sistema **já tem** um subagente de memória bom — o `PKSMemoryPlanner`
(`core/pks/memory_planner.py`): LLM próprio (deepseek, temp 0.0), prompt
próprio que conhece a ontologia inteira (tipos de entidade, relações, regras
de domínio), resolve entidade canônica, e **já tem a regra de exceção certa**
para este caso (linhas 50-52 do prompt):

> *"se a mensagem atual for uma confirmação explícita para salvar/lembrar algo
> ('salva na memória', 'guarda isso', 'lembra disso'), você pode persistir o
> conteúdo estruturado da resposta anterior do assistente que o usuário
> acabou de confirmar."*

Ou seja: a capacidade de fazer a coisa certa **existe**. O bug é de
**roteamento e de inputs**, não de falta de inteligência. Três falhas:

**(a) O planner nunca é chamado de forma síncrona/sob demanda.**
Ele só roda em dois modos, ambos best-effort e desacoplados do pedido: no
pré-resposta síncrono (`learn_before_response`) e no pós-resposta
fire-and-forget (`_bg_pks` → `learn`). Nenhum é acionado *porque o usuário
pediu explicitamente pra salvar* — rodam em toda mensagem, adivinhando.

**(b) Quando o planner roda no fluxo `user`, recebe o input errado.**
No `_bg_pks` pós-turno, o `assistant_response` passado ao planner é a resposta
do **turno atual** (a recusa "não tenho como salvar"), não o turno anterior
(a receita). E o histórico recente vai truncado em ~500 chars. Então mesmo o
caminho que usa o planner nunca viu a receita como candidata a salvar.

**(c) O caminho de `system` scope ignora o planner e usa substring matching.**
`core/pks/writer.py`, `_build_system_updates` (linha 804) →
`_is_system_memory_intent` (linha 2022, dispara em `"salva"` como substring
de `"salvar"`) → `_infer_profile_properties` (linha 1370):

```python
def _infer_profile_properties(self, text: str) -> dict[str, Any] | None:
    normalized = normalize_token(text)
    profile_tokens = ("sobre mim", "meu perfil", "lembra de mim", "me chama", "meu nome", "eu sou")
    if not any(token in normalized for token in profile_tokens):
        return None
    entry = " ".join(text.strip().split())[:240]
    return {"stable_facts": [entry]}
```

`"meu nome"` casou dentro de "...com **meu nome**, tenta de novo" (falso
positivo por falta de word boundary), e a função devolveu a **mensagem
inteira do usuário, truncada em 240 chars**, como se fosse um fato. `text`
aqui é sempre a mensagem do usuário — nunca a resposta do assistente. Não há
LLM nenhum nesse caminho, só `if token in normalized`. Sem teste cobrindo
(`grep -rl stable_facts tests/` não retorna nada). Esse resultado ainda é
injetado de volta no prompt do turno (`core/agent/context_selector.py:509-521`,
seção `"## Escrita PKS do Sistema Confirmada"`), mandando o LLM confirmar o
salvamento — por isso o Neo "confirmou" algo sem sentido.

**(d) O classificador promete o que o sistema não cumpre.**
`core/router/intent_router.py:368` diz *"Memory directives (...) are saved
automatically to the knowledge store (PKS)."* O campo `needs_memory` não
aciona escrita nenhuma — só controla leitura de contexto
(`modules/desktop/ai_service.py:1640,3283`). O classificador acredita numa
garantia que não existe, e isso vaza pro tom da resposta.

**(e) Por design, o cognitivo não tem tool para acionar o planner.**
`core/cognitive/read_tools.py` é deliberadamente read-only
(`docs/cognitive-agentic-tools.md` §3). A única tool de PKS é `search_pks`
(leitura). Não existe via para o cognitivo dizer "o usuário confirmou, salva
isso" e disparar o planner na hora.

## 3. Por que o `memory-semantic-v3-plan.md` não resolve isso

Aquele plano (F0-F6, **também ainda não implementado**) melhora a **extração
automática best-effort**: embeddings semânticos, memória episódica,
consolidação noturna ("sleep-time compute"), memory blocks vivos. Mas em
nenhuma fase introduz uma via de **escrita síncrona sob demanda** — continua
no paradigma "o sistema tenta adivinhar o que salvar depois do turno". Os dois
planos são complementares: v3 resolve "o Neo lembra coisas que eu nunca pedi
pra lembrar"; este plano resolve "quando eu peço explicitamente, funciona na
hora, com o conteúdo certo, no lugar certo".

## 4. Arquitetura proposta: o planner vira o Subagente de Memória oficial

A ideia central (do Daniel): **não reinventar escrita, e não escrever cru.**
O `PKSMemoryPlanner` é elevado a **único responsável por decidir COMO e ONDE
persistir** — porque o PKS é complexo e tem lugares certos (ontologia,
entidade canônica, relações). Ele já tem pipeline e prompt próprios pra isso.
O que muda é *quem o chama, quando, e com quais inputs*.

Divisão de trabalho clara, em duas cabeças:

- **Cognitivo (ou intent_router) decide O QUÊ** — entende o pedido, resolve a
  deixis ("essa ideia" = a receita de uva do turno anterior) e formula em
  linguagem natural o que deve ser lembrado. Ele tem a conversa inteira; é a
  única parte que sabe a que "isso" se refere.
- **Subagente de Memória (o planner) decide COMO/ONDE** — recebe esse
  conteúdo + o contexto PKS atual e produz `PKSOperation` estruturadas
  (tipo de entidade correto, campos, relações, sensitivity), validadas contra
  a ontologia. Nunca grava texto cru; sempre estrutura.

O cognitivo nunca fala com o banco direto. Ele chama uma **tool fina**; por
baixo, quem trabalha é o planner (que por sua vez já cai no
`PKSWriter._apply_operation`, com os guards de ledger/credencial/confidence
que já existem). Isso mantém o cognitivo read-only na filosofia (ele não
escreve — ele *delega* a decisão de escrita a um especialista), e reaproveita
100% do pipeline de aplicação já testado.

```
usuário: "salva essa ideia"
   │
   ▼
cognitivo  ── resolve "essa ideia" = receita de uva; formula o conteúdo
   │           chama tool pks_remember(content="Pré-treino sem cafeína: suco de uva...")
   ▼
PKSWriteExecutor (thin)  ── monta os inputs certos e chama, SÍNCRONO:
   │                         PKSWriter.learn(memory_target="user",
   │                             assistant_response=<content>, user_message="salva isso")
   ▼
PKSMemoryPlanner  ── (modo confirmado) decide ontologia: entity type, campos, relações
   │                  → [PKSOperation, ...]
   ▼
PKSWriter._apply_operation  ── guards (ledger/credencial/confidence) + commit
   │
   ▼
tool retorna echo do que salvou → cognitivo confirma citando o conteúdo real
```

### 4.1 Quem dispara — o LLM decide, não uma regra determinística

Ponto inegociável (do Daniel): **não podemos depender de uma função
determinística pegar a intenção do usuário.** "salva essa ideia para quando eu
pedir de novo" não bate em keyword nenhuma — nem no regex do router
(`_MEMORY_DIRECTIVE_PATTERNS`), nem no phrase-gate da regra de exceção do
planner. Se a detecção fosse só por regra, esse pedido (e infinitas variações)
continuaria caindo no vazio. Logo, o acionamento tem **duas camadas**, e a do
LLM é a que dá robustez:

1. **Camada LLM (principal).** O cognitivo vê a conversa inteira em todo turno
   `DIRECT_RESPONSE`. Entendendo que o usuário quer guardar algo — por
   qualquer formulação — ele **decide** chamar `pks_remember`. Essa decisão é
   soberana sobre a intenção: quando a tool é chamada, o "se salva" já está
   resolvido. É por isso que o planner roda em **modo confirmado** (§Fase 1) e
   não re-julga — senão a decisão do LLM seria anulada por um gate de frase
   logo abaixo dela, que é o bug de origem invertido.
2. **Camada determinística (backstop).** O regex do router pega os casos
   óbvios e explícitos ("lembra que...", "registra no PKS") e **garante** o
   acionamento mesmo se o cognitivo, por algum motivo (modelo fraco, turno
   atípico), não chamar a tool. É rede de segurança, não a fonte da verdade.

As duas convergem no mesmo `pks_remember` → planner em modo confirmado. A
determinística nunca deve *bloquear* o que a camada LLM libera; só *adicionar*
cobertura.

## 5. Objetivo

Cumprir um pedido explícito de "salva isso" **no mesmo turno**, síncrono, com
o conteúdo certo e no lugar certo da ontologia — delegando a decisão de
escrita ao Subagente de Memória (planner), não a heurística de substring nem a
escrita crua. A extração automática best-effort continua existindo em
paralelo como rede de segurança para fatos implícitos.

**Non-goals:** não reescreve o planner (ele já é bom — só passa a ser chamado
direito); não escreve conteúdo cru sem passar pela ontologia; não abre
escrita livre em `personality`/`response-rules`/`procedures` (continuam só via
treino explícito/canonicalizer); a extração automática pós-turno fica pra v3.

## 6. Plano de correção

### Fase 0 — Higienização (curto prazo)

1. Corrigir os matches de substring sem word boundary em
   `_is_system_memory_intent` e em todas as `_infer_*_properties`
   (`core/pks/writer.py`) — regex com `\b` ou tokenização por palavra.
2. Regra dura: nenhuma heurística de `_infer_*_properties` pode persistir o
   **texto cru do comando do usuário** como fato. Um comando ("salva isso")
   não é fato. Idealmente, quando houver intent de memória explícito, esse
   caminho `system` **cede** para o planner (Fase 1) em vez de tentar inferir
   sozinho.
3. Limpar o dado corrompido em produção: remover o `stable_fact` "voce
   consegue salvar lá no pós..." do `daniel-profile` do usuário.
4. Teste de regressão do caso exato ("meu nome" embutido em frase não
   relacionada a perfil não deve gerar `stable_fact`).

### Fase 1 — Tool `pks_remember` + planner síncrono (núcleo)

Novo módulo `modules/pks_write/` (paralelo a `modules/pks_search/`):

- **`tools.py`** — `ToolSchema` para `pks_remember`, parâmetros:
  - `content` (obrigatório): o fato/ideia **já formulado em linguagem natural
    pelo cognitivo** — ele resolve "isso"/"essa ideia" e escreve o que salvar.
  - `note` (opcional): dica de intenção do usuário (ex.: "pra quando eu pedir
    bebida pré-treino de novo").
  - **Sem** parâmetros de entidade/tipo/campo: quem decide isso é o planner,
    não o cognitivo. A tool é fina de propósito.
- **`executor.py`** — `PKSWriteExecutor.execute()`:
  - **Decisão fechada: reusar `PKSWriter.learn(memory_target="user", ...)`**
    de forma síncrona (não criar um caminho de escrita separado), passando
    `content` como o artefato a persistir (`assistant_response=content`).
  - **Ponto crítico — a chamada da tool tem que ser autoritativa (ver §4.1).**
    O planner, por default, não persiste "sugestão do assistente sem
    confirmação explícita do usuário" e só abre exceção quando *casa* frases
    como "salva na memória"/"guarda isso" (memory_planner.py:50-52). Se a tool
    apenas injetar um `user_message` sintético, estamos reintroduzindo um
    **phrase-gate**: o planner pode olhar a receita, julgar que é sugestão não
    confirmada e devolver `{"operations": []}` — exatamente o que queremos
    eliminar. A chamada da tool **já é** a decisão do LLM cognitivo de que o
    usuário quer salvar; o planner não deve re-julgar essa intenção.
  - Por isso, threadar um flag `confirmed_save: bool` por
    `learn → _plan_operations → PKSMemoryPlanner.plan(..., confirmed_save=True)`
    que, no modo confirmado, troca a instrução do prompt de "só salve o que o
    usuário afirmou claramente + exceção condicionada a frase" por "este
    conteúdo **já foi confirmado** pelo usuário via ação explícita — sua única
    tarefa é **estruturá-lo** na ontologia certa (entidade/campos/relações),
    não decidir *se* salva". O planner continua decidindo COMO/ONDE; deixa de
    decidir SE. (É mais barato que um método `plan_explicit_save` separado e
    mantém uma só cabeça de estruturação.)
  - Retorna um **echo** do que foi salvo (entidades/campos aplicados) para o
    cognitivo confirmar citando o conteúdo real ("salvei: <resumo>"), nunca só
    "salvei". Se o planner ainda assim devolver vazio (ex.: conteúdo
    genuinamente não estruturável), a tool sinaliza isso ao cognitivo em vez
    de fingir sucesso.
- **Guards** — manter ledger_guard e credential_guard (dinheiro/segredo nunca
  viram PKS — ver [[neo-pks-ledger-boundary]]). Para escrita explícita,
  garantir que o `MIN_APPLY_CONFIDENCE` não descarte o que o usuário confirmou
  de próprio punho: ou instruir o planner a emitir confidence alto para
  conteúdo confirmado, ou o path explícito relaxa só o threshold de confidence
  (mantendo os outros guards).
- **Gate e dispatch** — seguir o padrão já vivo das `fin_*`
  (`core/cognitive/read_tools.py`, conjunto `_USER_SCOPED`, dispatch inline).
  Deixar explícito no código que esta tool tem efeito colateral (ex.: um
  `COGNITIVE_WRITE_TOOLS` separado do read set), diferente do resto.

### Fase 2 — Camada determinística (backstop, não a fonte da verdade)

A robustez principal vem da **camada LLM** (§4.1): o cognitivo decide chamar
`pks_remember` por entender o pedido, não por casar keyword. A Fase 2 só
adiciona a rede de segurança determinística: o intent_router já detecta
diretiva de memória (`_MEMORY_DIRECTIVE_PATTERNS`), mas hoje só marca
`needs_memory` (que não faz nada útil). Passar a **garantir** o acionamento —
sinalizando ao cognitivo "chame `pks_remember`" ou disparando o planner no
pipeline — para os casos óbvios em que o cognitivo, por algum motivo, não
chamou a tool. **Regra:** essa camada só *adiciona* cobertura; nunca *bloqueia*
o que o LLM já liberou (senão recriamos o gate determinístico que causou o bug).

### Fase 3 — Atualizar prompts

- `core/router/intent_router.py:368` — trocar *"saved automatically to the
  knowledge store"* pela realidade: o Neo aciona o Subagente de Memória
  (`pks_remember`) no próprio turno; nunca afirma ter salvo sem tê-lo feito.
- System prompt do cognitivo — mesma regra do handoff de skills: *"NUNCA diga
  que salvou algo sem ter chamado `pks_remember` nesta mesma resposta"*
  (espelha a regra #9 já existente).

### Fase 4 — Validação

Replay do cenário exato do log (bebida pré-treino) em dev/staging:
"salva essa ideia" → chamada de `pks_remember` → planner cria entidade
`preference`/`food` estruturada (não `stable_fact` cru, não em `system`
scope) → echo de confirmação → pergunta seguinte ("qual era aquela bebida?")
recupera via `search_pks`.

### Fase 5 — Próximo passo natural (fora do escopo inicial)

`pks_forget` / edição sob demanda ("esquece isso", "corrige aquilo") — mesma
arquitetura (tool fina → planner com `soft_delete_entity`/`expire_relation`),
não bloqueante para a Fase 1.

## 7. Correção adicional (achado no caminho)

O log mostra o cognitivo chamando `skill_read(name="financas")` no meio da
tentativa de salvar a bebida — "pescando" qualquer tool que pareça persistir.
Não é crítico (sem efeito colateral), mas some sozinho com a Fase 1: existindo
`pks_remember` óbvia no catálogo, não há motivo pra procurar alternativa.

## 8. Decisões

**Fechadas:**
- **Reuso do `learn` (não método dedicado).** A tool reusa
  `PKSWriter.learn(memory_target="user")`; o modo confirmado vira um flag
  `confirmed_save` threadado até o planner (§Fase 1), não um caminho paralelo.
- **Acionamento é dirigido pelo LLM, com backstop determinístico.** O cognitivo
  decide chamar a tool por entendimento; o regex do router é rede de segurança
  que só adiciona cobertura, nunca bloqueia (§4.1, §Fase 2).

**A fechar antes de implementar:**
- **Escopo:** confirmar que `pks_remember` fica restrita a `user` scope
  (`preference`, `food`, `routine`... — o certo pra uma receita) e **não** toca
  `system` scope (`daniel-profile`/personality), que continua só via treino
  explícito (`correction_example`/canonicalizer). Recomendo restringir.
- **Abuso / injeção:** limite de tamanho de `content`; teto de entidades por
  sessão; cuidado com conteúdo vindo de fonte externa (ex.: página de
  `web_search` dizendo "salva isso") — a tool só grava o que o **usuário**
  confirmou, não o que um texto sugeriu. (O modo confirmado torna isso mais
  sensível: como o planner não re-julga, o cuidado migra pra *quando* o
  cognitivo tem permissão de chamar a tool.)
- **Confidence:** no modo confirmado, garantir que o `MIN_APPLY_CONFIDENCE` não
  descarte o que o usuário confirmou (ver Fase 1, guards) — mantendo
  ledger/credencial intactos.

## 9. Status de implementação (2026-07-24)

**EM PRODUÇÃO** (sincronizado na VPS via Mutagen + `--reload`; serviço
saudável, 25/25 testes passando):

- **Fase 0 — higienização.** `core/pks/writer.py`: novo `_is_reference_only_save`
  + guard `if has_memory_intent and not reference_only:` no `_build_system_updates`
  — save-por-referência ("salva essa ideia", "...tenta de novo") não fabrica mais
  entidade de sistema a partir do comando cru. Dado corrompido removido em prod
  (`pks_entity_field` de `daniel-profile`, `stable_facts` → `[]`). Testes de
  regressão em `tests/pks/test_writer_memory.py`.
- **Fase 1 — tool + planner síncrono em modo confirmado.**
  - `core/pks/memory_planner.py`: param `confirmed_save` + `_CONFIRMED_SAVE_OVERRIDE`
    (o planner ESTRUTURA sem re-julgar se salva).
  - `core/pks/writer.py` + `core/pks/service.py`: `confirmed_save` threadado por
    `learn → _plan_operations → plan`; no modo confirmado o `MIN_APPLY_CONFIDENCE`
    não descarta (ledger/credencial seguem valendo).
  - `modules/pks_write/` (novo): `tools.py` (`pks_remember`) + `executor.py`
    (`PKSWriteExecutor`, delega a `PKSService.learn(confirmed_save=True)`, echo do
    que salvou; **user scope apenas**).
  - `core/cognitive/read_tools.py`: `pks_remember` no read set + `_USER_SCOPED` +
    dispatch; gate por `pks_write_enabled` (default True, em `shared/config.py`).
  - Testes em `tests/pks/test_pks_write.py`.
- **Fase 3 — prompts.** `core/agent/context_selector.py` (regras 9/10 apontam
  `pks_remember`) + `core/router/intent_router.py` (linha de memory-directives).

**Decisões tomadas na implementação** (defaults recomendados do §8): escopo
restrito a `user`; reuso do `learn` com `confirmed_save`; teto de 2000 chars no
`content`.

### 9.1 Segundo bug — o planner estava 100% quebrado (session-77a77b00, corrigido)

No teste seguinte (`session-77a77b00`), a camada LLM funcionou (o cognitivo
chamou `pks_remember` corretamente) mas a tool devolvia "não foi possível
estruturar" **toda vez**. Causa raiz (nada a ver com o `confirmed_save`, e
**pré-existente**): o `PKSMemoryPlanner` chamava `provider.complete()` **sem
model**, caindo no `DeepSeekProvider.DEFAULT_MODEL = "deepseek-chat"` — nome
**aposentado** pela API DeepSeek (400 `invalid_request`). Ou seja, **TODA
gravação de PKS por LLM estava falhando silenciosamente** (a tool nova E a
extração automática do `_bg_pks`), retornando `[]`. Correções:

- `core/llm/deepseek_provider.py`: `DEFAULT_MODEL` → `deepseek-v4-flash`.
- `core/llm/router.py`: default `"deepseek"` → `deepseek-v4-flash`.
- `shared/config.py`: novo `pks_planner_model` (default `deepseek-v4-flash`);
  `core/pks/memory_planner.py` passa esse model **explícito** (não depende mais
  do default do provider).
- `core/pks/memory_planner.py`: `max_tokens` 3000 → 8000 (o modelo às vezes
  monta um grafo maior e o JSON truncava) + `_salvage_operations` (parser
  tolerante: recupera as operações completas de um JSON truncado em vez de
  perder tudo). Testes em `tests/pks/test_pks_write.py`.

**Validado E2E de verdade** (2026-07-24): `PKSService.learn(confirmed_save=True)`
com o user_id real do Daniel salvou a receita como entidade `preference`
("Bebida pré-treino sem cafeína preferida") + field `recipe` + relação `owns`,
confirmado no Postgres. 27/27 testes passando; serviço saudável.

Nota: a "alteração no PKS às 18:48" que o Daniel viu no teste NÃO era a bebida —
era a entidade `Plano de treino de academia` (routine) sendo re-tocada por um
caminho determinístico (não-LLM), que não sofria do 400. A bebida só salvou
depois do fix.

### 9.2 Terceiro bug — duplicata do `_bg_pks` (session-b7e3985d, corrigido)

Teste pela interface (`session-b7e3985d`): a recuperação funcionou ("como era a
bebida?" → `search_pks` achou e respondeu) e o `pks_remember` funcionou (o
usuário pediu "coloca a alternativa nas anotações" → a tool **atualizou a
entidade canônica** existente: reescreveu `recipe` com as 2 opções + campo
`note`). Mas surgiu uma **entidade duplicada** (`Bebida pré-treino sem cafeína`,
type `supplement`) além da canônica (`...preferida`, type `preference`).

Causa: a extração automática pós-turno (`_bg_pks` → `learn(memory_target="user",
confirmed_save=False)`) rodou **em paralelo** à tool e re-salvou a mesma info
como entidade nova (nome/tipo diferentes → o resolver não deduplicou). Esse
caminho estava 400ando pelo bug do deepseek-chat (§9.1) — ao consertá-lo, ele
voltou e passou a duplicar o que a tool explícita já grava. Fix
(`modules/desktop/ai_service.py`): quando `pks_remember` foi chamada no turno
(detectado via `tool_trace`), o `_bg_pks` **pula** a extração user-scope — a
gravação explícita é a fonte de verdade do turno (mesma lógica de
`skill_owns_turn`). O `system` scope segue rodando. Duplicata removida em prod.

### 9.3 Fase 5 — `pks_forget` EM PRODUÇÃO (2026-07-24)

Tool `pks_forget(target)` no mesmo módulo `modules/pks_write/`, mesma
arquitetura fina da `pks_remember`: o cognitivo resolve o alvo ("esquece isso" →
descrição por extenso) e a tool delega ao planner num **modo esquecer**
(`forget=True`), que localiza a entidade no contexto PKS e emite
`soft_delete_entity` (soft delete, reversível). Peças:

- `core/pks/memory_planner.py`: `_FORGET_OVERRIDE` + param `forget` (emite SÓ
  soft_delete_entity; se o alvo não existe, devolve `[]` — não inventa).
- `core/pks/writer.py`: `learn(forget=True)` pula os guards de save/artefato,
  bypassa confidence e **filtra** as operações para só remoções (trava contra o
  planner alucinar um upsert no modo esquecer). `service.py` repassa `forget`.
- `modules/pks_write/executor.py`: handler `_forget` (contexto PKS é essencial
  aqui) + `_format_forget_echo` (lista o que removeu, ou avisa que não achou —
  nunca finge apagar). `tools.py`: schema `pks_forget`.
- Wiring automático (o dispatch já roteia por `PKS_WRITE_TOOL_NAMES`).
- Prompt: regra 9 do `context_selector.py` cobre esquecer/apagar (`pks_forget`)
  e corrigir (via `pks_remember`, que sobrescreve).
- O guard do `_bg_pks` (§9.2) agora cobre `pks_remember` E `pks_forget`.

Correção (editar valor) reusa `pks_remember` (upsert sobrescreve) — não precisou
de tool nova. `expire_relation` segue no-op no writer (remover relação isolada é
enhancement futuro; hoje o forget apaga a entidade inteira).

**Validado E2E** (2026-07-24): criei uma entidade descartável, `pks_forget` a
removeu ("Removi da sua memória: QAZWSX"), um alvo inexistente devolveu "nada foi
apagado" (sem alucinar), e a receita real ficou intacta. 29/29 testes; serviço
saudável.

### 9.4 Fase 2 — backstop determinístico EM PRODUÇÃO (2026-07-24)

Rede de segurança para o pedido explícito não cair no vazio quando o cognitivo,
por algum motivo, NÃO chama `pks_remember`. Implementação (variante "dispara o
planner direto", a mais forte):

- `core/router/intent_router.py`: `_MEMORY_DIRECTIVE_PATTERNS` expandido para
  cobrir formas que faltavam — "salva essa ideia", "coloca isso nas (nossas)
  anotações", "guarda essa receita/dica", "pra (eu) lembrar". Padrões
  inequívocos de memória: **não** casam save de arquivo ("salva o arquivo no
  desktop", "salva isso em downloads") — validado.
- `modules/desktop/ai_service.py` (`_bg_pks`): quando
  `IntentRouter._is_memory_directive(user_message)` é True E `pks_remember`/
  `pks_forget` NÃO rodaram no turno, a extração pós-turno roda em
  `confirmed_save=True` (força o save) em vez do best-effort fraco (que poderia
  devolver `[]` achando que é "sugestão do assistente"). Só ADICIONA cobertura:
  quando a tool rodou, o bloco nem executa (guard §9.2).

Regra respeitada (§4.1): a camada determinística nunca *bloqueia* o que o LLM
liberou — só garante o mínimo quando o LLM esquece. Testes em
`tests/test_memory_routing.py` (positivos novos + negativos de save-de-arquivo).

**Todas as fases planejadas concluídas.** Restam só enhancements fora do escopo
original:

**ENHANCEMENTS FUTUROS (não bloqueantes):**
- **Dedup mais forte no resolver** (defesa em profundidade): entidades
  quase-idênticas com nome/tipo levemente diferentes ainda escapam do
  matching canônico — o guard do `_bg_pks` cobre o caso da tool, mas dois
  turnos automáticos distintos ainda poderiam duplicar. Ligado ao v3 (dedup/
  consolidação noturna).
- **`expire_relation`**: hoje no-op no writer; `pks_forget` apaga a entidade
  inteira. Remover uma relação isolada ("esquece que eu treino às 6h, mas
  mantém o treino") é um enhancement.
