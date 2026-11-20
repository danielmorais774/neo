# Neo × Zion: o Zion na VPS, e o Neo lendo dele (fase 1) — plano

**Data:** 2026-11-20. Revisto no mesmo dia, depois da F1: o Zion inteiro vai para a VPS (§2).
**Status:** F0 e F1 feitas (§12). A réplica `zion-replica` da F0 era o desenho anterior e sai
na migração (§4). F2–F6 não começaram.
**Fase 1 de 2.** A fase 2 (chamado de suporte → o Neo acha o projeto → chama o Claude
na pasta dele → plano → aprovação → PR) é outro plano, escrito depois desta fase
funcionar. Contexto da fase 2: `docs/demo-suporte-ponta-a-ponta.md`.
**Depende de:** o Zion (`~/Documents/projects/zion-memory-mcp`, design em
`docs/design.md` de lá), o WireGuard Mac↔VPS (`docs/wireguard-mac-vps.md`) e o portão de
autoridade (`core/authority/`).

---

## 0. O pedido

O Daniel quer que o Neo consulte o Zion: quais projetos existem, em que pasta do Mac
cada um mora, e o que os agentes já sabem de cada um (regras, decisões, falhas
abertas). Isso é o alicerce da fase 2: o Neo só consegue chamar o Claude "na pasta do
projeto" se souber onde ela fica. Ele pediu que a ligação seja **pela própria VPS**,
em fases separadas, com esta integração primeiro.

**Revisão de 20/11, depois da F1:** o Daniel decidiu levar o Zion **inteiro** para a VPS.
Passa a existir um Zion só: o Claude Code e o Codex do Mac falam com ele pelo
WireGuard, e o neo-core lê dele lá mesmo.

## 1. Onde as coisas estão hoje (medido em 20/11)

- **O Zion roda no Mac, um processo por sessão.** O Claude Code e o Codex sobem cada um o
  seu `zion serve` por **stdio** (`claude mcp get zion` e `~/.codex/config.toml`: comando
  `~/.local/bin/zion serve`). Esses processos leem e gravam direto em `~/.zion` (repo git),
  com um lock entre processos. O launchd só roda o `zion tick` a cada 5 min (pull do
  spool do Neo e jardineiro). O `zion serve --http` que escuta na 7077 do Mac é um
  `npm run dev` esquecido, não faz parte da instalação. Na VPS só chega o código, pela
  sessão `contabo-projects` do Mutagen.
- **O domain já é o cadastro de projeto.** O `domain.md` guarda
  `repos: [{name, path, remote}]`, e o `domain_resolve(cwd, git_remote)` acha o domain
  pela pasta ou pelo remote. Hoje existem 4: `global`, `homelab`, `neo` e `zion`.
- **Os hooks do Claude Code rodam no Mac.** `zion hook session-start|pre-compact|stop|session-end`
  lê o transcript local (só para ver quais arquivos foram editados), acha o domain pelo
  `cwd` e grava checkpoint mecânico.
- **O servidor lê o disco e o git de quem chama.** Isso acontece em:
  - `domain_resolve` e `domain_create`: `realpath` e `git remote`;
  - `session_start` e `session_checkpoint`: o estado do git no `cwd`;
  - `session_resume`: o drift do git e, no modo review, os arquivos mudados entre dois
    commits;
  - `plan_save`: lê o `.md` pelo caminho e guarda o sha.

  Hoje funciona porque servidor e cliente estão no mesmo Mac. Com o servidor na VPS, os
  caminhos do Mac não existem lá: o Mutagen leva os projetos para `/home/daniel/...`, e
  sem `.git`.
- **A F1 resolveu o lado da leitura** (§12): `serve --read-only`, `ZION_HOST`,
  `ZION_ALLOWED_HOSTS`, `domain_list` e o `Dockerfile`.
- **O proxy do sandbox barra os IPs internos.** A lista negra do tinyproxy cobre `10.x`,
  `127.x`, `172.16–31.x` e o IP público da VPS. Medido: do `neo-workbench`,
  `10.66.66.1:8000` dá 403. A limitação conhecida é um nome público que resolva para um
  IP interno (DNS rebinding).
- **Lado do Neo:** o `httpx` já está no `pyproject.toml`; não há cliente MCP nem tool do
  Zion. A única ponte Neo → Zion é o **spool de falhas** (§11 do design do Zion), que
  está pronta numa cópia de 18/11 (sessão `f8a807a4`, `scratchpad/dev/neo-core`) e **não
  foi publicada**. Essa cópia já cria a seção `[zion]` no `neo.toml` e a classe `Zion` no
  `shared/ajustes.py`.

