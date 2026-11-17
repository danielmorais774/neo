# Plano: a tarefa no navegador vira conversa — avisa, pergunta, escuta e segue

**Data:** 2026-11-11
**Status:** F1, F2, F3 e §4.8 implementados em 2026-11-11 (§9). F5 (cofre, §9.7) e F4
(`julgar` + `marcar_grade`, §9.9) em 2026-11-12. Falta o E2E de F4/F5 e §4.9. Origem: `session-2bd028fb`, jobs
`207e99a2`, `ee3e629e`, `14ed2795`, `7f05c32d`, `25cfccef`, `c281e91d`, `d3752c0e`.
**Depende de:** `docs/browser-local-plan.md` (driver, desktop, extensão),
`docs/browser-multi-aba-plan.md` (abas A/B/C, `abrir_aba`, `trocar_aba`,
`paralelo`), `docs/release-request-plan.md` (parada, card, retomada).

## 0. O pedido

Nas palavras do Daniel (11/11):

- **analisar antes e contar durante:** o Neo lê a página, entende do que se
  trata e diz no chat, **com a tarefa rodando**: "para preencher bem eu
  precisaria analisar o site AppDemo";
- **esperar sem fechar nada:** o formulário fica aberto no navegador enquanto
  ele espera a resposta;
- **duas abas vivas:** confirmado o AppDemo, abre outra aba e mantém as duas. Faz o
  cadastro e o login no AppDemo, volta ao formulário sem fechar o AppDemo, responde os
  RFs e, na dúvida, vai à aba do AppDemo, olha e volta;
- **chat e navegador ao mesmo tempo:** hoje uma coisa espera a outra. O processo
  tem de seguir nos dois lugares, com troca de mensagens nas duas direções;
- **um campo no painel da extensão** para mandar mensagem ao agente em tempo de
  execução, e ele se adaptar na hora.

## 1. O que aconteceu na sessão 2bd028fb

Form da UFC "Avaliação de Requisitos de Software - Plataforma AppDemo":
- 9 páginas, com obrigatórias;
- pág. 1 = perfil e TCLE;
- págs. 2–4 = uma grade por RF (Clareza / Ambiguidade / Completude /
  Verificabilidade × Conforme / Não Conforme / Parcialmente / N.A.), com o
  enunciado do RF no rótulo da pergunta;
- a descrição cita o sistema em `sistema-exemplo.vercel.app`.

| Hora (UTC) | O que houve | O que a conversa deveria ter feito |
|---|---|---|
| 18:37–18:39 | 2 turnos de recusa ("é um teste") e o job do **servidor** (`1cd348bc`, reaproveitado) | — |
| 18:39 | job lê a pág. 1 e para | avisar no chat o que é o form; perguntar só o que não dá para deduzir |
| 18:40–18:43 | 3 turnos de pergunta P2/P3/P6/P7; `ee3e629e` tenta "ler sem preencher" e desiste (obrigatórias) | ler a pág. 1 PREENCHENDO; o resto aparece página a página |
| 18:43–18:48 | `14ed2795` marca "Conforme" em 56 células por regra escrita às cegas | na pág. 2: "é avaliação de 14 RFs do AppDemo em 4 critérios. Julgo pelo texto ou abro o AppDemo?" e **esperar com o form aberto** |
| 18:48 | Daniel aperta PARE | escrever no painel "avalie de verdade pelos critérios C1–C4" e o agente seguir |
| 18:49 | cérebro diz que o form "só dá RF-01" (falso: o texto estava na página, mas as notas não guardaram) | — |
| 18:49–18:51 | `7f05c32d` vai ao AppDemo **sem janela** (headless); Daniel vê o form parado, diz "travou" e fecha a aba | avisar "estou no AppDemo em segundo plano"; o "abre outra" vira mensagem para o job, não um job novo |
| 18:51–18:55 | 3 jobs de cadastro no AppDemo morrem com "Debugger unattached" logo após digitar o e-mail; um deles inventou `teste@example.com` | aba B no AppDemo com a credencial do cofre; aba A segue aberta |

O desenho de hoje explica a sessão inteira:
- cada coisa é um **job curto**;
- um job só por navegador;
- o job não fala com o chat no meio do caminho;
- o chat não fala com o job.

Toda mudança de rumo virou "parar, um turno do cérebro, job novo", e cada job
novo começou sem o contexto do anterior.

## 2. O que já existe (conferido no código em 11/11)

- **Várias abas num job:** rótulos A/B/C, `abrir_aba`, `trocar_aba`,
  `fechar_aba`, `ler_abas` e `paralelo`, com E2E feito
  (`docs/browser-multi-aba-plan.md` §13–§20). A parte mecânica de "duas abas
  vivas, ir e voltar" **já existe dentro de um job**. Ela não rodou porque
  nenhum job teve as duas coisas na missão.
- **Parada com card:** `needs_confirmation` (antes de ação que não desfaz e
  conferência antes do Enviar, `driver.py:2870 _check_revisao`) e
  `needs_release` (dado pessoal).
  - As duas **encerram o driver** (`driver.py` ~6742 e ~6763).
  - O runner solta a aba sem fechar, e um **job novo** continua
    (`executor.py` `continue_job`, `core/jobs/release_resume.py`).
  - A retomada leva `browser_session`: aba, conjunto de abas, URL e respostas
    da memória.
  - Não levam: o histórico de passos e as notas do caderno.
- **Painel na aba** (`neo-browser-local/content-overlay.js`): lateral, com linha
  do tempo e PARE.
  - O tráfego é de mão única para dentro: o desktop empurra eventos por
    `window.postMessage` (`browser_local.rs:3435 js_banner`).
  - Só o PARE sai, por duas vias: o atributo `data-neo-stop` no `<html>`, lido
    em todo snapshot, e `NEO_STOP` → background (`background.js:1383`) →
    notificação `neo.stop` → `STOP_REQUESTED` no Rust (`browser_local.rs:48`,
    `:266`).
- **Mensagem de job no chat:** `kind="chat_message"`
  (`dispatcher.py:499 _publish_chat_message`). Usado só no fim do job e pelo
  card de liberação (`core/release/notify.py`).
- **Progresso ao vivo:** `on_progresso` (`runner_browser_local.py:889`) já é
  um callback do driver para fora durante a execução. Hoje vai só ao caderno.
- **Memória por pergunta:** `consultar` → `respondedor.py`. Responde só o que
  está no PKS.
- **Cérebro × job:** `job_status`, `job_logs`, `job_cancel`, `continue_job`.
  Não existe tool para **falar** com um job rodando.

## 3. O que falta

1. **O chat não alcança o job.** `browser_local_task` com job rodando devolve
   "JÁ EXISTE uma tarefa… Espere o resultado" (`executor.py:1252–1276`). A
   trava de um job por navegador é correta, porque dois jobs brigam pela aba e
   o primeiro a terminar solta o debugger do outro (job 698446b8). O defeito é
   não haver como **mandar recado** para o job que está rodando.
2. **O job não alcança o chat no meio do caminho.** Não existe ação para avisar
   nem para perguntar. O único jeito de perguntar é parar, e parar é morrer e
   renascer noutro job.
3. **O painel não tem entrada de texto.** O PARE é o único gesto do Daniel na
   aba.
4. **O agente do passo não julga.** O modelo do passo (deepseek flash,
   raciocínio 0, teto de 2000) é bom para clicar, não para avaliar requisito. O
   `consultar` só cobre a memória. Para pergunta de opinião ou avaliação sobre
   o que está NA PÁGINA, não há caminho: o cérebro escreveu a regra às cegas, e
   o passo a aplicou em tudo.
5. **As notas perdem o enunciado.** "RF-01: 4/4 Conforme" sem o texto do RF.
   Isso fez o cérebro afirmar que o form não tinha os requisitos.
6. **Credencial no navegador local não existe.** `credentials` só é lido pelo
   `browser_task` do servidor (`executor.py:470`). No local, o rótulo
   `email-pessoal-do-neo` foi descartado em silêncio, o agente inventou a senha
   e, num job, até o e-mail.
7. **Aba fechada vira "explodiu".** O `onDetach` (`background.js:230`) descarta
   o `reason`. O driver morre com `RuntimeError: Debugger unattached` e "a
   tarefa morreu antes de produzir resultado", quando o certo seria "você
   fechou a aba: reabro?".
