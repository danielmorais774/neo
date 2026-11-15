# Neo no ambiente de trabalho: VSCode, sessões do Claude e projeto no PKS — plano

> Estado em 08/11/2026: F0 e F0b medidas (§3). **F1 e F2 implementadas** (§13):
> o cérebro lista as sessões do Daniel (`claude_sessoes`), entrega o pedido na
> escolhida (`claude_enviar`) e um job traz a resposta ao chat. Falta o E2E pelo
> chat do Neo. Memória da sessão de pesquisa: `neo-ponte-sessoes-claude-vivas`.
> Plano vizinho: [claude-code-delegation-plan.md](claude-code-delegation-plan.md), que
> é o Neo usando o Claude Code dele próprio. Este plano é o Neo entrando nas sessões
> que o **Daniel** está usando.

## §1. A ideia

O Daniel fala com o Neo numa sessão limpa, por voz ou texto, sobre o trabalho que
está fazendo. Hoje o Neo não sabe de nada disso. Com este plano ele passa a:

1. **Ver o trabalho.** Quais janelas do VSCode estão abertas e qual está focada,
   o arquivo ativo, o branch de cada repositório e as sessões do Claude abertas
   (com título e o que estava sendo feito em cada uma).
2. **Saber do projeto.** A pasta da janela identifica uma entidade `project` no
   PKS, e a ficha dela entra no prompt.
3. **Entregar o pedido na sessão certa do Claude.** O Neo escolhe a sessão pelo
   tema do pedido, entrega o texto nela e avisa quando ela terminar.
