//! Comando demorado que virou TAREFA em segundo plano nesta máquina.
//!
//! Antes, todo comando do Neo no Mac tinha 120 s de vida. Na sessão 1bbdd7c5 um
//! `zip -r` de 5,6 GB morreu aos 120 s com 2,6 GB escritos, e o temporário ficou
//! para trás. Esticar o prazo só trocava um problema por outro: o turno ficaria preso
//! meia hora em "Executando".
//!
//! Agora o comando que passa do orçamento de primeiro plano ainda trabalhando NÃO é
//! morto: ele sai do turno e continua aqui, com a saída indo para um log local. O
//! turno responde na hora ("está rodando, te aviso"), e o neo-core acompanha pelo
//! canal de controle até o fim, quando avisa no chat.
//!
//! O que mantém isto seguro:
//! - **Ninguém inicia tarefa por aqui.** Só vira tarefa o comando que JÁ passou pela
//!   política (`command_policy`) ou pelo card de aprovação, dentro de um turno. O
//!   canal de controle só lê o estado, lista e para: não existe `tarefa.start`.
//! - **Teto total** (padrão 2 h, máximo [`MAX_RUNTIME`]), contado do início do comando;
//!   estourou, o grupo inteiro de processos é encerrado.
//! - **No máximo [`MAX_RUNNING`] ao mesmo tempo.** Sem vaga, o comando fica em primeiro
//!   plano e morre no prazo, como antes.
//! - **Fechou o app, as tarefas morrem** ([`kill_all`], no `RunEvent::Exit`): nada roda
//!   sem o Neo aberto para acompanhar.
//! - **O log fica só neste disco**, com permissão 0600 numa pasta 0700, e é apagado
//!   depois de [`LOG_RETENTION`]. Ao servidor vai só o fim da saída, como em qualquer
//!   comando.
//!
//! Ver neo-core/core/jobs/runner_comando_local.py, o lado que acompanha.

use crate::services::shell::{self, Running};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime};

/// Quanto um comando roda em primeiro plano antes de virar tarefa, quando o
/// neo-core não diz outro valor (`background_after_s`, do `config/neo.toml`).
pub const DEFAULT_BACKGROUND_AFTER: Duration = Duration::from_secs(90);
/// Teto total de uma tarefa quando ninguém pede outro (`max_runtime_s`).
pub const DEFAULT_MAX_RUNTIME: Duration = Duration::from_secs(2 * 60 * 60);
/// Teto que nem o neo-core nem o modelo passam.
pub const MAX_RUNTIME: Duration = Duration::from_secs(6 * 60 * 60);
/// Tarefas rodando ao mesmo tempo.
pub const MAX_RUNNING: usize = 3;
/// Quanto tempo uma tarefa terminada continua consultável (o servidor pode ter
/// reiniciado e voltar para buscar o resultado).
const FINISHED_RETENTION: Duration = Duration::from_secs(24 * 60 * 60);
/// Idade a partir da qual o log de uma tarefa é apagado.
const LOG_RETENTION: Duration = Duration::from_secs(7 * 24 * 60 * 60);
/// Rabo da saída que vai no aviso de "virou tarefa".
const ADOPT_TAIL: usize = 4 * 1024;

/// O que o turno recebe quando o comando vira tarefa.
#[derive(Debug, Clone)]
pub struct Adopted {
    pub id: String,
    pub command: String,
    pub cwd: Option<String>,
    pub elapsed: Duration,
    pub max_runtime: Duration,
    pub tail: String,
    pub stderr_tail: String,
    pub log_path: Option<PathBuf>,
}

impl Adopted {
    /// O `result` do ToolResult. O neo-core reconhece a chave `neo_tarefa_local` e
    /// abre o job que acompanha a tarefa (core/jobs/runner_comando_local.py).
    pub fn marker(&self) -> String {
        json!({
            "neo_tarefa_local": {
                "id": self.id,
                "status": "running",
                "command": self.command,
                "cwd": self.cwd,
                "elapsed_s": self.elapsed.as_secs(),
                "max_runtime_s": self.max_runtime.as_secs(),
                "tail": self.tail,
                "stderr_tail": self.stderr_tail,
                "log_path": self.log_path.as_ref().map(|p| p.to_string_lossy().to_string()),
            }
        })
        .to_string()
    }
}

