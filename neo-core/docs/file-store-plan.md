# Plano: File Store — entrada, catálogo e uso de arquivos pelo Neo

> **Status: F1 EM PRODUÇÃO** (2026-07-16) — `NEO_FILES_ENABLED=true` na VPS,
> migração aplicada, volume `neo-files` montado e **aceite da F1 validado ao
> vivo** (upload → read_slice → file_stage → `wc -l` no sandbox → GC).
> Ver §15 "Estado da implementação". F2–F6 pendentes.
> **Motivação:** o Neo não tem como RECEBER um arquivo. Não existe endpoint de
> upload; anexo de WhatsApp morre no gateway (que já baixa a mídia, mas o core
> nunca consome); o desktop não tem botão de anexar. E quando o próprio Neo
> PRODUZ um arquivo (research PDF, output de job no sandbox), ele fica solto em
> `/workspace` sem catálogo, dono, validade ou tela de consulta. Queremos:
> **uma entrada única de arquivos** (qualquer canal), um **catálogo persistido**
> (temporário vs permanente), **leitura fracionada para economizar tokens**
> (manifesto + fatias, nunca o arquivo inteiro no contexto), uma função de
> **materializar o arquivo onde ele for usado** (workbench hoje; browser-agent,
> gateway etc. depois) e uma **aba Arquivos no neo-panel**.

---

## 1. Estado atual (o que já existe e vamos reusar)

| Peça | Onde | O que dá pra reusar |
|------|------|---------------------|
| Sandbox do Neo + volume `neo-workspace` | `core/workbench/sandbox.py`, `docker-compose.yml` | É o "ambiente de manipulação" destino do stage; `resolve_path` é o precedente de path-jail |
| Tools inline no cognitivo (padrão read tools/workbench) | `core/cognitive/read_tools.py`, `api/main.py:194-204` | As tools `file_*` entram do mesmo jeito: `ToolSchema` server-side + registro condicional a flag |
| Registry/Executor por módulo | `core/tools/registry.py`, `core/tools/executor.py` | `register_module_executor("files", ...)` |
| Padrão de tabela de lifecycle | `db/models/agent_job.py` | Molde do `stored_file` (uuid, user/session, status, meta JSON, índices) |
| Loop de background com tick | JobManager (`core/jobs/`), engine-monitor do neo-voice | Molde do GC de arquivos expirados |
| Media do WhatsApp já baixável | `neo-whatsapp/src/index.js:568,583` (`mediaType`, `GET /chats/:jid/media/:msgId`) | Ingestão WhatsApp = baixar do gateway e registrar no store |
| Artefato em `/workspace` que o Neo relê | `db/models/research_task.py:92-95` (`report_path`) | Precedente informal; vira registro de verdade no catálogo (F6) |
| Metadata da mensagem chega ao core | `modules/desktop/ai_service.py` (`process_user_input(metadata=...)`, ex.: `mode=live`) | `metadata["attachments"]` viaja pelo mesmo canal |
| Lição de ativação de tools (skill finanças) | memória `neo-financas-skill` | Tool disponível ≠ tool usada: anexo presente ⇒ injetar tools `file_*` + manifesto no prompt |
| Aba nova no panel | `Sidebar.tsx`, `ResearchPanel.tsx` (padrão fetch+JWT) | Molde da aba Arquivos |

## 2. O gap

1. **Entrada** — nenhum endpoint multipart; nenhum canal entrega arquivo ao cérebro.
2. **Catálogo** — nenhum registro de "que arquivos o Neo tem", dono, origem, validade.
3. **Leitura econômica** — `neo_read_file` lê texto cru até o cap (60KB) e só do
   workspace; não existe fatia por linhas/página, nem extração de texto de
   PDF/DOCX/XLSX, nem manifesto compacto no prompt.
4. **Materialização** — arquivo recebido não tem como "aparecer" no `/workspace`
   para o `neo_bash` manipular (o core nem monta o volume).
