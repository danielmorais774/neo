//! Claude Code nesta máquina — o corpo local do orquestrador do neo-core.
//!
//! O core manda `claude.*` pelo canal `/ws/devices/control` (o mesmo do
//! navegador local) e decide tudo; aqui só se lança, se entrega bytes do stream
//! e se para (neo-core/docs/claude-code-delegation-plan.md §4). O login é o que
//! o Daniel já fez no Claude Code dele: este arquivo nunca roda `auth login`,
//! `setup-token` nem `logout`.
//!
//! Esta é a última linha na máquina dele, como o `command_policy.rs` é para o
//! `run_command`:
//! - **confirmação** num diálogo nativo na primeira rodada de cada tarefa;
//! - **pasta** dentro do home, fora de `~/.ssh`, `~/Library` & cia.;
//! - **flags** só de uma allowlist — nada de pular permissão, nada de MCP.
//!   `--permission-prompts none` é obrigatório (ninguém responde prompt aqui; a
//!   rodada travaria). O modo padrão é `auto`, escolha do Daniel depois do job
//!   decc8224 (com `acceptEdits` até `python3 hello.py` virava pergunta); ele
//!   julga risco e não lugar (medido na F0: escreveu em `/tmp`), por isso o
//!   diálogo não promete mais "só nesta pasta".
//!
//! O processo sobe destacado (grupo próprio, saída em ARQUIVO): sobrevive ao
//! reinício do `tauri dev`, e o `claude.read` relê o stream de qualquer vida do app.

use serde_json::{json, Value};
use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::OnceLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::AppHandle;

/// Prefixo que o core lê como "o Daniel recusou" (não é falha).
const RECUSADO: &str = "RECUSADO:";
/// Teto de uma leitura do stream.
const LEITURA_MAX: u64 = 1024 * 1024;
/// O core espera o `claude.start` por 180 s. Um "Permitir" depois disso lançaria
/// uma rodada que ninguém acompanha: a confirmação vence antes.
const CONFIRMACAO_VALE: Duration = Duration::from_secs(170);
/// `claude.collect`: por arquivo e no total.
const ARQUIVO_MAX: u64 = 512 * 1024;
const RECOLHA_MAX: u64 = 2 * 1024 * 1024;

/// Pastas do home onde o Claude não trabalha, nem a pedido.
const PASTAS_PROIBIDAS: &[&str] = &[
    ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".config", ".claude", "Library",
];

/// Flags que o core pode mandar. Valor nunca começa com `-`, então o que
/// começa com `-` e não está aqui é recusado — inclusive escondido como valor.
const FLAGS_PERMITIDAS: &[&str] = &[
    "-p",
    "--print",
    "--output-format",
    "--verbose",
    "--session-id",
    "--resume",
    "--max-turns",
    "--model",
    "--fallback-model",
    "--effort",
    "--json-schema",
    "--append-system-prompt",
    "--strict-mcp-config",
    "--permission-mode",
    "--permission-prompts",
    "--allowedTools",
    "--allowed-tools",
    "--disallowedTools",
    "--disallowed-tools",
];

/// Nunca `bypassPermissions`: aqui é a máquina do Daniel, não um sandbox.
const MODOS_PERMITIDOS: &[&str] = &["auto", "acceptEdits", "default", "plan", "dontAsk"];

/// O device tem o Claude Code instalado?
pub fn available() -> bool {
    find_claude().is_some()
}

/// Executa uma ação `claude.*` do canal de controle.
pub async fn execute(app: &AppHandle, action: &str, args: &Value) -> Result<Value, String> {
    let a = args.clone();
    match action {
        "claude.health" => blocking(move || health(&a)).await,
        "claude.start" => start(app, args).await,
        "claude.read" => blocking(move || read(&a)).await,
        "claude.stop" => blocking(move || stop(&a)).await,
        "claude.list" => blocking(list).await,
        "claude.collect" => blocking(move || collect(&a)).await,
        "claude.cleanup" => blocking(move || cleanup(&a)).await,
        // As sessões do DANIEL (não as rodadas do Neo): services/claude_sessoes.rs.
        "claude.sessoes" => blocking(move || crate::services::claude_sessoes::listar(&a)).await,
        "claude.situacao" => blocking(move || crate::services::claude_sessoes::situacao(&a)).await,
        "claude.enviar" => blocking(move || enviar(&a)).await,
        _ => Err(format!("ação desconhecida: {}", action)),
    }
}

async fn blocking<F>(f: F) -> Result<Value, String>
where
    F: FnOnce() -> Result<Value, String> + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(f)
        .await
        .map_err(|e| format!("falha interna: {}", e))?
}

// ── o binário e o ambiente ──────────────────────────────────────────────

fn find_claude() -> Option<PathBuf> {
    let home = dirs::home_dir()?;
    let candidatos = [
        home.join(".local/bin/claude"),
        home.join(".claude/local/claude"),
        PathBuf::from("/opt/homebrew/bin/claude"),
        PathBuf::from("/usr/local/bin/claude"),
    ];
    if let Some(p) = candidatos.iter().find(|p| p.is_file()) {
        return Some(p.clone());
    }
    // App aberto fora do terminal não tem o PATH dele: pergunta ao shell de login.
    let saida = run_quick(
        Path::new("/bin/zsh"),
        &["-lc", "command -v claude"],
        Duration::from_secs(5),
    )
    .ok()?;
    let caminho = PathBuf::from(saida.1.trim());
    caminho.is_file().then_some(caminho)
}

/// O PATH do shell de login (homebrew, node, python do Daniel). Sem ele o Bash do
/// Claude, lançado de um app aberto pelo Finder, não acha nem o `python3` certo.
fn login_path() -> Option<&'static str> {
    static PATH: OnceLock<Option<String>> = OnceLock::new();
    PATH.get_or_init(|| {
        run_quick(
            Path::new("/bin/zsh"),
            &["-lc", "printf %s \"$PATH\""],
            Duration::from_secs(5),
        )
        .ok()
        .map(|(_, out, _)| out)
        .filter(|p| !p.trim().is_empty())
    })
    .as_deref()
}

/// Ambiente do `claude` filho: o do usuário, MENOS as variáveis de sessão do
/// Claude Code. Medido na F0: um terminal aberto de dentro do Claude Code carrega
/// mais de 20 delas (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`…), e um `tauri dev`
/// lançado dali passaria todas adiante.
fn preparar(cmd: &mut Command) {
    for (chave, _) in std::env::vars() {
        if chave.starts_with("CLAUDE") {
            cmd.env_remove(&chave);
        }
    }
    if let Some(path) = login_path() {
        cmd.env("PATH", path);
    }
}

