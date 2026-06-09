# Plano de Implementação — Orquestrador de Pipelines + Núcleo Cognitivo

> Companheiro do [design](./pipeline-orchestrator-plan.md). Aqui está o **passo a
> passo executável**: arquivos, assinaturas, critérios de aceite, testes e rollback
> por fase. Cada fase é um PR independente e deixa o sistema funcionando.

**Princípio geral:** regressão zero a cada fase. Cadeia de tamanho 1 = comportamento
atual. Nada de big-bang.

---

## Fase 0 — Preparação (sem mudança de comportamento)

**Objetivo:** criar o pacote e os tipos, sem ligar em lugar nenhum.

**Arquivos novos:**
- `core/pipeline/__init__.py`
- `core/pipeline/types.py` — `Handoff`, `PipelineStep`, `StepOutcome` (dataclasses da §4 do design).
- `core/pipeline/_normalize.py` (ou função interna) — normalização de input para a chave anti-loop (lower + strip de acentos, reusando `_strip_accents` do `intent_router`).

**Tarefas:**
- [ ] Criar dataclasses com defaults seguros (`on_failure="stop"`, `user_visible=True`, `depth=0`).
- [ ] `PipelineStep.key()` → `(intent, _normalize(input_text))`.

**Critério de aceite:** `python -c "from core.pipeline.types import Handoff, PipelineStep, StepOutcome"` importa sem erro. Nenhum comportamento alterado.

**Testes:** `tests/pipeline/test_types.py` — construção, defaults, `key()` estável.

**Rollback:** deletar o pacote (nada o importa ainda).

---

## Fase 1 — Orquestrador com cadeia de tamanho 1 (regressão zero)

**Objetivo:** rotear o dispatch atual através do orquestrador, **sem handoff ainda**.

**Arquivos novos:**
- `core/pipeline/orchestrator.py` — `PipelineOrchestrator` (esqueleto da §4).

**Arquivos editados:**
- `modules/desktop/ai_service.py`:
  - Novo método `_seed_steps(decision, user_message) -> list[PipelineStep]` → retorna **1 step** a partir do `decision.intent` (Modo B fica para a Fase 4).
  - Novo método `_run_step(step, ...)` → adaptador que chama o `_handle_*` certo e produz `(AIServiceResponse | None, StepOutcome | None)`. Nesta fase o `StepOutcome` sempre vem com `handoffs=[]`.
  - Substituir o bloco `if/elif` de dispatch (`ai_service.py:657-716`) pela chamada ao orquestrador (§5 do design).

**Detalhe do `_run_step` (Fase 1):** os handlers atuais só dão `yield AIServiceResponse`. O adaptador encapsula cada handler e, ao final, devolve um `StepOutcome(ok=True, final_text=<última msg final>, handoffs=[])`. **Nenhuma assinatura de `_handle_*` muda.**

**Critério de aceite:**
- [ ] Todos os 5 caminhos (`DIRECT_RESPONSE`, `SERVER_TOOL`, `CLIENT_TOOL`, `WHATSAPP_MESSAGING`, `HOME_ASSISTANT`) funcionam idênticos a antes.
- [ ] `MAX_STEPS`/`MAX_DEPTH` presentes mas inertes (fila nunca passa de 1).
- [ ] Streaming do `direct_response` inalterado (tokens chegam na mesma ordem).

**Testes:**
- Rodar a suíte existente sem alterações: `tests/test_intent_router*.py`, `test_device_routing.py`, `test_home_assistant_service.py` etc. devem passar.
- `tests/pipeline/test_orchestrator_single.py` — mock do `ai_service`, verifica que 1 step → 1 chamada de handler → mesmos `AIServiceResponse`.

**Rollback:** reverter o bloco de dispatch para o `if/elif` (manter como comentário/branch atrás de flag `PIPELINE_ORCHESTRATOR_ENABLED` durante a fase facilita).

> **Recomendação:** proteger a troca com env flag `PIPELINE_ORCHESTRATOR_ENABLED`
> (default `true`) por 1–2 ciclos, para rollback instantâneo em produção.

---

## Fase 2 — Handoff em runtime (Modo A) — o caso do ar-condicionado

**Objetivo:** o Núcleo Cognitivo pode emitir `[[HANDOFF:skill]]...[[/HANDOFF]]` e o
orquestrador encadeia o próximo pipeline.