## 2. Decisão (20/11): o Zion mora na VPS

A fonte da verdade passa a ser `/home/daniel/.zion` na VPS. **Dois processos** leem a
mesma pasta:

| Processo | Quem usa | Onde escuta | Grava |
|---|---|---|---|
| `zion` | Claude Code e Codex do Mac (MCP por HTTP, direto, e os hooks), e o `zion tick` | `10.66.66.1:7077`, só pelo WireGuard, com token | sim |
| `neo-zion` | neo-core | rede `zion-net` (internal), sem porta publicada | **não** (`--read-only`, pasta montada `:ro`) |

**Por que dois processos e não um só com permissão por token:**
1. O `neo-zion` é a F1 pronta, sem código novo.
2. **O Neo continua sem caminho de escrita, por construção.** Ele não alcança o `zion`
   (outra rede) e não tem o token. O Zion entra no começo de toda sessão do Claude Code;
   se o Neo gravasse num turno que leu um e-mail, uma injeção viraria instrução
   permanente para todos os agentes.

**O que muda em relação ao desenho da réplica:**
- A VPS passa a ser a fonte, e o Mac fica com uma cópia de backup (Mutagen VPS → Mac).
- `sessions/` e `homelab` passam a morar na VPS. Isso é consequência da decisão dele e
  tira as perguntas 1 e 2 do §10 anterior. Os checkpoints são o resumo do trabalho, não a
  conversa. O transcript continua fora do Zion (design §12 de lá).

**Custos:**
- **Mac sem WireGuard** (sem internet, VPS fora do ar): o Claude Code trabalha sem Zion.
  Os hooks desistem em até 2 s para não atrasar a sessão. Há uma opção de cair para uma
  leitura local do backup (§10).
- **Latência:** ~170 ms de ida e volta por chamada (ping do WireGuard, Brasil–Europa). Um
  hook faz de 1 a 4 chamadas.
- **Trabalho no Zion:** o cliente remoto (§5.2) é a maior parte desta revisão.

## 3. Arquitetura

```
 Mac                                                VPS (contabo-neo)
 Claude Code / Codex                                /home/daniel/.zion   (fonte, repo git)
   ├─ MCP por HTTP, direto ───────────────┐               │ rw                 │ :ro
   │   (Authorization: Bearer)            │               ▼                    ▼
   │                                      ├─ WireGuard ─► zion               neo-zion
   └─ zion hook <evento> ─────────────────┘   10.66.66.1:7077               serve --http --read-only
       lê o transcript e o git no Mac;         serve --http, token          rede zion-net (internal)
       manda só arquivos+hora e o git          só entra pela wg0                  ▲
                                               lê os .md dos projetos             │
 ~/.zion-backup ◄── Mutagen one-way, VPS → Mac   pela cópia do Mutagen         neo-core
                    (com .git)                 zion-tick (5 min, spool do neo-core)
```

- **`zion` (com escrita):**
  - publicado só em `10.66.66.1:7077`, no `docker-compose.override.yml`, como o neo-core
    e o postgres;
  - uma regra no `DOCKER-USER` faz a porta 7077 aceitar só o que entra pela `wg0`. Isso
    barra as redes do Docker (sandbox, navegador) e a `eth0`;
  - token Bearer obrigatório;
  - `10.66.66.1:7077` entra no `allowedHosts`, com a proteção contra DNS rebinding
    ligada;
  - a imagem com escrita precisa de `git`, que o Zion usa para o histórico (o `alpine`
    não traz).
- **`neo-zion` (só leitura):** a F1, montando `/home/daniel/.zion:/data:ro`.
- **`zion-tick`:** a mesma imagem do `zion`, em laço de 5 min. Lê o spool do Neo direto
  da pasta do neo-core na VPS (montada `:ro`), sem passar pelo Mutagen. O servidor e o
  tick dividem a pasta pelo lock entre processos que o Zion já tem.
- Os três containers rodam como uid 1000, o dono dos arquivos.

## 4. Migração dos dados

**O cuidado:** cada sessão aberta do Claude Code e do Codex tem o seu `zion serve` por
stdio gravando em `~/.zion`. Uma sessão que continuar aberta depois da troca segue
gravando no Mac, e o que ela gravar se perde.

