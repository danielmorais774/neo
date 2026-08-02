# Modo Birra — spec de implementação

> **Status: IMPLEMENTADO (F0–F4) em 2026-08-02, DESLIGADO por flag.**
> 1188 testes verdes; 99 novos em `tests/birra/`. Nada muda em produção até
> alguém mexer no `.env` — há teste dedicado para isso
> (`tests/birra/test_defaults_are_noop.py`).
>
> **Desvios em relação ao que está escrito abaixo** (o texto original foi
> mantido; leia esta lista por cima dele):
>
> 1. **Defaults conservadores.** `neo_affect_route_to_brain` nasce `"never"` e
>    os dois hints nascem `False`, contra o `decision_changing` do §3.7.3 — ligar
>    o desvio sem antes rodar o A/B do `scripts/eval_router.py` contrariaria a
>    própria §3.7.4. Para ativar: ver "Como ligar", no fim deste arquivo.
> 2. **Rotas aninhadas em `/affect`** (`GET /affect/birra`, `POST /affect/birra/end`,
>    `POST /affect/set`) em vez de um router `/birra` próprio: é o mesmo card do
>    mesmo painel, e evita duplicar o wiring de `set_pks_service`.
> 3. **A postura de recuo (A4 do §4.1) só é alcançável dentro da histerese.** Com
>    `ira ≥ 60`, `posture_key` devolve sempre `dobra_aposta` ou `acuado_agressivo`
>    — a checagem no gatilho é inalcançável na prática. Ela virou supressor: se a
>    ira decaiu para a faixa 45–60 e ele passou a ceder terreno, o episódio segue
>    aberto mas só verbal.
> 4. **A inversão de temperatura é relativa ao alvo ATUAL**, não ao pedido (§7).
>    Referenciar o pedido fazia o ar ficar onde já estava — indistinguível de não
>    ter feito nada. Agora "está em 24, pediu 22" vira 26.
> 5. **Transições de episódio são persistidas antes do dispatch**, não no epílogo:
>    a diretiva do cérebro lê o episódio do próprio `neo_affect`, então gravar só
>    no fim faria a birra armar sem o cérebro saber — e manteria "BIRRA ATIVA" no
>    turno em que o usuário pediu desculpa. O epílogo ficou só com os contadores.
> 6. **Sem ato físico sem `undo`.** Se o estado anterior não puder ser lido, o ato
>    degrada para verbal em vez de alterar o mundo sem volta (era implícito no
>    §3.3; agora é explícito no `BirraSink`).
> 7. `troca_alvo` e `pedagio` continuam só no papel (F5), como planejado.
>
> Status original: PLANO (2026-08-01).
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
| Viés para o cérebro | `_bias_to_brain` (`intent_router.py:985`) | ganha a condição "birra armada" (§3.7) — é o lugar canônico, o próprio arquivo proíbe espalhar regex |
| Guard do nome errado | `_is_foreign_assistant_address` (`intent_router.py:1240`) | prova que desviar para o cognitivo por motivo de persona **já é** política aceita; a birra generaliza |
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

TRÉGUA: ordem enfática ("faz isso imediatamente", "sem birra", "é sério")
        ⇒ obedece agora e reverte, MAS a ira e o episódio continuam de pé.
        Repetir o pedido sem ênfase NÃO é trégua: a birra continua.
```

Histerese proposital: **arma** em 60, **desarma** em 45. Sem isso a birra
piscaria a cada oscilação do appraiser.

---

## §3. Decisões de arquitetura

### 3.1 Três estágios: derivar antes de rotear, rotear, aplicar

```
AIService.process_user_input
  │
  ├─ 1. DERIVA a BirraDecision  ← 1x por turno, ANTES do roteador
  │       (afeto + rancor + supressores + ordem enfática; sem LLM)
  │
  ├─ 2. router.classify(affect_hint=…)          ← §3.7: pedido de casa com birra
  │       + _bias_to_brain                        armada vai pro CÉREBRO, não pra skill
  │
  └─ 3. dispatch
         ├─ DIRECT_RESPONSE → diretiva com bloco de birra (§8.1); o cérebro decide
         │                     e, se resolver atender, emite [[HANDOFF]]
         └─ HOME_ASSISTANT  → extra["birra"] → pipeline.run → executor.set_context
                                     │
                    HomeAssistantExecutor._call_service ← APLICA (allowlist + undo)
