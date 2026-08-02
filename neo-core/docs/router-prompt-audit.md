# Auditoria do `ROUTER_SYSTEM_PROMPT`

Fonte: [`core/router/intent_router.py`](../core/router/intent_router.py) (constante `ROUTER_SYSTEM_PROMPT`).
Auditoria original em 2026-07-30; **poda aplicada em 2026-08-01** (v1 → v2).

Este prompt é o system message do **classificador de intenção** — vai em **toda mensagem do usuário**, num modelo flash barato. Tudo aqui é custo fixo por turno, e cada linha compete por atenção com todas as outras.

## Estado atual (v2)

| Bloco | Chars | ~Tokens | Observação |
|---|---:|---:|---|
| Template (v2) | 14,645 | ~3,661 | fixo, editável |
| `{server_tools}` | 1,139 | ~285 | era 2,729 — encurtado por `router_summary` |
| `{client_tools}` | 336 | ~84 | era 1,665 — idem |
| `{skills}` + `brain_note` | ? | ? | gerados do registry / SkillLibrary |
| `Contexto do Sistema` | 2,600 | ~650 | `_build_system_context()` |
| **Total conhecido** | **18,720** | **~4,680** | era 29,972 (~7,493) na v1 → **−37%** |

Além disso vão as **últimas 6 mensagens do histórico** como turnos `user`/`assistant` reais (é o que puxou o classificador pro modo conversa na sessão `69feab87`).

O texto integral da v1 está preservado em [`tests/router/prompt_v1.txt`](../tests/router/prompt_v1.txt) — é o baseline do A/B, não um arquivo morto.

## A poda: o que saiu e para onde foi cada regra

**Nenhuma regra foi removida.** O corte foi de *repetição*: a v1 dizia a mesma coisa em até 5 lugares, e 3 exemplos apareciam literalmente duas ou três vezes.

| Repetição na v1 | Onde estava | Onde está na v2 |
|---|---|---|
| Sandbox do cérebro × máquina do usuário | Paths/DIRECT_RESPONSE, Paths/CLIENT_TOOL, Tool Priority (§ inteira), Key Rules #1, seção de exemplos | **Regra mestra** (uma vez, no topo, antes de qualquer escolha de tool) + 1 bloco de exemplos |
| "Só existem 2 client tools, não invente uma terceira" | Tool Priority + comentário na seção de exemplos | bullet CLIENT_TOOL em Paths |
| "Nunca invente file_id/caminho/nome de arquivo" | Topologia, Tool Priority, Key Rules (anexo), Key Rules (needs_plan), 2 exemplos | Regra mestra (file_id), bullet CLIENT_TOOL (nome), Key Rule do anexo (caminho) |
| Imperativo × condicional/preferência para skill | Paths/HA, REGRA DURA, Key Rules "Casa inteligente", Key Rules "Imperativo direto", 2 blocos de exemplo | **REGRA DURA** (absorveu a cláusula de hábito/preferência) + 2 exemplos |
| `pipeline_plan` | Key Rules + seção de exemplo com contra-exemplo | Key Rule + 1 exemplo positivo |
| Exemplos idênticos | "acende a luz da sala" ×3, "se eu costumo usar o ar nas quintas" ×2 | 1 cada |
| 7 exemplos de `config_set` | seção CONFIG | 3 (voz, engine, listagem) — a regra com o mapeamento fica no bullet SERVER_TOOL |
| 14 exemplos de sandbox × máquina | seção de exemplos | 9 (um por classe: computar, baixar+analisar, disco, path explícito, busca, abrir/instalar, texto-do-comando, gerar arquivo, arquivo só descrito) |

Também saíram os "porquês" longos (explicações de *como* o File Store funciona). Um classificador precisa da decisão, não da arquitetura — e o texto que sobrou de cada regra continua dizendo o que fazer.

### O catálogo de tools (2ª rodada, 4.394 → 1.475 chars)

