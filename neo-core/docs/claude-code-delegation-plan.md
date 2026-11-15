# Claude Code acoplado ao Neo (`claude_task`): plano

> Status: **F0 local medida e F1 (orquestrador + corpo local) implementada e LIGADA
> em produção em 07/11/2026** (`CLAUDE_CODE_ENABLED=true`). Falta o E2E pelo chat.
> Registro em §13.
> Medido nesta sessão: CLI `2.1.302`; a conta do Daniel está logada como
> `claude.ai`, `subscriptionType: "pro"` (`claude auth status`).
> Fontes: code.claude.com/docs/en/authentication, /legal-and-compliance,
> /headless, /cli-reference, o `--help` da CLI instalada e o código do
> `browser_local` (core e desktop).

## §1. A ideia

Quando o Daniel pede explicitamente ("faz com o Claude", "pede pro Claude") ou,
numa fase posterior, quando a tarefa é difícil demais para o cérebro do Neo, o Neo
**delega ao Claude Code**: o binário oficial, sem modificação. O Neo monta a
tarefa, acompanha o que o Claude vai fazendo, decide ao fim de cada rodada se
aceita, manda continuar ou pergunta ao Daniel. No fim, pega o resultado e trabalha
em cima dele (entrega, resume, fala).

O Claude pode rodar em **dois lugares**, como o navegador:

| Corpo | Onde | Quem autentica | Para quê |
|---|---|---|---|
| **local** | o Mac do Daniel, pelo canal `/ws/devices/control` (o mesmo do `browser_local`) | **o próprio Daniel**, que já fez login no Claude Code dele. O Neo não toca em credencial. | Trabalho nos projetos e arquivos dele, com as ferramentas dele |
| **servidor** | container `neo-claude` na VPS, ao lado do sandbox | token `setup-token` da conta dele, injetado pelo core (§3.2) | Tarefa que não depende do Mac, ou com o Mac desligado |

**O orquestrador é um só.** Ele não sabe em qual corpo o Claude roda: fala com uma
interface de seis operações (§2), e cada corpo a implementa do seu jeito.

## §2. Um orquestrador, dois corpos

### 2.1 A interface do corpo

O stream de eventos é o mesmo nos dois lugares, porque o binário é o mesmo
(`claude -p --output-format stream-json`). Por isso o corpo **não interpreta
nada**: só lança, entrega bytes e para. É o mesmo princípio do `browser_local`, em
que o device não decide nada.

| Operação | O que devolve | Servidor (`neo-claude`) | Local (Mac) |
|---|---|---|---|
| `saude()` | instalado? versão? logado? `authMethod`? | `docker exec claude --version` + `auth status` com o token injetado | ação `claude.health` no desktop |
| `lancar(rodada)` | pid / id da rodada | `docker exec` destacado (`setsid nohup`) | ação `claude.start`; o desktop sobe um processo destacado |
| `ler(rodada, offset)` | bytes novos do stream, vivo?, código de saída | `read_tail` do arquivo no `neo-claude` | ação `claude.read` |
| `parar(rodada)` | ok | `kill` no grupo | ação `claude.stop` |
| `recolher(rodada, caminhos)` | arquivos para o Neo trabalhar em cima | copia de `/work` para o File Store | o desktop sobe os arquivos pelo caminho dos anexos (com teto) |
| `limpar(rodada)` | ok | `claude purge` + apaga `/work/<job>` e o stream | apaga só a pasta da rodada (stream, pid); o projeto e o histórico do Claude dele ficam |

### 2.2 O que é do orquestrador (compartilhado)

Tudo que não é "onde roda" mora no core, uma vez só:

- **leitura do stream**: transforma os eventos em notas curtas para o caderno e para
  a janela Tarefas, guarda `session_id`, `num_turns`, `total_cost_usd` e o
  `rate_limit_event`, e reconhece o erro de limite atingido;
- **vigias**: relógio total, stream parado por X min e `--max-turns`;
- **as rodadas** (§6): o supervisor decide entre aceitar, continuar, perguntar ou
  desistir;
- **as recusas de permissão** (`permission_denials` do `result`): viram pergunta ao
  Daniel, e a resposta dele vira a liberação da rodada seguinte;
- **mensagens dele no meio da tarefa** (`claude_reply`);
- **reatar depois de reiniciar** o core;
- o **`then`** que trabalha em cima do resultado;
- o **portão** (§9).

### 2.3 Onde mora o código

```
neo-core/core/claude_code/
  orquestrador.py      ← rodadas, supervisor, perguntas, permissões, reatar
  stream.py            ← stream-json → eventos tipados → notas do caderno
  corpo.py             ← Protocol com as 6 operações + tipos (Rodada, Leitura, Saude)
  corpo_servidor.py    ← docker exec no neo-claude (reusa WorkbenchSandbox)
  corpo_local.py       ← broadcaster.request(device_id, "claude.*", …)
neo-core/core/jobs/runner_claude.py   ← fino: monta o orquestrador com o corpo do job
neo-desktop-tauri/src-tauri/src/services/claude_cli.rs  ← as ações claude.* no Mac
```

Os testes do orquestrador usam um **corpo falso**, que devolve streams gravados de
rodadas reais (a F0 produz esses arquivos). Assim os dois corpos ficam presos ao
mesmo contrato.

## §3. Autenticação

### 3.1 Modo local: o Neo não toca em credencial

- O Claude Code do Mac usa o login que o Daniel já fez (`claude auth login`, que no
  macOS fica no Keychain). O desktop **nunca** roda `auth login`, `setup-token` nem
  `logout`, e o core nunca recebe nem guarda nada disso.
- **Só funciona se ele já estiver logado.** O `claude.health` roda
  `claude auth status --json` no mesmo ambiente em que a rodada vai rodar. Se vier
  `loggedIn: false`, a resposta é "abre o terminal e faz `claude` → `/login`; o Neo
  não faz isso por você".
- O `authMethod` vai para o job. Se a `ANTHROPIC_API_KEY` estiver no ambiente do
  `tauri dev`, ela **ganha do login** e a conta vai para a API. O health mostra isso
  antes da primeira rodada.
- Nos termos, é o caso mais limpo: é o usuário rodando o próprio binário, com a
  própria assinatura, na própria máquina. O Neo faz o papel de um script.

### 3.2 Modo servidor: as três formas que a CLI aceita

| Forma | O que é | Cobrança | Sobrevive a restart e rebuild? | Serve aqui? |
|---|---|---|---|---|
| `claude setup-token` | Token OAuth de **1 ano**, gerado pelo fluxo de login da Anthropic no navegador. Ele é impresso no terminal, a CLI não o salva, e quem usa lê `CLAUDE_CODE_OAUTH_TOKEN`. A doc diz que é para "CI pipelines, scripts, or other environments where interactive browser login isn't available". | Cota da assinatura | **Sim, por construção.** O token fica fora do container (§3.2.1). Login de novo: 1 vez por ano. | **Sim, recomendado** |
| `claude auth login` dentro do container | Grava `~/.claude/.credentials.json` (0600) com access token curto e refresh token. | Cota da assinatura | **Frágil.** Só sobrevive se `~/.claude` for volume, e o refresh pode se perder (§3.2.1). Cada login de novo exige `ssh` + `docker exec -it` na VPS. | Não |
| `ANTHROPIC_API_KEY` (Console) | Chave de API paga por uso. | Por token, separada da assinatura | Sim, também fica fora do container. Não vence. | Alternativa limpa se o uso crescer (§3.3) |