#[derive(Debug, Clone)]
enum State {
    Running,
    Exited {
        code: Option<i32>,
        signal: Option<i32>,
        ended_at: SystemTime,
        reason: Option<String>,
    },
}

struct Entry {
    id: String,
    command: String,
    cwd: Option<String>,
    pid: u32,
    started_at: SystemTime,
    max_runtime: Duration,
    shared: Arc<Mutex<shell::Streams>>,
    state: Arc<Mutex<State>>,
    /// Por que a tarefa foi encerrada por fora (parada, teto, app fechado).
    stop_reason: Arc<Mutex<Option<String>>>,
    log_path: Option<PathBuf>,
}

fn registry() -> &'static Mutex<HashMap<String, Entry>> {
    static REG: OnceLock<Mutex<HashMap<String, Entry>>> = OnceLock::new();
    REG.get_or_init(|| Mutex::new(HashMap::new()))
}

fn is_running(e: &Entry) -> bool {
    matches!(e.state.lock().map(|s| s.clone()), Ok(State::Running))
}

fn running_count() -> usize {
    registry()
        .lock()
        .map(|r| r.values().filter(|e| is_running(e)).count())
        .unwrap_or(MAX_RUNNING)
}

/// Ainda cabe uma tarefa?
pub fn has_room() -> bool {
    running_count() < MAX_RUNNING
}

fn log_dir() -> PathBuf {
    if cfg!(test) {
        return std::env::temp_dir().join("neo-tarefas-teste");
    }
    // Mesma base do conversation-history.json (commands/mod.rs).
    dirs::config_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("neo-desktop")
        .join("tarefas")
}

/// Apaga logs velhos, uma vez por execução do app.
fn purge_old_logs(dir: &Path) {
    static FEITO: OnceLock<()> = OnceLock::new();
    FEITO.get_or_init(|| {
        let Ok(itens) = std::fs::read_dir(dir) else { return };
        let agora = SystemTime::now();
        for item in itens.flatten() {
            let velho = item
                .metadata()
                .and_then(|m| m.modified())
                .ok()
                .and_then(|m| agora.duration_since(m).ok())
                .is_some_and(|idade| idade > LOG_RETENTION);
            if velho {
                let _ = std::fs::remove_file(item.path());
            }
        }
    });
}

fn open_log(id: &str) -> Option<(PathBuf, std::fs::File)> {
    let dir = log_dir();
    std::fs::create_dir_all(&dir).ok()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700));
    }
    purge_old_logs(&dir);
    let path = dir.join(format!("{}.log", id));
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    match opts.open(&path) {
        Ok(f) => Some((path, f)),
        Err(e) => {
            log::warn!("[tarefa] log {} não abriu: {}", path.display(), e);
            None
        }
    }
}

/// Esquece tarefas terminadas há mais de [`FINISHED_RETENTION`].
fn purge_finished(reg: &mut HashMap<String, Entry>) {
    let agora = SystemTime::now();
    reg.retain(|_, e| match e.state.lock().map(|s| s.clone()) {
        Ok(State::Exited { ended_at, .. }) => agora
            .duration_since(ended_at)
            .map(|d| d < FINISHED_RETENTION)
            .unwrap_or(true),
        _ => true,
    });
}

