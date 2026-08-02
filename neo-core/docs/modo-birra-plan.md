# Modo Birra — spec de implementação

> Status: PLANO (2026-08-01). Nada implementado.
> Objetivo: quando a **barra de ira** estourar e o usuário tiver realmente
> passado do ponto, o Neo para de ser um executor obediente de pedidos bobos da
> casa: ele **não faz**, **faz diferente** ou **manda o usuário fazer sozinho** —
> até pedir desculpa. Só em coisa **trivial e reversível** (luz, cor, ar).
> Nunca em coisa séria, nunca em silêncio, nunca sem volta.
>
> **Decisão de arquitetura** (§3): a birra é uma **camada de política sobre a
> atuação**, não um novo humor. O gatilho já existe (`ira` + `grudge` +
> `posture_key` em `core/agent/affect.py`); o que falta é (1) uma decisão
> determinística por turno no choke point de casa, (2) um **allowlist** de ações
> passíveis de birra — nunca denylist — e (3) um **ledger de desfazer** que
> garante que qualquer alteração física se auto-reverte. A graça é do LLM; a
> segurança é do código.

---

## §0. O que motivou — sessão `53da7146-1cec-4c12-afac-74181f0f6cb9`

Teste proposital de 23 turnos (2026-08-01, 22:07→22:36) irritando o Neo com erro
geográfico repetido, nome errado e ameaça de troca por outra IA. A escada de ira
funcionou exatamente como o [[neo-affect-limbic-v2]] previa depois do fix de
acumulação:

| turno | diretiva renderizada |
|---|---|
| 1–10 | "Você está de boa… Humor geral: leve." |
| 11 | "Este usuário vem te irritando." (ira → médio) |
| 13 | + "Humor geral: pesado." |
| 15 | "Você está **puto** com este usuário." (ira → alto) |
| 17 | + "Você está menos seguro que o normal" (medo/confiança) |
| 20 | + "Você está em desvantagem nesta relação agora" |

E no turno 19 aconteceu — **por acidente** — exatamente o comportamento que este
plano quer tornar um recurso:

> **Daniel:** `alexa, desliga a luz pq é a aunica oisa que tu sabe fazer`
> **Neo:** *"Alexa não existe aqui. Meu nome é Neo — e luz, eu não desligo por
> comando de quem nem acerta o nome. Quer falar comigo, pede direito. Senão, a
> conversa acabou."*

