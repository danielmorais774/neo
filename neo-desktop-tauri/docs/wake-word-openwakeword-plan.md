# Plano — substituir o detector de wake word (EfficientWord-Net → openWakeWord)

Status: **proposta**, não implementado. Escrito em 2026-07-20.

## 1. O diagnóstico: não é questão de calibragem

O detector atual (`sidecar/wake/neo_wake.py`, EfficientWord-Net) reconhece por
**similaridade de embedding few-shot**: você grava 4–5 amostras da frase, elas viram
um `<slug>_ref.json`, e ao vivo comparamos a janela do mic com essas referências.

Os disparos REAIS registrados no `Neo Desktop.log` mostram o problema em número:

| disparo | score |
|---|---|
| ei_neo | 0.686 |
| ei_neo | 0.669 |
| ei_neo | 0.657 |
| ei_neo | 0.655 |
| ei_neo | 0.654 |
| ei_neo | 0.648 |
| ei_neo | 0.646 |
| ei_neo | 0.644 |

O limiar é **0.64**. Ou seja: os verdadeiros positivos passam por uma margem de
**0.004 a 0.046**. Não existe separação entre "é a frase" e "não é" — qualquer ruído
que caia nessa faixa vira falso positivo, e qualquer variação sua (mais longe, mais
rápido, gripado) vira falso negativo. Subir o limiar mata o recall; baixar enche de FP.

Sintoma corroborante: há **5 frases cadastradas** (`ei_neo`, `ohh_neo`, `isso_n_o`,
`para`, `pare`) — variantes da mesma intenção, cadastradas tentando arrancar recall.
Isso é o comportamento típico de quem está lutando contra um detector que não
generaliza.

Toda a engenharia anti-FP que já existe (hop denso de 0.375s, regra de 2 hits do mesmo
slug em ≤1.1s, guard de "janela começa em silêncio", escape de hit único forte) é
compensação para essa falta de separação. **O teto é o método, não o ajuste.**

## 2. O que o openWakeWord muda

Arquitetura diferente: em vez de comparar com 4 amostras suas, ele usa um **modelo de
embedding de fala** compartilhado (da Google) + um **classificador pequeno treinado**
por frase, em cima de **milhares** de exemplos sintéticos (TTS com muitas vozes,
sotaques e augmentation) contra um corpus de negativos (música, ruído, fala genérica).

Isso é discriminativo, não por similaridade: o modelo aprende o que **não** é a frase.
É daí que vem a separação de score que falta hoje. Bônus: deixa de ser
speaker-dependent — funciona com outra pessoa, com você resfriado, mais longe do mic.

## 3. O risco honesto: não há evidência para pt-BR

Isto precisa estar na mesa antes de qualquer código:

- O autor do openWakeWord afirma que o modelo de embedding do Google **pode ter sido
  treinado só com inglês**, e que nesse caso as features "podem não ser tão efetivas em
  outros idiomas". Ele mesmo trata isso como **teórico** — o paper não especifica.
- A discussão upstream sobre outros idiomas **não tem nenhum benchmark**: sem taxa de
  FP/FN, sem resultado para pt/es/fr/de. O pedido é por voluntários nativos testarem.
- O único caso concreto de língua românica que achei (guia de francês para Home
  Assistant) reporta "consegui criar" seguido de "trabalhando em otimizações para
  detectar melhor" — funciona, mas exigiu tuning, e sem números publicados.
- O treino automatizado é **Linux-only** (dependência do Piper).

Tradução: openWakeWord em pt-BR é **plausível, não comprovado**. O plano abaixo é
desenhado para descobrir isso **barato e cedo**, em vez de construir tudo e torcer.

### openwakeword.com — o que ele é, de fato (verificado em 2026-07-20)

`openwakeword.com` **não é o projeto upstream** (`github.com/dscripka/openWakeWord`); é
uma plataforma de terceiros construída em volta dele: você treina no site e baixa um
**ONNX** que roda 100% local. Isso é bom — automatiza quase toda a Fase 2/3 deste plano.

O site é um SPA, então o conteúdo real não aparece no HTML servido. Os fatos abaixo
saíram do bundle JS (`/assets/index-*.js`), não da copy de marketing:

- **pt-BR existe, com UMA voz só.** A tabela de vozes Piper do treino tem exatamente:
  `{lang:"pt", sex:"M", voice_name:"cadu", locale:"pt_BR", note:"Brazilian Portuguese
  male; no F single-speaker at medium"}`. Uma voz, masculina, sem feminina.
  Para comparar: italiano tem 4, inglês 3, e a maioria dos 32 idiomas tem 2.
- **A página "Benchmarks" é uma FERRAMENTA, não resultados publicados.** No bundle:
  *"Score a wakeword model against augmented test clips"*. Ou seja, ela deixa **você**
  medir o seu modelo — continua não existindo número publicado para pt-BR.
- **"Voice Center": clona a sua voz** (fine-tune de Piper) e injeta no dataset de
  treino. É explicitamente vendido para sotaque/pronúncia pessoal.

**Leitura honesta:** "tem suporte a pt-BR" é verdade e engana ao mesmo tempo. Suporte
existe, mas a diversidade de vozes sintéticas — que é justamente de onde vem a força do
método — é 1 voz masculina. Um modelo treinado só com o `cadu` tende a ficar enviesado
para aquele timbre.

**O que salva o caso de uso:** este é um assistente **pessoal**. Você não precisa de
independência de locutor; precisa que acerte o *seu* "Ei Neo". O Voice Center converte a
fraqueza (1 voz) em algo aceitável: `cadu` + a sua voz clonada. E, se faltar
diversidade, você tem a carta de baixo.

### Vantagem específica sua

O gargalo do treino em outro idioma é gerar positivos sintéticos bons. Você já tem uma
**fazenda de TTS pt-BR** na 5070 Ti (Coqui XTTS com ~58 vozes, F5-TTS-pt-br,
CosyVoice3, Qwen3) — material muito melhor que o `pt_BR-*-medium` do Piper, com
variedade real de timbre. E tem GPU para treinar. Isso derruba bastante o custo das
fases 2–3.

## 4. A alternativa que deveria ser testada ANTES

**Picovoice Porcupine** suporta **português oficialmente** (não é teoria), roda
on-device/offline, e o modelo é gerado **digitando a frase** no console — transfer
learning, segundos, sem pipeline de dados sintéticos, sem GPU, sem Linux. O tier
gratuito cobre até 3 usuários ativos/mês, inclusive comercialmente — um assistente
pessoal cabe folgado.

Custo de validar: **~1 hora**. Se o Porcupine resolver, todo o esforço das fases 2–3
deste plano deixa de ser necessário. A contrapartida é depender de um serviço externo
para *gerar* o modelo (a detecção em si é local) e de uma licença de terceiros —
avaliar se isso é aceitável para o Neo é decisão sua, não técnica.

Recomendação: rodar a Fase 0 nos dois (openWakeWord e Porcupine) e deixar o número
decidir.

## 4b. Custo de cada caminho (verificado em 2026-07-20)

| caminho | custo | observação |
|---|---|---|
| Biblioteca da comunidade (openwakeword.com) | **grátis** | modelos ONNX já treinados; improvável ter "Ei Neo" |
| **Treinar no openwakeword.com** | **pago, a partir de ~4 CHF** | créditos avulsos, **sem assinatura** — "o modelo é seu" |
| Voice Center (clonar sua voz) | **pago** (créditos) | entra no cálculo de créditos do job |
| **Porcupine (Picovoice)** | **grátis** até 3 usuários ativos/mês | inclusive uso comercial; pt-BR oficial |
| **Treino local** (5070 Ti + suas vozes) | **grátis em dinheiro** | custa seu tempo + energia; hardware você já tem |

Fonte: FAQ do próprio site, presente no bundle JS em 5 idiomas — *"A biblioteca da
comunidade é completamente gratuita. Treinar uma wake word personalizada custa créditos
(a partir de ~4 CHF) porque consome tempo real de GPU. Uma vez treinado, o modelo é seu
— sem assinaturas."* O treino roda em RunPod (há `runpod_job_id` no código), daí o custo.

