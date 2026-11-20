# Neo × Zion: o Neo consulta a memória de trabalho (fase 1) — plano

**Data:** 2026-11-20
**Status:** só plano. Nada implementado.
**Fase 1 de 2.** A fase 2 (chamado de suporte → o Neo acha o projeto → chama o Claude
na pasta dele → plano → aprovação → PR) é outro plano, escrito depois desta fase
funcionar. Contexto da fase 2: `docs/demo-suporte-ponta-a-ponta.md`.
**Depende de:** o Zion (`~/Documents/projects/zion-memory-mcp`, design em
`docs/design.md` de lá) e o portão de autoridade (`core/authority/`).

---

## 0. O pedido

O Daniel quer que o Neo consulte o Zion: quais projetos existem, em que pasta do Mac
cada um mora, e o que os agentes já sabem de cada um (regras, decisões, falhas
abertas). Isso é o alicerce da fase 2: o Neo só consegue chamar o Claude "na pasta do
projeto" se souber onde ela fica. Ele pediu que a ligação seja **pela própria VPS**,
sem depender do Mac no caminho, e em fases separadas, com esta integração primeiro.

## 1. Onde as coisas estão hoje (medido em 20/11)

- **O Zion roda no Mac, não na VPS.** O `zion serve --http` sobe pelo launchd em
  `127.0.0.1:7077/mcp`, e os dados ficam em `~/.zion` (repo git próprio). Na VPS não
  há processo do Zion nem `~/.zion`. Só o **código** do repo chega lá, porque a sessão
  `contabo-projects` do Mutagen copia `~/Documents/projects` inteiro.
- **O domain já é o cadastro de projeto.** O `domain.md` guarda
  `repos: [{name, path, remote}]`, e o `domain_resolve(cwd, git_remote)` acha o domain
  pela pasta ou pelo remote. Hoje existem 4: `global`, `homelab`, `neo` e `zion`.
- **Não há tool MCP para listar os domains.** Existe `ZionService.domainList()`, que só
  a CLI usa (`zion domains`).
- **As leituras não gravam.** `contextGet`, `memorySearch` e `recordGet` não chamam
  lock, escrita nem commit (conferido no `src/service.ts`).
- **Subir o servidor grava.** O `startHttp` chama `service.init()`, que pega o lock, cria
  `.zion/cache`, garante o repo git e faz commit. Com a pasta montada só para leitura, ele
  morreria no boot.
- **O servidor HTTP só aceita `localhost`.** O transporte liga a proteção contra DNS
  rebinding, e o `allowedHosts` tem só `127.0.0.1`/`localhost`. Uma chamada a
  `http://neo-zion:7077` seria recusada. O host de escuta vem do `config.toml`
  (`127.0.0.1`), e não há flag `--host` na CLI (só `--port`).
- **Plano com caminho do Mac:** o `planProgress` só compara o `.md` de origem quando o
  arquivo existe (`existsSync`). Na VPS o caminho não existe, e o campo vira `null`, sem
  alarme falso.
- **Lado do Neo:** o `httpx` já está no `pyproject.toml`; não há cliente MCP nem tool do
  Zion. A única ponte Neo → Zion é o **spool de falhas** (§11 do design do Zion), que
  está pronta numa cópia de 18/11 (sessão `f8a807a4`, `scratchpad/dev/neo-core`) e **não
  foi publicada**. Essa cópia já cria a seção `[zion]` no `neo.toml` e a classe `Zion` no
  `shared/ajustes.py`.

## 2. Decisão: réplica só de leitura na VPS

| | Réplica só de leitura na VPS (**escolhida**) | Mover o Zion para a VPS | O Neo chama o Zion do Mac (WireGuard ou desktop) |
|---|---|---|---|
| Fonte da verdade | Mac, como hoje | VPS | Mac |
| Claude Code e Codex | não mudam | passam a falar com a VPS pelo WireGuard; os hooks (que hoje escrevem direto em `~/.zion`) precisam virar cliente HTTP | não mudam |
| Neo lê com o Mac desligado | sim (último estado) | sim | não |
| Neo lê no turno da agenda | sim | sim | só pelo desktop, e só com ele aberto |
| O Neo consegue gravar no Zion | **não, por construção** | sim, se não travar | sim, se não travar |
| Porta nova | nenhuma | a do Zion no WireGuard | a do Mac no WireGuard (ou nenhuma, pelo desktop) |

