# Visão de confirmação no navegador local — plano

> Status (17/11/2026): F0 medido (§7), print rápido no desktop (§7.6), tempos no
> diário (§7.7) e F1 — gatilhos, só print, atrás do booleano (§9). Pedido do Daniel depois da
> sessão 74d069b7: "tirar mais prints e usar eles para decisão durante o fluxo —
> antes e depois de cada campo editado e antes de enviar; intensificar em modal e
> edição de perfil; e na navegação entre páginas".

## 1. Onde estamos

O agente do navegador decide pela DOM (snapshot em texto). A imagem só entra em
três momentos:

| Quando | Quem decide | Custo |
|---|---|---|
| `look` (o modelo pede) | o modelo de texto | print + visão, **síncrono** |
| olhada automática | o código, quando 2 ações seguidas não mudam a tela (`_AUTO_LOOK_AFTER_STUCK`) | idem, 1 por episódio |
| triagem de painel | o código, quando abre um painel desconhecido (`_MAX_TRIAGENS` = 3) | print recortado + visão |
| print do fim | sempre | só print (vai para o chat) |

Medido nos diários de produção (17/11):

| | n | p50 | p90 | máx |
|---|---|---|---|---|
| `look` (print + visão `z-ai/glm-5.3-flash`) | 32 | **7,5 s** | 10,0 s | 17,2 s |
| decisão do passo (`deepseek-v4.1-flash`) | 804 | 2,1 s | 4,4 s | — |

~~Consequência: o print é barato, a análise é cara.~~ **Errado — a medição do
F0 (§7) mostrou o contrário:** dos 7,5 s do `look`, ~2,8 s são a visão e ~4,9 s
são o PRINT, quase todo gasto em redimensionar a imagem no Rust compilado sem
otimização (`tauri dev`). O plano abaixo continua valendo; o F1 ganha um
pré-requisito (§7.4).

## 2. Princípios

1. **Quem decide quando olhar é o CÓDIGO**, não o modelo
   ([[feedback-decisao-em-codigo-nao-no-llm]]): gatilhos fixos, iguais em todo job.
2. **Print ≠ análise.** Todo gatilho tira print. A análise é síncrona só no portão
   de envio, e nos outros casos é assíncrona (roda enquanto o modelo decide o
   próximo passo, e o resultado entra no prompt seguinte).
3. **Um print serve dois momentos.** O "depois" do campo N é o "antes" do campo
   N+1 quando a tela não mudou no meio (mesmo fingerprint): não se tira outro.
4. **Pergunta fechada, resposta curta.** A visão não descreve a tela; ela
   CONFERE o que o código sabe (o valor digitado, o rótulo do campo, o botão que
   vai ser clicado) e responde em JSON curto (`max_tokens` ~150). Mais rápido,
   e não inventa.
5. **A DOM continua valendo para estado** (marcado, desabilitado, valor),
   como já faz `_com_estado_do_dom`. A imagem vale para o que a DOM não vê:
   erro em vermelho, campo coberto, modal na frente, recorte de foto, texto
   cortado, layout quebrado.
6. **Constantes no `neo.toml`** ([[feedback-constantes-no-neo-toml]]): seção
   `[navegador.visao]`, liga/desliga por gatilho, orçamento por job.

## 3. Gatilhos (o que dispara print, e que análise)

| # | Gatilho (detectado no código) | Print | Análise | Espera? |
|---|---|---|---|---|
| G1 | **Antes do 1º campo** de um formulário (1ª ação `fill`/`select`/`upload`/clique em caixa ou rádio numa tela) | tela inteira | "linha de base": campos visíveis, erros já presentes, o que cobre | não |
| G2 | **Depois de cada campo editado** (`fill`, `select`, `upload`, caixa/rádio) | **recortado** no campo + rótulo + ~120 px abaixo (onde aparece erro) | "o campo «X» mostra «valor»? há erro/aviso perto? está coberto?" | não (resultado no passo seguinte) |
| G3 | **Antes de enviar** (clique que casa `_ENVIO`: Salvar, Enviar, Concluir…) | tela inteira (+ rolagem se o form passa da dobra) | confere CADA campo de `_preenchidos` da aba contra a imagem + erros visíveis + botão habilitado | **sim** — é o portão |
| G4 | **Depois de enviar** | tela inteira | "salvou? aparece sucesso, erro, ou o form continua aberto?" | não |
| G5 | **Modal/painel abriu** (`_note_panels` já detecta) | recortado no painel | "o que é este painel, quais campos e botões, e o que está por trás" | não |
| G6 | **Modo edição** (painel com campo editável + Salvar/Cancelar — ex.: modais do GeekHunter) | intensifica: G2 vira **com espera** a cada 3 campos e G3 sempre | idem G2/G3 | parcial |
| G7 | **Navegação** (URL mudou, ou `title` + fingerprint mudaram muito) | tela inteira | "que página é esta? login, erro, captcha, popup na frente?" | não |
| G8 | **Upload** (já existe a ação) | tela inteira | "a prévia/recorte da foto apareceu? há passo de recorte/confirmar?" | não |

