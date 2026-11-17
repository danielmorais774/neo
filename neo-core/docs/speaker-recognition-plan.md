# Plano — Reconhecimento de locutor (quem falou) no neo-voice

Status: **PLANO** · 2026-08-03 · nada implementado ainda

Objetivo: que o Neo saiba **quem** está falando, não só **o que** foi dito. A identidade
sai do neo-voice junto do texto final, viaja pelo mesmo caminho que `input_source=voice`
já percorre ([[voice-input-normalization-plan.md]]) e chega ao neo-core como um sinal de
primeira classe — para endereçar a pessoa certa, para não contaminar o PKS do Daniel com
fatos de terceiros, e para restringir o que um desconhecido consegue mandar o Neo fazer.

Vocabulário (importa, porque os três problemas têm dificuldades muito diferentes):

| termo | pergunta | dificuldade |
|---|---|---|
| **verificação** | "esta voz é do Daniel?" (1:1) | fácil |
| **identificação closed-set** | "qual dos 4 moradores é?" (1:N) | fácil |
| **identificação open-set** | "é um dos 4, ou é alguém que eu não conheço?" | **é aqui que mora o problema** |
| **diarização** | "neste áudio, quem falou em cada trecho?" | outro problema (F8, opcional) |

O pedido — "bem preciso" — é essencialmente um pedido de **open-set robusto**. Errar entre
dois moradores conhecidos é raro com qualquer embedding decente; o que quebra na prática é
(a) aceitar um estranho como se fosse o Daniel e (b) enunciado curto demais para decidir.

---

## 0. O achado que dita o desenho: os enunciados deste sistema são curtos

A precisão de um sistema de locutor não é um número — é uma **curva em função da duração
da fala**. E o corpus de produção já diz em que ponto dessa curva o Neo vive.

Do [[voice-input-normalization-plan.md]] §0, medido no Postgres da VPS:

```sql
SELECT count(*) FILTER (WHERE length(content)<=25) AS curtas, count(*) AS total
FROM conversation_message WHERE role='user' AND msg_metadata->>'mode'='live';
-- 190 curtas de 368 turnos de voz
```

**Mais da metade dos turnos de voz tem ≤25 caracteres** — "Neil que hora são", "A new.",
"Fala aí de novo, Nil." São 1–2 segundos de fala. Nessa faixa, qualquer embedding de
locutor perde muita margem: a EER típica de VoxCeleb é reportada em enunciados completos,
e cai várias vezes abaixo de ~2 s de fala efetiva.

Consequência de projeto, e é a decisão central deste plano:

> **A identidade não é decidida por enunciado isolado. É acumulada por sessão.**

Um turno curto contribui evidência (log-likelihood ratio) para um estado de identidade que
vive na conexão WS. Turnos longos decidem sozinhos; turnos curtos confirmam ou empurram o
estado. Trocar de pessoa exige evidência contrária consistente, não um único score torto.
Sem isso, o sistema pareceria funcionar nos testes (onde a gente fala frases longas) e
falharia justamente no uso real (onde se fala "Neo, apaga a luz").

