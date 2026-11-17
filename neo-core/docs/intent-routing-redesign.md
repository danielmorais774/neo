# Plano: Redesenho do Roteamento de Intent (fim do whack-a-mole)

> **Status:** pronto para implementar.
> **Motivação:** o classificador barato erra atos de fala (condicional, definição,
> hipótese) fazendo match raso de palavra-chave, e ainda alucina `entity_id`. Os
> guards determinísticos viraram remendo de um conjunto *infinito* de frases.
> **Princípio:** inverter o default. O barato só faz *fast-path* do que é
> obviamente simples; todo o resto cai no **COGNITIVE** (modelo forte + PKS +
> handoff), que é estritamente melhor em ambiguidade. O determinístico vira
> otimização, não mecanismo de correção.

---

## 0. Evidência (por que é seguro)

Dois erros reais, mesmo padrão — ato de fala mal classificado:

| Mensagem | Ato de fala real | Classificou | Efeito colateral |
|---|---|---|---|
| "se eu costumo, pode ligar" | condicional | HOME_ASSISTANT | acionou skill |
| "cadastrar uma brincadeira… você pode desligar a luz" | definição de regra | HOME_ASSISTANT | + alucinou `turn_on light.quarto` |

Consumo atual dos campos de execução do `RoutingDecision` (verificado no código):

| Campo | HOME_ASSISTANT | WHATSAPP | SERVER_TOOL | CLIENT_TOOL |
|---|---|---|---|---|
| `tool_arguments`/`entity_id` | **NÃO usa** (pipeline resolve) | **usa** (`recipient`/`text`) | — | usa |
| `tool_name` | não usa | — | hint/log | usa |
| `raw_command` | — | — | — | **usa** |

→ Remover a extração de execução **só do HA** é seguro e mata a alucinação + o viés
pró-HA. WhatsApp/Client mantêm o que consomem.

---

## 1. Workstream A — Separar roteamento de execução

**Objetivo:** o router escolhe o **caminho/skill**, não a ferramenta interna da skill.

**Arquivos:** `core/router/intent_router.py`.

1. **Tirar as ferramentas de skill do "palette" do router.** Hoje `_format_tools("server-side")` lista `ha_list_entities`, `ha_get_state`, `ha_call_service`, `whatsapp_send_message`, etc. — isso enviesa ("já que tenho `ha_call_service`…") e convida à alucinação.
   - Filtrar tools "donas de skill" da listagem do prompt (excluir prefixos `ha_`, `whatsapp_`). O router só vê tools genuínos do caminho SERVER_TOOL/CLIENT_TOOL (`get_weather`, `list_connected_devices`, tools de client).
   - Texto novo na seção HOME_ASSISTANT do prompt: *"home_assistant é uma SKILL. Para controlar a casa, use `intent=HOME_ASSISTANT`, `skill_name=home_assistant` e PARE. NÃO escolha tools `ha_*` nem preencha `entity_id`/`tool_arguments` — a skill resolve o dispositivo sozinha."*
2. **Output:** instruir o modelo a deixar `tool_arguments`/`entity_id`/`raw_command` **nulos** quando `intent=HOME_ASSISTANT`. Manter `recipient`/`text` para WhatsApp e `raw_command` para CLIENT_TOOL.
3. **Parsing (`_parse_classification`):** se `intent==HOME_ASSISTANT`, **descartar** `tool_arguments`/`tool_name`/`raw_command` (defensivo, mesmo que o modelo emita).

**Aceite:** payload do classificador para mensagens de casa não traz mais `ha_*`/`entity_id`; HA continua funcionando (a pipeline resolve a entidade). Custo de saída do router **menor**.

---

## 2. Workstream B — Ato de fala + viés-pro-cérebro (o coração)

**Objetivo:** generalizar o princípio em vez de decorar frases. O modelo passa a
**raciocinar sobre o ato de fala** e o código aplica um **override de conjunto
fechado**: skill com efeito colateral só dispara em imperativo claro e confiante.

**Arquivos:** `core/router/intent_router.py`, `shared/types.py`, `shared/config.py`.

### B1. Novo campo `speech_act` no output do router
Taxonomia (PT, ensinada com few-shot curto que ensina o PRINCÍPIO):
- `imperative` — ordem para fazer agora ("acende a luz", "manda zap pra mãe").
- `question` — pergunta ("qual o clima?", "que dispositivos tenho?").
- `conditional` — ação condicionada ("se eu costumo, pode ligar").
- `definition` — definir/cadastrar regra/comportamento ("cadastra uma brincadeira: se eu te provocar, você pode…").
- `chitchat` — conversa/sem ação.

Adicionar `speech_act: str` em `RoutingDecision` e no schema de saída do prompt.

### B2. Override determinístico e PRINCIPLED (não é regex de frase)
Pós-LLM, em `classify()` (e no fallback), aplicar sobre a saída do PRÓPRIO modelo:

```python
SIDE_EFFECT_SKILLS = {IntentType.HOME_ASSISTANT, IntentType.WHATSAPP_MESSAGING}
BIAS_TO_BRAIN_THRESHOLD = settings.router_skill_confidence_threshold  # ex.: 0.75

def _bias_to_brain(decision):
    # Uma skill com efeito colateral SÓ dispara em imperativo claro e confiante.
    # Qualquer condicional/definição/pergunta, ou baixa confiança → COGNITIVE,
    # que tem PKS + modelo forte e ainda pode acionar a skill via [[HANDOFF]].
    if decision.intent in SIDE_EFFECT_SKILLS:
        if decision.speech_act != "imperative" or decision.confidence < BIAS_TO_BRAIN_THRESHOLD:
            return _to_cognitive(decision)  # preserva knowledge_keywords; needs_memory=True
    return decision
```

