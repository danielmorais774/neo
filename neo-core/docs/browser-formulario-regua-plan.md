# Formulário no navegador local: régua de produção, Enter e autocomplete — plano

> Status (18/11/2026, 11:47 BRT): **F0–F5, F7, F8a, F9, F10 em produção** (§7.2–7.5).
> E2E ✅: F8a (119bde02), F10 (9e2a249a). Falta só o E2E de uma mensagem espontânea
> pelo painel (F2); sobras em §7.6.
> Fora: o G2 da visão ainda pergunta "deve mostrar X" no autocomplete (o aviso
> em código cobre o modelo) e a F6. Origem: sessão `4a345d50`,
> job `9fcb3bb6`. O pedido era preencher https://demoqa.com/automation-practice-form
> sem enviar. O job terminou `failed` no passo 18 com 8 dos 10 campos certos na
> tela, e uma instrução do Daniel chegou 200 ms antes da parada e foi perdida.

## 1. O que aconteceu

| Passo | Hora (UTC) | O que houve |
|---|---|---|
| 2–8 | 12:04:39–12:05:02 | First/Last Name, Email, Male, Mobile, Current Address: todos certos, conferidos pela visão (G2 `confere: true`) |
| 5 | 12:04:52 | a primeira e **única** nota nova (`anotou (1)`). Daqui em diante, o modelo só atualiza a nota 1 |
| 8–11 | 12:05:07–12:05:21 | datepicker: mês May, ano 1995, dia 15. Ficou certo |
| 12–13 | 12:05:24–12:05:28 | dois cliques no input do Subjects (4×24 px); o segundo `NÃO mudou` |
| 14 | 12:05:28 | `fill` "Computer Science" abre a lista do react-select com `[19] div(option) "Computer Science"` |
| 15 | 12:05:35 | `press Enter` para escolher a opção → **"Posso enviar?"** (`Enter (enviar o formulário)`) |
| 15 | 12:05:45 | Daniel: "Não envie" → o Enter é barrado; a opção nunca é escolhida |
| 16 | 12:05:47 | Reading marcado. O react-select perde o foco e apaga o texto digitado |
| 17 | 12:05:51 | o modelo vê o Subjects vazio e escreve: "só tenho 1 passo antes do encerramento" |
| 18 | 12:06:03.843 | Daniel pelo painel: "faltou seleciona o state e uma cidade" |
| 18 | 12:06:04.035 | **"Parei: 12 passos seguidos sem acrescentar nada"** → `failed` |

