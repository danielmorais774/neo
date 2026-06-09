# Plano: Orquestrador de Pipelines + Núcleo Cognitivo (ex-`direct_response`)

> **Status:** proposta (planejamento — nenhum código alterado ainda).
> **Escopo:** `neo-core`. Introduzir encadeamento (cascade/handoff) de pipelines e
> formalizar o `direct_response` como o **Núcleo Cognitivo** (o "cérebro") do Neo.

---

## 1. Diagnóstico do estado atual

O fluxo hoje é **single-shot**. Em [`process_user_input`](../modules/desktop/ai_service.py):

```
classify() → 1 RoutingDecision → 1 intent → 1 handler → fim
```

O dispatch (`ai_service.py:657-716`) é um `if/elif` que escolhe **um** handler e
termina. Cada handler (`_handle_direct_response`, `_handle_home_assistant`, …) é um
gerador isolado que dá `yield` em `AIServiceResponse` e **persiste sua própria
mensagem** na sessão.

O **único** encadeamento existente é hardcoded: `_handle_home_assistant`
(`ai_service.py:1685-1723`) cai para `_handle_direct_response` quando a skill não é
acionável. Isso prova que "um handler chamar outro" já funciona — só não é
generalizado.

Skills passam por [`SkillExecutor.execute()`](../core/skills/skill_executor.py) e
retornam um [`SkillResult`](../core/skills/__init__.py) `(ok, message, …)`
(não-streaming). Já o `direct_response` é **streaming**. Essa assimetria é central
para o design.

**O que falta:** um *input derivado* por um estágio (ex.: o cérebro analisa o PKS e
gera `"ligar o ar-condicionado do quarto"`) ser entregue como **entrada limpa** a
outro pipeline (HA) — isolado mas conectado.

---

## 2. Análise: `direct_response` virou o cérebro — e o que fazer com isso

### 2.1 Constatação

O `DIRECT_RESPONSE` nasceu como "conversa / conhecimento geral, sem ferramentas".
Mas acumulou responsabilidades cognitivas e **deixou de ser um par das skills**:

- leitura/escrita do **PKS** (`learn_before_response`, `ensure_user_pks`);
- **seleção de contexto** com orçamento (`PromptContextSelector.build_prompt`);
- **loop de clarificação** (`[[NEED_CLARIFICATION]]`);
- registro determinístico de **dispositivo**;
- extração de **preferências** e pipeline de **memória** pós-resposta.

Ou seja: **todo o conhecimento do usuário (PKS) e o raciocínio profundo já vivem
no prompt do `direct_response`.** Com o orquestrador, ele também passa a ser quem
**decide condicionalmente** acionar skills. O modelo mental correto é:

```
                       ┌─ simples (imperativo direto) ─→ SKILL (HA / WhatsApp)
INTENT (Router, triagem)
                       └─ complexo / condicional / PKS ─→ NÚCLEO COGNITIVO ─→ skills/pipelines
```

Há, portanto, **dois "cérebros" de naturezas diferentes**, e isso é proposital:

| Camada | Quem | Modelo | Papel |
|--------|------|--------|-------|
| **Triagem (Router)** | `IntentRouter` | barato/rápido (groq llama-3.3) | decide *direto-para-skill* vs *precisa-pensar* |
| **Núcleo Cognitivo** | ex-`direct_response` | modelo principal | raciocina com PKS/contexto e **orquestra** skills via handoff |

### 2.2 Recomendação sobre o rename

**Sim, vale renomear** — e o custo é baixo:

- O `intent` é **interno ao neo-core**. O [`_to_core_message`](../grpc_server.py)
  converte para protobuf por `is_final`/`message` (AgentResponse/AgentThinking),
  **não** pelo valor do intent. O proto `CoreMessage` **não tem campo `intent`**.
  → **Zero impacto em proto/cliente/wire.**
- Raio de impacto é **só interno**: ~63 referências a `DIRECT_RESPONSE` + 5 testes.
  O literal `"direct_response"` só aparece na definição do enum e num label de
  observabilidade.