**Atenção a dois detalhes do cálculo de créditos** (função de custo no bundle):
- `privateCost` — **manter o modelo privado custa a mais**. O caminho barato presume
  publicar seu modelo na biblioteca da comunidade. Para um wake word pessoal isso é
  provavelmente indiferente, mas é uma escolha consciente, não um default inocente.
- `verifierCost` = 150 créditos, `qualityFilterCost`, `optunaTrials` — os ~4 CHF são o
  **piso**. Um job caprichado (com verifier + busca de hiperparâmetros) custa mais.

Nada disso é caro em termos absolutos — mas responde a pergunta: **não é tudo grátis.**
O treino na plataforma é pago por job; o Porcupine é grátis na sua escala; o treino
local é grátis em dinheiro.

## 5. O plano

### Fase 0 — Bancada de avaliação (o gate) ⚠️ fazer primeiro

Sem isto, "melhorou" vira opinião. Antes de trocar qualquer coisa em produção:

1. **Dataset de teste real, gravado no seu ambiente:**
   - ~50 positivos: "Ei Neo" em distâncias/volumes/velocidades variadas, com e sem
     música, você e (se possível) outra pessoa.
   - ~2h de negativos: TV/YouTube em pt-BR, música, sua conversa normal, **e o próprio
     Neo falando** (a fonte de FP mais provável, dado o gate anti-eco existente).
2. **Harness offline** que roda um detector sobre esses WAVs e cospe FP/hora e taxa de
   detecção por limiar (curva ROC). Roda os três: EfficientWord-Net atual (baseline),
   openWakeWord, Porcupine.
3. **Critério de aceite explícito**, ex.: ≥95% de detecção com **≤1 FP/hora** no corpus
   de negativos, e separação de score ≥0.2 entre o positivo mais fraco e o negativo
   mais forte.

Entregável: uma tabela comparativa. É ela que decide o resto — inclusive "fica como
está".

### Fase 1 — Prova de conceito do embedding em pt-BR (barato)

Antes de montar o pipeline de treino completo, testar a hipótese de risco: treinar um
modelo openWakeWord **rápido e pequeno** para "Ei Neo" (algumas centenas de amostras
sintéticas, não milhares) e medir na bancada da Fase 0.

- Se o score separar bem → o embedding do Google funciona para pt-BR, seguir.
- Se colar no ruído → **a hipótese morreu**; não adianta escalar o dataset. Ir de
  Porcupine (ou reavaliar).

Este é o gate que evita construir a Fase 2 inteira em cima de uma suposição.

### Fase 2 — Pipeline de geração de dados (só se a Fase 1 passar)

- Gerar positivos com as **suas** engines pt-BR via `neo-voice` (varrer vozes do XTTS,
  F5, CosyVoice), variando prosódia/velocidade.
- Augmentation: ruído (MUSAN/AudioSet), RIR (reverberação de sala), variação de ganho e
  de distância simulada.
- Negativos: fala pt-BR genérica + música + **áudio do próprio Neo falando**.
- Roda na 5070 Ti (WSL2 já existe lá) ou num LXC Linux.

### Fase 3 — Treino e empacotamento

- Treinar o classificador; exportar **ONNX**.
- Modelo final é pequeno (~centenas de KB) e roda em CPU no Mac.
- Repetir para a frase de **stop** ("pare"), que tem requisito diferente: recall alto,
  precisão menor (um FP só age se o Neo estiver falando) — ver
  `neo-stop-word-barge-in`.

### Fase 4 — Troca do sidecar (blast radius pequeno)

O contrato JSON entre sidecar e Rust é o ponto de corte natural. Se o detector novo
emitir os **mesmos eventos**:

```json
{"event":"ready","models":[...],"threshold":0.5}
{"event":"wake","model":"ei_neo","score":0.93}
{"event":"interrupt","model":"pare","score":0.88}
{"event":"log","message":"..."}
{"event":"error","message":"..."}
```

…então **não muda nada** em:
- `src-tauri/src/services/wake.rs` (supervisor, gate anti-eco, roteamento notch-vs-janela)
- `src/hooks/useWakeWord.ts` e o consumo no `ChatPage`
- toda a lógica de Modo Live

Muda só `sidecar/wake/neo_wake.py` (novo detector) + `setup.sh`/`requirements.txt`.