**Por que a réplica:**
1. É a ligação "pela própria VPS" que o Daniel pediu, sem mexer no que já funciona no
   Mac.
2. **O Neo não pode gravar no Zion.** O Zion entra no começo de toda sessão do Claude
   Code (hook `SessionStart`). Se o Neo gravasse num turno que leu um e-mail, uma injeção
   viraria instrução permanente para todas as sessões do Claude do Daniel. Com a réplica,
   não existe caminho de escrita: não é regra de prompt nem lista de tools, é a
   arquitetura. A única escrita do Neo continua sendo o spool de falhas, com formato fixo,
   que o `zion pull` do Mac valida.

**Custo:** com o Mac desligado, a réplica fica parada no último estado (o que é aceitável:
projeto e pasta mudam pouco), e uma parte da memória passa a existir na VPS (§4).

## 3. Arquitetura

```
 Mac                                         VPS (contabo-neo)
 ~/.zion  (fonte; Claude Code, Codex,        /home/daniel/.zion-replica
           hooks e zion tick, como hoje)              │  montado :ro em /data
     │                                                ▼
     └── Mutagen "zion-replica" ──────────►  neo-zion (container)
         one-way-replica, Mac → VPS            zion serve --http --read-only
         (só o subconjunto do §4)              0.0.0.0:7077, sem porta publicada
                                                      ▲
                                                      │ rede zion-net (internal)
                                                      │
                                              neo-core (entra na zion-net)
                                                core/zion/cliente.py (httpx, MCP)
                                                tools zion_projetos / zion_buscar / zion_ler
```

- **`zion-net`** é `internal: true` e só tem o `neo-zion` e o `neo-core`. O sandbox
  (`workbench-net`), o proxy dele e o navegador (`browser-net`) não alcançam o Zion.
- **Nenhuma porta publicada**, nem em `127.0.0.1` do host.
- O container roda com `read_only: true`, `/data` montado `:ro`, `tmpfs` em `/tmp`,
  `cap_drop: ALL`, `no-new-privileges` e limite de memória.
- O código do Zion chega à VPS pelo `contabo-projects`, mas **a imagem não se reconstrói
  sozinha**: mudou o Zion de um jeito que importa para a leitura, tem de rebuildar o
  `neo-zion`.

## 4. O que vai para a VPS

| Entra | Fica no Mac |
|---|---|
| `config.toml`, `synonyms.yaml` | `domains/*/sessions/` (checkpoints: o resumo do trabalho de cada conversa) |
| `domains/*/domain.md` | `domains/*/transcripts/` |
| `domains/*/memories/`, `plans/`, `tasks/`, `failures/` | `.zion/` (lock e cache), `.git/` |
| | `declined.toml`, `recall.yaml` |
| | `domains/homelab/` (**decisão em aberto**, §10) |

- As `sessions/` ficam fora por causa da regra de não deixar conteúdo das conversas na
  VPS: os checkpoints são o resumo do que cada sessão fez. O Neo não precisa deles.
  Efeito: o `context_get` da réplica vem sem a seção de sessões, e o resto (regras,
  planos, falhas, tarefas) vem igual.
- O `global` entra porque o `context_get` de qualquer domain soma as regras do `global`.

## 5. Mudanças no Zion (repo `zion-memory-mcp`)

1. **`zion serve --http --read-only`**
   - registra só as tools de leitura: `domain_list` (nova), `domain_resolve`,
     `context_get`, `memory_search`, `record_get` e `record_related`;
   - não chama `init()`, não pega lock, não faz commit;
   - o diretório interno (`.zion/`) vai para um diretório temporário;
   - defesa em profundidade: no modo só leitura, o `Store` levanta erro em qualquer
     escrita, mesmo que alguém registre uma tool de escrita por engano.
2. **`domain_list`**: tool nova, de leitura, sobre o `domainList()` que a CLI já usa,
   devolvendo também o `remote` de cada repo.
