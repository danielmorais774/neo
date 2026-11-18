# Avaliações

Toda mudança de comportamento do Neo que dependia de um modelo foi decidida com uma medição. Este arquivo reúne essas medições em três partes:

1. **Comparativos**: modelos, provedores e configurações lado a lado, na mesma tarefa.
2. **Antes e depois**: o mesmo modelo, antes e depois de uma mudança no sistema.
3. **Estudos de caso**: problemas reais de produção, como foram diagnosticados, o que mudou e o número que provou a correção.

**O que estes números são e o que não são.** São medições de engenharia, feitas para decidir uma mudança. As amostras são pequenas (de 4 a 59 casos), e quase sempre o **request real** de produção foi reenviado ao mesmo modelo. Não são benchmark. Cada linha traz o `n`, para o leitor pesar o resultado.

**Regra que vale para todas:** cada tabela compara **uma coisa só**.

---

## 1. Comparativos

### 1.1 Classificador de intenção: três modelos, 30 pedidos reais

O classificador roda em **todo** turno. Por isso, latência de cauda (p90) e obediência ao formato pesam mais que inteligência.

| Modelo | p50 | p90 | máx | Problema encontrado |
|---|---|---|---|---|
| GLM-5.3-flash (com teto de raciocínio) | 3,4 s | 11,7 s | 19,6 s | um dos provedores ignora o teto de raciocínio |
| **MiniMax m3.1-flash, provedor fixo** (escolhido) | **2,5 s** | **4,2 s** | 6,5 s | 1/30 voltou vazio; o código repete a chamada |
| Haiku 5.5 | 2,6 s | 4,9 s | 6,8 s | em 19/30 respondeu ao usuário em vez de classificar |

Corpo completo do request de produção, 14/11/2026. Com o modelo escolhido, o golden set (59 casos) fica em **57/59**. As 2 falhas já falhavam com o prompt anterior, em 3 de 3 repetições.

Antes dessa troca, o classificador em produção tinha **p95 de 31,7 s** em 7 dias (106 turnos): o raciocínio comia o orçamento de tokens, a resposta voltava cortada e a nova tentativa custava outros 26 s.

### 1.2 O mesmo modelo, provedores diferentes

No OpenRouter, um mesmo modelo é servido por vários provedores de infraestrutura. Eles **não são equivalentes**.

| Modelo | Provedor | Velocidade | n |
|---|---|---|---|
| deepseek-v4-flash | roteamento livre (o padrão) | 21,7 tok/s | 4 chamadas |
| deepseek-v4-flash | fixo no provedor A | **74–80 tok/s** | 4 chamadas |
| deepseek-v4-flash | fixo no provedor B | 47–60 tok/s | 4 chamadas |

Mesmo prompt, 11/09/2026. Fixar o provedor também preserva o **cache de prefixo**: 24.576 de 24.723 tokens de entrada voltaram do cache com provedor estável, contra zero trocando a cada rodada.

O preço também varia. A mesma chamada do GLM-5.3-flash, com tokens parecidos, custou **US$ 0,00052 num provedor e US$ 0,00265 em outro (5×)**. A escolha do provedor mexeu mais no custo do que o teto de tokens.

### 1.3 Raciocínio ligado × desligado

No OpenRouter, vários modelos "flash" raciocinam por padrão, antes do primeiro token de texto, e esse raciocínio não respeita o `max_tokens`.

| Tarefa: avaliar o tom de uma mensagem e devolver um JSON de 5 números (qwen3.5-flash) | Latência | Tokens de saída |
|---|---|---|
| Raciocínio no padrão (ligado) | 11,0 s · 15,3 s · **20,3 s** | 2.133 · 2.971 · 4.053 |
| `reasoning: {enabled: false}` | **0,8 s · 0,8 s · 0,9 s** | 57 |

3 rodadas cada, mesmo JSON de saída, 01/08/2026. **~18× mais rápido e ~50× menos tokens.** O código já pedia para desligar o raciocínio, mas o provedor descartava o pedido no caminho do OpenRouter. O conserto está no estudo de caso 4.1.

### 1.4 Quantas ferramentas mostrar ao cérebro