**Arquivos novos:**
- `core/pipeline/handoff_parser.py` — `extract_handoffs(text) -> (clean_text, list[Handoff])`. Espelha `_extract_clarification_question` (`ai_service.py:1012`) e a limpeza de sentinela (`ai_service.py:1054`).

**Arquivos editados:**
- `modules/desktop/ai_service.py`:
  - Constante `_HANDOFF_INSTRUCTION` (system prompt) listando as skills elegíveis e o formato do bloco. Injetar junto com `_CLARIFICATION_INSTRUCTION` (`ai_service.py:877`).
  - Em `_handle_direct_response`, antes do `yield` final: parsear handoffs, **remover do texto visível**, e expor os `Handoff` ao adaptador. Mecanismo sugerido: o handler dá `yield` num `AIServiceResponse` final com `metadata["handoffs"]=[...]` (sem alterar a assinatura do gerador); o `_run_step` lê esse metadata e monta o `StepOutcome`.
  - `_run_step`: ao detectar `metadata["handoffs"]`, popular `StepOutcome.handoffs`.
- `core/pipeline/orchestrator.py`: ativar o enfileiramento de handoffs (a fila já existe da Fase 1).

**Mapeamento skill → intent** (para o `Handoff.target_intent`): tabela em
`core/pipeline/orchestrator.py` (`home_assistant → HOME_ASSISTANT`,
`whatsapp_messaging → WHATSAPP_MESSAGING`). Skill desconhecida no bloco → ignorar +
logar (não confiar cegamente no LLM).

**Critério de aceite:**
- [ ] Ex.1 do design (ar-condicionado): cérebro responde + HA executa o input limpo.
- [ ] O texto do handoff **nunca** vaza para o usuário.
- [ ] A skill recebe `user_message = input_text` (limpo), com o original só em `extra`.
- [ ] Sem handoff no texto → comportamento da Fase 1 (1 step).

**Testes:**
- `tests/pipeline/test_handoff_parser.py` — extração, múltiplos blocos, bloco malformado, skill desconhecida.
- `tests/pipeline/test_orchestrator_handoff.py` — cérebro (mock) emite handoff → HA (mock) é chamado com input limpo.

**Rollback:** não injetar `_HANDOFF_INSTRUCTION` (o parser vira no-op pois o LLM não emite o bloco). Flag `PIPELINE_HANDOFF_ENABLED`.

---

## Fase 3 — Robustez: anti-loop, persistência e observabilidade

**Objetivo:** tornar o encadeamento seguro e auditável.

**Arquivos editados:**
- `core/pipeline/orchestrator.py`:
  - Ativar `visited`-set `(intent, input_normalizado)` + `MAX_STEPS=4` / `MAX_DEPTH=3`.
  - Avaliar reuso de [`loop_detector.py`](../core/agent/loop_detector.py) para padrões repetidos.
- `modules/desktop/ai_service.py`:
  - Persistência: cada step **visível** gravado com `metadata={"pipeline_step": i, "origin": step.origin}`; intermediários com `generate_embedding=False` (decisão §7.2).
- `observability/session_file_logger.py`:
  - Novo `log_pipeline_step(index, step, outcome)`.
- Trace: estender para registrar a cadeia como sub-spans (hoje `trace.set_intent` loga 1; ver `ai_service.py:632`).

**Critério de aceite:**
- [ ] Cadeia A→B→A é cortada (chave repetida).
- [ ] Estouro de `MAX_STEPS` para com mensagem coerente, sem travar.
- [ ] Histórico não fica poluído por turnos intermediários (re-rank semântico intacto).
- [ ] Debug panel mostra os N steps com input/output/handoff.

**Testes:**
- `tests/pipeline/test_orchestrator_loops.py` — ciclo, profundidade máxima, budget.
- `tests/pipeline/test_step_persistence.py` — flags de metadata/embedding por step.

**Rollback:** baixar limites/ desligar tagging (cosmético, sem quebrar fluxo).

---

## Fase 4 — Cascade declarada pelo Router (Modo B)

**Objetivo:** o Router pode emitir uma sequência de steps para ações independentes.

**Arquivos editados:**
- `shared/types.py`: campo opcional `pipeline_plan: list[PipelineStepSpec] | None = None` em `RoutingDecision` (default `None` → 1 step).
- `core/router/intent_router.py`: seção nova no `ROUTER_SYSTEM_PROMPT` ("se houver N ações **independentes** explícitas com 'e', emita `pipeline_plan`"). Parsing em `_parse_classification`.
- `modules/desktop/ai_service.py`: `_seed_steps` passa a expandir `pipeline_plan` em N steps quando presente.