Segundo achado, direto do código: `Utterance.final_pcm()` devolve o buffer **com o pré-roll
e com a cauda de silêncio de ~`VAD_SILENCE_SEC`** — de propósito, porque o transducer do
Parakeet precisa desse silêncio para fechar a última palavra
([`neo_voice_server.py:1599-1603`](../../neo-voice/service/neo_voice_server.py#L1599-L1603)).
Para o STT isso é certo; para o embedding é **errado**: silêncio dilui o vetor. O extrator de
locutor precisa dos frames que o VAD já marcou como fala — que o `Utterance` hoje conta
(`speech_samples`) mas não guarda separado.

---

## 1. Decisões de arquitetura

### 1.1 Onde roda: dentro do container do neo-voice, na CPU

Não no PC gamer. O histórico deste sistema é inequívoco: o PC gamer dorme, o WSL2 cai atrás
do `netsh portproxy` ([[neo-voice-wsl-portproxy]]), as engines de TTS ficam DOWN e **hoje todo
TTS já sai pelo Kokoro** ([[neo-voice-vps-migration]]). Um reconhecedor de locutor pendurado
na RTX estaria indisponível exatamente com a mesma frequência — e identidade que some metade
do tempo é pior que identidade nenhuma, porque as guardas construídas em cima dela viram
loteria.

Além disso o áudio **já está em RAM no ponto certo**: `_panel_final` / `_finalize` têm o PCM
completo do enunciado, no formato que o modelo quer (16 kHz mono int16 — sem resample). Rodar
ali é zero hop de rede, zero dependência nova de infra.

Custo: um modelo de embedding de locutor é pequeno (dezenas de MB) e a inferência de um
enunciado de 3–5 s em CPU deve ficar na casa de dezenas a poucas centenas de ms — **a medir no
F0**, não assumir. E mesmo o pior caso não aparece no relógio, porque:

### 1.2 Latência: concorrente com o STT, nunca no caminho das parciais

```python
# em _panel_final, no lugar da chamada solta de hoje:
stt_res, spk_res = await asyncio.gather(
    engine_transcribe_pcm(pcm),
    speaker_identify(voiced_pcm),          # CPU local, nunca falha o turno
    return_exceptions=True,
)
```

O STT vai para a rede (PC gamer via ZeroTier, ou LXC, ou OpenRouter) e leva de ~100 ms a
alguns segundos. O embedding roda local, em paralelo, dentro dessa janela. **Custo de wall
clock esperado: ~0.** As parciais (`/ws/panel`, a cada `VAD_PARTIAL_SEC`) **não** recebem
identificação — são legenda ao vivo, e o mesmo raciocínio de `_engine_transcribe_openrouter`
vale aqui.

Se `speaker_identify` levantar ou estourar um deadline (`NEO_SPK_DEADLINE_MS`, default 800),
o turno segue **sem** identidade. A identidade é enriquecimento; nunca bloqueia a fala.

### 1.3 Qual modelo

Candidatos, com EER de referência em VoxCeleb1-O (número de **catálogo**, não deste sistema —
serve só para montar a lista curta):

| modelo | EER ref. | runtime | nota |
|---|---|---|---|
| **WeSpeaker `voxceleb_resnet34_LM`** | ~0.9 % | **ONNX** pronto no zoo | Apache-2.0, sem torch, é o candidato default |
| 3D-Speaker **CAM++** | ~0.6–0.7 % | ONNX exportável | bem mais leve (~7 M params), forte em multilíngue |
| WeSpeaker `voxblink2_samresnet100` | ~0.5 % | ONNX | melhor EER, bem mais pesado na CPU |
| SpeechBrain **ECAPA-TDNN** | ~0.9 % | torch (já está no container) | integração trivial, mais lento na CPU |

**Recomendação: começar pelo ResNet34-LM em ONNX.** O `onnxruntime` já entra no container via
`kokoro-onnx`, então não há dependência nova pesada; e não amarra o extrator ao torch, que só
está lá por causa do Silero VAD. CAM++ entra como segundo candidato no benchmark do F0 — em
voz pt-BR ele costuma se sair melhor do que o EER de VoxCeleb sugere.

Mas a escolha **se decide medindo** (F0), não por tabela. As EER acima são de VoxCeleb (inglês,
áudio de YouTube). O que importa aqui é a EER **nesta casa, em pt-BR, através desta cadeia**:
mic do notch / mic interno do Mac / headset Bluetooth ([[neo-desktop-audio-route-aec]]),
sala com eco, VAD do Silero recortando. É a mesma disciplina que este projeto já aplicou ao
medir int8 vs fp32 no Parakeet e as guardas contra o corpus real — dá para medir, então mede.

### 1.4 Um enunciado = um locutor

O VAD já segmenta por enunciado (silêncio de `VAD_SILENCE_SEC` fecha). Assumir **um locutor por
segmento** cobre o caso dominante (uma pessoa falando com o Neo) e mantém o F1–F6 simples.
Detecção de troca de locutor dentro do mesmo segmento e fala sobreposta ficam no F8, opcional,
e só se a medição mostrar que acontece de verdade.

---

## 2. Precisão: o que é o alvo e como se chega nele

### 2.1 Alvos declarados (a validar no F0/F7, não a prometer antes)

| métrica | alvo | por quê |
|---|---|---|
| identificação closed-set, fala ≥ 3 s | ≥ 99 % | é o caso fácil; abaixo disso o modelo/pipeline está errado |
| identificação closed-set, fala 1–2 s | ≥ 90 % **por sessão acumulada** | é o caso real (metade dos turnos) |
| falsa aceitação de desconhecido (open-set) | ≤ 1 % no limiar de aceite | é a métrica de segurança — a que protege PKS e atuação |
| falsa rejeição do Daniel | ≤ 5 % (cai em `uncertain`, não em erro) | preferir "não sei" a "é outra pessoa" |
| turnos sem decisão (`unknown`/`uncertain`) | aceitável até ~15 % em fala < 1.5 s | é resposta honesta, não falha |

Regra assimétrica explícita: **falso "é o Daniel" é muito mais caro que falso "não sei"**,
porque o primeiro abre a porta do PKS e da atuação. Os limiares são calibrados nessa direção.

### 2.2 Os cinco mecanismos que compram precisão

1. **Só frames de fala.** `Utterance` passa a manter um buffer paralelo `voiced` com os frames
   que passaram no VAD (`prob >= 0.5`), sem pré-roll e sem cauda. Custo: um `bytearray` a mais
   no `add()`. O buffer com silêncio continua indo para o STT, inalterado.

2. **Piso de duração.** Abaixo de `NEO_SPK_MIN_SPEECH_SEC` (default 1.2 s de fala efetiva) o
   enunciado não decide nada — só contribui evidência fraca ao estado da sessão. O código já
   tem `utt.speech_seconds()` para isso.

3. **Score normalization (AS-Norm).** O escore cru de cosseno passeia com microfone, sala e
   volume — um limiar fixo em cima dele não sobrevive à troca de mic BT para mic interno.
   AS-Norm normaliza o escore contra uma coorte de impostores (algumas centenas de embeddings
   de vozes quaisquer, embutidos na imagem), tornando o limiar **estável entre condições**.
   É o único item da lista que exige trabalho não óbvio, e é o que mais rende no open-set.

4. **Múltiplos embeddings por pessoa, não um centroide só.** Cada perfil guarda N embeddings
   (sessões, mics e distâncias diferentes) e o escore é o **top-k médio** contra eles, não a
   distância a uma média única. Um centroide só de um enrollment feito num único mic é o jeito
   clássico de o sistema funcionar na demo e falhar no sofá.

5. **Acumulação por sessão** (§0). Estado de identidade por conexão WS: prior por pessoa,
   atualizado a cada final com o LLR do enunciado, com decaimento por tempo de silêncio.
   Isso é o que faz "Neo, apaga a luz" (1.1 s) herdar a certeza construída pelos três turnos
   anteriores da mesma pessoa na mesma sessão.

### 2.3 Três estados de saída, não dois

```
score_norm >= ACCEPT   -> {"speaker": "daniel", "confidence": 0.93, "decision": "accept"}
REJECT < s < ACCEPT    -> {"speaker": null, "top": "daniel", "confidence": 0.61, "decision": "uncertain"}
score_norm <= REJECT   -> {"speaker": null, "decision": "unknown"}
```

`uncertain` é um estado de primeira classe: o neo-core recebe o palpite **e** o fato de que é
palpite, e pode agir (responder normal) sem ganhar as permissões do `accept`. E é o que
habilita a saída social correta — o Neo perguntar "quem é?" em vez de chutar um nome.

---

## 3. Enrollment (cadastro das vozes)

### 3.1 Fontes de áudio que já existem

Antes de gravar qualquer coisa nova, este repo já tem voz rotulada:

- `jarvis-samples/` e o `.mp3` na raiz do projeto;
- **os clones de TTS** — `/opt/neo-coqui/clones/` e `/opt/neo-tts/clones_qwen/` guardam o áudio
  de referência de cada clone, que é por definição 6–20 s de fala limpa de **uma pessoa
  identificada** ([[neo-voice]]). Enrollment de graça, já rotulado.

Isso serve para bootstrapar o F0. Não substitui o enrollment próprio, porque é áudio de uma
condição só.

### 3.2 Enrollment guiado (o que fica no produto)

Receita para dar precisão de verdade — **3 sessões por pessoa, ~30 s de fala cada, em condições
diferentes**: (a) perto do Mac, mic interno; (b) headset/AirPods; (c) longe, falando normal. É
a diversidade de canal, não o volume total de áudio, que faz o perfil generalizar.

A interface tem que **guiar** isso, em vez de pedir "fale por 90 segundos" — e é por isso que o
enrollment é uma tela, não um endpoint. O desenho dela está no **§4**; os endpoints
(`WS /ws/speaker/enroll` para gravar ao vivo, `POST /speaker/enroll` multipart para subir arquivo,
no mesmo padrão do `/clone` que já existe em
[`neo_voice_server.py:811`](../../neo-voice/service/neo_voice_server.py#L811)) são a mecânica por baixo.

### 3.3 Adaptação incremental (F7)

Depois de um `accept` de alta confiança em enunciado longo, o embedding entra no perfil
(janela deslizante, teto de N por pessoa). Guardas contra envenenamento e drift:
só acima de `ADAPT_MIN_CONF`, só acima de `ADAPT_MIN_SEC`, nunca em turno marcado como eco do
TTS, e com um comando de reverter (`POST /speaker/profile/{id}/rollback`) que descarta as
adições e mantém só o enrollment original.

---

## 4. A GUI: aba **🧑 Vozes** no painel web

O painel (`service/web/index.html`, 432 linhas, autocontido) já tem duas abas — `🎙️ Ouvir (STT)`
e `🔊 Falar (TTS)` — no padrão `data-tab` / `view-<nome>`
([:95-97](../../neo-voice/service/web/index.html#L95-L97)). **Terceira aba: `🧑 Vozes`**, com três
sub-modos, porque são três atividades diferentes que compartilham o mesmo microfone.

Isso não é enfeite: sem uma superfície de gravar-e-testar, o F0 (§9) não sai do papel. Corpus
rotulado e curva de acurácia × duração se constroem clicando, não por script.

```
┌─ 🧑 Vozes ─────────────────────────────────────────────────────────────┐
│  ( Gravar )  ( Testar )  ( Rotular )          perfis: 4 · 12min48s     │
├────────────────────────────────────────────────────────────────────────┤
│  Pessoa:  [ Daniel        ▾ ]  [+ nova]                    [🗑 apagar] │
│                                                                        │
│  Sessão 1 · perto, mic interno   ████████████████████░░░  26s / 30s ✓  │
│  Sessão 2 · headset / AirPods    ██████░░░░░░░░░░░░░░░░░   8s / 30s    │
│  Sessão 3 · longe, voz normal    ░░░░░░░░░░░░░░░░░░░░░░░   0s / 30s    │
│                                                                        │
│            ( ● gravar sessão 2 )        ou   [ subir arquivo… ]        │
│                                                                        │
│  ⚠ só 1 condição de mic — o perfil não generaliza (ver §3.2)           │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Gravar (enrollment guiado) — F3

O que faz a diferença entre um enrollment que funciona e um que engana: a barra conta
**segundos de fala efetiva** (frames que passaram no VAD), não segundos de gravação. Falar 30 s
com 12 s de pausa não é 30 s de enrollment, e um contador de relógio esconderia isso.

- **`WS /ws/speaker/enroll?person=<id>&session=<n>`** — mesmo transporte da aba STT (PCM int16
  16 kHz), mas o servidor roda só o VAD: acumula os frames de fala, devolve
  `{"type":"progress","voiced_sec":12.4,"snr_db":21,"clipping":false}` continuamente e o cliente
  desenha a barra. Nada de STT (é fala qualquer, não precisa virar texto).
- **Três sessões guiadas** (§3.2), com o rótulo da condição visível na tela. A UI **não deixa**
  concluir com uma condição só sem mostrar o aviso — é o erro clássico que faz o sistema passar
  na demo e falhar no sofá.
- **Sinal de qualidade ao vivo**: SNR estimado, clipping, e "fala muito curta / muita pausa".
  Enrollment ruim é a causa raiz mais comum de reconhecimento ruim, e o lugar de detectar isso é
  no momento da gravação, não seis fases depois.
- **`[subir arquivo…]`** — mesmo `multipart` do `/clone` que já existe
  ([:811](../../neo-voice/service/neo_voice_server.py#L811)). É por aqui que entram os áudios de
  referência dos clones de TTS e os `jarvis-samples/` (§3.1).
- **Consentimento**: ao criar uma pessoa nova, checkbox explícito de consentimento + escolha de
  guardar ou não o áudio (§8). Sem o check, não grava.

### 4.2 Testar (a prova ao vivo) — útil já no F2, completo no F4

É a tela que responde "está funcionando?" — e ela precisa mostrar **o escore contra todos os
perfis**, não só o vencedor. Um sistema que acerta com margem de 0.02 está a um mic de distância
de errar, e só o vetor completo mostra isso.

```
┌─ Testar ───────────────────────────────────────────────────────────────┐
│  ( ● falar )                            [x] modo cego (esconde nomes)  │
├────────────────────────────────────────────────────────────────────────┤
│  "apaga a luz do quarto"                          1.4s fala · 240ms    │
│                                                                        │
│     ✅ Daniel        0.81  ███████████████████░░░░░  accept            │
│        Lorenzo       0.34  ████████░░░░░░░░░░░░░░░░                    │
│        (clone_ana)   0.11  ██░░░░░░░░░░░░░░░░░░░░░░  synthetic         │
│        ─── aceite 0.72 ─── incerto ─── 0.45 rejeita ───                │
│                                                                        │
│     sessão: Daniel 0.97  (4 turnos acumulados)      [ 👤 era outro… ]  │
└────────────────────────────────────────────────────────────────────────┘
```

- **`WS /ws/speaker/test`** — devolve, por enunciado: vetor de escores normalizados (AS-Norm)
  contra cada perfil, o cru também, a decisão, os limiares vigentes, `voiced_sec` e o tempo de
  inferência. A régua de limiares desenhada embaixo das barras é o que torna a calibração do
  §2.3 tangível em vez de abstrata.
- **Estado da sessão visível** — a acumulação do §0 é a parte menos intuitiva do desenho; ver
  "sessão: Daniel 0.97 (4 turnos)" subindo enquanto se fala é o que mostra que turno curto
  herda certeza dos anteriores.
- **Modo cego** — esconde os nomes até a decisão sair. Sem isso a gente lê o resultado que
  espera. É o único jeito honesto de testar com outra pessoa falando.
- **`[👤 era outro…]`** — corrige na hora e manda o enunciado para o corpus com o rótulo certo.
  Cada teste vira dado; a tela de testar **alimenta** a de rotular.
- **Comparar canais** — o mesmo enunciado gravado no mic interno e no headset, lado a lado, é o
  teste que expõe o problema que o AS-Norm existe para resolver.

No **F2 (shadow)** essa tela já funciona sem nenhuma decisão ligada: mostra escores e nada mais.
É assim que se escolhem os limiares — vendo a distribuição real antes de deixar o sistema decidir
qualquer coisa.

### 4.3 Rotular (o corpus do F0)

Fila dos enunciados capturados em modo sombra: tocar o áudio, escolher quem falou (ou
`desconhecido` / `eco do TTS`), 2 cliques. Mostra o total por pessoa e por faixa de duração —
que é a barra de progresso que realmente importa, porque a faixa 1–2 s é a que precisa de
volume (§0).

Botões de expurgo: apagar áudio já rotulado (fica só o embedding + o rótulo) e apagar tudo.

### 4.4 O que dá para reaproveitar do `index.html`

Praticamente todo o caminho de captura já está pronto e testado, e o novo código deve
**reusá-lo**, não clonar:

| já existe | linha | uso na aba Vozes |
|---|---|---|
| `workletSrc` + `pcm-capture` | [:230-235](../../neo-voice/service/web/index.html#L230-L235) | idem |
| `makeResampler` / `f2i` / `onFrame` | [:237-259](../../neo-voice/service/web/index.html#L237-L259) | idem, `TARGET_SR=16000`, `CHUNK=1600` |
| `startAudio` / `cleanupRec` | [:287-305](../../neo-voice/service/web/index.html#L287-L305) | parametrizar a URL do WS (hoje `/ws/panel` está fixo em `startRec`) |
| medidor de nível (`#meter`) | [:110](../../neo-voice/service/web/index.html#L110) | barra de gravação |
| padrão de abas `data-tab`/`view-` | [:186-192](../../neo-voice/service/web/index.html#L186-L192) | terceira aba |

Refactor mínimo: extrair de `startRec` a escolha da URL do WebSocket
([:275](../../neo-voice/service/web/index.html#L275)) para um parâmetro. O resto entra como
funções novas na mesma página autocontida — sem build, sem dependência externa, como o painel
é hoje.

### 4.5 Dois gotchas da GUI que precisam ser resolvidos antes

**(a) O microfone só funciona em `localhost` — e a decisão é: acessar por túnel SSH.**

O `getUserMedia` exige *secure context*; `http://192.168.4.210:5002` (neo-host,
[[neo-voice-neo-host-deploy]]) **não é** — só `localhost` e `https`. O código já prevê a falha
([:273](../../neo-voice/service/web/index.html#L273): *"use localhost/https"*), e o README
menciona o requisito, mas isso era escrito quando o serviço rodava no Mac. **Hoje, abrir o painel
pelo IP da LAN e clicar em gravar simplesmente não grava.**

**Decisão (2026-08-03): sem TLS, sem flag de navegador. Túnel SSH e pronto.**

```bash
ssh -L 5002:localhost:5002 neo-host      # neo-host (LAN)
# se o 5002 do Mac estiver ocupado por um neo-voice nativo, escolha outra porta local:
ssh -L 5099:localhost:5002 neo-host      # -> http://localhost:5099
```

Abre-se `http://localhost:5002` (ou `:5099`) e o navegador trata como contexto seguro. O
container já publica `5002:5002` no host ([`docker-compose.yml:338-339`](../../docker-compose.yml#L338-L339)),
então o túnel cai direto nele.

**Custo em código: zero.** O painel monta a URL do WebSocket a partir de `location.host`
([:275](../../neo-voice/service/web/index.html#L275)), então, servido pelo túnel, ele já aponta
`ws://localhost:5002/ws/panel` — que volta pelo mesmo túnel. Vale para as portas novas
(`/ws/speaker/enroll`, `/ws/speaker/test`) sem nenhuma configuração: **basta as URLs novas serem
relativas, como as de hoje.** Isso vira requisito da aba Vozes — nada de host fixo no JS.

O que ainda tem que ser feito na UI: **detectar** `window.isSecureContext === false` e mostrar o
comando do túnel na tela, em vez de deixar o botão de gravar falhar em silêncio. É o único
trabalho que este item gera.

Consequências que ficam registradas:

- **O desktop não é afetado.** O Tauri serve a página de uma origem local e trata o
  `ws://192.168.4.210:5002` via ATS no `Info.plist` ([[neo-voice-neo-host-deploy]]) — a captura de
  voz do app continua igual. O túnel é só para o **painel web**.
- **O Caddy/TLS continua pendente**, mas por outro motivo (o `wss://` remoto do desktop na
  [[neo-voice-vps-migration]]). Este plano deixou de depender dele.
- Vale também para a VPS, trocando o host do `ssh -L`.

**(b) O pré-processamento do navegador distorce o enrollment.** A captura de hoje pede
`echoCancellation:true, noiseSuppression:true, autoGainControl:true`
([:288](../../neo-voice/service/web/index.html#L288)). Para STT isso ajuda. Para **embedding de
locutor**, `noiseSuppression` e `autoGainControl` mexem justamente na envoltória espectral e na
dinâmica que carregam a identidade — e, pior que degradar, eles criam **descasamento de canal**
se o enrollment for gravado com um conjunto de flags e o uso real vier com outro
([[neo-desktop-audio-route-aec]]: o AEC do desktop já é condicional à rota de saída).

Decisão: as flags viram explícitas na aba Vozes e o **enrollment grava na mesma configuração do
caminho de produção**. Qual é a melhor combinação é pergunta empírica — entra como uma das
variáveis do harness do F0, junto com mic e distância.

---

## 5. O gotcha específico deste sistema: o Neo se identificando

O Neo fala pelo alto-falante e o mic escuta. O AEC/VPIO é **condicional à rota de saída**
([[neo-desktop-audio-route-aec]]) — quando não está ativo, o TTS entra no mic. Hoje isso é
tratado no texto (`stt_text.py` descarta eco do wake word). Com locutor, vira um problema
novo **e uma oportunidade**:

- **Problema sério:** o Neo pode falar **na voz clonada de uma pessoa real**
  ([[neo-voice-imitation]] — `[[VOICE:nome]]`). Esse áudio, entrando pelo mic, seria
  identificado como **aquela pessoa**. É a rota mais direta para o sistema se auto-enganar,
  e não é hipotética: o recurso está em produção.
- **Oportunidade:** as vozes do TTS são um conjunto **finito e conhecido** (Kokoro `pf_dora`,
  vozes Qwen/Coqui, cada `clone_*`). Dá para enrolar cada uma **sintetizando com ela mesma** e
  registrá-las como perfis da classe `synthetic`. Aí o eco não é adivinhado por heurística de
  texto — é **reconhecido**, com a mesma máquina de tudo o mais.

Mitigação em camadas: (1) perfis `synthetic` para todas as vozes de TTS; (2) o cliente já sabe
quando está tocando áudio (barge-in / [[neo-stop-word-barge-in]]) — enunciado que cai dentro da
janela de playback entra com flag `during_playback=true` e **nunca** adapta perfil nem concede
permissão; (3) `decision: "echo"` como quarto estado, que descarta o turno como o
`fix.dropped` de hoje já faz.

---

## 6. Contrato de dados

### 5.1 Perfis (no volume `neo-voice-state`)

```
/app/state/speakers/
  profiles.json                 # metadados: id, nome, pks_entity_id, class, criado_em, consent
  emb/<id>.npy                  # matriz (N, D) float32 dos embeddings do perfil
  cohort.npy                    # coorte de impostores p/ AS-Norm (embutida na imagem)
  audio/<id>/*.wav              # OPCIONAL, só com consentimento explícito (§8)
```

O volume `neo-voice-state:/app/state` já existe no compose
([`docker-compose.yml:362-364`](../../docker-compose.yml#L362-L364)) e já é onde vivem
`stt_fallback.json`, `stt_lexicon.json`, `tts_hedge.json` — mesmo padrão de `_load_*_state()`.

`class` ∈ `human` | `synthetic` (§5). `pks_entity_id` amarra o perfil à entidade `person` do PKS
(`core/pks/` já tem `type="person"`), que é o que faz "quem falou" virar "o que eu sei sobre
quem falou".

### 5.2 Saída do neo-voice

`/ws/panel`, no JSON de `final` (campos novos, aditivos — nenhum cliente quebra):

```json
{"type": "final", "text": "apaga a luz do quarto",
 "speaker": "daniel", "speaker_id": "spk_01", "speaker_confidence": 0.93,
 "speaker_decision": "accept", "speaker_speech_sec": 1.4, "speaker_session_conf": 0.97}
```

`/ws/audio` (contrato antigo `FINAL_TEXT:`) fica **inalterado** — é drop-in de um cliente legado,
e uma linha nova quebraria o parser. Quem quiser identidade usa `/ws/panel`.

`GET /speaker/status` devolve o estado da sessão e as últimas decisões (o análogo de
`/engines/status`, que é como este projeto torna o invisível auditável).

### 5.3 Propagação até o neo-core

O caminho já existe e é literalmente o mesmo de `input_source`:

1. **Desktop (Rust)** — [`ai_agent.rs:204-213`](../../neo-desktop-tauri/src-tauri/src/services/ai_agent.rs#L204-L213):
   a struct `VoiceInput` ganha `speaker`, `speaker_id`, `speaker_confidence`; entram no
   `metadata` (é `map<string,string>` no gRPC — o mesmo motivo pelo qual `attachments` vai
   serializado).
2. **neo-core** — `core/text/asr_fixups.py` é o precedente do gate por metadata
   (`input_source == "voice"`). Um `core/identity/speaker.py` lê os campos, resolve
   `speaker_id` → entidade `person` do PKS e expõe um `SpeakerContext` ao turno.
3. **Prompt** — o construtor de contexto injeta "quem está falando" (e, se `uncertain`, injeta
   a incerteza, não o palpite como fato).
4. **PKS** — `speaker != daniel` ⇒ o planner de memória **não escreve no PKS do Daniel**. É
   exatamente o desenho do `core/pks/ledger_guard.py`, que já fecha canais para impedir
   contaminação ([[neo-pks-ledger-boundary]]) — mesmo padrão, outro eixo.

---

## 7. Identidade como autorização — com limite explícito

Vale a pena, e é metade do valor do recurso: hoje qualquer voz na sala pode mandar o Neo
desligar a luz, rodar `run_command`, mexer em finanças. Com locutor, dá para restringir.

Mas: **voz não é autenticação forte.** É replicável (o próprio repo tem clonagem de voz que
funciona com 6 s de áudio) e não resiste a gravação/deepfake. A regra que o plano adota:

> A identidade de voz pode **negar**; sozinha, não **libera** ação sensível.

| ação | desconhecido / `uncertain` | pessoa conhecida ≠ Daniel | Daniel (`accept`) |
|---|---|---|---|
| conversar, perguntar, buscar | ✅ | ✅ | ✅ |
| casa (HA), música | ❌ (ou pede confirmação do Daniel) | conforme allowlist por pessoa | ✅ |
| `run_command`, workbench, arquivos | ❌ | ❌ | ✅ |
| finanças, compras | ❌ | ❌ | ✅ + confirmação (voz não basta) |
| escrever no PKS do Daniel | ❌ | ❌ | ✅ |

Interação com o **Modo Birra** ([[neo-modo-birra-plan]]): a birra é sobre "pedido bobo de casa";
a identidade entra como dimensão ortogonal — de quem o Neo aceita pedido de casa, e de quem
aceita desculpa. Um estranho não deveria conseguir encerrar a birra do Daniel.

Toda decisão de negar por identidade vai ao log com o escore, para poder ser auditada e
recalibrada. Negação silenciosa é o pior resultado possível aqui.

---

## 8. Privacidade

Embedding de voz é **dado biométrico** — dado pessoal sensível pela LGPD (art. 5º, II).
Não é formalidade: são vozes de familiares e visitas numa casa.

- **Consentimento explícito por pessoa** no enrollment; `profiles.json` guarda quem e quando.
- **Padrão: guardar embedding, não áudio.** O `.wav` de enrollment só fica se a pessoa marcar
  (útil para re-treinar quando trocar de modelo) e é apagável.
- `DELETE /speaker/profile/{id}` apaga perfil, embeddings e áudio — de verdade, do volume.
- Nada de identificar quem não deu consentimento: sem perfil, a saída é `unknown`, e o áudio
  não é retido.
- Os embeddings ficam no volume do neo-voice (neo-host/LAN ou VPS) e **não** vão para nuvem
  nenhuma — o extrator é local, ao contrário do fallback de STT do OpenRouter.

---

## 9. Fases

| fase | entrega | GUI (§4) | depende de |
|---|---|---|---|
| **F0** | corpus rotulado + harness de medição + escolha do modelo por medição | aviso de contexto inseguro (§4.5a) · captura sombra · **Rotular** | — |
| **F1** | `service/speaker_id.py`: embedding ONNX, store de perfis, scoring + AS-Norm. Offline, com testes | — | F0 |
| **F2** | `Utterance.voiced` + integração **shadow** em `_panel_final`/`_finalize` (`asyncio.gather`), só log | **Testar** (só escores, sem decisão) | F1 |
| **F3** | enrollment: `WS /ws/speaker/enroll`, `POST /speaker/enroll`, `GET/DELETE /speaker/profile` | **Gravar** (3 sessões guiadas, barra de fala efetiva) | F1 |
| **F4** | decisão ao vivo: 3 estados, acumulação por sessão, campos no JSON de `final`, `GET /speaker/status` | **Testar** completo: limiares, sessão, modo cego, `[era outro…]` | F2+F3 |
| **F5** | propagação: `VoiceInput` no Rust → metadata gRPC → `core/identity/speaker.py` → prompt + resolução no PKS | — | F4 |
| **F6** | guardas: PKS por pessoa, matriz de autorização (§7), perfis `synthetic` + supressão de eco (§5) | perfis `synthetic` visíveis no Testar | F5 |
| **F7** | adaptação incremental, recalibração de limiares no corpus acumulado, rollback de perfil | rollback + histórico do perfil | F6 |
| **F8** | *(opcional)* diarização intra-enunciado; verificação texto-dependente na wake word (§11) | — | F7 |

A aba **🧑 Vozes** não é entrega de uma fase só: ela nasce no F0 (rotular), ganha o Testar no F2,
o Gravar no F3 e a decisão completa no F4. É a superfície pela qual o resto do plano é medido.

**F0 é a fase que decide se o resto funciona** e é a que não dá para pular. Ela produz:

0. **Acesso ao painel por túnel SSH** (§4.5a) — decidido, sem trabalho de infra. É o que
   destrava gravação e rotulagem; sem contexto seguro o F0 inteiro fica parado.

1. **Corpus.** (a) bootstrap com os clones e `jarvis-samples/`; (b) enrollment guiado das
   pessoas da casa (§3.2); (c) **captura sombra** de 1–2 semanas: o neo-voice guarda o PCM dos
   finais num diretório temporário (opt-in, com expurgo automático) e o painel oferece
   rotulagem em 2 cliques ("quem falou?"). O corpus real é o ativo — é ele que transforma
   "acho que está bom" em número.
2. **Harness** (`neo-voice/scripts/eval_speaker.py`, ao lado do `eval_stt_fixups.py` que já
   existe): dado o corpus rotulado, produz EER, DET, acurácia closed-set **por faixa de
   duração** (0–1 s, 1–2 s, 2–3 s, 3–5 s, 5 s+) e por canal (mic interno/BT/notch), com e sem
   AS-Norm, para cada modelo candidato. Mede também o tempo de inferência na CPU do container.
3. **Escolha**: modelo + limiares `ACCEPT`/`REJECT`, justificados pela tabela — não pela EER
   de VoxCeleb.

Critério de saída do F0: a curva acurácia × duração está medida e os alvos do §2.1 são
atingíveis com o modelo escolhido. Se não forem, o plano muda **aqui** (modelo maior, mais
enrollment, ou alvos revistos) — e não depois de seis fases construídas em cima.

---

## 10. Arquivos tocados

**neo-voice** (o grosso):
- `service/speaker_id.py` — **novo**: extrator ONNX, store, scoring, AS-Norm, estado de sessão.
- `service/neo_voice_server.py` — `Utterance.voiced`; `asyncio.gather` em `_panel_final`
  ([:1680](../../neo-voice/service/neo_voice_server.py#L1680)) e `_finalize`
  ([:1643](../../neo-voice/service/neo_voice_server.py#L1643)); campos no JSON de `final`;
  endpoints `/speaker/*`; **`WS /ws/speaker/enroll` e `WS /ws/speaker/test`** (§4.1/§4.2);
  `_load_speaker_state()` no padrão dos outros `_load_*_state`.
- `service/web/index.html` — **aba `🧑 Vozes`** (§4): Gravar / Testar / Rotular. Refactor mínimo
  em `startRec` para parametrizar a URL do WS ([:275](../../neo-voice/service/web/index.html#L275));
  o resto reusa worklet, resampler e `startAudio` que já existem.
- `Dockerfile` — baixar o modelo de embedding + a coorte para dentro da imagem e pré-aquecer,
  igual ao Kokoro e ao Silero fazem hoje ([:34-36](../../neo-voice/Dockerfile#L34-L36)).
- `scripts/eval_speaker.py` — **novo**: harness do F0.
- `tests/test_speaker_id.py` — **novo**.
- `README.md` — seção nova.

**neo-desktop-tauri**: `services/ai_agent.rs` (`VoiceInput` + metadata), `commands/mod.rs`
(assinatura), e o hook de voz do React repassando os campos do JSON de `final`.

**neo-core**: `core/identity/speaker.py` (**novo**), gate no construtor de contexto/prompt,
resolução `speaker_id` → `person` no PKS, guardas de escrita e a matriz de autorização do §7.

**neo-panel**: painel de perfis (listar, enrollment, apagar, ver decisões recentes) — o mesmo
lugar onde já moram os toggles de fallback.

---

## 11. Ideias descartadas ou adiadas (e por quê)

- **Rodar na RTX junto com o Parakeet.** Descartado: §1.1. O engine STT já tem o áudio e o
  embedding sairia quase de graça lá, mas a disponibilidade é ruim demais.
- **Fazer o próprio Parakeet devolver locutor.** Não faz isso; é um transducer de ASR.
- **Diarização de verdade (pyannote).** Adiado ao F8: resolve um problema que este sistema
  provavelmente não tem (fala sobreposta num turno dirigido ao Neo) por um custo de CPU e
  complexidade grandes.
- **Verificação texto-dependente na wake word.** *Adiada, mas promissora.* "Ei Neo" é sempre a
  **mesma frase** — verificação texto-dependente é bem mais precisa que texto-independente na
  mesma duração, e o sidecar do wake word já tem esse áudio isolado
  ([[neo-wake-word]]). Seria um segundo sinal, quase de graça, exatamente no ponto em que o
  texto-independente é mais fraco (1 s). Entra no F8 se a curva do F0 mostrar que a faixa
  curta continua ruim.
- **Um centroide por pessoa.** Descartado no desenho: §2.2 item 4.
- **Usar identidade de voz como autenticação forte.** Recusado explicitamente: §7.

---

## 12. Riscos

| risco | mitigação |
|---|---|
| enunciado curto demais (metade dos turnos) | acumulação por sessão (§0/§2.2.5) + estado `uncertain` |
| eco do TTS identificado como pessoa (agravado pela imitação de voz) | perfis `synthetic` + flag `during_playback` (§5) |
| perfil enrolado num mic só não generaliza | enrollment em 3 condições (§3.2) + AS-Norm |
| drift/envenenamento na adaptação incremental | limiares altos, teto de embeddings, rollback (§3.3) |
| identidade errada abre PKS/atuação | assimetria dos limiares (§2.1) + "nega, não libera" (§7) |
| biometria de terceiros na casa | consentimento, só embedding, delete real (§8) |
| CPU do container (vCPU da Contabo é fraca — Kokoro já dá RTF ~0.8 lá) | medir no F0 **na máquina alvo**; concorrência com o STT absorve (§1.2); no neo-host há folga |
| mais um estado no `/ws/panel` que os 3 clientes precisam entender | campos aditivos; `/ws/audio` intocado (§6.2) |
| **mic bloqueado no painel** servido em `http://192.168.4.210:5002` (contexto inseguro) | **resolvido por decisão**: acesso por túnel SSH em `localhost`; UI avisa em vez de falhar calada; URLs de WS relativas (§4.5a) |
| pré-processamento do navegador (NS/AGC) descasa enrollment e uso real | flags explícitas + enrollment na mesma config da produção (§4.5b) |

---

## 13. Critérios de aceitação

1. Os alvos do §2.1, medidos pelo harness do F0 no corpus real — não em VoxCeleb.
2. Latência de wall clock do turno de voz **não piora** (comparação antes/depois no p50/p95).
3. Com o extrator desligado (`NEO_SPK_ENABLED=0`) ou falhando, o turno de voz funciona
   exatamente como hoje.
4. O Neo chama cada pessoa da casa pelo nome sem ninguém dizer quem é, e responde "não sei
   quem é você" a um desconhecido em vez de chutar.
5. Um enunciado de terceiro **não** escreve nada no PKS do Daniel — provado com um teste, não
   por inspeção.
6. O Neo falando na voz clonada de alguém não é identificado como aquela pessoa.
7. `DELETE /speaker/profile/{id}` apaga tudo, e o sistema volta a responder `unknown` para
   aquela voz.
8. **Na aba `🧑 Vozes`**: dá para cadastrar uma pessoa nova do zero — as três sessões, sem tocar
   em terminal — e, logo em seguida, na sub-aba Testar, essa pessoa é reconhecida falando ao vivo,
   com os escores contra todos os perfis visíveis na tela.
9. A aba Testar mostra a decisão **e** a margem. Uma decisão certa com margem apertada tem que
   ser visível como tal — é o que permite recalibrar antes de virar erro em produção.