**Nome recomendado:** `COGNITIVE` (papel: **"Núcleo Cognitivo" / Cognitive Core**).
Alternativas consideradas: `REASONING` (bom, neutro), `BRAIN` (informal),
`AGENT` (colide com `AgentExecutor`). Recomendo `COGNITIVE`.

Migração sugerida (segura, incremental):

1. Adicionar `IntentType.COGNITIVE = "cognitive"`.
2. Manter `DIRECT_RESPONSE` como **alias deprecado** apontando para o mesmo handler
   durante a transição (evita quebrar testes/branches num PR só).
3. Renomear `_handle_direct_response` → `_handle_cognitive` (manter wrapper fino com
   o nome antigo até migrar os chamadores internos).
4. Atualizar prompt do Router, logs e o label de observabilidade `stage`.
5. Remover o alias quando todas as referências internas migrarem.

### 2.3 Princípio anti-"god object"

O cérebro **não deve** absorver lógica de skill. Ele apenas **raciocina e emite
handoffs**; a execução fica nas skills isoladas. O próprio orquestrador (seção 4)
extrai a lógica de encadeamento de dentro do handler, ajudando a conter o
crescimento do já grande `ai_service.py` (~3.4k linhas).

E o roteador deve continuar mandando **imperativos diretos** ("acende a luz") para a
skill **sem** passar pelo cérebro — por latência e custo. O cérebro é para o que é
**condicional, dependente de PKS ou multi-etapas**.

---

## 3. O conceito: `PipelineOrchestrator` com 2 gatilhos

Camada **entre a classificação e o dispatch**. Em vez de despachar 1 handler,
executa uma **fila de steps**; cada step pode gerar novos steps (handoffs). Dois
modos alimentam a mesma fila:

| Modo | Quem decide a cadeia | Quando | Exemplo |
|------|----------------------|--------|---------|
| **A — Handoff em runtime** | o próprio Núcleo Cognitivo, após ler o PKS | ação **condicional** | "se eu costumo usar, então pode ligar" |
| **B — Cascade declarada** | o `IntentRouter` | N ações **independentes** óbvias | "apaga as luzes **e** manda whatsapp" |

O caso principal é o **Modo A** (a decisão de chamar o HA depende de analisar o PKS,
o Router não tem como saber de antemão).

---

## 4. Abstrações novas (ilustrativo)

```python
# core/pipeline/types.py  (NOVO)

@dataclass
class Handoff:
    """Um estágio pede, em runtime, que outro pipeline rode a seguir."""
    target_intent: IntentType
    input_text: str                 # input LIMPO e auto-contido p/ o próximo
    target_skill: str | None = None
    reason: str = ""
    on_failure: str = "stop"        # stop | continue
    user_visible: bool = True

@dataclass
class PipelineStep:
    intent: IntentType
    input_text: str                 # pode != user_message original
    skill_name: str | None = None
    decision: RoutingDecision | None = None
    origin: str = "classifier"      # classifier | handoff | fallback
    depth: int = 0

@dataclass
class StepOutcome:
    ok: bool
    final_text: str | None
    handoffs: list[Handoff] = field(default_factory=list)
```

```python
# core/pipeline/orchestrator.py  (NOVO)

class PipelineOrchestrator:
    MAX_STEPS = 4
    MAX_DEPTH = 3

    def __init__(self, ai_service):
        self.ai = ai_service        # reusa os _handle_* existentes

    async def run(self, initial_steps, *, session_id, history, ...):
        queue = deque(initial_steps)
        visited: set[tuple] = set()
        ran = 0

        while queue and ran < self.MAX_STEPS:
            step = queue.popleft()
            key = (step.intent, _normalize(step.input_text))
            if key in visited or step.depth > self.MAX_DEPTH:
                continue            # anti-loop A→B→A
            visited.add(key)
            ran += 1

            outcome = StepOutcome(ok=True, final_text=None)
            async for resp, oc in self._run_step(step, history, ...):
                if resp is not None:
                    yield resp      # streaming/status segue chegando ao usuário
                if oc is not None:
                    outcome = oc

            # história compartilhada entre estágios = "conectado"
            if outcome.final_text:
                history = history + [{"role": "assistant", "content": outcome.final_text}]

            if not outcome.ok and step.origin != "classifier":
                break               # política de falha p/ cadeia dependente

            for h in outcome.handoffs:
                queue.append(PipelineStep(
                    intent=h.target_intent, input_text=h.input_text,
                    skill_name=h.target_skill, origin="handoff",
                    depth=step.depth + 1,
                ))
```