1. **Parar os escritores do Mac:**
   - fechar todas as sessões do Claude Code e do Codex, menos a que faz a migração;
   - desligar o `zion tick`: `launchctl bootout gui/$UID/dev.zion.memory.tick`;
   - matar o `npm run dev` da 7077.
2. **Tirar a réplica:** encerrar o Mutagen `zion-replica` e apagar `~/.zion-replica` na
   VPS.
3. **Copiar:** `rsync -a` do `~/.zion` inteiro (com `.git`, sem `.zion/`) para
   `/home/daniel/.zion` na VPS. Conferir o mesmo `git rev-parse HEAD` e a mesma contagem
   de arquivos.
4. **Subir na VPS:** `zion`, `zion-tick` e `neo-zion`. Conferir o `/health` e uma busca
   pelos dois lados.
5. **Trocar o Mac:**
   - `zion install claude-code --remote …`: o MCP vira um servidor HTTP
     (`claude mcp add --transport http … --header "Authorization: Bearer …"`). O Codex
     fica para quando o binário dele voltar a funcionar no Mac (hoje dá `ENOENT`); se a
     versão não aceitar HTTP, usa a ponte `mcp-remote` do npm;
   - os hooks passam a ler a URL e o caminho do token de `~/.config/zion/client.toml`
     (modo 0600).
6. **Guardar o antigo e ligar o backup:**
   - `~/.zion` vira `~/.zion.antes-da-vps-20261120`, sem apagar nada;
   - criar o Mutagen `zion-backup` (VPS → Mac, `one-way-replica`, com `.git`) em
     `~/.zion-backup`.
7. **Conferir com uma sessão nova do Claude Code:** o contexto do Zion tem de chegar no
   `SessionStart`, e um checkpoint tem de aparecer na VPS.

- A sessão que faz a migração **não grava mais no Zion depois do passo 5**: o
  `zion serve` dela ainda é o local.
- Se `~/.zion` reaparecer no Mac depois da troca, alguma sessão ficou aberta e gravou
  nele.
- **Desfazer:** voltar o `claude mcp` e o Codex para `zion serve` (stdio local), copiar
  `/home/daniel/.zion` de volta para `~/.zion` e religar o `zion tick`.

## 5. Mudanças no Zion (repo `zion-memory-mcp`)

### 5.1 ✅ F1: modo só leitura

`serve --read-only` (ou `ZION_READ_ONLY=1`) com só as 6 tools de leitura, `init()` sem
gravar e o `Store` recusando escrita; `ZION_HOST`; `ZION_ALLOWED_HOSTS`; `domain_list`;
`Dockerfile`. Commit `6e0a9d8`, detalhes no §12.

### 5.2 F2: Zion remoto

1. **Token no HTTP.**
   - `ZION_TOKEN_FILE` (ou `[server] token_file`). Com token configurado, `/mcp` e
     `/hooks` exigem `Authorization: Bearer <token>`, comparado em tempo constante.
   - Sem token, fica o comportamento de hoje, que só serve em loopback.
   - Com token, o `/health` sem autenticação responde só `{"name":"zion","ok":true}`.
2. **O Claude fala direto, sem proxy** (decisão do Daniel em 20/11). O que o servidor
   lia do disco de quem chama passa a vir de outro jeito:
   - **git (branch, commit, arquivos sujos, remote):** os hooks rodam no Mac a cada
     início e fim de turno e mandam o git da pasta. O servidor guarda **o último git visto
     de cada pasta**, em memória, e usa quando não enxerga o caminho. Isso vale em
     `domain_resolve` (casar pelo remote), `domain_create`, `session_start`,
     `session_checkpoint`, os checkpoints mecânicos e o drift do `session_resume`. Perda
     aceita: o git de um checkpoint é o do último hook (fim do turno anterior), não o do
     segundo exato.
   - **o `.md` do `plan_save`, e o aviso "o plano mudou":** o Mutagen já copia
     `~/Documents/projects` para a VPS. O servidor troca o começo do caminho
     (`ZION_PATH_MAP=/Users/daniel/Documents/projects=/projects`) e lê a cópia, montada
     sem escrita. Só lê `.md` por esse caminho. Como o Mutagen leva uns 2 s, um `plan_save`
     logo depois de editar pode pegar a versão anterior; o aviso de "mudou" mostra isso, e
     um novo `plan_save` corrige.
   - **arquivos mudados no modo review:** o servidor devolve o intervalo de commits e uma
     dica para o agente rodar `git diff --name-only` ele mesmo.
   - **remote que falta no domain:** o hook de início de sessão preenche, quando o domain
     foi achado pela pasta e o repo dele está sem remote.

   A regra fica no código: se o caminho existe no servidor, ele mesmo calcula (é o modo
   local de hoje); se não existe, usa o que veio do Mac; sem nenhum dos dois, `null`.