Mostrar só as ferramentas "relevantes" a cada mensagem parece óbvio para economizar tokens. Foi medido antes de implementar.

| Cenário (deepseek-v4-flash, provedor fixo) | Tokens de entrada | 1º token (mediana) | Cache | Custo por rodada |
|---|---|---|---|---|
| 36 ferramentas, sempre as mesmas (como está) | 18.925 | 1,4–3,7 s | 87–99% | US$ 0,00026–0,00039 |
| ~9 ferramentas, **mudando a cada mensagem** | 11.321 | 1,0–1,3 s | **45–69%** | US$ 0,00035–0,00050 |
| ~9 ferramentas, conjunto fixo | 12.059 | 1,4–2,5 s | 84–99% | US$ 0,00016–0,00027 |

Payload real de um turno, 6 amostras por cenário, 3 execuções, 12/09/2026. **Escolher as ferramentas por mensagem quebra o cache e sai mais caro**, mesmo mandando 40% menos tokens, porque token fora do cache custa ~10×. E o ganho de latência é de 0,3–0,5 s por rodada, ~2–4% de um turno em que 97% do tempo é geração de saída. Ficou como está.

### 1.5 Quem vê a tela decide o passo?

No navegador, o modelo de visão recebia um print a cada passo e, com ele, **decidia** a ação.

| Desenho | O que aconteceu |
|---|---|
| Modelo de visão decide todo passo (gemini-3.1-flash-lite) | decidiu 36 de 37 passos, **33% das decisões recusadas** por formato, e o `done` saiu **sem a resposta em 10 de 10** tentativas de terminar, com a descrição da tela correta. **3 de 3 tarefas morreram** |
| Modelo de texto decide; visão só responde a uma pergunta (`look`) | a mesma tarefa fecha com **1 imagem**, onde antes gastava uma por passo, e o `look` sai no momento de conferir o que foi preenchido |

Ver bem a tela e obedecer ao formato de ação são habilidades diferentes. O modelo enxergava e não obedecia. Os papéis foram separados (02/09/2026).

### 1.6 Juiz de formulário: Haiku × Sonnet

Mesma página real de avaliação, 24 respostas, 2 rodadas por modelo.

| Modelo | Concorda consigo mesmo entre as rodadas | Respostas "Conforme" |
|---|---|---|
| Haiku 5.5 | 20/24 | 15 e 19 (mais leniente) |
| Sonnet 5.5 | 18/24 | 8 e 9 |

12/11/2026. Os dois derrubaram o "CONFIRMADO" falso do agente, que tinha marcado 70 de 70 itens como "Conforme" sem olhar. Ficou o Haiku, pelo custo.

### 1.7 Voz: motores de TTS na mesma GPU (RTX 5070 Ti)

RTF = tempo de síntese ÷ duração do áudio. Acima de 1, o motor é mais lento que a própria fala e não sustenta streaming.

| Motor | RTF | Observação |
|---|---|---|
| Coqui XTTS-v2 | ~0,14 | lia o ponto final em voz alta ("beleza ponto") em 6/6 frases curtas; corrigido fatiando as sentenças e emendando silêncio de verdade |
| F5-TTS | ~0,2 | |
| CosyVoice 3 | 0,46–0,58 | em português, às vezes estica a duração |
| Chatterbox | ~0,57 | |
| Qwen3-TTS 0.6B | **1,89** | mais lento que tempo real; para tocar sem buracos, precisa de pré-buffer, e o 1º som sai em ~2× a duração da resposta |

Coqui, Chatterbox e Qwen foram medidos por regressão linear sobre 4 tamanhos de texto (12/07/2026). F5 e CosyVoice foram medidos em testes separados.

### 1.8 Voz: STT

| Opção | Resultado |
|---|---|
| Parakeet TDT 0.6B v3, int8, CPU (8 threads) | **RTF ~0,05** (2 s de fala em 170 ms, 66 s em 3,8 s); pico de 2,1 GB de RAM |
| Parakeet fp32 | devolve **texto vazio** em falas acima de ~16 s, de forma determinística, na CPU e na GPU (é o grafo do modelo, não o hardware) |
| Vosk (testado para transcrever vídeo no sandbox) | 32% de erro em fala lida, 69% em fala espontânea; descartado |
| Whisper via Groq (escolhido para vídeo) | vídeo de 39 s transcrito em 2,4 s dentro do container de produção |