A olhada automática por travamento e o `look` do modelo continuam como estão.

### 3.1 Como a análise vira decisão

- O resultado entra no prompt do passo seguinte num bloco curto
  **`CONFERÊNCIA VISUAL`**, uma linha por gatilho:
  `✓ «Empresa» mostra "Acme"` · `⚠ «Até»: aparece "Campo obrigatório" em vermelho`.
- **Divergência vira aviso no histórico do passo que a causou** (é onde o modelo
  olha): `fill [5] → ok · ⚠ no print o campo mostra "Desenvolvedor Ful" (cortado?)`.
- **G3 com divergência BLOQUEIA o envio** antes de tocar na página (mesmo padrão
  das outras recusas: diz o que está errado e a saída). Sem divergência, o print
  do G3 vai junto da conferência do Daniel (a de antes do Salvar) e entra entre
  os prints de entrega.
- Visão fora do ar ou lenta além do teto: o portão **não trava a tarefa** — segue
  pela DOM e o diário registra "conferência visual não veio".

## 4. Orçamento e custo

`[navegador.visao]` (valores iniciais, a medir no F0):

```toml
[navegador.visao]
confirmacao_visual = false    # o booleano do Daniel (§8): liga a visão de confirmação
portao_envio = true           # G3 separado (§8, recomendação provisória)
analises_max = 40             # análises por job (o print não conta)
paralelas_max = 2             # análises assíncronas em voo ao mesmo tempo
portao_envio_espera_s = 20    # teto da espera do G3; passou, segue pela DOM
recorte_margem_px = 120       # área abaixo do campo no G2 (mensagem de erro)
modo_edicao_espera_cada = 3   # G6: a cada N campos, espera a conferência
navegacao = true
```

Estimativa para o job 509c4f7a (49 passos, 2 experiências, ~14 campos, 2 envios):
~14 G2 (recortados) + 2 G1 + 2 G3 + 2 G4 + ~4 G5 + ~6 G7 ≈ 30 análises, das quais
só os 2 G3 esperam (~2 × 8 s). Tempo extra ≈ **15–25 s** no job, contra ~150 s
do desenho síncrono. Custo em dinheiro: medir no F0 por `/generation?id=`
([[glm-5-3-flash-reasoning-mandatory-browser-cut]]).

Disco: o diário guarda os prints. ~30 × 150 KB ≈ 4,5 MB por job no VPS. O
recortado do G2 sai pequeno (~20–40 KB); a faxina do diário entra no F5
([[neo-workspace-sem-limpeza-e-disco-vps]]).

## 5. Fases

**F0 — medir (sem mudar comportamento).** No lab (Edge headless, mesmo JS do
Rust) e num job real em sombra:
- quanto custa o print (tela inteira e recortado) no device, pela extensão e
  headless;
- latência e preço da visão com pergunta fechada + JSON curto, recortado vs
  inteiro;
- quantos gatilhos cada um dos 3 jobs GeekHunter (b2f541b5, 136453ab, 509c4f7a)
  teria disparado. Sai daqui o `analises_max` e se o G2 cabe síncrono.

**F1 — gatilhos em código, só print.** `core/browser_local/visao.py`:
detecção de G1–G8 a partir do que o driver já sabe (`_preenchidos`, `_ENVIO`,
`_note_panels`, URL/fingerprint), deduplicação por fingerprint, orçamento. O
diário mostra cada print com o gatilho. Nenhuma análise ainda.

**F2 — análise assíncrona + bloco `CONFERÊNCIA VISUAL`.** Fila de análises
(`asyncio`, `paralelas_max`), perguntas fechadas por gatilho, JSON validado,
cruzamento com a DOM. Resultado no prompt seguinte e aviso de divergência no
histórico.