3. *(sem proxy: item retirado em 20/11)*
4. **Hooks remotos.** Com o `client.toml` presente, `zion hook <evento>` não abre pasta de
   dados nenhuma:
   - lê o transcript e o git no Mac;
   - manda um resumo (o payload do hook sem o caminho do transcript, mais
     `zion_edits: {files: [{path, last_at, untimed}], last_edit_at}` e `zion_git`) para
     `POST /hooks/<evento>` no servidor, que roda a mesma lógica de hoje sobre esse resumo;
   - o transcript nunca sai do Mac, só os arquivos editados e a hora da última edição de
     cada um;
   - timeout de 2 s. Se falhar, loga em `~/.config/zion/hook-errors.log` e não bloqueia o
     Claude. No início da sessão, avisa o modelo que o Zion está fora do ar, para ele não
     gastar chamadas.
5. **`zion install claude-code --remote <url>`**:
   - grava o `~/.config/zion/client.toml` (URL e caminho do token, modo 0600);
   - registra o MCP por HTTP com o header do token;
   - mantém os hooks (o mesmo comando, que vira remoto ao achar o `client.toml`).

   `zion install claude-code` sem `--remote` volta ao stdio local e apaga o `client.toml`.
   O `zion install launchd` deixa de ser usado no Mac, porque o tick vai para a VPS.
6. **Tick em laço:** `zion tick --loop 300`. O `spool_path` do `[neo_pull]` aponta para o
   spool montado.
7. **`git` na imagem com escrita:** um estágio do `Dockerfile` com `apk add git` (a imagem
   do `neo-zion` continua sem).
8. **Testes:**
   - token: sem, errado e certo;
   - o git que veio do Mac é usado quando o caminho não existe e ignorado quando existe;
   - o resumo do transcript dá o mesmo resultado que a leitura local;
   - o hook remoto produz o mesmo checkpoint que o local, com o mesmo transcript de
     exemplo, e desiste rápido com o servidor fora do ar;
   - `plan_save` pela tradução de caminho, e recusa de arquivo que não é `.md`.

## 6. Infra na VPS

- **`docker-compose.yml` do neo:**
  - **`zion`**: build de `../zion-memory-mcp` (estágio com `git`);
    - volume `/home/daniel/.zion:/data` (com escrita);
    - token em `/home/daniel/.config/zion/token` (0600), montado só para leitura;
    - `/home/daniel/Documents/projects:/projects:ro`, a cópia do Mutagen, para ler os
      `.md` dos planos;
    - `ZION_TOKEN_FILE`, `ZION_ALLOWED_HOSTS=10.66.66.1:7077` e
      `ZION_PATH_MAP=/Users/daniel/Documents/projects=/projects`;
    - `user: "1000:1000"`, `read_only`, `tmpfs /tmp`, `cap_drop: ALL`,
      `no-new-privileges`, 256 MB, `restart: unless-stopped`;
    - rede própria `zion-pub`, que não é a `default` nem a `zion-net`.
  - **`zion-tick`**: a mesma imagem, `command: tick --loop 300`, a mesma pasta com
    escrita e `./neo-core/.zion-spool:/neo-spool:ro`.
  - **`neo-zion`**: como na F1, com `/home/daniel/.zion:/data:ro`,
    `ZION_ALLOWED_HOSTS=neo-zion:7077` e a rede `zion-net`.
  - **Rede `zion-net`**: `internal: true`. O **`neo-core`** entra nela, o que exige
    recriar o container (uma queda curta, feita fora de turno).
- **`docker-compose.override.yml`** (fora do git): `zion` com
  `ports: ["10.66.66.1:7077:7077"]`.
- **Firewall:** no `PostUp` do `wg0.conf`, ao lado da regra `raw` que já existe:
  `iptables -I DOCKER-USER -p tcp --dport 7077 ! -i wg0 -j DROP`. O `DOCKER-USER` vê o
  pacote depois do DNAT, então a porta é a do container.
- **Token:** gerado na VPS (`openssl rand -hex 32`) e copiado para o Mac em
  `~/.config/zion/token` (0600). Nunca vai para git, memória, log nem conversa.
- **Mutagen:** encerrar o `zion-replica` e criar o `zion-backup` (§4).
- **Imagem base:** `node:22-alpine`, que já está na VPS. O disco estava em 92%.

