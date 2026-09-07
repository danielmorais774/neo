# Plano — Normalização da entrada de voz (ASR) + tolerância no neo-core

Status: **F1–F4 IMPLEMENTADAS E EM PRODUÇÃO** · 2026-08-02 (F5/F6 pendentes — ver §8)

Prova end-to-end no sistema vivo (neo-voice do neo-host, engine Parakeet CPU):

```
FINAL_TEXT [local-cpu]: Ei Neo! Que horas são agora?  [cru: Ei New! Que horas são agora?]
```

O ASR escreveu "New"; o cliente recebeu "Neo".

Objetivo: que o Neo receba **o nome dele certo** (e o resto do texto o mais próximo
possível do que foi dito) quando a mensagem vem do módulo de voz, e que o neo-core
**saiba** que aquele texto veio de um ASR — para tolerar erro de grafia/pronúncia e
trecho fora de contexto em vez de responder "não entendi" ou (pior) transformar um
token torto em caminho de arquivo/entity_id.

---

## 0. O problema, medido no corpus de produção

Não é impressão: dá pra contar. Os turnos de voz ficam marcados com
`msg_metadata->>'mode' = 'live'` em `conversation_message` (Postgres da VPS).

```sql
-- 368 turnos de voz do usuário; 190 deles com ≤25 caracteres
SELECT count(*) FILTER (WHERE length(content)<=25) AS curtas, count(*) AS total
FROM conversation_message WHERE role='user' AND msg_metadata->>'mode'='live';

-- como o ASR escreve o nome dele
SELECT lower(m[1]) tok, count(*)
FROM conversation_message,
     regexp_matches(content,'(?i)\m(new|neil|nil|nir|niu|nio|neo|neu|knew|leo|neal|nell|bonio)\M','g') m
WHERE role='user' AND msg_metadata->>'mode'='live'
GROUP BY 1 ORDER BY 2 DESC;
```

Resultado (02/08/2026):

| token escrito | ocorrências |
|---|---|
| neil | 6 |
| new | 6 |
| nil | 3 |
| knew | 2 |
| nir | 1 |
| bonio | 1 |
| **neo** | **0** |

**Zero.** Em 368 turnos de voz o Parakeet nunca escreveu "Neo". Amostras reais:

- `Nil, você se lembra do Lorenzo?`
- `Foi tá bom, Nir. Foi tá bom. Pede pra Alexa desligar a luz do meu quarto…`
- `E aí, Neil, como vai?` · `Neil que hora são` · `Fala aí de novo, Nil.`
- `Alô New Testando.` · `Alô, testando a New.` · `Vamos lá, testando o new.`
- `A new.` · `A new` · `He knew.` · `He knew.` · `And you` · `Any` · `How` · `Hey Live.`

Três classes distintas de defeito — e elas pedem tratamentos diferentes:

**Classe A — nome próprio trocado.** "Neo" vira Neil/New/Nil/Nir/Niu/knew. Note o
loop fechado: o TTS pronuncia "níu" de propósito (`neo-voice/service/tts_text.py:138`,
`_DEFAULT_LEXICON = {"neo": "níu"}`), o usuário fala "níu", e o Parakeet mapeia esse
som para o inglês **"new"**. Os dois lados estão coerentes; quem falta é o corretor.