O cérebro repetiu no chat o motivo da parada ("rolar a página e esperar o
carregamento já tinham sido tentados") e ofereceu "preencher por script". A
mensagem sobre State/City não aparece em lugar nenhum da resposta.

## 2. As causas

### C1. A régua de produção não reconhece formulário (é o que matou o job)

`_contabilizar_producao` ([driver.py:5157](../core/browser_local/driver.py#L5157))
conta ganho em só dois casos:

- nota NOVA no caderno;
- `(nota cresceu ∧ interagiu) ∨ marcou`, **e também** ≥ `_TELA_INEDITA_MIN` (4)
  palavras nunca vistas no texto da tela.

Num formulário o texto da tela não muda. O valor digitado vai para a lista de
elementos (`= "Daniel"`), não para "NA TELA AGORA", que ficou igual do passo 2
ao 8. Assim, os 12 preenchimentos e cliques certos dos passos 6–17 contaram
como zero, e `_SEM_GANHO_MAX = 12` encerrou o job.

A régua foi medida em coleta e quiz (6633351e, 78d998ee, 379df104, b8dbde34).
Formulário longo nunca entrou na medição.

### C2. A mensagem do Daniel não zera a régua

O comentário do laço principal ([driver.py:8463](../core/browser_local/driver.py#L8463))
diz "mensagem nova é trabalho novo", mas `_acolher`
([driver.py:2269](../core/browser_local/driver.py#L2269)) só soma
`_bonus_mensagens` ao orçamento e não mexe em `_acting_no_ganho`. A mensagem
foi recebida, logada e marcada "já entregue a ela" no chat. Na mesma rodada,
`_motivo_para_parar` encerrou o job. O resumo final (`_com_caderno`) não leva as
mensagens que o modelo nem chegou a ver, então o cérebro também não ficou sabendo.

### C3. Enter em autocomplete aberto vira "enviar o formulário"

`_check_revisao_enter` ([driver.py:3046](../core/browser_local/driver.py#L3046))
dispara a conferência se houver ≥ 2 campos preenchidos e algum elemento com
`_ENVIO` (o "Submit"). Ela não olha se há uma lista de opções aberta, mas o
snapshot já traz isso:

- `type` = `role` do elemento: `div(option)`;
- `aberto` = `aria-expanded`, que o snapshot já lê ([browser_local.rs:2244](../../neo-desktop-tauri/src-tauri/src/services/browser_local.rs#L2244)).

Com a lista aberta, o Enter escolhe a opção e não envia. O resultado foi
perguntar "Posso enviar?" com o formulário pela metade, e o "não" dele
desmontou o Subjects.

### C4. Texto digitado no autocomplete passa como valor escolhido

- O G2 ([conferencia.py:89](../core/browser_local/conferencia.py#L89)) pergunta se
  "o campo deve mostrar «Computer Science»". A visão vê o texto digitado e
  responde `confere: true`, nos passos 15 e 18.
- A conferência antes do envio listou "Subjects → Computer Science" como
  preenchido.
- No passo 16 o modelo achou que o Subjects estava pronto.
- Nada no `detail` do `fill` avisa que, num combobox, o valor só vale depois de
  escolher a opção.

### C5. O motivo da parada é texto fixo, e o resumo confia na nota do modelo

- A frase "rolar a página e esperar o carregamento já foram tentados" foi escrita
  para o job 29c739d4. Aqui é falsa: ninguém rolou nem esperou.
- O resumo leva a nota do modelo, que dizia "Subjects ficou VAZIO", enquanto o
  print final mostra o texto digitado com a lista aberta.
- O estado real dos campos está no último snapshot e não entra na entrega.

## 3. Princípios

1. **Ganho de formulário é o que o DEVICE confirma**, não o que o modelo anota:
   valor no snapshot, `[marcado]`, opção que saiu da tela. Decisão em código
   ([[feedback-decisao-em-codigo-nao-no-llm]]).
2. **Um campo rende ganho uma vez só.** Preencher de novo o mesmo campo é giro,
   não produção. É isso que impede a régua nova de soltar um loop.
3. **Mensagem dele é trabalho novo**, como o comentário já promete.
4. **Medir no replay antes de publicar**
   ([[feedback-medir-no-diario-antes-de-concluir]]): a régua nova roda sobre os
   20 diários da VPS e sobre os testes que guardam os traces antigos.
5. **Nenhum número novo solto.** Se algum aparecer, vai para `config/neo.toml`
   ([[feedback-constantes-no-neo-toml]]).

## 4. Fases

### F0 — Medição: régua E no replay (antes de qualquer código no driver)

`scripts/replay_sem_ganho.py` ganha a régua **E** = régua C + ganho de campo:

- em cada passo, ler a lista de elementos do bloco "página como o modelo viu"
  (`[ref] tag(tipo) "rótulo" = "valor"`, `[marcado]`);
- **ganho de campo:** o passo foi `fill`/`select`/`click` e, no snapshot
  seguinte, um campo (chave = rótulo) aparece com valor diferente do PRIMEIRO
  valor visto para ele no job. Isso cobre a data que já vinha com
  "18 Nov 2026". O campo entra num conjunto e não rende de novo;
- **rádio/checkbox:** `→ SELECIONADO` rende uma vez por GRUPO de rádio e uma vez
  por checkbox, sem a exigência de palavras inéditas;
- **opção escolhida:** clique em `(option)` que "SAIU da tela" rende uma vez por
  rótulo do campo dono;
- **mensagem do Daniel** (`mensagem do Daniel` no diário) zera o contador.

Linha de base de hoje (régua D, a do driver; 20 diários, 18/11 — ver §7.1 para a medida com o simulador corrigido):

| job | o que era | real | régua D (simulador antigo) | o que a régua E precisa dar |
|---|---|---|---|---|
| 9fcb3bb6 | este formulário | parou no 18 | para@18 | **segue** |
| 1c55ba10 | YouTube, giro real | parou no 21 | para@20 | para no mesmo passo |
| c1a738d6 | YouTube, giro real | parou no 14 | para@13 | para no mesmo passo |
| ccfee96e | GeekHunter, 10 `fill`, 115 passos `completed` | não parou | para@32 | **explicar a divergência primeiro** |
| os outros 16 | | | segue | segue |

Em produção o ccfee96e não parou, mas o simulador diz que pararia no 32. Isso
tem de ser entendido antes de confiar no replay. Hipóteses: mensagens dele,
`_bonus_grade`, ou ações recusadas que o simulador cobra.

Os traces antigos dos quizzes não estão mais na VPS, só nos testes de
`tests/browser_local/test_orcamento_por_producao.py`, e esses testes têm de
passar sem mudança.

**Pronto quando:** a tabela da régua E sai com 9fcb3bb6 seguindo e os dois
YouTube parando no mesmo passo de hoje. O resultado entra neste doc (§7).
✅ Feita em 18/11. O critério do c1a738d6 caiu: ele é outro caso da C2 (§7.1).

### F1 — Régua do driver reconhece formulário (C1)

- `self._campos_com_ganho: set[tuple[aba, chave]]`, zerado por job.
- Em `_contabilizar_producao`, com o snapshot do topo da rodada:
  - o último passo foi `fill`/`select` que deu certo, e o elemento alvo (pelo
    mesmo rótulo de `rotulos_do_campo`, que `_registrar_preenchimento` já usa)
    tem valor não vazio e diferente do valor inicial → ganho, se a chave for
    inédita;
  - `_marcou` passa a render sem `ineditas >= 4` quando a chave (grupo do rádio
    ou checkbox) é inédita. O quiz já passa assim e "mesma pergunta marcada de
    novo" continua sem render;
  - clique em `type == "option"` que saiu da tela → ganho pela chave do campo
    dono (o combobox com `aberto` no snapshot anterior).
- A nota atualizada continua como está (régua C).

**Testes novos** (em `test_orcamento_por_producao.py`):

- formulário de 14 campos, tela sem palavra nova e uma nota só → chega ao `done`;
- o mesmo campo preenchido 12× seguidas → para;
- rádios do mesmo grupo alternados → 1 ganho só;
- datepicker com valor padrão trocado → 1 ganho.

### F2 — Mensagem do Daniel zera a régua e não se perde (C2)

- `_acolher`: `self._acting_no_ganho = self._acting_steps`. A régua volta a
  contar dali.
- Resumo do fim (`_com_caderno`): se houver mensagem em `_mensagens_novas` (que
  o modelo não viu), acrescentar
  `Mensagem do Daniel que chegou e não foi atendida: «…»`. Assim o cérebro
  responde a ela em vez de ignorar.
- **Testes:**
  - régua em 11 + mensagem chega → não para naquela rodada;
  - job que termina com mensagem não vista → o resumo a cita.

### F3 — Enter com lista de opções aberta não é envio (C3)

- `_check_revisao_enter` retorna sem perguntar quando o snapshot tem elemento
  visível com `type == "option"` ou algum elemento com `aberto is True`.
- O clique em "Submit" e o Enter num formulário sem lista aberta continuam
  pedindo conferência. É a garantia da sessão e752a399, e não pode cair.
- **Testes:**
  - lista aberta + 2 campos + Submit → Enter passa;
  - sem lista → `NeedsConfirmation` como hoje.

### F4 — Autocomplete: digitado ≠ escolhido (C4)

- Depois de `fill`, se o snapshot seguinte mostrar opções abertas para aquele
  campo, o `detail` ganha: `→ digitado, mas a lista de sugestões está ABERTA: o
  valor só vale depois de escolher — clique em [19] «Computer Science»`. Isso é
  código, não prompt.
- O G2 desse campo não pergunta "deve mostrar X", porque a visão confirma o texto
  digitado. Ou ele é pulado (o código já sabe a resposta), ou vira "o valor X foi
  ESCOLHIDO (etiqueta/valor fixo) ou a lista ainda está aberta?". Preferência:
  pular, que também economiza uma chamada.
- Em `_registrar_preenchimento`, o campo fica `pendente` até a opção ser
  escolhida, e a conferência antes do envio mostra
  `«Subjects» → Computer Science (digitado, opção NÃO escolhida)`.
- **Antes de codar:** conferir num snapshot real como o react-select multi
  mostra a etiqueta escolhida (div com o texto? some do `value` do input?).
  Sem isso não se sabe qual sinal do F1 confirma o Subjects.
- **Testes:**
  - `fill` com opção aberta → `detail` aponta a opção e o G2 não é disparado;
  - clique na opção → `pendente` sai.

### F5 — Parada que diz a verdade (C5)

- O motivo deixa de ser texto fixo. Ele é montado a partir de `self.steps` da
  janela parada, por exemplo:
  `nos últimos 12 passos: 9 fill, 2 click, 1 press — nenhum campo novo ficou preenchido`.
  A frase sobre rolar e esperar só entra se `scroll`/`wait_for` estiverem na
  janela.
- Quando há `_preenchidos`, o resumo (parada ou `done`) leva um bloco
  **"Campos na tela no fim"**, tirado do último snapshot: rótulo → valor,
  `[marcado]` ou VAZIO. É a verdade da DOM, não a nota do modelo.
- **Testes:**
  - parada após 12 `fill` não menciona rolar nem esperar;
  - o bloco de campos reflete o snapshot, não a nota.

### F6 — Opcional, junto com a F1

Levar `_SEM_GANHO_MAX`/`_SEM_GANHO_AVISO` para `neo.toml` (`[navegador]`).
Nesta mudança eles já estão sendo tocados.

## 5. Ordem e publicação

1. **F0** (só script; roda no container lendo os diários no lugar, nada sai da VPS).
2. **F2 + F3**: pequenas e independentes da medição; podem ir primeiro.
3. **F4 conferência real do react-select → F1 → F5.**

O `driver.py` é caminho quente: salvar é publicar. Por isso:

- editar numa cópia no scratchpad ([[neo-dev-em-copia-no-scratchpad]]);
- `cmp` contra o repo antes de copiar de volta;
- importar pelo `.venv`;
- rodar a suíte inteira, não só `tests/browser_local`;
- conferir no `app.log` que não há job do navegador rodando na hora de copiar.

Se a F6 entrar, publicar o `shared/ajustes.py` antes do `neo.toml`.

## 6. Riscos

| Risco | Contenção |
|---|---|
| Régua E solta um giro real (preencher → apagar → preencher) | ganho é por campo, uma vez; os dois YouTube e os testes de quiz/carregando no replay |
| Site que muda valor sozinho (relógio num input) | ganho só quando o passo anterior AGIU naquele campo |
| Pular a conferência no Enter envia um formulário | só com lista de opções visível; o Submit e o Enter sem lista continuam conferidos |
| Mensagem dele vira "vida infinita" | ele é a autoridade; o orçamento duro (`passos_teto`) continua valendo |

## 7. Resultados

### 7.1 F0 (18/11/2026)

`scripts/replay_sem_ganho.py` agora roda as réguas `velha`, `D` (a do driver
hoje), `E1` (D + campo novo = F1), `E2` (D + mensagem zera = F2) e `E`
(as duas juntas). Comando: o mesmo do cabeçalho do script.

**Antes de medir, o simulador estava errado.** Ele cobrava o passo pela ação
escolhida, mas o driver não cobra ação recusada nem a rodada da confirmação ao
vivo (o cabeçalho `Passo N/T` repete o N). A correção é cobrar quando o próximo
cabeçalho avança o número. Com isso:

- as três paradas reais batem exatamente: 1c55ba10 (21), c1a738d6 (14),
  9fcb3bb6 (18). Antes, as duas primeiras saíam 1 passo antes;
- o "para@32" do ccfee96e era fantasma. Na régua D ele segue, como foi em
  produção (115 passos, `completed`).

Resultado nos 20 diários da VPS ("segue" = não pararia ANTES do fim do diário;
num job morto pela régua, isso quer dizer que iria mais longe, não que iria até
o fim):

| job | real | D | E1 (campo) | E2 (mensagem) | E | campos com ganho |
|---|---|---|---|---|---|---|
| 9fcb3bb6 (este formulário) | parou 18 | para@18 | **segue** (teto 34) | para… segue¹ | **segue** | 9 |
| 1c55ba10 (YouTube, giro) | parou 21 | para@21 | para@21 | para@21 | para@21 | 1 |
| c1a738d6 (YouTube) | parou 14 | para@14 | segue² | segue³ | segue | 1 |
| os outros 17 | — | segue | segue | segue | segue | 0–7 |

1. A mensagem chegou na mesma rodada da parada. Com E2 o job sobrevive àquela
   rodada, mas sem E1 morreria de novo logo depois, porque o formulário continua
   sem render ganho.
2. Um ganho só: colar o link no site de download (passo 4). Isso empurra a
   parada 2 passos para frente, além do fim do diário. É limitado.
3. **Outro caso da C2, não contraexemplo.** Às 15:29:51 o Daniel respondeu
   "Sim, libere o googlevideo e baixe direto", o escopo foi ampliado e a aba foi
   para o MP4. O job morreu **um** passo cobrado depois. O critério da F0 ("os
   dois YouTube param no mesmo passo") estava errado para este job.

Nos jobs que terminaram bem, o efeito é só o teto um pouco maior (509c4f7a
54→60, 136453ab/cb0374f1/92aa9843 +1). Nenhum muda de desfecho.

Os 9 campos do 9fcb3bb6, um por um: Name, Last Name, Email, Male, Mobile,
Current Address, Date of Birth (trocou o padrão "18 Nov 2026"), Subjects e
Reading. O **Subjects conta pelo texto digitado**, sem a opção escolhida. O
replay não enxerga `aberto` (aria-expanded). No driver, a F1 deve ignorar campo
com `aberto is True`; a F4 cobre o resto.

**O que a F1 herda do replay** (é o desenho que foi medido):

- o ganho não depende do alvo da ação. Depois de uma interação que deu certo,
  qualquer campo cujo estado (valor, `checked`) difira do primeiro estado visto
  no job rende, uma vez por campo. É isso que pega o datepicker, onde o clique
  é no dia e quem muda é o input;
- a chave do campo é o `<label>` logo antes dele, como em `rotulos_do_campo`.
  Rádio e caixa usam o próprio texto, porque o label deles vem depois. Campo
  dentro de painel fica fora (o mês e o ano do datepicker);
- o que não foi medido: os traces antigos de quiz e de "carregando", que não
  estão mais na VPS. Eles vivem em `test_orcamento_por_producao.py`, e esses 14
  testes têm de continuar passando na F1.

### 7.2 F1–F5 no driver (18/11/2026)

Tudo em `core/browser_local/driver.py`, com testes em
`tests/browser_local/test_formulario_regua.py` (11). Contra o driver de antes,
10 desses 11 falham; o que passa é a regressão de propósito (Enter sem lista
aberta continua conferindo). Suíte: 4780 → 4792 passando.

| Fase | O que entrou |
|---|---|
| F1 | `_estado_dos_campos` + `_campo_novo`: campo com estado diferente do primeiro visto, depois de interação ok, rende ganho uma vez por (aba, campo). Painel, `aberto` e `file` ficam de fora |
| F2 | `_acolher` zera `_acting_no_ganho`; `_com_campos_e_mensagens` põe no resumo a mensagem que o agente não viu |
| F3 | `_check_revisao_enter` sai sem perguntar com `type == "option"` visível ou elemento `aberto` |
| F4 | `_aviso_de_sugestoes` no prompt depois de `fill` que abriu lista; o campo fica `pendente` na conferência até o clique numa opção. **Não feito:** pular/reformular o G2 da visão nesse campo |
| F5 | o motivo da parada conta as ações da janela (`fill ×9, click ×2…`) e só fala de rolar/esperar se houve; resumo ganha "Campos na tela no fim" lido do último snapshot |

### 7.3 Primeiro E2E (sessão 15ccd81e, job 7ce2a472) e F7 (18/11/2026)

✅ `completed` em 23 passos e 96 s, com os 12 campos certos, incluindo o
Subjects com dois chips e State/City. Nenhum "Posso enviar?". No passo 11 o
modelo seguiu o aviso da F4 ("A lista de sugestões de Subjects está aberta com
«Maths» em [19]. Clico nela"). **Não foi exercitado:** a mensagem pelo painel
(F2) e o Enter (F3), porque o modelo escolheu por clique.

O que o diário mostrou, consertado na F7:

| Defeito | Causa | Conserto |
|---|---|---|
| bloco do fim com "(sem rótulo): VAZIO" no Subjects | os chips "Remove Maths" ficam entre o label e o input; o valor de verdade está nos chips | `_rotulo_do_campo` pula até 3 não-campos para achar o label; campo vazio mostra o texto da tela ao lado do rótulo (`«Maths Computer Science»`) |
| "1: marcado", "2: marcado" | o texto do input checkbox é o `value`; o nome vem no label DEPOIS | caixa e rádio usam o label seguinte |
| State/City ausentes do bloco | o select personalizado já escolhido sai da lista de elementos | rótulo sem campo depois dele entra com o texto da tela (`«NCR Delhi»`) |
| inputs sem rótulo colidiam numa chave só (afeta o ganho da F1) | chave igual para todos os `input(text)\|` | sufixo `#2`, `#3` |
| entrega com 24 notas, várias velhas ("Subjects: pendente") | o modelo anotou muito; o caderno ia antes do bloco de campos | bloco de campos ANTES do caderno, e o caderno passa a ser "Anotações do caminho — podem estar DESATUALIZADAS" |
| 2 cliques mortos no input de 4 px, nos DOIS jobs | clicar na caixa do react-select não abre nada | dica no elemento: "caixa de digitar de uma lista: use `fill` direto" |

Visto e não tratado: o modelo escolheu só o dia no datepicker, e a data de
nascimento de teste ficou "15 Nov 2026". O pedido não fixava uma data, então
isso é escolha do modelo, não defeito do driver.

Testes: 16 em `test_formulario_regua.py`; suíte com 4797 passando.

### 7.4 Sessão 3a053c06 → F8a e F9 (18/11/2026, 11:38 BRT)

**O caso:** o job d33553b6 fez `fill` "10 Jan 1995" na Date of Birth. A trava
de dado pessoal disparou `NeedsRelease`, mas a memória não tem data de
nascimento, então o runner a pôs em "faltando": nenhum `release_request`, nenhum
card, job `failed` e a pergunta só como texto no chat. Depois do "pode inventar",
o cérebro abriu um job NOVO (1153f31a) e refez tudo do zero. O bloco de campos
saiu certo, mas o chat mostrou as 13 notas, porque o runner entregava
`outcome.notes`.

| Fase | O que entrou |
|---|---|
| F8a | `_dado_ausente_ao_vivo`: dado pessoal que a memória NÃO tem (`filler.disponivel`) vira pergunta viva — "Invente um valor de teste" / "Deixe em branco" / valor que ele digitar. A decisão vale para o resto do job; o valor ditado só entra naquele campo. Sem resposta no prazo → `NeedsRelease` como antes. Dado que a memória TEM continua indo para o card |
| F9 | `DriverResult.campos` + `_itens_da_entrega` no runner: os fatos da entrega são os campos da tela, e a nota que fala de um campo do bloco sai. Nota que não é de campo (preço, aviso) fica. Bloco só com 2+ campos (`_e_formulario`, a régua do `_check_revisao_enter`) |

**F8b, ainda não feita:** a frase do Daniel ("dados de teste", "pode inventar")
liberar valor inventado sem perguntar. Antes, medir a lista de frases no corpus
de mensagens reais. Hoje a F8a já cobre o caso com UMA pergunta.

Testes: 24 em `test_formulario_regua.py`; suíte com 4805 passando.

### 7.5 Sessão 119bde02 (job 9a75082a) → F10 (18/11/2026, 11:47 BRT)

✅ A F8a funcionou: a data digitada virou pergunta, e o "inventa" digitado no
painel foi entendido. O job seguiu na mesma aba até `completed`. ✅ F9: o chat
abriu pelos campos da tela.

**Os botões sumiram do card na extensão.** O desktop fecha a caixa de botões na
próxima faixa que não seja pergunta (`browser_local.rs`, `PERGUNTAS` →
`neoPergunta` vazio). A conferência visual G1b roda em fundo, terminou ~1,4 s
depois da pergunta e mandou "👁 …" para a linha do tempo, levando os botões
junto. **F10:** com uma pergunta aberta, `_banner` guarda as faixas que não são
pergunta (`_faixas_seguradas`) e as solta no `finally` da espera. Também:

- a pergunta citava o VALOR do campo («18 Nov 2026»); agora cita o rótulo
  («Date of Birth»);
- a nota "Mobile: …" escapava do filtro da F9, porque o rótulo é
  "Mobile(10 Digits)". O filtro agora compara o rótulo sem o parêntese.

Visto e não tratado: o cérebro fechou oferecendo "quer que eu clique em Submit?"
num pedido que dizia "Não envie".

Testes: 27 em `test_formulario_regua.py`; suíte com 4808 passando.

### 7.6 Sessão 9e2a249a (job b804f200): E2E da F10 ✅ (18/11/2026)

A pergunta saiu às 15:09:09 citando «Date of Birth». A conferência G1b terminou
durante a espera (~15:09:12), e a faixa dela ficou segurada: os botões ficaram
no card, e o Daniel clicou "Invente um valor de teste" às 15:09:14. O job seguiu
na mesma aba até `completed` (107 s), e o chat abriu pelos campos da tela.

Sobras, sem urgência:

- **Trava do painel no datepicker.** Ao digitar a data, o calendário abre; o
  modelo tentou fechar, e a trava "não feche o painel — ele É a resposta"
  recusou. Falso positivo num calendário, contornado.
- **Notas em português passam pelo filtro da F9** ("Data de nascimento…",
  "State: NCR…; City: Delhi…"), porque o rótulo é "Date of Birth" / "State and
  City".
- **O cérebro oferece "clico em Submit?"** num pedido de "Não envie".
- F8b, visão no autocomplete (F4-G2), constantes no `neo.toml` (F6).
- **F2 por conta própria** (mensagem que não é resposta a pergunta) ainda sem E2E.

## 8. Teste ponta a ponta (depois de F1–F5)

Pré-requisito: nenhum, porque a mudança é só no neo-core. Prompt (166 caracteres):

```
Preencha no meu navegador o formulário https://demoqa.com/automation-practice-form com dados de teste em TODOS os campos, inclusive Subjects, State e City. Não envie.
```

Roteiro:

1. **Não** responder nada até o Subjects. O que tem de acontecer:
   - o Subjects termina como etiqueta escolhida;
   - **nenhum "Posso enviar?"** no Enter/clique da opção.
2. Depois do Mobile, escrever no painel da aba: `troca o gênero para Female`.
   - Ele tem de trocar, e o job não pode parar nas rodadas seguintes por
     "sem acrescentar".
3. State e City escolhidos (ex.: NCR / Delhi).
4. Fim `completed` com `done`, e o resumo traz "Campos na tela no fim" batendo
   com o print.
5. Se ele tentar o Submit, tem de vir "Posso enviar?". Responder "Não envie".

O que este teste **não** mede:

- se a régua ainda para giro de verdade (isso é o replay da F0);
- o upload da Picture.

Mandar o id da sessão para conferir no diário.

## 9. Fora deste plano (visto na mesma sessão)

- **Cérebro:** os argumentos do `browser_local_task` vieram cortados pelo limite
  de tokens (coluna 924), o que custou uma rodada extra. A primeira resposta
  levou 61 s.
- **Passos 12–13:** dois cliques num input de 4×24 px antes do `fill`. O `fill`
  direto funcionou; não justifica regra nova.