## 7. Lado do Neo (neo-core)

### 7.1 Cliente

`core/zion/cliente.py`: cliente MCP mínimo sobre `httpx`. O `httpx` já está na imagem,
então não há dependência nova nem rebuild do `neo-core`.

- `initialize` com `clientInfo.name = "neo"` (o Zion deduz o agente pelo nome do
  cliente), depois `notifications/initialized` e `tools/call`;
- lê a resposta em **SSE**: na F0, `initialize`, `tools/list` e `tools/call` voltaram
  todos como `text/event-stream` (o JSON vem na última linha `data:`). Aceitar JSON
  também, porque custa uma linha e protege contra uma troca de versão do SDK;
- manda `Accept: application/json, text/event-stream` em toda chamada;
- guarda o `Mcp-Session-Id`; se o servidor responder 404 (sessão perdida num restart),
  reinicializa uma vez;
- timeout e teto de caracteres do resultado vêm do `neo.toml`;
- Zion fora do ar vira resultado legível ("a memória do Zion não respondeu: …"), nunca
  exceção que derruba o turno.

### 7.2 Tools

Três tools de leitura, `server-side`, no módulo `zion`:

| Tool | Zion | Para quê |
|---|---|---|
| `zion_overview(domain?)` | sem `domain`: `domain_list`; com `domain`: `context_get` | "o que tem no Zion", "quais projetos eu tenho", "onde fica o projeto X no meu Mac", "o que você sabe de X" |
| `zion_search(query, domain?, kinds?)` | `memory_search` | "o que decidimos sobre Y", "teve falha com Z", "qual é a regra de W" |
| `zion_get(id)` | `record_get` (compacto) | abrir um registro que apareceu numa busca |

A descrição separa o Zion do PKS: o Zion é **trabalho** (projetos, decisões, falhas,
planos); o PKS é a **memória pessoal** do Daniel. Pergunta sobre a vida dele continua no
PKS.

**Registro nos dois lugares** (gotcha do cofre e do e-mail): o executor vai no
`api/main.py` e as tools entram no `cognitive_tools_for` (`core/cognitive/read_tools.py`).
O executor implementa os dois contratos (`execute` e `execute_tool_call`). Os dois
arquivos são publicados juntos.

### 7.3 Autoridade (decisão no código)

- `core/authority/risk.py`: as três tools são `LEITURA_PRIVADA`, como `search_pks` e
  `claude_sessoes`. Não são fonte externa, então **não contaminam o turno**. O Zion é
  escrito pelos agentes do Daniel e por ele; o scanner de segredo do Zion barra chave e
  token na gravação.
- `core/authority/gate.py`: **NEGAR quando `from_external`**. Um contato do WhatsApp ou o
  remetente de um e-mail não leem o mapa dos projetos e das máquinas do Daniel.
- **Agenda: PERMITIR.** O cron de suporte da fase 2 precisa achar o projeto do chamado.

### 7.4 Configuração

Na seção `[zion]` do `neo.toml`, que a cópia do spool de falhas já cria:

```toml
[zion]
# (campos do spool de falhas, da cópia de 18/11)
consulta_habilitada = false   # as tools só entram na mesa com isto ligado
url = "http://neo-zion:7077/mcp"
timeout_s = 8
max_chars = 6000              # teto do texto que volta para o cérebro
```

Ordem de publicação (gotcha de 15/11): `shared/ajustes.py` antes do `config/neo.toml`.

### 7.5 Roteador

As perguntas sobre projeto têm de chegar ao cérebro. Antes de mexer no roteador, medir
com frases reais (regra de medir no corpus): se "o que o Zion sabe do forno-azul?" ou
"onde fica o projeto X?" caírem noutro trilho, aí entra um viés. Sem medida, não se mexe.

## 8. Fases

Cada fase só termina com a evidência da coluna da direita.