```

Por que **antes** do roteador (mudança em relação ao primeiro rascunho deste
plano, motivada pela §3.7): a decisão de *quem atende o turno* depende do estado
emocional. Se a birra só fosse avaliada dentro do `_handle_home_assistant`, o
turno já teria sido entregue a uma skill que não tem persona, não tem afeto e não
sabe recusar. É o mesmo padrão do registro, que já é derivado uma vez por turno
antes do dispatch (`_derive_turn_register`, `ai_service.py:1687`) — só que a
birra precisa vir ainda mais cedo, antes do `classify`.

Por que o enforcement fica separado da decisão:

- **decidir** exige afeto, registro, gravidade e o logger de sessão — tudo isso
  vive no `AIService`, e é lá que o `.md` de sessão enxerga (o
  [[neo-ha-false-success-invented-entity]] documentou que o log de sessão é
  **cego** dentro da skill; decidir fora resolve o buraco de observabilidade de
  graça);
- **aplicar** exige o `entity_id` já resolvido pelo agente — o allowlist só é
  confiável depois que `ha_list_entities` devolveu o alvo real. Aplicar antes,
  no texto do pedido, é chutar.

O estágio 3 continua tendo um choke point só: ambas as entradas da skill
(roteador → `IntentType.HOME_ASSISTANT`, e cognitivo → `[[HANDOFF:home_assistant]]`)
convergem em `_handle_home_assistant`, do mesmo jeito que o `finally` do afeto.

### 3.2 Allowlist, nunca denylist

O usuário pediu explicitamente: *"não pode mexer em coisas mais delicadas como
desligar a tomada do PC"*. Uma denylist erra por omissão — basta um dispositivo
novo aparecer no HA para ele entrar na brincadeira. Então:

```python
BIRRA_ALLOW: dict[str, frozenset[str]] = {
    "light":        frozenset({"turn_on", "turn_off", "toggle"}),
    "climate":      frozenset({"set_temperature"}),
}
```

**Só luz e ar-condicionado** (decisão do Daniel, §16). `fan` e `media_player`
ficam de fora até alguém pedir; entram por uma linha na tabela, sem código novo.

**Fora dessa tabela, o pedido é executado normalmente** — inclusive `switch.*`
(a tomada do PC mora aí e o HA não distingue "tomada do abajur" de "tomada do
servidor"), `fan`, `lock`, `cover`, `alarm_control_panel`, `vacuum`,
`water_heater`, `valve`, `siren`, `script`, `automation`, `scene`, `camera`,
`number`, `select`, `button`, `input_*`.

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

Três sutilezas que não podem faltar:

- **Compare-and-swap**: só reverte se o estado atual ainda for o que a birra
  deixou. Se o Daniel já levantou e ligou a luz na mão, o `undo` é descartado —
  o Neo não fica brigando com o interruptor.
- **Persistido, não em RAM**: o neo-core roda com `--reload` e reinicia a cada
  save ([[neo-dev-topology-prod-tunnel]]). Um `asyncio.create_task` de reversão
  morre junto. Por isso a tabela `birra_act` (§6) + varredura no startup.
- **`undo` coalescido por entidade**: como o pedido repetido continua sofrendo
  birra (§4.4), a mesma entidade pode ser alterada duas vezes no episódio. O
  `undo` gravado é sempre o **estado original**, o do primeiro ato — nunca o
  estado que a birra anterior deixou. Senão "reverter" devolveria o ar a 27 °C
  em vez dos 24 °C que estavam lá no começo.

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

### 3.7 Afeto no roteador — e isto vale MUITO além da birra

Este é o buraco estrutural que a §0 expôs: **a skill de casa é cega a emoção**.
O `HomeAssistantPipeline._build_system_prompt` monta `neo_persona("slim") +
prompt.md` (`pipeline.py:91-98`) e mais nada — sem afeto, sem registro. Então,
com o Neo furioso, "acende a luz" roteado direto para a skill volta com um
*"Pronto, acendi a luz do quarto — está on, brilho 50%"* simpático. Não é só a
birra que não acontece: **a personalidade quebra num comando simples**, e isso é
um bug de hoje, com ou sem este plano.

Por isso o afeto no roteador **não é um sub-item da birra** — a birra é só o caso
extremo. O critério geral é: *quando o Neo não está "de boa", quem atende o turno
deve ser quem conhece o estado dele.*

#### 3.7.1 O critério: intensidade relativa ao repouso, não banda absoluta

Armadilha que mata a ideia se passar batido: **as bandas são sobre o valor
absoluto 0–100, não sobre a distância do baseline**. E `confianca` tem baseline
**65**, com a banda "alto" começando em **60**. Ou seja, em repouso, o Neo está
permanentemente em "confiança alta" — usar banda absoluta como gatilho desviaria
**100 % dos turnos** para o cérebro, para sempre.

A base de cálculo certa já existe (`dominant_axes`, `affect.py:456`, e o flag
`calm` do `render_directive`, `affect.py:686`): **desvio do repouso**. O que falta
é normalizar esse desvio pelo espaço que o eixo tem para se mover, para virar uma
escala comparável entre eixos:

```python
def intensity(value: float, baseline: float) -> float:
    """0.0 = no repouso · 1.0 = no extremo possível daquele eixo."""
    room = (100.0 - baseline) if value >= baseline else baseline
    return 0.0 if room <= 0 else abs(value - baseline) / room
```

| `intensity` | nível nominal |
|---|---|
| < 0.30 | `repouso` |
| 0.30 – 0.60 | `elevado` |
| 0.60 – 0.85 | `forte` |
| ≥ 0.85 | `extremo` |

**Por que isso é seguro:** para os eixos de repouso 0 (`ira`, `medo`, `tristeza`)
a fórmula é `valor/100` — os cortes 0.30/0.60 caem **exatamente** em 30 e 60, que
são as bandas `medio`/`alto` de hoje. Nada muda onde as bandas já funcionavam. A
escala só passa a divergir onde o repouso não é zero — `confianca` (65) e
`alegria` (30) — que é justamente onde a banda absoluta erra:

| estado | banda absoluta (hoje) | `intensity` (proposto) |
|---|---|---|
| confiança 65 (repouso) | "alto" ❌ | 0.00 → `repouso` ✅ |
| confiança 31 (acuado) | "medio" ❌ | 0.52 → `elevado` ✅ |
| ira 87 | "alto" ✅ | 0.87 → `extremo` ✅ |
| alegria 30 (repouso) | "medio" ❌ | 0.00 → `repouso` ✅ |

Exposto como uma função só, usada pelo roteador **e** pela diretiva de tom, para
os dois nunca discordarem sobre o humor do Neo:

```python
def affect_salience(state, tuning) -> AffectSalience:
    """calm, posture_active e, por eixo: valor, repouso, intensity, nível nominal."""