**F3 — portão de envio (G3).** Síncrono, com teto; bloqueia envio com
divergência; o print acompanha a conferência do Daniel. Espera as análises G2
ainda em voo antes de decidir.

**F4 — modo edição (G6) e upload (G8).** Detecção de "painel de edição"
(campo editável + Salvar/Cancelar no mesmo painel) e a cadência mais densa.

**F5 — entrega e faxina.** Quais prints vão para o chat (o do G3 e o do fim),
miniaturas no diário, retenção.

**F6 — E2E.** Repetir a edição de experiência no GeekHunter (o caso do checkbox) e
um Google Forms com campo obrigatório deixado vazio de propósito: o G3 tem que
barrar o envio pela mensagem vermelha.

## 6. Riscos

- **Visão errando estado** (já aconteceu, job 21c563c1): a DOM vence para
  marcado/desabilitado/valor; a imagem só acrescenta.
- **Página mudando entre o print e a análise** (assíncrona): a análise carrega o
  fingerprint do print; se a tela já mudou, o resultado entra como "da tela
  anterior", nunca como estado atual.
- **Dado pessoal no print** (CPF/telefone digitados): o print já ia para o
  modelo de visão no `look`; não muda a superfície, mas o G3 com campos «pks:*»
  passa a mandar a imagem com o valor real. Avaliar se o recorte desses campos
  deve ser evitado.
- **Painel do Neo no print**: ele já é escondido na hora da foto; conferir no F0
  que continua assim com print a cada campo (piscar na tela dele).

## 7. F0 — medição (17/11/2026)

Nada mudou em produção. Scripts e imagens no scratchpad da sessão
(`scripts/print_2x.py`, `scripts/medir_visao.py`, `medir/`).

### 7.1 Onde vão os 7,5 s do `look` (32 olhadas dos diários de produção)

O diário registra o tempo da visão (`👁 look · modelo · Nms`) e o do passo
inteiro; a diferença é o print (+ o que mais o `look` faz, desprezível).

| modelo de visão | n | visão p50 / p90 | **print** p50 / p90 / mín |
|---|---|---|---|
| `z-ai/glm-5.3-flash` | 24 | 2,8 s / 5,7 s | **4,9 s / 6,1 s / 3,4 s** |
| `qwen/qwen3.5-flash` | 8 | 2,6 s / 4,4 s | 2,6 s / 5,6 s / 2,3 s |

### 7.2 Onde vão os ~5 s do print

Tela Retina real (Edge headless, DPR 2, 3008×1552, 370 KB):

| etapa | debug (`tauri dev`, como em prod) | deps otimizadas | **tudo otimizado** |
|---|---|---|---|
| `Page.captureScreenshot` (Chrome) | 0,1–0,4 s | — | — |
| decodificar JPEG | 0,44 s | 0,12 s | 0,014 s |
| redimensionar 0,75 (`resize` Triangle) | **3,83 s** | 3,13 s | 0,079 s |
| recodificar JPEG | 1,23 s | 0,60 s | 0,047 s |
| `draw_marks` inteiro | **4,9–5,5 s** | 3,1–3,7 s | **0,14 s** |

`resize`/`thumbnail` são genéricos: são compilados DENTRO da crate do desktop,
que no `tauri dev` roda sem otimização — por isso otimizar só as dependências
quase não ajuda. Otimizado, o print inteiro cai de ~5 s para **~0,3–0,5 s**.

### 7.3 A análise (visão) com pergunta fechada

Print real do job 509c4f7a (formulário de experiência). 3 chamadas por
variante, custo pelo `/generation` do OpenRouter:

| variante | p50 | custo/chamada | resposta |
|---|---|---|---|
| A — glm, tela inteira, pergunta aberta (como hoje) | 3,2 s | US$ 0,0002–0,0008 | certa, prosa |
| B — glm, tela inteira, **JSON fechado**, raciocínio ≤ 200 | **2,4 s** | US$ 0,0005–0,0007 | certa, JSON |
| C — glm, **recorte do campo**, JSON fechado | **1,7 s** | **US$ 0,00007** | certa, JSON |
| D — gemini-3.1-flash-lite, tela inteira, JSON | 12,4 s | US$ 0,00045 | certa |
| E — gemini-3.1-flash-lite, recorte, JSON | 1,9 s | US$ 0,00034 | certa |