5. **Ciclo de vida** — nada distingue temporário de permanente; nada expira; sem
   quota; `/workspace` só cresce.
6. **Consulta** — nenhuma tela mostra os arquivos.

## 3. Arquitetura proposta

```
 desktop (anexo/drag-drop) ─┐
 WhatsApp (media)  ─────────┤  POST /api/v1/files          ┌─────────────────────┐
 neo-panel (upload) ────────┼──────────────────────────────►  FileStore          │
 URL (web_fetch/download) ──┘                               │  (core/files/)      │
                                                            │                     │
 Neo produz (workspace) ── file_save (harvest) ────────────►│  blobs por sha256   │
                                                            │  + derived (texto)  │
                                                            │  + stored_file (PG) │
                                                            └──────┬──────────────┘
                                                                   │
        prompt do cognitivo ◄── manifesto compacto (id, nome, tipo, tamanho, páginas)
        tools file_* ◄────────── leitura fracionada / stage / save / retenção
                                                                   │
                                   file_stage ──► /workspace/files/<nome>  (sandbox cp)
                                                                   │
        GC (tick 1h): temp expirado → expired → unlink blob (refcount por sha)
        neo-panel /arquivos: lista, preview, download, pin, delete, uso de quota
```

**Princípio central de tokens:** o conteúdo do arquivo NUNCA entra inteiro no
contexto. O prompt recebe só o **manifesto** (uma linha por anexo); o modelo
decide o que ler via `file_read` em fatias com cap por chamada. Para binários,
lê-se o **texto derivado** (extraído uma vez, cacheado em disco).

### 3.1 Layout físico (volumes)

- Volume novo **`neo-files`**:
  - `neo-core` monta **rw** em `/data/files` — API e tools leem/escrevem direto
    (sem base64-por-exec; upload de 20MB é um `shutil` local).
  - `neo-workbench` monta **ro** em `/files` — o sandbox enxerga os blobs mas
    **não altera o store** (integridade por construção).
- `neo-core` passa a montar **`neo-workspace` ro** em `/workspace` — só para o
  harvest (`file_save`) copiar output do Neo pro store sem exec/base64.
- Estrutura dentro de `/data/files`:
  - `blobs/<sha256[:2]>/<sha256>` — conteúdo imutável, deduplicado por hash.
  - `derived/<sha256>/text.txt` + `derived/<sha256>/pages.json` — texto extraído
    e índice de offsets por página (PDF) ou por aba (XLSX).

### 3.2 Modelo de dados — `stored_file` (migração alembic)

```
id UUID pk · user_id UUID idx · session_id UUID nullable idx
origin: upload-desktop | upload-panel | whatsapp | url | workbench | research | job
name (original) · mime · size_bytes · sha256 idx
retention: temp | perm      · expires_at (NULL se perm) idx
status: available | expired | deleted
meta_json: {pages, line_count, extraction: pending|ok|failed, preview_head,
            source: {jid/msg_id | url | workspace_path}, summary?}
created_at · last_accessed_at · access_count
```

Dedup: blob compartilhado por sha; a linha é por-ingestão (mesmo arquivo enviado
2x = 2 registros, 1 blob). Unlink do blob só quando nenhum registro `available`
aponta pro sha.

### 3.3 FileStore service (`core/files/`)

`store.py` (ingest de bytes/stream/path, get, open_slice, stage, harvest,
set_retention, delete, gc_pass, usage), `extract.py` (orquestra extração via
sandbox, §3.5), `tools.py` (schemas), `executor.py` (tool → store). Guardas:
path sempre resolvido e preso ao root; leitura de `/workspace` com
`os.path.realpath` + checagem de prefixo + `O_NOFOLLOW` (o sandbox poderia criar
symlink malicioso — o core não pode segui-lo para fora do mount).

