# Plano de execução: F4-desktop — anexo de arquivo funcional no neo-desktop-tauri

> **Status: IMPLEMENTADO** (2026-07-16) — Etapas A–D concluídas; ver §"Estado da
> implementação" no fim. O core (Etapa A) está em produção; o desktop compila e
> roda no `tauri dev`. Falta só a passada de olho humana na GUI (clicar o clipe).
> **Objetivo:** o usuário clica no clipe (que já existe, decorativo) no chat do
> neo-desktop-tauri, escolhe um arquivo, ele sobe pro File Store (F1, já em
> produção), e a mensagem sai com `metadata["attachments"]` → o Neo recebe o
> manifesto e lê o arquivo por fatias (`file_info`/`file_read`). Fecha com um
> **teste E2E real**: anexar um `.txt` com um fato plantado e pedir análise.
> **Plano-mãe:** `docs/file-store-plan.md` (§5 fluxo de anexo, §15 estado da F1).
> Este documento é auto-contido — dá para executar sem reler o plano-mãe.

---

## 0. Contexto obrigatório (ler ANTES de tocar em qualquer arquivo)

1. **F1 está EM PRODUÇÃO** (`NEO_FILES_ENABLED=true` na VPS). Upload multipart,
   store, 6 tools `file_*` e manifesto já funcionam — validados por curl direto.
   O que NUNCA foi exercitado é o caminho **cliente → chat com anexo** (nenhum
   cliente envia `metadata["attachments"]` ainda). É exatamente este plano.
2. **Topologia dev = prod:** o neo-core "local" em `http://127.0.0.1:8000` é um
   TÚNEL para a VPS; Mutagen sincroniza e o core roda `--reload` → **salvar
   `.py` = deploy em produção**. Este plano não adiciona nenhuma dependência
   Python (se adicionasse, exigiria recreate do container — já quebrou prod uma
   vez). No lado Tauri: **NUNCA `tauri build`** — só `npm run tauri dev`.
3. **Teste com `.txt` de propósito:** na F1, `file_read` **recusa binário** (a
   extração de PDF/DOCX é a F2, não implementada). Texto puro é o caminho feliz.

### Mapa da cadeia de mensagem (verificado em 2026-07-16)

```
Chat.tsx (composer; clipe decorativo já existe em ~L317)
  └─ onSendMessage → ChatPage.tsx handleSendMessage (L288)
      └─ AgentService.executeWithLoop (src/lib/agentService.ts L2)
          └─ electronAPI.executeWithLoop (src/lib/tauriApi.ts L123/L189)
              └─ invoke('execute_with_loop') → src-tauri/src/commands/mod.rs L37
                  └─ run_agent_message (L56)
                      └─ GrpcClient::execute_task (src-tauri/src/services/ai_agent.rs L69;
                         metadata: HashMap<String,String> montado em L107-142)
                          └─ gRPC UserInput → neo-core grpc_server.py:158
                             metadata = dict(user_input.metadata)   ← TUDO string
                              └─ ai_service.process_user_input
                                  └─ _build_attachments_manifest (ai_service.py:591)
```

### Fatos verificados que o plano usa

