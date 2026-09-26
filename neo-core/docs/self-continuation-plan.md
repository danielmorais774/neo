# Autocontinuação do núcleo cognitivo (`[[HANDOFF:continuar]]`)

**Data:** 2026-08-09
**Status:** EM PRODUÇÃO (ciclo validado ao vivo). §4 e §5 **fechados em 2026-08-09**.
**Depende de:** [orquestrador de pipelines](./pipeline-orchestrator-plan.md) (handoff em prod)

O turno do Neo acaba quando o loop cognitivo devolve texto. Antes disto, uma resposta
que prometia trabalho ("sigo tentando", "vou resolver") encerrava o turno e **nada
rodava** — o usuário ficava esperando algo que nunca vinha. Medido na
`session-fd467d08`: quatro turnos seguidos assim, zero entrega.

Isto dá ao cérebro um alvo de handoff para **si mesmo**: ele roda a próxima ordem
concreta no mesmo turno, em ciclo, até entregar.

---

## 1. O que subiu

| Arquivo | O quê |
|---|---|
| `core/pipeline/handoff_parser.py` | `SELF_HANDOFF_TARGET = "continuar"` → `DIRECT_RESPONSE`; `extract_handoffs(allow_self=…)` |
| `core/pipeline/policy.py` | `DIRECT_RESPONSE: "auto"` — **sem isto não anda**: o default do `.get()` é `"confirm"` e todo passo nasceria pedindo confirmação |
| `core/pipeline/types.py` | `Handoff.is_self`, `PipelineStep.context_note` + `is_self_continuation`, `StepOutcome.work_note` |
| `core/pipeline/orchestrator.py` | orçamento maior quando o ciclo "latcha", `work_notes` acumuladas, `on_notice` em toda saída antecipada |
| `modules/desktop/ai_service.py` | `_SELF_HANDOFF_INSTRUCTION`, `_verify_promise_continuation` (backstop), `_format_work_note`, `CYCLE_PROGRESS_MARKER` |
| `api/routes/desktop.py` | tira o marcador no caminho NDJSON |
| `neo-desktop-tauri/src-tauri/src/services/ai_agent.rs` | tira o marcador e troca o rótulo para "Executando" |

**Flags** (`shared/config.py`): `pipeline_self_handoff_enabled` (default `true`),
`pipeline_self_handoff_max_steps=8`, `pipeline_self_handoff_max_depth=7`.

**Decisão de design que não pode ser desfeita sem quebrar tudo:** a nota de trabalho é
construída do `tool_trace`, **nunca** do texto do modelo. Se viesse da resposta, uma
promessa ("vou baixar") voltaria como resultado na rodada seguinte, e o ciclo passaria a
construir em cima de ficção.

## 2. Feedback no cliente

- Cada passo emite um thinking com prefixo `[[CYCLE]]` → vira bolha **"Executando ⚡"**.
- Respostas intermediárias saem com `persist_reply` → **bolha própria** no chat, não
  thinking transitório. É isto que faz as mensagens aparecerem conforme saem.
- Cadeia cortada por guarda ainda emite `is_final` com a razão real
  (`_CYCLE_CLOSING_MESSAGE`): o desktop só emite `complete` ao ver `is_final`, então sem
  esse terminador o chat gira para sempre.

## 3. Como reproduzir o ciclo (não é óbvio)

Pedido direto **não** exercita o ciclo — o gpt-4.1 conclui dentro de um passo só. O que
destrava é pedir trabalho **em etapas**:

> "trabalha em etapas separadas e me avisa ao concluir CADA uma antes de começar a
> próxima. etapa 1: … etapa 2: … não me pergunte nada, vai sozinho até o fim."

Cadeia observada em `session-bb22cc33` (2026-08-09): 3 steps de cascata → resposta
prometeu *"Vou mudar de estratégia… Segue o jogo"* sem bloco → `Continuação (verificador
de promessa)` sintetizou a ordem → `🔗 Pipeline — Step 4 (cognitive, origem self)` →
`Continuação — nota de trabalho` levou o `neo_bash` que falhou (`exit_code=1`) → passo
rodou e fechou.

---

## 4. ✅ FECHADO (2026-08-09) — guarda de verdade-do-resultado

**O defeito, medido na própria prova acima.** O passo final do ciclo fechou com:

> "Três imagens livres de Naruto, baixadas de fonte alternativa (Openverse) para a pasta
> `/workspace/ciclo-prova`. As imagens são reais."

E não havia imagem nenhuma:

```
find /workspace -name "*.jpg" -newermt "2026-08-09 02:35"   → vazio
ls /workspace/ciclo-prova/  → galeria.html index.html sobre.html style.css
```