## 4. Tools do cognitivo (módulo `files`, server-side, prefixo `file_`)

Prefixo `file_` entra na lista de exclusão do roteamento direto (como `neo_`/
`job_`): só o cognitivo aciona.

| Tool | Faz | Anti-desperdício |
|------|-----|------------------|
| `file_list` | Lista catálogo (filtros: retention, origin, sessão atual, texto) | Devolve tabela compacta, cap 50 |
| `file_info` | Metadados + preview (primeiros ~500 chars do texto) + nº páginas/linhas | Sempre chamar ANTES de ler |
| `file_read` | **Leitura fracionada**: `start_line`/`max_lines` OU `page` (usa `pages.json`); binário lê do texto derivado | Cap duro ~8k chars/chamada; devolve `has_more` + próximo offset |
| `file_search` | Grep no texto derivado, devolve linhas com contexto ±2 | Achar a agulha sem paginar o palheiro (F2) |
| `file_stage` | **Materializa no ambiente de uso**: copia blob → `/workspace/files/<nome>` (via `sandbox.run("cp /files/blobs/… …")`) e devolve o path | O `neo_bash` manipula o arquivo REAL sem ele nunca passar pelo contexto |
| `file_save` | **Harvest**: registra um path do `/workspace` no store (`retention` param) — "guarda esse resultado" | Output de job vira arquivo permanente consultável |
| `file_set_retention` | temp↔perm ("guarda esse arquivo" / "pode jogar fora depois") | — |

*Sem `file_delete` como tool* — deletar é ação do usuário (panel/API); o LLM no
máximo marca temp.

**Design do `file_stage` (a "função que traz o arquivo pro ambiente")**: assinatura
`file_stage(file_id, target="workbench", name?)`. `target` é enum aberto — F1 só
implementa `workbench`, mas a interface já nasce plural para os próximos
ambientes (browser-agent: F2 dele já tem volume de perfil; gateway: corpo de
upload HTTP). Cada target tem um `Stager` registrado (padrão strategy) que sabe
materializar o blob lá e devolver a referência utilizável (path, URL interna).

## 5. Fluxo de anexo na conversa (economia de tokens)

1. Cliente sobe o arquivo (`POST /api/v1/files`, multipart) → recebe `file_id`.
2. Cliente manda a mensagem de chat com `metadata["attachments"] = [file_id, …]`.
3. `ai_service.process_user_input` valida os ids e o prompt builder
   (`core/agent/prompt_builder.py` — confirmar seam exato na implementação)
   anexa o **manifesto** ao turno do usuário:
   `[anexo: relatorio.pdf — PDF, 1.2 MB, 34 págs, id=f_ab12. file_info/file_read para ler; file_stage para manipular]`
4. Anexo presente ⇒ tools `file_*` são **incluídas à força** no subset do turno
   (lição da skill finanças: keyword/manifesto ativa, senão o modelo ignora).
5. O modelo lê só o que precisa (`file_info` → `file_read` fatiado → responde),
   ou materializa (`file_stage` → `neo_bash python …`) para trabalho pesado.

Upload SEM mensagem também vale: o arquivo fica no catálogo (temp) e pode ser
referenciado depois ("aquele CSV que te mandei ontem" → `file_list`).

## 6. Extração de texto (F2) — parsing hostil roda NO SANDBOX

PDF/DOCX/XLSX viram `derived/<sha>/text.txt` para o `file_read`/`file_search`.
Parser de formato binário é superfície de ataque clássica ⇒ **extrair dentro do
`neo-workbench`** (não no processo do core): o core roda
`sandbox.run("python /skills/_internal/extract.py /files/blobs/<sha> --mime …")`
que escreve num tmp do workspace; o core valida e move pro `derived/`. Libs
pré-instaladas na imagem do sandbox: `pypdf`/`pdfminer.six`, `python-docx`,
`openpyxl`. Extração é **lazy** (primeira leitura) com `extraction: pending|ok|
failed` no `meta_json`; XLSX derived = CSV por aba com índice em `pages.json`.
Imagem: sem extração (F1/F2); visão entra depois como derived opcional.