Detalhe que só a medição mostrou: nessa CPU sem VNNI, o int8 é ~15% **mais lento** que o fp32. O int8 ficou porque o fp32 quebra com áudio longo.

### 1.9 Wake word ("ei Neo"), modelo treinado com openWakeWord

| Versão | Recall a 0,5 | Falsos positivos |
|---|---|---|
| v1, só voz sintética (TTS) | 0,9+ no TTS, **0,001 na voz real** | |
| v2, com gravações de voz real injetadas no treino | 50% | 0% |
| v3, peso dos negativos relaxado | **83%** | 28/1086 negativos acima de 0,5 (palavras parecidas: "né o", "-âneo") |

Lição: palavra inventada **precisa de voz real** no treino, porque o TTS sozinho não cobre a distância até a voz de verdade.

---

## 2. Antes e depois (mesmo modelo, uma mudança)

| Mudança | Antes | Depois | n e método | Data |
|---|---|---|---|---|
| Prompt do classificador sem repetição | 29.972 caracteres por mensagem, 43/46 | **18.720 (−37%), 43/46**, zero decisão diferente | golden set de 46 casos, A/B | 01/08/2026 |
| Classificador com JSON truncado pelo raciocínio (deepseek-v4-flash) | 13/46 caíam no fallback por palavra-chave | **44/46, zero truncagens** | mesmo golden set | 01/08/2026 |
| Avaliador de tom com o raciocínio desligado de verdade | 11–20 s (encostando no timeout de 25 s) | **1,0–2,6 s** | caminho real, em produção | 01/08/2026 |
| Turno do cérebro: stream por etapa, sem raciocínio, provedor fixo | 71,5 s; 1º token em 13–40 s | **28,0 s (−61%); 1º token em 2,3–3,4 s** | replay dos mesmos 6 rounds de um turno real | 11/09/2026 |
| Navegador: orçamento de passos pelo que entrou no caderno, não teto fixo | 27 passos, tarefa `failed` com a resposta pronta desde o passo 2 | **13 passos**, entregue; a tarefa longa que dava certo continua dando (17 passos) | replay de 2 tarefas reais | 30/09/2026 |
| Navegador lendo 3 anúncios: abas em `paralelo` | 21 passos, 71 s | **5 passos, 39 s**, mesmo veredito | 2 tarefas reais equivalentes | 02/11/2026 |
| Navegador: fechar aba passa a exigir o motivo | reabriu a aba já fechada em 6/6 | **0/6** | passo real reencenado | 02/11/2026 |
| Navegador: dica de "abas ainda não lidas" no bloco das abas | escolheu ler as abas em 1–2/6 | **5/6** | idem | 02/11/2026 |
| Navegador: consolidar depois do `paralelo` | reescreveu as 3 notas em 4/6 | **0/6**, conclusão em 6/6 | idem | 02/11/2026 |
| Raciocínio cortando o passo do navegador (GLM-5.3-flash): teto na fonte | ~10 s por corte; 28% do custo do job sem entregar nada | **1–67 tokens de raciocínio, ~2 s** | 6 reenvios do payload real, 5 provedores | 08/11/2026 |
| Última rodada do cérebro sem ferramentas passa a ser avisada | resposta final vazia | **3/3 respostas honestas** ("travei, a planilha não saiu") | reenvio do texto exato | 14/11/2026 |
| Aviso no resultado quando um download de URL inventada falha | 0/5 vão buscar a URL certa | **5/5** chamam `web_search` | A/B com o request real e as 68 ferramentas | 14/11/2026 |
| Descrição da ferramenta que delega o formulário | delegou 0/16 | **10/16** | A/B com o request real | 02/11/2026 |
| A mesma regra em outra posição: no prompt de sistema × logo depois da mensagem | 2–3/8 | **8/8** | A/B, mesma frase | 31/10/2026 |
| Recusa causada pela "postura" emocional num pedido de ação | recusou 3, 8 e 11 vezes (turnos 3, 4 e 5) | **0, 0 e 0**; despachou a ação 11 e 9 vezes | replay do request real, 12 amostras por turno | 01/11/2026 |
| Ranking da memória (PKS) v2, em modo sombra | recall 0,854, completude 0,879, nDCG 0,58 | **recall 0,976, completude 1,0, nDCG 0,84**, p95 de ~15 ms | gabarito de 50 turnos reais | 09/2026 |
| Gatilhos de "você me falou…" para buscar na conversa | 0/9 frases do usuário casavam | **9/9**, disparando em 2,9% das 2.053 mensagens reais | corpus de produção | 19/09/2026 |
| Guarda de "toca <música>" no roteador | `coloca/bota/põe` disparavam em pedidos que não eram música | **12/12 acertos, 0 falso positivo** | 1.865 mensagens reais | 07/09/2026 |
| Portão de mídia do roteador (vídeo com fala × gravação de tela) | | **0 falso positivo**, 28 casos na fatia certa | 2.820 mensagens reais | 16/11/2026 |
| TTS em streaming: rampa adaptativa pelo RTF medido | buraco de 6,4 s no meio da fala | **0 ms** | teste que simula o tocador | 12/07/2026 |
| Leitura delegada de PDFs longos (subagentes leem fora do contexto do cérebro) | a 1ª resposta nem abriu os PDFs: buscou na web | **165 páginas em 28 s**, 24 chamadas | tarefa real | 16/09/2026 |