/// Transforma o comando em curso numa tarefa e devolve o que o turno precisa saber.
pub fn adopt(running: Running, command: &str, cwd: Option<&Path>, max_runtime: Duration) -> Adopted {
    let id = uuid::Uuid::new_v4().to_string();
    let max_runtime = max_runtime.min(MAX_RUNTIME).max(Duration::from_secs(1));
    let log_path = open_log(&id).and_then(|(path, file)| {
        running.shared.lock().ok()?.start_log(file);
        Some(path)
    });
    let elapsed = running.started.elapsed();
    let (tail, stderr_tail) = match running.shared.lock() {
        Ok(s) => (s.out.last(ADOPT_TAIL), s.err.last(ADOPT_TAIL / 4)),
        Err(_) => (String::new(), String::new()),
    };
    let state = Arc::new(Mutex::new(State::Running));
    let stop_reason = Arc::new(Mutex::new(None));
    let cwd_text = cwd.map(|p| p.to_string_lossy().to_string());
    let entry = Entry {
        id: id.clone(),
        command: command.to_string(),
        cwd: cwd_text.clone(),
        pid: running.pid,
        started_at: SystemTime::now()
            .checked_sub(elapsed)
            .unwrap_or_else(SystemTime::now),
        max_runtime,
        shared: running.shared.clone(),
        state: state.clone(),
        stop_reason: stop_reason.clone(),
        log_path: log_path.clone(),
    };
    if let Ok(mut reg) = registry().lock() {
        purge_finished(&mut reg);
        reg.insert(id.clone(), entry);
    }
    log::info!(
        "[tarefa] {} virou tarefa depois de {}s (teto {}s): {}",
        id,
        elapsed.as_secs(),
        max_runtime.as_secs(),
        command.chars().take(120).collect::<String>()
    );
    let tid = id.clone();
    let _ = std::thread::Builder::new()
        .name(format!("neo-tarefa-{}", &id[..8]))
        .spawn(move || watch(tid, running, state, stop_reason, max_runtime));
    Adopted {
        id,
        command: command.to_string(),
        cwd: cwd_text,
        elapsed,
        max_runtime,
        tail,
        stderr_tail,
        log_path,
    }
}

fn watch(
    id: String,
    mut running: Running,
    state: Arc<Mutex<State>>,
    stop_reason: Arc<Mutex<Option<String>>>,
    max_runtime: Duration,
) {
    let deadline = running.started + max_runtime;
    let status = loop {
        match running.child.try_wait() {
            Ok(Some(st)) => break Some(st),
            Ok(None) => {
                if Instant::now() >= deadline {
                    if let Ok(mut r) = stop_reason.lock() {
                        r.get_or_insert_with(|| {
                            format!(
                                "passou do teto de {} e foi encerrada",
                                duracao(max_runtime)
                            )
                        });
                    }
                    shell::kill_tree(running.pid);
                    break running.child.wait().ok();
                }
                std::thread::sleep(Duration::from_millis(200));
            }
            Err(_) => {
                shell::kill_tree(running.pid);
                break running.child.wait().ok();
            }
        }
    };
    let _ = running.collect();
    let (code, signal) = match status {
        Some(st) => (st.code(), exit_signal(&st)),
        None => (None, None),
    };
    let reason = stop_reason.lock().ok().and_then(|mut r| r.take());
    log::info!(
        "[tarefa] {} terminou: exit={:?} sinal={:?} motivo={:?}",
        id,
        code,
        signal,
        reason
    );
    if let Ok(mut s) = state.lock() {
        *s = State::Exited {
            code,
            signal,
            ended_at: SystemTime::now(),
            reason,
        };
    }
}

fn exit_signal(status: &std::process::ExitStatus) -> Option<i32> {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        status.signal()
    }
    #[cfg(not(unix))]
    {
        let _ = status;
        None
    }
}

fn duracao(d: Duration) -> String {
    let s = d.as_secs();
    if s >= 3600 && s % 3600 == 0 {
        format!("{} h", s / 3600)
    } else if s >= 60 {
        format!("{} min", s / 60)
    } else {
        format!("{} s", s)
    }
}

fn rfc3339(t: SystemTime) -> String {
    chrono::DateTime::<chrono::Utc>::from(t).to_rfc3339()
}