Por que isso fecha o conjunto: hoje os guards tentam **enumerar o que é cognitive**
(aberto/infinito). Aqui só deixamos passar **imperativo claro** (fechado/finito) e
**falhamos pro lado seguro** (cérebro). `SERVER_TOOL`/`CLIENT_TOOL` (read-only,
pergunta É o gatilho normal) **não** entram no override — evita mandar "qual o
clima?" pro cérebro sem dado ao vivo.

### B3. Prompt: ensinar o ato de fala + default pro cérebro
- Seção nova "Ato de fala": definir as 5 categorias com 1 exemplo cada.
- Regra dura: *"Só use HOME_ASSISTANT/WHATSAPP quando `speech_act=imperative` e você tem ALTA confiança. Condicional, definição/cadastro de regra, hipótese ou dúvida → DIRECT_RESPONSE. Na dúvida, DIRECT_RESPONSE."*
- Few-shot dos dois casos reais (ensinando o porquê), ex.:
  - "cadastra uma brincadeira: se eu te provocar, você pode desligar a luz" → `speech_act=definition`, `intent=DIRECT_RESPONSE` (definir regra ≠ executar agora; **não** invente entity_id).
  - "se eu costumo usar nas quintas, pode ligar" → `speech_act=conditional`, `intent=DIRECT_RESPONSE`.

**Config:** `router_skill_confidence_threshold: float = 0.75`, `router_bias_to_brain_enabled: bool = True` (rollback).

**Aceite:** as duas frases reais → `DIRECT_RESPONSE`; "acende a luz da sala" → HOME_ASSISTANT; "qual o clima?" → SERVER_TOOL (intacto).

---

## 3. Workstream C — Rebaixar os guards determinísticos

**Objetivo:** o determinístico vira **fast-path de alta precisão**, não correção.

**Arquivos:** `core/router/intent_router.py`.

1. **Manter** os guards atuais que são corretos e grátis (training, memory-directive, known-devices, referential, e o cognitive que já criamos) — eles aceleram casos certos e **falham pro lado seguro** (todos vão pra DIRECT_RESPONSE).
2. **Congelar a expansão por frase.** Regra de ouro no topo do arquivo: *"Não adicione novos padrões de frase aqui para corrigir misroute pro cérebro — isso é trabalho do Workstream B. Guards só para (a) fast-path determinístico de alta precisão ou (b) bypass de segurança."*
3. **Remover** o guard `_is_brain_reasoning_request` **só se** o Workstream B cobrir os casos com folga (medir antes; provavelmente manter, pois é barato e ortogonal).

**Aceite:** nenhum guard novo por frase; B é o mecanismo primário.

---

## 4. Workstream D — Modelo do router (só se necessário, MEDIDO)

Não trocar o modelo às cegas. Depois de A+B+C em produção, medir taxa de misroute.
Se ainda alta:
- **Opção D1 (escalonamento):** quando `confidence < threshold` **e** `speech_act ∈ {conditional, definition}`, em vez de só cair no cérebro, opcionalmente reclassificar com um modelo melhor. (Custo limitado ao caso difícil.)
- **Opção D2 (upgrade direto):** subir o router pra um modelo melhor no groq. Medir latência (roda em toda mensagem).

Decidir por **dados**, não por achismo.

---

## 5. Testes (incluindo as duas regressões reais)

`tests/test_intent_router_speech_act.py` (novo):
- `"cadastra uma brincadeira: se eu te provocar, você pode desligar a luz"` → `DIRECT_RESPONSE` (regressão 6bb1d340/3e51616c).
- `"se eu costumo usar nas quintas, pode ligar"` → `DIRECT_RESPONSE`.
- `"acende a luz da sala"` → `HOME_ASSISTANT` (não pode roubar imperativo).
- `"desliga o ar do quarto"` → `HOME_ASSISTANT`.
- `"manda um oi pra minha mãe"` → `WHATSAPP_MESSAGING` (imperativo).
- `"se eu não almocei, pede comida"` → `DIRECT_RESPONSE` (condicional → cérebro → handoff c/ confirmação).
- `"qual o clima hoje?"` → `SERVER_TOOL` (pergunta read-only NÃO vai pro cérebro).
- HA: garantir que o parsing descarta `entity_id`/`tool_arguments`.

`_bias_to_brain` testado em isolamento (unit) com `speech_act`/`confidence` mockados — determinístico, sem LLM.

---

## 6. Rollback (flags)

- `router_bias_to_brain_enabled` (default `true`) — desliga o override do Workstream B.
- `router_skill_confidence_threshold` (default `0.75`) — ajusta a régua sem deploy.
- Workstream A é seguro por construção (HA não consumia os campos).

---

## 7. Ordem de implementação

1. **A** (separar execução do HA) — maior efeito, menor risco; sozinho já mata a alucinação.
2. **B** (speech_act + viés-pro-cérebro) — o coração; corrige o pragmatismo de forma geral.
3. **C** (rebaixar guards) — documental + congelar expansão.
4. **D** (modelo) — só depois de medir.

Cada um é um PR; A e B já podem ir juntos (ambos no `intent_router.py`).

---

## 8. Métrica de sucesso

- As 2 frases reais (e variações) → `DIRECT_RESPONSE` de forma estável.
- Zero alucinação de `entity_id` no payload do router (grep nos logs de sessão).
- Imperativos simples ("acende a luz", "manda zap") seguem rápidos no fast-path.
- Sem necessidade de novo guard por frase nos próximos misroutes — eles caem
  naturalmente no cérebro via baixa confiança / ato de fala.
