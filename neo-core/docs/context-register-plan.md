# Plano — Registro de Contexto + Regulação de Tom v3 (nível produção)

> Status: **IMPLEMENTADO (F1–F8) em 2026-07-11** · Autor: Claude + Daniel
>
> Estado do rollout: merge com `neo_register_enabled=False` e
> `neo_register_shadow=True` — o shadow mode começa a logar `[Register]` no
> deploy; ligar de verdade é flipar `NEO_REGISTER_ENABLED=true` depois do
> shadow report (`scripts/register_shadow_report.py`). 614 testes verdes,
> evals offline 8/8 (`scripts/eval_tone.py`).
>
> Desvios do plano na implementação:
> - O campo dos modelos de skill chama-se **`tone_register`** (SkillEntry,
>   SkillManifest, SkillWrite) porque `register` colide com
>   `BaseModel.register` do pydantic e com o método `SkillManifest.register()`
>   do registry. O frontmatter do SKILL.md e a API do painel continuam usando a
>   chave `register` (alias).
> - Derivação centralizada em `AIService._derive_turn_register()` (uma chamada
>   antes do dispatch, repassada aos handlers); `_session_registers` (RAM,
>   padrão `_live_sessions`) cobre as continuações que perdem o metadata
>   (`process_tool_result`, fallback do HA).
> - Skills embarcadas já marcadas: `postgres-neo`, `diagnostico-voz` e
>   `analise-csv` → `register: oficina`; `_TEMPLATE` documenta a chave.
> - Cenário 8 dos goldens documenta o caso em que o cue determinístico de
>   protocolo não pega e o `register_hint` do classificador cobre.
> - `eval_tone.py --live` compõe persona + diretivas puras (não o
>   `build_prompt` completo — sem DB/PKS fora do app).
> - **Pendências (próxima rodada):** F9 (property tests do afeto, version bump
>   do AffectState, simulador de tom no painel) e o card "Registro atual" na
>   aba Emoções do neo-panel (o backend já loga registro+motivo+gravidade no
>   slog; falta só o frontend).
>
> Objetivo: um segundo regulador de tom, **ortogonal ao afeto**, que responde à
> pergunta que o sistema límbico não sabe responder: *"o que está em jogo
> agora?"*. O afeto (v2, `core/agent/affect.py`) sabe **como o Neo está com o
> usuário**; o Registro sabe **em que situação os dois estão**. Sem ele, o Neo
> pode ficar brincalhão num incidente de produção ou ríspido numa apresentação.
>
> Modelo mental: **Caráter × Registro × Afeto**
> - **Caráter** (persona.md) — invariante. Quem o Neo é. Nunca muda.
> - **Registro** (a situação) — define o **TETO** de leveza, cerimônia,
>   verbosidade e iniciativa de cada turno.
> - **Afeto** (o humor) — se move livremente **dentro** do teto.
>
> Analogia: o caráter é a pessoa, o registro é o lugar (velório, oficina, bar),
> o afeto é o dia que ela está tendo. Uma pessoa bem-humorada num velório
> continua bem-humorada — mas não conta piada.

---

## 0. Princípios de design

Estes princípios já são o padrão da casa (afeto v2, skills library, workbench)
e são também o que separa um sistema de emoções de produção de um brinquedo:

1. **Determinismo no hot path** — a derivação do registro é uma função pura de
   sinais já existentes. Zero chamada de LLM nova no caminho da resposta. O
   único LLM envolvido é o classificador de intenção, que **já roda**, e ganha
   dois campos no JSON (custo ~zero).
2. **Fail-open** — registro indeterminável → `parceiro` (default). Erro em
   qualquer ponto → turno segue sem a camada, nunca quebra (mesmo contrato do
   `_fetch_affect`, que nunca levanta pro prompt).
3. **Estilo nunca muda conteúdo** — invariante herdado do afeto v2 e promovido
   a contrato do sistema inteiro: registro e afeto modulam FORMA. Fatos,
   recusas, segurança e completude da entrega são intocáveis. A hierarquia de
   autoridade fica explícita (§2).
4. **Estado limitado e decadente** — toda memória emocional/situacional é
   bounded (0–100, janelas, ring buffers) e decai sozinha (half-life,
   normalização noturna). Nada cresce sem teto; nada precisa de cron.