/// Roda um comando curto com teto de tempo: `(código, stdout, stderr)`.
fn run_quick(prog: &Path, args: &[&str], teto: Duration) -> Result<(i32, String, String), String> {
    let mut cmd = Command::new(prog);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if prog.ends_with("claude") {
        preparar(&mut cmd);
    }
    let mut filho = cmd.spawn().map_err(|e| format!("falha ao rodar {}: {}", prog.display(), e))?;
    let inicio = Instant::now();
    loop {
        match filho.try_wait() {
            Ok(Some(status)) => {
                let mut out = String::new();
                let mut err = String::new();
                if let Some(mut s) = filho.stdout.take() {
                    let _ = s.read_to_string(&mut out);
                }
                if let Some(mut s) = filho.stderr.take() {
                    let _ = s.read_to_string(&mut err);
                }
                return Ok((status.code().unwrap_or(-1), out, err));
            }
            Ok(None) if inicio.elapsed() > teto => {
                let _ = filho.kill();
                let _ = filho.wait();
                return Err(format!("{} não respondeu em {}s", prog.display(), teto.as_secs()));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(e) => return Err(e.to_string()),
        }
    }
}

// ── saúde ────────────────────────────────────────────────────────────────

fn health(args: &Value) -> Result<Value, String> {
    // A pasta que o Neo quer usar, conferida ANTES de a tarefa nascer: no job
    // 21b91d36 o classificador inventou `~/Projetos/…` (nem `~/Projetos` existia)
    // e a tarefa morreu no `claude.start`, um turno depois.
    let pasta = args
        .get("pasta")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .map(conferir_pasta_pedida);
    let Some(claude) = find_claude() else {
        return Ok(json!({ "instalado": false, "pasta": pasta }));
    };
    let versao = run_quick(&claude, &["--version"], Duration::from_secs(15))
        .map(|(_, out, _)| out.trim().to_string())
        .unwrap_or_default();
    // Sem e-mail na resposta: o core só precisa saber SE está logado e COMO.
    let status = run_quick(&claude, &["auth", "status", "--json"], Duration::from_secs(15))?;
    let dados: Value = serde_json::from_str(status.1.trim()).unwrap_or(Value::Null);
    Ok(json!({
        "instalado": true,
        "versao": versao,
        "logado": dados.get("loggedIn").and_then(Value::as_bool).unwrap_or(false),
        "metodo": dados.get("authMethod").and_then(Value::as_str).unwrap_or(""),
        "plano": dados.get("subscriptionType").and_then(Value::as_str).unwrap_or(""),
        "pasta": pasta,
    }))
}

/// `{ok, caminho}` ou `{ok: false, motivo}`, pelas mesmas regras do `claude.start`
/// mas sem criar nada.
fn conferir_pasta_pedida(pedida: &str) -> Value {
    let Some(home) = dirs::home_dir() else {
        return json!({ "ok": false, "motivo": "sem home" });
    };
    let home = home.canonicalize().unwrap_or(home);
    let caminho = match pedida.strip_prefix("~/") {
        Some(resto) => home.join(resto),
        None => PathBuf::from(pedida),
    };
    let resultado = caminho
        .canonicalize()
        .map_err(|_| format!("a pasta não existe: {}", caminho.display()))
        .and_then(|real| conferir_pasta(&home, &real).map(|_| real));
    match resultado {
        Ok(real) => json!({ "ok": true, "caminho": real.display().to_string() }),
        Err(motivo) => json!({ "ok": false, "motivo": motivo }),
    }
}

// ── pastas ───────────────────────────────────────────────────────────────

fn pasta_das_rodadas() -> Result<PathBuf, String> {
    let base = dirs::data_dir().ok_or("sem pasta de dados do app")?;
    let pasta = base.join("neo-desktop").join("claude-runs");
    fs::create_dir_all(&pasta).map_err(|e| format!("não criei {}: {}", pasta.display(), e))?;
    Ok(pasta)
}

fn pasta_da_rodada(args: &Value) -> Result<PathBuf, String> {
    let run_id = args.get("run_id").and_then(Value::as_str).unwrap_or("");
    let valido = !run_id.is_empty()
        && run_id.len() <= 80
        && run_id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-');
    if !valido {
        return Err(format!("run_id inválido: {:?}", run_id));
    }
    Ok(pasta_das_rodadas()?.join(run_id))
}

/// A pasta de trabalho, resolvida e conferida. Vazia = pasta nova da tarefa.
fn pasta_de_trabalho(pedida: &str, tarefa: &str) -> Result<PathBuf, String> {
    let home = dirs::home_dir().ok_or("sem home")?;
    let home = home.canonicalize().unwrap_or(home);
    let bruta = pedida.trim();
    let caminho = if bruta.is_empty() {
        let curta: String = tarefa.chars().filter(|c| c.is_ascii_alphanumeric()).take(8).collect();
        let nova = home.join("Neo").join("claude").join(if curta.is_empty() { "tarefa".into() } else { curta });
        fs::create_dir_all(&nova).map_err(|e| format!("não criei {}: {}", nova.display(), e))?;
        nova
    } else if let Some(resto) = bruta.strip_prefix("~/") {
        home.join(resto)
    } else {
        PathBuf::from(bruta)
    };
    let real = caminho
        .canonicalize()
        .map_err(|_| format!("a pasta não existe: {}", caminho.display()))?;
    conferir_pasta(&home, &real)?;
    Ok(real)
}

fn conferir_pasta(home: &Path, real: &Path) -> Result<(), String> {
    if !real.is_dir() {
        return Err(format!("não é uma pasta: {}", real.display()));
    }
    if real == home || !real.starts_with(home) {
        return Err(format!(
            "o Claude só trabalha numa pasta DENTRO do seu home (não nele inteiro): {}",
            real.display()
        ));
    }
    for proibida in PASTAS_PROIBIDAS {
        if real.starts_with(home.join(proibida)) {
            return Err(format!("o Claude não trabalha em ~/{}", proibida));
        }
    }
    Ok(())
}

// ── flags ────────────────────────────────────────────────────────────────

fn conferir_flags(flags: &[String]) -> Result<Vec<String>, String> {
    let mut saida = Vec::with_capacity(flags.len() + 1);
    let mut i = 0;
    while i < flags.len() {
        let f = flags[i].as_str();
        if f.starts_with('-') {
            if !FLAGS_PERMITIDAS.contains(&f) {
                return Err(format!("flag recusada no Mac: {}", f));
            }
            let valor = flags.get(i + 1).map(String::as_str).unwrap_or("");
            if f == "--permission-mode" && !MODOS_PERMITIDOS.contains(&valor) {
                return Err(format!("modo de permissão recusado no Mac: {}", valor));
            }
            if f == "--permission-prompts" && valor != "none" {
                return Err("aqui ninguém responde prompt: só --permission-prompts none".into());
            }
        }
        saida.push(flags[i].clone());
        i += 1;
    }
    let tem = |nome: &str| saida.iter().any(|f| f == nome);
    if !tem("-p") && !tem("--print") {
        return Err("rodada sem -p".into());
    }
    if !tem("--permission-mode") || !tem("--permission-prompts") {
        return Err("rodada sem modo de permissão explícito".into());
    }
    if !tem("--strict-mcp-config") {
        // MCP e conectores (Gmail, Calendar) ficam de fora, mande o core ou não.
        saida.insert(1, "--strict-mcp-config".into());
    }
    Ok(saida)
}

// ── confirmação ──────────────────────────────────────────────────────────

fn arquivo_de_confirmadas() -> Result<PathBuf, String> {
    Ok(pasta_das_rodadas()?.join("confirmadas.json"))
}

fn confirmadas() -> HashSet<String> {
    arquivo_de_confirmadas()
        .ok()
        .and_then(|p| fs::read_to_string(p).ok())
        .and_then(|t| serde_json::from_str::<Vec<String>>(&t).ok())
        .unwrap_or_default()
        .into_iter()
        .collect()
}

fn guardar_confirmada(tarefa: &str) {
    let mut todas: Vec<String> = confirmadas().into_iter().collect();
    todas.push(tarefa.to_string());
    // Guarda as recentes: o que importa é a tarefa em curso e as continuações dela.
    if todas.len() > 200 {
        let corte = todas.len() - 200;
        todas.drain(..corte);
    }
    if let Ok(p) = arquivo_de_confirmadas() {
        let _ = fs::write(p, serde_json::to_string(&todas).unwrap_or_default());
    }
}

/// Diálogo nativo: quem decide é quem está na frente da máquina (o mesmo padrão
/// de desligar/suspender no `websocket.rs`).
async fn confirmar(app: &AppHandle, pasta: &Path, objetivo: &str) -> bool {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

    let objetivo: String = objetivo.chars().take(500).collect();
    let corpo = format!(
        "O Neo quer que o Claude Code trabalhe nesta pasta:\n{}\n\nTarefa: {}\n\n\
         Ele usa a sua conta do Claude, no modo automático: o que for seguro ele \
         faz sozinho, e o que for arriscado volta para você como pergunta no chat.",
        pasta.display(),
        objetivo
    );
    trazer_para_a_frente(app, &pasta.display().to_string());
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .message(corpo)
            .title("Rodar o Claude Code?")
            .buttons(MessageDialogButtons::OkCancelCustom(
                "Permitir".to_string(),
                "Recusar".to_string(),
            ))
            .blocking_show()
    })
    .await
    .unwrap_or(false)
}