Fatos da doc que mudam o desenho:

- **O token do `setup-token` só faz chamada de modelo:** "It can only make model
  requests, so it can't establish Remote Control sessions or fetch claude.ai
  connectors." Se vazar, quem pegar gasta a cota, mas **não lê as conversas do
  claude.ai nem os conectores** (Gmail, Calendar).
- **Precedência:** `ANTHROPIC_AUTH_TOKEN` > `ANTHROPIC_API_KEY` > `apiKeyHelper` >
  `CLAUDE_CODE_OAUTH_TOKEN` > login salvo. Em `-p` a doc diz que a API key "is
  always used when present". Vale nos dois corpos.
- **`--bare` não lê `CLAUDE_CODE_OAUTH_TOKEN`** e também não lê o login salvo. Não
  usar `--bare` em nenhum corpo.

#### 3.2.1 Persistência: o container é descartável

Requisito do Daniel (07/11): rebuild e restart acontecem o tempo todo, e o login
não pode cair junto. A regra que resolve isso é que **nada de autenticação mora no
`neo-claude`**. O container pode ser reconstruído, recriado ou ter o volume
apagado, e o login continua igual.

- **Onde o token mora:** no Postgres (volume `pgdata`), cifrado com
  `NEO_CRED_KEY`/Fernet, como a chave do `llm_provider` (`modules/llm_catalog/store.py`).
  O `neo-core` lê o token **a cada rodada** e o injeta no exec. O container nunca o
  guarda.
- **Por que o `setup-token` aguenta isso:** ele foi feito para CI, em que cada
  execução é uma máquina nova (um runner do GitHub Actions nasce e morre a cada
  job). Um container recriado é a mesma situação.
- **Por que o `auth login` no container não aguenta:**
  - a credencial mora em `~/.claude/.credentials.json`, dentro do container. Um
    rebuild que esqueça o volume leva o login embora;
  - mesmo com o volume, o access token é curto e se renova pelo refresh token, que
    muda a cada renovação. Duas rodadas em paralelo renovando o mesmo arquivo podem
    invalidar uma à outra;
  - cada perda exige login interativo na VPS (`ssh` + `docker exec -it`), que é
    justamente o "ficar logando" que ele não quer.
- **Por que o banco, e não o `.env`:**
  - trocar o token no `.env` exige `docker compose up -d neo-core`, porque o
    `--reload` não relê o `.env`;
  - o Mutagen copia o `.env` da raiz para o Mac em texto puro;
  - no banco, colar o token novo no painel vale **na rodada seguinte**, sem
    reiniciar nada.

  O `.env` (`NEO_CLAUDE_OAUTH_TOKEN`) fica só como reserva, com a mesma precedência
  do catálogo de LLM: o banco vence. O nome é diferente de `CLAUDE_CODE_OAUTH_TOKEN`
  de propósito, para nenhum processo herdar o token por engano.

| Evento | Login | Tarefa em andamento |
|---|---|---|
| Restart do `neo-core` (salvar `.py`) | intacto (banco) | reata (§6) |
| Rebuild ou recreate do `neo-claude` | intacto (não mora lá) | o processo morre, mas a sessão fica no volume `neo-claude-home` e `--resume` continua de onde parou |
| Volume `neo-claude-home` apagado | intacto | perde a sessão; a tarefa recomeça do zero |
| Recreate do `neo-postgres` com o volume mantido | intacto | não afetada |
| Troca da `NEO_CRED_KEY` | perdido (não decifra mais) → colar de novo | não afetada |
| 1 ano depois, ou token revogado | aviso → colar um novo | fica `waiting` até colar, e depois `--resume` |

**Quantas vezes ele vai logar:** uma vez por ano, ou se o token for revogado. Para
isso não pegá-lo de surpresa:

- **aviso por data**, 30 dias antes de completar 1 ano (a data de criação fica
  gravada junto do token);