fn status_json(e: &Entry, tail_bytes: usize) -> Value {
    let state = e.state.lock().map(|s| s.clone()).unwrap_or(State::Running);
    let (out_total, err_total, tail, stderr_tail) = match e.shared.lock() {
        Ok(s) => (
            s.out.total(),
            s.err.total(),
            s.out.last(tail_bytes),
            s.err.last((tail_bytes / 4).max(512)),
        ),
        Err(_) => (0, 0, String::new(), String::new()),
    };
    let (status, alive, code, signal, ended_at, reason) = match &state {
        State::Running => ("running", true, None, None, None, None),
        State::Exited { code, signal, ended_at, reason } => {
            ("exited", false, *code, *signal, Some(*ended_at), reason.clone())
        }
    };
    let fim = ended_at.unwrap_or_else(SystemTime::now);
    let elapsed = fim.duration_since(e.started_at).unwrap_or_default();
    json!({
        "id": e.id,
        "command": e.command,
        "cwd": e.cwd,
        "status": status,
        "alive": alive,
        "exit_code": code,
        "signal": signal,
        "reason": reason,
        "elapsed_s": elapsed.as_secs(),
        "max_runtime_s": e.max_runtime.as_secs(),
        "started_at": rfc3339(e.started_at),
        "ended_at": ended_at.map(rfc3339),
        "bytes": out_total + err_total,
        "tail": tail,
        "stderr_tail": stderr_tail,
        "log_path": e.log_path.as_ref().map(|p| p.to_string_lossy().to_string()),
    })
}

/// Estado de uma tarefa.
pub fn status(id: &str, tail_bytes: usize) -> Result<Value, String> {
    let reg = registry().lock().map_err(|_| "registro de tarefas indisponível".to_string())?;
    let e = reg.get(id).ok_or_else(|| desconhecida(id))?;
    Ok(status_json(e, tail_bytes))
}

fn desconhecida(id: &str) -> String {
    // O registro mora na memória do app: reiniciar o Neo no Mac perde a lista (e o
    // fechamento já encerrou as tarefas). O core lê este texto como "não existe mais".
    format!("tarefa desconhecida neste Mac: {} (o app pode ter sido reiniciado)", id)
}

/// Para uma tarefa. O vigia registra o fim logo depois.
pub fn stop(id: &str, reason: &str) -> Result<Value, String> {
    let reg = registry().lock().map_err(|_| "registro de tarefas indisponível".to_string())?;
    let e = reg.get(id).ok_or_else(|| desconhecida(id))?;
    let vivo = is_running(e);
    if vivo {
        if let Ok(mut r) = e.stop_reason.lock() {
            r.get_or_insert_with(|| reason.to_string());
        }
        shell::kill_tree(e.pid);
    }
    Ok(json!({ "id": e.id, "stopping": vivo }))
}

/// Todas as tarefas que o app conhece.
pub fn list() -> Value {
    let mut itens: Vec<Value> = registry()
        .lock()
        .map(|reg| reg.values().map(|e| status_json(e, 512)).collect())
        .unwrap_or_default();
    itens.sort_by(|a, b| {
        a["started_at"].as_str().unwrap_or("").cmp(b["started_at"].as_str().unwrap_or(""))
    });
    json!({ "tarefas": itens })
}

/// Fechamento do app: nenhuma tarefa sobrevive sem o Neo para acompanhá-la.
pub fn kill_all() {
    let Ok(reg) = registry().lock() else { return };
    for e in reg.values().filter(|e| is_running(e)) {
        if let Ok(mut r) = e.stop_reason.lock() {
            r.get_or_insert_with(|| "o app do Neo foi fechado".to_string());
        }
        shell::kill_tree(e.pid);
    }
}