/// O diálogo não tem janela-mãe, e um alerta de app em segundo plano abre ATRÁS
/// da janela que o Daniel está usando: no job 40852ae7 o pedido esperou 3 min
/// sem "Permitir" e a tarefa morreu. Traz a janela do Neo para a frente (o que
/// ativa o app) e avisa por notificação, para o caso de ele estar em outra mesa.
fn trazer_para_a_frente(app: &AppHandle, pasta: &str) {
    use tauri::Manager;

    if let Some(janela) = app.get_webview_window("main") {
        let _ = janela.unminimize();
        let _ = janela.show();
        let _ = janela.set_focus();
    }
    let aviso = json!({
        "kind": "claude_confirmar",
        "title": "O Claude quer começar",
        "description": format!("Permita no diálogo do Neo para ele trabalhar em {}.", pasta),
    });
    match serde_json::from_value::<crate::notifications::types::NeoNotification>(aviso) {
        Ok(notificacao) => crate::notifications::show_notification(&notificacao),
        Err(e) => log::warn!("[claude] aviso de confirmação não montou: {}", e),
    }
}

// ── ações ────────────────────────────────────────────────────────────────

async fn start(app: &AppHandle, args: &Value) -> Result<Value, String> {
    let rodada = pasta_da_rodada(args)?;
    let claude = find_claude().ok_or("o Claude Code não está instalado neste Mac")?;
    let pedido = args.get("confirmar").cloned().unwrap_or(Value::Null);
    let tarefa = pedido.get("tarefa").and_then(Value::as_str).unwrap_or("").to_string();
    if tarefa.is_empty() {
        return Err("rodada sem tarefa".into());
    }
    let pasta = pasta_de_trabalho(args.get("cwd").and_then(Value::as_str).unwrap_or(""), &tarefa)?;
    let flags: Vec<String> = args
        .get("flags")
        .and_then(Value::as_array)
        .map(|v| v.iter().filter_map(|f| f.as_str().map(String::from)).collect())
        .unwrap_or_default();
    let flags = conferir_flags(&flags)?;
    if let Some(sessao) = valor_da_flag(&flags, "--resume") {
        if let Some(onde) = onde_esta_aberta(&sessao) {
            // Duas cópias do Claude escrevendo na mesma sessão dividem o histórico
            // em dois ramos. O core entrega este texto ao Daniel como o motivo da falha.
            return Err(format!("a sessão está aberta {}. Feche o Claude lá e me peça de novo.", onde));
        }
    }

    if !confirmadas().contains(&tarefa) {
        let objetivo = pedido.get("objetivo").and_then(Value::as_str).unwrap_or("");
        let inicio = Instant::now();
        if !confirmar(app, &pasta, objetivo).await {
            return Err(format!("{} o Daniel recusou no Mac", RECUSADO));
        }
        if inicio.elapsed() > CONFIRMACAO_VALE {
            return Err("a confirmação chegou tarde demais; peça a tarefa de novo".into());
        }
        guardar_confirmada(&tarefa);
    }

    let prompt = args.get("prompt").and_then(Value::as_str).unwrap_or("").to_string();
    let run_id = args.get("run_id").and_then(Value::as_str).unwrap_or("").to_string();
    tauri::async_runtime::spawn_blocking(move || lancar(&claude, &rodada, &pasta, &run_id, &tarefa, &flags, &prompt))
        .await
        .map_err(|e| format!("falha interna: {}", e))?
}

