# Relatório Técnico: Roteamento de Memória e Treinamento do Neo

Data: 2026-05-26

Escopo: análise somente leitura do fluxo atual de ingestão de memória, preferências e treinamento do Neo. Este relatório não propõe alterações aplicadas; apenas mapeia comportamento, diagnóstico e plano de correção.

## 1. Fluxo Atual De Ingestão

A entrada principal do desktop passa pelo endpoint de chat e depois pelo `AIService`.

- `api/routes/desktop.py:299`: `handle_desktop_chat_message()` recebe `/chat`.
- `api/routes/desktop.py:345`: chama `ai_service.process_user_input(...)`.
- `modules/desktop/ai_service.py:280`: `AIService.process_user_input()` cria ou resolve sessão, salva mensagem e chama o roteador.
- `core/router/intent_router.py:106`: `IntentRouter.classify()` decide rota de execução, `needs_memory` e keywords.
- `modules/desktop/ai_service.py:567`: `_handle_direct_response()` monta o contexto para resposta direta.
- `modules/desktop/ai_service.py:661`: antes da resposta, chama `self.pks.learn_before_response(... memory_target="user")`.
- `modules/desktop/ai_service.py:670`: antes da resposta, chama também `self.pks.learn_before_response(... memory_target="system")`.
- `modules/desktop/ai_service.py:682`: injeta `self.pks.retrieve_system_context(...)` antes do retrieval comum.
- `modules/desktop/ai_service.py:903`: depois da resposta, agenda `PreferenceExtractor.extract_and_save(...)`.
- `modules/desktop/ai_service.py:922`: depois da resposta, agenda `self.pks.learn(... memory_target="user")` e `self.pks.learn(... memory_target="system")`.

Trecho crítico:

```python
pks_system_prewrite = await self.pks.learn_before_response(
    user_id=user_uuid,
    user_message=user_message,
    source_message_id=source_message_id,
    memory_target="system",
)
```

```python
system_applied = await self.pks.learn(
    user_id=user_uuid,
    user_message=user_message,
    assistant_response=final_text,
    memory_target="system",
)
```

O mesmo texto de treino pode passar duas vezes pelo escritor de memória do sistema: antes e depois da resposta.

## 2. Arquivos E Funções Relevantes

- `api/main.py:158`: instancia `IntentRouter`, `PreferenceExtractor` e `PKSService`.
- `api/routes/desktop.py:299`: endpoint REST de chat desktop.
- `modules/desktop/ai_service.py:280`: orquestra sessão, roteamento, memória, contexto e resposta.
- `core/router/intent_router.py:25`: prompt do roteador de intenção. Decide `DIRECT_RESPONSE`, `CLIENT_TOOL`, `SERVER_TOOL`, etc.
- `core/pks/service.py:95`: fachada do PKS; delega escrita para `PKSWriter`.
- `core/pks/service.py:135`: `retrieve_system_context()` injeta personalidade, regras, procedimentos e preferências do Neo.
- `core/pks/writer.py:90`: `PKSWriter.learn()` escolhe `user` ou `system`.
- `core/pks/writer.py:367`: `_build_system_updates()` aplica heurísticas de treino, preferência de comunicação, estilo e memória sistêmica.
- `core/pks/writer.py:519`: `_build_agent_training_updates()` roteia treino para `identity`, `behavior_policies`, `procedures`, `training_examples` e `reflection`.
- `core/pks/writer.py:642`: `_training_item()` monta o item salvo.
- `core/session/preference_extractor.py:24`: prompt genérico de extração de preferências do usuário.
- `core/session/preference_extractor.py:65`: `extract_and_save()` salva preferências pessoais.
- `core/pks/system_bootstrap.py:163`: `NeoSystemBootstrap.ensure()` cria workspace `neo`, collections e entidades base.

## 3. Como O Sistema Decide O Destino Da Memória

O destino é decidido por camadas diferentes, sem uma classificação central única.