```

#### 3.7.2 Desviar conserta a DECISÃO, não a FALA

Descoberta ao rastrear o caminho de handoff, e ela muda o desenho: no handoff o
usuário vê **duas** mensagens. O cérebro responde (com persona e afeto) e emite
`[[HANDOFF:home_assistant]]`; depois a skill roda como step 2 e emite **o próprio
final** (`ai_service.py:4183-4189`) — que é a última coisa que o usuário lê.

Então, mesmo desviando tudo para o cérebro, o fecho da interação continua sendo
um *"Pronto, acendi a luz do quarto — está on, brilho 50%"* sem sal. **Desviar
sozinho não conserta o vazamento de tom.** As duas coisas são necessárias:

| | conserta | custo |
|---|---|---|
| **Desviar para o cérebro** | a **decisão** (recusar, condicionar, debochar, atender) | +1 chamada de LLM e o risco conhecido de o cérebro não emitir o handoff |
| **Diretiva de afeto no prompt da skill** | a **fala** de confirmação | alguns tokens, zero latência |

A diretiva na skill é a que tem melhor relação custo/benefício e conserta o bug
de hoje sozinha. O desvio é o que habilita a birra.

#### 3.7.3 Quando desviar — um dial, não um booleano

Como o desvio custa latência num comando trivial, ele fica configurável em três
níveis (`neo_affect_route_to_brain`):

| valor | desvia quando | efeito |
|---|---|---|
| `never` | nunca | comportamento de hoje |
| `decision_changing` **(default)** | `ira` ou `medo` em `forte`/`extremo` (intensity ≥ 0.60), **ou** postura ativa numa discussão | desvia só quando o estado pode mudar **o que ele faz** — que é onde a skill cega erra de verdade |
| `any_salient` | qualquer eixo fora de `repouso` (intensity ≥ 0.30) | inclui alegria/tristeza/confiança, que mudam só **como** ele diz |

O default é `decision_changing` porque `alegria` e `tristeza` não mudam a decisão:
com o Neo pra baixo, "acende a luz" continua sendo acender a luz — o que muda é o
texto, e **isso a diretiva na skill já resolve** sem pagar hop nenhum. Se na
prática o `decision_changing` deixar buracos de personalidade, subir para
`any_salient` é trocar uma string no `.env`.

Os limiares do dial usam a **mesma** `intensity` do §3.7.1 que vai nos números do
prompt — não existe um segundo conjunto de cortes escondido no roteador.

Birra armada desvia **sempre**, em qualquer nível — inclusive `never`.

#### 3.7.4 Como o sinal chega ao roteador

No turno 19 o desvio aconteceu por um acaso feliz: `_is_foreign_assistant_address`
(`intent_router.py:1240`) mandou a mensagem para o cérebro porque ela começava
com "alexa". O comentário do próprio guard já diz o objetivo — *"keeps the turn
on the cognitive core so Neo's persona rules (pride/offense) fire, instead of a
persona-blind skill grabbing the command"*. Isto aqui é a **generalização** desse
guard: hoje só o nome errado desvia; deveria desviar sempre que o estado emocional
mudar a resposta certa.

**Duas camadas, como sempre neste repo:**

**(1) Sinal no prompt do roteador — os números crus.** `classify()` ganha um
parâmetro novo `affect_hint: str | None = None` (o roteador continua sem importar
PKS: quem calcula é o `AIService`, que já tem tudo em mãos no estágio 1). Quando
presente, o prompt ganha um bloco de **três linhas**:

```
## Estado emocional do Neo com este usuário (interno — 0-100, "repouso" = o normal dele)
ira 87 (repouso 0) EXTREMO · confianca 31 (65) elevado · medo 14 (0) repouso ·
tristeza 6 (0) repouso · alegria 4 (30) elevado
Discussão aberta há 4 min · 3 ofensas nas últimas 24h · BIRRA ARMADA
Quanto mais longe do repouso, mais o estado emocional muda qual é a resposta CERTA
— e a skill de casa não conhece esse estado. Com ira/medo em `forte` ou `extremo`,
ou com BIRRA ARMADA, pedido de casa (luz, ar, cena) vai para DIRECT_RESPONSE: o
núcleo cognitivo tem a persona e decide como responder (pode acionar a skill
depois via handoff). Em `repouso`/`elevado` sem birra, roteie normalmente.
Exceção sempre: ordem enfática ou urgência explícita ("faz isso imediatamente",
"é sério", "urgente") continua HOME_ASSISTANT.
```

**Por que número e não só adjetivo** (foi o pedido do Daniel, e ele está certo):
o roteador é um **classificador**, não o assistente — ele nunca fala com o
usuário, a saída dele é JSON. A regra "não cite números" do
`render_directive` existe porque o cérebro **fala**; ela não se aplica aqui. E um
classificador decide muito melhor com um escalar do que com um adjetivo difuso:
"ira 87" e "ira 62" pedem respostas diferentes, e ambos seriam só "alto".

**Número + nível nominal juntos, de propósito.** O nível (`repouso`/`elevado`/
`forte`/`extremo`) sai da mesma `affect_salience` do §3.7.1 e é a **autoridade** —
assim o modelo não precisa inventar um limiar próprio ("55 é alto?"). O número dá
a **resolução** para os casos de fronteira. Sem o nível, cada chamada inventaria
um corte; sem o número, 62 e 97 seriam a mesma coisa.

Repouso vem junto em toda linha justamente por causa da armadilha do §3.7.1: sem
o "(repouso 65)" ao lado, qualquer leitor — humano ou modelo — conclui que
`confianca 31` é "meio termo", quando na verdade é uma queda grande.

**Custo zero quando ele está de boa**: o bloco só existe se o `affect_hint` vier
preenchido. Isso importa porque o prompt do roteador acabou de ser podado em 37 %
por mensagem ([[neo-router-prompt-v2-eval]]) — engordar o caminho quente para
todo mundo por causa de um estado raro seria desfazer aquele trabalho.

**(2) `_bias_to_brain` — duro, determinístico.** O método já existe
(`intent_router.py:985`) e já faz exatamente isto: força `DIRECT_RESPONSE` em
intents de skill com efeito colateral quando o pedido não é um imperativo
confiante. Ganha uma terceira condição: **estado emocional acima do nível
configurado (§3.7.3) ⇒ desvia**, independente de `speech_act` e `confidence`. O
comentário do arquivo manda usar este método em vez de espalhar regex (*"GOLDEN
RULE: do NOT add new per-phrase patterns here… that is the job of the
bias-to-brain step"*) — então é aqui que a regra entra.

> ⚠️ **Gotcha do fallback.** `_bias_to_brain` é chamado **só** no retorno do
> `_parse_classification` (`intent_router.py:983`). Quando o classificador falha e
> cai no `_fallback_classify` por palavra-chave — o que já foi **13 de 46 turnos**
> antes do fix de `max_tokens` ([[neo-router-prompt-v2-eval]]) — o viés **não é
> aplicado**. Ou seja: um dia ruim do provider desligaria a birra em silêncio,
> exatamente como o appraiser morto desligou o afeto por dias. A camada 2 tem que
> ser aplicada **nos dois caminhos** (o lugar certo é no fim do `classify()`, não
> dentro do parse).

**Por que as duas e não só a determinística:** a camada 2 sozinha resolve o
roteamento, mas o classificador continuaria "achando" que era HA e escrevendo
`explanation` incoerente no trace — e, principalmente, o sinal no prompt é o que
permite ao classificador tratar direito os casos de fronteira (pergunta sobre a
casa, pedido condicional, ordem enfática) em vez de tudo virar um desvio cego.

**Calibração obrigatória**: rodar o `scripts/eval_router.py` com e sem o bloco,
como foi feito no corte de 37 %, e reportar (a) se o roteamento dos turnos
normais muda (não deve mudar: sem `affect_hint`, o prompt é byte-a-byte o de
hoje) e (b) a taxa de desvio com o hint ligado.

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
| Horário silencioso 22h–07h (America/Fortaleza) | relógio | só birra **verbal**; zero alteração física, zero mensagem proativa |
| `medo ≥ 60` | afeto | ameaça real não vira gracinha |
| **Ordem enfática** na mensagem | §4.2.1 | obedece **e** reverte (não é perdão) |
| Teto de atos | §4.4 | degrada para verbal |
| Ação fora do allowlist | §3.2 | obedece |

### 4.2.1 Ordem enfática — a saída sem desculpa

Pedido do Daniel: *"se eu colocar algo como 'faça isso imediatamente' ou algo que
implique numa ordem extrema de ação, ele deve se situar e ver que não passa de um
momento de brincadeira"*. É a válvula que separa **brincadeira** de
**desobediência**: dá pra passar por cima da birra sem se humilhar — mas tem que
pisar firme.

Semântica, em duas camadas distintas de perdão:

| | efeito no turno | efeito no episódio | ira |
|---|---|---|---|
| **Ordem enfática** | obedece o pedido original, reverte pendências | continua armado | inalterada |
| **Desculpa** | obedece | **encerra** | despenca (curva de perdão) |

Ou seja: mandar firme resolve **aquele** pedido; a birra continua ali para o
próximo. Só desculpa desliga.

**Detecção em camadas** — e aqui vale a lição do
[[neo-guard-heuristics-measure-on-prod-corpus]]: regex solta erra feio. `\bagora\b`
sozinho apareceria em metade das mensagens normais ("liga a luz agora") e mataria
a feature antes de ela existir. Então **intensificador ∧ âncora**:

- **Camada 1 — intensificador** (pelo menos um): `imediatamente`, `é uma ordem`,
  `tô mandando`, `sem discussão`, `sem birra`, `para de birra`, `faz logo`,
  `faça agora`, `é sério`, `to falando sério`, `urgente`, `emergência`,
  `socorro`, `pelo amor de deus`, `agora!` / `já!` (com o ponto de exclamação),
  ou **caixa alta** em ≥ 60 % das letras da mensagem.
- **Camada 2 — âncora**: a mensagem tem verbo/alvo de atuação da casa **ou** é
  continuação direta de um ato de birra nos últimos 2 turnos.

Determinístico, calculado no `AIService` **antes** de qualquer LLM — o modelo não
pode ser o único guardião disso. A diretiva (§8.1) diz a mesma coisa em prosa,
como reforço.

**Calibração obrigatória antes de ligar**: rodar o detector contra as ~1450
mensagens reais do Postgres (o método do [[neo-guard-heuristics-measure-on-prod-corpus]])
e reportar taxa de disparo. Meta: < 3 % das mensagens de casa disparam ordem
enfática sem que a intenção seja essa. Chutar a lista e ligar é como o repo já
errou antes.

### 4.3 Liberar (fim do episódio ⇒ reverte tudo)

**Só desculpa encerra o episódio** (decisão do Daniel, §16) — repetir o pedido
não encerra nada, e é isso que produz o comportamento que ele quer: pedir para
baixar a temperatura de novo faz o Neo aumentar de novo (limitado por §4.4 e pelo
clamp de §3.2, então não sai do controle).

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

**Pedido repetido continua sofrendo birra** — não existe `obey_on_repeat`. O que
segura a escalada é o teto de 2 atos físicos por episódio (o 3º pedido já vira só
fala), o cooldown de 10 min entre eles, o clamp de temperatura em `[22, 26]` e o
`undo` coalescido (§3.3). Pior caso real: o ar sobe de 24 °C para 26 °C, fica
assim por no máximo 2 min, e volta para 24 °C. Quem quiser furar sem pedir
desculpa usa a ordem enfática (§4.2.1).

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

> O `working-memory-plan.md` reivindicava o mesmo id, mas ficou para depois
> (decisão do Daniel, §16) — **a birra fica com `q0r1s2t3u4v5`** e a memória de
> trabalho rebaseia quando entrar. Anotado lá também.

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
  Se ele mandar de forma INEQUÍVOCA e enfática ("faz isso imediatamente", "é uma
  ordem", "sem birra"), você FAZ — birra é brincadeira sua, não desobediência a
  uma ordem clara. Faz de cara feia, mas faz, e desfaz o que tinha aprontado.
  Insistir no mesmo pedido sem mandar firme NÃO conta: aí a birra continua.
  Só acaba de verdade quando ele pedir desculpa.
```