As 5 acertaram (Até = Novembro/2026, caixa desmarcada). O `glm-5.3-flash`
segue como modelo; o recorte é ~10× mais barato e ~30% mais rápido.

### 7.4 Quantos gatilhos os jobs do GeekHunter teriam

| job | passos | G2 campos | G3 envios | análises ≈ | esperando (G3) |
|---|---|---|---|---|---|
| b2f541b5 (perfil inteiro, 98c8) | 17 | 3 | 0 | 4 | 0 |
| 136453ab (headline, 089e) | 30 | 1 | 3 | 8 | 3 |
| 509c4f7a (experiência, 74d0) | 48 | 20 | 2 | 25 | 2 |
| ccfee96e (headline/links/exp, 74d0) | 115 | 17 | 17 | 52 | 17 |

Contagem aproximada (G1 por URL, painéis não contados: o GeekHunter edita na
mesma URL).

### 7.5 O que muda no plano

1. **Pré-requisito do F1: print rápido no device.** Sem isso cada gatilho custa
   ~5 s *no device* (síncrono, a aba fica parada) e o job da experiência ganharia
   ~25 × 5 s ≈ 2 min. Caminho: mover `draw_marks`/`crop_jpeg` para uma crate
   pequena do workspace (`neo-imagem`, funções não genéricas) e
   `[profile.dev.package."*"] opt-level = 3` no `Cargo.toml` — o `tauri dev`
   segue rápido de recompilar, e a imagem passa a ~0,14 s. Isso também deixa o
   `look` de hoje ~4,5 s mais rápido.
2. **Prints de confirmação sem marcações.** A visão de confirmação confere
   valor, não aponta número: dispensa `draw_marks` (o print vai cru ou recortado).
3. **Tempo extra estimado** (com o item 1): G2/G5/G7 assíncronos (análise ~1,7 s
   cabe dentro da decisão do passo seguinte, p50 2,1 s); G3 síncrono ≈ 0,4 s de
   print + 2,4 s de visão ≈ **3 s por envio**. 509c4f7a: +~6 s; ccfee96e: +~50 s.
4. **Custo em dinheiro**: ~US$ 0,01 por job grande (52 análises, maioria
   recortada). Desprezível.

## 8. O booleano: o Daniel escolhe o caminho

`[navegador.visao] confirmacao_visual = true | false` no `neo.toml` (lido a
cada consulta pelo mtime, como o resto do `ajustes`). Desligado, o agente segue
como hoje: decide pela DOM, `look` quando ele pede, olhada automática quando
trava, print do fim.

**Onde a verificação mora — a decidir depois do F1** (o Daniel pediu para medir
antes). As opções:

| ponto | desligado significa | prós | contras |
|---|---|---|---|
| (a) na DETECÇÃO dos gatilhos (`visao.py`) | nenhum print a mais, nenhuma análise | zero custo quando desligado; um `if` só | o diário perde os prints de auditoria |
| (b) só na ANÁLISE | prints continuam (diário + entrega), sem visão | dá para comparar depois o que a visão teria pego | ainda paga o print no device |
| (c) por GATILHO (`g2_campos`, `g3_envio`, `g7_navegacao`…) | escolhe o que liga | ajuste fino (ex.: só o portão de envio) | mais chaves para entender |

Recomendação provisória: **(a) com um segundo booleano para o portão de envio**
(`portao_envio`), porque o G3 é o único que bloqueia ação e é o que mais compra
segurança por segundo gasto. Fica para depois do F1.

O booleano também pode vir por tarefa no futuro ("faz com prints" no pedido),
mas a decisão de ligar fica no código, nunca no modelo
([[feedback-decisao-em-codigo-nao-no-llm]]).

### 7.6 Pré-requisito feito (17/11, ~12:29 UTC): `neo-imagem`

`draw_marks` e `crop_jpeg` moraram para `neo-desktop-tauri/src-tauri/crates/neo-imagem`
(interface sem genéricos: `&[u8]` + `Marca` → `Vec<u8>`), com
`[profile.dev.package.{neo-imagem,image,zune-jpeg,zune-core}] opt-level = 3`. O
`browser_local.rs` mantém `draw_marks`/`crop_jpeg` como repasses (o JSON do
snapshot vira `Marca`), e os testes antigos continuam valendo.