| Fato | Onde |
|------|------|
| Upload F1: `POST /api/v1/files` multipart — campos `file`, `retention` ("temp" default), `origin` ("upload-desktop" default), `session_id?`, `user_id?`; **sem guarda de auth** (mesmo nível do chat) | `neo-core/api/routes/files.py:75-99` |
| Resposta do upload: `{"id": …, "name", "mime", "size_bytes", "sha256", "retention", "expires_at", …}` — o campo é **`id`**, não `file_id` | `files.py:53-70` (`_serialize`) |
| O manifesto aceita, na lista, id cru (string) OU objeto `{"file_id": …}` | `ai_service.py:604-607` |
| **Armadilha central:** metadata do gRPC é `map<string,string>` → `attachments` chega como **string JSON**, e `_build_attachments_manifest` hoje faz `isinstance(raw, list)` → **ignora string silenciosamente** (nenhum erro, nenhum manifesto) | `ai_service.py:601-603` |
| Precedente de metadata-como-JSON-string parseado no uso: `media_context` | `core/skills/music/service.py:112` (`parse_media_context`) |
| Base HTTP no Rust já existe: `neo_core_http_url()` (env `NEO_CORE_HTTP_URL`, default `http://127.0.0.1:8000`) | `src-tauri/src/commands/mod.rs:224-233` (há outra cópia em `lib.rs:51`) |
| `reqwest = 0.12` já é dep, **feature `json` só** — falta `multipart` | `src-tauri/Cargo.toml:30` |
| `tauri-plugin-dialog = "2"` já em Cargo.toml e inicializado (`lib.rs:147`); **o pacote JS `@tauri-apps/plugin-dialog` NÃO está no package.json** e `capabilities/default.json` não tem permissão de dialog → abrir o picker **pelo lado Rust** (`DialogExt`), sem npm dep e sem mexer em capability | verificado |
| **Colisão de assinatura:** `handleSendMessage(content, modeOverride?)` é compartilhado — o Modo Live chama com `"live"` no 2º arg (`ChatPage.tsx:314-316`); se o Chat passar anexos no 2º arg, viram `modeOverride` | `ChatPage.tsx:288-316` |
| AppState do Rust tem `session_id` (Mutex) e `user_id` — usar no form do upload | `commands/mod.rs:63-67` |

---

## Etapa A — neo-core: aceitar `attachments` como JSON string + log de observabilidade

**Arquivo:** `modules/desktop/ai_service.py`, `_build_attachments_manifest` (L591).

1. Depois de `raw = metadata.get("attachments") or []`, aceitar string:

```python
if isinstance(raw, str):
    # Clientes gRPC (map<string,string>) mandam a lista como JSON string —
    # mesmo padrão do media_context (core/skills/music/service.py).
    try:
        raw = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("[files] metadata.attachments não é JSON válido: %.80s", raw)
        return None
if not isinstance(raw, list) or not raw:
    return None
```

2. Quando o manifesto FOR injetado, logar (é o sinal do teste E2E nos logs):

```python
logger.info("[files] manifesto injetado: %d anexo(s)", len(lines))
```

3. **Teste unitário** em `tests/files/` (espelhar o teste existente do manifesto):
   um caso com `metadata={"attachments": '["<uuid>"]'}` (string JSON) esperando
   manifesto, e um com string inválida esperando `None` sem exceção.
4. Rodar a suíte de files: `pytest tests/files/ -q` (na raiz do neo-core; se a
   suíte roda via túnel/container, usar o mesmo jeito das execuções anteriores).
5. **Deploy = salvar o arquivo** (sem dep nova). Conferir no log do reload que o
   core subiu sem erro de import.

## Etapa B — Rust: picker + upload + anexos na cadeia do send

**B1. `src-tauri/Cargo.toml`:**
- `reqwest = { version = "0.12", features = ["json", "multipart"] }`
- Adicionar `mime_guess = "2"` (leve, sem transitivas pesadas) para o mime por
  extensão — o `.txt` deve subir como `text/plain`, não `octet-stream`.

**B2. `src-tauri/src/commands/mod.rs` — dois commands novos:**

```rust
/// Abre o picker nativo e devolve os paths escolhidos (vazio = cancelou).
#[tauri::command]
pub async fn pick_attachment_files(app: AppHandle) -> Result<Vec<String>, String> {
    // DialogExt (tauri_plugin_dialog). blocking_pick_files NÃO pode rodar na
    // main thread → spawn_blocking. Devolver FilePath::into_path() como String.
}

/// Lê o arquivo local e sobe pro File Store do neo-core. Devolve o JSON do
/// catálogo ({id, name, size_bytes, mime, expires_at, ...}).
#[tauri::command]
pub async fn upload_attachment(
    app: AppHandle,
    path: String,
) -> Result<serde_json::Value, String> {
    // 1. tokio::fs::metadata → recusar > 50 MB ANTES de ler (mensagem amigável;
    //    o servidor também recusa com 413, isto só evita ler 2GB à toa).
    // 2. tokio::fs::read(path).
    // 3. mime_guess::from_path(&path).first_or_octet_stream().
    // 4. reqwest multipart: part "file" (file_name = basename, mime_str),
    //    "retention"="temp", "origin"="upload-desktop",
    //    "session_id" = AppState.session_id (lock), "user_id" = AppState.user_id.
    // 5. POST {neo_core_http_url()}/api/v1/files; em !success(), extrair
    //    detail.message do corpo (o core devolve {"detail":{"error","message"}})
    //    e devolver Err legível.
}
```