O ciclo está mecanicamente correto e mesmo assim entrega **conclusão falsa**. Mesma
classe do bug da skill de Home Assistant, que dizia "desliguei" sem desligar.

**Por que o guard atual não pega:** existe o anti-alegação-de-download
(`ai_service.py`, procurar `download_claim_retried`), mas ele só cobre a afirmação de
*entrega ao usuário* — dispara quando a resposta promete algo baixável sem ter chamado
`file_save`. **Não existe nada** para "criei/baixei arquivo no sandbox". Enquanto isto
não existir, "até concluir" significa "até o modelo DIZER que concluiu", e o ciclo
autônomo herda esse buraco inteiro.

**Desenho proposto** (mesmo molde do guard que já existe, para não inventar mecanismo):

1. Detectar na resposta final afirmação de artefato criado no sandbox — passado, não
   promessa: "baixei", "salvei em", "gerei o arquivo", "está em `/workspace/...`".
   Reusar o molde de `_ASSERTS_EXECUTION`; **não** reusar `_looks_like_promise`, que é o
   inverso (futuro) e daria falso negativo em cima de falso negativo.
2. Cruzar com o `tool_trace` do turno: houve algum tool **com sucesso** que toque aquele
   caminho? Se não → rodada extra obrigatória com instrução de verificar
   (`neo_list_files` no caminho citado) antes de reafirmar.
3. Se a verificação mostrar que não existe, a resposta tem que dizer o que **de fato**
   aconteceu — não repetir a alegação.

**Critério de aceite:** o cenário exato acima (Wikimedia 403 → fonte alternativa falha)
termina com o Neo dizendo que não conseguiu baixar, e **nunca** com "as imagens são
reais". Teste de regressão com o `tool_trace` real daquele turno, gravado.

**Cuidado:** o guard roda no fim de CADA passo do ciclo, então precisa ser barato —
cruzamento com o `tool_trace` é determinístico e não deve custar chamada de LLM.

### Como ficou

`_claims_sandbox_artifact` (afordância ∧ âncora ∧ polaridade, molde do guard de
download) + `_trace_backs_artifact_claim`, ligados em `_run_cognitive_tool_loop`
(`artifact_claim_retried`). Zero LLM, como o plano exigia.

**A correção que só apareceu com o trace real na mão.** O desenho acima dizia
"cruzar com o `tool_trace`" — e o primeiro corte cruzava contra a entrada inteira,
comando incluído. Contra o trace gravado do turno que mentiu isso **não pegava nada**:

```json
{"ok": true, "tool": "neo_bash", "exit_code": 0, "stdout_preview": "",
 "command": "python3 … baixar_imgs(\"Naruto anime\", \"/workspace/ciclo-prova\", 3) …"}
```

O bash saiu **0** (o script Python tinha `except Exception: continue` em volta de cada
download) e o caminho `/workspace/ciclo-prova` estava escrito **dentro do comando** — que
"confirmava" a alegação. **Comando é intenção, não resultado**, mesmo erro de montar a
nota de trabalho a partir da promessa. Só conta o que o sistema DEVOLVEU:

| tool | evidência |
|---|---|
| `neo_write_file` | `path` (a tool É a escrita) |
| `file_save` / `produced_files` | `name` (catalogado, com id) |
| `neo_bash` | `stdout_preview`, e só com `exit_code == 0`; saída vazia não prova nada |
| `neo_list_files` e demais | `summary`, descartado quando contém `'ok': False` / `cannot access` — senão `ls: cannot access '/workspace/x'` provaria o oposto do que diz |

