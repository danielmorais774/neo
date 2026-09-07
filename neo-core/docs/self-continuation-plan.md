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

## 6. Testes

`tests/pipeline/test_self_continuation.py` (parser, policy, orçamento, anti-loop,
profundidade, notas de trabalho, gate de promessa, e o contrato com o desktop:
marcador por passo, `persist_reply` nas intermediárias, terminador na cadeia cortada).
`tests/pipeline/test_cycle_stream_marker.py` (marcador não vaza no NDJSON).

**Rollback:** `pipeline_self_handoff_enabled=false` — o bloco continua sendo removido do
texto (nunca vaza) e o turno volta a terminar como antes.