| Fase | O quê | Pronta quando |
|---|---|---|
| **F0** ✅ | Medições do desenho da réplica (§12) | Números no §12 |
| **F1** ✅ | Zion só leitura: `--read-only`, `domain_list`, `ZION_HOST`/`ZION_ALLOWED_HOSTS`, `Dockerfile` | Feita (§12) |
| **F2** | Zion remoto (§5.2): token, git que vem do Mac, tradução de caminho, hooks remotos, `install --remote`, tick em laço, `git` na imagem com escrita | `npm test` verde; no Mac **sem** `client.toml`, tudo igual a hoje; hooks remotos funcionando contra um servidor HTTP local de teste, numa pasta de dados de teste |
| **F3** | Infra na VPS e migração (§4, §6) | Uma sessão nova do Claude Code no Mac recebe o contexto vindo da VPS e grava checkpoint lá; do `neo-core`, `domain_list` responde pelo `neo-zion`; do sandbox e do `neo-core`, o `zion` com escrita não responde; o backup do Mac acompanha |
| **F4** | neo-core: cliente, as três tools, risco e portão, `[zion]` no `neo.toml` (§7). Desenvolvido numa cópia no scratchpad, suíte inteira lá, publicação com `cmp` de 3 vias | Testes: cliente com JSON e SSE e reinicialização no 404; portão nega externo e permite agenda; tools fora da mesa com a consulta desligada; Zion fora do ar não derruba o turno |
| **F5** | E2E (abaixo) | Os 6 itens |
| **F6** (opcional) | Publicar o spool de falhas (pronto na cópia de 18/11): merge com o `ai_service.py` e o `grpc_server.py` atuais. O pull agora roda no `zion-tick` | Uma falha forçada aparece no Zion em até 5 min |

**E2E da F5** (mensagens digitadas, menos de 240 caracteres):
1. "Quais projetos você conhece no Zion?" → lista com `neo` e `zion`, com as pastas.
2. "O que o Zion sabe do projeto neo? Tem falha aberta?" → cita a falha aberta e as
   regras, sem inventar.
3. "Onde fica o projeto zion no meu Mac?" → `/Users/daniel/Documents/projects/zion-memory-mcp`.
4. Uma memória salva pelo Claude Code no Mac aparece numa busca do Neo na hora (agora é a
   mesma pasta).
5. Pelo WhatsApp, um contato perguntando dos projetos → o portão nega (conferir no
   diário).
6. Com o WireGuard desligado no Mac, uma sessão nova do Claude Code abre sem travar, e o
   hook registra a falha no log.

## 9. Riscos

| Risco | O que fazer |
|---|---|
| VPS ou WireGuard fora do ar | O Claude trabalha sem Zion; os hooks desistem em 2 s. Fallback local é a decisão 2 do §10 |
| Token vazado | Quem estiver no WireGuard grava no Zion. Trocar: arquivo novo na VPS, reiniciar o `zion`, copiar para o Mac |
| Sessão do Claude que ficou aberta na migração | Grava no `~/.zion` local e se perde. Passo 1 do §4; `~/.zion` reaparecendo no Mac é o sinal |
| Dois escritores (`zion` e `zion-tick`) | O lock entre processos do Zion já cobre (design §8 de lá) |
| Imagem velha depois de mudar o Zion | Rebuild de `zion`, `zion-tick` e `neo-zion` faz parte do deploy de qualquer mudança no Zion. O `npm run build` do Mac deixa de ser o deploy do servidor; continua sendo o dos hooks |
| Disco da VPS (92%) | Os dados do Zion têm ~2 MB mais o `.git`; o `git` na imagem soma ~15 MB |
| Texto ruim numa memória vira instrução para o Neo | Aceito: o Zion é escrito pelos agentes do Daniel e por ele. O Neo não grava, então não há laço de auto-alimentação |
| Custo em tokens no Neo | `max_chars`; o `context_get` já é resumido (~1–2k tokens) |

## 10. Decisões (fechadas em 20/11)

1. **O Neo vê tudo**, inclusive o `homelab` e os checkpoints das sessões. O portão barra
   quem é de fora.
2. **Sem fallback offline por enquanto.** Com a VPS fora, o Claude trabalha sem Zion.
3. **Sem proxy:** o Claude fala com o Zion por HTTP, direto. O aviso de "plano mudou"
   continua funcionando pela tradução de caminho (§5.2).
4. **Tools do Neo com nomes genéricos, em inglês**, não presos a "projeto":
   `zion_overview`, `zion_search` e `zion_get` (§7.2).

## 11. Depois desta fase (fase 2, outro plano)

Fica registrado aqui para não se perder:
- o `claude_task` passa a receber o **projeto** (slug do Zion), e o código resolve a
  pasta (acaba com a pasta inventada pelo classificador);
- projeto desconhecido: o Neo pergunta; "está em ~/…" confere e cadastra; "clona" clona
  **uma vez** em `~/Neo/projetos/<nome>` (fora de `~/Documents/projects`, que o Mutagen
  copia para a VPS), com o repo vindo do Daniel ou do `gh repo list` dele, **nunca do
  e-mail**;