Duas travas contra falso positivo, ambas com teste: sem `tool_trace` a guarda **não
acusa** (ausência de rastro ≠ prova de ausência), e resposta negada ("não consegui
baixar") nunca dispara — é justamente o comportamento desejado.

**Critério de aceite cumprido:** `tests/test_artifact_claim_guard.py` roda os DOIS
traces reais da `bb22cc33` — a mentira é barrada, a entrega honesta do mesmo ciclo passa
limpa. Mais 3 testes em `tests/test_cognitive_tool_loop.py` provando que a rodada extra é
de fato forçada, que ela acontece uma vez só, e que sem trace a guarda fica quieta.

## 5. ✅ FECHADO (2026-08-09) — `_verify_music_decision` tinha o mesmo bug do `disable_thinking`

O verificador de promessa voltava **vazio** e caía no `except` (`Expecting value: line 1
column 1 (char 0)`) exatamente no turno que devia continuar: `max_tokens=160` +
raciocínio ligado por default no OpenRouter → o modelo despeja "Thinking Process:…" como
content. Corrigido com `disable_thinking=True` + `max_tokens=700` (provado lado a lado
com `qwen3.5-flash`).

`_verify_music_decision` tem o **mesmo formato** (`max_tokens=120`, sem o flag) e não foi
tocado — fora do escopo daquele trabalho. Provavelmente falha em silêncio do mesmo jeito,
o que significa música que não toca sem ninguém saber por quê. Verificar e corrigir.

### Confirmado e corrigido

Não era "provavelmente": estava quebrado. A/B contra o `qwen3.5-flash` real, mesmo
prompt do verificador de música, só mudando os parâmetros:

```
ANTES  (max_tokens=120, sem flag): FALHOU -> Expecting value: line 1 column 1 (char 0)
   content='Thinking Process:\n\n1.  **Analyze the Request:**\n    *   Input: A user…'
DEPOIS (max_tokens=700 + disable_thinking): JSON OK
   content='{"action":"play","query":"Highway to Hell","service_hint":"spotify"}'
```

O `except` engolia isso e devolvia `None` — que o chamador lê como "não era pra tocar".
Resultado: o backstop de música **nunca** sintetizava o handoff, e a música não tocava
sem erro nenhum aparecendo. Corrigido com os mesmos `max_tokens=700` +
`disable_thinking=True`. Os quatro providers aceitam o kwarg (cada um dá `pop`), então
não há risco de `TypeError` no caminho que cai no provider default.

---

## 6. ✅ FECHADO (2026-09-24) — amnésia do ciclo e anti-loop que matava o turno

Medido na `session-389114d4`, interação 29 (input "tem o mais recente do INSS que foi
um rombo ainda maior"): **4 rodadas de LLM, 30,1 s, zero tool, 4 bolhas quase
idênticas** e, no lugar da resposta, `"Parei aqui: o próximo passo seria repetir um
que já rodou neste turno"`. Aconteceu 2× nessa sessão. Três defeitos somados.

### 6.1 O ciclo não lembrava do que ele mesmo disse

`history` vinha de um `get_history` único, feito no começo do request, e era
repassado **idêntico** a todos os passos (o snapshot nunca era refeito, mesmo com a
resposta do passo já no banco). O payload do passo 2:

```
[49] assistant  "Achei o fio…"                     ← turno ANTERIOR
[50] user       "tem o mais recente do INSS…"
[51] user       "levantar o histórico completo…"   ← a ordem sintetizada
                 ↑ a resposta do passo 1 não existe
```

Duas `user` seguidas leem como pedido novo: as quatro respostas abriram com a mesma
frase ("Você tem razão — e esse é o caso mais grave que eu deixei de fora").

A nota de trabalho não cobre isso **de propósito** — ela é montada só do `tool_trace`
(senão uma promessa volta como resultado) e passo sem tool vira "nenhuma ação
executada", que é verdade e ainda assim não conta o que ele JÁ DISSE.

**Fix:** `cycle_history` em `_run_pipeline` — cópia própria que CRESCE, com a ordem e
a fala de cada passo apendadas quando uma continuação vai ler. Teto por resposta
(`_CYCLE_REPLY_CHARS = 1200`) e **sem janela deslizante**: manter só as últimas N
recria o bug de 2026-09-18 (janela do prompt menor que o teto de rodadas). Append no
fim também preserva o cache de prefixo.

⚠️ `history` alimenta também PKS, memória de conversa e `_find_memory_artifact`. Os
dois primeiros ficam mais corretos; o terceiro pode eleger uma resposta
INTERMEDIÁRIA do ciclo como "o artefato" — é o ponto a olhar se aparecer estranheza.

### 6.2 A chave do anti-loop era igualdade de string, o gerador é um LLM

As quatro ordens sintetizadas, verbatim:

```
1. …com status processual para fechar a camada 5
2. …com status processual real e fonte                      (0,80 de cobertura sobre a 1)
3. …real, o que foi provado, o que foi anulado…, e a fonte   (0,53 sobre a 1)
4. idêntica à 3                                              ← só AQUI a guarda acordava
```

**Fix:** `order_is_near_repeat` / `content_tokens` / `ORDER_COVERAGE=0.7` em
`core/pipeline/types.py` — conjunto de palavras de conteúdo, cobertura do texto NOVO
pelo antigo (alvo inédito passa). Espelha o `_near_repeated_actuation` do loop de
shell, calibrado no mesmo tipo de falha.

⚠️ **Vale só dentro do ciclo.** Cascata do classificador tem passos parecidos de
propósito ("buscar os links" / "baixar os links"); aplicar cobertura ali
transformaria cascata em correção. Fora do ciclo segue valendo só a igualdade
literal, com o comportamento antigo (descarta e segue a fila).

⚠️ A ordem 3 fica em **0,53/0,67 — abaixo do corte, e isso é proposital**: baixar o
limiar compraria falso positivo em ordem que só compartilha assunto, e falso positivo
aqui não custa uma chamada barata, custa recusar um passo que ia trabalhar. Quem
fecha esse caso é a §6.3.

### 6.3 Guarda nova: passo que só fala não é progresso

`outcome.ok` era `True` porque "respondeu", então a fila andava. `StepOutcome.acted`
(tool rodada ou arquivo entregue — nunca texto) + `MAX_IDLE_CYCLE_STEPS=2`: dois
passos de ciclo seguidos sem executar nada **e o ciclo pedindo outra volta** fecham o
turno. Só intervém se a cadeia ia continuar — passo sem handoff já fechou sozinho com
`is_final`, e aí não há nada a corrigir.

### 6.4 Guarda não mata mais o turno: ela manda pensar de novo

`PipelineStep.self_correction(kind=)`, enfileirado à frente da fila por repetição,
orçamento, profundidade ou falha:

- **`retry`** (1ª volta, `MAX_CYCLE_NUDGES=1`) — irmão do empurrão do loop de shell:
  não despacha, devolve ao cérebro dizendo ONDE a resposta já está, com uma lista
  FECHADA — (a) responda agora, (b) chame a tool que traz a peça que falta, (c)
  pergunte em uma linha e pare — e o aviso de que reescrever o plano não conta como
  nenhuma das três. A versão sem a lista recebia "vou levantar o histórico…" outra vez.
- **`close`** (2ª volta, ou orçamento/profundidade/falha) — passo final que escreve a
  resposta com o que existe. Roda com **`enable_handoff=False`**: sem handoff não há
  verificador de promessa (que leria o próprio fecho como promessa e abriria outro
  ciclo) e a resposta é `is_final` por `handoff_defers_final`. **É ela que fecha o
  turno** — fala do Neo, não string do sistema.

Detalhes que são carga, não estilo:

- `depth` do fechamento é o do **pai**, não `+1`: com `+1` a guarda de profundidade
  mataria justamente o passo que existe para o turno não morrer.
- Fechamento é isento do orçamento e roda **uma vez** por turno (`closed_once`).
- A fila **ignora handoff vindo de um `close`**: em produção ele nem emite, mas a
  fila não pode depender disso — senão o fecho daria outra volta na guarda e o turno
  ganharia um segundo aviso pelo mesmo motivo.
- O rótulo da bolha de progresso deixa de ser "Continuando (5/8): <ordem repetida>",
  que era a bolha mentindo: vira "Repensando o próximo passo" / "Fechando com o que
  já tenho".
- `_CYCLE_CLOSING_MESSAGE` virou **último recurso** (o passo de fechamento não
  respondeu nada, ex.: erro de provider), com entradas novas para `near_repeat` e
  `idle_cycle`.

### 6.5 Replay da sequência real

As 4 ordens da interação 29, sem tool nenhuma, contra o código novo:

```
1. [normal] tem o mais recente do INSS…          2. [normal] levantar o histórico…
3. [retry ] levantar o histórico…                4. [close ] fechar o turno com o que já foi levantado
avisos: near_repeat, idle_cycle
```

Mesma contagem de passos (4), com três diferenças que importam: o laço é detectado no
**3º** passo e não no 5º, o 3º passo recebe a ordem de agir ou responder (chance de
se corrigir), e o 4º é **resposta de verdade** em vez de string de erro.

## 7. Testes

`tests/pipeline/test_self_continuation.py` (parser, policy, orçamento, anti-loop,
profundidade, notas de trabalho, gate de promessa, e o contrato com o desktop:
marcador por passo, `persist_reply` nas intermediárias, terminador na cadeia cortada).
`tests/pipeline/test_cycle_stream_marker.py` (marcador não vaza no NDJSON).
`tests/pipeline/test_order_near_repeat.py` (as 4 ordens REAIS + fronteira 0,53/0,67 +
falso positivo de palavra de função).
`tests/pipeline/test_cycle_history_and_correction.py` (fala do passo entra no
histórico do seguinte, truncada e sem janela; ciclo ocioso fecha falando; empurrão
carrega a instrução; ciclo que trabalha não é interrompido; cascata intocada).

⚠️ Os dois harnesses (`_drive` e `_install_handler`) **precisam honrar
`enable_handoff`** — é por ele que o fechamento fecha. Harness que ignora o parâmetro
devolve o fecho como não-final e reproduz o bug em vez do conserto.

**Rollback:** `pipeline_self_handoff_enabled=false` — o bloco continua sendo removido do
texto (nunca vaza) e o turno volta a terminar como antes.