- **aviso na primeira recusa:** se a rodada voltar com erro de autenticação, o job
  **não falha**. Ele vira `waiting` com a instrução exata: "rode `claude
  setup-token` no Mac e cole em Painel → Claude". Ao colar, a tarefa continua
  pela mesma sessão.

Recomendação para o servidor:

1. **O Daniel roda `claude setup-token` no Mac** (é o fluxo da própria Anthropic) e
   cola o token no painel.
2. **Onde guardar:** no banco, cifrado, **fora do cofre**, pelo mesmo motivo da
   chave do `llm_provider`: o `vault_list` mostra todo item do cofre ao cérebro.
   Desde a primeira fase do servidor; o `.env` é só a reserva.
3. **Quem vê o token:** só o `neo-core`, que o injeta **no exec do processo `claude`**
   (`Env` do `docker exec`). O token não entra no ambiente do container, não vai
   para disco e não aparece em prompt, caderno, log ou tool result.
4. **Na F0**, descobrir e anotar aqui:
   - o que revoga o token (`claude auth logout` no Mac? trocar a senha? "sair de
     todos os dispositivos" no claude.ai?);
   - se `claude auth status` confere o token no servidor da Anthropic ou só lê a
     variável. Se só ler a variável, a prova real de que o token vale é a primeira
     rodada.

### 3.3 O que os termos dizem

Trechos de `/legal-and-compliance`:

> "OAuth authentication is intended exclusively for purchasers of Claude Free, Pro,
> Max, Ultra, Team, and Enterprise subscription plans and is designed to support
> ordinary use of Claude Code and other native Anthropic applications."

> "Anthropic does not permit third-party developers to offer Claude.ai login into
> their own applications, or to route requests through Free, Pro, Max, or Ultra plan
> credentials on behalf of their users. Moreover, developers may not collect, store,
> or intermediate Claude.ai credentials or session tokens — sign-in to a Claude
> account must complete through Anthropic's own flow."

> "Nor does it prevent an end user from signing in to the unmodified Claude Code
> binary with their own Claude subscription…"

> "Advertised usage limits for Pro, Max, and Ultra plans assume ordinary,
> individual usage of Claude Code and the Agent SDK."

Leitura:

- **No corpo local** não há credencial nenhuma passando pelo Neo. É o binário dele,
  com o login dele.
- **No corpo servidor**, o Daniel é o assinante e o único usuário, faz o login pelo
  fluxo da Anthropic e roda o binário oficial: é o uso de "scripts/CI" que a
  página de autenticação recomenda.

Regras para os dois corpos:

- **Nunca** usar o token para chamar a API direto. Só o binário `claude` usa o token.
- **Nunca** ligar isso para outra pessoa. Um só principal: o Daniel.
- **Zona cinza:** "ordinary, individual usage". O uso explícito e sob demanda é o
  mais defensável. Se virar automático e frequente, o certo é usar **API key** no
  servidor. O código não muda: só a variável injetada.

### 3.4 Cota e privacidade (os dois corpos)

- **A cota é uma só:** a conta Pro do Daniel, a mesma do claude.ai e do Claude Code
  que ele usa à mão. Rodar no Mac não economiza nada. O Pro é o plano com o menor
  limite, então o orquestrador precisa:
  - ler o `rate_limit_event`;
  - dizer "o limite do Claude volta às HH:MM" em vez de "falhou";
  - não gastar a cota em tarefa que o Neo resolve sozinho.
- **Plano de consumidor:** o que vai ao Claude cai sob os Consumer Terms. O Daniel
  deve conferir a opção de uso dos chats para treinamento em claude.ai →
  Configurações → Privacidade.
- **Transcrições:**
  - no servidor, saem no fim, conforme [[feedback-no-conversation-copies-on-vps]];
  - no Mac, ficam no `~/.claude/projects` dele, como qualquer sessão dele. Com isso
    ele pode abrir a sessão no terminal (`claude --resume <id>`) e assumir (§4.4).

## §4. Corpo local (o Mac)

### 4.1 As ações no desktop

As ações vão pelo mesmo canal `/ws/devices/control`. No `websocket.rs`, o prefixo
`claude.` é desviado para `services/claude_cli.rs`, como o `browser.` já é desviado
para `browser_local.rs`. Cada comando já roda na sua própria tarefa, então um
`claude.read` não espera outro.

| Ação | Faz |
|---|---|
| `claude.health` | Acha o binário (`~/.local/bin/claude`, Homebrew; por último `zsh -lc 'command -v claude'`, porque um app aberto fora do terminal não tem o PATH dele). Roda `--version` e `auth status --json`. |
| `claude.start` | Recebe `run_id`, `cwd`, a lista de flags (montada pelo core) e o prompt. Mostra o **diálogo de confirmação** (§4.3) e sobe o processo. |
| `claude.read` | Devolve `{chunk, offset, vivo, exit_code}` a partir de um offset do `stream.jsonl`, com teto de bytes. |
| `claude.stop` | SIGTERM no grupo; SIGKILL depois de 5 s. |
| `claude.list` | Rodadas que existem na pasta de rodadas, para o core reatar depois de reiniciar. |
| `claude.collect` | Sobe ao File Store os arquivos pedidos pelo caminho que os anexos já usam, com teto por arquivo e no total. |
| `claude.cleanup` | Apaga a pasta da rodada. |

### 4.2 Como o processo sobe

- **Pasta da rodada:** `~/Library/Application Support/<app>/claude-runs/<run_id>/`,
  com `prompt.md`, `stream.jsonl`, `stderr.log` e `meta.json` (pid, hora de início,
  cwd, `session_id`).
- **Processo destacado:** `Command::new(<caminho absoluto>)` no `cwd` da tarefa, com
  `process_group(0)`, como o `shell.rs`:
  - o stdin é o `prompt.md` (`claude -p` lê o prompt do stdin). O texto não vai no
    argv, porque lá aparece no `ps` e tem limite de tamanho;
  - stdout e stderr vão para **arquivo**, não para pipe. Assim o processo sobrevive
    ao reinício do `tauri dev`: sem pipe não há SIGPIPE, e o Claude segue escrevendo;
  - **não** entra no `track_pid` do `shell.rs`. O Stop do chat para o turno, não a
    tarefa, que se cancela por `job_cancel`.
- **Ambiente:** herda o do usuário, menos `CLAUDECODE` e as variáveis de sessão
  `CLAUDE_CODE_*` / `CLAUDE_*`. Medido nesta sessão: um terminal aberto de dentro do
  Claude Code carrega mais de 20 delas (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`,
  `CLAUDE_CODE_ENTRYPOINT`…). Um `tauri dev` lançado dali passaria essas variáveis
  adiante, e o Claude filho se acharia sessão filha de outra.
- **Saber se está vivo:** `kill(pid, 0)`, conferindo também a hora de início, para
  não confundir com um pid reaproveitado. O `claude.read` funciona mesmo que outra
  vida do app tenha subido a rodada.

### 4.3 Segurança na máquina dele

O corpo local é o de **maior risco**: rede livre, os arquivos dele, os logins dele.
O desktop é a última linha, como o `command_policy.rs` é para o `run_command`.

- **Confirmação no Mac antes da primeira rodada de cada tarefa**, com um diálogo
  nativo (o mesmo padrão de desligar e suspender): "O Neo quer rodar o Claude Code
  em `~/projects/x`: <objetivo>. [Permitir] [Recusar]". Quem decide é quem está na
  frente da máquina. As rodadas seguintes da mesma tarefa não perguntam de novo.
- **Pasta:** precisa existir, ficar dentro do `$HOME` e não ser o `$HOME` em si nem
  `~/.ssh`, `~/Library`, `~/.claude`, `~/.config` ou `~/.aws`. Sem pasta, o desktop
  cria `~/Neo/claude/<job8>/`.
- **Flags:** o desktop confere a lista contra uma allowlist. Ele **recusa**
  `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`,
  `bypassPermissions`, `--mcp-config`, `--plugin-dir`/`--plugin-url` e `--add-dir`
  fora da pasta.
- **Permissões:** `--permission-mode acceptEdits`. O `auto` funciona em `-p`, mas
  **medido na F0, ele deixou escrever fora da pasta** (`/tmp/neo-f0-fora.txt`): ele
  julga risco, não lugar. Por isso o desktop recusa `auto`. Junto vai
  `--permission-prompts none`: o que pediria aprovação é **negado na hora**
  e registrado em `permission_denials`. Quem pergunta ao Daniel é o orquestrador
  (§6, passo 6), igual nos dois corpos. A rodada nunca fica parada esperando um
  prompt que ninguém vê.
- **MCP e conectores desligados por padrão** (`--strict-mcp-config`). O Claude dele
  tem Gmail, Calendar e o Docs do claude.ai. Uma rodada dirigida pelo Neo, com
  injeção no meio, mais acesso ao Gmail é exatamente o ataque do
  `release-request-plan`. Liga só quando ele pede, por tarefa.
- **Settings e CLAUDE.md** do projeto e do usuário continuam valendo: as regras dele
  e as do projeto são úteis e são o Claude que ele conhece.

### 4.4 Mac dormindo e o Daniel assumindo

- **Device offline** (Mac dormindo, desktop fechado): o processo pausa junto com o
  Mac. O job **não falha**. Fica `running` com a nota "Mac fora do ar desde HH:MM" e
  volta a ler quando o canal reconecta. Depois de 6 h (configurável), vira
  `waiting` e pergunta.
- **Assumir:** a bolha da tarefa mostra `claude --resume <session_id>` com a pasta.
  Se ele abrir a sessão no terminal com uma rodada do Neo rodando, as duas brigam
  pela mesma sessão. Por isso o botão "assumir" primeiro para a rodada
  (`claude.stop`), marca o job como `assumido` e só então mostra o comando. A
  partir daí o orquestrador não mexe mais.

## §5. Corpo servidor (`neo-claude`)

### 5.1 Por que não no `neo-workbench`

1. **O `neo_bash` do Neo roda lá.** Qualquer comando, inclusive um que nasceu de
   injeção numa página lida, faria `env` ou `cat /proc/*/environ` e mandaria o token
   pela internet do tinyproxy. Medimos em 03/10 que **o modelo obedece injeção** e
   que quem segura é a estrutura ([[neo-security-audit-prompt-injection]]).
2. **O `/workspace` tem arquivos de outras conversas.**
3. **A saída do workbench é a internet inteira.** O Claude só precisa da Anthropic.