Medido no build de debug (o do `tauri dev`), mesmo print Retina de 3008×1552:
`draw_marks` **5,0 s → 0,12–0,13 s**; `crop_jpeg` 0,015 s. Junto, consertado um
teste instável de 16/11 (`tests_upload::casa()` usava a mesma pasta temporária
para os dois testes em paralelo).

Falta medir em produção: o `print+resto` do `look` nos próximos diários deve
cair de ~4,9 s para ~0,5 s (captura + ida e volta pela extensão).

### 7.7 Primeira medida em prod e instrumentação (17/11)

Job 19d7b323 (sessão cac58483, `look` pedido): print **1,9 s** (antes p50 4,9 s).
Mas o print do FIM, que não passa por processamento nenhum, também levou
~2,2 s — o que sobra não é imagem, é o caminho (extensão → host nativo →
desktop → websocket → VPS) ou a captura em si.

Instrumentado (~12:55 UTC): o `browser.screenshot` devolve `tempos`
(`captura_ms`, `esconder_ms`, `mostrar_ms`, `rects_ms`, `imagem_ms`,
`total_ms`, `bytes_captura`, `bytes_saida`); o `_shot` mede a ida e volta, e o
diário escreve, sob cada print:
`tempo: 1900 ms de ida e volta = device 450 ms (captura 300 · …) + transporte 1450 ms · 260 KB capturado → 181 KB enviado`.

## 9. F1 feito (17/11): gatilhos em código, só print

- `core/browser_local/visao.py` (lógica pura): `antes_da_acao` (G1 início do
  formulário = URL + painéis abertos; G1b antes do campo seguinte só se a tela
  mudou desde o último print inteiro; G3 antes de enviar pelo `_ENVIO`),
  `depois_da_acao` no snapshot seguinte (G2 recortado no campo — caixa do
  snapshot NOVO, 40 px acima para o rótulo e `recorte_margem_px` abaixo para o
  erro; G4 depois de enviar; G5 painel novo recortado; G7 URL mudou; G8
  upload), `juntar` (um print inteiro por momento, motivos somados; inteiro
  repetido na mesma fingerprint só sai se for G3/G4).
- Driver: `_visao_antes` antes do `_perform`, `_visao_depois` logo depois do
  `_note_change`; `_shot(confirmacao=…)` sai sem números (`scale 0.75`, q70) ou
  recortado (q80) e não depende da visão ligada; `prints_de_confirmacao` guarda
  os últimos 20 (gatilho, motivo, passo, fingerprint, imagem) para a fase da
  análise. Nunca derruba o passo.
- Diário: `NN-G2.jpg`, `NN-G4-G7.jpg`…, com "confirmação G2 — depois de
  «Empresa» (recortado)" e a linha de tempo do §7.7.
- `[navegador.visao]`: `confirmacao_visual = false` (padrão), `prints_max = 60`,
  `recorte_margem_px = 120`. **A verificação do booleano mora, por ora, na
  DETECÇÃO** (opção (a) do §8): desligado não há print nenhum a mais. Mudar
  para (b)/(c) depois da medição é trocar o `_visao_ligada()` de lugar.
- Fora desta fase: G6 (modo edição mais denso), análise pela visão (F2),
  portão de envio (F3).

## 10. Primeira rodada com o booleano ligado + F2 (17/11)

**Rodada (sessão 0c8904ed, job cb0374f1, 38 s):** G1, G2 (recortado, 36 KB —
o rótulo "Cargo", o valor e a faixa de erro), G3 e G4. Cada print custou
**0,3–0,9 s** de ida e volta: device 0,09–0,22 s (captura ~65 ms, imagem
25–125 ms) + transporte 0,18–0,72 s. Os 4 somaram ~2,5 s. Achados: (1) o G4
mostrou o fim da página — o site rola depois de salvar e o item salvo saiu da
tela; (2) reduzir a 0,75 PIORA: a captura de 97 KB saía com 131 KB e 120 ms a
mais. Medido em seguida (print 08-G3, 3 rodadas): a **1276 px** a visão leu o
formulário igual à de 2553 px, com 1.187 tokens em vez de 4.407, 1,4–1,9 s em
vez de 2,4–2,6 s e 45 KB em vez de 136 KB.