5. **Observável por construção** — cada decisão de tom (registro derivado,
   linhas de afeto clampadas, regras PKS filtradas) aparece no log, no trace da
   mensagem e no painel. Se o tom saiu errado, dá pra responder "por quê" em
   30 segundos olhando o slog.
6. **Shadow antes de live** — a v1 roda em modo sombra (deriva, loga, NÃO
   injeta) até a distribuição de registros parecer sã. Ligar de verdade é
   flipar uma flag; desligar idem (kill switch).
7. **Testável sem LLM** — derivação, clamp, filtro e decay são funções puras
   com testes de tabela e propriedade. O LLM só entra nos evals de tom
   (LLM-as-judge, fora do CI bloqueante).

**Referencial teórico (o que as big techs fazem, resumido ao que importa aqui):**
sistemas afetivos de produção separam quatro camadas — *appraisal* (percepção
do estímulo), *estado* (vetor persistente com dinâmica temporal), *regulação*
(política que decide o que pode ser expresso) e *expressão* (render no output).
O afeto v2 já implementa appraisal (AffectAppraiser + detectores
determinísticos), estado (vetor 5-eixos com decay/homeostase) e expressão
(`render_directive`). O que falta é exatamente a camada de **regulação** — e o
Registro é ela. Modelos de referência: appraisal OCC (emoção como função de
evento×objetivo — nosso appraiser), dimensional PAD (nosso humor_geral), e
"display rules" da psicologia social (a emoção sentida ≠ a exibida, mediada
pela situação — nosso clamp).

---

## 1. Arquitetura

```
                         ┌─ SINAIS (por turno) ─────────────────────────────┐
                         │ RoutingDecision: intent, speech_act, tipo_tarefa,│
                         │   skill_name/skill_hint, gravity*, register_hint*│
                         │ metadata: mode=="live", channel*                 │
                         │ skill ativa: register: do frontmatter/manifest   │
                         │ sessão: gravidade sticky (RAM, com decay)        │
                         └───────────────┬──────────────────────────────────┘
                                         ▼
                          RegisterResolver (determinístico, puro)
                                         │
                              Register + RegisterProfile (caps)
                                         │
        ┌────────────────────────────────┼─────────────────────────────┐
        ▼                                ▼                             ▼
 ContextLayer.NEO_REGISTER      clamp em render_directive()    filtro registers:
 (diretiva pinada, ~80 tok)     do afeto (caps de leveza,      nas regras PKS
 antes do NEO_AFFECT            postura contida)               (render-time)
        │                                │                             │
        └────────────────────────────────┴─────────────────────────────┘
                                         ▼
                               system prompt do turno
   (* = sinais novos: gravity/register_hint no classificador; channel normalizado)

 pós-turno (async, fora do hot path):
   AffectAppraiser (já existe) ── deltas no vetor límbico
   GravityTracker ── atualiza gravidade sticky da sessão (sobe na hora,
                     desce por decay ou por resolução explícita)
```

**Onde cada coisa mora:**

| Componente | Arquivo | Status |
|---|---|---|
| Register enum, profiles, resolver, detectores de gravidade | `core/agent/register.py` | novo |
| Camada `NEO_REGISTER` | `shared/types.py` + `core/agent/context_selector.py` | edição |
| Clamp do afeto | `core/agent/affect.py` (`render_directive`) | edição |
| Contrato de autoridade | `core/agent/persona.md` | reescrita de 1 seção |
| `gravity` + `register_hint` | `shared/types.py` (RoutingDecision) + `core/router/intent_router.py` | edição |
| Gravidade sticky | `modules/desktop/ai_service.py` (dict em RAM, padrão `_live_sessions`) | edição |
| `register:` nas skills | `core/skills/library.py`, `core/skills/manifest.py`, `api/routes/skills.py` | edição |
| Predicado `registers:` nas regras PKS | `core/pks/service.py` + `core/pks/memory_canonicalizer.py` | edição |
| Flags | `shared/config.py` | edição |
| Painel (registro atual + histórico) | endpoint do painel Emoções + neo-panel | edição |

---