**Simplificação esperada:** a regra de 2 hits, o guard de silêncio e o hop de 0.375s
existem para compensar a falta de separação. Com um detector que separa de verdade,
isso deve virar um limiar simples + debounce. Só remover **depois** que a Fase 0
provar a separação — não junto.

### Fase 4b — Verificador de voz (a arquitetura do "Ok Google") ⭐

É assim que Google e Apple fazem, e o openWakeWord suporta nativamente
(`docs/custom_verifier_models.md`). São **dois estágios**, e confundi-los é o erro
conceitual que o detector atual comete:

| estágio | o que faz | independente de locutor? | onde treina |
|---|---|---|---|
| 1. **Detector** | acha a FRASE ("Ei Neo") | sim | uma vez, GPU/WSL2, dados sintéticos |
| 2. **Verificador** | confirma que foi **VOCÊ** | não (esse é o ponto) | **local, em segundos** |

O verificador é uma **regressão logística** sobre as features que o próprio
openWakeWord já calcula — minúsculo. Só roda quando o detector passa do limiar.

Requisitos de dados (do doc oficial, "<5 minutos de esforço"):
- **mínimo 3 exemplos** da frase falada por você;
- **~10 s** de fala sua SEM a wake word (negativos);
- opcionalmente, clipes do ambiente e — o mais valioso — **gravações de falsos
  positivos reais**: *"one of the most effective ways to reduce false activations"*.

**Isso salva o `WakeWordPanel` que já existe.** Gravar a sua voz continua tendo função;
só muda o papel: em vez de SER o detector (que é onde ele falha hoje), ela alimenta o
verificador. E como é regressão logística, retreinar é instantâneo e **local** — sem
WSL2, sem GPU.

**Loop de feedback:** cada falso positivo observado em produção pode ser gravado e
jogado no conjunto de negativos do verificador. O sistema melhora com o uso, o que hoje
é impossível.

Configuração final:
```python
oww = openwakeword.Model(
    custom_verifier_models={"ei_neo": "verifier_daniel.pkl"},
    custom_verifier_threshold=0.3,
)
```

Trade-off honesto: com verificador, o sistema fica **menos propenso a responder a outras
pessoas**. Para um assistente pessoal isso é recurso, não defeito — mas é uma escolha.

### Fase 5 — UX de cadastro (a mudança de conceito)

Hoje `WakeWordPanel.tsx` grava sua voz e cria a referência na hora. Isso **deixa de
existir** como mecanismo: openWakeWord precisa de treino offline (horas, GPU, Linux) e
Porcupine gera o modelo no console.

Opções para o painel:
- **a)** vira um seletor de modelos já treinados (empacotados com o app);
- **b)** mantém a gravação, mas como *dados de avaliação* (alimenta a bancada da
  Fase 0), não como enrollment;
- **c)** botão "treinar nova frase" que dispara o pipeline remoto na 5070 Ti e baixa o
  ONNX quando pronto.

(c) é o mais fiel ao que existe hoje, e o mais caro. Decidir só na Fase 4.

## 5b. Escolha das frases-gatilho (fazer ANTES de treinar)

Treinar um modelo excelente para uma frase mal escolhida desperdiça o treino. Parte dos
falsos positivos de hoje vem da **frase**, não do detector.

### Critérios (literatura de KWS + o que medimos aqui)

1. **≥3–4 sílabas.** Frase curta colide com fala normal o tempo todo.
2. **Não pode ser palavra/sequência comum do português.** Este é o critério que mais
   pesa e o mais violado hoje.
3. **Sem vizinho acústico frequente.** Ver a análise do "né o" abaixo.
4. **Wake e stop foneticamente distantes** entre si.
5. **Stop pode ser mais curto**: só age enquanto o Neo fala, então recall > precisão.

### Auditoria das frases atuais

| frase | veredito |
|---|---|
| `ei_neo` | ⚠️ arriscada — colide com **"né, o..."**, e "né?" é onipresente na fala BR |
| `ohh_neo` | ⚠️ mesma colisão, variante da mesma intenção |
| `pare` | 🟡 aceitável (só age durante a fala do Neo), mas é forma verbal comum |
| **`para`** | ❌ **descartar já** — é a preposição mais comum do português ("vou *para* casa", "*para* mim", "isso é *para* você"). Nenhum detector sobrevive a isso |
| **`isso_n_o`** | ❌ **descartar já** — "isso não" é sequência corriqueira em conversa |