`_run_step` é um adaptador fino que chama o handler certo (`_handle_cognitive`,
`_handle_home_assistant`, …) e extrai o `StepOutcome` (com handoffs).

---

## 5. Onde pluga (mudança mínima)

No `process_user_input`, o bloco `if/elif` de dispatch (`ai_service.py:657-716`) vira:

```python
initial = self._seed_steps(decision, user_message)   # 1 step, ou N se houver pipeline_plan
orchestrator = PipelineOrchestrator(self)
async for resp in orchestrator.run(initial, session_id=sid, history=history, ...):
    if resp.is_final:
        final_response = resp.message
    yield resp
```

Os `_handle_*` **continuam iguais** — viram os executores-folha. Cadeia de tamanho 1
= comportamento atual (degrada graciosamente, regressão zero).

---

## 6. Mecanismo de handoff (reaproveitando padrão existente)

Já existe o padrão de **sentinela no stream**: `[[NEED_CLARIFICATION]]` é parseado
por `_extract_clarification_question` e removido do texto. **Replicar para handoff.**

Instrução nova no system prompt do Núcleo Cognitivo:

> Se, após analisar o contexto/PKS, você concluir que uma **ação** deve ser
> executada por uma skill, escreva normalmente sua resposta ao usuário e, **no
> final**, emita um bloco de controle:
> `[[HANDOFF:home_assistant]]ligar o ar-condicionado do quarto[[/HANDOFF]]`
> O texto dentro do bloco deve ser uma **ordem imperativa, limpa e auto-contida** —
> a skill não vê a conversa original.

No fim de `_handle_cognitive`, antes do `yield` final:

1. parseia `[[HANDOFF:...]]...[[/HANDOFF]]`;
2. **remove** do texto visível (igual ao sentinela de clarificação,
   `ai_service.py:1054`);
3. devolve `StepOutcome(handoffs=[Handoff(...)])`.

Para o **Modo B**: campo opcional `pipeline_plan: list[...]` em
[`RoutingDecision`](../shared/types.py) + seção no `ROUTER_SYSTEM_PROMPT`. Default
vazio → 1 step (nada quebra).

---

## 7. Decisões críticas (definidas para esta proposta)

1. **Anti-loop** — `MAX_STEPS=4`, `MAX_DEPTH=3` e set `(intent, input_normalizado)`
   já visitados. Reaproveitar [`loop_detector.py`](../core/agent/loop_detector.py).
2. **Persistência na sessão** — gravar cada step **visível** com
   `metadata={"pipeline_step": i, "origin": ...}` e `generate_embedding=False` nos
   intermediários, para não poluir o re-rank semântico do histórico.
3. **Streaming/UX** — terminar de streamar a parte conversacional **antes** de
   mostrar o status do próximo estágio (`"Executando: ligar o ar-condicionado…"`).
   O orquestrador serializa: tokens → boundary → resultado da skill.
4. **Input auto-contido** — o handoff carrega o input **transformado**, nunca o
   original. Passar o original + cadeia só em `extra` (rastreio), jamais para a
   decisão da skill. É o que garante "isolado mas conectado".
5. **Safety/aprovação** — handoff para ação externa (WhatsApp, abrir fechadura)
   **respeita** o fluxo de missão/confirmação existente. Auto-execução só para
   ações reversíveis/locais (luz, clima).