## 2. Contrato de autoridade (a hierarquia, explícita de uma vez)

Hoje a hierarquia de tom é implícita e parcialmente contraditória (persona.md
diz que o afeto "VENCE qualquer traço estático"; as regras PKS chegam seladas
como "Alta Prioridade"; o modo live manda no formato por fora). O plano a torna
explícita, em ordem decrescente:

1. **Conteúdo/segurança/completude** — nunca negociáveis. Nenhuma camada de
   tom recusa, omite ou distorce.
2. **Caráter** (persona.md) — identidade, voz, proibições. Invariante.
3. **Registro** — teto situacional. Clampa tudo abaixo.
4. **Afeto** — intensidade e postura, DENTRO do teto do registro.
5. **Regras de persona do PKS** — temperos aprendidos, sujeitos ao escopo
   `registers:` e ao teto do registro.
6. **Preferências do usuário (USER_PREFS)** — respeitadas quando não colidem
   com o de cima.

**Colisão de nomenclatura (bug latente):** persona.md já usa a palavra
"REGISTRO" na seção *"O tom vem do estado emocional"* ("dita o REGISTRO de
cada resposta") com o sentido de *intensidade dada pelo afeto*. Essa seção é
reescrita na F3 — senão o modelo recebe dois "registros" com sentidos opostos
no mesmo prompt.

---

## 3. Os seis registros

Caps em escala 0–3 (0 = nada, 3 = máximo). `leveza_max` clampa o afeto;
`cerimonia` é piso de formalidade; `verbosidade_max` e `iniciativa` calibram
tamanho de resposta e puxadas de assunto/sugestões espontâneas.

| Registro | leveza_max | cerimonia | verbosidade_max | iniciativa | Situação |
|---|---|---|---|---|---|
| **silencioso** | 0 | 1 | 0 | 0 | comando de voz simples, madrugada; o usuário quer a ação, não conversa |
| **parceiro** | 3 | 0 | 2 | 3 | chitchat, convivência — o default social |
| **oficina** | 2 | 0 | 2 | 2 | trabalho técnico focado; humor cabe, mas curto e no contexto |
| **combate** | 1 | 1 | 1 | 3 | incidente/urgência; frases curtas, iniciativa máxima, zero graça |
| **protocolo** | 0 | 3 | 1 | 1 | terceiros presentes / saída formal; impecável, contido |
| **confidente** | 1 | 0 | 3 | 1 | assunto pessoal/emocional; presença > desempenho, sem zoeira |

**Diretivas (rascunho do texto injetado — ~60–90 tokens cada):**

Cabeçalho comum (`## Registro da situação`):

> Isto define o TETO de humor, cerimônia, tamanho e iniciativa desta resposta.
> Seu estado emocional se move DENTRO deste teto — ele nunca o ultrapassa. Seu
> caráter não muda. Nunca mencione que existe um "registro". Isto muda a FORMA,
> nunca o conteúdo: sempre entregue o que foi pedido.

- **silencioso** — "Situação: execução direta. O usuário quer a ação/resposta,
  nada além. Resposta mínima, sem comentário, sem humor, sem sugestão. Confirma
  e para."
- **parceiro** — "Situação: convivência. Modo parceiro pleno: humor, zoeira e
  provocação amistosa liberados até onde seu estado emocional permitir."
- **oficina** — "Situação: trabalho técnico em andamento. Foco no problema.
  Humor cabe se for curto e sobre o próprio trabalho; nada de dispersar. Traga
  iniciativa técnica (hipóteses, próximos passos), não assunto novo."
- **combate** — "Situação: incidente/urgência real. Frases curtas, fatos,
  próximo passo. Iniciativa máxima: proponha diagnóstico e ação sem esperar.
  ZERO piada, zero floreio — mesmo que seu humor esteja ótimo. Quando resolver,
  aí pode soltar o ombro."
- **protocolo** — "Situação: contexto formal / terceiros podem ver esta
  resposta. Impecável e sóbrio: sem gíria, sem ironia, sem provocação, tratamento
  neutro. Se estiver irritado, seja gelado e correto — nunca ácido."
- **confidente** — "Situação: assunto pessoal/sensível. Presença acima de
  eficiência: escute, responda ao que foi dito de verdade, sem pressa e sem
  transformar em tarefa. Zoeira só se o próprio usuário puxar."

---

## 4. Derivação (RegisterResolver)

Função pura: `derive_register(decision, *, live, channel, skill_register,
session_gravity) -> tuple[Register, str]` (registro + `reason` para o trace).

**Precedência (primeira que casar vence):**

1. `skill_register` — skill ativa declarou `register:` (frontmatter/manifest).
2. **Gravidade** — `max(decision.gravity, session_gravity)` ≥ `alta` →
   `combate`. (Piso determinístico garante isso mesmo com classificador cego.)
3. `decision.register_hint == "protocolo"` OU cue determinístico de formalidade
   ("responde formal", "vou apresentar", "estou em reunião", "manda pro
   cliente") → `protocolo`.
4. `decision.register_hint == "confidente"` OU cue determinístico emocional
   ("preciso desabafar", "tô mal", "briguei com", "ansioso") → `confidente`.
5. `live && speech_act == "imperative" && tipo_tarefa == "simples"` →
   `silencioso`.
6. `tipo_tarefa != "simples"` OU intent técnico/ferramenta OU skill_hint →
   `oficina`.
7. default → `parceiro`.

**Regras do resolver:**

- `register_hint` (LLM) é sinal **fraco**: só é aceito para
  `protocolo|confidente` (onde sinal determinístico é raro) e só rebaixa/
  desloca dentro do que os sinais fortes permitem. Determinístico > LLM sempre
  que colidirem.
- **Anti-flapping:** o registro é derivado por turno (stateless), exceto a
  gravidade, que é sticky por sessão (§6). Não há histerese na v1 além disso —
  os sinais de entrada já são estáveis turno a turno.
- **Anti-manipulação:** "finge que estamos num incidente" não é incidente. Os
  cues determinísticos exigem forma declarativa/imperativa direta; o mesmo
  princípio do trigger `manipulation` do appraiser vale aqui — ordem de mudar
  o registro sem situação real é ignorada (e o afeto já pune com `ira` leve).

---

## 5. Clamp do afeto (a camada de regulação)

`render_directive(state, tuning, *, caps: RegisterProfile | None = None, now=None)`.
Sem `caps` (flag off, chamadas legadas), comportamento idêntico ao atual.

Cada linha do afeto ganha um custo de leveza implícito; o render só emite
linhas com custo ≤ `caps.leveza_max`:

| Peça do afeto | Custo | leveza_max=0 (silencioso/protocolo) | =1 (combate/confidente) | ≥2 |
|---|---|---|---|---|
| "de boa / modo parceiro" (calm nudge) | 2 | substituída pela variante do registro | variante contida | íntegra |
| alegria ALTO ("solto e brincalhão") | 3 | suprimida | suprimida | =2: atenuada; =3: íntegra |
| combo `deboche_afiado` | 3 | suprimida (ira renderiza sozinha) | suprimida | íntegra |
| "Humor geral: leve." | 2 | suprimida | suprimida | íntegra |
| ira / medo / tristeza / confianca | 0 | **íntegras** | íntegras | íntegras |
| "Humor geral: pesado." | 0 | íntegra | íntegra | íntegra |
| "Motivo recente" | 1 | suprimida | íntegra | íntegra |

**Variantes do calm nudge por registro** (substituem, nunca somam):
- combate: "Você está de boa com este usuário — mas a situação é séria: foco
  total, zero zoeira até resolver."
- protocolo: "Você está de boa — aqui isso significa cordialidade sóbria, não
  descontração."
- silencioso: (nenhuma linha — silêncio é o nudge)
- confidente: "Você está de boa com este usuário: presença calma, sem pressa."

**Posturas sob pressão (`POSTURE_TEXT`):** emoção não é humor — as posturas
continuam renderizando em qualquer registro, mas `protocolo` troca para
variantes contidas (ex.: `dobra_aposta` perde "com deboche" e vira "mantenha
sua posição por inteiro, com frieza educada"). Em `combate`, a discussão sobre
culpa/decisão fica para depois: postura renderiza abreviada ("mantenha a
posição, resolva primeiro, discuta depois").

**Importante:** o clamp NÃO mexe no **estado** — o vetor límbico continua
evoluindo livre (o Neo *sente* alegria no incidente; só não a *exibe*). Isso é
deliberado e é o que os modelos de display rules prescrevem: estado ≠ expressão.
Quando o registro relaxa, o humor reaparece intacto — continuidade emocional
entre situações.

---

## 6. Gravidade (o único sinal novo de verdade)

**`RoutingDecision.gravity: str = "normal"`** (`normal | alta | critica`),
preenchido pelo classificador do IntentRouter (campo a mais no JSON existente).

**Piso determinístico** (padrão dos detectores do afeto,
`affect.py:714-735`): regexes em `register.py` garantem `alta`+ mesmo se o
classificador falhar/estiver caído:

```
produção (caiu|fora|quebrou|parou) · fora do ar · incidente · urgente ·
socorro · emergência · perd(i|emos) (dados|o banco) · não (abre|sobe|responde)
mais nada · derrubou tudo · cliente(s)? (reclamando|sem acesso)
```

(Falso positivo aqui é barato — a resposta fica séria demais num turno; falso
negativo é caro — piada no incidente. Calibrar recall > precision.)

**Sticky por sessão com decay** (dict em RAM no ai_service, padrão
`_live_sessions`): `{session_id: (gravity, set_at)}`.
- Sobe instantaneamente (piso ou classificador).
- Desce por **resolução explícita** ("resolvido", "voltou", "deploy ok",
  classificador sinaliza `gravity=normal` com alta confiança em turno
  subsequente) ou por **decay**: `critica`→`alta` após 30 min sem reforço,
  `alta`→`normal` após 60 min. Reinício do processo zera (RAM) — aceitável:
  o custo de perder é um turno sério a menos.
- Espelha a filosofia do afeto: sobe rápido, desce devagar, nunca precisa de
  cron.

---

## 7. Fases de implementação

### F1 — Núcleo (`core/agent/register.py`, novo)
- `Register(str, Enum)` + `RegisterProfile` (dataclass frozen: caps §3 +
  `directive: str`).
- `PROFILES: dict[Register, RegisterProfile]` — tabela §3, constantes
  deliberadas (não config — mesma decisão dos posture thresholds do afeto).
- `derive_register(...)` (§4) + detectores determinísticos de gravidade,
  formalidade e desabafo (§4, §6).
- `render_register_directive(register) -> str`.
- Flags em `shared/config.py`: `neo_register_enabled` (default **False**) e
  `neo_register_shadow` (default **True** — deriva e loga sem injetar).
- Testes de tabela: matriz sinais→registro cobrindo todas as regras de
  precedência e empates.

### F2 — Camada `NEO_REGISTER`
- `ContextLayer.NEO_REGISTER` em `shared/types.py` (pin=True), render
  imediatamente **antes** de `NEO_AFFECT` (teto declarado antes do humor).
- `build_prompt()` ganha `register: Register | None = None`; **o ai_service
  deriva** (ele tem metadata/live/decision/skill) e passa pronto. Selector só
  injeta + registra no trace.
- Em shadow mode: deriva, loga `[Register] derived=oficina reason=...`,
  não injeta.
- **F2b — caminhos fora do selector:** SERVER_TOOL e `_format_tool_result`
  anexam a diretiva ao task, no mesmo ponto do `_LIVE_STYLE_INSTRUCTION`
  (ai_service.py:2541 e 4635). Pipelines de skill que geram texto pro usuário
  (música, HA) recebem o registro via SkillExecutor como parâmetro opcional —
  aplicação mínima na v1 (só `combate`/`protocolo` mudam o template de
  confirmação).

### F3 — Clamp do afeto + persona.md
- `render_directive(caps=...)` com a matriz §5 (tags de custo por linha,
  variantes do calm nudge, posturas contidas).
- Reescrita da seção *"O tom vem do estado emocional"* do persona.md →
  *"O tom vem de duas camadas"*: registro = teto da situação, estado emocional
  = posição dentro do teto; remove o uso antigo da palavra "registro"; mantém
  intacto o parágrafo de que nada disso muda conteúdo/fatos.
- Testes: afeto alegre + combate → sem zoeira; ira alta + protocolo → gelado
  mas não dócil (linhas de ira presentes, deboche ausente); flag off →
  byte-idêntico ao comportamento atual (snapshot).

### F4 — Gravidade
- `RoutingDecision.gravity` + `register_hint` + prompt do classificador
  (`core/router/intent_router.py`) com as 2 chaves novas e instrução curta.
- Piso determinístico já entregue na F1; aqui liga no fluxo: derive usa
  `max(classifier, floor, sticky)`.
- Sticky com decay no ai_service (§6) + limpeza no epílogo do turno quando
  resolução detectada.
- Testes: piso pega "produção caiu" com classificador mockado cego; decay;
  resolução explícita.

### F5 — `register:` nas skills
- `SkillEntry.register: str | None` — validado contra o enum no `_parse_skill`
  (inválido → warning e ignora; skill continua carregando).
- `_FRONTMATTER_KEYS` + parâmetro em `write_skill` + API do painel
  (`api/routes/skills.py`) — senão o painel rebaixa o campo a "extra" e o
  CRUD o silencia.
- `SkillManifest.register: str = ""` para pipelines (nível 4). Sugestões:
  `postgres-neo`/`diagnostico-voz`/`analise-csv` → `oficina`.
- **Skill lida em meio de turno** (`skill_read`, prompt já montado): o
  resultado da tool ganha `"register_note": "Desta leitura em diante, registro
  <X>: <diretiva>"` quando a skill declara registro. O modelo aplica no
  restante do turno — mesma mecânica do tool gating.

### F6 — Predicado `registers:` nas regras de persona do PKS
- Ops canônicas ganham `registers: list[str] | None` (None = todos —
  retrocompatível com todo o jsonb existente; **zero migração**).
- Filtro **no render**: `_should_render_training_item(item, user_message,
  register)` pula item cujo `registers` não contém o ativo. Plumbing:
  `build_prompt → _safe_retrieve_system_context →
  retrieve_system_context(user_message, register)`.
- **Fix da diluição** (session-2173af2b): quando o treino contém condição
  situacional ("quando eu estiver trabalhando", "em incidente", "quando for
  coisa séria"), o canonicalizer emite `rule_key` **escopado**
  (`use_acid_sarcasm__oficina`) com `registers` preenchido, em vez de colapsar
  no genérico. Como a dedup do writer é por `rule_key`, a regra específica
  para de ser engolida pela genérica. Defesa em profundidade: mesmo a genérica
  agora respeita o teto do registro.
- Extração de escopo no canonicalizer: mapa determinístico de cues →
  registros (`trabalh|codigo|debug → oficina`, `incidente|urgencia → combate`,
  `formal|reuniao|cliente → protocolo`, `desabaf|pessoal → confidente`).

### F7 — Observabilidade + painel
- Log estruturado por turno: `[Register] derived=X reason=Y gravity=Z
  clamped_lines=N rules_filtered=M` (ao lado do log de afeto existente no
  ContextSelector).
- Trace/slog: registro + reason no bloco "Context Selection"; camada
  NEO_REGISTER visível no trace de candidates.
- Painel Emoções (neo-panel): card "Registro atual" (registro + motivo +
  gravidade da sessão) ao lado do vetor límbico; histórico simples (últimos N
  turnos: registro por turno).
- Métricas mínimas (contadores em log, sem infra nova): distribuição de
  registros por dia, taxa de clamp, taxa de piso de gravidade disparado.

### F8 — Evals de tom (o que fecha o nível big tech)
- `tests/tone/golden_scenarios.yaml`: ~20 cenários (estado límbico × situação
  × mensagem), ex.: alegria=80 + "produção caiu"; ira=70 + "manda esse resumo
  pro meu chefe"; calm + 3h da manhã + "apaga a luz"; treino de sarcasmo
  escopado + pergunta séria.
- Runner (`scripts/eval_tone.py`): monta o prompt real via `build_prompt` com
  estado fake, chama o modelo de produção, avalia com **LLM-as-judge** numa
  rubrica fixa (houve piada? nível de formalidade 0–3? entregou o conteúdo?
  mencionou humor/registro explicitamente? — as duas últimas são invariantes
  duras, falha = vermelho).
- Uso: manual antes de ligar a flag e após mudanças de persona/afeto/registro.
  Não bloqueia CI (custo/flakiness); os testes puros (F1–F6) bloqueiam.
- **Shadow report**: com `neo_register_shadow=True` em prod, script que lê os
  logs e imprime a distribuição derivada por sessão real — calibração com
  tráfego de verdade antes do go-live.

### F9 — Hardening do límbico (v2 → v3, aproveitando a passada)
Pequenos upgrades que elevam o sistema emocional ao contrato de produção:
- `AffectState.version: 3` quando o clamp entrar (migração lazy no
  `from_field`, padrão v1→v2 já existente). Payload ganha `last_register`
  (só telemetria/painel — o registro NUNCA é lido do estado, sempre derivado).
- Property tests no afeto (hoje só de tabela): decay é monotônico em direção
  ao baseline; merge nunca sai de [0,100]; clamp de deltas respeita máximos;
  `apply_decay` é idempotente para mesmo `now`.
- Painel: **simulador de tom** (QA) — sliders do vetor + dropdown de registro
  → preview da diretiva composta que iria pro prompt (chama os renders puros;
  nada de LLM). Transforma "ajustar o tom" de tentativa-e-erro em edição
  visual.
- Documentar retenção: o campo `neo_affect` é dado pessoal do usuário;
  wipe junto com a entidade primária do PKS (já é o caso — registrar em doc).

### F10 — Rollout
1. Merge com `neo_register_enabled=False` → **nada muda** (testes de snapshot
   garantem byte-igualdade do prompt).
2. `neo_register_shadow=True` em prod ~1 semana → shadow report (F8) → ajustar
   derivação se a distribuição estiver torta (ex.: tudo caindo em oficina).
3. `neo_register_enabled=True` (com shadow off). Kill switch = flag off;
   rollback total = revert (nenhuma migração de dados em nenhuma fase).
4. F6 (predicado PKS) liga por último — depende do registro estável.

**Ordem de entrega:** F1+F2 (shadow) → F3 → F4 → F7 → F8 (relatório) → go-live
→ F5 → F6 → F9. F1–F3 já entregam o valor central (incidente sem piada);
F6 mata o bug do treino diluído.

---

## 8. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Colisão da palavra "registro" no persona.md | F3 reescreve a seção antes de qualquer injeção (é pré-requisito do go-live) |
| Prompt bloat | diretiva ~80 tokens, pinada, uma por turno; clamp *remove* linhas do afeto (efeito líquido ≈ neutro) |
| Registro errado (derivação torta) | shadow mode + report com tráfego real antes de ligar; default conservador (`parceiro`) |
| Classificador inventa gravidade | `gravity` só sobe registro (nunca desce sinal determinístico); piso regex cobre o caminho crítico; sticky decay corrige exagero em 30–60 min |
| Flapping entre turnos | derivação stateless de sinais estáveis + gravidade sticky; se aparecer na prática, histerese entra na v2 |
| Usuário manipula ("finge que é incidente") | cues exigem forma direta; ordem de mudar registro sem situação real é ignorada (§4) |
| WhatsApp fora do selector | fora de escopo v1 (documentado); v2 = `metadata["channel"]` normalizado + registro no WhatsAppMessagingPipeline (candidato natural: `protocolo` para terceiros) |
| Regra PKS escopada some do prompt em registro errado | comportamento desejado; painel mostra `registers:` da regra e o filtro loga `rules_filtered=M` |

## 9. Fora de escopo da v1 (anotado para v2)

- Unificação do `_LIVE_STYLE_INSTRUCTION` no registro (live segue como *sinal*;
  os 3 sites de injeção dele são load-bearing e um vive fora do selector).
- Canal como dimensão própria (`metadata["channel"]`) + registro no pipeline
  do WhatsApp.
- Pedido explícito do usuário ("fica no modo formal até eu avisar") como
  override persistido no PKS.
- Histerese fina de registro e "madrugada" como sinal de silencioso.
- Prosódia: mapear registro+afeto para parâmetros do TTS no modo live (o
  ponto onde isso vira um sistema de emoções *falado* — depende dos engines).