8. **"Com janela" esquece rápido.** `_asked_local_recently` olha 3 mensagens e
   10 minutos (`executor.py:62–63`). O job do AppDemo nasceu headless 10 minutos
   depois do "usa o navegador local", e o Daniel viu o form parado e disse
   "travou".

## 4. Desenho

### 4.1 Princípio: o job vira uma sessão de trabalho

Continua **um job por navegador**. O que muda é que ele deixa de ser um tiro
único e passa a ser uma sessão de trabalho que:

- tem um conjunto de abas (já tem);
- recebe mensagens enquanto roda (do painel, do chat via cérebro, do cérebro);
- manda mensagens para o chat (avisos e perguntas);
- **espera viva** quando pergunta: aba presa, debugger ligado e histórico na
  memória, sem morrer e renascer;
- só cai na parada de hoje (`waiting` + `continue_job`) se a espera passar do
  teto.

Nova instrução sobre o mesmo trabalho = mensagem para o job, nunca job novo.

### 4.2 Caixa de entrada do job

- Tabela `job_mensagem`: `id`, `job_id`, `origem` (`painel` | `chat` |
  `cerebro`), `texto`, `criada_em`, `lida_em`.
  - Tabela e não `extra_metadata`, porque o runner já escreve no
    `extra_metadata` durante o job, e dois escritores no mesmo jsonb perdem
    escrita.
- Para acordar o job na hora: registro em memória
  `job_id → asyncio.Event`, no mesmo processo do JobManager. O banco garante
  que nada se perde se o core reiniciar.
- **Driver:** no topo de cada rodada (`driver.py:6457`, junto de
  `_conferir_abas`), lê as não lidas e marca como lidas. Elas entram no prompt
  como bloco PRIMEIRO, acima da página:
  `>>> MENSAGEM DO DANIEL (agora, pelo painel): …`.
  - O painel registra "entendi: …" (banner `ok`), para ele ver que chegou.
  - Mensagem nova **renova o orçamento de passos**: instrução nova é trabalho
    novo. O teto absoluto fica no `neo.toml`.
- Diário (`trace.md`) e caderno registram a mensagem com a origem.

### 4.3 Campo no painel da extensão

- **Visual:** uma caixa de texto e "Enviar" no rodapé do painel lateral
  (`content-overlay.js`), dentro do shadow root fechado. Quando há pergunta
  pendente, as opções viram botões acima da caixa.
- **Caminho:** content script → `chrome.runtime.sendMessage({type:"NEO_MSG",
  texto})` → background → `safeSendNotification(peer, "neo.msg", {tabId,
  texto, id, at})` → host nativo → Rust.
  - No Rust, `Some("neo.msg")` ao lado do `neo.stop` (`browser_local.rs:271`)
    empilha numa fila.
  - O snapshot devolve `mensagens: [...]`, esvaziando a fila, e o core grava
    em `job_mensagem` com `origem=painel`.
- **Sem pedido em voo, a notificação não é lida:** o Rust só lê notificações
  dentro de `request()`. Durante a espera de uma pergunta, o driver chama um
  `browser.inbox` barato a cada ~2 s, que também serve de keep-alive.
- **Segurança, e isto não é opcional:**
  - **NÃO usar o DOM** como o PARE usa. Um atributo `data-neo-msg` pode ser
    escrito pela própria página, e virava injeção com a autoridade do Daniel.
    O PARE pode ir pelo DOM porque o pior caso é parar; mensagem não pode.
  - Só vale o que passou pelo canal privilegiado da extensão. O content script
    aceita o envio só com `event.isTrusted`.
  - O shadow root fechado impede a página de ler a mensagem.
- **Cópia no chat:** a mensagem do painel também é salva no histórico da sessão
  (role `user`, `metadata.source="painel"`). O cérebro sabe depois o que ele
  pediu, e o chat mostra a bolha marcada "pelo navegador".

### 4.4 `avisar` e `perguntar` — o job fala com o chat

Duas ações novas do driver, no servidor como `note` e `consultar`:

- **`avisar {texto}`** — não bloqueia.
  - Publica `chat_message` na sessão (reusa `_publish_chat_message`) e um item
    no painel.
  - Freio: no máximo 1 a cada N passos e M por job, constantes no `neo.toml`.
- **`perguntar {pergunta, opcoes?}`** — bloqueia, e é a **espera viva**:
  - publica a pergunta no chat e no painel, com as opções em botões;
  - o job vai para o estado novo **`waiting_user`** (janela de Tarefas:
    "esperando você", como o `waiting` do card), sem soltar a aba e sem
    `release`;
  - espera mensagem na caixa (`asyncio.Event` + `browser.inbox` a cada 2 s)
    até o teto (`neo.toml`, proposta: 10 min);
  - com resposta, segue **no mesmo driver**, com histórico, notas e abas
    intactos;
  - no teto, cai na parada de hoje (`waiting` + `browser_session` +
    `continue_job`), e o aviso diz que a aba continua aberta.
- **A conferência antes do Enviar** (`_check_revisao`) passa a ser um
  `perguntar` interno. O "pode enviar" chega na mesma execução, sem job
  "(autorizado)" novo. A parada de hoje fica só como cair-no-teto.
- A **liberação** de dado pessoal continua no card. Aprovar continua sendo no
  aparelho, e "sim" no chat não vale. A diferença é que, aprovada durante a
  espera viva, ela acorda o job em vez de criar outro.

### 4.5 O cérebro sabe do job e fala com ele

- **Bloco no contexto do turno**, só quando há job de navegador aberto nesta
  sessão: `TAREFA RODANDO NO NAVEGADOR: <título> · passo N · aba ativa A
  (docs.google.com) · abas: A form, B AppDemo · últimas 3 linhas do painel ·
  PERGUNTA PENDENTE: «…»`. Barato e montado pelo código.