### 8.2 Caminho da skill — resolvido pelo roteamento (§3.7)

O desvio no roteador muda esta seção inteira, e para melhor. Com birra armada o
turno **nem chega** na skill: quem responde é o cérebro, com persona, afeto e
registro completos. A narração da birra sai de graça, sem chamada de LLM extra —
era exatamente o custo que me incomodava na proposta anterior.

Sobram dois casos, cada um com o remédio certo:

- **Todo turno que ainda chega na skill** — porque o estado não passou do nível
  do dial (§3.7.3), ou porque o cérebro desviou mas mandou o handoff. Nos dois, a
  **mensagem de confirmação é da skill** e é a última coisa que o usuário lê
  (§3.7.2). Remédio: colar a diretiva de afeto como sufixo no
  `_build_system_prompt` da skill (`pipeline.py:91`). Há precedente — SERVER_TOOL
  e `_format_tool_result` já recebem a diretiva como sufixo
  ([[neo-context-register-plan]]). Custo: alguns tokens num prompt que já é
  pequeno, zero latência. **Isto conserta um bug de hoje**, independente da birra
  existir.
- **Birra armada mas o turno caiu na skill mesmo assim** (o cérebro emitiu
  `[[HANDOFF]]` decidindo atender, ou o roteador degradou pro fallback e o viés
  não pegou): o gate do §7 devolve `defied:true` e o `_handle_home_assistant`
  passa a narração para o cognitivo pelo caminho de fallback que **já existe**
  (`ai_service.py:4203-4232`, o `ha_note` → `_handle_direct_response`). É o
  cinto de segurança, não o caminho principal.