- `workspace`: em `PKSWriter.learn()`, `memory_target="system"` força workspace `neo`; `memory_target="user"` usa workspace pessoal.
- `collection`: em treino de sistema, `_build_agent_training_updates()` escolhe `identity`, `behavior_policies`, `procedures`, `training_examples` ou `reflection`.
- `entity`: nomes fixos como `neo-personality`, `neo-response-rules`, `neo-operating-procedures`, `neo-communication-preferences`.
- `entity type`: vem de constantes do bootstrap/escritor, como `identity`, `behavior_policy`, `operating_procedure`, `preference`, `correction_example`, `learned_lesson`.
- `owner`: em treino, `_training_item()` grava `created_by_user` como `str(user_id)` ou `"daniel"`.
- `target_agent`: hardcoded como `neo-core`.
- `target_workspace`: hardcoded como `neo`.
- `rule_key`: hash do texto normalizado inteiro da `description`.
- `properties`: merge JSON feito por `_merge_structured_properties()`; listas como `rules`, `procedures`, `examples`, `lessons`, `training_rules` deduplicam só por `rule_key`.

Trecho relevante:

```python
description = " ".join((user_message or "").strip().split())[:800]
rule_key = self._rule_key(description)
```

Isso explica parte central do problema: o `rule_key` nasce do texto bruto, não da regra canônica.

## 4. Diagnóstico Do Bug

### Envelope salvo como regra ativa

- `_training_item()` usa a mensagem inteira como `description`.
- Não há parser que separe envelope operacional de conteúdo real.
- Frases como “Neo, modo treino”, “salve no workspace do Neo” e “esta mensagem não é preferência pessoal” viram parte da regra.

### Texto bruto em `description`

```python
description = " ".join((user_message or "").strip().split())[:800]
```

### Duplicação em várias collections

- `_build_system_updates()` roda detectores independentes.
- Uma mesma mensagem pode gerar `training_updates`, `communication_policy` e `style_pref`.
- Não há decisão mutuamente exclusiva do tipo “isto é treino do agente, então não rode extrator de preferência/comunicação”.

Trecho relevante:

```python
training_updates = self._build_agent_training_updates(...)
communication_policy = self._extract_assistant_communication_policy(user_message)
style_pref = self._extract_system_style_preference(user_message)
```

### Duplicação antes/depois da resposta

- `AIService` chama `learn_before_response(... system)` e depois `learn(... system)`.
- A deduplicação depende de `rule_key`.
- Como `rule_key` é hash do texto bruto, variações pequenas ou truncamentos produzem chaves diferentes.

### Conteúdo de comportamento salvo como preferência

- `core/session/preference_extractor.py:24` trata “behavior preferences” como preferência pessoal.
- Existe skip por `_looks_like_agent_training()`, mas é heurístico e incompleto.
- Se a frase não bater exatamente nos marcadores, o LLM extractor pode salvar como preferência do Daniel.
- Conteúdos já salvos antes desse filtro continuam sendo carregados.

### Texto truncado

- `_training_item.description`: `[:800]`.
- `_extract_assistant_communication_policy.examples`: `[:500]`.
- `PreferenceExtractor.extract_and_save()`: `source_message=user_message[:500]`.
- `_infer_goals_properties()`, `_infer_profile_properties()`, `_infer_projects_properties()`, `_infer_world_state_properties()`: `[:240]`.
- fallback de artifact confirmado em `_build_system_updates()`: `[:320]`.

### `examples_good` e `examples_bad` vazios

- `_training_item()` só preenche `examples_bad` se encontrar tokens como `errado` ou `nao aja assim`.
- Só preenche `examples_good` se encontrar literalmente `correto seria`.
- Não existe extração estruturada de exemplos dentro da mensagem.

### Traits genéricos como `slightly_funny`

- `core/pks/writer.py:798`: `_infer_personality_properties()` mapeia qualquer humor/ironia para valores fracos.
- `sarcasm_level` é fixo em `0.4`.
- `traits.append("slightly_funny")` é usado para humor, sem intensidade como “deboche”, “humor ácido” ou “sarcasmo forte”.

Trecho:

```python
if properties["humor"]:
    traits.append("slightly_funny")
```

## 5. Pontos De Melhoria

- Criar um classificador único de envelope de treino antes dos extratores atuais.
- Se detectar treino do Neo, bloquear extração de preferência pessoal do usuário para aquela mensagem.
- Separar `raw_source_message` de `canonical_description`.
- Remover comandos operacionais antes de salvar regra ativa.
- Gerar regra canônica curta, estável e deduplicável.
- Deduplicar por `semantic_rule_key`, não por hash do texto bruto.
- Tratar `neo-communication-preferences` como uma subclasse de policy ou como projeção derivada, não como rota paralela para a mesma regra.
- Fazer `learn_before_response` e `learn` compartilharem idempotency key por `source_message_id`.
- Preencher `examples_good` e `examples_bad` com parsing real de blocos de exemplo, não só tokens literais.
- Representar personalidade com intensidade: `humor_style`, `sarcasm_intensity`, `edge_level`, `allowed_rudeness_boundaries`.