6. **Política de falha** — cadeia dependente para no primeiro erro e o cérebro
   explica; cascade independente continua e reporta parcial (`on_failure` no
   `Handoff`).
7. **Observabilidade** — o `trace` hoje loga 1 intent (`set_intent`). Estender para
   logar a cadeia (sub-spans por step com input/output/handoff) via
   `slog.log_event` / novo `log_pipeline_step`.
8. **Router conservador** — cascade eager (Modo B) só para "X **e** Y" explícitos;
   o condicional fica no Modo A (runtime), mais seguro.

---

## 8. Exemplos de `user_input` + tratamento esperado

**Ex. 1 — Handoff condicional (Modo A, caso principal)**
```
"Como eu gosto de usar ar-condicionado nas quintas? Se eu costumo usar, então pode ligar ele"
```
1. Router → `COGNITIVE` (pergunta sobre preferência + ação condicional).
2. Núcleo Cognitivo carrega PKS → "liga o AC do quarto nas quintas à noite".
3. LLM responde *"Você costuma ligar o ar do quarto nas quintas…"* + emite
   `[[HANDOFF:home_assistant]]ligar o ar-condicionado do quarto[[/HANDOFF]]`.
4. Orquestrador: yield do texto → status *"Executando no Home Assistant…"* →
   enfileira step HA.
5. `_handle_home_assistant` roda **isolado** com `"ligar o ar-condicionado do
   quarto"` → *"Liguei o ar do quarto."*
6. Usuário vê: explicação **+** confirmação da ação.

**Ex. 2 — Cascade declarada (Modo B)**
```
"Apaga todas as luzes da casa e manda um whatsapp pro porteiro avisando que vou dormir"
```
- Router → `pipeline_plan = [HOME_ASSISTANT("apagar todas as luzes da casa"),
  WHATSAPP_MESSAGING("avisar o porteiro que vou dormir")]`.
- Orquestrador roda em sequência. ⚠️ O step de WhatsApp **continua** passando pelo
  fluxo de missão/confirmação — handoff **não** burla aprovação externa.

**Ex. 3 — Saída de um estágio alimenta o próximo (cérebro como juiz)**
```
"Vê a previsão do tempo e, se for chover hoje, fecha as persianas"
```
- `SERVER_TOOL`(weather) → resultado volta ao Núcleo Cognitivo → avalia "vai
  chover" → `[[HANDOFF:home_assistant]]fechar as persianas[[/HANDOFF]]`.

**Ex. 4 — PKS dispara comunicação**
```
"Se eu ainda não almocei hoje, pede comida no iFood"
```
- `COGNITIVE` consulta PKS → handoff `WHATSAPP_MESSAGING("fazer pedido no iFood")`
  (com confirmação de missão).

**Ex. 5 — Sem cadeia (regressão zero)**
```
"acende a luz da sala"
```
- Router → HA direto, `plan` de 1 step. Idêntico a hoje.

---

## 9. Fases de implementação

1. **Fase 1 (núcleo, baixo risco):** `core/pipeline/types.py` + `orchestrator.py`;
   plugar no dispatch com cadeia de tamanho 1 (sem handoff) → provar regressão zero.
2. **Fase 2 (Modo A):** sentinela `[[HANDOFF]]` no Núcleo Cognitivo + parsing +
   `StepOutcome`. Liga o caso do ar-condicionado.
3. **Fase 3 (anti-loop + persistência + observabilidade):** budgets, tagging de
   mensagens, sub-spans no trace.
4. **Fase 4 (Modo B):** `pipeline_plan` no `RoutingDecision` + prompt do Router,
   conservador.
5. **Fase 5 (rename):** migrar `DIRECT_RESPONSE → COGNITIVE` com alias deprecado
   (ver §2.2), atualizando os ~63 usos internos + 5 testes.
6. **Fase 6:** safety gating de handoff para skills externas (reusar missões do
   WhatsApp).