### 5.2 Arquitetura

```
                       docker exec (Env: token só neste processo)
 neo-core ───────────────────────────────────────────────┐
   │  (docker.sock)                                       ▼
   │                                          ┌──────────────────────────┐
   │ docker exec                              │ neo-claude  (uid 1000)   │
   ▼                                          │  claude -p … stream-json │
 neo-workbench                                │  cap_drop ALL, 2g, pids  │
   /workspace/claude ◄── volume neo-claude-work ──► /work   (rw)          │
   (Neo lê e escreve os arquivos da tarefa)   │  /home/claude ◄─ volume   │
                                              │   neo-claude-home (stream,│
                                              │   sessões; NÃO compartilha│
                                              └────────────┬─────────────┘
                                                           │ rede claude-net (internal)
                                                           ▼
                                              neo-claude-proxy (allowlist)
                                                api.anthropic.com:443 só
```

- **Imagem:** `FROM` a imagem do `neo-workbench` (mesmo python, git, build-essential
  e libs), com a CLI **numa versão fixa** e `DISABLE_AUTOUPDATER=1`. Usuário
  não-root, porque `bypassPermissions` é recusado como root.
- **Volumes:**
  - `neo-claude-work` aparece em `/work` no `neo-claude` e em `/workspace/claude` no
    workbench. O Claude **não enxerga o resto do `/workspace`**;
  - `neo-claude-home` guarda o stream e as sessões e não é montado em mais nenhum
    lugar. Ele existe para uma tarefa sobreviver ao recreate do container no meio
    de uma rodada (`--resume`). **Não guarda autenticação**: apagá-lo não desloga
    nada (§3.2.1).
- **Rede:** `claude-net` é `internal: true`, e a única saída é um proxy com
  **allowlist** (só CONNECT 443 para `api.anthropic.com`).
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` desliga a telemetria.
- **Permissões:** `bypassPermissions`. O container é a fronteira, e a doc só
  recomenda pular permissões "for sandboxes with no internet access", que é o caso
  com a allowlist.

| Ameaça | Defesa |
|---|---|
| Bash do Claude roda `env` | `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` (existe no 2.1.302) |
| Bash do Claude lê `/proc/<pid do claude>/environ` (mesmo uid) | Token por descritor (`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`, que existe no binário). **Medir na F0** se o filho herda o fd. Mesmo que herde, sobra a allowlist. |
| Token mandado para fora | Allowlist só para a Anthropic |
| Token escrito em `/work` e levado para fora pelo `neo_bash` | O core procura o token literal em `/work/<job>` e no stream **antes** de entregar. Se achar, apaga o arquivo e marca o job. |
| `ANTHROPIC_API_KEY` no ambiente toma a frente | O container não recebe `env_file`, e o `saude()` de cada job grava o `authMethod`. Se vier `apiKey`, aborta. |

## §6. O ciclo de uma tarefa (igual nos dois corpos)

Um job `kind="claude_code"`, com `spec_json.corpo = "local" | "servidor"` (e
`device_id` no local).

1. **Pedido.** O cérebro chama `claude_task(...)` (§8). Sai um job e a resposta volta
   na hora ("mandei pro Claude no seu Mac; te aviso").
2. **Saúde e preparo.** `corpo.saude()`. Se falhar, o job termina na hora, explicando
   o motivo: "o Claude Code do seu Mac não está logado", "o desktop está fechado" ou
   "o token do servidor venceu". Depois o orquestrador monta o prompt (objetivo,
   contexto, entregáveis, restrições e o contrato do relatório final). No servidor,
   ele também copia os arquivos para `/work/<job8>/`.
3. **Rodada.** `corpo.lancar(...)` com as flags comuns:
   ```
   -p  --output-format stream-json --verbose
   --session-id <uuid do job>          (1ª rodada)   |  --resume <session_id>  (seguintes)
   --max-turns N  --model <sonnet|opus>  --effort <nível>
   --json-schema '<relatório: status, resumo, arquivos, pergunta?>'
   --append-system-prompt '<contrato>'
   --strict-mcp-config
   ```
   Mais as flags de cada corpo:
   - servidor: `--permission-mode bypassPermissions`;
   - local: `--permission-mode acceptEdits --permission-prompts none`, mais as
     `--allowedTools` que o Daniel liberou nesta tarefa (a regra do comando exato,
     `Bash(npm install)`; escrita fora da pasta não se libera daqui).

   O `session_id` e a identificação da rodada ficam no `spec_json`.
4. **Acompanhamento.** A cada tick, `corpo.ler(rodada, offset)`. O `stream.py`
   transforma os eventos em notas: "Claude: editou `x.py`", "rodou `pytest`",
   "12 turnos". As notas vão para `progress_note`, para a janela Tarefas e para o
   caderno, com `touch_progress`. O texto cru do stream **não** entra no prompt do
   Neo. Os vigias param a rodada com `corpo.parar` quando estoura relógio, stream
   parado ou `--max-turns`.
5. **Fim da rodada.** O evento `result` traz:
   - o `structured_output`: `{status: concluido|precisa_de_voce|travou, resumo, arquivos[], pergunta?}`;
   - as `permission_denials`;
   - o custo e os turnos.
6. **Supervisor** (uma chamada de LLM do Neo, sem tools, com o objetivo, o relatório,
   as recusas e a lista de arquivos). Ele decide uma de quatro coisas:
   - **aceitar**: vai para o passo 7;
   - **continuar**: nova rodada com `--resume` (volta ao passo 3);
   - **perguntar ao Daniel**: o job vira `waiting` e a bolha no chat mostra a
     pergunta do Claude, ou as recusas ("o Claude quis rodar `git push` e
     `npm install`; libero?"). A resposta entra por `claude_reply` e vira a rodada
     seguinte, com as `--allowedTools` correspondentes, **só nesta tarefa**;
   - **desistir**: conta o que travou.

   Teto de **3 rodadas automáticas**. Passou disso, pergunta.
7. **Trabalhar em cima.** `corpo.recolher(...)` traz para o File Store o relatório e
   os entregáveis pequenos. O `then` (turno `unattended` na mesma sessão, que não
   alcança o disco do Mac) lê esse material, confere, ajusta e entrega por
   `file_save`, com o resumo no chat ou na voz. No corpo local, quando o resultado é
   mudança no projeto dele, o que sobe é o relatório e o `git diff --stat`. O código
   já está onde deve estar.
8. **Limpeza.** `corpo.limpar(...)`. O que subiu ao File Store para o `then`
   segue a mesma regra de não deixar cópia na VPS depois de entregue.

### Por que rodadas destacadas, e não um stream vivo pelo stdin

- **Salvar um `.py` reinicia o neo-core** ([[neo-dev-topology-prod-tunnel]]), e um
  `stdin` aberto morreria junto. No local é parecido: reiniciar o `tauri dev`
  derrubaria um pipe.
- Hoje o boot marca **todo job `running` como `failed`**
  (`reconcile_orphan_agent_jobs`). Para `claude_code`, o reconcile **reata**: chama
  `corpo.ler` a partir do último offset gravado.
  - processo vivo: segue lendo;
  - morto e com `result`: fecha a rodada;
  - morto e sem `result`: `--resume` com "continue de onde parou".
- Processo destacado + stream em arquivo + `session_id` no banco sobrevivem a
  reinício do core e do desktop por construção, nos dois corpos.

**Mensagem do Daniel no meio da tarefa** ("fala pro Claude usar Postgres"):
`claude_reply(job_id, texto)`.

- Com uma rodada rodando, o texto entra numa fila e vira a próxima rodada.
- Se for "para" ou "muda tudo", o orquestrador chama `corpo.parar` e manda
  `--resume` com o texto.
- Conversa ao vivo pelo stdin fica para a F4.

## §7. Qual corpo e quando chamar

**Quando chamar:**

- **Pedido explícito** ("com o Claude", "pede pro Claude"): chama sempre.
- **Tarefa difícil, sem pedido:**
  - até a F3: desligado (`NEO_CLAUDE_AUTO=false`);
  - na F3: liga, mas **pergunta antes**, porque é a cota dele.
- **Roteamento:** a tool só existe no conjunto cognitivo, fora do roteamento direto.

**Qual corpo** (campo `onde`; `auto` por padrão). A regra fica em código e não no
prompt, como o portão do navegador, que rebaixa e promove entre
`browser_local_task` e `browser_task`:

| Situação | Corpo |
|---|---|
| Ele disse "no meu Mac/computador", ou citou uma pasta ou projeto dele (`~/…`, "no neo-desktop") | **local** |
| A tarefa precisa dos arquivos, repositórios ou ferramentas dele | **local** |
| Nada disso, e o servidor está configurado | **servidor** (não depende do Mac ligado) |
| Pediu local e o local não está disponível (desktop offline, sem `claude`, deslogado) | pergunta: "seu Mac não está disponível; rodo no servidor?" Nunca troca calado: os arquivos e o risco são outros. |
| Servidor sem token configurado | só local |

## §8. Contrato das tools

```
claude_task(
  objetivo: str,            # o que é "pronto"
  contexto: str,            # o que o Neo sabe e o Claude precisa (passa pela redação do PKS)
  onde: "auto"|"local"|"servidor" = "auto",
  pasta: str | None,        # local: pasta do projeto no Mac; servidor: ignorado
  arquivos: list[str],      # servidor: file_id ou /workspace/... copiados para /work/<job8>/
  entregaveis: list[str],   # ex.: ["testes passando", "RELATORIO.md"]
  modelo: "sonnet"|"opus" = "sonnet",
  esforco: "medium"|"high" = "high",
  max_rodadas: int = 3,
  then: str                 # o que fazer com o resultado
) -> {job_id, corpo, status: "queued"}