**Classe B — drift de idioma.** `parakeet-tdt-0.6b-v3` é **multilíngue e autodetecta
o idioma**; em enunciado curto ele escolhe errado e devolve inglês/francês:
`How`, `And you`, `Any`, `Come by`, `Her best`, `Although the standard`,
`Hello, the estando`, `Et c'est ton poème`, `Fality enough`, `Deswise`.
Boa parte disso é eco do wake word ("Ei Neo" → "He knew" / "A new" / "Hey Live").
**Não dá pra fixar o idioma nesse modelo**: no `onnx-asr` o parâmetro
`language=` de `recognize()` só vale para Whisper e Canary
([API reference](https://istupakov.github.io/onnx-asr/reference/)). Ou seja, a
correção tem que ser pós-ASR — ou trocar de modelo (§6).

**Classe C — ruído léxico comum.** `Desligaliz do quarto` (desliga a luz do quarto),
`Alô, tá estando` / `Adult testando` (alô, testando), `Coloco no map`,
`Edifica para mim`, `Heita, porta`. Aqui não existe correção determinística
confiável — quem resolve é o modelo, **desde que ele saiba que o texto veio de voz**.

**O sinal que falta.** Hoje nada diz ao neo-core "isto veio de um microfone".
`metadata["mode"] == "live"` (`neo-core/modules/desktop/ai_service.py:1713`) marca a
**saída** (responder em texto falado, sem markdown) — é estilo de resposta, não
tolerância de entrada. São coisas separadas: áudio do WhatsApp, um push-to-talk que
responda em texto, ou o notch em modo silencioso são entrada de voz sem `mode=live`.

---

## 1. Arquitetura: três camadas, um choke point cada

```
  mic ──► engine STT (Parakeet)
            │  texto cru
            ▼
     [1] neo-voice: normalize_stt_text()   ← determinístico, léxico + fonética
            │  texto corrigido + raw + fixes
            ▼
     [2] cliente (desktop web / notch): metadata["input_source"]="voice"
            │  gRPC
            ▼
     [3] neo-core: instrução de tolerância no system_context
                 + backstop determinístico do nome
                 + regra "não invente dado exato"
```

Por que três e não uma:

- A **1** conserta o que é conhecido e barato (o nome). É o único lugar onde o texto
  passa **antes** de todo mundo: `_finalize` (`/ws/audio`) e `_panel_final`
  (`/ws/panel`) em `neo-voice/service/neo_voice_server.py:1572` e `:1602` cobrem as
  **três** superfícies de voz de uma vez (chat do desktop via `liveVoiceService.ts`,
  notch via `NeoNotchHost.swift`, painel web do próprio neo-voice).
- A **2** é o parâmetro que você pediu: um campo no metadata, não uma heurística.
- A **3** é o que cobre a Classe C, que nenhuma regra determinística pega, e é a
  defesa quando o cliente for outro (mobile, áudio de WhatsApp) e não passar pela 1.

---

## 2. Camada 1 — `neo-voice/service/stt_text.py` (novo)

Espelha o `tts_text.py` (mesmo padrão de léxico + env de extensão), na direção
inversa.

```python
def normalize_stt_text(text: str, *, vocab: dict | None = None) -> tuple[str, list[dict]]:
    """Devolve (texto_corrigido, fixes) — fixes = [{"from","to","rule"}]."""
```

**Passo 1 — vocativo/nome (alta precisão, léxico literal).**
Só corrige em posição de vocativo, que é onde o nome aparece de verdade:

- início do enunciado seguido de vírgula/fim: `^(ei|oi|olá|alô|opa|fala|escuta|olha)?[,\s]*<var>\b(?=[,.!?]|\s|$)`
- entre vírgulas: `,\s*<var>\s*[,.!?]`
- depois de "testando": `test\w+\s+(a|o)?\s*<var>\b`

Variantes iniciais (medidas, §0): `neil, nil, nir, niu, nio, neu, new, knew, neal,
nell, bonio, léo`. Nunca fora de âncora: `new` solto no meio de uma frase em inglês
legítima não vira "Neo".

**Passo 2 — fonética (cobertura, precisão controlada).**
Chave fonética pt-BR simplificada (metaphone-BR ou soundex adaptado) do token vs
`neo`; corrige quando a distância é ≤ limiar **e** o token não está numa stoplist de
palavras pt válidas **e** não é um nome conhecido (ver §5, vocabulário do PKS —
"Neil" pode ser gente de verdade). Em dúvida: **não mexe** — a Camada 3 tolera.

**Passo 3 — eco do wake word (Classe B curta).**
Se o enunciado **inteiro** casar com a lista fechada de lixo conhecido
(`he knew`, `a new`, `and you`, `any`, `how`, `hey live`, `come by`, `her best`, …)
e a fala for curta, devolve `{"type":"final","text":"","dropped":"wakeword_echo"}`.
**Não precisa mexer em cliente nenhum**: tanto o `liveVoiceService.ts:163` quanto o
`handleSttJSON` do Swift já ignoram final vazio. Isso mata o "Neo respondendo *How*
do nada". Lista fechada + log de tudo que foi descartado (para auditar).

**Passo 4 — léxico de domínio (opcional, data-driven).**
`desligaliz` → `desliga a luz`, variações de "Alexa/Alexia". Entra só o que o corpus
provar que repete.

**Configuração** (mesmo padrão do `/stt/fallback`, `neo_voice_server.py:622`):

- `NEO_STT_NORMALIZE=1` — kill switch.
- `NEO_STT_LEXICON` — JSON no env que estende/sobrescreve o léxico base.
- `service/state/stt_lexicon.json` — persistido, editável em runtime via
  `GET/POST /stt/lexicon` (sobrevive a restart do container, igual ao fallback).

**Contrato do WS** (aditivo, cliente antigo não quebra):

```json
{"type":"final","text":"Neo, que horas são","raw_text":"Neil que hora são",
 "fixes":[{"from":"Neil","to":"Neo","rule":"vocative-lexicon"}],
 "infer_ms":420,"audio_seconds":1.8,"on_gpu":true}
```

Parciais também passam pelo corretor (é legenda: ver "Neil" piscando na tela é o
mesmo bug, só que visual). Log `FINAL_TEXT` passa a mostrar os dois textos quando
houve correção — é assim que a gente mede depois.

---

## 3. Camada 2 — o parâmetro `input_source` até o neo-core

Campo novo no `metadata` do `UserInput` (gRPC): `input_source` ∈ `voice` | `text`.
Independente de `mode` (`live`/`agent`), que continua sendo **estilo de saída**.
Opcionais no mesmo pacote: `asr_engine` (parakeet-gpu/cpu/openrouter), `asr_raw`
(texto antes da correção), `asr_fixes` (JSON string).

> Gotcha já conhecido: `metadata` do gRPC é `map<string,string>` — lista/objeto vai
> serializado como string JSON (mesmo padrão de `attachments` e `media_context`,
> `neo-desktop-tauri/src-tauri/src/services/ai_agent.rs:167`).

Encanamento por superfície:

| # | Superfície | Arquivos a tocar |
|---|---|---|
| 1 | Chat do desktop (Live) | `src/lib/liveVoiceService.ts` (lê `raw_text`/`fixes`) → `src/hooks/useLiveMode.ts:200` (`onSendMessage(text,'live')` ganha 3º arg `source`) → `src/pages/ChatPage.tsx:313` → `src/lib/agentService.ts` → `src/lib/tauriApi.ts:324` (`invoke('execute_with_loop', {…, inputSource})`) → `src-tauri/src/commands/mod.rs:37/67` → `services/ai_agent.rs:154` (insere no metadata) |
| 2 | Notch | `src-tauri/macos/NeoNotchHost.swift` (evento `prompt` ganha `"source":"voice"`, ele já sabe que veio do STT) → `src-tauri/src/notch_host.rs:199` (repassa a `run_agent_message`) |
| 3 | Painel web do neo-voice | nada (não fala com o core no fluxo do desktop) |

Texto digitado no notch/chat continua sem o campo (ou `text`) — a tolerância **não**
pode valer para texto digitado, senão o modelo começa a "corrigir" o que o usuário
escreveu de propósito.

---

## 4. Camada 3 — neo-core: saber que veio de voz

### 4.1 Instrução no system_context (mesmo seam do modo Live)

Em `modules/desktop/ai_service.py:1713`, ao lado do `live`:

```python
voice_in = (metadata or {}).get("input_source") == "voice"
if voice_in:
    self._voice_input_sessions.add(str(sid))
    sys_context = "\n\n".join(p for p in (sys_context, _VOICE_INPUT_INSTRUCTION) if p)
else:
    self._voice_input_sessions.discard(str(sid))
```

Set sticky por sessão igual a `_live_sessions` (`:965`) e `_image_capable_sessions`
(`:969`) — as continuações do turno perdem o metadata original, esse é o jeito que já
funciona no repo. O mesmo `sys_context` alimenta o **classificador** e o núcleo
cognitivo, então o roteamento ganha a tolerância de graça.

Rascunho de `_VOICE_INPUT_INSTRUCTION`:

```
## ENTRADA POR VOZ (transcrição automática)
Esta mensagem foi TRANSCRITA de áudio por um ASR (Parakeet), não digitada. Ela pode ter
erro de grafia, palavra colada, pontuação faltando, um trecho em outro idioma ou uma
palavra solta de ruído.
- Interprete por PROXIMIDADE FONÉTICA + contexto. "Neil/Nil/New/Niu/Nir/He knew/A new"
  é você (Neo). Nome de pessoa, lugar ou dispositivo pode vir torto.
- NÃO responda "não entendi" por erro pequeno: responda ao que é obviamente a intenção.
- NUNCA transforme um token estranho em caminho de arquivo, nome de arquivo, entity_id,
  comando ou parâmetro literal. Se o dado exato importa (caminho, id, valor, nome
  próprio novo), CONFIRME numa pergunta curta antes de agir.
- Se a mensagem inteira for incompreensível (uma ou duas palavras sem relação com a
  conversa), peça pra repetir numa frase curta — não invente assunto.
- Não comente nem corrija a transcrição em voz alta.
```

O terceiro bullet não é enfeite: ele fecha exatamente o buraco de
`neo-client-tool-invented-path-hang` — um token torto virando `file_path` inventado —
que a entrada por voz torna **mais** provável, não menos.

### 4.2 Backstop determinístico no core

`core/text/asr_fixups.py` com o mínimo: canonicalizar o nome do assistente. Roda
**só** quando `input_source == "voice"`, antes do `intent_router.classify()` e antes
de persistir. Motivo: o core não pode depender de o cliente ter um neo-voice
atualizado (mobile e áudio de WhatsApp vão entrar por outra porta). O léxico grande
mora no neo-voice; aqui fica só o nome — é o que evita as duas implementações
divergirem.

Isso também protege os guards determinísticos do router (`_is_foreign_assistant_address`,
`_is_memory_directive`…), que passam a ver o texto já canonizado.

### 4.3 O que é persistido

`content` = texto **corrigido** (é o que o usuário quis dizer, e é o que o
`conversation_recall`/embedding vai indexar). `msg_metadata.asr_raw` = original, mais
`asr_fixes`. `add_message(metadata=metadata)` já grava o metadata — sem migração.
Bônus: com `asr_raw` no banco, o eval do §7 se re-mede sozinho daqui pra frente.

### 4.4 Regra de segurança no PKS

Com `input_source=voice`, criar **entidade nova com nome próprio** exige confirmação
(o planner não pode cadastrar "Bonio" porque o ASR ouviu errado). Encaixa no guard-rail
que já existe em `core/pks/ledger_guard.py`/`pks_write` — é uma condição a mais, não
um sistema novo.

---

## 5. Vocabulário dinâmico do PKS (fase posterior)

`GET /api/v1/voice/vocabulary` no neo-core devolvendo nomes que importam (pessoas,
dispositivos, lugares do PKS + aliases). O neo-voice busca a cada N minutos e usa:

- como **whitelist** do passo fonético (não corrigir "Neil" se existe um Neil);
- como **alvo** de correção fonética ("Lorenço/Lorenzo", "Campinas" torto).

É o que ataca a Classe A além do nome do Neo, sem hardcode.

---

## 6. Alternativa na raiz (avaliar em paralelo, não bloqueia)

O `onnx-asr` aceita `language=` **em Whisper e Canary**. `nemo-canary-1b-v2` roda no
mesmo loader (`onnx_asr.load_model`) e aceita `language="pt"` + `pnc` — isso mataria a
Classe B (drift de idioma) na origem, e provavelmente metade da Classe A. Custo: modelo
maior, latência maior na 5070 Ti. Teste barato: subir um engine na porta 5012 com
`NEO_STT_MODEL=nemo-canary-1b-v2`, medir RTF e WER contra os mesmos áudios, e — se
passar — usá-lo como tier 1 com o Parakeet de fallback (a cascata do
`_local_stt_tiers()` já existe). Mesmo que dê certo, as Camadas 2 e 3 continuam
valendo: nenhum ASR acerta nome próprio 100%.

---

## 7. Medição (antes de ligar, não depois)

Corpus de avaliação já existe — são os 368 turnos de voz do §0:

```sql
\copy (SELECT id, created_at, content FROM conversation_message
       WHERE role='user' AND msg_metadata->>'mode'='live' ORDER BY created_at)
  TO '/tmp/asr_eval.csv' CSV HEADER
```

- `neo-voice/tests/test_stt_text.py` — fixtures com os casos reais do §0
  (`Nil, você se lembra…` → `Neo, você se lembra…`).
- `scripts/eval_stt_fixups.py` — roda o corretor no CSV e reporta: (a) % de vocativos
  corrigidos, (b) quantos enunciados foram descartados como eco de wake word,
  (c) **falsos positivos**: rodar o mesmo corretor nos 615 turnos digitados
  (`mode='agent'`) e exigir **zero** alteração de sentido — é o teste de precisão.

Critério de aceite para ligar em produção: ≥90% dos vocativos corrigidos e 0 falso
positivo no corpus digitado. Mesma disciplina de
`neo-guard-heuristics-measure-on-prod-corpus`: medir contra mensagem real, não chutar
regex.

### Resultado medido (02/08/2026, antes de ligar)

| corpus | resultado |
|---|---|
| 368 turnos de voz | **17 de 18** mensagens com o nome torto corrigidas (94%) + 17 descartes de eco/grunhido (4,6% dos turnos que nem deviam ter virado turno). A única que escapou é lixo puro: `Dashtum, das Tandle, New Dash Tumble` |
| 1135 turnos digitados | 4 alteradas (0,35%) — e **nenhuma é falso positivo**: `Oi, como vai, Neil?`, `niu o teu nome?` (o nome torto DIGITADO), `New ?` e `Humm`. Em produção o corretor nem roda nesse caminho |

A primeira rodada do eval reprovou: a camada fonética trocou 14 `né?`/`não`/`no` por
`Neo` — a chave de "não" é "nau" e a de "no" é "nu", ambas a distância 1 de "niu". Foi
o que criou a keep-list de pt (`_PT_KEEP_DEFAULT`) e a regressão
`test_fonetica_nao_come_palavra_pt`. Sem medir no corpus real, isso teria ido pra
produção.

---

## 8. Fases

| Fase | Entrega | Estado |
|---|---|---|
| **F1** | `stt_text.py` + plug em `_finalize`/`_panel_final` + parciais + `raw_text`/`fixes` no WS + testes + kill switch | ✅ em produção (neo-host) |
| **F2** | `input_source=voice` nas 3 superfícies + `_VOICE_INPUT_INSTRUCTION` + set sticky + `asr_raw` no metadata | ✅ core em produção; desktop precisa de `tauri dev` |
| **F3** | Backstop `core/text/asr_fixups.py` (+ a regra de confirmação de dado exato, que virou bullet da instrução) | ✅ em produção |
| **F4** | Drop de eco de wake word + `/stt/lexicon` + `/stt/normalize` + edição no `VozTab` do neo-panel | ✅ |
| **F5** | Vocabulário dinâmico do PKS | ⬜ pendente |
| **F6** (opcional) | Spike do Canary pt-BR | ⬜ pendente |

O guard do PKS ("nome próprio novo vindo de voz não vira entidade sem confirmação",
§4.4) ficou de fora do código: hoje ele é regra no prompt (bullet do dado exato), não
trava determinística no `pks_write`.

### O que foi tocado

| Onde | Arquivo |
|---|---|
| corretor | `neo-voice/service/stt_text.py` (novo), `neo_voice_server.py` (import, `_finalize`, `_panel_final`, parciais, `/stt/lexicon`, `/stt/normalize`, estado em disco) |
| testes/eval | `neo-voice/tests/test_stt_text.py` (54), `neo-voice/scripts/eval_stt_fixups.py`, `neo-core/tests/test_voice_input.py` (26) |
| transporte | `liveVoiceService.ts`, `useLiveMode.ts`, `ChatPage.tsx`, `agentService.ts`, `tauriApi.ts`, `commands/mod.rs`, `services/ai_agent.rs` (`VoiceInput`), `notch_host.rs`, `NeoNotchHost.swift` |
| core | `modules/desktop/ai_service.py` (`_VOICE_INPUT_INSTRUCTION`, `_is_voice_input`, `_build_system_context`→`_build_os_context`, backstop no início do turno, `_voice_input_sessions`, SERVER_TOOL), `core/text/asr_fixups.py` (novo), `api/routes/context.py` (`metadata` no prompt-preview) |
| painel | `neo-panel/src/settings/VozTab.tsx` (seção "STT — Corretor da transcrição") |

---

## 9. Riscos e como cada um é contido

| Risco | Contenção |
|---|---|
| Corretor destrói mensagem legítima ("Neil" é uma pessoa) | Âncora de vocativo, whitelist do PKS (§5), `raw_text` sempre preservado, kill switch |
| Drop de eco engolir turno de verdade | Lista **fechada** + limite de duração + log de todo descarte |
| A instrução de tolerância virar licença pra chutar | O bullet "nunca invente caminho/id; confirme o dado exato" faz parte da mesma instrução |
| Léxico duplicado (voice + core) divergir | Core tem só o nome canônico; léxico completo mora no neo-voice; F5 unifica via endpoint |
| Tolerância vazar pra texto digitado | `input_source` é opt-in do cliente; sem o campo, nada muda |

---

## 10. Como testar de novo (o caminho real, sem falar)

O E2E que validou tudo não precisa de microfone — usa o próprio TTS como boca:

```bash
# 1. gera o áudio ("Neo" sai como "níu", que é o que o ASR ouve errado)
curl -s -X POST http://192.168.4.210:5002/tts -H 'Content-Type: application/json' \
  -d '{"text":"Ei Neo, que horas são agora?","backend":"coqui"}' -o /tmp/say.wav

# 2. joga no WS do STT como se fosse o microfone
cd neo-voice/service && ./.venv-sys/bin/python ws_feed_wav.py /tmp/say.wav \
  ws://192.168.4.210:5002/ws/audio
# << FINAL_TEXT:Ei Neo! Que horas são agora?

# 3. confirma que quem consertou foi o corretor (e não sorte do ASR)
ssh neo-host "docker logs neo-voice --since 2m | grep FINAL_TEXT"
# FINAL_TEXT [local-cpu]: Ei Neo! …  [cru: Ei New! …]     <- o cru prova
```

Dry-run de uma frase, sem áudio nenhum: `POST /stt/normalize {"text": "..."}` (é o que
o botão "Testar" da aba Voz do neo-panel chama). E para o lado do core, o prompt do
turno de voz sai inteiro em `POST /api/v1/context/prompt-preview` com
`metadata: {"input_source": "voice", "os_platform": "darwin"}`.

## 11. Notas de deploy (não são as de sempre)

- **neo-voice não é o neo-core.** Ele roda em container no neo-host (LXC
  `192.168.4.210`, `/root/neo-voice-deploy`) — mudança em `service/*.py` exige rebuild
  **lá**, não é o Mutagen/`--reload` do neo-core.
- **neo-core**: salvar já é deploy (túnel + `--reload`), então F2/F3 entram quentes.
- **desktop**: exige rebuild (`tauri dev` — nunca `tauri build`, ver
  `neo-desktop-no-release-build`); mexer no `NeoNotchHost.swift` passa pela
  re-assinatura do `build.rs` (permissão de mic).