## 7. Temporário vs permanente + GC + quota

- Default de ingestão por chat/WhatsApp: **temp**, `expires_at = now + NEO_FILES_TTL_DAYS` (7).
- **perm**: pin no panel, `file_set_retention`, `file_save(retention="perm")`,
  ou upload já marcado (checkbox no panel / flag na API).
- **GC**: task asyncio no core (tick 1h; padrão `_offline_checker`): `available`
  + temp + `expires_at < now` → `status=expired`, unlink do blob se refcount
  zero, remove `derived/`. Registro expira mas a LINHA fica (auditável no panel,
  purge manual). Staged copies em `/workspace/files/` não são geridas pelo GC
  (workspace é rascunho por definição; um `find -mtime` no próprio sandbox
  resolve depois, se incomodar).
- **Quota**: `NEO_FILES_MAX_FILE_MB` (500) rejeita no upload;
  `NEO_FILES_QUOTA_GB` (2) — acima disso upload falha com mensagem clara e o
  panel mostra a barra em vermelho.

## 8. API HTTP (`api/routes/files.py`)

| Rota | Uso | Auth |
|------|-----|------|
| `POST /api/v1/files` (multipart: file, retention?, session_id?, origin?) | upload (desktop, panel, gateway WA) | mesma auth do cliente que chama (desktop = padrão das rotas desktop; panel = JWT) |
| `GET /api/v1/files?retention=&origin=&status=&q=` | listagem do panel | panel JWT |
| `GET /api/v1/files/{id}` | metadados | panel JWT |
| `GET /api/v1/files/{id}/download` | `FileResponse` do blob (panel baixar; desktop "me manda o arquivo") | conforme chamador |
| `GET /api/v1/files/{id}/preview` | texto derivado (cap) ou bytes de imagem p/ thumb | panel JWT |
| `PATCH /api/v1/files/{id}` | retention/name | panel JWT |
| `DELETE /api/v1/files/{id}` | soft-delete + unlink refcount | panel JWT |
| `GET /api/v1/files/usage` | total, por retention, % quota | panel JWT |

## 9. Ingestão por canal

- **neo-panel (F3):** botão upload na aba Arquivos (multipart direto).
- **desktop-tauri (F4):** clipe + drag-drop no `ChatPage.tsx` → upload → chip de
  anexo → mensagem sai com `metadata.attachments`. (Rebuild = `tauri dev`,
  NUNCA `tauri build`.)
- **WhatsApp (F4):** mensagem inbound com `mediaType ∈ {document, image}` → core
  baixa via `GET /chats/:jid/media/:msgId` do gateway → ingest (origin
  `whatsapp`, temp) → manifesto na mensagem que segue pro pipeline. (Áudio/ptt
  continua no fluxo de voz existente.)
- **URL (F5):** `file_from_url` ou variante do `web_fetch`: baixa server-side
  (respeitando o mesmo egress do core) e cataloga — "baixa esse CSV e analisa".
- **Neo produz (F1 harvest manual / F6 auto):** `file_save` já na F1; F6
  auto-registra artefatos conhecidos (PDF do research, outputs declarados de
  job) com origin próprio.

## 10. Aba Arquivos no neo-panel (F3)

Rota `/arquivos`, grupo **Operação** (`Sidebar.tsx`), `FilesPanel.tsx` no molde
do `ResearchPanel`:

- Header: barra de uso da quota (total/`NEO_FILES_QUOTA_GB`), contadores
  (temp/perm/expirados), botão **Upload** (com toggle temp/perm).
- Filtros: retention, origin, status, busca por nome.
- Tabela: nome, tipo (ícone por mime), tamanho, origem, retenção (badge perm /
  countdown "expira em 3d"), sessão, último acesso, nº de acessos.
