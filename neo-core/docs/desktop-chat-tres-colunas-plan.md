# Chat em três colunas no desktop — plano

> Status (20/11/2026): **F0–F4 em produção no desktop e verificadas no app real**
> (session-0e232e1b e session-8661abd6). F5 e multimodelo continuam fora. O que
> saiu diferente do plano e os E2E estão no §9. Protótipo aprovado:
> https://claude.ai/artifact/Gh1HYEzmxSBhnK84gUDKXH (artboard Main).
> Pedido do Daniel: com a janela maximizada, conversas e histórico à esquerda,
> o chat no centro e os agentes em segundo plano à direita. Embaixo, na coluna
> da esquerda, o que a sessão atual está usando agora. **Multimodelo fica para
> depois (§8).** O resto deve sair hoje, sem mexer no fluxo do turno.

## 1. O que muda e o que não muda

**Muda:** só o front do `neo-desktop-tauri` (React + CSS), e só quando a janela
está larga.

**Não muda:**
- nada em `neo-core/`, no `.proto` ou em `src-tauri/` (Rust);
- `useAgentEvents.ts`, o fluxo do turno no `ChatPage.tsx` e os eventos
  `neo-new-chat` / `neo-load-session` (as colunas só disparam os mesmos eventos);
- a janela compacta: o padrão é 560×720 (`tauri.conf.json`), e abaixo do
  primeiro ponto de quebra a tela fica idêntica à de hoje, com gaveta de
  sessões e botões na barra.

Como não há mudança em Rust, o `tauri dev` recarrega o front sozinho. Também não
há deploy de core.

## 2. O que já existe e serve

| Peça do protótipo | Já existe | Fonte dos dados |
|---|---|---|
| Lista de conversas | `SessionsSidebar.tsx` (gaveta) + `useSessionManagement.ts` | `GET /api/v1/desktop/sessions` → `session_id`, `title`, `message_count`, `last_activity_at` (UTC sem fuso: usar o `parseUtc`) |
| Abrir conversa / novo chat | eventos `neo-load-session` e `neo-new-chat`, ouvidos no `ChatPage` | — |
| Conversa atual | `getSessionId()` + evento `session_changed` | Rust (`AppState`) |
| Tarefas em segundo plano | `JobsViewer.tsx`, que é uma janela separada | `listJobs('active'\|'all')`, `getJob`, `getJobLogs(id, tail)`, `cancelJob`, `openJobsWindow(jobId)`. O `JobSummary` traz `session_id`, `kind`, `status`, `started_at`, `duration_s` e `preview` |
| Status "em ação" durante o turno | evento `thinking` com `cycle: true` | frase de `_TOOL_PROGRESS_LABEL` (`neo-core/modules/desktop/ai_service.py:2095`), p.ex. "olhando a caixa de email", "lendo o email", "dirigindo o seu navegador", "abrindo a página: …". **Só a frase: o nome da tool não vem.** WhatsApp e Home Assistant mandam `thinking` sem `cycle` ("Iniciando missão WhatsApp...", "Falando com o Home Assistant...") |
| Seletor de modelo | `Chat.tsx` (Agent / provider / modelo) | catálogo do core |

## 3. Decisões

1. **Pontos de quebra pela largura da janela.** A webview tem o tamanho da
   janela, então basta media query, mais um hook (`matchMedia`) para não montar o
   que não aparece:
   - `< 960 px`: como hoje;
   - `960–1279 px`: conversas + chat, com a coluna direita aberta por botão;
   - `≥ 1280 px`: três colunas.
2. **Status "em ação" calculado no desktop, sem mudar o core.** Vem de duas fontes:
   - (a) as tarefas da sessão (`session_id`), que são dados estruturados;
   - (b) a frase de progresso do turno, casada por prefixo numa tabela que espelha
     `_TOOL_PROGRESS_LABEL`.

   Casar frase é frágil: o próprio `ai_agent.rs` evita isso para o marcador de
   ciclo. Aqui o risco é aceitável porque as frases saem de um dict fixo no
   código, não do modelo, e o pior caso é o agente não aparecer na lista. O chat
   não muda. A tabela do desktop cita o arquivo e a linha do dict no core. A
   versão robusta é a F5.
3. **Reaproveitar em vez de copiar:**
   - `SessionsList` sai de dentro do `SessionsSidebar` e passa a servir a gaveta
     e a coluna;
   - `STATUS_LABEL`, `KIND_LABEL` e os conjuntos ativo/cancelável saem do
     `JobsViewer` para `lib/jobs.ts`.

   A janela Tarefas continua existindo e passa a importar de lá.
