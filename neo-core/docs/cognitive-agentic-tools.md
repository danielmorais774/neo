# Plano: Cognitivo agêntico — o cérebro segura as tools de leitura

> **Status:** desenho para revisão (nada implementado).
> **Motivação:** o núcleo cognitivo (`DIRECT_RESPONSE`/`COGNITIVE`) responde por
> texto sem poder **ler/verificar** nada. A correção de "salvou mesmo?" ficou um
> paliativo determinístico (detector de frase + injeção). O certo: o cérebro
> **segura as tools de leitura e as chama quando decide** — agência, não regex.

---

## 1. Estado atual (o gap, com evidência)

`_handle_direct_response` faz **uma** chamada de streaming **sem tools**:

```python
async for chunk in provider.stream_complete(messages, model=..., max_tokens=...):
    if chunk.event == StreamEventType.TOKEN: full_response += chunk.content
    elif chunk.event == StreamEventType.ERROR: break
```

→ ele só consome `TOKEN`/`ERROR`. Não passa `tools`, não trata `TOOL_CALL_*`.
O handoff (Fase 2) deu a ele **delegar ações**; falta **ler/verificar inline**.

**Infra já existe** (nada novo a construir, só fiar):
- `provider.stream_complete(messages, tools=...)` aceita tools ([provider.py:73](../core/llm/provider.py#L73)).
- O provider OpenAI/DeepSeek emite `TOOL_CALL_START` → `TOOL_CALL_ARGS` (delta) →
  `TOOL_CALL_END` (args completos) e `DONE` com `finish_reason="tool_calls"`
  ([openai_provider.py:150-194](../core/llm/openai_provider.py#L150-L194)).
- `ToolExecutor.execute_tool_call` já despacha por módulo; `search_pks`,
  `get_weather`, `list_connected_devices`, `ha_get_state` já existem.

---

## 2. Arquitetura: cérebro com toolset de LEITURA + handoff para AÇÃO

```
COGNITIVE (cérebro)
  ├─ tools de LEITURA, inline (loop de tool com streaming):
  │     search_pks · get_weather · list_connected_devices · ha_get_state
  │     → lê a verdade quando decide, vê o resultado, continua a resposta
  └─ AÇÃO com efeito colateral → via HANDOFF (safety gating da Fase 6):
        ligar luz, mandar whatsapp …
```

Separação principled e geral: **lê à vontade, nunca dispara ação irreversível
inline.** Resolve, sem regex de frase: "salvou mesmo?", "qual o clima?", "que
dispositivos eu tenho?", "a luz tá acesa?".

---

## 3. Toolset curado (somente leitura)

| Tool | Por quê | Escopo |
|------|---------|--------|
| `search_pks` | verificar/recuperar o que está salvo (o caso que motivou) | por `user_id` |
| `ha_get_state` | "a luz tá acesa?", confirmar estado sem agir | — |
| `list_connected_devices` | "quais dispositivos tenho online?" | por `user_id` |
| `get_weather` | "vai chover?" sem precisar rotear pra SERVER_TOOL | — |

Regras de curadoria: **só read-only**; nada que mude estado (turn_on, send_message,
write_file) entra aqui — isso é handoff. Toolset pequeno (4) para não confundir
modelos rápidos. Tools de ação (`ha_call_service`, `whatsapp_send_message`) **não**
são oferecidas ao cérebro.

---

## 4. O loop de tool com streaming (coração)

Substituir o `async for` atual por um loop limitado (`MAX_TOOL_ROUNDS = 3`):

```
messages = [...]                       # system + handoff/verify notes + history
for round in range(MAX_TOOL_ROUNDS + 1):
    tools = COGNITIVE_READ_TOOLS if round < MAX_TOOL_ROUNDS else None  # último round sem tools
    pending = {}                        # id → {name, args}
    round_text = ""
    async for chunk in provider.stream_complete(messages, tools=tools, ...):
        match chunk.event:
          TOKEN          -> round_text += chunk.content; yield token ao usuário
          TOOL_CALL_END  -> pending[id] = {name, args}
          DONE/ERROR     -> break
    if not pending:
        full_response = round_text          # esta rodada é a resposta final
        break
    # executar as tools de leitura e realimentar
    yield status("consultando …")           # UX entre rodadas
    append assistant message com tool_calls
    for call in pending: append tool result (via ToolExecutor.execute_tool_call)
```

**Decisão de streaming:** rodadas que chamam tool quase nunca trazem prosa (o
modelo emite tool_calls OU texto). Então streamar ao vivo é seguro; na prática o
texto só "vaza" na rodada final (sem tools). Edge case (modelo que mistura texto +
tool call) é raro e some na rodada seguinte. Alternativa conservadora: segurar os
primeiros tokens até saber se a rodada tem tool — não recomendo (mata o streaming).

**Por que NÃO reusar `AgentExecutor`:** ele roda o loop com `complete()`
**não-streaming** ([agent_executor.py:68](../core/agent/agent_executor.py#L68)). O
cognitivo **precisa** streamar tokens (UX atual). Logo, loop de streaming próprio
no `_handle_direct_response`, não o AgentExecutor.

---

## 5. Execução + escopo de `user_id`

`search_pks` e `list_connected_devices` precisam do `user_id`. Reusar o mesmo seam
da Fase de devices: um **executor por request** que injeta `user_id` e delega o
resto ao global. Já existe `DevicesContextExecutor`; criar um
`CognitiveReadExecutor(global_executor, user_id, pks_search_exec, devices_exec)`
que intercepta `search_pks`/`list_connected_devices` (set_context) e passa
`get_weather`/`ha_get_state` adiante. Passado como `tool_executor` do loop.

---

## 6. Coexistência com handoff e clarificação

- O **handoff** (`[[HANDOFF:skill]]`) e a **clarificação** (`[[NEED_CLARIFICATION]]`)
  são parseados do **texto final** (`full_response`) — exatamente como hoje. O loop
  de tool roda ANTES; quando ele termina, o `full_response` já é a resposta final
  e os parsers de sentinela rodam sobre ela. Zero conflito.
- Ordem: read-tools (inline) → texto final → extrai handoff/clarificação → grava/yield.

---

## 7. O que acontece com o detector determinístico (rebaixamento)

Hoje: `_is_memory_verification_request` + `_build_pks_verification_note` fazem a
busca e **injetam o resultado**, pulando a agência do cérebro.

Com o cérebro agêntico, ele chama `search_pks` sozinho (guiado pela **regra 10**).
Duas opções para o detector:

- **(A) Remover** — confiar na regra + tool. Mais limpo; risco: modelo fraco às
  vezes não chama a tool numa pergunta de honestidade crítica.
- **(B) Rebaixar a *nudge*** (recomendado) — manter o detector, mas em vez de
  injetar resultado, injetar **instrução forte**: *"O usuário está duvidando de um
  salvamento — chame `search_pks` ANTES de responder."* O cérebro ainda exerce a
  agência (chama a tool de verdade); o nudge só **garante** o comportamento no
  caso crítico, sem hardcodar resultado. Custo zero, não é o anti-padrão de injeção.

Recomendo **(B)**: agência por padrão, garantia onde honestidade importa.

---

## 8. Tradeoffs, flags, capacidade do provider

- **Custo/latência:** rodada extra de LLM **só quando o cérebro chama uma tool**.
  Papo simples = um único streaming, zero a mais. Curar toolset pequeno reduz
  chamadas espúrias.
- **Provider:** deepseek (default) herda streaming-com-tools do OpenAI base ✅.
  Validar groq/gemini/grok/ollama: se um provider não suportar tools em streaming,
  o loop detecta (nunca emite TOOL_CALL) e degrada para o comportamento atual.
  Gate por flag + checagem de capacidade.
- **Flags:**
  - `cognitive_tools_enabled` (default `true`) — liga o toolset de leitura no cérebro.
  - `cognitive_max_tool_rounds` (default `3`).
- **Anti-loop:** `MAX_TOOL_ROUNDS` + dedup de chamadas idênticas (mesma tool+args)
  para o modelo não repetir a mesma leitura.

---

## 9. Mudanças por arquivo

| Arquivo | Mudança |
|--------|---------|
| `core/cognitive/read_tools.py` (novo) | lista `COGNITIVE_READ_TOOLS` (curada) + `CognitiveReadExecutor` (escopo user) |
| `modules/desktop/ai_service.py` | `_handle_direct_response`: trocar o `async for` por `_stream_with_tools(...)` (loop §4); montar o executor por request; passar tools |
| `modules/desktop/ai_service.py` | rebaixar `_build_pks_verification_note` → nudge (§7B); manter detector |
| `core/agent/context_selector.py` | regra 10 já existe; ajustar para "chame `search_pks`" (linguagem de tool) |
| `shared/config.py` | flags `cognitive_tools_enabled`, `cognitive_max_tool_rounds` |
| `observability/session_file_logger.py` | log das tool-calls do cognitivo (reusar `log_tool_request/result`) |

---

## 10. Testes

- `_stream_with_tools` com um **provider fake** que emite uma sequência
  TOKEN→TOOL_CALL_END→(executa)→TOKEN final: garante (a) tool executada, (b)
  resultado realimentado, (c) só o texto final vai ao usuário, (d) cap de rounds.
- `CognitiveReadExecutor` injeta `user_id` em `search_pks`/`list_connected_devices`
  e delega `get_weather`/`ha_get_state`.
- Caso real "salvou mesmo?": cérebro (fake) chama `search_pks`, recebe vazio →
  resposta admite que não salvou (sem injeção determinística).
- Regressão: papo simples ("oi") → nenhuma tool chamada, um único streaming.
- Dedup: duas chamadas idênticas no mesmo turno → segunda não re-executa.

---

## 11. Fases de implementação

1. **F1 — `read_tools.py` + `CognitiveReadExecutor`** (isolado, testável).
2. **F2 — `_stream_with_tools`** no `_handle_direct_response`, atrás de
   `cognitive_tools_enabled` (regressão zero quando off).
3. **F3 — rebaixar o detector** a nudge (§7B) + ajustar regra 10.
4. **F4 — observabilidade** das tool-calls + validar providers não-deepseek.

F1+F2 entregam o valor (o cérebro lê de verdade). F3 limpa o paliativo. F4 endurece.

---

## 12. Riscos / decisões em aberto

- **Modelo fraco não chama a tool** quando deveria → mitigado pelo nudge (§7B) no
  caso crítico; para o resto, é refinamento de prompt/modelo, não regex.
- **Streaming de rodada com texto + tool** (raro) → texto parcial some na rodada
  seguinte; aceitável. Decidir se vale o "segura-token" conservador (acho que não).
- **Quais read-tools** no toolset inicial — começar com as 4; `ha_get_state` e
  `get_weather` podem ficar para F4 se quiser o MVP só com `search_pks` +
  `list_connected_devices`.
- **Limite de contexto**: resultados de tool entram no histórico do turno; truncar
  payloads grandes (já fazemos em `compact_entity`/`search_pks` retorna compacto).
```