## 6. Proposta De Estrutura Ideal

### `neo-personality`

```json
{
  "persona_traits": ["direct", "dry_humor"],
  "tone": "pragmatic",
  "humor_style": "acidic_when_appropriate",
  "sarcasm_intensity": 0.7,
  "boundaries": ["do_not_be_cruel", "avoid_personal_attacks"],
  "source_refs": []
}
```

### `neo-response-rules`

```json
{
  "rules": [
    {
      "rule_key": "avoid_unrelated_historical_context",
      "description": "Evitar incluir fatos, objetivos, rotinas, treinos, projetos ou histórico não relacionado quando o usuário não mencionou o assunto ou quando isso não ajuda a resposta atual.",
      "scope": "response_generation",
      "priority": "high",
      "target_agent": "neo-core",
      "target_workspace": "neo",
      "created_by_user": "daniel",
      "source_message_id": "...",
      "confidence": 0.96
    }
  ]
}
```

### `neo-operating-procedures`

```json
{
  "procedures": [
    {
      "rule_key": "when_training_envelope_extract_rules_only",
      "trigger": "Mensagem contém envelope de treino do Neo",
      "action": "Separar envelope, extrair regras canônicas e não salvar comandos operacionais como regra ativa.",
      "priority": "high"
    }
  ]
}
```

### `neo-training-examples`

```json
{
  "examples": [
    {
      "type": "correction_example",
      "input_context": "...",
      "bad_behavior": "...",
      "good_behavior": "...",
      "lesson_key": "...",
      "source_message_id": "..."
    }
  ]
}
```

### Source/Audit Metadata

```json
{
  "source": {
    "raw_message": "...",
    "cleaned_training_content": "...",
    "removed_envelope_phrases": ["Neo, modo treino"],
    "created_by_user": "daniel",
    "created_at": "...",
    "source_message_id": "..."
  }
}
```

## 7. Riscos

- Quebrar memórias existentes se mudar nomes de entities/collections sem migração.
- Perder contexto se regras antigas forem removidas em vez de normalizadas.
- Sobrescrever preferências reais do Daniel ao tratar tudo sobre “você” como treino global.
- Confundir preferência pessoal de comunicação com regra global do Neo.
- Duplicar ainda mais regras se a migração não gerar `rule_key` estável.
- Afetar outros agentes se `target_agent` não for respeitado no retrieval.
- Alterar a ordem de injeção pode mudar comportamento do modelo de forma perceptível.
- Remover truncamento sem limite alternativo pode inflar contexto e custo.

## 8. Plano De Correção

1. Criar `TrainingEnvelopeParser` ou função equivalente antes do `PKSWriter`, retornando `is_training`, `envelope_phrases`, `clean_content`, `training_kind` e `source_audit`.
2. Tornar o roteamento mutuamente exclusivo: se `is_training=True`, não executar `PreferenceExtractor` nem `_extract_assistant_communication_policy()` como rota paralela, salvo se for uma projeção explícita derivada da regra canônica.
3. Substituir `_training_item.description` por `canonical_description`, mantendo o texto original apenas em `source.raw_message`.
4. Criar `rule_key` determinístico baseado em intenção canônica, por exemplo `avoid_unrelated_context`, não em hash do texto bruto.
5. Adicionar idempotência por `source_message_id + rule_key` para impedir duplicação entre `learn_before_response()` e `learn()`.
6. Separar claramente destinos: personalidade em `identity`, regras em `behavior_policies`, procedimentos em `procedures`, exemplos em `training_examples`, lições em `reflection`.
7. Melhorar extração de exemplos com campos `examples_good`, `examples_bad`, `bad_behavior`, `expected_behavior`.
8. Atualizar `retrieve_system_context()` para injetar regras de alta prioridade por `target_agent`, `target_workspace` e `scope`, evitando carregar artefatos brutos.
9. Criar migração/normalização das entidades existentes para remover envelopes salvos como regras ativas e consolidar duplicatas.
10. Adicionar testes cobrindo mensagem com “Neo, modo treino”, mensagem de preferência pessoal real, correção com exemplo bom/ruim, e idempotência pre/post resposta.