4. **A coluna direita só tem ações que já existem:**
   - Abrir no visor (`openJobsWindow(jobId)`);
   - Cancelar, com a mesma regra do visor e da `job_cancel`;
   - Ir para a conversa (`neo-load-session` com o `session_id` da tarefa).

   O "Enviar" do cartão que espera você **não entra**: a liberação continua no
   card do chat, que tem a conferência.
5. **Uma consulta de tarefas só, no mesmo ritmo do visor.** Um `JobsProvider`
   consulta a cada 4 s se há tarefa ativa e a cada 20 s se está tudo parado. A
   coluna direita, o selo das conversas e o painel de agentes leem dele. Ninguém
   consulta quando nenhuma das colunas está na tela: cada consulta vai até o core
   de produção pelo túnel.
6. **O estado das colunas não passa pelo `ChatPage`.** A lista do chat é
   virtualizada e o `memo` depende de callbacks estáveis
   (`neo-desktop-chat-virtualization`). Se a consulta das tarefas re-renderizasse
   o chat a cada 4 s, a rolagem pioraria. As colunas ficam no `App`, irmãs do
   `ChatPage`, e consomem o contexto direto.
7. **Coluna aberta ou fechada fica salva no `localStorage`**, com o mesmo padrão
   de leitura protegida de `lib/chatPrefs.ts`.
8. **Sem seção "Modelos" por enquanto.** O seletor continua embaixo do campo de
   mensagem. A seção entra junto com o multimodelo.

## 4. Fases

### F0 — Estrutura das três colunas
- `hooks/useWideLayout.ts` → `'compact' | 'medium' | 'wide'`.
- `App.tsx`: na aba Chat, com `medium`/`wide`, o `.app-content` vira grade
  `esquerda | ChatPage | direita`. A aba Devices fica como está.
- Barra do topo, fora do modo compacto: some o botão de histórico (a lista já
  está na tela) e entram dois botões para mostrar ou esconder cada coluna. O
  "Novo chat" vai para a coluna.
- `Chat.css`: no modo largo, mensagens e campo de mensagem com `max-width: 880px`
  centralizados.
- Cuidado com a regra `.app > *:not(...)` do `App.css` (gotcha 4 de
  `neo-desktop-session-picker`): as colunas moram dentro do `.app-content`, então
  não são afetadas. Ver também a margem da barra de rolagem perto do
  `.resize-handle-e` (comentário no `Chat.css`).
- **Pronto quando:** a 560×720 a tela está igual à de hoje (print antes e
  depois); maximizada, aparecem as três colunas vazias.

### F1 — Conversas (coluna esquerda, em cima)
- `components/SessionsList.tsx`, extraído do `SessionsSidebar`, que passa a usá-lo.
- `components/ConversationsPanel.tsx`:
  - busca local por título;
  - grupos Hoje / Ontem / Esta semana / Mais antigas por `last_activity_at`;
  - destaque na conversa atual;
  - "Novo chat" dispara `neo-new-chat`; clicar numa conversa dispara
    `neo-load-session`.
- Recarrega ao montar, no `session_changed`, no `complete` (a conversa nova ganha
  título com a 1ª mensagem) e a cada 60 s.
- **Pronto quando:** dá para trocar de conversa e criar uma nova pela coluna, e a
  gaveta do modo compacto continua funcionando.

### F2 — Segundo plano (coluna direita)
- `lib/jobs.ts` (rótulos e conjuntos) e `JobsViewer.tsx` importando de lá, sem
  mudar comportamento.
- `hooks/useJobs.ts` + `JobsProvider` (decisão 5).
- `components/JobsColumn.tsx`:
  - filtros Ativos / Desta conversa / Todos;
  - cartão com ícone do tipo, título, tipo · tempo (`started_at`/`duration_s`),
    selo de status e a linha de `preview`;
  - clicar abre o fim do log (`getJobLogs(id, 2000)`, últimas 4 linhas), "de:
    conversa" e os botões da decisão 4.
- **Pronto quando:** uma tarefa real (um `neo_bash` longo) aparece rodando,
  termina sozinha na coluna e "Abrir" leva ao visor com ela selecionada.

### F3 — Ligações entre as colunas
- Selo "N rodando" / "esperando" em cada conversa, contado no `useJobs` por
  `session_id`.
- Título da conversa atual no topo do centro, tirado da lista de sessões.

### F4 — Agentes em ação (coluna esquerda, embaixo)
- `lib/agentActivity.ts`: tabela de prefixos de frase → agente (Email,
  Navegador, Navegador do servidor, Web, Sandbox, Arquivos, Caderno, Claude Code,
  WhatsApp, Home Assistant).