3. **Host:** `--host` na CLI (ou `ZION_HOST`), e `ZION_ALLOWED_HOSTS` para somar
   `neo-zion:7077` ao `allowedHosts`. A proteção contra DNS rebinding continua ligada.
4. **`Dockerfile`**: Node 22 slim, `npm ci`, `npm run build`, usuário não-root,
   `HEALTHCHECK` no `/health` que o servidor já tem.
5. **Testes (vitest):**
   - no modo só leitura, as tools de escrita não existem;
   - com a pasta de dados sem permissão de escrita, o servidor sobe e responde;
   - `domain_list` devolve slug, nome, descrição, pastas e remote;
   - o `allowedHosts` aceita o host configurado e recusa outro.

No Mac nada muda: o modo só leitura é desligado por padrão, e o `npm run build` continua
sendo o deploy de lá.

## 6. Infra na VPS

- **Mutagen**, criado do Mac (a sintaxe exata dos ignores é conferida na F0):
  ```
  mutagen sync create --name zion-replica --mode one-way-replica \
    --ignore-vcs --ignore sessions --ignore transcripts --ignore .zion \
    --ignore declined.toml --ignore recall.yaml --ignore domains/homelab \
    ~/.zion contabo-neo:/home/daniel/.zion-replica
  ```
  `one-way-replica` não tem conflito: o que existir do lado da VPS e não vier do Mac é
  apagado.
- **`docker-compose.yml`**: serviço `neo-zion` (build de `../zion-memory-mcp`), volume
  `/home/daniel/.zion-replica:/data:ro`, `ZION_DATA_DIR=/data`,
  `command: serve --http --read-only --host 0.0.0.0`, as travas do §3,
  `restart: unless-stopped`, rede `zion-net`.
- **Rede `zion-net`**: `driver: bridge`, `internal: true`.
- **`neo-core`** entra na `zion-net`. Isso exige recriar o container
  (`docker compose up -d --no-deps neo-core`): uma queda curta, feita fora de turno.

## 7. Lado do Neo (neo-core)

### 7.1 Cliente

`core/zion/cliente.py`: cliente MCP mínimo sobre `httpx`. O `httpx` já está na imagem,
então não há dependência nova nem rebuild do `neo-core`.

- `initialize` com `clientInfo.name = "neo"` (o Zion deduz o agente pelo nome do
  cliente), depois `notifications/initialized` e `tools/call`;
- aceita resposta em JSON e em SSE (o transporte do SDK pode responder dos dois jeitos;
  qual sai é medido na F0);
- guarda o `Mcp-Session-Id`; se o servidor responder 404 (sessão perdida num restart),
  reinicializa uma vez;
- timeout e teto de caracteres do resultado vêm do `neo.toml`;
- Zion fora do ar vira resultado legível ("a memória do Zion não respondeu: …"), nunca
  exceção que derruba o turno.

### 7.2 Tools

Três tools de leitura, `server-side`, no módulo `zion`:

| Tool | Zion | Para quê |
|---|---|---|
| `zion_projetos(projeto?)` | sem `projeto`: `domain_list`; com `projeto`: `context_get` | "quais projetos eu tenho", "onde fica o projeto X no meu Mac", "o que você sabe do projeto X" |
| `zion_buscar(consulta, projeto?, tipos?)` | `memory_search` | "o que decidimos sobre Y", "teve falha com Z" |
| `zion_ler(id)` | `record_get` (compacto) | abrir um registro que apareceu numa busca |

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
| **F0** | Medições, sem código: (1) Mutagen `one-way-replica` com os ignores: o que chega e quanto demora uma gravação do Mac até a VPS; (2) um `zion serve --http` num container Node na VPS lendo uma **cópia** da réplica: o que ele tenta gravar; (3) de dentro do `neo-core`, `initialize` + `tools/call` por `curl`: resposta em JSON ou SSE, e o `Host` recusado; (4) tamanho do `context_get neo` e de um `memory_search` (o `zion budget-check` já mede) | Números anotados no §12 |
| **F1** | Zion: `--read-only`, `domain_list`, `--host`/`ZION_ALLOWED_HOSTS`, `Dockerfile`, testes do §5 | `npm test` passa; no Mac, o `zion serve` de sempre continua igual (o Claude Code abre sessão e grava checkpoint) |
| **F2** | Infra: sessão `zion-replica`, `neo-zion` no compose, rede `zion-net`, `neo-core` na rede | Do `neo-core`, `tools/call domain_list` responde; do `neo-workbench`, `neo-zion` não resolve nem conecta; uma memória salva no Mac aparece na réplica |
| **F3** | neo-core: cliente, as três tools, risco e portão, `[zion]` no `neo.toml`. Desenvolvido numa cópia no scratchpad, suíte inteira lá, publicação com `cmp` de 3 vias | Testes: cliente com JSON e SSE e reinicialização no 404; portão nega externo e permite agenda; tools fora da mesa com a consulta desligada; Zion fora do ar não derruba o turno |
| **F4** | E2E pelo chat | Ver abaixo |
| **F5** (opcional) | Publicar o spool de falhas (pronto na cópia de 18/11): precisa de merge com o `ai_service.py` e o `grpc_server.py` atuais | Uma falha forçada aparece no Zion do Mac depois do `zion tick` |

**E2E da F4** (mensagens digitadas, menos de 240 caracteres):
1. "Quais projetos você conhece no Zion?" → lista com `neo` e `zion`, com as pastas.
2. "O que o Zion sabe do projeto neo? Tem falha aberta?" → cita a falha aberta e as
   regras, sem inventar.
3. "Onde fica o projeto zion no meu Mac?" → `/Users/daniel/Documents/projects/zion-memory-mcp`.
4. Uma memória nova salva no Mac (pelo Claude Code) aparece numa busca do Neo em menos
   de um minuto.
5. Pelo WhatsApp, um contato perguntando dos projetos → o portão nega (conferir no
   diário).

## 9. Riscos

| Risco | O que fazer |
|---|---|
| Mac desligado | A réplica fica no último estado; é aceitável para projeto e pasta. Se um dia precisar saber a idade da réplica, a `domain_list` devolve o `updated_at` |
| Imagem do `neo-zion` velha depois de mudar o Zion | Rebuild do `neo-zion` faz parte do "deploy" de qualquer mudança no formato dos registros |
| Texto ruim numa memória vira instrução para o Neo | Aceito: o Zion é escrito pelos agentes do Daniel e por ele. O Neo não grava, então não há laço de auto-alimentação |
| Custo em tokens | `max_chars`; o `context_get` já é resumido (~1–2k tokens) |
| A mesma memória em dois lugares (nativa do Claude e Zion) | Fora do escopo; a decisão de desligar a nativa continua com o Daniel |

## 10. Decisões em aberto (do Daniel)

1. **`homelab` na réplica?** Ele tem o mapa da infra (hosts, usuários, portas). Não tem
   segredo, porque o scanner barra, mas o Neo não precisa dele para projetos.
   **Recomendo fora.**
2. **`sessions/` fora da réplica?** Recomendo fora (§4).
3. **Nomes das tools:** `zion_projetos`, `zion_buscar`, `zion_ler`.

## 11. Depois desta fase (fase 2, outro plano)

Fica registrado aqui para não se perder:
- o `claude_task` passa a receber o **projeto** (slug do Zion), e o código resolve a
  pasta (acaba com a pasta inventada pelo classificador);
- projeto desconhecido: o Neo pergunta; "está em ~/…" confere e cadastra; "clona" clona
  **uma vez** em `~/Neo/projetos/<nome>` (fora de `~/Documents/projects`, que o Mutagen
  copia para a VPS), com o repo vindo do Daniel ou do `gh repo list` dele, **nunca do
  e-mail**;
- **a escrita do Neo no Zion** (cadastrar projeto) não pode passar pela réplica. Vai por
  um spool de pedidos que o `zion pull` aplica no Mac, ou pelo desktop, sempre num turno
  limpo do Daniel;
- worktree por chamado, papel analista só de leitura, tabela de chamados, revisor.

## 12. Registro de execução

(preencher a cada fase: data, o que foi medido, desvios do plano)