**B3. Estender a cadeia do send** (mesmo shape do `mode`):
- `execute_with_loop` (L37) e `run_agent_message` (L56): novo param
  `attachments: Option<Vec<String>>`, repassado adiante.
- `GrpcClient::execute_task` (`ai_agent.rs:69`): novo param; junto dos outros
  inserts de metadata (L107-142):

```rust
// Ids do File Store anexados a ESTA mensagem. map<string,string> → JSON string;
// o core parseia em _build_attachments_manifest (docs/file-store-plan.md §5).
if let Some(ids) = attachments.filter(|v| !v.is_empty()) {
    metadata.insert(
        "attachments".to_string(),
        serde_json::to_string(&ids).unwrap_or_else(|_| "[]".into()),
    );
}
```

**B4. Registrar** `pick_attachment_files` e `upload_attachment` no
`generate_handler![…]` (`lib.rs:172`).

## Etapa C — React: chips de anexo + fiação até o invoke

**C1. `src/lib/tauriApi.ts`:**
- Interface `ElectronAPI`: adicionar
  `pickAttachmentFiles(): Promise<string[]>`,
  `uploadAttachment(path: string): Promise<StoredFileMeta>`, e estender
  `executeWithLoop(message, model?, mode?, provider?, attachments?: string[])`.
- Implementações: `invoke('pick_attachment_files')`,
  `invoke('upload_attachment', { path })`,
  e `invoke('execute_with_loop', { message, model, mode, provider, attachments })`.
  (Tauri v2 mapeia camelCase JS → snake_case Rust automaticamente — precedente:
  `resetAgent` passa `{ newSessionId }`.)
- Tipo `StoredFileMeta` mínimo: `{ id: string; name: string; size_bytes: number;
  mime: string | null; expires_at: string | null }`.

**C2. `src/lib/agentService.ts`:** `executeWithLoop` ganha `attachments?: string[]`
e repassa (pass-through, igual `provider`).

**C3. `src/components/Chat.tsx` (composer, ~L295-350):**
- Estado: `attachments: AttachmentChip[]` com
  `{ localPath, name, sizeBytes, status: 'uploading' | 'ready' | 'error', id?, error? }`.
- O botão clipe que JÁ EXISTE (`btn-attachment`, ~L317) ganha `onClick`:
  `pickAttachmentFiles()` → para cada path: chip `uploading` →
  `uploadAttachment(path)` → `ready` (guarda `id`) ou `error` (tooltip com a
  mensagem). Clique no ✕ do chip remove da lista (não deleta do store — é temp,
  o GC limpa).
- Renderizar a fileira de chips ACIMA do textarea, dentro do
  `form.input-container` (CSS novo em `Chat.css`, coerente com o glass style:
  pill pequena, nome truncado + tamanho humano, spinner em `uploading`, borda
  vermelha em `error`).
- `handleSubmit` (L197): manter a exigência de texto não-vazio (enviar SÓ anexo
  fica fora de escopo). **Send desabilitado enquanto houver chip `uploading`**
  (evita mandar a mensagem sem o manifesto do arquivo que ainda sobe). No
  submit: `onSendMessage(input.trim(), readyIds.length ? readyIds : undefined)`
  e limpar os chips.
- Prop: `onSendMessage: (message: string, attachmentIds?: string[]) => void` —
  os botões de sugestão (L246-252) continuam válidos (2º arg opcional).