A `description` de cada tool era paga duas vezes: ela vai como schema da função para o **cérebro** (que precisa saber *usar* a tool) e ia inteira no catálogo do **classificador** (que só precisa *reconhecer*). E repetia o prompt: a de `save_generated_file` explicava de novo "nunca `cp`/`mv`/`curl`, o arquivo está no sandbox"; a de `config_set` repetia o mapeamento `"usa a voz do X"` → `tts.voice`.

Encurtar a `description` enfraqueceria o cérebro (a de `run_command` carrega a regra "nunca diga que não tem acesso ao terminal"), então `ToolSchema` ganhou **`router_summary`** — uma linha usada só por `IntentRouter._format_tools`, com fallback para a `description`. Preenchida em 7 tools; as outras seguem como estavam.

Medido: 44/46 com catálogo curto **e** com catálogo completo, as mesmas duas falhas (`uv run python scripts/eval_router.py --full-tool-descriptions`). O corte saiu de graça.

### Sobre o que os *guards* já resolvem

Vários casos nunca chegam ao LLM: `_is_memory_directive`, `_is_known_devices_query`, `_is_referential_question`, `_is_brain_reasoning_request`, `_is_music_request`, `_is_foreign_assistant_address` e `_is_training_message` decidem antes, e `_bias_to_brain` + o guard de `tool_name` inválido corrigem depois. Isso não torna a regra no prompt inútil (o regex é mais estreito que a classe), mas justifica dizê-la **uma vez e curta**, em vez de reforçá-la com exemplos.

## Resultado medido (2026-08-01)

| Variante | Acerto | Prompt |
|---|---:|---:|
| v1 (baseline) | 43/46 (93.5%) | 22.977 chars ≈ 5.744 tokens |
| v2 (atual) | 43/46 (93.5%) | 14.645 chars ≈ 3.661 tokens |

**Zero diferenças de decisão** — as duas variantes classificaram os 46 casos exatamente igual, com 2.083 tokens a menos por mensagem. Log completo em [`tests/router/ab-2026-08-01-gpt4o-mini.txt`](../tests/router/ab-2026-08-01-gpt4o-mini.txt).

Rodado em `openai/gpt-4o-mini` porque a chave do OpenRouter (provider do classificador em produção) estava com **limite estourado** no dia — nesse estado o classificador falha e a decisão vem do fallback por palavra-chave, que não lê o prompt. Vale repetir o A/B em `openrouter/qwen/qwen3.5-flash-02-23` quando a chave voltar.

As 3 falhas são **iguais nas duas variantes** (não vieram da poda): `ha-termostato` (pergunta sobre termostato vai pro `get_weather`/SERVER_TOOL em vez da skill HA — fraqueza real e antiga do prompt), `clima` (o modelo prefere mandar pro cérebro, que tem `web_search` — discutível se é erro) e `gravidade-alta` (respondeu `critica` onde o golden pede `alta`).

## Como verificar que a poda não custou acurácia

```bash
uv run python scripts/eval_router.py --ab            # v1 (baseline) vs prompt atual
uv run python scripts/eval_router.py --ab --repeat 3 # 3 amostras por caso
uv run python scripts/eval_router.py --provider openrouter --model qwen/qwen3.5-flash-02-23
```

O golden set fica em [`tests/router/golden_intents.yaml`](../tests/router/golden_intents.yaml) — um caso por classe de decisão, mais os bugs reais citados nos comentários do `intent_router.py`. **Bug novo entra como caso de teste, não como exemplo no prompt**: exemplo custa token em toda mensagem do usuário; caso de teste custa só quando se roda o eval.

[`tests/router/test_prompt_invariants.py`](../tests/router/test_prompt_invariants.py) roda offline (sem LLM) e falha se a próxima poda derrubar uma regra, um campo do JSON de saída, um placeholder — ou se o prompt voltar a passar de 17k chars.

## O JSON truncado (corrigido em 2026-08-01)

Sintoma: o classificador "não devolvia JSON" e o turno caía no `_fallback_classify` por palavra-chave — que não lê o prompt nem o histórico, e por isso mandava *"quantos GB livres no meu disco"* para o cérebro e *"manda pra mim aquele pdf"* para o WhatsApp.