fn lancar(
    claude: &Path,
    rodada: &Path,
    pasta: &Path,
    run_id: &str,
    tarefa: &str,
    flags: &[String],
    prompt: &str,
) -> Result<Value, String> {
    if rodada.exists() {
        return Err(format!("a rodada {} já existe", run_id));
    }
    fs::create_dir_all(rodada).map_err(|e| e.to_string())?;
    let prompt_md = rodada.join("prompt.md");
    fs::write(&prompt_md, prompt).map_err(|e| e.to_string())?;
    let stream = File::create(rodada.join("stream.jsonl")).map_err(|e| e.to_string())?;
    let erros = File::create(rodada.join("stderr.log")).map_err(|e| e.to_string())?;

    let mut cmd = Command::new(claude);
    // O prompt vai pelo stdin (`claude -p` lê dali): no argv ele apareceria no
    // `ps` e esbarraria no limite de tamanho.
    cmd.args(flags)
        .current_dir(pasta)
        .stdin(File::open(&prompt_md).map_err(|e| e.to_string())?)
        .stdout(stream)
        .stderr(erros);
    preparar(&mut cmd);
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let mut filho = cmd.spawn().map_err(|e| format!("não consegui iniciar o claude: {}", e))?;
    let pid = filho.id();
    let meta = json!({
        "run_id": run_id,
        "tarefa": tarefa,
        "pid": pid,
        "cwd": pasta.display().to_string(),
        "inicio": SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0),
    });
    fs::write(rodada.join("meta.json"), meta.to_string()).map_err(|e| e.to_string())?;

    // Colhe o filho (sem isto vira zumbi) e grava o código de saída. Se o app
    // morrer antes, o launchd colhe, e o `read` cai no `kill(pid, 0)`.
    let destino = rodada.join("exit_code");
    std::thread::spawn(move || {
        if let Ok(status) = filho.wait() {
            let codigo = status.code().unwrap_or(-1);
            let _ = fs::write(destino, codigo.to_string());
        }
    });
    log::info!("[claude] rodada {} lançada (pid {}) em {}", run_id, pid, pasta.display());
    Ok(json!({ "pid": pid, "cwd": pasta.display().to_string() }))
}

fn meta_de(rodada: &Path) -> Result<Value, String> {
    let texto = fs::read_to_string(rodada.join("meta.json")).map_err(|_| "rodada desconhecida".to_string())?;
    serde_json::from_str(&texto).map_err(|e| e.to_string())
}

/// `(vivo, código de saída)`. O pid só conta se ainda é um `claude`: depois de um
/// reinício do app o número pode ter sido reaproveitado.
fn situacao(rodada: &Path) -> (bool, Option<i64>) {
    if let Ok(texto) = fs::read_to_string(rodada.join("exit_code")) {
        return (false, texto.trim().parse().ok());
    }
    let pid = meta_de(rodada)
        .ok()
        .and_then(|m| m.get("pid").and_then(Value::as_i64))
        .unwrap_or(0);
    if pid <= 0 || unsafe { libc::kill(pid as i32, 0) } != 0 {
        return (false, None);
    }
    let nome = run_quick(Path::new("/bin/ps"), &["-p", &pid.to_string(), "-o", "comm="], Duration::from_secs(3))
        .map(|(_, out, _)| out)
        .unwrap_or_default();
    (nome.contains("claude"), None)
}

fn read(args: &Value) -> Result<Value, String> {
    let rodada = pasta_da_rodada(args)?;
    let offset = args.get("offset").and_then(Value::as_u64).unwrap_or(0);
    let teto = args
        .get("max_bytes")
        .and_then(Value::as_u64)
        .unwrap_or(256 * 1024)
        .min(LEITURA_MAX);
    // A situação ANTES de ler: morto antes da leitura = tudo que ele escreveu já
    // está no arquivo, e "morto e sem bytes novos" é fim de verdade.
    let (vivo, codigo) = situacao(&rodada);
    let mut arquivo = OpenOptions::new()
        .read(true)
        .open(rodada.join("stream.jsonl"))
        .map_err(|_| "rodada desconhecida".to_string())?;
    arquivo.seek(SeekFrom::Start(offset)).map_err(|e| e.to_string())?;
    let mut bytes = Vec::new();
    arquivo.take(teto).read_to_end(&mut bytes).map_err(|e| e.to_string())?;
    // Corta numa fronteira de caractere: o pedaço viaja como texto no JSON. A
    // linha cortada no meio o core guarda até a próxima leitura.
    let valido = match std::str::from_utf8(&bytes) {
        Ok(_) => bytes.len(),
        Err(e) => e.valid_up_to(),
    };
    let pedaco = String::from_utf8_lossy(&bytes[..valido]).to_string();
    Ok(json!({
        "chunk": pedaco,
        "offset": offset + valido as u64,
        "vivo": vivo,
        "exit_code": codigo,
    }))
}