---

## 3. Estudos de caso

Cada caso começou com um sintoma em produção. Todos seguem o mesmo roteiro: sintoma, o que estava acontecendo de verdade, o que mudou e o número que provou a correção.

### 3.1 O modelo "flash" que pensava por 20 segundos

**Sintoma.** O painel mostrava "avaliador degradado": a avaliação do tom de cada mensagem estourava o timeout de 25 s.

**Diagnóstico.** A API do OpenRouter devolve, por chamada, o provedor, a latência e os tokens de raciocínio. Isso mostrou 2.000 a 4.000 tokens de raciocínio invisível para produzir um JSON de 160 caracteres. O código mandava "sem raciocínio", mas o provedor do OpenRouter herdava um método que **descartava** esse pedido. O classificador de intenção pagava o mesmo custo em todo turno, desde sempre.

**Correção.** O pedido virou o parâmetro que o OpenRouter entende (`reasoning.enabled=false`), com teste.

**Número.** 11–20 s → 0,8 s por chamada (~18×), e ~50× menos tokens.

### 3.2 Resposta vazia não é "nada a dizer"

**Sintoma.** Seções inteiras sumiam de relatórios de pesquisa, sem nenhum erro no log. O chat respondia "não consegui gerar uma resposta" a uma pergunta simples.

**Diagnóstico.** Modelos que raciocinam cobram o raciocínio do mesmo `max_tokens`. Quando o teto acaba durante o raciocínio, a API devolve `finish_reason=length` com **conteúdo vazio**: 1 em 4 chamadas, com teto de 4.000. O código tratava vazio como "o modelo não tinha o que dizer". Nenhum teste com dublê pegava isso, porque um dublê nunca devolve vazio.

**Correção.**
- Vazio ou cortado agora é **orçamento curto**: repete a chamada com o dobro de tokens, até um teto.
- No navegador, o teto passou a ser **aprendido por modelo**, com a conta do timeout do TCP (média móvel + 4 desvios, RFC 6298).
- A mensagem de recusa passou a culpar o raciocínio quando é ele que corta. Antes, ela mandava o modelo "encurtar a resposta", e ele obedecia ao diagnóstico errado.

**Número.** Classificador: 13/46 → 44/46. Navegador: cortes de ~10 s viraram passos de ~2 s.

### 3.3 Uma API que aceita o parâmetro e ignora

**Sintoma.** Ferramentas obrigatórias (`tool_choice="required"`) quebravam o turno com erro 400 no DeepSeek. Pelo OpenRouter, em outra sessão, simplesmente não aconteciam.