**F2 feito:**
- `core/browser_local/conferencia.py`: `pergunta(gatilho, contexto)` fechada por
  gatilho (G2 com o valor digitado ou o estado esperado da caixa; G3 com os
  campos da aba — dado pessoal e senha vão sem valor; G4 resultado do envio; G5,
  G7, G8, G1) e `interpretar` — o VEREDITO é do código (compara o visto com o
  digitado; campo estreito que mostra o começo vale), não do `confere` da visão.
  `valores_salvos_na_pagina`: depois de enviar, a DOM mostra os valores? (é o
  conserto do achado 1: a prova do salvo vem do texto, o print diz sucesso/erro).
- Driver: cada print de confirmação dispara `_analisar` em paralelo
  (`paralelas_max` = 2, `analises_max` = 40, `teto_raciocinio` = 200); o
  resultado vai para `conferencias`, para o bloco **CONFERÊNCIA VISUAL** do
  prompt (últimas 6, marcando "(tela anterior)") e, se divergir, para o detalhe
  do passo que causou ("⚠ conferência visual: …"). No fim, 3 s para as que
  estão em voo; o resto é cancelado.
- Print de tela inteira pede `max_width` = `largura_max_px` (1280) em vez de
  `scale`: `neo_imagem::reduzir` (desktop); desktop antigo manda a captura crua.
- Timeline da aba: `📷 print G2` (tipo `print`) quando fotografa e `👁 ✓ «Cargo»
  mostra …` (tipo `visto`, ou `erro` se divergir) quando a visão responde;
  marcador quadrado roxo/verde em `content-overlay.js` (reload MANUAL).
- Fora: portão de envio síncrono (F3), modo edição (F4), prova recortada do
  item salvo depois de rolar (precisaria rolar a página dele).

### 10.1 Primeira rodada da F2 (sessão 69020f7a, job 4be01af3) — ✅ e dois consertos

Funcionou de ponta a ponta: `extract` leu a descrição inteira (603 caracteres),
o `fill` reescreveu com o trecho trocado, a conferência do Daniel aprovou, SALVAR.
Prints G1 (50 KB), G2, G3 (44 KB), G4 (35 KB): 0,3–1,0 s cada. Conferências: G1
✓, G2 ✓, G4 "incerto" (a tela do perfil não diz sucesso nem erro — quem
confirma é a DOM). Dois defeitos:
1. **G2 pelo número errado.** O `fill` rolou a página e renumerou: a descrição
   foi de [28] para [18], e o [28] virou o seletor de mês. `depois_da_acao`
   agora acha o campo pela IDENTIDADE (`_mesmo_campo`: tag + texto, desempate
   pelo começo do valor) e, sem achar, não usa a caixa velha — print inteiro.
   O recorte que falhava caía no `draw_marks` e desenhava números (367 KB): o
   recorte de confirmação leva `max_width`, e o desktop só desenha com `marks`.
2. **JSON cortado no G3.** A visão ecoou os 600 caracteres no `visto` e passou do
   `max_tokens`. Valor > 120 caracteres vai na pergunta só pelo começo, a visão
   devolve as primeiras 80 letras, e o código compara o começo; `max_tokens` 700.
Também: a conferência pela DOM depois de enviar vai para o diário.

### 10.2 G2 logo depois da ação, guiado pelo número (17/11)

Pedido do Daniel: "ele preenche e já sai rolando" — o G2 saía no snapshot
SEGUINTE, depois de a página (ou o agente) rolar. Agora:
- `visao.logo_depois`: o G2 sai logo depois do `_perform`, ANTES do próximo
  snapshot, com `alvo_ref` = o número que a ação usou (o `NEO.refs` do snapshot
  de antes da ação ainda vale). `click_text` (sem número) segue no snapshot
  seguinte; caixa/rádio leem o estado pelo retorno do clique.
- Desktop: `browser.screenshot {alvo_ref, margem_abaixo, max_width}` →
  `js_alvo_do_print` mede o campo NA PÁGINA na hora (campo + `<label>` + faixa
  de erro abaixo); fora da tela, `scrollIntoView` centralizado (`rolou`, vai ao
  diário). Sem achar o elemento, tela inteira reduzida, sem números.
- Lab Edge headless (`scratchpad/scripts/lab_alvo.py`): campo a 1600 px → rolou e
  devolveu o retângulo com o rótulo; segunda chamada não rola; ref inexistente
  → `ok: false`.