fn stop(args: &Value) -> Result<Value, String> {
    let rodada = pasta_da_rodada(args)?;
    let (vivo, _) = situacao(&rodada);
    if !vivo {
        return Ok(json!({ "parado": true, "ja_estava": true }));
    }
    let pid = meta_de(&rodada)?.get("pid").and_then(Value::as_i64).unwrap_or(0) as i32;
    // O grupo inteiro (o claude e o que ele rodou), como o Stop do `shell.rs`.
    unsafe {
        libc::kill(-pid, libc::SIGTERM);
    }
    let inicio = Instant::now();
    while inicio.elapsed() < Duration::from_secs(5) {
        if unsafe { libc::kill(pid, 0) } != 0 {
            return Ok(json!({ "parado": true }));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
    }
    Ok(json!({ "parado": true, "forcado": true }))
}

fn list() -> Result<Value, String> {
    let mut rodadas = Vec::new();
    for entrada in fs::read_dir(pasta_das_rodadas()?).map_err(|e| e.to_string())?.flatten() {
        let caminho = entrada.path();
        if !caminho.is_dir() {
            continue;
        }
        let (vivo, codigo) = situacao(&caminho);
        let meta = meta_de(&caminho).unwrap_or(Value::Null);
        rodadas.push(json!({
            "run_id": entrada.file_name().to_string_lossy(),
            "vivo": vivo,
            "exit_code": codigo,
            "cwd": meta.get("cwd"),
            "tarefa": meta.get("tarefa"),
        }));
    }
    Ok(json!({ "rodadas": rodadas }))
}

/// Arquivos do relatório para o Neo trabalhar em cima. Só texto, só de dentro da
/// pasta da tarefa, com teto: o que sobe vai para a VPS.
fn collect(args: &Value) -> Result<Value, String> {
    let home = dirs::home_dir().ok_or("sem home")?;
    let home = home.canonicalize().unwrap_or(home);
    let pasta = PathBuf::from(args.get("cwd").and_then(Value::as_str).unwrap_or(""))
        .canonicalize()
        .map_err(|_| "a pasta da tarefa não existe mais".to_string())?;
    conferir_pasta(&home, &pasta)?;
    let mut total = 0u64;
    let mut arquivos = Vec::new();
    for item in args.get("paths").and_then(Value::as_array).cloned().unwrap_or_default() {
        let Some(pedido) = item.as_str() else { continue };
        let Ok(real) = pasta.join(pedido).canonicalize() else { continue };
        if !real.starts_with(&pasta) || !real.is_file() {
            continue;
        }
        if total >= RECOLHA_MAX {
            break;
        }
        let teto = ARQUIVO_MAX.min(RECOLHA_MAX - total);
        let tamanho = real.metadata().map(|m| m.len()).unwrap_or(0);
        let mut bytes = Vec::new();
        let Ok(arquivo) = File::open(&real) else { continue };
        let _ = arquivo.take(teto).read_to_end(&mut bytes);
        let valido = match std::str::from_utf8(&bytes) {
            Ok(_) => bytes.len(),
            // Erro antes do fim de um corte por teto = binário de verdade.
            Err(e) if e.error_len().is_some() => continue,
            Err(e) => e.valid_up_to(),
        };
        total += valido as u64;
        let relativo = real.strip_prefix(&pasta).unwrap_or(&real).display().to_string();
        arquivos.push(json!({
            "path": relativo,
            "nome": real.file_name().map(|n| n.to_string_lossy().to_string()),
            "conteudo": String::from_utf8_lossy(&bytes[..valido]),
            "tamanho": tamanho,
            "truncado": (valido as u64) < tamanho,
        }));
    }
    Ok(json!({ "files": arquivos }))
}

fn cleanup(args: &Value) -> Result<Value, String> {
    let rodada = pasta_da_rodada(args)?;
    if rodada.exists() {
        fs::remove_dir_all(&rodada).map_err(|e| e.to_string())?;
    }
    Ok(json!({ "limpo": true }))
}

// ── pedido numa sessão do Daniel ─────────────────────────────────────────

/// O mensageiro leva ~10 s (F0); o core espera `envio_timeout_s` (120 s).
const MENSAGEIRO_TETO: Duration = Duration::from_secs(100);
/// Um pedido é texto de conversa, não arquivo.
const PEDIDO_MAX: usize = 20_000;

/// `claude.enviar`: entrega o texto numa sessão do Daniel que está ABERTA (no
/// VSCode ou num terminal), e ela executa sozinha — o pedido chega ao vivo e
/// ninguém precisa apertar Enter (medido na F0). Quem entrega é o próprio Claude
/// Code: um `claude -p` mensageiro usa o SendMessage dele. O socket entre as
/// sessões é protocolo interno, com chave, e não é tocado daqui.
///
/// Fechada, não entrega: responde `sessao_fechada` com a pasta onde ela nasceu,
/// e o core a retoma pelo orquestrador (`--resume`, sem dividir histórico,
/// porque nenhum processo a segura).
fn enviar(args: &Value) -> Result<Value, String> {
    use crate::services::claude_sessoes;

    let sessao = uuid::Uuid::parse_str(args.get("sessionId").and_then(Value::as_str).unwrap_or("").trim())
        .map_err(|_| "sessão inválida".to_string())?
        .hyphenated()
        .to_string();
    let texto = args.get("texto").and_then(Value::as_str).unwrap_or("").trim();
    if texto.is_empty() {
        return Err("pedido vazio".into());
    }
    if texto.chars().count() > PEDIDO_MAX {
        return Err(format!("pedido grande demais ({} caracteres; o teto é {})", texto.chars().count(), PEDIDO_MAX));
    }
    let ficha = claude_sessoes::ficha_da_sessao(&sessao).ok_or("essa sessão do Claude não está neste Mac")?;
    if !ficha.viva {
        return Ok(json!({
            "entregue": false,
            "motivo": "sessao_fechada",
            "titulo": ficha.titulo,
            "pasta": ficha.pasta_inicial,
        }));
    }
    let nome = ficha.nome.clone().ok_or("a sessão aberta não tem nome no registro do Claude Code")?;
    let claude = find_claude().ok_or("o Claude Code não está instalado neste Mac")?;
    // Antes do mensageiro sair: a sessão pode terminar o pedido antes de ele
    // voltar, e o job compara com o `statusUpdatedAt` do registro, que é do Mac.
    let enviado_em = claude_sessoes::agora_ms();
    let marca = format!("=====NEO-{}=====", uuid::Uuid::new_v4().simple());
    let lido = rodar_mensageiro(&claude, &prompt_do_mensageiro(&nome, texto, &marca))?;
    if !lido.sucesso {
        log::warn!("[claude] mensageiro não entregou em {}: {}", nome, lido.detalhe);
        return Ok(json!({ "entregue": false, "motivo": "mensageiro", "erro": lido.detalhe }));
    }
    let igual = lido.para == nome && lido.mensagem.trim() == texto;
    if !igual {
        log::warn!("[claude] mensageiro entregou em {} um texto diferente do pedido", lido.para);
    }
    log::info!("[claude] pedido entregue na sessão {} ({})", nome, sessao);
    Ok(json!({
        "entregue": true,
        "nome": nome,
        "titulo": ficha.titulo,
        "status": ficha.status,
        "enviado_em": enviado_em,
        "texto_igual": igual,
    }))
}

fn prompt_do_mensageiro(nome: &str, texto: &str, marca: &str) -> String {
    format!(
        "Você é só um mensageiro. Faça exatamente isto e nada mais:\n\
         1. Chame a ferramenta SendMessage UMA vez, com `to` = \"{nome}\" e `message` = o texto \
         que está entre as duas linhas {marca}, copiado caractere por caractere, sem as linhas {marca}.\n\
         2. Não mude, não resuma, não traduza e não acrescente nada ao texto.\n\
         3. Depois responda só: ok\n\n{marca}\n{texto}\n{marca}\n"
    )
}

#[derive(Debug, Default)]
struct Mensageiro {
    para: String,
    mensagem: String,
    sucesso: bool,
    detalhe: String,
}

/// O `stream-json` do mensageiro: o `tool_use` do SendMessage (para quem e o
/// quê) e o `tool_result` dele (`"success":true`).
fn ler_mensageiro(stream: &str) -> Mensageiro {
    let mut m = Mensageiro::default();
    let mut id = String::new();
    for linha in stream.lines() {
        let Ok(d) = serde_json::from_str::<Value>(linha) else { continue };
        let itens = d.get("message").and_then(|x| x.get("content")).and_then(Value::as_array);
        for item in itens.into_iter().flatten() {
            let tipo = item.get("type").and_then(Value::as_str);
            if tipo == Some("tool_use") && item.get("name").and_then(Value::as_str) == Some("SendMessage") {
                id = item.get("id").and_then(Value::as_str).unwrap_or("").to_string();
                let entrada = item.get("input");
                let campo = |k: &str| entrada.and_then(|e| e.get(k)).and_then(Value::as_str).unwrap_or("").to_string();
                m.para = campo("to");
                m.mensagem = campo("message");
            }
            if tipo == Some("tool_result")
                && !id.is_empty()
                && item.get("tool_use_id").and_then(Value::as_str) == Some(id.as_str())
            {
                let resultado = match item.get("content") {
                    Some(Value::String(s)) => s.clone(),
                    Some(Value::Array(partes)) => partes
                        .iter()
                        .filter_map(|p| p.get("text").and_then(Value::as_str))
                        .collect::<Vec<_>>()
                        .join(" "),
                    _ => String::new(),
                };
                m.sucesso = resultado.contains("\"success\":true");
                m.detalhe = resultado.chars().take(300).collect();
            }
        }
    }
    if id.is_empty() {
        m.detalhe = "o mensageiro não chamou o SendMessage".into();
    }
    m
}

/// O `claude -p` mensageiro: só SendMessage e ListAgents, modo `default` (nunca
/// pular permissão: um remetente sem permissão é o caso em que a outra sessão
/// seguraria a mensagem), sem MCP e sem deixar transcrição. A saída vai para
/// arquivo, como na rodada: num pipe, um stream maior que o buffer travaria o
/// filho até o teto.
fn rodar_mensageiro(claude: &Path, prompt: &str) -> Result<Mensageiro, String> {
    let pasta = pasta_das_rodadas()?;
    let base = pasta.join(format!("mensageiro-{}", uuid::Uuid::new_v4().simple()));
    let entrada = base.with_extension("md");
    let saida = base.with_extension("jsonl");
    fs::write(&entrada, prompt).map_err(|e| e.to_string())?;
    let resultado = (|| {
        let mut cmd = Command::new(claude);
        cmd.args([
            "-p",
            "--no-session-persistence",
            "--model",
            "haiku",
            "--output-format",
            "stream-json",
            "--verbose",
            "--strict-mcp-config",
            "--permission-mode",
            "default",
            "--allowedTools",
            "SendMessage,ListAgents",
            "--disallowedTools",
            "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch",
        ])
        .current_dir(&pasta)
        .stdin(File::open(&entrada).map_err(|e| e.to_string())?)
        .stdout(File::create(&saida).map_err(|e| e.to_string())?)
        .stderr(Stdio::null());
        preparar(&mut cmd);
        let mut filho = cmd.spawn().map_err(|e| format!("não consegui iniciar o mensageiro: {}", e))?;
        let inicio = Instant::now();
        loop {
            match filho.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if inicio.elapsed() > MENSAGEIRO_TETO => {
                    let _ = filho.kill();
                    let _ = filho.wait();
                    return Err(format!("o mensageiro não terminou em {}s", MENSAGEIRO_TETO.as_secs()));
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                Err(e) => return Err(e.to_string()),
            }
        }
        Ok(ler_mensageiro(&fs::read_to_string(&saida).unwrap_or_default()))
    })();
    let _ = fs::remove_file(&entrada);
    let _ = fs::remove_file(&saida);
    resultado
}

// ── abrir no Terminal ────────────────────────────────────────────────────

/// Abre o Terminal do Mac em `cd <pasta> && claude --resume <sessão>`: o botão
/// "terminal" da faixa e do balão do chat. Quem pede é o clique do Daniel, mas a
/// pasta e a sessão vêm do core, então passam pelo mesmo crivo de uma rodada:
/// a sessão é um UUID que EXISTE no `~/.claude/projects` desta máquina, e a
/// pasta, o `conferir_pasta`. Nada do core vira texto de shell sem aspas.
///
/// Um `.command` aberto pelo Terminal, e não AppleScript: o `do script` pede a
/// permissão de Automação, e no `tauri dev` cada build é outra assinatura. O
/// arquivo se apaga ao rodar; quando o Claude sai, fica um shell na pasta.
pub fn abrir_no_terminal(cwd: &str, session_id: &str) -> Result<(), String> {
    let sessao = uuid::Uuid::parse_str(session_id.trim())
        .map_err(|_| format!("sessão inválida: {:?}", session_id))?
        .hyphenated()
        .to_string();
    let home = dirs::home_dir().ok_or("sem home")?;
    let home = home.canonicalize().unwrap_or(home);
    let pasta = Path::new(cwd.trim())
        .canonicalize()
        .map_err(|_| format!("a pasta da tarefa não existe mais: {}", cwd))?;
    conferir_pasta(&home, &pasta)?;
    if !sessao_existe(&home, &pasta, &sessao) {
        return Err("essa sessão do Claude não está nesta máquina".into());
    }
    let claude = find_claude().ok_or("não achei o Claude Code instalado")?;

    let script = pasta_das_rodadas()?.join(format!("terminal-{}.command", sessao));
    fs::write(&script, script_do_terminal(&pasta, &claude, &sessao))
        .map_err(|e| format!("não gravei {}: {}", script.display(), e))?;
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    }
    let (codigo, _, erro) = run_quick(
        Path::new("/usr/bin/open"),
        &["-a", "Terminal", &script.display().to_string()],
        Duration::from_secs(10),
    )?;
    if codigo != 0 {
        let _ = fs::remove_file(&script);
        return Err(format!("o Terminal não abriu: {}", erro.trim()));
    }
    log::info!("[claude] Terminal aberto na sessão {} em {}", sessao, pasta.display());
    Ok(())
}