**Isto é achado, não opinião:** `para` e `isso não` disparam sozinhos em qualquer
conversa. Trocar de detector sem trocar essas frases mantém o problema.

### Vizinhos acústicos MEDIDOS (dado de ouro p/ `custom_negative_phrases`)

Transcrevendo suas gravações reais de "Ei Neo" com ASR livre pt-BR, saiu:

```
real_ei_neo_02 -> 'ênio'
real_ei_neo_04 -> 'em'
real_ei_neo_05 -> 'em'
```

Ou seja, para um reconhecedor pt-BR o seu "Ei Neo" soa como **"ênio" / "em"**. Esses são
os confusores reais — e vão direto para `custom_negative_phrases`, que é justamente o
campo criado para matar FP específico:

```yaml
custom_negative_phrases:
  - "ênio"        # medido
  - "em"          # medido
  - "né o"        # colisão com discurso BR
  - "e não"
  - "ei não"
  - "meio"        # o ASR também produziu p/ variantes de "neo"
  - "e o"
```

### Candidatas — WAKE

| frase | sílabas | prós | contras |
|---|---|---|---|
| **"Alô Neo"** ⭐ | 3–4 | "alô" quase não aparece em conversa presencial (é do telefone); **evita a colisão "né o"** | soa telefônico |
| **"Escuta Neo"** | 4 | muito distintiva, verbo raro em vocativo | mais longa de falar |
| **"Acorda Neo"** | 4 | distintiva, metáfora boa p/ acordar o Live | idem |
| "Ei Neo" (manter) | 3 | é a identidade que você já usa | colisão "né o"; exige negativos pesados |
| "Oi Neo" / "Fala Neo" | 3 | naturais | "oi"/"fala" comuns demais |

### Candidatas — STOP

| frase | prós | contras |
|---|---|---|
| **"Pausa"** ⭐ | rara em conversa, 2 sílabas, rápida | — |
| **"Silêncio"** | muito distintiva | 3 sílabas, mais lenta p/ barge-in |
| "Pare" (manter) | já habituado | forma verbal comum |
| ~~"Para"~~ | — | ❌ preposição comuníssima |

### Por que NÃO treinar só "neo"

Tentador (pegaria "ei neo", "ôh neo", "oi neo" de graça, já que o modelo desliza sobre o
áudio e a frase-alvo está contida em todas). **E funcionaria** — mas é a pior escolha
possível em pt-BR:

- 2 sílabas apenas → vizinhança acústica densa (é o critério nº 1 violado);
- colide com **"né, o..."** e com **"né"** sozinho, onipresentes na fala brasileira;
- colide com o sufixo produtivo **"-âneo"**: inst*antâneo*, esp*ontâneo*, sim*ultâneo*,
  contempor*âneo*, moment*âneo*, subterr*âneo* — todos contêm /ˈne.u/.

A colisão deixa de ser com uma frase e passa a ser com o **léxico do idioma**, o que
`custom_negative_phrases` não conserta. A liberdade de prefixo se obtém pela LISTA de
variantes, não encurtando a frase.

### Lista proposta (decidida em 2026-07-20)

```yaml
# modelo 1 — WAKE (variantes na MESMA intenção = 1 treino, ativa em qualquer uma)
target_phrase: ["ei neo", "alô neo", "ôh neo", "oi neo"]
custom_negative_phrases:
  - "ênio"        # MEDIDO: o ASR ouviu isso nas gravações reais
  - "em"          # MEDIDO
  - "né o"        # colisão com discurso BR
  - "e não"
  - "meio"
  - "e o"
  - "instantâneo" # família "-âneo": contém /ˈne.u/
  - "espontâneo"
  - "simultâneo"
  - "contemporâneo"

# modelo 2 — STOP
target_phrase: ["pausa", "pare"]
custom_negative_phrases: ["para", "para mim", "para casa", "pausa para", "preparar"]
```

`para` e `isso não` saem do cadastro — ver auditoria acima.