4. **Aprender sobre o projeto.** O que o Daniel ensina ("nesse projeto salvar é
   publicar") vai para a ficha do projeto e volta toda vez que ele trabalhar nele.

Quatro princípios:

- **Sob demanda.** Turno que não é de trabalho não paga nada: zero token no prompt
  e zero request extra.
- **Dados no Mac até serem pedidos.** O retrato do trabalho e as fichas das sessões
  passam pela VPS só durante o turno e não ficam gravados lá. Só o que o Daniel manda
  guardar vai para o PKS ([[feedback-no-conversation-copies-on-vps]]).
- **Só o dono.** Nada disso funciona para terceiros, agenda ou turno contaminado.
- **Medir antes de concluir.** Cada fase tem um E2E de verdade antes de ser dada
  como pronta.

## §2. Decisões tomadas (08/11/2026)

| # | Decisão | Por quê |
|---|---|---|
| D1 | A sessão do Claude é escolhida pelo **tema** do pedido. A recência só desempata. Na dúvida, o Neo **pergunta** citando os títulos. | O Daniel alterna entre duas sessões e às vezes pede algo da sessão anterior. |
| D2 | Para entregar numa sessão **aberta**: SendMessage, por um `claude -p` mensageiro com ambiente limpo. | Medido: chega ao vivo no VSCode e no CLI, sem reload (§3). |
| D3 | **Nunca** usar `--resume -p` numa sessão aberta. | Dois processos gravando a mesma sessão dividem o histórico em dois ramos. O `claude_cli.rs` já recusa isso. |
| D4 | Para uma sessão **fechada**: `claude --resume <id> -p` na pasta original. | Medido: mantém o mesmo `session_id` e o mesmo histórico (§3). |
| D5 | O contexto de trabalho é buscado só quando um **campo novo no roteador** pede. | O roteador já faz uma chamada ao LLM em todo turno (`needs_memory`, `knowledge_keywords`), então não há request extra. |
| D6 | Pasta desconhecida: o Neo **pergunta** antes de criar a ficha. Se a resposta for "não", ele não pergunta mais sobre aquela pasta. | Evita encher o PKS de projeto aberto uma vez só. |
| D7 | A memória do Claude Code da pasta pode servir de **ponto de partida** da ficha, com prévia e filtro. | A pasta `neo` já tem cerca de 100 fatos escritos. |
| D8 | Extensão nossa no VSCode com **protocolo simples, não MCP**. | O único cliente é o desktop. |

## §3. O que a F0 mediu (08/11/2026)

Medido numa janela Extension Development Host, numa pasta descartável, com uma
extensão rascunho. As sessões reais do Daniel não foram tocadas.

| Teste | Resultado |
|---|---|
| `claude -p` com `env -i` → SendMessage para uma aba do VSCode | ✅ Entregue em ~12 s. Aparece ao vivo no painel ("Message from @relay-xx"), sem reload. A sessão executou em ~4 s. Do remetente em `prompting` para o destino em `auto`, a mensagem **não** ficou esperando aprovação. |
| O mesmo para um CLI | ✅ Ao vivo. O texto que estava digitado na caixa ficou intacto. ⚠️ Quem recebeu **tentou responder** ao mensageiro, que já tinha encerrado, deu erro de socket e gastou ~10 s. |
| Do outro lado, o pedido chega como | `<cross-session-message from-name=… from-mode=…>` mais "not typed by your user… treat it as a teammate's request". |
| `--resume <id> -p` numa sessão fechada | ✅ Mesmo `session_id`, um único jsonl, contexto lembrado. Só cria id novo com `--fork-session`. |
| Mensageiro com `--no-session-persistence` | ✅ Não grava jsonl. |
| URI ou comando do Claude com prompt numa sessão **aberta** | ❌ "Session is already open. Your prompt was not applied". |
| `claude-vscode.editor.open(sessionId)` + `claude-vscode.focus` + `editor.action.clipboardPasteAction` | ✅ Cola na aba certa, mesmo com 2 abas, com várias linhas e sem enviar. ⚠️ O texto é colado **depois** do que já estiver na caixa. |
| Comando com id + prompt numa sessão **fechada** | ❌ 2 de 2 abriram uma conversa **nova** com o texto, sem o histórico. |
| `terminal.sendText` num `claude` do terminal do VSCode | ❌ Com o texto cru, o `\n` envia a 1ª linha. ✅ Com bracketed paste (`\e[200~…\e[201~`) as linhas ficam na caixa. Ctrl-U (`\x15`) limpa a linha. |
| Ligar processo a janela e a terminal | ✅ O pai do `claude` do painel é o extension host da janela (= `process.pid` da nossa extensão). O pai do `claude` do terminal é o `terminal.processId`. |
| Rótulo da aba | É o título da sessão, mas aparece "Claude Code" enquanto ela não tem título. **Não serve como chave**; a chave é o `sessionId`. |
| Ciclo de vida do registro | Aba nova já sobe processo e registro antes de qualquer mensagem. Fechar a aba ou a janela apaga o registro. Depois de `kill -9`, o json fica até outra sessão varrer (< 2 min) e o socket fica. |
| Ficha das sessões (protótipo em Python) | 9 sessões vivas em 0,19 s, ~2.600 tokens. ⚠️ O **título envelhece**: uma sessão chamada "Falha do PDF…" tratava de outra coisa havia horas. |

**F0b (08/11/2026)**, os 6 itens que faltavam, numa janela de teste aberta com
`env -i` (sem herdar o `CLAUDE_*` da sessão que testava):

| # | Teste | Resultado |
|---|---|---|
| 1 | Mensagem para uma sessão `busy` (rodando `ping -c 25`) | ✅ Entra na fila (`enqueue`), espera a ferramenta terminar e entra **no mesmo turno** como `queued_command`. A resposta sai junta com a do trabalho em andamento ("FIM-C OK-D"). |
| 2 | Mensagem chegando a um painel com texto na caixa | ✅ O texto da caixa fica intacto. Com "Não responda por mensagem", a sessão não tentou responder ao mensageiro. |
| 3 | Remetente em `auto`, destino em `default` | ✅ Entregue e respondida, sem esperar aprovação. O protocolo mostra `from-mode="prompting"` tanto para o `claude -p` (mesmo com `--permission-mode auto`) quanto para uma sessão interativa em `Auto`. O rótulo `auto`/`default` não é o que conta. Remetente em `bypassPermissions` **não foi testado**: o modo auto bloqueou. O mensageiro do Neo nunca usa esse modo. |
| 4 | Colar com a janela do VSCode atrás de outra | ✅ O texto entra na caixa, mas a janela **não vem para a frente** sozinha. `code <pasta>` traz a janela que tem aquela pasta aberta, sem precisar de Acessibilidade. Captura de janela coberta sai velha, porque o Chromium não redesenha. |
| 5 | Várias janelas: um lock por janela? | ✅ Sim: `~/.claude/ide/<porta>.lock`, cada um com as `workspaceFolders` da sua janela, e o lock some quando a janela fecha. O `pid` do lock é o do processo principal do Code, igual para todas as janelas, então não serve para saber se a janela está viva. |
| 6 | Reabrir uma sessão interativa real fechada (`claude-vscode.primaryEditor.open(id, prompt)`) | ✅ O painel sobe com `--resume=<id>`, mostra o histórico inteiro e deixa o prompt **preenchido**, sem enviar. Vale até para uma sessão criada no CLI. Os dois casos que falharam na F0 eram uma sessão só com mensagem de outra sessão e uma de `-p` (`sdk-cli`): essas abrem conversa nova. |

Gotchas que a F0 queimou:
- **Herança do ambiente.** `code` aberto de dentro de uma sessão do Claude passa
  `CLAUDE_CODE_CHILD_SESSION` aos terminais, e o CLI fica sem transcrição e sem
  registro. O desktop precisa limpar o ambiente de todo `claude` que lançar.
- **Pasta nova abre em Modo Restrito.** A extensão do Claude não carrega ali.
- **Modo das sessões novas.** Abas novas do Claude sobem em `auto`.

## §4. Fontes de dados no Mac

### 4.1 Registro das sessões vivas — `~/.claude/sessions/<pid>.json`

Há um arquivo por processo interativo vivo. Campos usados:
- `sessionId`, `cwd`, `name` (o endereço do SendMessage, ex. `neo-a4`);
- `entrypoint`: `cli` ou `claude-vscode`;
- `kind`, `status` (`busy` | `idle`), `statusUpdatedAt`;
- `procStart` (texto em **UTC**), `version`.

**Vivo** = `kill(pid, 0)` funciona **e** `procStart` bate com
`TZ=UTC ps -p <pid> -o lstart=`. Isso cobre pid reaproveitado e o json que sobra
depois de um `kill -9`. Os `.key` e o `messagingSocketPath` nunca são lidos.

### 4.2 Transcrições — `~/.claude/projects/<cwd codificado>/<sessionId>.jsonl`

Achar o arquivo por glob em `*/<sessionId>.jsonl`. A ficha (§9.1) sai de:
- linhas `ai-title`/`custom-title` (vale a última);
- o `cwd` da última linha `user` (a pasta onde a sessão está agora, ex. `neo/neo-core`);
- pedidos do usuário: linhas `user` sem `isMeta`, só blocos de texto, descartando
  blocos que começam com `<` (system-reminder, ide_*, tags de comando);
- última resposta: o texto da última linha `assistant` fora de sidechain;
- anexos `edited_text_file` (arquivos editados) e linhas `frame-link` (artefatos).

O arquivo pode passar de 12 MB. Ler o **início** (objetivo) e a **cauda**
(o resto); não ler tudo a cada pedido. O que fica de fora:
- arquivos de scratchpad (`/private/tmp/...`);
- sessões dos jobs do próprio Neo (`~/Neo/claude/*`). Essas aparecem como
  "tarefa do Neo", não como sessão do Daniel.

### 4.3 Janelas do VSCode

- **Com a extensão `neo-vscode` (F5):** há um arquivo de estado por janela (§5.2), com
  pastas, se a janela está focada, arquivo ativo, branch e abas do Claude. É a única
  fonte de foco e de arquivo ativo.
- **Sem a extensão:** as pastas vêm de `~/.claude/ide/*.lock` (`workspaceFolders`;
  o `authToken` nunca é lido) e do `cwd` das sessões do Claude.
  - Há **um lock por janela**, e ele some quando a janela fecha (F0b). O `pid` do
    lock é o do Code principal, comum a todas, então a janela existe enquanto o
    arquivo existir.
  - Sem a extensão não se sabe qual janela está focada.
- **Trazer uma janela para a frente:** `code <pasta>` foca a janela que já tem aquela
  pasta aberta (F0b), sem permissão de Acessibilidade.
- O `windowsState` do `storage.json` do VSCode **não serve**: sai vazio com as
  janelas abertas.

### 4.4 Git

Branch de cada repositório da pasta: `git -C <repo> rev-parse --abbrev-ref HEAD`,
local e barato. A raiz `neo` não é repositório; os subprojetos (`neo-core`,
`neo-desktop-tauri`, …) são. Com a extensão, a API `vscode.git` dá o mesmo dado sem
lançar processo.

### 4.5 Memória do Claude Code — `~/.claude/projects/<cwd codificado>/memory/`

Usada **só** para o ponto de partida da ficha (§8.6) e só quando o Daniel pede.
`MEMORY.md` é o índice. Cada arquivo tem frontmatter com `type`
(`user` | `feedback` | `project` | `reference`).

## §5. Arquitetura

```
Daniel ──► neo-core: roteador (1 chamada LLM, já existe) → contexto_trabalho?
                │ não → turno normal, nada a mais
                │ sim
                ▼
        desktop: trabalho.contexto ──► retrato (janelas, foco, sessões)
                ▼
        core: pasta → entidade `project` no PKS (ou pergunta para criar)
                ▼
        prompt: camada WORK_CONTEXT (retrato + ficha do projeto, com teto)
                ▼
        cérebro: responde / claude_sessoes / claude_enviar / projeto_consultar /
                 pks_remember (com projeto em foco) / projeto_criar
                ▼
        desktop: claude.enviar (mensageiro ou extensão) ──► sessão do Claude
                ▼
        desktop observa busy→idle ──► core ──► mensagem proativa no chat
```

### 5.1 Desktop (Rust)

- **`services/trabalho.rs`** (módulo novo), prefixo `trabalho.` em
  `websocket.rs`, como o `claude.` faz hoje:
  - `trabalho.contexto`: o retrato (§6.1). ✅ F1;
  - `trabalho.memoria_claude`: lê a memória do Claude Code de uma pasta (§6.4). F4.
- **`services/claude_sessoes.rs`** (módulo novo, ✅ F1): registro, conferência de
  processo vivo, leitura da transcrição e a ficha. O `claude_cli.rs` despacha:
  - `claude.sessoes`: as fichas (§6.2). ✅ F1;
  - `claude.enviar`: entrega numa sessão aberta (§6.3). F2.
- **Trava do `--resume` (✅ F1):** o `claude.start` consulta o registro antes de
  retomar uma sessão. Antes só olhava o `ps` e não via o VSCode (§13).
- **Ambiente limpo** em todo `claude` lançado: `env_clear()` e uma lista do que
  passa (`HOME`, `USER`, `PATH`, `LANG`, `TERM`). Assim nada `CLAUDE_*` do ambiente
  do `tauri dev` vaza.
- **Observador de retorno:** acompanha o `status` do registro da sessão alvo até
  `idle` e então lê a última resposta no jsonl (§9.4).

### 5.2 Extensão `neo-vscode` (TypeScript, pasta `neo-vscode/` na raiz, F5)

- Ativa em `onStartupFinished`. Cada janela tem o seu extension host, então cada
  janela tem a sua instância.
- **Estado em arquivo:** grava `~/.neo/vscode/<pid do extension host>.json`
  (modo 600) quando muda o foco, o editor ativo, as abas ou os terminais, com
  debounce de 1 s. O desktop **só lê arquivos** para montar o retrato, sem conexão.
  Conteúdo:
  - pastas da janela;
  - se a janela está focada;
  - arquivo ativo (caminho relativo) e intervalo selecionado, **sem o conteúdo**;
  - branch por repositório;
  - abas do Claude (quantas e qual está ativa);
  - terminais (nome, `processId`, se está ativo);
  - `extHostPid` e versão da extensão do Claude.
- **Comandos:** HTTP em `127.0.0.1`, porta e token no mesmo arquivo. Rotas:
  - `revelar {sessionId}`;
  - `preencher_aba {sessionId, texto}`: revelar, `claude-vscode.focus`, guardar a
    área de transferência, colar, devolver a área de transferência. Funciona com
    a janela atrás de outra, mas a janela não vem para a frente. Se o Daniel deve
    ver, o desktop roda `code <pasta>` depois (F0b);
  - `reabrir_preenchido {sessionId, texto}`: `claude-vscode.primaryEditor.open(id, texto)`
    para uma sessão **fechada** interativa: retoma o histórico e preenche (F0b);
  - `preencher_terminal {processId, texto}`: Ctrl-U + bracketed paste, sem Enter.
- Instalação local por `.vsix` (`code --install-extension`), fora do marketplace.
- Se o arquivo de uma janela some ou o pid morre, o desktop ignora a janela.

### 5.3 Core (Python)

- **`core/trabalho/`** (pacote novo):
  - `contexto.py`: pede o retrato ao desktop, com cache e timeout do `neo.toml`;
    desktop offline = sem retrato;
  - `projeto.py`: liga a pasta à entidade `project` (§8.1), usando o
    `entity_resolver` do PKS;
  - `pendencia.py`: a pergunta "crio a ficha?" e as pastas recusadas (§8.2);
  - `bloco.py`: monta a camada `WORK_CONTEXT` dentro do teto (§7.3);
  - `foco.py`: o projeto e a sessão do Claude em foco na conversa (§7.4).
- **`modules/trabalho/`** (`tools.py` + `executor.py`, no padrão do
  `modules/pks_write`), com as tools do §5.4.
- **Roteador** (`core/router/intent_router.py`): campo novo `contexto_trabalho`
  no JSON, mais o parse e o campo em `RoutingDecision` (`shared/types.py`).
- **`ContextLayer.WORK_CONTEXT`** (novo, `shared/types.py`) e a coleta dele no
  `core/agent/context_selector.py`.
- **`pks_remember`**: o executor passa o projeto em foco ao `PKSMemoryPlanner`
  (§8.4). A tool continua fina: não ganha parâmetro de entidade.
- **Override de intenção** em `modules/desktop/ai_service.py`, junto do
  `pending_clarification` e do `claude_esperando` (§8.2).
- **Chave** liga/desliga no neo-panel, no padrão de `core/claude_code/chave.py`
  (`user_preference` service=neo, category=trabalho).

### 5.4 Tools do cérebro

| Tool | Efeito | O que faz |
|---|---|---|
| `claude_sessoes` | leitura | Fichas completas das sessões vivas + fechadas recentes (§9.1). |
| `claude_enviar` | **escrita** | Entrega `texto` à sessão `sessionId`, no `modo` `enviar` ou `preencher` (§9.3). Para sessão fechada, abre o fluxo do §9.5. |
| `projeto_consultar` | leitura | Busca dentro da ficha do projeto em foco, para o que não coube no bloco. |
| `projeto_criar` | **escrita** | Cria a entidade `project` com nome e pasta, ou liga a pasta a uma entidade existente. Só depois de um "sim" à pergunta. |
| `projeto_importar_memoria` | **escrita**, via job | Ponto de partida da ficha a partir da memória do Claude Code (§8.6). |
| `pks_remember` / `pks_forget` | já existem | Ganham o projeto em foco como sujeito padrão (§8.4). |

Gotcha conhecido: ao pôr tools em `JOB_TOOLS`, salvar **juntos** o filtro do
`ai_service` e o do `register_many` em `api/main.py`, senão a tool entra no toolset
de prod antes da chave existir.

## §6. Contratos das ações do desktop

### 6.1 `trabalho.contexto` (args `{}`)

```json
{
  "fonte": "extensao" | "arquivos",
  "gerado_em": "2026-11-08T22:40:00Z",
  "janelas": [
    {"pastas": ["~/Documents/projects/neo"], "focada": true,
     "arquivo_ativo": "neo-core/core/pks/retriever.py",
     "branch": {"neo-core": "claude-code", "neo-desktop-tauri": "claude-code"}}
  ],
  "sessoes": [
    {"sessionId": "…", "nome": "neo-d0", "titulo": "…", "origem": "claude-vscode",
     "status": "busy", "ultimo_uso_min": 0, "pasta": "~/Documents/projects/neo"}
  ]
}
```

O retrato é curto de propósito (~150 tokens). As sessões entram só com título,
status e recência. A ficha completa vem pela `claude_sessoes`.

### 6.2 `claude.sessoes` (args `{recentes_horas, max}`)

Retorna a lista de fichas do §9.1, as vivas primeiro e por recência, depois as
fechadas que caibam na janela de horas.

### 6.3 `claude.enviar` (args `{sessionId, texto, modo}`)

- Retorna `{entregue: bool, via: "mensagem" | "aba" | "terminal", sessao: {nome, titulo}}`.
- Erros com nome: `sessao_fechada` (o core decide o §9.5), `sessao_sumiu`,
  `extensao_ausente` (cai para `enviar` só se o core mandar), `mensageiro_falhou`.
- Antes de qualquer entrega, o desktop **relê o registro** e confirma que a sessão
  está viva. Ela pode ter fechado entre a escolha e o envio.

### 6.4 `trabalho.memoria_claude` (args `{pasta}`)

Retorna `{arquivos: [{nome, tipo, descricao, corpo}]}` só da pasta pedida. O filtro
fica no core, porque a prévia é mostrada pelo Neo.

## §7. Contexto sob demanda

### 7.1 O portão: campo `contexto_trabalho` no roteador

- **`true`** quando o turno é sobre o trabalho do Daniel no computador, ou quando
  ele quer ensinar ou consultar algo de um projeto. Exemplos: "roda os testes",
  "como ficou o deploy?", "manda pro Claude…", "o que eu tava fazendo?",
  "lembra que nesse projeto…", "qual o comando pra publicar o neo?".
- **`false`** em saudação, clima, música, finanças, conversa social e pedido de
  navegador sem relação com projeto.
- Custo: alguns tokens de saída por turno, na chamada que já existe.
- **Reforço determinístico**, no espírito de [[neo-guard-heuristics-measure-on-prod-corpus]]:
  - o nome de um projeto vinculado ou palavras de trabalho ("projeto", "vscode",
    "claude", "testes", "deploy", "branch") com uma âncora;
  - continuação curta logo depois de um turno de trabalho, que herda o portão.
- **Medir antes de ligar:** rodar o roteador novo nas ~1.450 mensagens reais e
  contar quantos turnos abrem o portão e os falsos positivos. O A/B reenviando
  requests reais gasta a chave de produção ([[reference-ab-reenviando-request-do-log]]).

### 7.2 A busca

Com o portão aberto:
1. `core/trabalho/contexto.py` pede `trabalho.contexto` ao desktop, com timeout
   curto e cache do `neo.toml`.
2. **Desktop offline:** não há bloco. O Neo só comenta se o pedido depende do Mac
   ("o Mac está desligado, não consigo ver o VSCode").
3. A pasta da janela **focada** é a candidata ao projeto em foco. Sem foco
   conhecido (sem extensão), vale a pasta da sessão do Claude mais recente.

### 7.3 A camada `WORK_CONTEXT`

Formato:

```
## Trabalho agora
VSCode: neo (focada) · neo-core/core/pks/retriever.py · branch claude-code
Sessões do Claude: "Sessões Claude no neo-desktop-tauri" (agora), "Caderno VPS" (2 h)

## Projeto neo
- como_publicar: salvar no Mac = deploy (Mutagen + --reload); compilar antes de salvar
- como_testar: pytest roda no Mac pelo neo-core/.venv
- …
(mais na ficha: use projeto_consultar)
```

- Entra **fixa quando o portão abre**, com **teto próprio** (`teto_bloco_tokens`).
  Não disputa espaço com `USER_PKS`, que já tem problema de lotação
  ([[neo-pks-retrieval-cap-crowding]]).
- A ficha é ordenada pelos campos atualizados mais recentemente. O que não cabe
  fica para `projeto_consultar`.
- Com o portão fechado, a camada não existe.

### 7.4 Foco que gruda

Os metadados da sessão do Neo guardam:
- `projeto_em_foco` = `{entity_id, nome, pasta, desde}`;
- `sessao_claude_alvo` = `{sessionId, titulo, desde}`.

Regras:
- Valem por um tempo e um número de turnos (`neo.toml`).
- Continuação genérica ("e o deploy?") usa o foco.
- Pedido com outro tema, ou janela focada noutra pasta, reavalia o foco.
- O Neo anuncia a troca: "Agora no projeto frotas".

### 7.5 Custo

| Turno | Tokens a mais no prompt | Request a mais |
|---|---|---|
| Fora do trabalho | 0 | 0 |
| De trabalho | ~150 do retrato + até `teto_bloco_tokens` (padrão ~400) da ficha | 0, ou 1 se o cérebro chamar uma tool |
| Escolha de sessão | + fichas pela `claude_sessoes` (~1.500 tokens com cortes) | 1 rodada de tool |

## §8. Projeto no PKS

### 8.1 Vínculo entre pasta e `project`

- A entidade `project` (o tipo `EntityType.PROJECT` já existe) ganha o campo
  `pasta`. Pode haver mais de um valor, se o mesmo projeto morar em lugares
  diferentes.
- A ligação é pelo **prefixo mais longo**: `~/Documents/projects/neo/neo-core` cai no
  projeto `neo`, sem pergunta, porque está dentro de uma pasta já vinculada. O
  subprojeto aparece no retrato ("neo · neo-core").
- As relações que já existem (`works_on`, `uses_stack`) ligam o projeto ao Daniel e
  às tecnologias.

### 8.2 Projeto desconhecido: o Neo pergunta

**Quando:** só com o portão aberto, e só se a pasta em foco não tem ficha, não está
na lista de recusadas e não foi perguntada nos últimos `pergunta_reaberta_dias`.

**Como:**
1. O Neo **responde primeiro** ao que foi pedido.
2. No fim, faz a pergunta em uma linha: "Ainda não tenho uma ficha do projeto
   frotas (~/Documents/work/frotas). Quer que eu crie, para ir guardando o que você
   me ensinar?"
3. Grava em `pending_projeto` = `{pasta, nome_sugerido, expira}`, nos metadados da
   sessão.

**Próximo turno:** é um override como o `pending_clarification` e o
`claude_esperando`. Se a mensagem responde à pergunta, a intenção vira
`DIRECT_RESPONSE`, com uma nota no fim do prompt no padrão do
`_nota_claude_esperando`. Sem isso, um "sim" solto podia virar `run_command`, como
no job decc8224 ([[neo-claude-code-delegation-plan]]).

| Resposta | O que o Neo faz |
|---|---|
| "sim" | `projeto_criar` e confirma. Pergunta se o Daniel quer contar do que se trata, e se quer usar a memória do Claude Code como ponto de partida (§8.6). |
| "não" | A pasta entra na lista de recusadas (`user_preference`, category=trabalho). Ele diz "tudo bem, não pergunto mais sobre essa pasta". |
| Mudou de assunto | `pending_projeto` expira calado. Pergunta de novo só depois de `pergunta_reaberta_dias`. |
| "cria a ficha do projeto X" a qualquer hora | Cria direto, mesmo que a pasta tenha sido recusada antes. |

### 8.3 Já existe um projeto com esse nome

Se o `entity_resolver` acha um `project` com o mesmo nome ou apelido, mas sem a
pasta, o Neo pergunta "É o mesmo projeto neo que eu já conheço?".
- "sim" → `projeto_criar` **liga** a pasta à entidade existente (acrescenta o
  campo `pasta`);
- "não" → cria outra entidade, com nome que as diferencie.

Renomear apaga a duplicada ([[neo-pks-primary-user-name-hijack]]), então a ligação
nunca renomeia.

### 8.4 Ensinar

- "Lembra que nesse projeto o pytest roda pelo .venv" vira `pks_remember`. O
  cérebro escreve o fato **por extenso e com o nome do projeto**: "No projeto neo,
  o pytest roda no Mac pelo neo-core/.venv".
- O executor passa ao `PKSMemoryPlanner` uma dica:
  `sujeito_sugerido = project:<entity_id>`. Com ela, "nesse projeto", "aqui" e
  "nesse código" caem na entidade certa e não criam outra.
- Chaves sugeridas ao planner, não obrigatórias: `descricao`, `stack`, `como_rodar`,
  `como_testar`, `como_publicar`, `arquitetura`, `convencao`, `armadilha`,
  `decisao`, `pendencia`.
- Corrigir ou esquecer usa as tools que já existem (`pks_remember` sobrescreve,
  `pks_forget` apaga).

### 8.5 Consultar

`projeto_consultar(pergunta)` busca só nos campos do projeto em foco, com o mesmo
ranking do PKS. Serve para quando a ficha é maior que o bloco. Exemplo: "como
era o túnel caído mesmo?".

### 8.6 Ponto de partida: a memória do Claude Code

**Quando:** ao criar a ficha, o Neo oferece ("O Claude Code tem N anotações sobre
esse projeto. Quer que eu use como ponto de partida?"). Também a pedido, a qualquer
hora.

**Fluxo:**
1. O desktop devolve os arquivos (`trabalho.memoria_claude`).
2. O core filtra:
   - entram `type: project` e `type: feedback`;
   - **ficam fora** `reference` (SSH, IPs, painéis) e `user`;
   - linhas que parecem segredo (token, chave, `-i ~/.ssh/…`, IP com porta) são
     cortadas;
   - o resto vai com `sensitivity` `internal`.
3. **Prévia:** o Neo mostra a lista do que vai entrar (título + uma linha de cada) e
   espera o "pode importar". O Daniel pode tirar itens.
4. Um job (`projeto_importar_memoria`) passa os itens pelo `PKSMemoryPlanner` em
   lotes, para a ficha do projeto, e entrega um relatório no fim: N salvos,
   M já existiam, K recusados.
5. **Importação única, sem sincronia.** A memória do Claude continua viva no Claude
   Code. Uma nova importação só acontece a pedido, e pula o que já existe.

Custo: uma chamada do planner por lote. O tamanho do lote vai no `neo.toml`.

## §9. Sessões do Claude

### 9.1 A ficha

```json
{"sessionId": "…", "nome": "neo-e9", "origem": "claude-vscode", "status": "idle",
 "ultimo_uso_min": 161, "viva": true,
 "titulo": "…", "pasta": "~/Documents/projects/neo",
 "objetivo": "1º pedido, até 160 caracteres",
 "ultimos_pedidos": ["até 3, 120 caracteres cada"],
 "ultima_resposta": "até 200 caracteres",
 "arquivos": ["até 5, sem scratchpad"], "artefatos": ["títulos"]}
```

Os cortes e a idade máxima de sessão ficam no `neo.toml`. Sessões mais velhas que
`idade_max_sessao_dias` ficam de fora.

`arquivos` junta duas fontes:
- os `file_path` das chamadas `Edit`/`Write`/`MultiEdit`/`NotebookEdit` da própria
  sessão, ou seja, o que ela escreveu;
- os anexos `edited_text_file`, que são arquivos mudados **fora** da sessão
  enquanto ela estava aberta.

Ficam de fora o scratchpad e `~/.claude/`. Na F1, o desktop também devolve
`viva`, `vazia` (aba sem conversa) e `tarefa_do_neo` (pasta em `~/Neo/claude`).

### 9.2 Escolher a sessão

1. **Tema antes de recência.** O cérebro compara o pedido com título, objetivo,
   últimos pedidos, última resposta e arquivos. O título sozinho engana, porque
   envelhece.
2. **Pedido genérico** ("continua", "manda ele rodar os testes", "terminou?"):
   - a `sessao_claude_alvo` em foco;
   - senão, a sessão mais recente;
   - com a extensão, a aba do Claude que está na frente.
3. **Na dúvida, perguntar** com os títulos, curto para funcionar por voz: "Mando para
   'X' ou 'Y'?". Vale quando duas sessões combinam ou quando nenhuma combina.
4. **Grudar:** a escolha vira `sessao_claude_alvo` (§7.4).
5. **Anunciar:** sempre dizer para onde mandou ("Mandei para 'X'").

Mede-se com um conjunto de ~20 pedidos reais contra retratos reais das fichas:
quantos acertam, quantos perguntam e quantos erram.

### 9.3 Entregar

| Estado da sessão | Modo | Caminho | Disponível |
|---|---|---|---|
| Aberta (VSCode, CLI no VSCode, CLI em terminal de fora) | `enviar` | Mensageiro: `claude -p --no-session-persistence --allowedTools SendMessage,ListAgents`, ambiente limpo, texto num arquivo, instrução de repassar **literalmente**. Confere `success` no stream. | F2 |
| Aberta, aba do VSCode | `preencher` | Extensão: revelar → focar → colar. O Enter é do Daniel. | F5 |
| Aberta, CLI no terminal do VSCode | `preencher` | Extensão: Ctrl-U + bracketed paste, sem Enter. | F5 |
| Aberta, CLI em terminal de fora | só `enviar` | (não há como preencher sem Acessibilidade) | F2 |
| Fechada | — | §9.5 | F2 |

**Texto que vai na mensagem.** O que o Daniel pediu, mais uma linha fixa no fim: "Pedido
do Daniel enviado pelo Neo. Não responda por mensagem; responda aqui." Sem essa
linha, a sessão tenta responder ao mensageiro, que já encerrou (§3).

**Mexer na caixa de texto.** O modo `preencher` cola **depois** do que já estiver na
caixa. Por isso só vale para a sessão que o Daniel acabou de pedir, quando ele está
no computador.

**Sessão ocupada.** No modo `enviar`, a mensagem para uma sessão `busy` espera a
ferramenta em andamento terminar e entra **no mesmo turno** (F0b), como quando o
Daniel digita com o Claude trabalhando.
- A resposta sai misturada com a do trabalho em andamento.
- O Neo avisa ("a sessão 'X' estava ocupada; o pedido entrou no meio do trabalho
  dela").
- No retorno (§9.4), ele entrega a resposta do turno inteiro.
- Se o Daniel preferir esperar a sessão ficar livre, o desktop guarda o pedido e só
  envia depois de `busy → idle`. Ver §15.

### 9.4 Retorno

1. Depois de uma entrega com sucesso, o desktop acompanha o `status` da sessão alvo
   no registro até `busy → idle`. Usa um observador de arquivo, ou consulta com
   recuo, até `retorno_timeout_min`.
2. Quando a sessão fica `idle`, o desktop lê a **última resposta** no jsonl e o core
   posta uma mensagem proativa no chat do Neo, pelo `ProactiveDispatcher`: "A sessão
   'X' terminou: …". O texto vai **literal**, sem paráfrase do redator, pela lição
   do job 8f83b37c.
3. Com o modo `preencher`, só há retorno se o Daniel apertar Enter. Se a sessão
   não passar a `busy` em alguns minutos, o desktop para de acompanhar, calado.

### 9.5 Sessão fechada

Há dois caminhos:

- **`preencher` (F5, com a extensão).** Só para sessão **interativa**, do painel ou
  do CLI. `reabrir_preenchido` abre a sessão no painel do VSCode, com o histórico e
  o texto na caixa, e o Enter é do Daniel (F0b). Sessões de `-p` (`sdk-cli`) e
  sessões só com mensagem de outra sessão abrem conversa nova, então vão pelo
  caminho seguinte.
- **`enviar` (F2):**

1. Reaproveita o orquestrador do `claude_task` com uma **sessão externa**:
   `--resume <id>` na pasta original (`cwd` do jsonl), sem `--fork-session`.
2. Isso reaproveita a faixa (`ClaudeBar`), o diário, o diálogo de confirmação e a
   entrega literal que já existem.
3. A rodada aparece no histórico da sessão quando o Daniel a reabrir.
4. Antes de lançar, o desktop **confere de novo** que a sessão não está viva. Se
   alguém a abriu nesse meio-tempo, cai para o §9.3.

## §10. Segurança e privacidade

- **Portão de autoridade.** `claude_enviar`, `projeto_criar`,
  `projeto_importar_memoria` e a escrita via `pks_remember` só valem em turno do
  dono, limpo, no mesmo portão do `claude_task`. Ficam negados em turno
  contaminado, agenda e principal externo.
  - Mandar texto para uma sessão que edita arquivos em modo `auto` é o caminho
    mais perigoso do Neo para injeção ([[neo-security-audit-prompt-injection]]).
  - Conteúdo de página, e-mail ou arquivo nunca vira pedido para uma sessão.
- **Leitura também tem dono.** `trabalho.contexto` e `claude_sessoes` nunca
  respondem a terceiro (WhatsApp de outra pessoa, agenda). O que o Daniel está
  fazendo é dado dele.
- **Confirmação no modo `enviar`.** Diálogo no desktop com o destino e o texto
  exato, trazendo a janela para a frente como o `claude_cli` já faz. O modo
  `preencher` dispensa: o Enter é a confirmação.
- **Permissão não se lava.** O Neo não pede a uma sessão o que foi negado noutra.
  Do outro lado, a sessão trata o pedido como de um colega, dentro do modo de
  permissão dela. Ela pode parar e perguntar, e isso é esperado.
- **Nada fica na VPS.**
  - O retrato e as fichas não são gravados no `tool_trace` nem no session log. O
    trace guarda só `sessionId`, nome e título.
  - O painel Contexto mostra o prompt ao vivo, sem guardar.
  - Só o que o Daniel mandou guardar vai para o PKS.
- **Ambiente limpo** em todo `claude` lançado pelo desktop (§5.1).

## §11. Configuração

**Seção `[trabalho]` no `config/neo.toml`**, lida via `shared/ajustes.py`
([[feedback-constantes-no-neo-toml]]): `cache_retrato_s`, `timeout_desktop_s`,
`teto_bloco_tokens`, `recentes_horas`, `idade_max_sessao_dias`, os cortes da ficha
(objetivo, pedidos, resposta, arquivos), `foco_ttl_min`, `foco_ttl_turnos`,
`pergunta_reaberta_dias`, `retorno_timeout_min`, `lote_importacao`.

**Chave liga/desliga** no neo-panel (Configurações → Trabalho), no padrão do
`chave.py`. Ela vale na hora. O `.env` é só o padrão.

**Lista de pastas recusadas e vínculos** (pasta → projeto): visível e editável no
painel, na F6.

## §12. Fases

| Fase | Entrega | Pronto quando |
|---|---|---|
| **F0** ✅ 08/11 | Medição (§3) | — |
| **F0b** ✅ 08/11 | Os 6 itens que faltavam na F0 (§3) | — |
| **F1** ✅ 08/11 | Desktop: `claude.sessoes`, `trabalho.contexto` (fonte arquivos), conferência de processo vivo, cortes e filtros; testes Rust com pastas de exemplo (registro, jsonl, lock) | ✅ `cargo test` passa (18 + 2 `--ignored` no Mac real); as 9 sessões vivas batem com o `ListAgents`. O canal `/ws/devices/control` só vai ser exercitado quando o core chamar, na F2. |
| **F2** ✅ 08/11 (falta E2E pelo chat) | Sessões no cérebro: `claude_sessoes`, `claude_enviar` em modo `enviar` (mensageiro), §9.4 (retorno), §9.5 (sessão fechada pelo orquestrador) e portão, sem diálogo no envio; avaliação de escolha de sessão (§9.2) ainda por fazer | ✅ E2E do desktop: entrega numa aba real em 6,7 s e a resposta volta. Falta: E2E por voz (pedido → sessão certa → aparece no VSCode → "terminou" no chat), E2E de sessão fechada, E2E com duas sessões e pedido da anterior. |
| **F3** | Contexto de projeto: campo no roteador (medido no corpus antes), camada `WORK_CONTEXT`, vínculo pasta → `project`, pergunta e recusa (§8.2–8.3), `pks_remember` com projeto em foco, `projeto_consultar`, foco que gruda | E2E: pasta nova → pergunta → "sim" → ensina um fato → numa sessão nova do Neo, "como publico?" volta com o fato. Turno social continua com 0 token a mais. |
| **F4** | Ponto de partida a partir da memória do Claude Code (§8.6) | E2E com a pasta `neo`: prévia → aprovação → relatório; nenhum segredo entrou |
| **F5** | Extensão `neo-vscode`: estado em arquivo, foco, arquivo ativo, branch; modo `preencher` em aba e terminal; a aba da frente desempata pedido genérico | E2E: "preenche no Claude: …" cai na aba certa sem enviar; retrato mostra o arquivo ativo |
| **F6** | Painel (chave, vínculos, pastas recusadas). Resumo em uma frase por sessão, guardado no Mac, **só se** a avaliação do §9.2 mostrar erro por título velho | Conforme o que a avaliação pedir |

A ordem é de propósito. F1 a F3 já entregam o valor principal **sem extensão**. A
extensão (F5) traz precisão (foco e arquivo ativo) e o modo `preencher`.

## §13. Registro de execução

### F0 e F0b (08/11/2026, CLI 2.1.319, extensão 2.1.314/2.1.319, conta Pro)

- Resultados no §3.
- Como foi feito: extensão rascunho (HTTP local com token, rotas `/estado`, `/cmd`,
  `/clipboard`, `/terminal-*`) numa janela Extension Development Host, aberta com
  `env -i … code --new-window --disable-workspace-trust --extensionDevelopmentPath=…`
  numa pasta do scratchpad, e um mensageiro `claude -p` com `env -i`.
- Limpeza: as transcrições das pastas de teste foram apagadas e a área de
  transferência foi devolvida (só texto).

Bloqueios e lições:
- O modo auto do Claude Code bloqueou o teste com `bypassPermissions`.
- A sessão de destino não deixa rodar `sleep` em primeiro plano: manda para segundo
  plano e fica `idle`. Para manter uma sessão ocupada, use `ping -c N`.

### F1 (08/11/2026) — implementada no desktop, sem chamada do core

O que entrou (`neo-desktop-tauri`, branch `claude-code`, nada commitado):
- `services/claude_sessoes.rs`: registro e conferência de processo vivo (um
  `ps -A -o pid=,lstart=` com `TZ=UTC` para todos), ficha, `claude.sessoes`,
  `sessao_viva`.
- `services/trabalho.rs`: `trabalho.contexto`.
  - Janelas pelos locks, pulando o lock de Code morto e nunca lendo o `authToken`.
  - Branch lido direto do `.git/HEAD`, com suporte a worktree (`gitdir:`) e sem
    rodar `git`.
  - Cada sessão aponta a janela mais funda que contém a pasta dela.
- Despacho de `trabalho.*` no `websocket.rs` e capacidade `trabalho` no
  `device_control.rs`.
- `claude_cli.rs`: a trava do `--resume` passou a consultar o registro primeiro
  (`onde_esta_aberta`) e aceita `--resume=<id>`.
  - **Era um furo:** o painel do VSCode roda com `--resume=<id>` e a aba nova nem
    leva `--resume`. O `ps` sozinho não via sessão aberta no VSCode, e um
    `claude_reply` poderia dividir o histórico dela.
  - O `sessao_existe` passou a usar o mesmo localizador de transcrição.

O que foi medido no Mac real e mudou o código:
- **Tempo.** A 1ª versão levava ~1,3 s por chamada no build de debug do
  `tauri dev`: lia 4 MB do fim de cada sessão e varria cada linha 6 vezes.
  - Agora só os primeiros 512 bytes decidem o tipo da linha (o 1º `"type":"`), e
    só as linhas de conversa olham até 4 KB.
  - Começo e fim começam pequenos (64 KB / 512 KB) e crescem ×4 só quando falta
    o 1º pedido ou os últimos pedidos.
  - Resultado: ~0,5–0,7 s para as 9 sessões vivas (2–88 ms cada).
- **Ordem das chaves.** Na linha do assistente, `message` vem primeiro e o
  `"type":"assistant"` fica no fim, a até 78 KB do começo. A marca usada é o
  `"role":"assistant"` (até 290 bytes). Os testes montam as linhas na ordem real,
  porque o `json!` ordena as chaves.
- **Captura de tela incha a transcrição.** A sessão desta pesquisa tinha 18 MB,
  16 MB em resultados de ferramenta. Os 4 MB do fim guardavam um pedido só, daí a
  janela que cresce.
- **`edited_text_file` não é o que a sessão editou.** Ele conta o que mudou fora
  dela. As edições da sessão vêm dos `tool_use` `Edit`/`Write` (§9.1).

Testes:
- 18 testes de unidade em `claude_sessoes`, `trabalho` e `claude_cli`.
- `cargo test --lib -- --ignored sessoes_reais retrato_real --nocapture` imprime o
  retrato e as fichas reais e confere que cada sessão viva é achada pela trava.

### F2 (08/11/2026) — implementada, sem E2E pelo chat

**Core** (publicado às 23:30, em grupos, numa ordem em que cada passo continua
importável; antes, a suíte inteira passou na cópia de trabalho: 4.029 testes):
- **Tools:** `claude_sessoes` e `claude_enviar` em `core/jobs/tools.py`, despachadas
  pelo `JobToolExecutor`. Saem com a chave do Claude Code (`chave.TOOLS`); a chave
  própria é a F6.
- **Portão:**
  - `claude_enviar` entra na regra NEGAR do `claude_task`: turno contaminado,
    pedido de fora e agenda;
  - `claude_sessoes` é negada para pedido de fora e agenda (regra antes do
    `is_sink`, porque é leitura).
  - Classes de risco: `claude_sessoes` = leitura privada, `claude_enviar` = atuação.
- **Lista para o cérebro:** a ficha vem enxuta (onde, estado, "há 3 h"), sem as
  tarefas do próprio Neo e sem as abas vazias.
- **Envio:** o pedido vai com um rodapé fixo ("Pedido do Daniel, enviado pelo
  Neo… Não responda por mensagem").
- **Volta:** o `claude_enviar` cria um job `kind="claude_sessao"`, rodado por
  `core/jobs/runner_claude_sessao.py`.
  - Ele pergunta ao Mac (`claude.situacao`) até a sessão ficar `idle` com horário
    depois do envio.
  - Entrega a última resposta LITERAL.
  - Desiste com aviso por prazo (`retorno_timeout_min`) ou com o Mac fora do ar
    (`fora_do_ar_min`).
  - Sobrevive a reinício (`REATTACHABLE_JOB_KINDS`).
- **Sessão fechada:** o Mac responde `sessao_fechada` com a pasta onde a sessão
  nasceu. O core cria um job `claude_code` com `claude.session_id` +
  `sessao_iniciada`, e o orquestrador retoma com `--resume`.
- **Ajustes:** seção `[trabalho]` no `neo.toml` (§11).
- **`ai_service`:** `claude_enviar` entra nas tools assíncronas e nas de adiamento
  legítimo. O filtro pela chave usa `chave.TOOLS`. A nota da tarefa recente do
  Claude passou a dizer que sessão DELE é `claude_sessoes`/`claude_enviar`.
- Testes: `tests/claude_code/test_sessoes_do_daniel.py` (14).

**Desktop:**
- `claude.enviar` (em `claude_cli.rs`): um `claude -p` mensageiro com
  `--no-session-persistence --model haiku`, modo `default`, só SendMessage e
  ListAgents, sem MCP.
  - O texto vai entre marcas aleatórias e a saída vai para arquivo.
  - O desktop confere no stream o `tool_use` (para quem, qual texto) e o
    `"success":true`.
  - Devolve `enviado_em` no relógio do Mac e `texto_igual`.
- `claude.situacao` (em `claude_sessoes.rs`): viva, status, desde quando, e a
  última resposta do assistente desde o envio. As quebras de linha ficam; a janela
  do fim cresce até ver uma resposta de antes do envio.
- A ficha ganhou `pasta_inicial`: o `--resume` tem de rodar onde a sessão nasceu.
- **E2E real** (aba descartável numa janela de teste): entrega em **6,7 s**, texto
  idêntico, e a resposta "OK-NEO-F2" voltou pelo `claude.situacao`.
  `NEO_TESTE_SESSAO=<id> cargo test --lib -- --ignored envio_real --nocapture`.

**Bloqueado:** o modo auto do Claude Code negou a edição que tirava o diálogo
"Rodar o Claude Code?" do `claude.start` ao retomar uma sessão que já existe no
Mac. Por isso o caminho da **sessão fechada** ainda pede esse clique na 1ª
retomada de cada tarefa. A **aberta**, que é o caso principal, não pede nada.

## §14. Riscos e gotchas

- **Formatos internos.** O registro de sessões, o jsonl e os comandos da extensão do
  Claude não são API documentada. Parse tolerante, todo campo opcional, registrar
  `version`; se algo mudar, cair para o caminho seguinte (extensão → mensagem →
  perguntar ao Daniel).
- **Título envelhece** (§3). O roteamento nunca decide só pelo título.
- **A sessão pode recusar** o pedido vindo de colega, se for arriscado. O Neo
  relata isso; não insiste nem reformula para contornar.
- **Local = PROD.** Salvar arquivo do core é deploy na hora
  ([[neo-dev-topology-prod-tunnel]]). Compilar antes de salvar e ligar tudo atrás
  da chave.
- **Tool nova em `JOB_TOOLS`:** salvar juntos os dois filtros (§5.4).
- **Proto não muda.** Tudo passa por ações JSON no `/ws/devices/control`. Se um dia
  mudar, há o gotcha do `grpc_gen/neo_desktop.rs` copiado à mão.
- **UI do desktop:** o turno é `isExecuting`, não `isLoading` (estado morto).
- **Área de transferência** no `preencher_aba`: a extensão guarda e devolve só
  **texto**. Imagem ou texto formatado que estivesse lá se perde. Documentar e, se
  incomodar, trocar por outra forma de inserir.
- **Foco errado depois de trocar de janela.** O retrato do turno manda mais que o
  foco que grudou. O Neo sempre anuncia a troca.
- **Várias janelas sem extensão:** o retrato vê todas as janelas (um lock por janela,
  F0b), mas não sabe qual está focada. O desempate fica pela sessão do Claude mais
  recente.
- **Pedido para sessão ocupada:** ele entra no meio do turno em andamento (F0b). O
  Neo avisa, e a decisão de esperar ou não fica no §15.

## §15. Decisões que ainda são do Daniel

1. ~~Modo padrão quando ele está no Mac~~ **Decidido em 08/11: `enviar`.** O Neo
   não depende do Daniel apertar Enter para interagir com a sessão. O `preencher`
   (F5) fica como opção, só quando ele pedir.
2. ~~Confirmação no modo `enviar`~~ **Decidido em 08/11: sem diálogo.** Quem
   guarda é o portão (pedido do Daniel, turno limpo) e o anúncio do destino no
   chat. Falta decidir a **sessão fechada**: o `claude.start` ainda pede o
   diálogo na 1ª retomada, porque a edição que o tirava foi barrada pelo modo
   auto (§13).
3. **Contexto de trabalho fora do Mac** (celular ou WhatsApp **do dono**, com o Mac
   ligado): liga? Recomendação: sim, com o mesmo portão.
4. **O que entra no ponto de partida** (F4): `project` + `feedback` (recomendado)
   ou também `reference` com os segredos cortados?
5. **Sessão ocupada** (F0b): mandar na hora, e o pedido entra no meio do trabalho
   dela, ou guardar e mandar quando ela ficar livre? Recomendação: na hora, com o
   aviso. É o que acontece quando o Daniel digita com o Claude trabalhando.