- Ações por linha: **preview** (modal: texto derivado paginado ou imagem),
  **download**, **pin/unpin** (temp↔perm), **delete** (confirmação).
- Linha expandida: sha256, path da origem, status da extração, sessões que
  referenciaram.

## 11. Segurança

1. **Path-jail** em toda resolução (`resolve_path` do store, molde do sandbox).
2. **Symlink do sandbox**: harvest e stage nunca seguem symlink
   (`realpath`+prefixo, `O_NOFOLLOW`) — o sandbox não pode fazer o core ler
   fora do mount.
3. **Parsing no sandbox** (§6): PDF malicioso explode dentro da jaula, não no core.
4. **Store ro no sandbox**: blobs invioláveis por construção.
5. **Mime**: registra o declarado + sniff dos magic bytes; divergência marca
   `meta_json.mime_mismatch` (informativo, não bloqueia).
6. **Limites**: tamanho por arquivo, quota global, cap por `file_read`, cap de
   extração (texto derivado truncado em N MB).
7. **Download do panel** com `Content-Disposition: attachment` (não renderizar
   HTML hostil no domínio do panel).

## 12. Config (shared/config.py)

`NEO_FILES_ENABLED` (default **false** até o smoke) · `NEO_FILES_ROOT`
(`/data/files`) · `NEO_FILES_TTL_DAYS` (7) · `NEO_FILES_MAX_FILE_MB` (500) ·
`NEO_FILES_QUOTA_GB` (2) · `NEO_FILES_READ_CAP_CHARS` (8000) ·
`NEO_FILES_GC_INTERVAL_S` (3600). Rollback = flag off + restart (tools somem do
registry, rotas devolvem 503).

## 13. Fases

- **F1 — Núcleo** *(fundação)*: volume `neo-files` + mounts (core rw, sandbox
  ro, workspace ro no core); migração `stored_file`; `core/files/` (store +
  executor); tools `file_list`/`file_info`/`file_read` (texto puro)/
  `file_stage(target=workbench)`/`file_save`/`file_set_retention`; rotas upload/
  download/list/delete; manifesto + force-include das tools quando há anexo;
  GC básico; flag off.
  *Aceite:* upload via curl → `file_id`; mensagem com attachment → Neo responde
  citando conteúdo lido por fatia; `file_stage` + `neo_bash wc -l` funciona;
  temp com TTL vencido some no tick do GC.
- **F2 — Extração e busca**: extract no sandbox (PDF/DOCX/XLSX), `pages.json`,
  `file_read(page=)`, `file_search`; libs na imagem do workbench.
  *Aceite:* PDF de 30 págs → `file_info` mostra 30, `file_read(page=12)` traz a
  página, `file_search` acha um termo; contexto total gasto ≪ tamanho do doc.
- **F3 — Panel**: aba Arquivos completa (upload, preview, pin, delete, quota).
- **F4 — Canais**: WhatsApp media inbound + anexo no desktop-tauri. A metade
  desktop tem plano de execução próprio (pronto p/ executar):
  `docs/file-store-f4-desktop-attachments-plan.md`.
- **F5 — URL + quota dura**: `file_from_url`; enforcement fino de quota;
  telemetria de acesso.
- **F6 — Auto-registro + saída**: artefatos de research/jobs auto-catalogados;
  "me manda esse arquivo" → link de download no chat / `sendFile` no WhatsApp;
  `file_stage` para novos targets (browser-agent).

## 14. Riscos / decisões em aberto

1. **Auth do upload pelo desktop** — confirmar na implementação qual guarda as
   rotas desktop usam hoje e replicar (não inventar esquema novo).
2. **Seam exato do manifesto** — `prompt_builder.py` vs `context_manager.py`;
   decidir vendo onde o turno do usuário é montado (citação a confirmar).