claude_reply(job_id: str, mensagem: str, interromper: bool = False)
```

`job_status`, `job_logs` (as notas, nunca o stream cru) e `job_cancel` servem como
estão.

## §9. Portão de autoridade

- **Classe de risco, nos dois corpos:** `fonte_externa`, porque o Claude lê web e
  arquivos. Quando o resultado volta, o turno fica contaminado.
- **Corpo local** é atuação na máquina dele, mais forte que um `run_command`:
  - só com pedido dele (`principal=admin`);
  - **barrado** em turno contaminado (de qualquer fonte, inclusive web) e em turno
    `unattended`/agenda;
  - confirmação no Mac em toda tarefa (§4.3).

  É a regra mais dura do Neo, porque um Claude com rede, arquivos e logins dele é o
  melhor veículo de exfiltração que existe.
- **Corpo servidor:**
  - barrado em `unattended`/agenda e em turno contaminado por e-mail, PDF ou arquivo
    dele (a mesma regra do navegador);
  - liberado em turno contaminado só por web. Ali não há nada para vazar além da
    cota, e a saída é só para a Anthropic.
- **O que vai para a Anthropic:** o que o Neo escreve na tarefa e o que o Claude lê.
  O `contexto` passa pela redação de campos sensíveis do PKS, e `«pks:»` **não** é
  resolvido aqui.

## §10. Fases

**F0: medir antes de codar (sem tocar no core).**

No Mac, à mão, com `claude -p … --output-format stream-json`:

- (a) `--permission-mode auto` funciona em `-p`? E `--permission-prompts none` com
  `permission_denials`?
- (b) o formato real de `result`, `structured_output`, `rate_limit_event` e do erro
  de limite;
- (c) `--resume` depois de `kill -9` no meio de uma rodada;
- (d) quanto uma tarefa típica come da janela do Pro;
- (e) o processo sobrevive ao fechamento do app que o lançou (stdout em arquivo);
- (f) o Keychain entrega o login a um `claude` lançado pelo app;
- (g) gravar 3 ou 4 streams reais para os testes do orquestrador.

No servidor:

- (h) `neo-claude` + proxy de pé; `auth status` com o token mostra a assinatura;
- (i) com a allowlist só da Anthropic, a tarefa roda;
- (j) SCRUB e FD escondem o token do Bash do Claude;
- (k) `claude purge` apaga o `.jsonl`;
- (l) o que revoga o token, e se `auth status` confere o token de verdade (§3.2.1);
- (m) recriar o `neo-claude` no meio de uma rodada e confirmar que nada pede login
  e que `--resume` continua.

**F1: orquestrador + corpo local.**
Primeiro o local: não precisa de token, de container nem de decisão sobre termos,
e é o caso "ele já está logado". Entram:

- `core/claude_code/` completo: rodadas, supervisor, `waiting`, `claude_reply`,
  recusas → pergunta, reatar;
- `claude_cli.rs` com as 7 ações, o diálogo de confirmação e a allowlist de flags;
- a tool `claude_task` com `onde=local`.

Testes com o corpo falso e os streams da F0. E2E: "pede pro Claude, no meu Mac,
consertar o teste X do projeto Y". Exige reiniciar o `tauri dev`, nunca `tauri build`.

**F2: corpo servidor.**
`neo-claude`, o proxy, o token no banco desde o início (campo no painel, `.env`
como reserva), os avisos de vencimento e de recusa, a varredura do token, o
`authMethod` conferido e `onde=auto` com a tabela do §7. O orquestrador não muda:
se mudar, a interface do §2.1 estava errada.

**F3: gatilho implícito com pergunta.**
Orçamento por janela da cota.

**F4 (opcional).**
Stdin `stream-json` para conversa ao vivo. MCP **só leitura** do Neo para o Claude
(ex.: consultar o PKS). Cada uma pede a sua análise de segurança.

## §11. Decisões que são do Daniel

1. **Ordem:** começar pelo corpo local (recomendado: sem token e sem container) ou
   pelo servidor.
2. **Servidor: assinatura (`setup-token`) ou API key.** As duas sobrevivem a
   restart e rebuild, porque ficam no banco e não no container. O `auth login`
   dentro do container está descartado por isso (§3.2.1). Recomendo a assinatura,
   e trocar se o uso virar automático e frequente.
3. **Saída do `neo-claude`:** só Anthropic (recomendado) ou também PyPI/npm/GitHub.
4. **Local: confirmação no Mac em toda tarefa** (recomendado) ou só na primeira vez
   por pasta.
5. **Local: MCP/conectores** desligados por padrão (recomendado) ou ligados.
6. **Gatilho implícito:** perguntar sempre antes (recomendado) ou ir direto.
7. **Modelo padrão:** `sonnet` gasta menos cota; `opus` só quando ele pedir ou o
   supervisor escalar.

## §12. Gotchas conhecidos que esbarram nisto

- `.env` da RAIZ, não o `neo-core/.env`. Mudar `.env`/compose exige `docker compose
  up -d`; o `--reload` não relê ([[neo-dev-topology-prod-tunnel]]).
- O core não ganha dependência Python nova. A CLI mora no Mac e na imagem do
  `neo-claude`, e o core fala com ela por `docker exec` ou pelo canal de controle.
- `--bare` ignora o login e o token OAuth. `bypassPermissions` recusa root. Uma
  `ANTHROPIC_API_KEY` no ambiente ganha do login calada, nos dois corpos.
- `CLAUDECODE` e `CLAUDE_CODE_*` herdados de um terminal do Claude Code fazem o filho
  se achar sessão aninhada: o desktop limpa essas variáveis (§4.2).
- `capabilities` do device só é gravada no pareamento. A verdade do "dá para usar
  agora" é o `claude.health` a cada job, como o `browser.health`
  ([[neo-browser-local-plan]]).
- O Stop do chat mata os pids do `shell.rs`. A rodada do Claude não pode estar
  nessa lista (§4.2).
- Atualização da CLI: no servidor fica fixa na imagem; no Mac atualiza sozinha. Por
  isso o `stream.py` tolera evento desconhecido (ignora e registra) em vez de
  quebrar a tarefa.
- Toda leitura de resultado passa pelos tetos que já existem: relatório cortado em
  silêncio foi o defeito da síntese do `deep_read`
  ([[neo-deep-read-delegated-reading]]).

## §13. Registro de execução

### F0 local (07/11/2026, CLI 2.1.302, conta Pro)

Medido à mão no Mac, com rodadas de `haiku`/`sonnet` (custo estimado < US$ 0,10).
Os streams, com caminhos trocados e sem eventos de hook, viraram fixtures em
`tests/claude_code/fixtures/`.

| Item | Resultado |
|---|---|
| (a) `auto` em `-p` | Funciona, mas **escreveu fora da pasta** sem perguntar. Trocado por `acceptEdits` (§4.3). |
| (a) `acceptEdits` + `--permission-prompts none` | Negou o `python3` e a escrita em `/tmp`; as duas em `permission_denials` com a entrada exata; relatório `precisa_de_voce`. |
| Recusa na hora | Evento `system/permission_denied` no meio do stream. **Ele traz só o `tool_use_id`**: a entrada (comando, caminho) vem do `tool_use` anterior, e o `stream.py` cruza os dois. |
| `--allowedTools "Bash(sleep:*)"` | Liberou `sleep 6` e negou `echo … && sleep …`: a regra casa o comando inteiro. |
| (b) `result` | `structured_output` (com `--json-schema`), `permission_denials`, `num_turns`, `total_cost_usd`, `terminal_reason`, `api_error_status`. |
| (b) `rate_limit_event` | `rate_limit_info.unifiedWindows.five_hour.utilization` (0,14 → 0,17 no teste) e `seven_day` (0,36), com `resetsAt`. É o "volta às HH:MM". O formato do erro de limite estourado não foi visto; o código trata `api_error_status == 429` ou `status` fora de `allowed`. |
| `init` | `apiKeySource: "none"` = assinatura. `--strict-mcp-config` zera `mcp_servers` (sem ele vinham os conectores Gmail/Calendar/Docs do claude.ai). |
| (c) `kill -9` + `--resume` | Mesma sessão; ele conferiu a pasta e criou só o que faltava (`p4`–`p6`). |
| (e) pai sai | O processo destacado com saída em arquivo seguiu sozinho. |
| `claude purge` | Apaga o estado do **projeto inteiro** (a pasta), não de uma sessão. No Mac **não pode** rodar: levaria o histórico do Claude dele naquele projeto. No servidor serve, porque cada job tem a sua pasta. |
| Variáveis herdadas (`CLAUDECODE`…) | Não quebraram nada visível (o `messaging_socket_path` do filho era o dele próprio). O desktop limpa mesmo assim, como higiene. |
| (f) Keychain pelo app | O teste `rodada_real_lanca_le_e_colhe` (Rust, `--ignored`) rodou pelo caminho do desktop com o ambiente limpo e saiu `apiKeySource: none`. Falta ver lançado pelo app aberto fora do terminal. |

Não medidos: (d) quanto uma tarefa típica come da janela (o job grava a
`utilization` de cada rodada, então mede-se em produção); erro de limite real.

### F1 (07/11/2026) — implementada, sem E2E

- **Core:** `core/claude_code/` (`stream.py`, `corpo.py`, `corpo_local.py`,
  `orquestrador.py`) e `core/jobs/runner_claude.py`.
  - tools `claude_task`/`claude_reply` (`core/jobs/tools.py`, executor);
  - `REATTACHABLE_JOB_KINDS`: no boot, o job `claude_code` volta para a fila e
    reata, em vez de virar `failed`;
  - portão `NEGAR` em turno contaminado, da agenda ou de fora;
  - viés do roteador `_bias_claude_code` (citou o Claude → cérebro, para não
    virar `run_command`);
  - flag `CLAUDE_CODE_ENABLED`, que também tira as tools do registry e do
    conjunto cognitivo.
- **Continuação:** o `claude_reply` sobre tarefa `waiting` cria um job novo com a
  mesma sessão (o padrão da retomada do navegador). Sobre tarefa rodando, a
  mensagem entra em `extra_metadata["claude_mensagens"]`; com `interromper`, a
  rodada para.
- **Desktop:** `services/claude_cli.rs`, com as 7 ações. A confirmação fica em
  `claude-runs/confirmadas.json` por tarefa (as continuações não perguntam de
  novo). Uma confirmação que chega depois de 170 s é recusada, porque o core
  desiste aos 180 s.
- **Testes:** 40 no orquestrador e no stream contra as fixtures, mais tools,
  portão e roteador; 4 no Rust, e 1 `--ignored` com rodada real.

**Ligado em 07/11/2026:** `CLAUDE_CODE_ENABLED=true` no `.env` da RAIZ +
`docker compose up -d --no-deps neo-core`. Resultado: `healthy` e "Claude Code
runner wired (modelo=sonnet)". O `tauri dev` recompilou e reiniciou o desktop
sozinho quando o Rust mudou: o executável em uso já tinha o `claude_cli`.

**E2E pendente:** "pede pro Claude, no meu Mac, …" pelo chat do desktop.

### Visibilidade (07/11/2026)

Pedido do Daniel: ver o que o Claude está fazendo pela interface, sem poluir o
chat e sem gerar saída grande no Neo.

- **Diário** (`Diario` em `core/jobs/runner_claude.py`): uma linha por passo, com
  hora, em `logs/claude_code/<job>/trace.md`.
  - inclui o "💬" (a primeira linha do que o Claude diz que vai fazer), o
    início e o fim de cada rodada (com a cota) e as recusas;
  - teto de 256 KB por tarefa; só as 20 tarefas mais recentes ficam em disco;
  - o `job.log_path` aponta para cá, então a rota de log e a `job_logs` leem o FIM
    sem mudança nenhuma;
  - não guarda conteúdo de arquivo nem saída de comando.
- **Faixa no chat** (`ClaudeBar.tsx`): uma linha acima do campo de mensagem, só
  enquanto há tarefa do Claude na fila, rodando ou esperando o Daniel. Mostra o
  último passo e o tempo; fica âmbar quando espera resposta. Não vira mensagem: a
  conversa segue com o pedido e o resultado. Consulta a lista de jobs a cada 4 s
  com tarefa viva e a cada 30 s sem. É da conversa onde a tarefa nasceu
  (`job.session_id` = sessão aberta, espelhada no `ChatPage` nos três lugares que
  chamam `resetAgent`): "Novo chat" a esconde, voltar à conversa a mostra — é
  só lá que o aviso do `claude_reply` existe.
- **Faixa expansível**: clicar na linha abre uma caixa logo abaixo dela com o fim
  do diário (o mesmo `getJobLogs` da janela Tarefas, ~6 KB). O diário só é buscado
  com a caixa aberta, e aberta ou fechada fica guardada em
  `localStorage["neo.chat.claudeBox"]`.
- **Janela Tarefas abre na tarefa**: o botão "ver ›" da faixa chama
  `open_jobs_window(job_id)`. Com a janela fechada, a tarefa vai na URL; com ela
  aberta, num evento `jobs-focus`. O log dessa tarefa aparece como "Atividade do
  Claude".
- **Botão no balão** (`ClaudeNoBalao.tsx`): a resposta que pediu a tarefa e a que
  entrega o resultado ganham uma pílula "Claude · ver o processo". Ela abre ali a
  mesma linha + diário da faixa (`ClaudeProcesso.tsx`, compartilhado), acompanha
  a cada 4 s se a tarefa ainda está viva e não consulta nada enquanto fechada. É
  o jeito de achar o processo depois que a faixa some. O vínculo vem do core,
  por três caminhos com o mesmo resultado (`core/claude_code/balao.py`):
  - ao vivo: `AgentResponse.jobs` (campo 7 do proto, `JobRef{id, kind}`), tirado
    do `tool_trace` da resposta (`claude_task`, e `claude_reply` que guarda o id
    da continuação no trace);
  - histórico: `/desktop/session/{id}/history` devolve `jobs` a partir do
    `tool_trace` salvo, então respostas de antes do campo também ganham o botão;
    e a entrega gravada pelo dispatcher (`source=agent_job`, `kind=claude_code`);
  - entrega ao vivo: a notificação `chat_message` já trazia `job_id` e
    `kind_detail`.
  Gotcha: o Rust usa o código gerado guardado em `src-tauri/src/grpc_gen/`, não o
  do `OUT_DIR`. Mudou o proto, copie o `neo.desktop.rs` do build para lá. O
  Python usa o `neo_desktop_pb2.py` da raiz do neo-core, gerado pelo
  `grpc_tools` do `.venv` (gencode 6.31.1, roda no protobuf 7.35 do container).

**1º teste real da faixa (job `decc8224`, 07/11):** três defeitos, todos corrigidos.

- **A faixa parecia aparecer só DEPOIS do trabalho.** A rodada inteira levou 13 s
  (6 s até lançar, 7 s de trabalho), e a faixa, sem tarefa visível, só consultava
  a cada 30 s: achou a tarefa já esperando o Daniel. Agora ela consulta na hora
  em que cada turno começa e termina (prop `ocupado`) e fica a 4 s por 2 min
  depois. **Esse conserto não pegou na 1ª versão** (job `0a33cb31`, 25 s,
  faixa aos 18 s): o `ocupado` vinha do `isLoading`, que nada liga no desktop;
  o turno é o `isExecuting`. O log de acesso do core mostrou as consultas em
  22:05:43 e 22:06:13, 30 s cravados atravessando o turno. Medir pelo
  `GET /desktop/jobs` no `docker logs -t` antes de supor o ritmo.
- **"rodou `python3 hello.py`" num comando NEGADO.** A nota nasce do `tool_use`, que
  é o pedido, antes do resultado. As notas foram para o gerúndio ("rodando",
  "escrevendo"). Além disso, o banco só grava uma nota a cada 3 s, e a recusa no
  mesmo segundo se perdia: agora a nota pulada fica pendente e é gravada na volta
  seguinte.
- **O "Fim:" do diário quebrou em várias linhas** (o resumo tem parágrafos): o
  `Diario` junta tudo numa linha.

**A resposta do Daniel não chegava ao Claude (job `decc8224`, 07/11).** A pausa
chegou ao chat às 19:54, ele respondeu "pode rodar" às 20:00, e o **roteador
classificou como `run_command`**. O Neo rodou `cd ~/Neo/claude/decc8224 && python3
hello.py` no terminal dele, respondeu "oi", e a tarefa ficou em `waiting` para
sempre. O `_bias_claude_code` só pega frase que cita "Claude".

Corrigido:

- **Desvio antes do roteador** (`[INTENT-OVERRIDE]` no `ai_service`, ao lado do de
  esclarecimento pendente): com uma tarefa `claude_code` em `waiting` NESTA
  sessão, há menos de 12 h, a mensagem vai ao cérebro.
- **Aviso ao cérebro** (`_nota_claude_esperando`, no fim do prompt): job_id,
  pergunta, ações negadas e a regra "chame `claude_reply`, liberar=true se ele
  autorizou; NÃO execute você mesmo". Assunto diferente é atendido normalmente.
- **Modo `auto` como padrão no Mac** (`CLAUDE_CODE_PERMISSION_MODE`, opção
  `acceptEdits`). É a escolha do Daniel: com `acceptEdits`, até rodar o script da
  própria pasta virava pergunta. O desktop aceita `auto` e continua recusando
  `bypassPermissions`. O diálogo deixou de prometer "só nesta pasta", porque o
  `auto` julga risco, não lugar.
- **Não adotado:** o Neo decidir sozinho o que liberar. Seria o modelo barato do
  Neo julgando segurança de comando, e é o texto que pode chegar contaminado. O
  `auto` é o classificador da Anthropic, feito para isso.

### Auditoria: o que o Claude disse × o que chegou (job `8f83b37c`, 07/11)

Comparada a transcrição do próprio Claude Code no Mac
(`~/.claude/projects/-Users-daniel-Neo-claude-8f83b37c/<sessão>.jsonl`) com o
diário e com o chat.

**Completo:** o único texto que ele escreveu durante o trabalho; o
`result_summary` no banco (744 caracteres, sem corte); os dois arquivos
recolhidos. O raciocínio (4 blocos) já vem VAZIO na transcrição dele: o Claude
Code não guarda o texto do thinking, então não há o que mostrar.

**Cortado no diário:**
- "Fim:" em 400 caracteres, bem em cima de "Não testei o modo de dois jogadores
  nem o reinício";
- os 4 comandos em 100 caracteres; o 2º terminava em `> RELATORIO.md; rm
  saida.txt`, uma exclusão que o diário não mostrou;
- a saída dos comandos nunca entrava (a partida de teste, 1.185 caracteres).

**Perdido no chat (o pior):** com `then`, a entrega do próprio job era
silenciada e só a continuação falava. O turno de continuação (modelo barato, sem
ninguém olhando) gastou 8 chamadas com `file_save` em caminhos que não existiam,
chamou `claude_task` de novo (o portão barrou, certo) e respondeu "Arquivos
salvos e disponíveis para download." — 44 caracteres. Os itens íntegros (lista
de arquivos, `claude --resume`) caíram junto com o silêncio, e a regra "NÃO cole
o conteúdo inteiro" contradizia o combinado "cole a saída da partida".

Corrigido:
- **Entrega literal**: `entrega["literal"]` no runner; o dispatcher entrega o
  fecho como está, sem redator (`_fecho_literal`). No `completed` abre com "O
  Claude terminou. O relatório dele:"; pausa e falhas já vinham escritas pelo
  orquestrador.
- **`then` não cala o Claude** (`_settle`, como o `browser_local`); a
  continuação ouve que relatório, arquivos e comando de retomada JÁ foram
  entregues e faz só o combinado. A regra genérica virou "não cole o ARQUIVO
  inteiro; se o combinado pede para mostrar um trecho, cole o trecho".
- O resumo da continuação diz que os arquivos recolhidos estão no catálogo, não
  no `/workspace` (`file_stage` primeiro).
- **Diário inteiro**: texto do Claude inteiro numa linha; comandos inteiros até
  4.000 caracteres, cortados no MEIO acima disso (o fim aparece); saída de cada
  comando como bloco `↳ saída (n linhas):` com as linhas recuadas por `│`
  (60 linhas / 4.000 caracteres, começo e fim); erro de outra ferramenta como
  `↳ erro em X`. Resultado de comando negado não repete a recusa. A saída é só
  do diário: o runner não a põe no `progress_note`, que a faixa mostra (cortado
  em 300).

**Botão "terminal"** (faixa e balão): abre o Terminal do Mac em `cd <pasta> &&
claude --resume <sessão>`. O core manda `claude: {session_id, cwd, rodando}` em
cada job `claude_code` com sessão começada (`_claude_retomar`); o botão espera
enquanto a tarefa está com o Neo (`rodando`), porque duas cópias na mesma sessão
se atropelam. No Mac (`claude_cli::abrir_no_terminal`): a sessão tem que ser um
UUID que existe em `~/.claude/projects` desta máquina e a pasta passa pelo
`conferir_pasta`; o comando vai entre aspas de shell num `.command` aberto com
`open -a Terminal`, que se apaga ao rodar e deixa um shell na pasta quando o
Claude sai. Não é AppleScript porque o `do script` pede permissão de Automação, e
no `tauri dev` cada build é outra assinatura.

### Ajuste numa tarefa terminada (07/11)

Antes, o `claude_reply` só valia com a tarefa rodando ou parada esperando. Com
ela terminada, recusava ("peça uma tarefa nova"), e a tarefa nova nascia em
**sessão nova** (o Claude sem lembrar do que fez) e **pasta nova** (sem os
arquivos de antes).

Agora:
- **`claude_reply` aceita tarefa terminada** (concluída, falhou, passou do
  tempo, cancelada) e retoma a MESMA sessão, na mesma pasta, com o mesmo
  `tarefa_id` (o Mac não pergunta de novo). Título "(ajuste)"; o `then` antigo
  NÃO vai junto (pedido novo); a terminada fica com o status dela.
- Recusa quando a tarefa nem começou (recusada no Mac: não há sessão) e quando
  outra tarefa do Claude está rodando (manda para ela).
- **Ponta da cadeia**: resposta a uma tarefa que já seguiu num ajuste ou
  continuação vai para a mais nova (`continuacao_de`), sem abrir dois ramos da
  mesma sessão.
- **Aviso ao cérebro** (`_nota_claude_recente`): só quando a mensagem CITA o
  Claude, aponta a tarefa mais nova da conversa (até 7 dias) e manda
  `claude_reply` para ajuste e `claude_task` só para trabalho novo. Ajuste sem
  citar o Claude o Neo resolve sozinho — escolha do Daniel: não gasta a cota do
  Claude sem ele pedir.
- **Sessão aberta no Terminal**: o `claude.start` com `--resume` confere no `ps`
  se há um Claude INTERATIVO (sem `-p`) com aquela sessão; se houver, não lança
  e a falha diz "feche o Claude lá e me peça de novo".

### Chave no neo-panel (07/11)

**Configurações → Claude Code** liga e desliga o Claude Code integrado, sem
reiniciar (`api/routes/claude_code_config.py`, GET/PUT `/api/v1/claude-code-config`,
JWT do painel). A chave mora em `core/claude_code/chave.py`:
- grava em `user_preference` global (service="neo", category="claude-code",
  key="enabled") e atualiza o valor em memória — o core é um processo só
  (uvicorn com o gRPC dentro), então vale no turno seguinte;
- `CLAUDE_CODE_ENABLED` do `.env` virou só o PADRÃO, enquanto o painel nunca
  gravou (a aba mostra quem decide);
- o runner e as tools são montados SEMPRE (com `JOBS_ENABLED`); quem confere a
  chave é quem lista (classificador `_format_tools`, agente do SERVER_TOOL
  `_server_tool_toolset`, cérebro `_claude_code_available`, viés do roteador) e
  quem executa (`_claude_ligado`, com erro que aponta o painel);
- desligado: nada novo vai ao Claude (tarefa, resposta, ajuste); o que já roda
  termina sozinho. Sem runner (jobs desligados), o PUT recusa ligar (409).

### Duas tarefas que morreram antes de começar (sessão 5efdf38d, 08/11)

- **`40852ae7` (02:31): ninguém permitiu.** O `claude.start` chegou (a pasta
  `~/Neo/claude/40852ae7` nasceu às 23:31:15), ficou no diálogo de confirmação e o
  core desistiu aos 180 s com "o Mac não respondeu". O diálogo não tem janela-mãe
  e o app não vinha para a frente: no macOS, alerta de app em segundo plano abre
  atrás da janela em uso. Não deu para provar — o log do desktop era UM arquivo
  de 40 KB (padrão do `tauri-plugin-log`) e já tinha descartado esse trecho.
- **`21b91d36` (13:10): pasta inventada.** "tenta de novo pq deu erro" não cita o
  Claude, foi SERVER_TOOL direto, e o classificador pôs `pasta=/Users/daniel/
  Projetos/revolucao-industrial-claude` (nem `~/Projetos` existe). Morreu no Mac
  em 1 s, um turno depois. O "Dessa vez foi" do Neo veio antes de a tarefa
  começar.

Corrigido:
- antes do diálogo, a janela do Neo vem para a frente (`set_focus` ativa o app)
  e sai uma notificação "O Claude quer começar";
- o `claude.start` sem resposta diz "ninguém permitiu no Mac em 3 minutos… pode
  ter aberto atrás de outra janela";
- `claude.health` aceita `pasta` e responde se ela serve (mesmas regras do start,
  sem criar nada); o `claude_task` confere NO MESMO TURNO e devolve ao modelo
  "chame de novo SEM `pasta`". Desktop antigo não responde a pasta e segue como
  antes. A descrição de `pasta` diz "que JÁ EXISTE, nunca invente";
- o aviso do `claude_task` diz "na fila, ainda NÃO começou";
- rodada que não lançou não conta ("nenhuma rodada chegou a rodar");
- log do desktop: 5 MB × 5 arquivos (`KeepSome(5)`).