**C4. `src/pages/ChatPage.tsx` — ⚠️ a colisão de assinatura:**
- `handleSendMessage(content, modeOverride?)` é usado pelo **Modo Live**
  (L314-316) com `"live"` no 2º argumento. NÃO passar `handleSendMessage`
  direto pro Chat depois da mudança.
- Fazer: `handleSendMessage(content: string, modeOverride?: string,
  attachmentIds?: string[])` chamando
  `AgentService.executeWithLoop(content, selectedModel, modeOverride ?? chatMode,
  selectedProvider, attachmentIds)`; e no `<Chat onSendMessage={...}>` (L432)
  passar o wrapper `(msg, ids) => handleSendMessage(msg, undefined, ids)`.
  O caminho do Live fica intocado.
- (Opcional, barato) `addMessage` do turno do usuário: prefixar
  `📎 nome.txt · ` no `content` exibido localmente — só apresentação; o texto
  enviado ao core NÃO muda. Se complicar, pular.

## Etapa D — Teste E2E (o "vamos testar" do pedido)

1. **Arquivo com fato plantado** (fora do repo, ex.: `~/Desktop/vendas-teste-neo.txt`):
   ~30 linhas tipo `regiao=Sul unidades=1893`, com UMA linha-agulha:
   `regiao=Norte unidades=4217`. O fato não pode ser adivinhável.
2. `cd neo-desktop-tauri && npm run tauri dev` (recompila o Rust; **nunca build**).
3. Clipe → escolher o txt → chip `uploading` → `ready`.
4. Enviar: *"analisa o arquivo que anexei e me diz quantas unidades a região
   Norte vendeu"*.
5. **Aceite (todos os 4):**
   - resposta cita **4217** (fato que só existe no arquivo);
   - log do core mostra `[files] manifesto injetado: 1 anexo(s)` e chamadas
     `file_info`/`file_read` no turno — verdade absoluta é o log do container
     (`ssh` na VPS → `docker logs neo-core --since 10m`), não a resposta do chat
     (lição do falso-sucesso da skill HA);
   - o arquivo aparece no catálogo: `docker exec neo-postgres psql -U neo -d
     neodb -c "select name, origin, retention, status, session_id is not null as
     tem_sessao from stored_file order by created_at desc limit 3"` — esperado
     `origin=upload-desktop`, `retention=temp`, `session_id` preenchido;
   - os chips somem após o send e o campo volta ao normal.
6. **Teste negativo:** tentar anexar um arquivo > 50 MB
   (`mkfile -n 60m /tmp/grande.bin` no mac) → chip `error` com mensagem legível,
   e o send continua funcionando depois de remover o chip.
7. **Limpeza:** nada a fazer — o upload é `temp` (TTL 7d, GC remove); se quiser,
   `DELETE /api/v1/files/{id}` com panel JWT.

## Fora de escopo (NÃO fazer neste plano)

- Drag-drop de arquivo na janela (Tauri `onDragDropEvent`) — iteração seguinte.
- Enviar mensagem só-com-anexo (sem texto).
- WhatsApp media inbound (a outra metade da F4).
- Aba Arquivos no neo-panel (F3) e extração de PDF/DOCX (F2).
- Renderização rica de anexos na timeline / re-download pelo chat.

## Ordem de execução e pontos de verificação

| # | O quê | Verifica com |
|---|-------|--------------|
| 1 | Etapa A (core) | `pytest tests/files/ -q` verde; reload do core sem erro |
| 2 | Etapa B (Rust) | `cargo check` dentro de `src-tauri/` compila |
| 3 | Etapa C (React) | `npm run tauri dev` sobe; clipe abre o picker; chip fica `ready` |
| 4 | Etapa D (E2E) | os 4 critérios de aceite + teste negativo |