**Critério de aceite:**
- [ ] Ex.2 do design ("apaga as luzes **e** manda whatsapp") gera 2 steps.
- [ ] Mensagem sem cascata explícita continua 1 step (Router conservador — §7.8).
- [ ] WhatsApp no plano ainda passa pela confirmação de missão.

**Testes:**
- `tests/test_intent_router_cascade.py` — frases com/sem "e", garante não-eagerness.
- `tests/pipeline/test_orchestrator_cascade.py` — N steps sequenciais + política de falha.

**Rollback:** ignorar `pipeline_plan` no `_seed_steps` (volta a 1 step).

---

## Fase 5 — Rename `DIRECT_RESPONSE → COGNITIVE` (Núcleo Cognitivo)

**Objetivo:** alinhar o nome ao papel real. **Interno apenas** (sem impacto em
proto/cliente — confirmado: `_to_core_message` não usa o valor do intent).

**Passos (incrementais, na ordem):**
1. [ ] `shared/types.py`: adicionar `COGNITIVE = "cognitive"`; manter
   `DIRECT_RESPONSE = "direct_response"` como **alias deprecado** (docstring).
2. [ ] `ai_service.py`: renomear `_handle_direct_response → _handle_cognitive`
   (wrapper fino com o nome antigo até migrar chamadores).
3. [ ] Migrar os ~63 usos internos de `DIRECT_RESPONSE` → `COGNITIVE` em lotes
   (dispatch, fallbacks, força-intent, logs, label `stage="direct_response"`).
4. [ ] Atualizar `ROUTER_SYSTEM_PROMPT` e exemplos.
5. [ ] Atualizar os 5 testes que referenciam `DIRECT_RESPONSE`
   (`test_intent_router*`, `test_device_routing`, `test_memory_routing`,
   `test_context_selector`).
6. [ ] Remover o alias quando não houver mais referências.

**Critério de aceite:**
- [ ] Suíte inteira verde.
- [ ] `grep -rn "DIRECT_RESPONSE" --include=*.py | grep -v .venv` → vazio (pós-remoção do alias).
- [ ] Cliente desktop inalterado (sem deploy do desktop necessário).

**Rollback:** o alias garante que parar no meio não quebra nada.

---

## Fase 6 — Safety gating de handoff para skills externas

**Objetivo:** handoff para ação externa/irreversível respeita aprovação.

**Arquivos editados:**
- `core/pipeline/orchestrator.py` / `_run_step`: para `target_intent ∈
  {WHATSAPP_MESSAGING, ...}`, **não** auto-executar — encaminhar pelo fluxo de
  missão/confirmação já existente (`_handle_whatsapp_messaging`).
- Tabela de política: `auto_exec` por intent (HA luz/clima = sim; WhatsApp/fechadura = requer confirmação).

**Critério de aceite:**
- [ ] Handoff → WhatsApp gera missão/confirmação, nunca envio automático.
- [ ] Handoff → HA (luz/clima) executa direto.

**Testes:** `tests/pipeline/test_handoff_safety.py`.

---

## Ordem de dependência

```
Fase 0 → Fase 1 → Fase 2 → Fase 3 → Fase 4 ─┐
                                    Fase 5 (rename, independente — pode rodar em paralelo após F2)
                                    Fase 6 (depende de F2)
```

- F0/F1 são pré-requisito de tudo.
- F2 destrava o caso do ar-condicionado (valor principal).
- F5 (rename) é ortogonal — pode entrar a qualquer momento depois da F2.
- F6 deve entrar antes de liberar handoff p/ WhatsApp em produção.

---

## Resumo de arquivos tocados

| Fase | Novos | Editados |
|------|-------|----------|
| 0 | `core/pipeline/{__init__,types}.py` | — |
| 1 | `core/pipeline/orchestrator.py` | `ai_service.py` (dispatch, `_seed_steps`, `_run_step`) |
| 2 | `core/pipeline/handoff_parser.py` | `ai_service.py` (prompt, `_handle_direct_response`), `orchestrator.py` |
| 3 | — | `orchestrator.py`, `ai_service.py`, `session_file_logger.py` |
| 4 | — | `shared/types.py`, `intent_router.py`, `ai_service.py` |
| 5 | — | `shared/types.py`, `ai_service.py`, `intent_router.py`, 5 testes |
| 6 | — | `orchestrator.py` |