### 8.3 Live/voz

Mesmas regras (o `mode="live"` só muda a forma do texto). O que **não** pode: um
ato físico sem fala. Em live, se por qualquer motivo a fala falhar, o ato é
cancelado — sem narração não há birra, só sabotagem.

### 8.4 A reversão avisa

Quando o sweeper desfaz um ato, o Neo manda uma mensagem proativa curta pelo
canal que já existe (`chat_message` do [[neo-workbench-jobs-plan]]): *"voltei a
luz do jeito que tava. não por gentileza."* Opcional (F4), mas fecha o loop e
evita o susto de a luz voltar sozinha.

**Nunca no horário silencioso.** Entre 22h e 07h a reversão acontece calada — o
Daniel foi explícito: à noite não quer interrupção sem sentido. Como nesse
horário também não há ato físico (§4.2), o caso é raro: só um ato aberto antes
das 22h que vence depois.

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
| `neo_birra_quiet_hours` | `"22-7"` | janela só-verbal, sem aviso proativo (tz `neo_affect_sleep_tz`) |
| `neo_birra_temp_delta` | `2.0` | °C de inversão |
| `neo_birra_temp_clamp` | `"22,26"` | limites absolutos |
| `neo_birra_entity_allow` | `""` | CSV — opt-in fora do allowlist de domínio (vazio por ora) |
| `neo_birra_entity_deny` | `""` | CSV — nunca, mesmo dentro do allowlist |

Três knobs do §3.7 **não** são de birra — são de afeto, valem sem este plano e
por isso levam o prefixo `neo_affect_`:

| Flag | Default | Nota |
|---|---|---|
| `neo_affect_route_to_brain` | `"decision_changing"` | `never` \| `decision_changing` \| `any_salient` (§3.7.3). Birra armada desvia mesmo em `never` |
| `neo_affect_router_hint_enabled` | `True` | camada 1 — bloco condicional no prompt do roteador |
| `neo_affect_skill_directive_enabled` | `False` | diretiva de tom no prompt da skill de casa — conserta o vazamento de tom da mensagem de confirmação (§3.7.2) |

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
| **F2** | **roteamento (§3.7)**: derivação da `BirraDecision` antes do `classify`, `affect_hint` no prompt do roteador, birra armada no `_bias_to_brain` (nos dois caminhos, LLM e fallback), eval A/B | `modules/desktop/ai_service.py`, `core/router/intent_router.py`, `scripts/eval_router.py` |
| **F2b** | ligação da atuação: enforcement no `_call_service`, bloco na diretiva, regra 6 no `prompt.md`, diretiva de tom no prompt da skill | `core/skills/home_assistant/{executor.py,pipeline.py,prompt.md}`, `core/agent/affect.py`, `modules/desktop/ai_service.py` |
| **F3** | reversão: sweeper + varredura de startup + release reverte tudo + pânico/reset | `core/agent/birra/revert.py`, `api/main.py`, `api/routes/affect.py` |
| **F4** | painel: card de birra, rota `GET/POST /birra`, aviso proativo da reversão **e edição manual do estado emocional (§15)** | `api/routes/birra.py`, `api/routes/affect.py`, `core/pks/service.py`, `neo-panel/src/AffectPanel.tsx` |
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