/// O `~/.claude/projects/<pasta codificada>/<sessão>.jsonl` existe?
fn sessao_existe(home: &Path, pasta: &Path, sessao: &str) -> bool {
    let projetos = home.join(".claude").join("projects");
    crate::services::claude_sessoes::arquivo_da_sessao(&projetos, Some(&pasta.display().to_string()), sessao)
        .is_some()
}

fn script_do_terminal(pasta: &Path, claude: &Path, sessao: &str) -> String {
    format!(
        "#!/bin/zsh -l\nrm -f \"$0\"\ncd {} || exit 1\n{} --resume {}\nexec \"${{SHELL:-/bin/zsh}}\" -l\n",
        aspas(&pasta.display().to_string()),
        aspas(&claude.display().to_string()),
        sessao,
    )
}

fn valor_da_flag(flags: &[String], nome: &str) -> Option<String> {
    flags.iter().position(|f| f == nome).and_then(|i| flags.get(i + 1)).cloned()
}

/// Onde a sessão está aberta num Claude INTERATIVO, se estiver. O registro do
/// Claude Code é a fonte exata: o painel do VSCode roda com `--resume=<id>` e a
/// aba nova nem leva `--resume` (F0b), então o `ps` sozinho não via o VSCode.
/// Ele fica de reserva, para o processo que não está no registro.
fn onde_esta_aberta(sessao: &str) -> Option<&'static str> {
    if let Some(reg) = crate::services::claude_sessoes::sessao_viva(sessao) {
        return Some(match reg.entrypoint.as_deref() {
            Some("claude-vscode") => "no VSCode",
            _ => "num terminal do Mac",
        });
    }
    sessao_aberta_no_terminal(sessao).then_some("no Terminal do Mac (claude --resume)")
}