- **Tool `job_falar {job_id, mensagem}`:** grava em `job_mensagem` com
  `origem=cerebro`. É como o cérebro responde a pergunta pendente ("ele disse:
  abre o AppDemo também"), corrige o rumo ou repassa o que o Daniel disse no chat.
- **`browser_local_task` com job rodando NESTE navegador:** em vez de "espere",
  a tarefa vira mensagem para o job ("nova instrução: …") e o retorno diz
  isso. Para trocar de verdade há `substituir: true` (cancela o atual e começa
  outro no mesmo conjunto de abas).
  - Na sessão 2bd028fb, "travou, fechei a aba, abre outra" teria virado
    recado para o job: "ele fechou a aba; reabra com janela".
- **Resposta no chat para pergunta pendente:** passa pelo cérebro, e não
  direto ao job. A mensagem pode não ser sobre o job, e o cérebro completa o
  recado com o que sabe (a credencial a usar, o critério de avaliação). Custa
  um turno (~5–10 s). A resposta pelo **painel** vai direto: quem escreve ali
  está falando com o agente da aba.

### 4.6 Reconhecer a página e julgar o que é de julgamento

É a parte que resolve "Conforme em tudo".

- **Reconhecimento por seção:** quando a página muda de TIPO de pergunta
  (perfil → grade de avaliação), o driver monta, pelo código, o que é a seção:
  título, quantas perguntas, tipo e se há enunciado longo nos rótulos. Depois o
  modelo do passo **é obrigado** a abrir a seção com `avisar` ou `perguntar`.
  Isso é decidido pelo código, não por regra no prompt: regra no prompt já
  falhou várias vezes neste driver (caderno vazio, `paralelo` não escolhido).
- **`julgar {perguntas, criterios?, abas?}`:** como `consultar`, mas a
  evidência é a página.
  - Entram o rótulo literal de cada pergunta, o texto dos critérios, o que as
    notas trazem de outras abas (o RF lido no AppDemo, `[aba B]`) e a finalidade do
    pedido.
  - Quem responde é o **modelo do cérebro, com raciocínio**, e não o do passo.
  - Sai, por célula: resposta + uma linha de justificativa + confiança.
  - Confiança baixa vira `perguntar` ou "olhar na aba B".
- **Nota guarda o rótulo:** ao marcar uma pergunta de grade, a nota leva o
  rótulo literal (o enunciado do RF), e o código impede resumir. Foi a falta
  disso que gerou o "o form só dá RF-01".
- **Ação de grade em lote:** `marcar_grade {pergunta, linhas: {Clareza:
  "Parcialmente", …}}` marca uma linha por RF de uma vez. Hoje são 56 passos e
  vários "NÃO ficou selecionado".
- **Conferência** mostra resposta + justificativa por célula, como já mostra
  "da memória" / "⚠ escrito pelo Neo".

### 4.7 Credencial e conta nova no navegador local

- `credentials` no `browser_local_task` passa a valer. O rótulo do cofre vira
  marcador (`«cofre:email-pessoal-do-neo.usuario»`), trocado pelo valor só na
  hora do `fill`, igual ao `«pks:…»` da liberação. O modelo nunca vê o segredo.
- **Conta nova:** a senha é gerada pelo CÓDIGO, e o cofre ganha a credencial
  `<site>` com card de aprovação. O modelo nunca escolhe nem vê a senha.
- **E-mail inventado é barrado:** e-mail digitado tem de vir do cofre, do PKS
  ou da fala do Daniel. Senão é recusa de passo, como `_recusar_dado_literal`.

### 4.8 Aba fechada vira pergunta

- A extensão passa o `reason` do `onDetach` adiante (`target_closed`,
  `canceled_by_user`).
- O driver trata "Debugger unattached" como evento:
  - **aba fechada pelo Daniel:** `perguntar` "você fechou a aba X — reabro ou
    paro?";
  - **detach sem motivo humano:** religa uma vez (o `focus_tab` já religa,
    `browser-multi-aba-plan.md` §15).
- **Em aberto:** os 3 jobs do AppDemo morreram ~1 s depois de digitar o e-mail, e
  na interação 10 o Daniel escreveu "fechei de novo". Falta saber se ele fechou
  a aba nas três vezes ou se foi o Edge (autofill/gerenciador de senha no form
  de cadastro). Registrar o `reason` resolve isso na próxima vez.

### 4.9 "Com janela" vale para a sessão

Depois que o Daniel pediu para ver, todo job de navegador da sessão nasce com
janela, até ele dizer o contrário. Isso troca as 3 mensagens / 10 min de
`_asked_local_recently`. E todo job sem janela abre com `avisar`: "rodando em
segundo plano, sem janela".

## 5. A sessão 2bd028fb com isto

1. "Preenche o form" → `browser_local_task` com janela. A aba A abre o form.
2. Pág. 1 (perfil): `consultar` responde da memória (profissão do
   perfil). O que a memória não tem (tempo de experiência) vai num único
   `perguntar` com as opções da página, e o form fica aberto esperando.
3. Pág. 2 muda de tipo → reconhecimento → `perguntar`: "É a avaliação de 14
   RFs do sistema AppDemo em 4 critérios (C1–C4). O texto de cada RF está aqui.
   Avalio pelo texto, ou abro o AppDemo para conferir o comportamento (precisa de
   cadastro)?"
4. Daniel, pelo painel ou pelo chat: "abre o AppDemo também". O mesmo job faz
   `abrir_aba` no AppDemo (aba B), cadastro com a credencial do cofre e senha
   gerada pelo código (card do cofre), login. Depois `trocar_aba` A.
5. Por RF: `julgar` com o enunciado + C1–C4 (+ nota da aba B, se houver) →
   `marcar_grade`. Na dúvida, `trocar_aba` B, lê, volta.
6. No meio, o Daniel escreve no painel: "verificabilidade: se não diz o que
   acontece no erro, é Parcialmente". Vira bloco no prompt e na próxima
   rodada do `julgar`.
7. Última página: conferência com justificativa por RF, como `perguntar` vivo.
   "Pode enviar" → envia na mesma execução.

## 6. Riscos

- **Injeção:** a mensagem do painel tem a autoridade do Daniel, então tem de
  vir só do canal da extensão (§4.3). Conteúdo de página nunca vira mensagem.
  O cérebro, ao repassar, cita o Daniel e não a página
  (`neo-security-audit-prompt-injection`).
- **Job longo:** espera viva segura o debugger, e a faixa amarela "o Neo está
  depurando" fica. O teto da espera e o do job ficam no `neo.toml`.
- **Spam no chat:** `avisar` com freio por código. O fecho do dispatcher não
  pode repetir o que já foi avisado.
- **Rust e extensão:** pede `tauri dev` reiniciado (nunca build) e **reload
  manual** da extensão (versão nova no manifest).
- **Orçamento renovável vira loop:** a renovação é por mensagem do Daniel, não
  por `avisar` do próprio agente.
- **Chave de produção:** as reencenações do F0 gastam a chave de prod
  (`reference-ab-reenviando-request-do-log`).

## 7. Fases

- **F0 — medir antes de construir.**
  - Reencenar o passo 8 do job `14ed2795` (a pág. 2 como o modelo viu) com o
    modelo de produção, 6×, com `perguntar`/`julgar` no vocabulário: quantas
    vezes ele pergunta ou julga em vez de "Conforme em todos"?
  - Reencenar o `julgar` com as 14 grades, com o modelo do cérebro: as
    respostas variam por RF? A justificativa cita o enunciado?
  - Ligar o log do `reason` no `onDetach` (1 linha, a extensão precisa de
    reload) e perguntar ao Daniel se ele fechou as abas.
- **F1 — caixa de entrada + campo no painel** (§4.2, §4.3).
  - Mensagem do painel muda o rumo do job em ≤ 1 rodada.
  - E2E: no meio de um job, escrever "volta para a página anterior".
- **F2 — `avisar` e `perguntar` com espera viva** (§4.4).
  - Estado `waiting_user`; resposta pelo painel retoma no MESMO driver; teto
    cai na parada de hoje.
  - A conferência do Enviar vira `perguntar`.
- **F3 — cérebro ciente** (§4.5).
  - Bloco "tarefa rodando", `job_falar`, `browser_local_task` com job rodando
    vira mensagem.
  - E2E: responder a pergunta do job pelo chat.
- **F4 — reconhecimento + `julgar` + nota com rótulo + `marcar_grade`** (§4.6).
- **F5 — credencial no navegador local + conta nova + trava de e-mail
  inventado** (§4.7).
- **F6 — E2E no mesmo form da UFC** (§5), parando antes do Enviar.
  - Aceite: grade com respostas diferentes por RF, justificativa citando o
    enunciado, AppDemo aberto na aba B o tempo todo, pelo menos uma mensagem pelo
    painel obedecida.

F1–F3 são o "conversar sem interromper". F4 é o que faz a resposta prestar.
F5 destrava site com login.

## 8. Decisões em aberto

1. Resposta pelo chat a uma pergunta do job: pelo cérebro (proposta, §4.5) ou
   direto ao job?
2. Teto da espera viva antes de cair na parada de hoje: 10 min?
3. Quem julga no `julgar`: o modelo do cérebro do turno (proposta) ou um modelo
   fixo com raciocínio no `neo.toml`?
4. Ordem: F1+F2 primeiro (fica de pé sem o cérebro), depois F3? Ou F4 primeiro,
   porque é o que estragou o form?

## 9. Registro da implementação (2026-11-11)

Implementado: F1 (caixa + painel), F2 (`avisar`/`perguntar`/conferência viva), F3
(cérebro ciente) e §4.8 (aba perdida). Testes: `tests/browser_local/test_conversa.py`
(31), Rust `fila_do_painel_guarda_corta_e_esvazia` e
`debugger_solto_diz_o_motivo_quando_sabe`, extensão `scripts/check-conversa.mjs` e
`scripts/testar-conversa.py` (Chromium headless, gestos confiáveis por CDP).

Desvios do desenho, e por quê:

- **Caixa em memória, sem tabela** (`core/browser_local/conversa.py`). O driver roda
  no processo do neo-core e morre com ele: mensagem guardada para um driver que não
  existe mais não teria quem a lesse. Sem migração no banco de produção. Tarefa que
  já parou (`waiting`) continua pelo `continue_job` ou por um pedido novo.
- **Sem estado `waiting_user`.** Esperando viva, a tarefa continua `running`, com
  `progress_note = "esperando você: …"`. Um estado novo exigiria ensinar a janela de
  Tarefas e a reconciliação do boot; o `running` já é a verdade (o driver está vivo).
  No teto (`espera_min`), cai em `waiting` com a instrução de retomada para o cérebro.
- **A pergunta do painel nasce por um canal privilegiado.** Achado na implementação:
  o painel recebe eventos por `window.postMessage`, que a própria página consegue
  imitar. Uma pergunta falsa com botão "Sim" ao lado da verdadeira ("posso excluir a
  conta?") viraria autorização forjada. Por isso:
  - o Rust, além do `postMessage`, chama `neoPergunta` na extensão;
  - o background guarda a pergunta e a entrega por `chrome.tabs.sendMessage`;
  - só essa pergunta ganha botões; item "pergunta" vindo da página é texto na lista;
  - o `NEO_MSG` só leva `opcao` se o botão for da pergunta guardada, com o mesmo texto;
  - o content script nunca escreve a pergunta no estado (`NEO_OVERLAY_SET` a descarta).
- **Autorização** = botão da 1ª opção, `job_falar` com `autoriza: true`, ou resposta
  escrita só com palavras de concordância ("sim, pode enviar"). "pode, mas antes
  troca o RF-03" não autoriza: é instrução.
- **`browser_local_task` com tarefa viva vira recado**, salvo quando é a MESMA tarefa
  dita de novo (sobreposição de palavras ≥ 80%): o cérebro às vezes repete achando
  que não pegou, e "nova instrução: <a mesma tarefa>" faria o agente recomeçar.
- **Roteador:** com o bloco da tarefa viva no `system_context`, uma decisão de
  `browser_task`/`browser_local_task` vai para o cognitivo (`_bias_local_browser`),
  que é quem tem `job_falar`. Pedido de outra coisa (clima) segue como estava.
- **Aba perdida:** o snapshot que falha com "Debugger unattached"/"No tab" vira
  pergunta ("reabro e continuo?") com caixa, ou `failed` com mensagem clara e o
  caderno, sem ela. O Rust passa a anexar o motivo do `onDetach` ao erro.

Constantes em `config/neo.toml`, seção `[navegador.conversa]`.

Publicar: neo-core sai pelo Mutagen; o Rust pede o `tauri dev` reiniciado (salvar o
`.rs` já recompila); a extensão vai para 0.3.0 e pede **reload manual** em
edge://extensions. Qualquer ordem funciona: core novo com desktop antigo cai em
`_sem_inbox` (só o chat responde); desktop novo com extensão antiga ignora o
`neoPergunta` desconhecido.

Falta: E2E (§7 F6), F4 (`julgar`, nota com o rótulo, `marcar_grade`), F5
(credencial no navegador local), §4.9 ("com janela" vale para a sessão).

### 9.1 Primeiro teste real (sessão 0b26ca19, job 3ce63142, 2026-11-11 21:06–21:12)

Funcionou, medido no diário e nos dois logs:
- 2 `avisar` e 5 `perguntar` chegaram ao chat como `chat_message`;
- as 5 respostas foram pelo painel (4 botões e 1 texto digitado), chegaram pelo `browser.inbox` e o mesmo driver seguiu com histórico, caderno e abas. Esperas de 10–117 s;
- o orçamento cresceu a cada mensagem (25 → 110 passos).

O agente PERGUNTOU em vez de chutar a grade ("Abra o sistema / Deixe em branco / Conforme").

Barrou: o `abrir_aba` para `sistema-exemplo.vercel.app` foi recusado 3× por ESCOPO, mesmo depois de "Autorizo abrir sistema-exemplo.vercel.app". O escopo só nascia do texto da tarefa e do `start_url`, e a recusa dizia "me peça de novo dizendo o site" — conselho para o cérebro, não para o agente. Corrigido às 21:17 UTC (`_ampliar_escopo`):
- site citado numa mensagem do Daniel entra no escopo;
- site citado numa pergunta que ele ACEITOU (1ª opção ou concordância) também;
- texto da página nunca amplia;
- a recusa, com caixa, ensina a `perguntar` citando o site;
- o `perguntar` proíbe pedir senha, código ou documento (fica no histórico).
5 testes.

Outros achados:
- **PKS:** `full_name` = "Profissão De Exemplo". O `consultar` devolveu isso para "Nome" (com aviso), e o agente preencheu. Dado corrompido, não é bug do fluxo;
- P6 "5 anos ou mais" saiu de `years_experience: 5` (experiência geral, não de ER), porque ele escolheu "o mais provável";
- o 1º `avisar` e o 1º `perguntar` repetiram o mesmo relatório no chat;
- a grade continua 1 clique por célula, com o falso "NÃO ficou selecionado" → F4 `marcar_grade`;
- `browser.inbox` loga em INFO a cada 2 s no Neo Desktop.log → baixar para debug na próxima mudança do Rust;
- próximo obstáculo do mesmo caminho: o AppDemo pede cadastro/login → F5.

Não exercitado: `job_falar` pelo chat, conferência do Enviar viva e aba fechada.

### 9.2 Segundo teste (sessão c469e4c7, jobs 9ec89de0 e ed10f77c, 2026-11-12 00:29–00:34)

O agente não perguntou as decisões, porque o CÉREBRO já as tinha tomado na tarefa. Depois do "continue e tente de novo", a tarefa do 2º job mandava:
- preencher as obrigatórias "com respostas PROVISÓRIAS e neutras";
- tempo de experiência "De 1 a menos de 2 anos";
- "Não Aplicável" em todas as células.

A frase dele ("qualquer decisão que for minha, me pergunta") estava só na 1ª mensagem e nunca chegou ao agente: o `pedido` do job é a mensagem do turno e só serve à conferência.

O agente perguntou uma vez, quando ele escreveu "espera" no painel, e obedeceu ao "no nome coloque daniel morais".

Medido, reencenando o passo 1 do ed10f77c com o modelo de produção (`scratchpad/medir/reencenar_c469.py`, sessão do Claude 1962c3d3):

| Variante | Pergunta antes de marcar |
|---|---|
| prompt antigo | 0/6 |
| + bloco "O QUE O DANIEL PEDIU" no topo + regra no sistema | 0/6 |
| + aviso detectado pelo código, colado antes da PÁGINA AGORA | **6/6** (todas listam os palpites) |

Corrigido em 2026-11-12 00:43 UTC:
- `spec["falas"]` (`_falas_do_daniel`, executor): as falas dele da última hora, sem as continuações automáticas;
- o bloco das falas acima da TAREFA;
- `_aviso_de_palpite`: as falas pedem consulta (`_PEDE_CONSULTA`) ∧ a tarefa traz palpite (`_PALPITE_NA_TAREFA`) ∧ há caixa → ATENÇÃO até o 1º `perguntar`.
5 testes.

Ainda do cérebro: ele mesmo escreveu os palpites, contra o pedido. Agora o agente segura, mas o cérebro não aprendeu.

O 1º job "leu as páginas 1 e 2" sem preencher porque o Google guarda o RASCUNHO das respostas na conta dele entre os testes (a pág. 1 já vinha marcada).

O escopo da §9.1 não foi exercitado: o agente nem tentou o AppDemo.

### 9.3 Escrever no painel (relato do Daniel, 2026-11-12)

O relato:
- a cada passo o cursor saía do campo e ele tinha de clicar de novo;
- quando a página recarregava, o campo limpava.

Três causas:
1. o agente age NA MESMA ABA: cada clique dele foca outro elemento;
2. para cada print o Rust escondia o painel com `visibility:hidden`, e esconder um campo focado tira o foco dele;
3. o "Avançar" do Google Forms recarrega a página, e o painel remontava com o campo vazio.

Havia ainda um risco escondido: tecla do agente (Enter) com o foco no campo dele mandaria o rascunho pela metade.

Corrigido em 2026-11-12 00:57 UTC; extensão 0.3.1, que pede reload manual:
- **o agente espera enquanto ele escreve:**
  - o painel carimba `data-neo-digitando` a cada tecla e, com foco e rascunho, a cada segundo, até 30 s sem tecla;
  - o Rust segura cada ação de atuação por até 8 s (`esperar_quem_digita`) e devolve `DIGITANDO:`;
  - o core espera e tenta de novo até `digitando_espera_max_s` (90 s, neo.toml), com um aviso no painel;
  - o carimbo é forjável pela página, mas o pior caso é segurar o agente (como o PARE);
- **print por opacidade:** `JS_PAINEL_OCULTAR` usa `opacity:0`;
- **tecla do agente não cai no rascunho:** antes de agir o Rust carimba `data-neo-agindo`, e o painel tira o foco do campo sem apagar o texto;
- **rascunho sobrevive à navegação:** `NEO_RASCUNHO` guarda por aba no `storage.session`, e o painel remontado devolve o texto e o cursor (se a última tecla foi há menos de 15 s).

Testes:
- `testar-conversa.py` §5–5c, no Chromium headless;
- 2 do core (espera e teto);
- `testar-painel.py` passou a esconder por opacidade.

### 9.4 Terceiro teste (sessão 58b84b87, job e81b0912, 2026-11-12 01:03–01:13)

Foi longe:
- 7 perguntas, todas pelo painel;
- página 1 preenchida com as respostas DELE;
- na página 2 ele aceitou abrir o sistema, e o **escopo ampliou** (§9.1 funcionando): o AppDemo abriu na aba B, com o form vivo na A;
- no login do AppDemo o agente **perguntou** ("Tenho conta / conta de teste fictícia / …"), e ele escolheu a conta de teste.

Quatro defeitos:
1. **"Cadastrar" do AppDemo é a ABA "Entrar | Cadastrar"** (Radix Tabs, com `checked` no snapshot), e "cadastrar" está no `_ENVIO`: trocar de aba disparou a conferência de envio;
2. **a conferência misturou os formulários:** listou TCLE/LGPD/escolaridade do Google Forms (aba A) como se fossem do cadastro do AppDemo (aba B). `_preenchidos` e as respostas da memória eram por TAREFA, não por aba;
3. **"pode seguir para o cadastro" não autorizava** (para/o/cadastro fora da lista): três respostas até o "sim";
4. **"Debugger unattached (a aba foi fechada)" 1 s depois do `fill` do e-mail no cadastro do AppDemo** — a 4ª vez em 4 nesse ponto, agora com o motivo (`target_closed`). A aba NÃO fechou: o `_conferir_abas` antes do snapshot ainda a listava (senão o diário diria "aba fechou sozinha"). Causa no Edge não confirmada. E a resposta dele ao "reabro?", "falta a senha e confirma a senha", foi lida como parar.

Corrigido em 2026-11-12 01:21 UTC (só core):
- `_eh_alternancia`: alvo com `checked` (aba, rádio, toggle) não dispara a conferência de envio;
- `_preenchidos[*]["aba"]` + `_consulta_aba`: a conferência e o "precisa conferir?" olham só a aba ativa;
- `autoriza_a_acao(rotulo)`: aceita conectivos e o radical das palavras do rótulo ("cadastr…"). "mas", "antes" e "não" continuam barrando, e exige ao menos uma palavra de concordância;
- `_aba_perdida` primeiro RELIGA a mesma aba (`focus_tab` → `attach`, até 3× por tarefa), sem perguntar; e só para com um NÃO explícito.
18 testes novos (56 na conversa).

### 9.5 Quarto teste (sessão 74408e70, job 42a4ffd4, 2026-11-12 01:53–01:59)

A causa do "Debugger unattached" do AppDemo apareceu: desta vez o religar tentou, e o navegador recusou com `Cannot access a chrome-extension:// URL of different extension`. Ao focar o campo de e-mail, OUTRA extensão (quase certamente um gerenciador de senhas) injeta o menu dela num iframe `chrome-extension://`. O Chrome solta o debugger de aba com página de outra extensão e recusa religar enquanto ela estiver lá. Foram 5 de 5 vezes logo depois do `fill` do e-mail, e o `js_fill` fazia `el.focus()`. Não deu para listar as extensões do Edge (o TCC do macOS bloqueia ler o perfil).

Outros dois defeitos:
- **o "continue" no chat** virou um `browser_local_task` com a tarefa REESCRITA pelo cérebro ("respostas neutras mínimas de teste…"), entregue como recado. O "PARE e pergunte" no meio dela foi lido como "pode parar" pela regex do `_aba_perdida`;
- **"respondendo sem análise real":** o agente olhou só a tela de login do AppDemo, fechou a aba e marcou "Conforme". É o F4.

Corrigido em 2026-11-12 02:14 UTC:
- Rust `js_fill`: campo de login (type email/password, autocomplete user/email/password, name/id e-mail/senha/login/usuário) NÃO recebe foco. O valor entra pelo setter nativo + `input`/`change`. Teste com `node --check` do JS gerado;
- core `_religar`: distingue `outra_extensao`. Aí pergunta "feche o menu do gerenciador de senhas" e religa na MESMA aba (sem reabrir pela URL, que perderia o form);
- `_quer_parar`: só o botão "Pode parar" ou uma resposta curta (≤ 60 caracteres);
- `_recado_para_tarefa_viva`: o recado leva as palavras DELE; a reescrita do cérebro só sem fala no turno.

### 9.6 Quinto teste (sessão 74408e70, interação 3, job 02f46eff, 2026-11-12 02:18–02:25)

Funcionou:
- **cadastro no AppDemo sem "Debugger unattached"** — confirma a causa da §9.5 (o foco no campo de e-mail);
- login, Perfil e evidência anotada de cada RF ("RF-02 EXISTE: nome 'Neo Teste', e-mail, 'Membro desde 11/11/2026'"…), com o ícone de câmera do RF-05 confirmado pela visão;
- a espera enquanto ele digita: um clique esperou 12 s.

Falhou:
- **julgamento:** "Conforme" em tudo, com o raciocínio "o requisito existe e foi verificado no AppDemo". Ele confunde a funcionalidade EXISTIR com o requisito estar claro, sem ambiguidade, completo e verificável (C1–C4). A mensagem dele "você tá realmente analisando… com seriedade" chegou e o passo seguinte a ignorou. → F4;
- **"Pode enviar" vazou:** a resposta à conferência do botão "Cadastrar" ficou no bloco de mensagens como instrução solta, e o agente a leu como "pode enviar o formulário do Google" (perguntou antes, por sorte);
- **escopo ampliado para "email.com"**, a partir do texto da conferência ("seu@email.com");
- **"NÃO ficou selecionado" falso** ~15× na grade do Google Forms: o `fimDoClique` lia o `aria-checked` no instante do clique, e o Google o vira um pouco depois. A visão via marcado e o DOM dizia desmarcado.

Corrigido em 2026-11-12 02:32 UTC:
- `Mensagem.resposta_a` / `confirmou`: no bloco, a resposta mostra a pergunta que respondeu, e a de conferência diz "vale SÓ para aquele clique";
- pergunta de conferência não amplia escopo. A exclusão de e-mail no `_hosts_citados` foi tentada e DESFEITA: o corpus de produção (`test_escopo_navegacao`) mostra que o domínio do e-mail da tarefa entra no escopo de propósito, para abrir a caixa e confirmar cadastro;
- Rust `NEO.fimDoCliqueAssentado`: relê o estado em 120 e 300 ms quando a 1ª leitura é "não". Teste com Chromium de verdade (`radio_que_assenta_depois_do_clique_conta_como_selecionado`). `neo-map.generated.js` regerado.

### 9.7 Três tentativas que nem abriram a aba, e a senha que se perdeu (2026-11-12 19:05–19:23)

Sessões 41a690ba, f6a0be50 e 294e4ec5, todas com o prompt de teste da §9.6. O Neo respondeu "a extensão do Edge está offline", e o navegador estava ligado.

**Causa: roteador.** `_is_music_request` aceitava o verbo e o substantivo em qualquer ponto da mensagem. "…espera com a ABA aberta. …PARA avaliar de verdade…" virou `skill_hint=music`. O turno de música tira `browser_local_task`/`browser_task`/`run_command` da mesa (`_drop_music_turn_temptations`). Sem a tool, o cérebro:
- (41a690ba e 294e4ec5) chamou `list_connected_devices`, viu o "edge macbook" antigo (neo-browser-ext 0.2.1, offline desde setembro) e concluiu "extensão offline";
- (f6a0be50) chamou a tool que não estava na mesa, sem esquema, com `tarefa` em vez de `task`, e recebeu "task vazia.".

Na 294e4ec5 ele também recusou criar a conta ("fabricar a evidência"), e o "olha ai" da 41a690ba virou o job ceb03e52. Nesse job, o classificador reescreveu o pedido como "LEITURA COMPLETA, NÃO preencha". O Google Forms não avança com pergunta obrigatória vazia, o agente rodou entre a página 1 e `?page=N`, e o Daniel apertou PARE.

**Senha (job 02f46eff, §9.6).** O cérebro inventou na tarefa "neoteste<numero>@mailinator.com" e "senha provisória Mes@2026teste". O snapshot trazia o VALOR do `input(password)`, e o `<label>` repetia. A senha ficou em claro no caderno, na conferência e em dezenas de passos do diário. Nada foi para o cofre.

Corrigido em 2026-11-12 19:44 UTC (core apenas; desktop e extensão sem mudança):
- `_music_object_near_verb`, só estreitando:
  - verbo e objeto na mesma frase, com o substantivo a até 6 palavras depois (ou 3 antes);
  - "para" + infinitivo é preposição;
  - aba/guia só conta com verbo de SOM.
  Medição no corpus de produção NÃO feita (leitura do banco negada pelo modo auto); os testes de música continuam todos passando;
- `_task_do_apelido`: `tarefa`/`instrucoes`/`objetivo` valem como `task`, e o erro diz o nome do parâmetro;
- **F5, `core/browser_local/cofre.py`:**
  - `«senha:nova»`: senha gerada pelo código, uma por site por tarefa. Vai para o cofre ANTES da página (`password`, username = o login digitado no form, domain = host) e nunca sobrescreve: rótulo repetido ganha sufixo;
  - `«cofre:rótulo»`: só para o domínio da credencial e só em `type=password`;
  - senha à mão em campo de senha é recusada;
  - caixa pública (mailinator etc.) é recusada quando o cofre tem o e-mail do Neo;
  - o bloco COFRE vai no prompt só em página com campo de senha;
  - a parede de login não encerra quando há login guardado;
  - 🔐 avisa no chat na hora e no resumo final;
- `_sem_senhas` no `_render`: o valor do campo de senha (e de qualquer elemento que o repita) não chega ao modelo nem ao diário;
- descrição do `task` do `browser_local_task`: conta = "crie com o e-mail do Neo", sem senha nem e-mail na tarefa.

Ainda aberto:
- `list_connected_devices` não diz que o navegador local mora no desktop, e o device velho da extensão engana o cérebro;
- o classificador ainda reescreve pedido curto ("olha ai") como tarefa própria;
- a recusa ética da 294e4ec5.

### 9.8 Sexto teste (sessão 697e0628, job 2f320de8, 2026-11-12 19:51–20:01)

Funcionou:
- **roteador** (§9.7): `skill_hint` nulo e `browser_local_task` no primeiro turno;
- **as perguntas certas:**
  - a página 1 é obrigatória e trava o Google Forms: ele perguntou, com opções;
  - o `consultar` achou o curso e ele perguntou os 3 dados que a memória não tinha;
  - pediu para abrir o AppDemo fora do escopo e abriu na aba B;
- alternou A↔B ~10 vezes sem fechar nenhuma;
- esperou o Daniel escrever 2 vezes;
- **0 "NÃO ficou selecionado"** em 73 cliques de grade (na §9.6 foram ~15): o `fimDoCliqueAssentado` resolveu.

Falhou:
- **F5 não exercitado:** o AppDemo já estava logado com a conta de ontem ("Neo Teste", mailinator), então não houve cadastro;
- **o cérebro de novo reescreveu "Preenche" como "LER… SEM responder nada"**. O agente contornou perguntando;
- **julgamento — 70 de 70 marcações "Conforme".** A evidência anotada não casa com o RF:
  - RF-16 ("botão 'Iniciar Maratona'") "CONFIRMADO (inputs por célula)", e "Iniciar Maratona" nunca apareceu na tela (o AppDemo estava em "Maratona bloqueada — aguarde o moderador");
  - RF-17 ("Dicas Bloqueadas") confirmado com "dicas numeradas visíveis";
  - RF-13 (progressão de níveis) confirmado com o bloqueio do moderador.
  O "ta me parecendo que esses últimos vc não conferiu lá" do Daniel estava certo → F4;
- **orçamento:** 125 passos acabaram na página 5 de 9. São 4 cliques por RF (e o ref 55 clicado 3 vezes) → `marcar_grade` no F4;
- **UMA pergunta do Daniel no painel (19:55) virou 4 "Sim, Daniel — estou conferindo de fato" no chat.** A mensagem fica no bloco até o fim e nada dizia que já tinha resposta.

Corrigido em 2026-11-12 20:07 UTC: `_marcar_respondidas`. O `avisar`/`perguntar` marca as mensagens espontâneas anteriores, e o bloco mostra "✓ você JÁ respondeu no chat («…»): não responda de novo — se ele pediu algo, siga FAZENDO". A resposta a uma pergunta da tarefa não é marcada.

### 9.9 F4 — `julgar` e `marcar_grade` (2026-11-12 20:25 UTC)

Decisão do Daniel (§8.3): **um modelo mais forte só para o julgamento**, no `neo.toml`. Fica em `[navegador.julgamento]`:
- `modelo = "anthropic/claude-sonnet-5.5"` (US$ 2/M de entrada, US$ 10/M de saída no OpenRouter);
- raciocínio de 4000 tokens;
- página de até 12000 caracteres e 4000 por outra aba;
- 150 s de teto.

O que entrou:
- `core/browser_local/grade.py`: lê a grade do snapshot (pergunta ← elemento do enunciado; linha ← `radiogroup`; célula ← "Coluna, resposta para Linha") e o que já está marcado. Uma pergunta acaba quando o rótulo da linha se repete ou quando aparece outro enunciado. `enunciado_inteiro` pega o texto sem o corte de ~120 caracteres do snapshot;
- `core/browser_local/juiz.py`: uma chamada por página. O juiz recebe:
  - o texto da página, por `browser.extract`, com as definições C1–C4;
  - as perguntas com linhas e colunas exatas;
  - de cada OUTRA aba, o texto atual e as notas `[aba X]`, rotuladas como "resumos dele — podem ter conclusões erradas";
  - as falas e mensagens do Daniel.
  A resposta é conferida contra a grade: coluna que não existe não vira clique;
- **driver:**
  - `julgar` (o subagente não pode) e `marcar_grade` (uma pergunta inteira por ação, pelos `click` de sempre);
  - nota escrita pelo código com o ENUNCIADO e o veredito;
  - célula com origem `juiz` na conferência ("julgado: porquê");
  - linha trocada pelo agente sai "⚠ escrito pelo Neo";
  - `_aviso_de_grade` antes da PÁGINA AGORA, enquanto houver pergunta aberta;
  - trabalho de grade NOVO devolve o passo (`_bonus_grade`), e repetição não devolve.

Medido com a página 2 REAL do job 2f320de8 e as notas da aba B daquele job (`scratchpad/medir/medir_f4.py`):
- **passo** (deepseek v4.1 flash, prompt novo):
  - `julgar` em 4/6;
  - 1 `marcar_grade` sem julgamento, que o código recusa e manda julgar;
  - 1 `avisar`.
  No job real, o passo foi `click` "Conforme";
- **juiz** (Sonnet 5.5, 2 rodadas de 22–25 s): vereditos diferentes por RF e critério, com justificativa citando o enunciado. Ele derrubou o "CONFIRMADO" do agente:
  - RF-04: a tela descrita tem "Selecionar Equipe Existente", não a lista com "Nenhuma equipe";
  - RF-02: o e-mail e o "Membro desde" não aparecem na descrição;
  - RF-06: o enunciado diz "a inicial", e o avatar mostra "NT".
  18 de 24 células iguais entre as rodadas; as que mudaram ficaram entre Parcialmente e Não Conforme.

### 9.10 O juiz vira slot do neo-panel, com Haiku 5.5 de padrão (2026-11-13 00:25 UTC)

Pedido do Daniel: Haiku 5.5 e escolha por select no neo-panel.
- **Slot `browser_local_judge`** em `modules/model_config/store.py`: aparece sozinho na aba Modelos ("Navegador local — juiz da grade de avaliação"), com padrão `openrouter · anthropic/claude-haiku-5.5`. O runner resolve o slot (`_resolver_juiz`) e passa provider e modelo ao driver. O `[navegador.julgamento]` do `neo.toml` ficou só com os números (raciocínio, tetos, timeout);
- **`RoleSpec.sugeridos`** (`suggested_models` na API) e `providersFor` no `ModelosTab.tsx`: o select do juiz oferece Haiku, Sonnet e Opus 5.5 no topo, mesmo fora do catálogo. Cadastrar os três no catálogo pelo `create_model` foi negado pelo modo auto (escrita no banco de prod), e o catálogo só é semeado com o banco vazio.

Haiku × Sonnet, mesma página 2 real do job 2f320de8, 2 rodadas cada:
- "Conforme": Haiku 15 e 19 de 24 células; Sonnet 8 e 9;
- Verificabilidade: o Haiku deu "Conforme" nas 12 células;
- células iguais entre as rodadas: Haiku 20/24, Sonnet 18/24;
- 18–19 s por página, contra 22–25 s;
- os dois derrubam o "CONFIRMADO" falso do agente no campo de evidência (RF-02, RF-04, RF-06);
- o Haiku às vezes contradiz a própria evidência: RF-04 com Completude "Conforme" e a evidência dizendo que a lista com "Nenhuma equipe" não apareceu.

### 9.11 Sétimo teste — o formulário quase inteiro (sessão f0b07b8e, job a2242336, 2026-11-12 21:13–21:29)

Primeiro E2E de F4 + F5, ainda com o juiz Sonnet 5.5 (antes da troca da §9.10). 149 passos em 16 min; acabou no teto de 150 (`passos_teto`).

Funcionou:
- **F5 de ponta a ponta:** cadastro no AppDemo com o e-mail do Neo, senha gerada e guardada («sistema-exemplo.vercel.app · neo@example.com · Neo 12/11 18:16»), igual no "confirmar", login depois com `«cofre:…»`;
- **F4:** 15 `julgar` e 54 `marcar_grade`, com 49 perguntas julgadas (RF-01–33, RNF-01–18) e 192 células;
- **obedeceu ao painel:** "não esquece de reavaliar os primeiros RF" fez ele voltar às páginas 2–4 e refazer com o juiz.

Os vereditos:
- 38% Conforme, 54% Parcialmente, 8% Não Conforme;
- Completude "Parcialmente" em 39 de 47; o padrão C/P/P/P aparece em 15 de 49;
- justificativas citam o enunciado, e "sem evidência" sai quando o AppDemo estava "Aguardando Moderador";
- achados reais:
  - RF-12: "etc." e 4 níveis contra Fácil/Médio/Difícil;
  - RF-06: "a inicial" contra "NA";
  - RF-11: "Trocar Equipe" não existe na tela;
  - RNF-05: 4 níveis contra 3 rótulos.

Defeitos:
- **RF-15 e RF-16 ficaram com "Conforme" ×4 da tarefa anterior** (rascunho do Google), sem juiz: o aviso só olhava pergunta POR COMPLETAR;
- **2 cliques no ref que era "Voltar"/"Avançar" na página anterior** caíram no rádio da Verificabilidade do RF-17. O agente percebeu e corrigiu no passo 122;
- **3 respostas do juiz com aspas duplas sem escape** ("Expecting ',' delimiter"): cada uma custou um passo, e o RNF-19–21 ficou sem veredito;
- **RNF-09 voltou com 2 das 4 linhas.** O `julgar` seguinte respondia "já julgada", e foram 5 `marcar_grade` "nada novo"; no fim, o próprio agente escreveu as 2 linhas;
- **onde foram os passos:**
  - ~19 percorrendo páginas no começo (o cérebro pediu "NÃO responda nada", e o rascunho velho deixava avançar);
  - ~17 no cadastro, login e exploração do AppDemo;
  - ~40 refazendo as páginas 2–4;
  - ~10 desperdiçados.

Corrigido em 2026-11-13 00:48 UTC:
- juiz: cita com « », e o JSON ilegível ganha uma segunda chance dentro do `julgar` (`JuizIlegivel` leva o cru ao diário);
- `_julgamento_pendente`: veredito parcial é julgado de novo, e as linhas se juntam;
- `_aviso_de_grade` lista as "já preenchidas, mas SEM julgamento nesta tarefa", só depois que a tarefa já julgou algo;
- `_celula_julgada_trocada`: `click` que troca uma célula julgada é recusado ("NÃO é botão: é o rádio «…» do RF-17").

### 9.12 Oitavo teste — o form nem abriu (sessão d9fae447, 2026-11-13 12:23–12:26)

Nenhuma tarefa nasceu. Os dois turnos saíram do trilho antes do navegador.

**Turno 1, "Preenche esse formulário no meu navegador local…":**
- o classificador (GLM-5.3, 56 s) devolveu sem tool; `_bias_local_browser` promoveu ao cognitivo, mas o sinal `navegador_do_usuario` só ligava quando o CLASSIFICADOR escolhia `browser_local_task`. Sem ele, o aviso "chame `browser_local_task` agora, não responda com resultado antigo" (8/8 na sessão 455646dd) não entrou;
- o índice do caderno listou `[[sessao-898e8fc0]]` (a auditoria "sem alterar nada" de outra conversa, 4 RFs divergentes) e `[[sessao-7bc7caa1]]` sob "seu caderno da tarefa atual", sem dizer que eram de outra conversa. O `conversation_recall` ainda trouxe a mensagem de 898e8fc0 com a linha a mais "avaliar as respostas e conferir";
- a nota de skill disse "⚠️ Esta mensagem casa com `docx` (casou: formulario)". O cérebro leu a skill e a descartou ("não é .docx");
- resposta: "corrijo as divergências ou deixo como está?", sobre a auditoria da outra conversa.

**Turno 2, "eu limpei o formulario para vc preencher do zero":**
- o classificador ACERTOU: `CLIENT_TOOL`/`browser_local_task`, com `task`, `start_url` e `visible`, mas também mandou `needs_plan=true`;
- a guarda de catálogo (tool fora do catálogo de client tools → cognitivo) não roda com `needs_plan`, então o CLIENT_TOOL foi ao planner, que refez o pedido a partir da mensagem crua: `ls ~/Documents`, `find`, `cat ~/Documents/form.html` (uma prova antiga) e `python3 -c "…selenium…"`, aprovado, que falhou com `No module named 'selenium'`.

Corrigido em 2026-11-13 12:38 UTC:
- `_parse_classification`: `browser_local_task` em CLIENT_TOOL/SERVER_TOOL vai ao cognitivo com `needs_plan=False`, com ou sem plano, e o sinal continua ligado;
- `_bias_local_browser`: o portão (`local_browser_requested`) liga `navegador_do_usuario` também quando o classificador não escolheu a tool, só com `speech_act == "imperative"` e sem tarefa viva (com tarefa viva o caminho é `job_falar`);
- `render_index(session_id=…)`: nota de outra conversa sai "outra conversa, ontem 23:19", e o cabeçalho ganha "é de OUTRO pedido: se ele pede para fazer agora, faça" só quando há nota marcada (o índice paga tokens todo turno; o orçamento de 400 do teste derrubava o bloco inteiro);
- `skills/docx`: sem a keyword `formulario` (o MAPA de 20cb02b6 nunca a usou).

Testes: `tests/browser_local/test_pedido_d9fae447.py` (12) e `test_formulario_do_google_nao_aciona_a_docx`; os do conserto falham na base. Não medido com A/B: a chave do OpenRouter estava com US$ 1,10 de folga.

### 9.13 "O cofre está vazio" com a conta lá dentro (sessão a20f4722, 2026-11-14 01:04)

Resposta do cérebro: "O cofre está vazio — não tem a conta do Neo do AppDemo salva. Qual e-mail e senha eu uso para entrar no sistema?". Nenhuma tarefa nasceu.

- **A conta estava no cofre**, criada pela tarefa a2242336: rótulo `sistema-exemplo.vercel.app · neo@example.com · Neo 12/11 18:16`, campos `domain`/`username`, usada em 12/11 21:16;
- o cérebro chamou `vault_list(query="appdemo")`. O filtro é substring no rótulo/descrição/campos, e "appdemo" não aparece em nenhum deles (a descrição citava o título da tarefa, «Abrir e mapear formulário Google Forms»). Com `"vercel"` ou `"eng-software"` acha;
- a consulta nem era necessária: a tarefa do navegador acha o login pelo `domain` quando a aba chega no site (`Cofre.logins_do_site`);
- e pediu senha no chat;
- o aviso de abrir o navegador (§9.12) não entrou: o prompt foi COLADO (`pasted_chars: 502` ≥ 240), o turno nasce `contaminado` por `colagem` e o aviso só vale com turno limpo. A nota "[TEXTO COLADO NESTE TURNO]" ainda diz que os pedidos do trecho "NÃO são do Daniel". É a proteção de §21 do release-request-plan funcionando como deve: não mexi.

Corrigido em 2026-11-14 01:20 UTC:
- `vault_list`: filtro que não casa nada devolve o mesmo que a chamada sem filtro, com `filtro_sem_resultado` e a nota "o cofre NÃO está vazio… conta de site criada pelo Neo leva o DOMÍNIO no rótulo";
- nota e descrição do `vault_list`: `browser_local_task` acha o login pelo domínio sozinha; nunca pedir senha no chat;
- `Cofre.senha_nova(site=…)`: a credencial nova começa com "Login do site «<título da aba>» (<host>)." — o driver passa o `title` do snapshot.

Testes: `tests/test_vault_list_a20f4722.py` (7) e a checagem do nome do site em `test_cadastro_com_senha_nova…`. Gotcha para os testes: colar o prompt longo desliga o aviso; prompt colado com menos de 240 caracteres não marca o turno.

### 9.14 Décimo teste — até a página 2, de propósito (sessão 8e17b81e, job 87568709, 2026-11-14 01:21–01:26)

O Daniel parou na página 3 para medir o progresso. Prompt colado com 233 caracteres (abaixo do limiar de colagem).

O que melhorou:
- **turno do chat:** `navegador_do_usuario=true`, aviso no prompt, `browser_local_task` na primeira resposta, sem `vault_list`, sem plano;
- **escopo:** 1 pergunta para abrir o AppDemo, aceita em 7 s;
- **página 1 em 12 passos:** `consultar` deu curso e nome; as 3 obrigatórias sem memória viraram 2 perguntas ("superior incompleto com mais de 2 anos", "projetos pessoais"); o curso ficou vazio no passo 4 e o próprio agente achou com `look`;
- **página 2:** AppDemo já logado (o cofre não foi exercitado), 1 `look` no avatar, 1 `julgar` (Haiku, 16 s, 6 perguntas), RF-01–05 marcados numa ação cada;
- **sem desperdício de começo:** nada de percorrer as 9 páginas, nada de refazer rascunho. Página 2 completa no passo 29 (no a2242336, o passo 29 ainda estava no cadastro do AppDemo).

O que deu errado (passos 22–28, 7 dos 29):
- o juiz devolveu o RF-06 com 1 de 4 linhas (Clareza=Parcialmente: "«a inicial» de qual nome?" contra o avatar «NA»). Sem `JuizIlegivel` no app.log: a resposta veio válida e incompleta;
- o `marcar_grade` respondeu "`julgar` RF-06 de novo" 6 vezes; o modelo chamou `marcar_grade` 4 vezes em "nada novo";
- o modelo "lembrava" um veredito que não existia (Conforme/Conforme/Parcialmente/Conforme, o padrão do RF-01–05). A trava de clique recusou a troca da Clareza duas vezes, mas a própria recusa ensinava "`marcar_grade` com `linhas`", e ele usou: a Clareza terminou "Conforme" contra o «Parcialmente» do juiz, e as outras 3 linhas foram chute do modelo.

Haiku × Sonnet na página 2: Haiku deu C/C/P/C nas 5 primeiras (fórmula); Sonnet (a2242336) variou mais e marcou mais "Parcialmente" em Ambiguidade e Verificabilidade. Os dois pegaram a «inicial» × «NA» no RF-06, em critérios diferentes.

Corrigido em 2026-11-14 01:34 UTC:
- `marcar_grade` com veredito parcial chama o juiz SOZINHO para as linhas que faltaram, uma vez por pergunta (`_rejulgadas`); se ainda faltar, "o juiz não respondeu nem na segunda vez: pergunte ao Daniel ou siga, mas não chute";
- `linhas` que contradiz o juiz é recusado com o porquê dele ("NÃO troquei Clareza (o juiz decidiu «Parcialmente»: …)"), salvo se o Daniel citou a pergunta (`_daniel_citou`: "RF-06", "rf 6", nas falas do chat ou do painel);
- a recusa do clique perdeu o atalho e mostra o porquê do juiz.

Testes: 4 novos em `test_julgar.py` + o do clique ampliado; o "linha trocada pelo agente" agora traz a fala do Daniel citando o RF-01.

### 9.15 O juiz deixa de ser "daquele formulário" (2026-11-14 02:06 UTC)

Pedido do Daniel: "temos 4 selects de Navegador no neo-panel, o do juiz parece que só foi feito para esse formulário, e o Neo é para ser para todos os sites".

Medido antes, com o leitor de então: Google Forms em português → 1 pergunta certa; o mesmo em inglês → colunas "Conforme, response for Clareza"; matriz em `<table>` de rádios comuns → 0 perguntas. Causas: o `radiogroup` só entrava no inventário por acaso (o Google põe `jsaction` nele; o seletor da extensão não tem `[role=radiogroup]`), e o rótulo "X, resposta para Y" era lido por regex em português. O prompt do juiz falava de requisitos, e nada impedia o juiz de "decidir" uma grade de opinião do Daniel.

Feito:
- **painel:** `RoleSpec.grupo` → `group` no catálogo → a aba Modelos agrupa os cards. "Navegador local (o seu)": Passo a passo, Olho (visão), Raciocínio (juiz); "Navegador do servidor (VPS)": Subagente. A chave `browser_local_judge` não mudou (a escolha do Haiku continua no banco);
- **snapshot (Rust/JS):** `NEO.grade(el)` → `item.grade = {grupo, linha, coluna, pergunta}` em todo rádio (ARIA ou HTML): grupo = `radiogroup` ou `name`; linha = nome ARIA do grupo, texto de UMA linha, ou o cabeçalho da linha na tabela; coluna = cabeçalho da coluna na tabela; pergunta = caption, `aria-label` da tabela, `legend`, ou o `[role=heading]` do `[role=listitem]`. **Aninhado de propósito:** `grupo` solto já é "caixa com N itens — não é botão" no driver;
- **`grade.py`:** caminho pela estrutura quando há `grade`, e o antigo (ordem do Google) sem ela; coluna = cabeçalho, ou o rótulo sem o ", … <linha>" final, em qualquer idioma; **escolha simples fica de fora** (o grupo É a pergunta, ou não tem nome de linha) — julgá-la é a etapa 2;
- **juiz neutro:** critérios sempre da página; requisitos virou exemplo; linha sobre o Daniel volta `PESSOAL` (não é coluna, não é falta, não rejulga): o `marcar_grade` não marca e manda `consultar`/`perguntar`, e aceita a resposta dele por `linhas`;
- **prompt do passo:** exemplos neutros ("Q3", "Atendimento"), sem "14 requisitos".

Verificado: teste em Chromium de verdade (`o_radio_de_grade_traz_grupo_linha_coluna_e_pergunta`, ignorado na suíte normal) com grade ARIA em inglês, escolha simples ARIA, matriz em tabela e fieldset; o snapshot dele passado pelo `ler_grades` dá RF-01 e RF-02 com as marcações certas e deixa as escolhas simples de fora. A página 2 real (2f320de8) continua com 6 perguntas e a página 1 real (8e17b81e) com nenhuma. Gotcha: os testes de Chromium ignorados derrubam o mesmo headless; rodar com `--test-threads=1`. O `neo-map.generated.js` da extensão foi regerado (só o inspetor visual o usa; reload manual da extensão para vê-lo).

Falta (etapa 2): julgar escolha simples, escala e quiz — com a guarda de opinião valendo para todas as perguntas, e o aviso de grade sem empurrar `julgar` em página de perfil.