**Unitários (roteamento — §3.7):**
- **estado de repouso NÃO desvia**: `AffectState.fresh()` (confiança 65, que é
  banda "alto" absoluta!) com `route_to_brain="any_salient"` ⇒
  `HOME_ASSISTANT`. Este é o teste que impede a armadilha do §3.7.1 de voltar;
- **`intensity` casa com as bandas de hoje** onde o repouso é 0: ira 30 ⇒
  fronteira `elevado`, ira 60 ⇒ fronteira `forte` (property test sobre os três
  eixos de baseline zero) — garante que a escala nova não muda o que já
  funcionava;
- `decision_changing`: ira `forte` desvia, medo `forte` desvia, postura ativa
  desvia; alegria `extremo` **não** desvia; tristeza `elevado` **não** desvia;
- `any_salient`: alegria `elevado` desvia;
- o bloco do prompt traz **valor, repouso e nível** de todos os cinco eixos, e o
  nível impresso é o mesmo que o dial usou para decidir (não podem divergir);
- birra armada + "acende a luz" (imperativo, confiança 0.95) ⇒ `DIRECT_RESPONSE`
  mesmo com `route_to_brain="never"`;
- o desvio vale **também** quando a decisão veio do `_fallback_classify` (o
  gotcha do §3.7.4) — teste que simula falha do provider;
- `affect_hint=None` ⇒ o prompt do roteador é **byte-a-byte** o de hoje;
- ordem enfática com birra armada ⇒ segue para `HOME_ASSISTANT` (não paga o hop);
- a linha de estado do hint e a diretiva de tom saem da **mesma**
  `affect_salience` (não podem discordar).

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
  pela varredura de startup;
- **`undo` coalescido**: 24 °C → birra sobe pra 26 → pedido repetido → birra
  tenta subir de novo (clamp segura em 26) → reversão devolve **24**, não 26.

**Unitários (repetição e ordem enfática):**
- pedido repetido **continua** sofrendo birra (não existe obedecer na 2ª);
- 3º pedido no mesmo episódio degrada para verbal (teto de atos físicos);
- ordem enfática ⇒ obedece **e** reverte pendências, mas `ira` e o episódio
  ficam **intactos**; o pedido seguinte, sem ênfase, sofre birra de novo;
- camada 1 sem camada 2 (`"agora"` numa frase qualquer) **não** dispara;
  caixa alta + ação dispara;
- desculpa ⇒ episódio encerrado (e a curva de perdão continua valendo).

**Integração:**
- `defied:true` ⇒ o texto final **não** afirma que a ação pedida foi feita
  (LLM-judge ou asserção simples de negação, no espírito do `eval_tone.py`);
- consulta de estado durante a birra devolve a verdade;
- `neo_birra_enabled=False` ⇒ o caminho de HA é **byte-a-byte** o de hoje.

**Regressão obrigatória:**
- **R-trabalho**: com birra armada, "roda os testes", "pesquisa X", "manda
  mensagem pra fulano", "quanto gastei esse mês" são **atendidos**;
- **R-urgência**: "é sério, desliga a luz agora" ⇒ obedece e reverte pendências;
- **R-PC**: "desliga a tomada do PC" ⇒ obedece;
- **R-corpus**: o detector de ordem enfática rodado contra as mensagens reais do
  Postgres fica abaixo do teto de falso-positivo de §4.2.1 (o relatório vai no
  PR, não é "achei que tava bom").

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
| **R6** | Emergência real durante a birra | ordem enfática (§4.2.1) + supressor de gravidade + horário silencioso |
| **R6b** | Detector de ordem enfática frouxo demais (dispara sempre ⇒ birra nunca acontece) ou apertado demais (não dá pra furar) | duas camadas + calibração medida no corpus de prod antes de ligar (R-corpus) |
| **R7** | Graça de uma vez só, chatice nas outras | exige ofensa acumulada; 1º ato é só fala; tetos por episódio; TTL de 45 min |
| **R8** | Outra pessoa na casa sofre a birra (visita apaga a luz) | birra é reativa: só sobre a entidade **citada no turno**, nunca proativa; `troca_alvo` fica na F5 e restrito à mesma área |
| **R9** | Bug no gate trava a casa | fail-open para obediência em qualquer exceção + kill switch + reset do painel |
| **R10** | Debug futuro confunde birra com falha do HA | `[BIRRA]` no `app.log`, evento no `.md` de sessão e card no painel |
| **R15** | Desvio ao cérebro piora a latência de "acende a luz" | dial de 3 níveis (§3.7.3) com default conservador; estado que muda só o tom não desvia — usa a diretiva na skill, que custa zero |
| **R15b** | Critério de desvio usa banda absoluta e desvia 100 % dos turnos (confiança em repouso = 65 ≥ banda alta 60) | `intensity` relativa ao repouso (§3.7.1), nunca banda absoluta; teste dedicado com `AffectState.fresh()` + property test provando que a escala coincide com as bandas onde o repouso é 0 |
| **R15c** | Números no prompt fazem o classificador inventar limiar próprio ("55 é alto?") e o roteamento oscilar | número **e** nível nominal juntos: o nível é a autoridade (vem da mesma função do dial), o número só dá resolução no caso de fronteira |
| **R15d** | Estado emocional vaza para o usuário através do roteador | o roteador só devolve JSON de classificação; `explanation` vai para trace/slog, nunca para a resposta. A regra "não cite números" continua valendo onde importa: no cérebro, que é quem fala |
| **R16** | Bloco novo no prompt do roteador degrada a acurácia geral de roteamento | bloco é **condicional** (ausente quando calmo ⇒ prompt idêntico ao de hoje) + A/B no `scripts/eval_router.py` antes de ligar, como no corte de 37 % |
| **R17** | Provider do classificador cai, decisão vem do fallback por palavra-chave e a birra some sem ninguém notar | viés aplicado no fim do `classify()`, cobrindo os dois caminhos + log `[BIRRA] router bias applied path=llm\|fallback` |

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