3. **Volume de mídia do WhatsApp** — auto-ingerir TODA imagem recebida pode
   inflar o store; começar só com `document` + imagem quando a mensagem é
   direcionada ao Neo, e medir.
4. **Dedup vs privacidade de sessão** — blob compartilhado entre sessões é ok
   (single-user hoje), mas registrar a decisão para o dia em que houver
   multiusuário.
5. **`neo_read_file`/`neo_write_file` seguem existindo** — são do rascunho
   `/workspace`; as `file_*` são do catálogo. Documentar a distinção nas
   descriptions para o modelo não confundir (mesma técnica "SEU workspace" vs
   "catálogo de arquivos").

---

## 15. Estado da implementação (F1 — 2026-07-16)

**Entregue e testado**: 45 testes em `tests/files/`; suíte inteira **884 passando**,
zero regressão. Tudo atrás de `NEO_FILES_ENABLED` (default off).

| Peça | Arquivo |
|------|---------|
| Model + migração | `db/models/stored_file.py`, alembic `n7o8p9q0r1s2` (head único) |
| Store | `core/files/store.py` — ingest bytes/stream/file c/ dedup sha256, `read_slice` (has_more/next_start_line + cap de chars), `stage`, `harvest`, `set_retention`, `delete`, `gc_pass` (refcount), `usage` (uso real: soma por sha DISTINTO), `manifest_line`, path-jail + anti-symlink |
| Stager | `core/files/stagers.py` — `WorkbenchStager` (cp de `/files/blobs/…` → `/workspace/files/` DENTRO do sandbox); interface plural pronta p/ novos targets |
| Tools | `core/files/tools.py` (6 tools) + `core/files/executor.py` |
| GC | `core/files/gc.py` — tick asyncio, cancelado no shutdown |
| Rotas | `api/routes/files.py` — upload/download/list/info/preview/patch/delete/usage |
| Wiring | `shared/config.py` (7 settings), `api/main.py` (tools+executor+router+GC task), `core/cognitive/read_tools.py` (gating), `modules/desktop/ai_service.py` (manifesto) |
| Infra | `docker-compose.yml` (volume `neo-files`; core rw `/data/files` + `neo-workspace:/workspace:ro`; workbench ro `/files`), `pyproject.toml` (+`python-multipart`) |

### Decisões que o plano deixou em aberto (§14) — resolvidas com evidência

1. **Auth do upload (§14.1)** — as rotas do desktop hoje **não têm guarda**
   (`app.include_router(desktop.router, prefix="/api/v1")`, sem `require_panel_jwt`).
   Replicado: upload/download abertos (mesmo nível do chat, que já é aberto);
   gestão do catálogo (list/info/preview/patch/delete/usage) com panel JWT por
   endpoint. Não inventamos esquema novo — se o desktop ganhar guarda, estas seguem.
2. **Seam do manifesto (§14.2)** — **não** é `prompt_builder.py` nem
   `context_manager.py`: é `ai_service.process_user_input` → `sys_context`, o
   mesmo ponto onde o `_LIVE_STYLE_INSTRUCTION` do modo Live é injetado; chega ao
   cognitivo via `system_context` de `_handle_direct_response`.
3. **Force-include das tools (§5)** — as 6 tools `file_*` entram no read set
   permanentemente (gated pela flag), porque `file_list` precisa existir sem anexo
   ("aquele CSV de ontem"). O que ATIVA o uso é o manifesto — force-include
   separado seria código morto.

### Gaps conhecidos da F1 (documentados, não esquecidos)

- **`user_id`/`session_id` em `file_save`**: as tools despacham pelo executor
  global (fall-through do `CognitiveReadExecutor`), que não thread-a contexto por
  request → gravam `None`. `FilesExecutor.set_context` já existe para quando o
  cognitivo criar executor fresco por request (padrão devices/finance). Mesmo
  trade-off da auditoria do gateway. Single-user hoje ⇒ sem impacto prático.