Causa: `max_tokens=300`. O contrato de saída tem 16 campos e cabe em ~250 tokens, mas em modelo que **raciocina** o raciocínio consome o orçamento antes da primeira chave — o conteúdo volta cortado no meio ou vazio. Pior: o código tratava isso como o *outro* modo de falha (o modelo respondendo em prosa ao usuário) e retentava com uma instrução de reparo — mandando parar de conversar quem estava sem espaço para escrever. A retentativa vinha truncada igual.

O conserto separa as duas doenças:

| Sintoma | Diagnóstico | Remédio |
|---|---|---|
| `finish_reason="length"` ou conteúdo vazio | acabou o orçamento | repete a MESMA pergunta com `_CLASSIFIER_MAX_TOKENS_RETRY` (4.096) |
| texto completo, mas é prosa | o modelo entrou em modo conversa (sessão `69feab87`) | instrução de reparo, como antes |

Mais duas mudanças na origem: o orçamento inicial subiu para **2.048** (com **4.096** na retentativa) e a chamada passa `disable_thinking=True`.

O teto é margem, não estimativa: `max_tokens` limita, não reserva — cobra-se o que o modelo gera, e ele para sozinho ao fechar o JSON. 2.048 dá ~14× a resposta típica (~150 tokens) e ~5× o pior caso realista (`pipeline_plan` com vários steps, ~400), com sobra para absorver raciocínio se algum provider ignorar o `disable_thinking`. 4.096 na retentativa é o **teto de saída mais baixo entre os modelos que o painel oferece** (`claude-3-haiku-20240307` para em 4.096) — pedir mais trocaria um turno truncado por um 400.

O teste amarra a RAZÃO, não o número (`_CLASSIFIER_MAX_TOKENS >= 10 × _CLASSIFIER_TYPICAL_OUTPUT_TOKENS`): foi exatamente assim que o 300 apodreceu sem ninguém notar — era 5× o contrato de fev/2026 e virou 2× quando ele dobrou de tamanho. Quando o contrato crescer de novo, quem cai é o teste, não o turno do usuário. Essa flag é **intenção do chamador**, não parâmetro de API: o `DeepSeekProvider` traduz para `extra_body={"thinking": {"type": "disabled"}}` (o mesmo campo já usado no `tool_choice` forçado) e os demais providers a engolem — se vazasse para o corpo da requisição, viraria 400.

Medido no golden set com `deepseek/deepseek-v4-flash`:

| | Antes | Depois |
|---|---:|---:|
| Acerto | inutilizável (13/46 vindos do fallback) | **44/46** |
| Truncagens / quedas para o fallback | 13 | **0** |

Cobertura offline em [`tests/router/test_classifier_truncation.py`](../tests/router/test_classifier_truncation.py) (os dois modos de falha, cada um com o seu remédio) e [`tests/router/test_disable_thinking_kwarg.py`](../tests/router/test_disable_thinking_kwarg.py) (a flag chega ao `extra_body` no DeepSeek e nunca vaza no corpo dos outros).

## Notas para a próxima poda

- `{{` e `}}` são escapes de `str.format()` — no prompt final viram `{` e `}`. Ao editar, **mantenha o escape duplo** em todo JSON literal, senão o `.format()` estoura `KeyError` (o teste `test_prompt_formats_without_stray_braces` pega isso).
- Os placeholders `{server_tools}`, `{client_tools}` e `{skills}` são preenchidos em runtime — o texto vem da `ToolSchema` de cada módulo, não daqui. Tool nova com `description` longa deve trazer um **`router_summary`** de uma linha; sem isso o catálogo volta a inchar sozinho.
- Regra × exemplo: exemplo ensina **um caso**, regra ensina a **classe**. Onde houver 3+ exemplos da mesma classe, cabe uma regra só.
- Cuidado ao cortar: várias regras têm um bug real por trás (as sessões citadas em comentário no `.py`). Antes de remover, confira o comentário correspondente no código — e rode o `--ab`.