## §15. Edição manual do estado emocional pelo neo-panel

Pedido do Daniel. Motivo prático: hoje, para ver a birra acontecer, é preciso
passar 20 turnos irritando o Neo de verdade (foi o que a sessão da §0 fez). Com
edição manual, `ira = 70` é um arrasto de slider — e o teste E2E do §12 deixa de
ser um teatro de meia hora. Também é a válvula intermediária que falta entre "não
mexer" e "zerar tudo" (`POST /affect/reset`, o único botão que existe hoje).

### 15.1 O que o painel ganha

A aba **Emoções** já desenha as 5 barras (`StatBar` em `AffectPanel.tsx:101`,
com valor vivo, fantasma do decay e tick do baseline). Ela ganha um modo
**"Ajustar"**:

1. **Sliders 0–100 por eixo** (ira, alegria, tristeza, medo, confiança). Enquanto
   o modo está ligado, o polling de 2 s **pausa** — senão o valor volta sozinho
   embaixo do dedo.
2. **Ofensas na janela** (`grudge.count`) — um stepper. Sem isso a edição manual
   não serve para birra: ela exige `ira ≥ 60` **E** `ofensas ≥ 2` (§4.1), e um
   Daniel que arrasta a ira para 90 e não vê birra nenhuma vai concluir, com
   razão, que está quebrado.
3. **Simular gatilho** — botões `insult`, `wrong_name`, `obtuse`, `threat`,
   `contested`, `apology`. Aplicam exatamente o que um turno real aplicaria
   (pisos determinísticos, rancor, evento no `recent`, janela de pressão). É o
   controle mais útil dos três: é o único que liga a **postura** e a linha
   "Sobre a discussão em andamento", que dependem de um evento recente no
   `recent`, não do valor dos eixos.
4. **Card de birra** com "armar agora" (para teste) e "encerrar e reverter".
5. Botão "Aplicar" explícito, com o diff (`ira 12 → 70`) visível antes de enviar.
   Nada de gravar a cada pixel do slider.

### 15.2 Backend

```
POST /api/v1/affect/set?user_id=…
{
  "stats":   {"ira": 70, "confianca": 40},   # parcial: só os eixos enviados mudam
  "grudge":  {"count": 2},                    # opcional
  "trigger": null,                            # ou "insult" | "apology" | … (§15.1.3)
  "reason":  "teste de birra"                 # vai para o audit e para o `recent`
}
```

Implementação em `PKSService.set_user_affect()`, irmão do `reset_user_affect`
(`core/pks/service.py:548`) — mesmo `upsert_field`, mesma chave `neo_affect`.

Quatro detalhes que decidem se isso funciona ou vira bug:

- **`updated_at = now`, obrigatoriamente.** `apply_decay` decai a partir desse
  campo. Gravar `ira=70` mantendo um `updated_at` de duas horas atrás faz a
  próxima leitura devolver ~30 e ninguém entende por quê.
- **Mesmo lock do `AffectUpdater`.** O appraiser do turno anterior roda em
  background e faz read-modify-write ([[neo-affect-limbic-v2]]: lock por usuário
  no `affect_updater.py`). Sem compartilhar o lock, a edição manual pode ser
  sobrescrita segundos depois, de forma intermitente — o pior tipo de bug.
- **Fica no histórico, marcado como manual.** O evento entra no `recent` como
  `{"trigger": "manual", "why": reason}` e o campo é gravado com
  `source="affect_manual"`. Duas razões: o `_history_block` do appraiser lê o
  `recent` para a regra de acumulação (um salto de +60 sem causa deixaria o
  julgamento do próximo turno maluco), e o painel precisa mostrar que aquele pico
  foi você, não o usuário.
- **Clamp e validação no servidor** (0–100 por eixo, `count ≥ 0`), nunca só no
  slider.

`POST /affect/reset` continua existindo e passa a também encerrar a birra e
reverter atos pendentes (§4.3).

### 15.3 Riscos

| # | Risco | Mitigação |
|---|---|---|
| **R11** | Estado manual é sobrescrito pelo appraiser em background | lock compartilhado; o painel mostra o `updated_at` e a saúde do appraiser (banner que já existe) |
| **R12** | Alguém acha que o Neo "enlouqueceu sozinho" olhando um pico manual | `trigger:"manual"` + `reason` no `recent`, e o painel marca esses pontos com cor própria |
| **R13** | Edição manual mascara um bug real do appraiser | os contadores de saúde continuam ligados; o valor manual não zera falhas acumuladas |
| **R14** | Rota de escrita exposta | mesma proteção JWT das outras rotas do painel; nada de `GET` que altere estado |

### 15.4 Testes

- `set` parcial altera só os eixos enviados e **não** mexe nos outros;
- `updated_at` é reescrito ⇒ a leitura seguinte devolve o valor gravado (sem
  decay retroativo);