- **`line_count` só para arquivo ≤ 256 KB** (o probe não varre arquivo grande na
  ingestão); acima disso o campo fica ausente e o modelo pagina por `has_more`.
- **Binário sem extração** (é a F2): `file_read` recusa com mensagem que aponta
  para `file_stage` + `neo_bash`.

### Deploy (pendente — exige mais que salvar `.py`)

> ⚠️ **Já queimamos isto uma vez (2026-07-16):** `python-multipart` é dep NOVA no
> `pyproject.toml`. O Mutagen sincroniza o `.py` na hora e o `--reload` reimporta
> na hora, mas o entrypoint só instala deps no **boot do container** → o
> `api/routes/files.py` (que exige multipart já no decorator, mesmo com a flag
> off, porque o `api/main.py` importa o router no topo) derrubou o neo-core em
> produção ANTES de qualquer passo de deploy. Fix = recreate (o entrypoint
> detecta o pyproject e instala). **Regra: dep nova ⇒ recreate junto com o save,
> não "depois".**

Diferente das features anteriores, esta **não sobe só com o Mutagen/--reload**:
1. `alembic upgrade head` (cria `stored_file`);
2. `NEO_FILES_ENABLED=true` no `.env`;
3. **recreate do `neo-core` E do `neo-workbench`** (`docker compose up -d --no-deps
   neo-core neo-workbench`) — volume novo e mounts novos não entram por reload;
4. o entrypoint reinstala deps sozinho (pyproject mudou → `python-multipart`).
*Aceite (§13):* upload via curl → `file_id`; mensagem com attachment → Neo cita o
conteúdo lido por fatia; `file_stage` + `neo_bash wc -l`; temp vencido some no GC.

### Deploy realizado (2026-07-16) — aceite validado em produção

1. `docker exec -w /app neo-core alembic upgrade head` → `stored_file` criada
   (8 índices), `alembic_version = n7o8p9q0r1s2`.
2. `NEO_FILES_ENABLED=true` no `.env` (backup `.env.bak-files-20260716`).
3. `docker compose up -d --no-deps neo-core neo-workbench` (sandbox ocioso, zero
   jobs ativos — recreate seguro). Boot: `✅ File Store wired (root=/data/files,
   tools file_*)`, 62 tools registradas (era 56).

**Mounts conferidos dentro dos containers** (a garantia de integridade do §11):
`neo-core` → `/data/files` **RW**, `/workspace` **RO** (o core não escreve no
rascunho do sandbox); `neo-workbench` → `/files` legível e **RO** (o sandbox não
corrompe o store).

**Aceite da F1 (§13) exercitado ao vivo, com dado descartável e limpo depois:**

| Critério | Resultado |
|---|---|
| upload via curl → `file_id` | ✅ multipart real → id + sha256 + TTL 7d |
| leitura fracionada | ✅ `read_slice(start_line=2, max_lines=2)` → 2 linhas, `has_more=True`, `next_start_line=4` |
| `file_stage` + `neo_bash wc -l` | ✅ `/workspace/files/smoke-neo.csv` → `wc -l` = 5 (arquivo real, nunca passou pelo contexto) |
| temp vencido some no GC | ✅ `gc_pass` → `{'expired': 1, 'blobs_removed': 1}`; linha `expired`, blob unlinkado |

Limpeza pós-teste: cópia do sandbox removida, linha do catálogo deletada, store
com 0 blobs, `neo-core` healthy, API `/health` 200.

**Pendente de validação real:** o fluxo de anexo pelo CHAT (manifesto →
`file_info`/`file_read`) só será exercitado quando um cliente mandar
`metadata["attachments"]` — hoje nenhum cliente envia (é a F4: desktop-tauri e
WhatsApp). O caminho está testado em unidade (`tests/files/test_manifest.py`).