**Riscos residuais** (se algo travar, olhar aqui primeiro):
- `blocking_pick_files` deadlocka se rodar na main thread → `spawn_blocking`.
- Se o manifesto não aparecer no log: conferir se o metadata chegou como string
  JSON (logar `metadata.keys()` no `grpc_server.py:158` temporariamente) e se a
  Etapa A realmente sincronizou (Mutagen) antes do teste.
- Se o upload der 503: `NEO_FILES_ENABLED` desligou em algum restart — conferir
  `.env` da VPS.

---

## Estado da implementação (2026-07-16)

Etapas A–D feitas. Suíte do core: **939 passando** (era 937; +2 do anexo), zero
regressão. Nenhuma dep Python nova → **deploy da Etapa A foi o próprio save**
(Mutagen + `--reload`), sem recreate.

| Etapa | Entregue |
|-------|----------|
| A — core | `modules/desktop/ai_service.py`: `_build_attachments_manifest` parseia `attachments` quando vem como **JSON string** (gRPC = `map<string,string>`), com `logger.warning` em JSON inválido e `logger.info("[files] manifesto injetado: N anexo(s)")` no caminho feliz. +2 testes em `tests/files/test_manifest.py` (string JSON válida; malformada e JSON de tipo errado → `None` sem raise) |
| B — Rust | `Cargo.toml`: reqwest ganha feature `multipart`, +`mime_guess = "2"`. `commands/mod.rs`: `pick_attachment_files` (picker nativo via `DialogExt` em `spawn_blocking`) e `upload_attachment` (guarda de 50 MB antes de ler o disco; multipart com session_id/user_id do `AppState`; extrai `detail.message` do erro do core). `attachments: Option<Vec<String>>` atravessa `execute_with_loop` → `run_agent_message` → `GrpcClient::execute_task`, virando JSON string no metadata. Handlers registrados em `lib.rs`; `notch_host.rs` passa `None` |
| C — React | `tauriApi.ts` (+`StoredFileMeta`, `pickAttachmentFiles`, `uploadAttachment`, `executeWithLoop` c/ anexos), `agentService.ts` (pass-through), `Chat.tsx` (chips uploading/ready/error, clipe funcional, send travado enquanto sobe), `Chat.css` (pills glass + spinner), `ChatPage.tsx` (wrapper que evita a colisão com o Modo Live) |
| D — E2E | validado contra **produção** (túnel :8000 + :50051) |

### Aceite E2E — evidência (2026-07-16)

Arquivo `~/Desktop/vendas-teste-neo.txt` (28 linhas) com a agulha
`mes=mar regiao=Norte unidades=4217` — fora do `preview_head` (que corta em 500
chars, no meio da palavra "Nor").

1. `POST /api/v1/files` → `id=a1f9d222-…`, `mime=text/plain`, `line_count=28`.
2. Turno gRPC mimetizando o Rust (`metadata["attachments"] = '["a1f9d222-…"]'`,
   script em scratchpad) → resposta: *"A região Norte só aparece uma vez: mar —
   **4.217 unidades**"*.
3. Log do container (`docker logs neo-core`): `[files] ingest a1f9d222-…
   (vendas-teste-neo.txt, 1004 bytes, origin=upload-desktop, retention=temp)` +
   `[files] manifesto injetado: 1 anexo(s)`.
4. Catálogo no Postgres: `access_count=1`, `last_accessed_at` preenchido.

**Por que isso prova leitura real (e não chute):** `FileStore.manifest_line`
(`core/files/store.py:651`) emite só `nome — mime, tamanho, N linhas, id=…` —
**zero conteúdo**. O número 4217 não existe em lugar nenhum do prompt; só podia
vir de `file_read`/`file_info`. O `access_count=1` no banco é a contraprova
persistente: o store serviu **uma** leitura para responder um arquivo de 28
linhas (o princípio de economia de tokens, funcionando).

### GUI confirmada em produção (session `a65830cf`, 2026-07-17)

O primeiro anexo real pela GUI funcionou de ponta a ponta: metadata chegou com
`"attachments": "[\"66eaf9a3-…\"]"` (JSON string do Rust), manifesto injetado
(`- form.html — text/html, 11 KB, 179 linhas, id=…`) e o catálogo gravou
`session_id` **e** `user_id` — o pendente do teste por curl, resolvido.