/// Algum Claude INTERATIVO está com esta sessão aberta (o botão "terminal", ou o
/// Daniel na mão)? As rodadas do Neo sempre levam `-p`; a interativa, não.
fn sessao_aberta_no_terminal(sessao: &str) -> bool {
    match run_quick(Path::new("/bin/ps"), &["-axww", "-o", "args="], Duration::from_secs(5)) {
        Ok((_, saida, _)) => saida.lines().any(|linha| linha_abre_sessao(linha, sessao)),
        Err(_) => false,
    }
}

fn linha_abre_sessao(linha: &str, sessao: &str) -> bool {
    let partes: Vec<&str> = linha.split_whitespace().collect();
    let retoma = partes.windows(2).any(|par| par[0] == "--resume" && par[1] == sessao)
        || partes.iter().any(|p| p.strip_prefix("--resume=") == Some(sessao));
    retoma && !partes.iter().any(|p| *p == "-p" || *p == "--print")
}

/// Aspas simples de shell: nada dentro delas é interpretado.
fn aspas(texto: &str) -> String {
    format!("'{}'", texto.replace('\'', "'\\''"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(flags: &[&str]) -> Vec<String> {
        flags.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn flags_do_core_passam_e_mcp_fica_de_fora_sempre() {
        let ok = conferir_flags(&v(&[
            "-p", "--output-format", "stream-json", "--verbose", "--session-id", "x",
            "--permission-mode", "acceptEdits", "--permission-prompts", "none",
            "--allowedTools", "Bash(npm install)",
        ]))
        .unwrap();
        assert!(ok.iter().any(|f| f == "--strict-mcp-config"));
    }

    #[test]
    fn pular_permissao_e_recusado_ate_escondido() {
        for ruim in [
            v(&["-p", "--dangerously-skip-permissions", "--permission-mode", "acceptEdits", "--permission-prompts", "none"]),
            v(&["-p", "--permission-mode", "bypassPermissions", "--permission-prompts", "none"]),
            v(&["-p", "--permission-mode", "acceptEdits", "--permission-prompts", "host"]),
            v(&["-p", "--mcp-config", "x.json", "--permission-mode", "acceptEdits", "--permission-prompts", "none"]),
            v(&["-p", "--allowedTools", "Bash", "--add-dir", "/", "--permission-mode", "acceptEdits", "--permission-prompts", "none"]),
            v(&["-p", "--output-format", "stream-json"]),
        ] {
            assert!(conferir_flags(&ruim).is_err(), "{:?}", ruim);
        }
    }

    #[test]
    fn terminal_so_abre_sessao_valida_e_tudo_vai_entre_aspas() {
        assert!(abrir_no_terminal("/tmp", "x; rm -rf ~").unwrap_err().contains("sessão inválida"));
        let home = dirs::home_dir().unwrap().canonicalize().unwrap();
        let pasta = home.join("Neo").join("claude").join(format!("teste-terminal-{}", std::process::id()));
        fs::create_dir_all(&pasta).unwrap();
        let sessao = uuid::Uuid::new_v4().to_string();
        let erro = abrir_no_terminal(&pasta.display().to_string(), &sessao).unwrap_err();
        assert!(erro.contains("não está nesta máquina"), "{erro}");
        assert!(abrir_no_terminal(&home.display().to_string(), &sessao).is_err());
        let _ = fs::remove_dir_all(&pasta);

        let script = script_do_terminal(Path::new("/Users/d/it's here"), Path::new("/x/claude"), &sessao);
        assert!(script.contains("cd '/Users/d/it'\\''s here' || exit 1\n"), "{script}");
        assert!(script.contains(&format!("'/x/claude' --resume {}\n", sessao)), "{script}");
        assert!(script.starts_with("#!/bin/zsh -l\nrm -f \"$0\"\n"));
    }

    #[test]
    fn so_o_claude_interativo_conta_como_sessao_aberta() {
        let s = "16126d81-d3d7-4908-a1ef-856967d60af4";
        assert!(linha_abre_sessao(&format!("/Users/d/.local/bin/claude --resume {s}"), s));
        // o painel do VSCode reaberto (F0b): stream-json sem -p, com `=`
        assert!(linha_abre_sessao(
            &format!("/x/native-binary/claude --output-format stream-json --input-format stream-json --resume={s} --setting-sources=user"),
            s
        ));
        // a rodada do próprio Neo (headless) não trava a retomada
        assert!(!linha_abre_sessao(&format!("claude -p --output-format stream-json --resume {s}"), s));
        // o script do botão e outra sessão também não
        assert!(!linha_abre_sessao(&format!("/bin/zsh -l /x/terminal-{s}.command"), s));
        assert!(!linha_abre_sessao("claude --resume 00000000-0000-0000-0000-000000000000", s));
        assert_eq!(
            valor_da_flag(&v(&["-p", "--resume", s, "--verbose"]), "--resume").as_deref(),
            Some(s)
        );
    }

    #[test]
    fn mensageiro_recebe_o_pedido_entre_marcas_e_le_a_entrega() {
        let p = prompt_do_mensageiro("neo-a4", "roda os testes\n\n(Pedido do Daniel)", "=====NEO-x=====");
        assert!(p.contains("`to` = \"neo-a4\""));
        assert!(p.ends_with("=====NEO-x=====\nroda os testes\n\n(Pedido do Daniel)\n=====NEO-x=====\n"), "{p}");

        // O stream do mensageiro da F0 (claude -p 2.1.319), enxuto.
        let stream = [
            r#"{"type":"system","subtype":"init","tools":["SendMessage"]}"#,
            r#"{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"ToolSearch","input":{"query":"select:SendMessage"}}]}}"#,
            r#"{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":[{"type":"tool_reference","tool_name":"SendMessage"}]}]}}"#,
            r#"{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t2","name":"SendMessage","input":{"to":"neo-a4","message":"roda os testes","summary":"x"}}]}}"#,
            r#"{"type":"user","message":{"content":[{"tool_use_id":"t2","type":"tool_result","content":[{"type":"text","text":"{\"success\":true,\"message\":\"→ neo-a4\"}"}]}]}}"#,
            r#"{"type":"result","subtype":"success","result":"ok"}"#,
        ]
        .join("\n");
        let m = ler_mensageiro(&stream);
        assert!(m.sucesso, "{m:?}");
        assert_eq!((m.para.as_str(), m.mensagem.as_str()), ("neo-a4", "roda os testes"));

        let sem_envio = ler_mensageiro(r#"{"type":"result","subtype":"success","result":"não sei"}"#);
        assert!(!sem_envio.sucesso && sem_envio.detalhe.contains("não chamou"));
        let recusado = stream.replace(r#"\"success\":true"#, r#"\"success\":false"#);
        assert!(!ler_mensageiro(&recusado).sucesso);
    }

    #[test]
    fn pasta_pedida_e_conferida_sem_criar_nada() {
        let inventada = format!("~/Projetos-que-nao-existem-{}/x", std::process::id());
        let r = conferir_pasta_pedida(&inventada);
        assert_eq!(r["ok"], false, "{r}");
        assert!(r["motivo"].as_str().unwrap().contains("não existe"), "{r}");
        assert!(!dirs::home_dir().unwrap().join(&inventada[2..]).exists());
        assert_eq!(conferir_pasta_pedida("/tmp")["ok"], false);
        let home = dirs::home_dir().unwrap().canonicalize().unwrap();
        let ok = conferir_pasta_pedida("~/Neo");
        if home.join("Neo").is_dir() {
            assert_eq!(ok["ok"], true, "{ok}");
        }
    }

    #[test]
    fn pasta_fora_do_home_ou_sensivel_e_recusada() {
        let home = dirs::home_dir().unwrap().canonicalize().unwrap();
        assert!(conferir_pasta(&home, &home).is_err());
        assert!(conferir_pasta(&home, Path::new("/tmp")).is_err());
        let ssh = home.join(".ssh");
        if ssh.is_dir() {
            assert!(conferir_pasta(&home, &ssh).is_err());
        }
    }

    /// Rodada de verdade, sem o app (o diálogo fica de fora): gasta um tiquinho
    /// da cota, então só com `cargo test -- --ignored rodada_real`.
    #[test]
    #[ignore]
    fn rodada_real_lanca_le_e_colhe() {
        let health = health(&json!({})).unwrap();
        assert_eq!(health["logado"], true, "{health}");
        let claude = find_claude().unwrap();
        let run_id = format!("teste-{}", std::process::id());
        let rodada = pasta_da_rodada(&json!({ "run_id": run_id })).unwrap();
        let pasta = pasta_de_trabalho("", &run_id).unwrap();
        let flags = conferir_flags(&v(&[
            "-p", "--output-format", "stream-json", "--verbose", "--model", "haiku",
            "--max-turns", "3", "--permission-mode", "acceptEdits", "--permission-prompts", "none",
        ]))
        .unwrap();
        lancar(&claude, &rodada, &pasta, &run_id, &run_id, &flags, "Crie oi.txt com o texto oi.").unwrap();
        let mut offset = 0u64;
        let mut stream = String::new();
        let inicio = Instant::now();
        loop {
            let r = read(&json!({ "run_id": run_id, "offset": offset, "max_bytes": 4096 })).unwrap();
            stream.push_str(r["chunk"].as_str().unwrap());
            offset = r["offset"].as_u64().unwrap();
            if r["vivo"] == false && r["chunk"] == "" {
                assert_eq!(r["exit_code"], 0, "{stream}");
                break;
            }
            assert!(inicio.elapsed() < Duration::from_secs(120), "{stream}");
            std::thread::sleep(Duration::from_millis(500));
        }
        assert!(stream.contains("\"type\":\"result\""), "{stream}");
        assert!(stream.contains("\"apiKeySource\":\"none\""), "não é a assinatura: {stream}");
        let recolhido = collect(&json!({ "cwd": pasta.display().to_string(), "paths": ["oi.txt", "../../.zshrc"] })).unwrap();
        let arquivos = recolhido["files"].as_array().unwrap();
        assert_eq!(arquivos.len(), 1, "{recolhido}");
        assert!(arquivos[0]["conteudo"].as_str().unwrap().contains("oi"));
        cleanup(&json!({ "run_id": run_id })).unwrap();
        assert!(!rodada.exists());
        let _ = fs::remove_dir_all(&pasta);
    }

    /// Entrega de verdade numa sessão DESCARTÁVEL e espera a resposta. Gasta um
    /// tiquinho da cota: `NEO_TESTE_SESSAO=<sessionId> cargo test -- --ignored
    /// envio_real --nocapture`. Sem a variável, não faz nada.
    #[test]
    #[ignore]
    fn envio_real_numa_sessao_de_teste() {
        let Ok(sessao) = std::env::var("NEO_TESTE_SESSAO") else { return };
        let inicio = Instant::now();
        let r = enviar(&json!({ "sessionId": sessao, "texto": "Teste do Neo: responda só OK-NEO-F2.\n\n(Não responda por mensagem: responda aqui mesmo.)" })).unwrap();
        println!("enviar em {:?}: {r}", inicio.elapsed());
        assert_eq!(r["entregue"], true, "{r}");
        assert_eq!(r["texto_igual"], true, "{r}");
        let desde = r["enviado_em"].as_i64().unwrap();
        loop {
            let s = crate::services::claude_sessoes::situacao(&json!({ "sessionId": sessao, "desde_ms": desde })).unwrap();
            let livre = s["status"] == "idle" && s["status_desde_ms"].as_i64().unwrap_or(0) > desde;
            if livre && s["resposta"].is_string() {
                println!("volta em {:?}: {s}", inicio.elapsed());
                assert!(s["resposta"].as_str().unwrap().contains("OK-NEO-F2"), "{s}");
                break;
            }
            assert!(inicio.elapsed() < Duration::from_secs(120), "{s}");
            std::thread::sleep(Duration::from_secs(2));
        }
    }

    #[test]
    fn run_id_so_aceita_nome_simples() {
        assert!(pasta_da_rodada(&json!({ "run_id": "../../etc" })).is_err());
        assert!(pasta_da_rodada(&json!({ "run_id": "" })).is_err());
        assert!(pasta_da_rodada(&json!({ "run_id": "abc-123-r1" })).is_ok());
    }
}