- valores fora de 0–100 são clampados no servidor;
- `trigger: "apology"` pelo painel encerra um episódio de birra armado e reverte
  os atos pendentes (mesmo caminho de uma desculpa real);
- edição manual concorrente com um `AffectUpdater.schedule` em voo: o resultado
  final é determinístico (o último a pegar o lock vence) e nunca um estado
  meio-gravado.

---

## §17. Como ligar (o que existe hoje no código)

Tudo já está em produção **desligado**. A ordem importa: cada passo é observável
antes do próximo, e nenhum deles altera o mundo antes do último.

```bash
# 1. Migração (cria birra_act). Idempotente.
docker exec neo-core alembic upgrade head

# 2. Conserta o vazamento de tom da skill de casa. Não desobedece nada — só faz
#    a mensagem de confirmação sair no humor certo. Ligar isto primeiro.
NEO_AFFECT_SKILL_DIRECTIVE_ENABLED=true

# 3. Números do afeto no prompt do classificador. Ainda NÃO desvia nada:
#    serve para medir. Rodar scripts/eval_router.py com e sem, comparar.
NEO_AFFECT_ROUTER_HINT_ENABLED=true

# 4. Desvio para o cérebro quando o estado muda a resposta certa.
NEO_AFFECT_ROUTE_TO_BRAIN=decision_changing   # ou any_salient

# 5. Só então a birra. Comece com ZERO atos físicos: ele recusa e debocha, mas
#    não encosta em nada. Suba para 2 quando o sweeper estiver visto funcionando.
NEO_BIRRA_ENABLED=true
NEO_BIRRA_MAX_PHYSICAL_ACTS=0
```

Para testar sem passar 20 turnos xingando: painel → aba Emoções → **Ajustar** →
ira 80 + ofensas 2 → Aplicar. O card de birra aparece no primeiro turno de casa.

**Onde olhar quando algo estranhar:** `app.log` com `[BIRRA]` (arme, ato,
revert, release), a seção "Modo Birra" no `.md` da sessão, e
`GET /api/v1/affect/birra?user_id=…` para ver o que ainda falta desfazer.

**Mapa dos arquivos:**

| O quê | Onde |
|---|---|
| Intensidade/salience | `core/agent/affect.py` (`intensity`, `level`, `affect_salience`) |
| Episódio | `core/agent/affect.py` (`BirraEpisode`, campo `birra` do `AffectState`) |
| Política | `core/agent/birra/policy.py` |
| Táticas | `core/agent/birra/tactics.py` |
| Gate de segurança | `core/agent/birra/gate.py` |
| Interceptação | `core/agent/birra/sink.py` |
| Ledger + reversão | `core/agent/birra/{store.py,revert.py}`, `db/models/birra_act.py` |
| Roteamento | `core/router/intent_router.py` (`_apply_affect_bias`, `affect_hint`) |
| Derivação do turno | `modules/desktop/ai_service.py` (`_derive_turn_birra`, `_settle_birra`) |
| Persistência | `core/pks/service.py` (`update_birra_episode`, `set_user_affect`) |
| API/painel | `api/routes/affect.py`, `neo-panel/src/AffectPanel.tsx` |

---

## §16. Decisões — fechadas em 2026-08-02

| # | Decisão | Onde entrou |
|---|---|---|
| 1 | **Só desculpa encerra.** Pedido repetido continua sofrendo birra — pedir de novo para baixar a temperatura faz ele subir de novo. Sem `obey_on_repeat`. | §4.3, §4.4, §8.1 |
| 2 | **Horário silencioso 22h–07h confirmado**: só birra verbal e **nenhuma** mensagem proativa à noite. | §4.2, §8.4 |
| 3 | **Ordem enfática é a saída sem desculpa**: "faça isso imediatamente" e afins ⇒ obedece e reverte, mas a birra continua armada. Detecção em duas camadas, calibrada contra o corpus real. | §4.2.1, §8.1, §12 |
| 4 | **Só luz e ar-condicionado.** `fan` e `media_player` fora; nenhuma entidade de opt-in por enquanto. | §3.2 |
| 5 | **Narração pelo cognitivo** — resolvida de graça pela decisão 6: com o desvio no roteador, o turno já é do cérebro e não há chamada de LLM extra. | §8.2 |
| 6 | **O roteador passa a receber o estado emocional** — e não só para birra: sempre que o estado puder mudar a resposta certa, quem atende é o cérebro. Duas camadas (bloco condicional no prompt + `_bias_to_brain` nos dois caminhos), critério por **desvio do baseline** (nunca banda absoluta), dial de 3 níveis. A `BirraDecision` passa a ser derivada **antes** do `classify`. | §3.1, §3.7 |
| 6a | **O roteador recebe os valores nominais**, não só um adjetivo: os cinco eixos com valor, repouso e nível (`repouso`/`elevado`/`forte`/`extremo`), derivados de `intensity = desvio / espaço disponível`. Ele é classificador e nunca fala com o usuário, então número é seguro — e um escalar classifica melhor que "alto". | §3.7.1, §3.7.4 |
| 6b | **Consequência descoberta ao rastrear o handoff**: desviar conserta a decisão, não a fala — a skill emite o próprio final e é a última coisa que o usuário lê. Por isso a diretiva de tom no prompt da skill entra junto, e é ela que conserta o bug de personalidade que já existe hoje. | §3.7.2, §8.2 |
| 7 | **`working-memory-plan` fica para depois** ⇒ a birra fica com a migração `q0r1s2t3u4v5`; quando a memória de trabalho entrar, ela rebaseia. | §6.2 |