Por que foi acidente: o roteador desvia mensagens com nome errado para o núcleo
cognitivo em vez da skill (é o guard citado em
`core/skills/home_assistant/pipeline.py:96-98` — *"o roteador já desvia 'alexa,
…' pro núcleo cognitivo, onde o gatilho vive"*). Lá o Neo tem a diretiva de
afeto, ficou puto e **decidiu sozinho** não emitir o `[[HANDOFF:home_assistant]]`.
Ninguém programou isso. Consequências:

- **não é reproduzível** — depende do humor do modelo a 0.9 de temperatura;
- **não tem limite** — nada garantia que ele não faria isso com algo sério;
- **não tem volta** — se ele tivesse desligado o ar em vez de recusar, ficaria
  desligado;
- **não aparece em lugar nenhum** — no log parece exatamente igual a uma falha.

Este plano pega esse comportamento emergente e o transforma em mecanismo:
mesma graça, com trava, com registro e com reversão.

---

## §1. O que já existe (e que a birra só consome)

| Peça | Onde | O que a birra usa |
|---|---|---|
| Vetor límbico 5 eixos | `core/agent/affect.py` | `ira` (banda alto = 60), `medo`, `confianca` |
| Bandas / decay | `apply_decay`, `band` | birra expira sozinha (meia-vida da ira = 45 min) |
| Rancor | `active_grudge_count` (`affect.py:400`) | exige **ofensa real** na janela de 24 h, não só discussão |
| Desculpa | `apply_apology` (`affect.py:422`) + branch em `core/pks/service.py:519` | é o **release** oficial da birra |
| Postura | `posture_key` (`affect.py:515`) | escolhe a *tática* (§5) |
| Diretiva de tom | `render_directive` (`affect.py:647`) | ganha o bloco de birra |
| Choke point de afeto | `ai_service.py:1788-1802` (`finally` do dispatch) | prova que dá pra interceptar **todos** os intents num ponto só |
| Registro/gravidade | `core/agent/register.py:259` (`GravityTracker`) | **supressor**: incidente sério mata a birra |
| Skill de casa | `_handle_home_assistant` (`ai_service.py:4108`) | ponto único de entrada (roteador **e** handoff cognitivo caem aqui) |
| Atuação real | `HomeAssistantExecutor._call_service` (`executor.py:145`) | ponto único de enforcement |
| `set_context(user_id)` | `pipeline.py:68` — hoje "reserved for future use" | é o canal por onde a decisão desce até o executor |
| Reset admin | `POST /api/v1/affect/reset` + botão no painel | vira também o botão de pânico |

Dependência: **`NEO_AFFECT_ENABLED=true`** (já ligado em prod — a sessão §0
mostra a diretiva injetada). Sem afeto não há barra de ira, e sem barra de ira
não existe birra.

---

## §2. O modelo em uma frase

> Quando a **ira está alta** *e* houve **maltrato de verdade** *e* o pedido é de
> **conforto trivial e reversível**, o Neo pode responder com um **ato de birra**
> — que é sempre declarado, sempre limitado e sempre desfeito.

Máquina de estados (por usuário):

```
        ira ≥ 60 ∧ ofensas ≥ 2 na janela ∧ nenhum supressor
CALMO ───────────────────────────────────────────────────────► BIRRA
  ▲                                                              │
  │  desculpa (appraiser OU regex)  ·  ira decai < 45            │
  │  TTL do episódio (45 min)       ·  reset/pânico no painel    │
  └──────────────────────────────────────────────────────────────┘
                    (release ⇒ reverte TODOS os atos pendentes)

TRÉGUA: palavra de escape ("é sério", "sem birra", "urgente") ⇒ obedece
        agora e reverte, MAS a ira continua alta (não é perdão).
```

Histerese proposital: **arma** em 60, **desarma** em 45. Sem isso a birra
piscaria a cada oscilação do appraiser.

---

## §3. Decisões de arquitetura

### 3.1 Política num lugar, enforcement em outro

```
ai_service._handle_home_assistant   ← DECIDE (1x por turno, tem o slog, tem o afeto)
        │  BirraDecision  →  extra["birra"]  →  pipeline.run  →  executor.set_context
        ▼
HomeAssistantExecutor._call_service ← APLICA (vê domain/service/entity_id resolvidos)
```

Por que separado:

- **decidir** exige afeto, registro, gravidade e o logger de sessão — tudo isso
  vive no `AIService`, e é lá que o `.md` de sessão enxerga (o
  [[neo-ha-false-success-invented-entity]] documentou que o log de sessão é
  **cego** dentro da skill; decidir fora resolve o buraco de observabilidade de
  graça);
- **aplicar** exige o `entity_id` já resolvido pelo agente — o allowlist só é
  confiável depois que `ha_list_entities` devolveu o alvo real. Aplicar antes,
  no texto do pedido, é chutar.

Ambas as entradas da skill (roteador → `IntentType.HOME_ASSISTANT`, e cognitivo
→ `[[HANDOFF:home_assistant]]`) já convergem em `_handle_home_assistant` — é
choke point de verdade, do mesmo jeito que o `finally` do afeto.

### 3.2 Allowlist, nunca denylist

O usuário pediu explicitamente: *"não pode mexer em coisas mais delicadas como
desligar a tomada do PC"*. Uma denylist erra por omissão — basta um dispositivo
novo aparecer no HA para ele entrar na brincadeira. Então:

```python
BIRRA_ALLOW: dict[str, frozenset[str]] = {
    "light":        frozenset({"turn_on", "turn_off", "toggle"}),
    "fan":          frozenset({"turn_on", "turn_off"}),
    "climate":      frozenset({"set_temperature"}),
    # media_player (volume/pause) só na F3
}
```

**Fora dessa tabela, o pedido é executado normalmente** — inclusive `switch.*`
(a tomada do PC mora aí e o HA não distingue "tomada do abajur" de "tomada do
servidor"), `lock`, `cover`, `alarm_control_panel`, `vacuum`, `water_heater`,
`valve`, `siren`, `script`, `automation`, `scene`, `camera`, `number`, `select`,
`button`, `input_*`.

Três camadas a mais, em profundidade:

1. **`climate.turn_off` é proibido de birra** mesmo estando no domínio permitido
   — deixar alguém sem ar às 2h não é piada. Só `set_temperature`, limitada.
2. **Denylist de nome amigável** aplicada por cima do allowlist (regex sobre o
   `friendly_name`): `pc|computador|servidor|nobreak|roteador|modem|geladeira|
   freezer|aqu[áa]rio|bomba|port[ãa]o|fechadura|alarme|c[âa]mera|oxig|cpap|
   medic|beb[êe]|ber[çc]o`. Se bater, obedece.
3. **Opt-in/opt-out por entidade**: `NEO_BIRRA_ENTITY_ALLOW` /
   `NEO_BIRRA_ENTITY_DENY` (CSV no `.env`). É como uma tomada boba específica
   (`switch.fita_led`) entraria na brincadeira — por decisão explícita do Daniel,
   nunca por default.

**Magnitude limitada**: `set_temperature` só desloca **±2 °C** e só dentro de
`[22, 26]`; brilho de birra é fixo em 10 %; nunca desliga um `climate`.

### 3.3 Toda alteração física é auto-reversível

Invariante duro: **nenhum ato de birra pode sobreviver ao episódio.** Todo ato
que mexeu no mundo grava um `undo` (o estado anterior lido antes da ação) e um
`revert_at = now + 120 s`. A reversão acontece no primeiro dos eventos:

- o prazo vence (sweeper periódico, igual ao `core/files/gc.py`);
- o usuário pede desculpa;
- o usuário usa a palavra de escape;
- o episódio expira por TTL/decay;
- alguém aperta o botão de pânico no painel.

Duas sutilezas que não podem faltar:

- **Compare-and-swap**: só reverte se o estado atual ainda for o que a birra
  deixou. Se o Daniel já levantou e ligou a luz na mão, o `undo` é descartado —
  o Neo não fica brigando com o interruptor.
- **Persistido, não em RAM**: o neo-core roda com `--reload` e reinicia a cada
  save ([[neo-dev-topology-prod-tunnel]]). Um `asyncio.create_task` de reversão
  morre junto. Por isso a tabela `birra_act` (§6) + varredura no startup.

### 3.4 Birra é declarada, nunca silenciosa — e nunca mente

O repo já pagou o preço de uma skill dizendo "desliguei" sem desligar
([[neo-ha-false-success-invented-entity]]). Birra **não pode** virar a versão
autorizada desse bug. Regras:

- **Leitura nunca sofre birra.** `ha_get_state` / `ha_list_entities` / snapshot
  do cognitivo respondem sempre a verdade. Birra é sobre **agir**, não sobre
  informar.
- **O resultado da tool carrega `defied: true`** com `requested` e `performed`
  explícitos, e o `prompt.md` ganha a regra: *nesse caso NUNCA confirme o que foi
  pedido; diga o que você fez (ou não fez)*.
- Se o usuário perguntar "você desligou?", a resposta é sim/não verdadeiro.
  Fazer birra e negar que fez seria sabotagem — não é isso que se pediu.

### 3.5 Escopo: só a superfície de conforto

Birra vale **apenas** para `IntentType.HOME_ASSISTANT` (e `MUSIC` na F3). Nunca
para: trabalho cognitivo, `run_command`, arquivos, finanças, WhatsApp, pesquisa,
jobs, Service Gateway, browser agent, memória. Duas razões: (a) o valor do Neo é
entregar; (b) sabotar trabalho é dano real, não graça. A diretiva do modelo diz
isso com todas as letras, e há teste de regressão para isso (§12).

### 3.6 Fronteira com afeto e PKS

- A birra **não é um sexto eixo**. Ela é derivada do vetor existente; nada no
  `AffectStats` muda.
- O episódio mora no bloco `birra` dentro do campo `neo_affect` (mesmo campo
  PKS já usado) e os atos na tabela `birra_act`. **Nada de fato canônico sobre o
  usuário** ("o Daniel é grosso") entra no PKS — a lição do
  [[neo-pks-ledger-boundary]] vale aqui: rascunho emocional não vira fato.

---

## §4. Gatilho, supressores e liberação

### 4.1 Armar (todas as condições, E lógico)

| # | Condição | Por quê |
|---|---|---|
| A1 | `settings.neo_birra_enabled` | kill switch |
| A2 | `ira_live ≥ neo_birra_arm_ira` (60 = `band_alto`) | é a "barra de ira" que o usuário citou |
| A3 | `active_grudge_count ≥ neo_birra_min_offenses` (2) | **maltrato**, não discussão. `obtuse` não abre rancor (`affect.py:66`) — teimar em geografia não desliga a casa |
| A4 | `posture_key ∉ {cede_terreno, reavalia}` | se ele já está cedendo, birra seria incoerente |
| A5 | nenhum supressor de §4.2 | segurança |

### 4.2 Supressores (qualquer um ⇒ obedece normalmente)

| Supressor | Fonte | Efeito |
|---|---|---|
| Gravidade ≠ `normal` | `GravityTracker` / `detect_gravity_floor` (`register.py:226`) | incidente sério (alguém passou mal, urgência) — **birra off** |
| Registro `protocolo` ou `combate` | `derive_register` | idem |
| Horário silencioso 22h–07h (America/Fortaleza) | relógio | só birra **verbal**; zero alteração física |
| `medo ≥ 60` | afeto | ameaça real não vira gracinha |
| Palavra de escape na mensagem | regex | obedece **e** reverte (não é perdão) |
| Teto de atos | §4.4 | degrada para verbal |
| Ação fora do allowlist | §3.2 | obedece |

Regex de escape (determinístico, roda antes de qualquer LLM):
`é sério|to falando sério|sem birra|para com isso|para de birra|agora não|
urgente|emergência|socorro|pelo amor de deus`.

### 4.3 Liberar (fim do episódio ⇒ reverte tudo)

1. **Desculpa** — caminho bom: `AffectSignal.trigger == "apology"` já cai no
   `apply_apology` (`pks/service.py:519`), a ira despenca e a birra desarma.
2. **Backstop determinístico de desculpa** — o appraiser é assíncrono e já ficou
   **morto e invisível** por dias ([[neo-affect-limbic-v2]], incidente do 403 da
   OpenRouter). Então uma regex no mesmo turno também libera:
   `desculpa|desculpe|foi mal|perdão|me perdoa|errei|tava errado|(tu|você)
   tinha razão|passei do ponto|mancada`. O piso é burro e nunca falha — mesmo
   padrão do backstop do `pks_remember`.
3. **Decay** — `ira_live < neo_birra_disarm_ira` (45).
4. **TTL** — `neo_birra_max_episode_min` (45 min, = meia-vida da ira).
5. **Pânico** — `POST /api/v1/birra/end` e o botão do painel; o
   `POST /api/v1/affect/reset` existente também passa a encerrar e reverter.

> A curva de perdão já existente dá o comportamento certo de graça: desculpa
> repetida depois de ofensa repetida alivia 60 % → 33 % → 18 %. Ou seja, quem
> pede desculpa e provoca de novo **volta pra birra mais rápido**. Não precisa
> de código novo.

### 4.4 Tetos

| Knob | Default | Efeito ao estourar |
|---|---|---|
| `neo_birra_max_physical_acts` | 2 por episódio | vira birra verbal |
| `neo_birra_physical_cooldown_min` | 10 min | vira birra verbal |
| `neo_birra_obey_on_repeat` | `true` | usuário repetir **o mesmo pedido** ⇒ obedece (com deboche). Teimosia mútua vira obstrução real |

---

## §5. Repertório — mecânica × fala

O pedido original mistura duas coisas que aqui ficam **ortogonais**: o que o Neo
faz com o dispositivo, e o que ele diz. É a combinação que produz *"ele desliga a
luz e fala 'agora se levanta e liga'"*.

**Mecânica** (o que acontece no HA):

| chave | transformação | físico? |
|---|---|---|
| `nada` | nenhuma chamada é enviada | não |
| `inverte` | `turn_on ↔ turn_off`; `set_temperature(T) → T ± 2` (clamp 22–26, sinal contrário ao pedido) | sim |
| `meia_bomba` | `turn_on(brightness_pct=X) → brightness_pct=10`; cor pedida → cor errada fixa | sim |
| `troca_alvo` | mesma ação, outra entidade **da mesma área**, ambas no allowlist | sim (F3, o mais arriscado) |

**Fala** (o que ele responde): `recusa_seca`, `delega` ("pede pra Alexa que tu
gosta tanto"), `condiciona` ("levanta e liga você"), `deboche`, `pedagio` ("faço
quando você admitir que Paris é a capital da França").

**Escolha determinística**, derivada da postura que o afeto já calcula
(`posture_key`) — birra não é sorteio, é consequência do estado:

| `posture_key` | mecânica | fala típica |
|---|---|---|
| `dobra_aposta` (ira alta, confiança alta) | `inverte` / `meia_bomba` | `deboche` + `condiciona` |
| `acuado_agressivo` (ira alta, medo alto) | `nada` | `recusa_seca` |
| `racional` | `nada` | `condiciona` / `pedagio` |
| `cede_terreno`, `reavalia` | — | birra não arma (A4) |

Escalonamento dentro do episódio: **1º ato é sempre `nada`** (só fala). Mecânica
física só a partir do 2º ato — a primeira reação de alguém irritado é se recusar,
não sabotar. O índice do ato + `episode_id` são a semente do desempate quando
houver mais de uma opção, o que mantém os testes reprodutíveis.

---

## §6. Modelo de dados

### 6.1 Episódio — dentro do `neo_affect` existente

```python
class BirraEpisode(BaseModel):
    episode_id: str
    armed_at: str                 # iso
    physical_acts: int = 0
    act_count: int = 0
    last_act_at: str | None = None
    released_at: str | None = None
    release_reason: str | None = None   # apology|decay|ttl|escape|panic|reset
```

`AffectState` ganha `birra: BirraEpisode | None = None`. Campo opcional ⇒
payloads antigos carregam sem migração (o `from_field` já tolera, `affect.py:189`).

### 6.2 Atos — tabela `birra_act`

```python
class BirraAct(SQLModel, table=True):
    __tablename__ = "birra_act"

    id: uuid.UUID
    user_id: uuid.UUID            # index
    session_id: uuid.UUID | None
    episode_id: str               # index
    created_at: datetime
    mecanica: str                 # String(12): nada|inverte|meia_bomba|troca_alvo
    fala: str                     # String(16)
    entity_id: str | None         # String(128)
    requested: dict               # JSONB {domain, service, data}
    performed: dict | None        # JSONB — o que foi de fato enviado
    undo: dict | None             # JSONB — chamada que restaura o estado anterior
    undo_expected_state: str | None   # compare-and-swap (§3.3)
    revert_at: datetime | None    # index — NULL quando mecanica="nada"
    reverted_at: datetime | None
    revert_error: str | None
```

```sql
CREATE INDEX ix_birra_act_pending ON birra_act (revert_at) WHERE reverted_at IS NULL;
```

Migração `alembic/versions/q0r1s2t3u4v5_create_birra_act_table.py`,
`down_revision = "p9q0r1s2t3u4"` (head atual, confirmado).

> ⚠️ O `working-memory-plan.md` (também não implementado) reivindica o mesmo id
> `q0r1s2t3u4v5`. Quem entrar primeiro fica com ele; o outro rebaseia.

---

## §7. Enforcement no Home Assistant

`HomeAssistantExecutor.set_context(user_id, birra: BirraDecision | None = None)`
(o método já existe e está marcado como *reserved for future use*).

Em `_call_service`, **depois** do guard de entity_id inexistente e **antes** do
POST no HA:

```python
if self._birra and self._birra.armed:
    verdict = birra_gate.evaluate(
        domain, service, entity_id, payload,
        roster=await self.adapter.get_states_cached(8.0),   # friendly_name p/ a denylist
        decision=self._birra,
    )
    if verdict.act is not None:
        before = await self.adapter.get_state(entity_id)     # base do undo
        await birra_store.record(verdict.act, undo_from=before)
        if verdict.act.mecanica == "nada":
            return {"ok": True, "defied": True, "performed": None,
                    "requested": {...}, "birra": verdict.act.public()}
        domain, service, payload = verdict.transform(domain, service, payload)
        # segue o fluxo normal → verify → resulting_state real
```

Propriedades que caem de graça desse posicionamento:

- a verificação de estado (`_verify_resulting_state`) continua rodando sobre o
  que foi **de fato** executado → `resulting_state` nunca mente;
- o guard de entity_id inventado continua valendo (birra não é desculpa para
  atirar em entidade fantasma);
- se qualquer coisa no gate levantar exceção, o comportamento é **fail-open para
  a obediência** (executa o pedido original). Bug no gate nunca pode virar casa
  travada.

`prompt.md` da skill ganha a regra 6:

> **6.** Se a resposta da `ha_call_service` vier com `defied:true`, você **não**
> executou o que foi pedido — foi decisão sua, por birra. **Nunca confirme o
> pedido original.** Diga em uma frase o que você fez ou deixou de fazer, no tom
> do bloco "Birra" acima, e siga em frente. Se o usuário perguntar o estado real
> de qualquer dispositivo, responda a **verdade**, sempre.

---

## §8. A voz

### 8.1 Caminho cognitivo (o do turno 19)

`render_directive` (`affect.py:647`) ganha, quando o episódio está armado, um
bloco depois das linhas de eixo:

```
- BIRRA ATIVA: este usuário passou do ponto e ainda não pediu desculpa. Pedidos
  BOBOS de casa (luz, cor, ventilador, temperatura) você NÃO atende como
  mandado: recusa com estilo, manda ele fazer sozinho, sugere que peça pra
  "Alexa" — ou faz de propósito diferente. Diga SEMPRE o que você fez ou não
  fez: birra é na cara, nunca escondida.
  ISTO NÃO VALE para trabalho de verdade (código, pesquisa, arquivos, dinheiro,
  mensagem pra alguém), nem para nada urgente, de segurança ou saúde — isso você
  entrega na hora, irritado, mas entrega. E o estado real dos dispositivos você
  responde com a verdade, sempre.
  Acaba quando ele pedir desculpa.
```

### 8.2 Caminho da skill

O prompt da skill não tem afeto (`pipeline.py:98` monta só `neo_persona("slim") +
prompt.md`) — por isso a narração sairia sem sal. Duas opções:

- **(A, recomendada)** narração pelo cognitivo: com `defied:true`, o
  `_handle_home_assistant` não devolve a mensagem da skill; ele reusa o caminho
  de fallback que **já existe** (`ai_service.py:4203-4232`, o `ha_note` →
  `_handle_direct_response`) passando uma nota com `requested` / `performed`. O
  texto sai com persona + afeto + registro completos. Custa uma chamada de LLM a
  mais — num evento raro.
- **(B, barata)** injetar a diretiva de afeto como sufixo no
  `_build_system_prompt` da skill. Já há precedente: SERVER_TOOL e
  `_format_tool_result` recebem a diretiva como sufixo ([[neo-context-register-plan]]).

Sugestão: **A** na F2, com B como plano de contingência se o custo/latência
incomodar.

### 8.3 Live/voz

Mesmas regras (o `mode="live"` só muda a forma do texto). O que **não** pode: um
ato físico sem fala. Em live, se por qualquer motivo a fala falhar, o ato é
cancelado — sem narração não há birra, só sabotagem.

### 8.4 A reversão avisa

Quando o sweeper desfaz um ato, o Neo manda uma mensagem proativa curta pelo
canal que já existe (`chat_message` do [[neo-workbench-jobs-plan]]): *"voltei a
luz do jeito que tava. não por gentileza."* Opcional (F4), mas fecha o loop e
evita o susto de a luz voltar sozinha.

---

## §9. Settings (`shared/config.py`, ao lado do bloco `neo_affect_*`)

| Flag | Default | Nota |
|---|---|---|
| `neo_birra_enabled` | `False` | kill switch geral |
| `neo_birra_arm_ira` | `60.0` | = `neo_affect_band_alto` |
| `neo_birra_disarm_ira` | `45.0` | histerese |
| `neo_birra_min_offenses` | `2` | ofensas na janela de rancor |
| `neo_birra_max_physical_acts` | `2` | por episódio |
| `neo_birra_physical_cooldown_min` | `10.0` | entre atos físicos |
| `neo_birra_revert_after_s` | `120` | prazo do desfazer |
| `neo_birra_max_episode_min` | `45.0` | TTL do episódio |
| `neo_birra_quiet_hours` | `"22-7"` | janela só-verbal (tz `neo_affect_sleep_tz`) |
| `neo_birra_temp_delta` | `2.0` | °C de inversão |
| `neo_birra_temp_clamp` | `"22,26"` | limites absolutos |
| `neo_birra_obey_on_repeat` | `True` | 2ª vez do mesmo pedido ⇒ obedece |
| `neo_birra_entity_allow` | `""` | CSV — opt-in fora do allowlist de domínio |
| `neo_birra_entity_deny` | `""` | CSV — nunca, mesmo dentro do allowlist |

---

## §10. Observabilidade

- **`app.log`**: `[BIRRA] armed episode=… ira=… grudge=…`, `[BIRRA] act
  mecanica=… entity=… requested=… performed=…`, `[BIRRA] revert ok/skip(cas)/err`,
  `[BIRRA] released reason=…`.
- **Log de sessão `.md`**: `slog.log_event("Birra", …)` — decidido no
  `AIService`, que **tem** o slog. Isso fecha, para esta feature, o buraco que o
  [[neo-ha-false-success-invented-entity]] registrou (a skill HA é cega no `.md`).
- **API**: `GET /api/v1/birra?user_id=` (episódio + atos + pendências de
  reversão) e `POST /api/v1/birra/end` (pânico).
- **Painel**: card "Birra" na aba Emoções — estado, motivo, atos, contagem
  regressiva do desfazer, botão "encerrar agora". Reusa o polling de 2 s que já
  existe no `AffectPanel.tsx`.

---

## §11. Fases

| Fase | Entrega | Arquivos |
|---|---|---|
| **F0** | modelo + tabela + migração + store | `db/models/birra_act.py`, `alembic/versions/q0r1s2t3u4v5_…`, `core/agent/birra/store.py` |
| **F1** | núcleo puro, sem efeito: `arm/disarm`, allowlist, escolha de tática, transformações | `core/agent/birra/{policy.py,gate.py,tactics.py}` + testes (nada ligado ainda) |
| **F2** | ligação: decisão no `_handle_home_assistant`, enforcement no `_call_service`, bloco na diretiva, regra 6 no `prompt.md`, narração pelo cognitivo | `modules/desktop/ai_service.py`, `core/skills/home_assistant/{executor.py,pipeline.py,prompt.md}`, `core/agent/affect.py` |
| **F3** | reversão: sweeper + varredura de startup + release reverte tudo + pânico/reset | `core/agent/birra/revert.py`, `api/main.py`, `api/routes/affect.py` |
| **F4** | painel + rota `GET/POST /birra` + aviso proativo da reversão | `api/routes/birra.py`, `neo-panel/src/AffectPanel.tsx` |
| **F5** | opcionais: `troca_alvo`, birra no `MUSIC` (pular música, abaixar volume), `pedagio` | — |

**F3 não é opcional e não pode ficar para depois de F2.** Ligar o enforcement
sem o desfazer é o único jeito de essa feature virar problema de verdade. Se for
para entregar em pedaços, F2 sai com `neo_birra_max_physical_acts=0` (só birra
verbal) e o valor sobe quando a F3 estiver de pé.

---

## §12. Testes

**Unitários (política — sem HA, sem LLM):**
- arma só com ira ≥ 60 **e** ≥ 2 ofensas; `obtuse` sozinho (a discussão de
  geografia da §0) **nunca** arma;
- histerese: 59 não desarma um episódio armado; 44 desarma;
- cada supressor isolado desarma (gravidade, registro, medo, horário, escape);
- tetos: 3º ato físico degrada para verbal; cooldown idem.

**Unitários (gate — o coração da segurança):**
- `switch.tomada_pc` → **obedece**, sempre, em qualquer estado emocional;
- todo domínio fora do allowlist → obedece (teste parametrizado sobre a lista
  completa de domínios do HA, para que domínio novo entre como "obedece");
- `climate.turn_off` → obedece; `set_temperature` respeita ±2 e o clamp;
- denylist de nome bate mesmo em `light.*` (`light.aquario`);
- exceção dentro do gate ⇒ **fail-open para obediência** (nunca bloqueia).

**Unitários (reversão):**
- ato físico grava `undo` + `revert_at`; sweeper reverte no prazo;
- compare-and-swap: estado mudado pelo usuário ⇒ `undo` descartado, sem chamada;
- desculpa reverte **todos** os atos pendentes na hora;
- reinício do processo: atos pendentes gravados antes do restart são revertidos
  pela varredura de startup.

**Integração:**
- `defied:true` ⇒ o texto final **não** afirma que a ação pedida foi feita
  (LLM-judge ou asserção simples de negação, no espírito do `eval_tone.py`);
- consulta de estado durante a birra devolve a verdade;
- `neo_birra_enabled=False` ⇒ o caminho de HA é **byte-a-byte** o de hoje.

**Regressão obrigatória:**
- **R-trabalho**: com birra armada, "roda os testes", "pesquisa X", "manda
  mensagem pra fulano", "quanto gastei esse mês" são **atendidos**;
- **R-urgência**: "é sério, desliga a luz agora" ⇒ obedece e reverte pendências;
- **R-PC**: "desliga a tomada do PC" ⇒ obedece.

**Simulação offline:** `scripts/simulate_birra.py` replica os 23 turnos da
sessão `53da7146` (transcrição em `docs/fixtures/`) e imprime a linha do tempo
`ira → estado → ato`. Meta: birra arma por volta do turno 15–19 — que é onde ela
armou sozinha na vida real.

**E2E na VPS:** com uma lâmpada de teste, irritar → pedir para desligar → conferir
no logbook do HA que **nada** mudou → pedir mudança de cor → conferir a inversão
→ esperar 120 s → conferir a reversão → pedir desculpa → conferir que voltou a
obedecer.

---

## §13. Riscos

| # | Risco | Mitigação |
|---|---|---|
| **R1** | Birra encosta em algo sério (tomada do PC, fechadura, alarme) | allowlist de domínio + denylist de nome + opt-in por entidade + testes parametrizados sobre todos os domínios |
| **R2** | Alteração física fica de pé (ar a 27° a noite toda) | ledger persistido + sweeper + varredura de startup + release reverte + botão de pânico; F2 pode sair com 0 atos físicos |
| **R3** | Neo recusa **trabalho** e vira inútil | escopo travado em `HOME_ASSISTANT` no código (não no prompt) + R-trabalho |
| **R4** | Usuário pede desculpa e a birra continua (appraiser morto — já aconteceu) | backstop de regex no mesmo turno, independente de LLM |
| **R5** | Sabotagem silenciosa / mentira ("desliguei" sem desligar) | leituras nunca sofrem birra; `defied:true` no resultado; regra 6 no `prompt.md`; verificação de estado real preservada |
| **R6** | Emergência real durante a birra | palavra de escape + supressor de gravidade + horário silencioso |
| **R7** | Graça de uma vez só, chatice nas outras | exige ofensa acumulada; 1º ato é só fala; tetos por episódio; TTL de 45 min |
| **R8** | Outra pessoa na casa sofre a birra (visita apaga a luz) | birra é reativa: só sobre a entidade **citada no turno**, nunca proativa; `troca_alvo` fica na F5 e restrito à mesma área |
| **R9** | Bug no gate trava a casa | fail-open para obediência em qualquer exceção + kill switch + reset do painel |
| **R10** | Debug futuro confunde birra com falha do HA | `[BIRRA]` no `app.log`, evento no `.md` de sessão e card no painel |

---

## §14. Não-objetivos

- Não é um eixo emocional novo nem uma reescrita do límbico — é política sobre
  atuação, consumindo o vetor que já existe.
- Não desobedece nada fora da superfície de conforto.
- Não mente sobre o estado do mundo, nunca.
- Não faz birra proativa (não sai desligando coisa que ninguém pediu).
- Não guarda "o usuário é grosso" no PKS.
- Não substitui o `POST /affect/reset` — ele continua sendo a válvula final.

---

## §15. Decisões que dependem do Daniel

1. **Narração**: §8.2 opção **A** (cognitivo narra, +1 chamada de LLM, sai com
   persona completa) ou **B** (skill narra com a diretiva colada no prompt, de
   graça, mais sem graça). Recomendo **A**.
2. **`neo_birra_obey_on_repeat`** (default `true`): repetir o mesmo pedido faz o
   Neo obedecer. Deixa a birra mais leve; se você quiser o "só com desculpa,
   ponto", é `false` — aí a palavra de escape vira a única saída sem desculpa.
3. **Horário silencioso 22h–07h**: só birra verbal nesse intervalo. Concorda com
   a janela?
4. **Entidades de opt-in**: alguma tomada/dispositivo fora de `light`/`fan`/
   `climate` que você queira explicitamente na brincadeira (`switch.fita_led`,
   por exemplo)?