- `hooks/useAgentActivity.ts`: escuta `onAgentEvent` por conta própria (o Tauri
  aceita vários ouvintes, e o handler do `useAgentEvents` fica intocado) e
  combina com o `useJobs`:
  - `thinking` casado na tabela → aquele agente fica **em ação**, com a frase como
    passo atual;
  - `complete`/`error` → acaba o que veio do turno;
  - tarefa da sessão `running` → o agente dela fica em ação até a tarefa acabar
    (p.ex. `browser_local` → Navegador, com a linha de `preview`).
- `components/AgentActivityPanel.tsx`: **só lista quem está em ação**. Sem
  ninguém, a seção some.
- **Pronto quando:** o roteiro do §7 mostra o Email passando por "olhando a
  caixa" → "lendo o email" e sumindo no fim do turno.

### F5 — opcional, fora de hoje: nome da tool junto com o progresso
Campo novo opcional `tool_name` no `AgentThinking` (proto), preenchido onde o
loop já chama `_progress_label`. O Rust repassa o campo e o desktop passa a
preferir ele à tabela de frases. Isso mexe no core (salvar = deploy) e no Rust
(reiniciar o `tauri dev`), por isso fica separado.

## 5. Arquivos

**Novos** (todos em `neo-desktop-tauri/src/`):
- `hooks/useWideLayout.ts`, `hooks/useJobs.ts`, `hooks/useAgentActivity.ts`;
- `lib/jobs.ts`, `lib/agentActivity.ts`;
- `components/SessionsList.tsx`, `ConversationsPanel.tsx(.css)`,
  `JobsColumn.tsx(.css)`, `AgentActivityPanel.tsx`.

**Alterados:** `App.tsx`, `App.css`, `Chat.css`, `SessionsSidebar.tsx`,
`JobsViewer.tsx`.

**Intocados:** `src-tauri/**`, `neo-core/**`, `useAgentEvents.ts`, o fluxo do
turno no `ChatPage.tsx`.

## 6. Riscos

| Risco | Como evitar / medir |
|---|---|
| Janela compacta muda sem querer | F0 compara prints a 560×720 antes e depois |
| Chat fica mais lento (re-render a cada consulta) | decisão 6; rolar uma conversa longa com tarefa ativa |
| Mais carga no core de produção | uma consulta só, parada quando escondida (decisão 5); a mesma de quando o visor está aberto |
| Frase de progresso muda no core | o agente só não aparece; a F5 resolve de vez |
| Conversa nova sem título na lista | recarregar no `complete` (F1) |

## 7. Verificação

- `npx tsc -b` e `npm run lint` em `neo-desktop-tauri/`. **Nunca `tauri build`**
  (`neo-desktop-no-release-build`).
- Roteiro com o `tauri dev` rodando e a janela maximizada:
  1. Janela em 560×720 → tela igual à de hoje.
  2. Maximizar → três colunas; esconder e mostrar cada uma; fechar e reabrir o
     app → continuam como ficaram.
  3. Trocar de conversa pela coluna e voltar.
  4. Mandar o prompt abaixo e olhar a coluna esquerda embaixo durante o turno.
  5. Pedir uma tarefa longa (`neo_bash` > 2 min) e acompanhar na coluna direita
     até terminar.
- Prompt de teste da F4 (colar no chat):
  > Olha meus emails de hoje e me diz quais parecem importantes, com o remetente e o assunto de cada um.

  Depois, mandar o id da sessão (aparece no painel de detalhes da mensagem) para
  conferir no diário quais frases de progresso saíram.

## 8. Fora de hoje: multimodelo

Mandar a mesma mensagem para N modelos e juntar as respostas exige coisa no
core que não existe:
- disparar o turno em N provedores;
- guardar N respostas por turno;
- o passo que une as respostas;
- uma forma de mandar várias respostas por turno no proto;
- as abas no chat.

Vai ter plano próprio. As fases acima não atrapalham: a seção "Modelos" entra
no painel de baixo da coluna esquerda, acima de "Agentes em ação".

## 9. Implementação (19/11/2026)

Decisões confirmadas pelo Daniel: as duas larguras do §3.1 (960 e 1280 px) e o
cartão "esperando você" só com "Ir para a conversa", sem "Enviar".

**O que saiu diferente do plano:**
- **Sem `JobsProvider`.** A consulta das tarefas é um store de módulo
  (`hooks/useJobs.ts`) que conta quem está inscrito: com uma coluna na tela, ele
  consulta; com nenhuma, para. Também pausa com a janela oculta e consulta de
  novo no fim de cada turno (`complete`), quando a tarefa nova costuma nascer. Dá
  no mesmo que a decisão 5, sem um contexto no `App`.
- **A lista de conversas das colunas tem store próprio**
  (`hooks/useSessionsList.ts`), lido pela coluna e pelo título do centro. A
  gaveta continua no `useSessionManagement`, sem mudança.