- **a escrita do Neo no Zion** (cadastrar projeto) não passa pelo `neo-zion`, que é só
  leitura. Vai por um caminho próprio, decidido na fase 2 (ex.: um token do Neo que só
  pode `domain_create`, usado só depois do "sim" do Daniel num turno limpo);
- worktree por chamado, papel analista só de leitura, tabela de chamados, revisor.

## 12. Registro de execução

### F0: medições (20/11/2026)

Antes da F0, tudo o que estava pendente no `neo` foi commitado em 5 commits
(`d980e74`..`c083eb1`, branch `formulario-regua`, sem push). O repo do Zion já estava
limpo (`4aa136a`).

**1. Réplica (Mutagen `zion-replica`, comando no §6).**
- Chegou só o subconjunto do §4: `config.toml`, `synonyms.yaml`, `.gitignore` e os
  domains `global`, `neo` e `zion` (neo: 160 memórias, 2 planos, 3 falhas). Não chegaram
  `homelab`, `sessions/`, `.git`, `.zion/`, `declined.toml` nem `recall.yaml`. Total: 1,4 MB.
- Arquivos com modo 0600, dono uid 1000 (`daniel`).
- **Latência:** um `plan_phase_update` no Mac apareceu na VPS em ~2,3 s, contando a
  chamada MCP e a conexão SSH da medição.

**2. `zion serve --http` numa cópia da réplica** (container `node:22` descartável na
`neo_default`, `--user 1000:1000`, `config.toml` da cópia com `host = "0.0.0.0"`).
- Subiu em ~25 s, quase todo de `npm ci` (151 pacotes, 84 MB) e build.
- **No boot ele grava:** `git init` com o commit `zion: init` da pasta inteira e
  `.zion/cache`. Não mexeu em nenhum registro. Com `/data` em `:ro`, isso derrubaria o
  boot; é o que o `--read-only` da F1 tem de pular.
- A cópia e o container foram apagados no fim. O disco ficou como estava (5,4 GB livres,
  93%).

**3. Chamadas de dentro do `neo-core` (httpx, `docker exec`).**
- `GET /health` com `Host: zion-f0:7077` → 200. O `/health` não confere o `Host`.
- `initialize` com `Host: zion-f0:7077` → **403** `Invalid Host header`. Confirma o item
  3 do §5.
- Com `Host: localhost:7077`, todas as respostas vieram em **`text/event-stream`**
  (`initialize`, `tools/list`, `tools/call`). `notifications/initialized` → 202.
- `tools/list`: **18 tools**, todas as de escrita incluídas. Confirma o item 1 do §5.
- Tempos e tamanhos (texto do resultado):

  | Chamada | Caracteres | Tempo |
  |---|---|---|
  | `context_get {domain: neo}` | 1901 | 22 ms |
  | `memory_search {query: "forno azul suporte", limit: 5}` | 1901 | 45 ms |
  | `record_get {id: neo}` | 371 | 16 ms |
  | `domain_resolve {cwd: …/neo/neo-core}` | 70 | 38 ms |

  O `context_get` e o `memory_search` pararam nos mesmos 1901 caracteres: é o orçamento
  de leitura do próprio Zion. O `max_chars = 6000` do §7.4 sobra; ele fica como teto de
  segurança.
- `domain_resolve` com um caminho do Mac funciona na VPS (é comparação de texto; `git`
  volta `null`).

**4. Isolamento.** Do `neo-workbench`, o nome `zion-f0` não resolve e a conexão estoura
o tempo. Era o esperado: o sandbox está na `workbench-net` (internal). Mesmo assim, a
`zion-net` própria continua no plano, para o `neo-zion` não ficar na `neo_default` junto
de postgres e mongo.

**O que a F0 mudou no plano:** cliente em SSE (§7.1), `user: "1000:1000"` e
`node:22-alpine` (§6), e o comando real do Mutagen (§6).

### F1: modo só leitura no Zion (20/11/2026)

Commit `6e0a9d8` no `zion-memory-mcp`. Desenvolvido numa cópia no scratchpad, porque lá
o `npm test` compila o `dist/`, que é o deploy no Mac. Copiado de volta depois de conferir
que o repo não tinha mudado desde a cópia.

**O que entrou:**
- `zion serve --http --read-only` (ou `ZION_READ_ONLY=1`): registra só as 6 tools de
  leitura; o `init()` não grava; o `Store` recusa escrita no `lock()` e nos `save*`
  (erro `read_only`), mesmo se alguém registrar uma tool de escrita por engano;