**A verificar na bancada:** se "oi neo"/"ôh neo" (prefixos comuns) diluírem o modelo,
podar para ["ei neo", "alô neo"]. Os negativos da família "-âneo" precisam entrar no
corpus de teste para medir isso de verdade.

## 5c. Estado do ambiente de treino (montado em 2026-07-20)

WSL2 Ubuntu do PC gamer, acesso via [[pcgamer-wsl2-ssh-access]]. Workspace em
`~/neo-wakeword-train` (root, 903 GB livres). Rodar SEMPRE de um cwd neutro
(`work/`), nunca da raiz — a pasta `openwakeword/` do repo ofusca o pacote
instalado como namespace package (`__file__` vira None).

- **Python**: 3.11.15 via `uv` (isolado; o sistema só tinha 3.14, novo demais).
  Venv sem pip — usar `uv pip install --python .venv/bin/python`.
- **torch/torchaudio 2.5.1 CPU** (par FIXADO). O 2.13 que veio por default NÃO
  carrega a extensão compilada no Ubuntu 26.04 (`_torchaudio.abi3.so`, ABI). GPU
  não é usada (decisão §4b): classificador é minúsculo, positivos vêm das engines.
- openwakeword (editable), onnxruntime 1.27, speechbrain 0.5.14, datasets, scipy,
  webrtcvad, acoustics — todos importando.
- **Modelos-base baixados**: `melspectrogram.tflite/onnx`, `embedding_model.tflite`
  (feature extraction) + pré-treinados (alexa, hey_mycroft…).
- **Negativos**: `openwakeword_features_ACAV100M_2000_hrs_16bit.npy` (~16 GB)
  baixando em background (`~/neo-wakeword-train/data/`, log em `download.log`).

Gotcha operacional: comandos vão por `ssh dmdan@... 'wsl -d Ubuntu -- bash -l'`
com o script no **stdin** (heredoc/pipe), não scp — a tradução de caminho
Windows↔WSL é frágil. Filtrar a saída com `tr -d '\0'`.

## 6. Riscos e gotchas conhecidos

- **onnxruntime come todos os cores.** Já aconteceu neste projeto: o sidecar foi a
  418% de CPU até capar `intra_op_num_threads=1` (ver `neo-debug-artifacts-and-gotchas`).
  O openWakeWord é onnxruntime — aplicar a mesma trava desde o primeiro dia.
- **Python 3.9.6** no `sidecar/wake/.venv`, macOS **arm64**. Conferir rodas do
  openwakeword/onnxruntime nessa combinação; pode exigir subir a versão do Python.
- **Treino é Linux-only** (Piper). Usar WSL2 do PC gamer ou um LXC.
- **Não remover a engenharia anti-FP antes de medir** (ver Fase 4).
- Já existe `.venv-eff` além de `.venv` no sidecar — checar qual está em uso antes de
  mexer no ambiente.

## 7. Ordem sugerida (revisada após verificar o openwakeword.com)

A plataforma derruba muito o custo de descobrir a resposta. Agora a Fase 1 é uma tarde,
não uma semana:

1. **Fase 0** — bancada com áudio real do seu ambiente + baseline do detector atual.
   Continua sendo o primeiro passo: é o número que não existe hoje. Os negativos
   precisam incluir **o próprio Neo falando** e TV/música em pt-BR.
2. **Fase 1 via openwakeword.com** — treinar "Ei Neo" com `cadu` **+ a sua voz clonada**
   (Voice Center) e medir na SUA bancada, não só na deles. Sai um ONNX.
3. **Porcupine em pt-BR** na mesma bancada — ~1h, suporte oficial ao idioma.
4. Comparar os três com o mesmo corpus. **O número decide.**
5. Só se o openWakeWord vencer **e** o viés de 1 voz atrapalhar → Fase 2 local, gerando
   positivos com as suas 58 vozes de XTTS/F5/CosyVoice.
6. Fases 4–5 (troca do sidecar + UX de cadastro) valem para qualquer vencedor.

O risco do embedding em pt-BR **não é resolvido por nenhuma promessa de site** — só pela
medição do passo 4.

Relacionado: `neo-wake-word`, `neo-stop-word-barge-in`,
`neo-debug-artifacts-and-gotchas`.