**Diagnóstico.**
- A API nativa rejeita "ferramenta obrigatória" com o modo de raciocínio ligado. **O parâmetro que destrava foi achado sondando a API**, testando 7 variações, e não lendo a documentação.
- Pelo OpenRouter a falha era pior, porque silenciosa: o parâmetro era aceito e ignorado. Foram **9 interações com ferramenta obrigatória e zero chamadas de ferramenta**.
- Com provedor fixo, a mesma incompatibilidade aparecia como "404, nenhum provedor encontrado".

**Correção.**
- O raciocínio é desligado só quando a ferramenta é obrigatória. Os outros 99% dos turnos continuam pensando.
- Um 400 que fala de `tool_choice` refaz a chamada uma vez com `auto`.
- Falha de provedor nunca vira "a fala do Neo" no histórico.

**Lição.** "Required" é uma promessa do provedor, não do modelo. Por isso a garantia de que a ação aconteceu passou a ser conferida em código.

### 3.4 O navegador que lia a mesma coisa cinco vezes

**Sintoma.** Uma tarefa achou a resposta **no passo 2, aos 5 s**, e morreu no teto de 25 passos, 115 s depois, entregando cinco cópias do menu de atalhos do site.

**Diagnóstico.**
- A leitura sem alvo mirava o corpo da página em ordem de documento: os primeiros 1.500 caracteres eram sempre links de acessibilidade e atalhos.
- A trava contra repetição era chaveada pela tela, e uma rolagem já fazia a tela parecer nova.
- O teto fixo de passos errava para os dois lados: matava tarefa boa e deixava tarefa perdida girar.

**Correção.**
- A leitura usa a mesma região que o snapshot elegeu como conteúdo principal: uma escolha só, não uma reimplementação.
- A trava passou a ser por **leitura**, e não por tela.
- O orçamento passou a ser **por produção**: cada nota nova no caderno devolve um passo, até o dobro do orçamento base, e 12 passos sem nada novo encerram a tarefa.
- Os limiares foram escolhidos para que as duas tarefas reais terminem certo: com 10, a tarefa boa era cortada.

**Número.** 27 → 13 passos com a resposta entregue. A tarefa longa que já dava certo continuou dando certo, em 17 passos.

### 3.5 A recusa que se alimentava sozinha

**Sintoma.** O Neo passou uma sessão inteira recusando um pedido trivial de "lembra disso". A cada insistência, recusava com mais firmeza.

**Diagnóstico.**
- O sistema afetivo do Neo, que muda o tom das respostas, contabilizava o próprio pedido do usuário como "obstinação". Cada recusa aumentava a irritação, e a irritação justificava a próxima recusa.
- A linha de "postura" no prompt dizia "não entregue o ponto central", e a regra logo acima dizia "sempre entregue o que foi pedido". A segunda perdia.
- Depois do conserto, o laço tinha uma segunda metade: a recuperação de conversas antigas trazia as recusas anteriores como contexto.

**Correção.** Em pedido de ação, o afeto governa o tom e não decide se a ação acontece. A postura renderiza outro texto, e a pressão recente sai do prompt.

**Número.** Replay do request real, 12 amostras por turno: recusas 3 → 0, 8 → 0 e 11 → 0. A ação foi despachada 11 e 9 vezes.

### 3.6 A conversa que esquecia a própria resposta

**Sintoma.** O Neo escreveu uma análise de 3.982 tokens e, 13 turnos depois, jurava que ela não existia.

**Diagnóstico.** Cinco travas independentes:
- a nota de relevância nunca passava do corte (0,2131 contra 0,25): **295 de 295 trechos descartados**, para todo mundo e sempre;
- 94% da nota era recência;
- um teto fixo de 3.000 tokens em que a mensagem não cabia;
- a busca excluía a conversa atual;
- os embeddings estavam salvos num formato que o índice não lia.

**Correção.** Cada trava foi corrigida e testada. A migração dos embeddings preservou 90 de 90 vetores, com erro de 7×10⁻⁹. Os gatilhos de "você me falou…" passaram a ser testados contra as 2.053 mensagens reais.