- **A conversa aberta, fora do ChatPage**, vem de `lib/activeSession.ts`. O
  `ChatPage` ganhou um único efeito que publica o `sessionId` quando ele muda. O
  turno não foi mexido. Abrir conversa e Novo chat pelas colunas continuam
  disparando `neo-load-session` / `neo-new-chat`, e a conversa já aberta não é
  reaberta, porque o handler pararia o turno.
- **Janela média (960–1279 px):** o Segundo plano abre por cima do chat, com X e
  Esc, e não fica salvo. Na larga, a escolha fica no `localStorage`, junto com a
  da esquerda e o filtro da coluna (`lib/chatPrefs.ts`).
- **O selo de status** saiu do `JobsViewer.css` para
  `components/JobStatusBadge.{tsx,css}`. A janela Tarefas e a coluna usam o mesmo.
- **"Agentes em ação"** também mostra a tarefa da conversa `na fila` e
  `esperando você`, além de `rodando`. A tarefa vence a frase do turno quando as
  duas são do mesmo agente. `comando_local` aparece como "Seu computador".
- **A conversa nova**, que só entra na lista do core quando o 1º turno fecha,
  aparece no topo como "Nova conversa", destacada.

**Verificado (sem o app real):** `tsc -b` limpo. O lint não tem erro nos
arquivos novos (o total caiu de 54 para 50, todos antigos). A 560×720 a tela é
idêntica pixel a pixel à do commit anterior, com e sem a gaveta aberta:
Playwright headless com o Tauri simulado, versão antiga num vite à parte. Prints
a 1100 e a 1440 px com sessões e tarefas falsas mostram as colunas, os selos, o
cartão aberto com o fim do log e o painel de agentes.

**1º E2E, prompt do email (session-0e232e1b, 20/11):** "Agentes em ação" não
apareceu. O classificador mandou pelo trilho do servidor (`SERVER_TOOL`,
`email_inbox`). Esse trilho manda UMA frase só, com o nome cru da tool
(`Executando ferramenta no servidor: email_inbox...`, ai_service.py ~9683). O
agente dele (AgentExecutor) roda sem a fila de progresso, então nenhum rótulo
de `_TOOL_PROGRESS_LABEL` sai. A tabela casava só os rótulos do cérebro.
Corrigido no desktop: a tabela virou tool → (agente, rótulo) e reconhece as duas
frases que trazem o nome da tool, a do servidor e a do Mac
(`Solicitando execução de <tool> no desktop...`). Um script confere que os
rótulos copiados batem com o dict do core. **Limite:** no trilho do servidor o
passo fica parado em "olhando a caixa de email" até o fim do turno. Para ele
andar ("lendo o email"), o AgentExecutor teria que emitir progresso por passo,
e isso é core (junto da F5).

**2º E2E (20/11):** o Email apareceu. Pedidos do Daniel em seguida, já feitos:
- **A seção de baixo fica sempre na tela** ("Agentes e canais"). Quem está em
  ação aparece primeiro. Depois vem quem já trabalhou nesta conversa: os agentes
  que o turno usou desde que o app abriu, com selo "feito", e as tarefas da
  conversa que terminaram, com o status delas. Sem ninguém, a seção mostra um
  aviso. O histórico dos turnos fica só na memória do app (o core não guarda
  quem trabalhou), então some ao fechar; o das tarefas vem do core.
- **Tamanhos por arraste** (`components/ResizeHandle.tsx` e
  `components/ChatColumns.tsx`): largura de cada coluna (esquerda 240–440 px,
  direita 280–480 px) e a divisória entre Conversas e Agentes e canais (mínimo
  de 180 px em cima e 110 px embaixo). Fica salvo no `localStorage`. Duplo
  clique volta ao padrão, e as setas, Home e End ajustam pelo teclado. Cada
  moldura é dona do próprio tamanho: arrastar não re-renderiza o App nem o
  ChatPage, que só sofre o reflow da largura. Conferido no headless: arraste,
  limite, recarregar, duplo clique e teclado; e a 560×720 a tela continua
  idêntica pixel a pixel.

**3º E2E, tarefa longa (session-8661abd6, 20/11):** ✅ tudo como esperado. O job
`c52d1491` (`sandbox_task`, contagem de 1 a 150) aparece "rodando" na coluna
direita, nos filtros Ativas e Desta conversa. A conversa ganha o selo "1
rodando", o título do centro ganha o selo "1 em segundo plano", e o Sandbox fica
"em ação" em Agentes e canais com o título da tarefa. No banco: das 02:17:23 às
02:19:54 UTC, exit 0. A continuação automática entrou às 02:19:54 e avisou na
conversa.

**Status:** F0–F4 verificadas no app real. A F5 (nome da tool no progresso, que
também faria o passo andar no trilho do servidor) e o multimodelo continuam
fora.