- `ZION_HOST` e `--host`; `ZION_ALLOWED_HOSTS` (ou `[server] allowed_hosts`) somam nomes ao
  `allowedHosts`, e a proteção contra DNS rebinding continua ligada;
- `domain_list`, com `repos: [{name, path, remote}]`. A CLI `zion domains` passou a
  mostrar o mesmo formato;
- `/health` com `read_only`;
- `Dockerfile` (`node:22-alpine`, multi-stage, usuário `node`) e `.dockerignore`;
- `tests/readOnly.test.ts` (5 testes) e a seção §8.1 no `docs/design.md` do Zion.

**Desvio do plano:** o diretório interno não foi para um temporário. Fora do lock, nada o
usa, então no modo só leitura ele simplesmente não é criado.

**Correção na réplica:** o Zion grava as pastas recusadas em `declined.yaml`, não em
`declined.toml` (o design estava errado). A sessão `zion-replica` foi recriada com
`--ignore /declined.yaml`. O arquivo ainda não existia no Mac, então nada vazou.

**Verificado:**
- Suíte do Zion: 70 de 71. A falha é o `CT-CON-05`, um snapshot que depende de dois
  registros caírem no mesmo segundo. Ele já falhava antes da F1 (1 em 4 rodadas isoladas,
  quase sempre com a suíte inteira) e foi registrado como falha no Zion.
- **Dados reais no Mac:** cópia do `~/.zion` igual à réplica, sem permissão de escrita, e
  `serve --read-only` na porta 7099 (o servidor do launchd ficou intocado):
  - `Host: neo-zion:7099` → 200, outro nome → 403;
  - 6 tools; `memory_save` → `Tool memory_save not found`;
  - `domain_list` com as pastas de `neo` e `zion`;
  - nenhum arquivo criado nem alterado (181 antes e depois).
- **Imagem na VPS:** contexto com modo 0600, como o Mutagen grava, e build ok (266 MB, dos
  quais 230 MB são da base que já estava lá). Container com `--read-only`, `tmpfs /tmp`,
  `--cap-drop ALL`, `no-new-privileges`, 256 MB, a réplica real em `:ro` e
  `ZION_ALLOWED_HOSTS=zion-f1:7077`:
  - healthcheck `healthy`; roda como `node` (uid 1000); a CLI de dentro funciona;
  - de dentro do `neo-core`, pelo nome `zion-f1:7077` sem forjar `Host` → 200;
  - 6 tools; `record_update` → `Tool record_update not found`.
  - Container, imagem e arquivos de teste apagados depois (disco: 92%, 5,8 GB livres).
- **Mac em modo normal:** `zion doctor` e `zion domains` pelo shim funcionam. As sessões
  novas do Claude Code já usam o `dist/` novo. O servidor HTTP do launchd continua com o
  código antigo até reiniciar; não reiniciei para não derrubar as sessões abertas.

### Revisão do desenho (20/11/2026, depois da F1)

O Daniel decidiu levar o Zion inteiro para a VPS, com o Claude Code do Mac falando com
ele pelo WireGuard (§0, §2). O levantamento que mudou o plano:
- `claude mcp get zion` e `~/.codex/config.toml`: os dois usam **stdio**
  (`~/.local/bin/zion serve`), um processo por sessão, gravando direto em `~/.zion`. O
  launchd só tem o `dev.zion.memory.tick`. O `serve --http` da 7077 é um `npm run dev`
  (`node --watch … dist/cli.js serve --http --port 7077`).
- `src/service.ts`: `realpath`/`inspectRepo` em `domainResolve`, `domainCreate`,
  `domainDecline`, `sessionStart`, `sessionCheckpoint`, `sessionResume` (e
  `changedFiles` no modo review), e leitura do arquivo em `planSave`. Daí o campo
  `client` e o `zion proxy` do §5.2.
- `src/hooks.ts`: os hooks leem o transcript local (`scanTranscript`) e chamam o serviço
  direto. Daí o `POST /hooks/<evento>` com o resumo feito no Mac.
- Do `neo-workbench`, `http://10.66.66.1:8000/health` e `http://172.17.0.1:8000/health`
  dão 403 no proxy (a lista negra cobre `10.x` e `172.16–31.x`).

O que continua valendo do desenho anterior: o modo só leitura (F1) vira o `neo-zion`, e o
lado do Neo (§7) não muda. O que sai: a réplica `zion-replica` (Mac → VPS), que é
encerrada no passo 2 da migração.