**Número.** Os gatilhos foram de 0/9 para 9/9, disparando em 2,9% das mensagens.

### 3.7 A otimização que sairia mais cara

**Hipótese.** As 36 ferramentas ocupam ~9.400 tokens, metade da entrada. Mostrar só as relevantes deixaria tudo mais rápido e barato.

**Medição antes de implementar** (tabela 1.4). Trocar o conjunto a cada mensagem derruba o cache de prefixo de ~99% para 45–69%, e token fora do cache custa ~10×. O ganho de latência é de ~2–4% do turno.

**Decisão.** Não implementar o filtro dinâmico. O risco real dele nem era latência: era funcionalidade sumindo em silêncio, porque ferramenta que o modelo não vê nunca é chamada.

### 3.8 Guardas que leem a resposta do modelo, medidas nas mensagens reais

**Contexto.** O cérebro tem guardas em código que leem a prosa do modelo: dizer que fez sem ter feito, prometer "vou fazer" e encerrar, dizer que um arquivo está disponível para baixar sem estar. Mudar uma guarda dessas no olho é chute.

**Método.** O predicado novo e o antigo rodam sobre todas as mensagens reais. Olha-se os dois deltas: o que a guarda nova deixou de pegar tem que ser só falso positivo, e o que passou a pegar tem que ser caso real.

**O que isso achou.** A guarda de "download" antiga disparava em 20 mensagens: ~7 eram falso positivo e **11 alegações reais passavam batido**. Era ruim nos dois eixos. O pior falso positivo derrubava o turno: a palavra "download" dentro do caminho de uma imagem entregue certo.

**Correção.** Alegação passou a ser **afordância ∧ âncora**, avaliada por frase, mascarando antes os caminhos, URLs e nomes de arquivo. A mesma técnica, aplicada ao roteador de música, deu 12/12 com 0 falso positivo.

**Lição registrada.** A máscara é a parte perigosa: uma versão que aceitava `/` solto como início de caminho engolia a frase inteira, e a guarda parava de guardar **em silêncio**.

---

## 4. Como reproduzir

| Medição | Onde |
|---|---|
| Classificador (golden set) | `neo-core/scripts/eval_router.py` + `neo-core/tests/router/golden_intents.yaml`. O `--ab` compara duas versões do prompt. Um run em que o provedor falhou é marcado **inválido**, porque o fallback por palavra-chave "concordaria" com tudo |
| Portão de mídia do roteador | `neo-core/scripts/eval_midia_gate.py` |
| Ranking da memória (PKS) | `neo-core/scripts/pks_ranking_bench.py`. O gabarito é feito de conversas reais e fica fora do repositório |
| Recuperação de conversa | `neo-core/scripts/eval_conversation_recall.py` |
| Orçamento de passos do navegador | `neo-core/scripts/replay_sem_ganho.py` (replay sobre os diários das tarefas) e `neo-core/tests/browser_local/test_orcamento_por_producao.py` |
| JS do snapshot do navegador | `neo-desktop-tauri/scripts/snapshot-lab/`: roda o JS de verdade num Chromium contra um corpus de páginas, sem queimar uma tarefa |
| TTS em streaming | `neo-voice/service/ws_tts_test.py`: simula o tocador e imprime o buraco por trecho |

**O método do A/B:** extrair do diário da sessão o payload exato que foi ao modelo, aplicar a mudança e reenviar ao mesmo modelo e provedor, de 5 a 16 vezes por braço. Custa centavos e mede a mudança com o contexto real, não com um exemplo inventado.

## 5. Próximas suítes

| Suíte | O quê | Métricas |
|---|---|---|
| Classificador, vários modelos | o golden set de 59 casos × 5–6 modelos × 3 repetições | acerto, p50/p90, custo por 1.000 classificações, % no fallback |
| Navegação | 6 a 8 tarefas fixas num site de demonstração com resposta conhecida, × 4–5 modelos de driver × 3 repetições | sucesso conferido por código, passos, tempo, tokens, custo |
| Ponta a ponta | GAIA nível 1 (`neo-core/scripts/eval_gaia.py`) | acerto, "errou confiante" × "não sei", custo por pergunta |