/// Ações `tarefa.*` do canal de controle (/ws/devices/control). Só leitura e parada:
/// iniciar tarefa por aqui não existe, de propósito.
pub fn execute(action: &str, args: &Value) -> Result<Value, String> {
    let id = || {
        args.get("id")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| "falta o id da tarefa".to_string())
    };
    match action {
        "tarefa.status" => {
            let tail = args
                .get("tail_bytes")
                .and_then(|v| v.as_u64())
                .unwrap_or(8 * 1024)
                .clamp(512, 64 * 1024) as usize;
            status(id()?, tail)
        }
        "tarefa.stop" => stop(id()?, "parada pelo Neo"),
        "tarefa.list" => Ok(list()),
        other => Err(format!("ação de tarefa desconhecida: {}", other)),
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::services::shell::{run_limited, Limits, Ran};

    fn limits(background_ms: u64, max_ms: u64) -> Limits {
        Limits {
            timeout: Duration::from_secs(30),
            background_after: Some(Duration::from_millis(background_ms)),
            max_runtime: Duration::from_millis(max_ms),
        }
    }

    fn adopt_cmd(cmd: &str, l: Limits) -> Adopted {
        match run_limited(cmd, None, l).expect("shell") {
            Ran::Background(a) => a,
            Ran::Done(o) => panic!("devia virar tarefa, terminou: {:?}", o),
        }
    }

    fn esperar_fim(id: &str) -> Value {
        let t0 = Instant::now();
        loop {
            let s = status(id, 4096).unwrap();
            if s["status"] == "exited" {
                return s;
            }
            assert!(t0.elapsed() < Duration::from_secs(15), "tarefa não terminou: {s}");
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    #[test]
    fn comando_lento_vira_tarefa_e_termina_com_a_saida() {
        let a = adopt_cmd("echo comeco; sleep 0.6; echo fim-da-tarefa", limits(150, 20_000));
        assert!(a.tail.contains("comeco"), "{:?}", a.tail);
        assert!(a.marker().contains("neo_tarefa_local"));
        let s = esperar_fim(&a.id);
        assert_eq!(s["exit_code"], 0);
        assert!(s["tail"].as_str().unwrap().contains("fim-da-tarefa"), "{s}");
        // O log local tem a saída inteira, com permissão só do dono.
        let log = PathBuf::from(s["log_path"].as_str().unwrap());
        let corpo = std::fs::read_to_string(&log).unwrap();
        assert!(corpo.contains("comeco") && corpo.contains("fim-da-tarefa"), "{corpo}");
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&log).unwrap().permissions().mode() & 0o777, 0o600);
    }

    #[test]
    fn comando_rapido_nao_vira_tarefa() {
        match run_limited("echo rapido", None, limits(2_000, 20_000)).unwrap() {
            Ran::Done(o) => assert!(String::from_utf8_lossy(&o.stdout).contains("rapido")),
            Ran::Background(a) => panic!("virou tarefa à toa: {}", a.id),
        }
    }

    #[test]
    fn parar_encerra_o_grupo_e_diz_o_motivo() {
        let a = adopt_cmd("sleep 30", limits(100, 60_000));
        let r = stop(&a.id, "parada pelo usuário").unwrap();
        assert_eq!(r["stopping"], true);
        let s = esperar_fim(&a.id);
        assert_eq!(s["reason"], "parada pelo usuário");
        assert!(s["elapsed_s"].as_u64().unwrap() < 10);
    }

    #[test]
    fn teto_total_encerra_a_tarefa() {
        let a = adopt_cmd("sleep 30", limits(100, 700));
        let s = esperar_fim(&a.id);
        assert!(s["reason"].as_str().unwrap().contains("teto"), "{s}");
    }

    #[test]
    fn timeout_explicito_menor_que_o_orcamento_nunca_adota() {
        let l = Limits {
            timeout: Duration::from_millis(300),
            background_after: Some(Duration::from_secs(5)),
            max_runtime: Duration::from_secs(60),
        };
        match run_limited("sleep 20", None, l).unwrap() {
            Ran::Done(o) => {
                assert!(String::from_utf8_lossy(&o.stderr).contains("tempo limite"));
            }
            Ran::Background(a) => panic!("adotou um comando com prazo explícito: {}", a.id),
        }
    }

    #[test]
    fn canal_de_controle_nao_inicia_nada() {
        assert!(execute("tarefa.start", &json!({"command": "echo x"})).is_err());
        assert!(execute("tarefa.status", &json!({"id": "nao-existe"}))
            .unwrap_err()
            .contains("desconhecida"));
        assert!(execute("tarefa.status", &json!({})).is_err());
        assert!(execute("tarefa.list", &json!({})).unwrap()["tarefas"].is_array());
    }
}