---

## Correções pós-primeiro-uso real (2026-07-17)

O log da `a65830cf` expôs dois defeitos que o teste por script não pegava. Ambos
corrigidos; suíte: **945 passando** (+6 testes).

### 🔴 Follow-up virava leitura de arquivo inventado no disco do usuário

Turno 2 (*"de qual instituição de ensino?"*, sem reanexar) foi classificado como
`CLIENT_TOOL` / `read_file` com `path=/Users/daniel/Downloads/form.html` —
**caminho que não existe** (o arquivo real estava em `~/Documents`), confiança
1.0, sem aprovação. O classificador confabulou o path a partir do nome que viu no
histórico.

Causa: o manifesto era montado só a partir de `metadata["attachments"]` **do
turno**, e os chips limpam após o envio (by design) → turno 2 sem manifesto.
Como o `sys_context` também alimenta o classificador, ele ficou cego. Mesma
família do entity_id inventado da skill HA e do misroute computação→`run_command`.

Fix em duas camadas:
1. **Anexos sticky por sessão** (`ai_service.py`) — `_session_attachments` (RAM,
   teto 10, mais recentes; padrão dos `_live_sessions`/`_session_registers`). O
   manifesto lista os do turno (`ARQUIVOS ANEXADOS NESTA MENSAGEM`) e os
   anteriores (`OUTROS ARQUIVOS DESTA CONVERSA`), separados.
2. **Regra dura no `ROUTER_SYSTEM_PROMPT`** (`intent_router.py`) — arquivo
   anexado está no catálogo do Neo; qualquer pergunta sobre ele é
   `DIRECT_RESPONSE`; nunca `read_file`; **nunca inventar caminho** a partir do
   nome. (Excluir as `file_*` da palette não bastava: o `read_file` continuava lá.)

### ⚠️ Respondia pelo preview, sem nunca ler

Turno 1: chamou `file_info` e descreveu um HTML de 179 linhas a partir dos ~500
chars do `preview_head` — *"Pelo início do conteúdo, **parece ser**…"*. Prova:
`access_count=0` no Postgres (só `read_slice`/`stage` chamam `_touch`).

Causa: o fecho do manifesto (*"Leia só o necessário — não peça o arquivo
inteiro"*) somado ao preview do `file_info` calibrou forte demais para o lado da
economia. Reescrito: o preview é **amostra**; proibido descrever/"parece ser" sem
`file_read`; "leia quantas fatias precisar, só não peça o arquivo inteiro de uma
vez".

### Aceite dos fixes (mesmos 2 turnos, mesmo arquivo, em prod)

| Sinal | Antes | Depois |
|-------|-------|--------|
| Manifesto no turno 2 | ausente | `0 anexo(s) neste turno, 1 da conversa` |
| Intent do turno 2 | `client_tool`/`read_file` em path inventado | `cognitive` (0.90) |
| `access_count` do form.html | **0** (nunca leu) | **3** (leu de verdade) |
| Resposta do turno 1 | "parece ser…", pelo preview | cita conteúdo além do preview (loader "Estamos preparando o ambiente…", `QUESTÃO {{vm.questaoAtualOrdem}}`) |
| Resposta do turno 2 | turno morria no `read_file` | "não aparecem referências a instituição no HTML lido" — **correto**: `grep -icE "institui\|escola\|faculdade\|universidade"` no arquivo = **0** |

### Resíduo conhecido (não corrigido)

No turno 2 o Neo leu em fatias, não achou a instituição e ofereceu *"posso ler
mais linhas… Confirma se é isso que quer"* em vez de terminar um arquivo de 179
linhas — e "Se quiser…" é justamente o que a persona proíbe. É calibragem de
comportamento, não bug: a resposta é honesta e ancorada no conteúdo real. Se
incomodar, o caminho é o manifesto mandar ler o arquivo inteiro quando
`line_count` for pequeno.
