//! Execução de linha de comando no shell nativo da plataforma.
//!
//! Os dois executores do app estavam fixos em `/bin/bash`. No Windows esse caminho
//! não existe, então TODO comando falharia com "program not found" antes de rodar —
//! inclusive os comandos de PowerShell que o neo-core passou a gerar corretamente
//! depois que o bloco de ambiente aprendeu a diferenciar as plataformas
//! (`_build_shell_rules`). Prompt certo com executor errado é o mesmo bug, só
//! deslocado de camada.
//!
//! No Linux `/bin/bash` existe na maioria das distros mas não é garantido (Alpine
//! usa busybox `sh`, NixOS não tem `/bin/bash`), e `/bin/sh` sozinho seria dash —
//! que não entende bashismo (`[[ ]]`, arrays), justamente o que o prompt autoriza o
//! modelo a escrever ao dizer "Shell padrão: bash". Daí a lista de candidatos com
//! fallback em vez de um caminho único.
//!
//! Três coisas que o executor não fazia e o schema do `run_command` prometia
//! (análise de 2026-09-15):
//! - **pasta padrão = home.** Sem `working_dir` o comando herdava o cwd do app, que
//!   no `tauri dev` é `neo-desktop-tauri/src-tauri` — um `rm` relativo cairia no
//!   código-fonte;
//! - **timeout.** `Command::output()` esperava para sempre; um comando parado
//!   segurava o turno até o teto de 1h;
//! - **Parar mata o processo.** O Stop só derrubava o stream; o comando seguia
//!   rodando. Agora cada comando roda no próprio grupo e o Stop encerra o grupo.

use std::collections::HashSet;
use std::io::{ErrorKind, Read};
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Output, Stdio};
use std::sync::mpsc;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

/// Tempo de um comando quando o chamador não diz outro.
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(120);
/// Teto do `timeout` que o modelo pode pedir no `run_command`.
pub const MAX_TIMEOUT: Duration = Duration::from_secs(30 * 60);
/// Bytes guardados por fluxo. O resto é lido e descartado — parar de ler travaria o
/// processo no pipe cheio.
const OUTPUT_CAP: usize = 4 * 1024 * 1024;
/// Depois que o shell sai, quanto esperar um filho em background soltar o pipe.
const DRAIN_GRACE: Duration = Duration::from_secs(2);

/// Shells a tentar, em ordem de preferência, para a plataforma compilada.
///
/// `std::env::consts::OS` / `cfg!` são resolvidos em tempo de compilação — cada build
/// nativo carrega só a sua lista, que é o comportamento desejado: um binário Windows
/// nunca deve tentar bash.
fn candidates() -> Vec<(&'static str, &'static [&'static str])> {
    if cfg!(target_os = "windows") {
        // pwsh (PowerShell 7+) quando instalado; powershell 5.1 é o que vem de fábrica.
        // -NonInteractive impede que um prompt de confirmação pendure o processo, já que
        // ninguém está lendo o stdin deste shell.
        vec![
            ("pwsh", &["-NoProfile", "-NonInteractive", "-Command"][..]),
            ("powershell", &["-NoProfile", "-NonInteractive", "-Command"][..]),
        ]
    } else if cfg!(target_os = "macos") {
        // bash 3.2 do sistema — mantido como era para o comportamento no Mac não mudar.
        vec![("/bin/bash", &["-c"][..])]
    } else {
        // Resolvido pelo PATH (não caminho absoluto): cobre /usr/bin/bash do NixOS.
        vec![("bash", &["-c"][..]), ("/bin/sh", &["-c"][..])]
    }
}

fn running() -> &'static Mutex<HashSet<u32>> {
    static RUNNING: OnceLock<Mutex<HashSet<u32>>> = OnceLock::new();
    RUNNING.get_or_init(|| Mutex::new(HashSet::new()))
}

/// Marca um processo (líder de grupo) como "do turno": o Parar o encerra.
/// Público porque o opencode (services/file_ops.rs) entra na mesma lista.
pub fn track_pid(pid: u32) {
    if let Ok(mut set) = running().lock() {
        set.insert(pid);
    }
}

pub fn untrack_pid(pid: u32) {
    if let Ok(mut set) = running().lock() {
        set.remove(&pid);
    }
}

/// Encerra todo comando ainda rodando (botão Parar). Devolve quantos eram.
pub fn kill_running() -> usize {
    let pids: Vec<u32> = running()
        .lock()
        .map(|set| set.iter().copied().collect())
        .unwrap_or_default();
    for pid in &pids {
        kill_tree(*pid);
    }
    pids.len()
}

/// Mata o grupo inteiro (o shell E o que ele abriu).
pub fn kill_tree(pid: u32) {
    #[cfg(unix)]
    {
        // SAFETY: killpg só envia sinal; um pid que já morreu devolve ESRCH.
        unsafe {
            libc::killpg(pid as libc::pid_t, libc::SIGKILL);
        }
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .output();
    }
}

/// Roda `command` no primeiro shell disponível da plataforma e devolve a saída bruta.
///
/// Só cai para o próximo candidato quando o shell em si não existe (`NotFound`); erro
/// do comando executado é resultado legítimo e volta no `Output`, não vira retry.
pub fn run(command: &str) -> Result<Output, String> {
    run_with(command, None, DEFAULT_TIMEOUT)
}

/// Igual a [`run`], mas executando dentro de `working_dir` quando informado.
///
/// Os passos de plano do neo-core mandam `{"command": ..., "working_dir": ...}`; sem
/// honrar o diretório, um comando com caminho relativo roda no cwd do app e devolve a
/// saída de outra pasta — errada, mas plausível, que é o pior tipo de resultado.
#[cfg_attr(not(test), allow(dead_code))]
pub fn run_in(command: &str, working_dir: Option<&str>) -> Result<Output, String> {
    run_with(command, working_dir, DEFAULT_TIMEOUT)
}

/// [`run_in`] com prazo. Estourado, o grupo de processos é encerrado e o `Output`
/// volta como falha, com a explicação no stderr.
pub fn run_with(
    command: &str,
    working_dir: Option<&str>,
    timeout: Duration,
) -> Result<Output, String> {
    let dir: Option<PathBuf> = match working_dir.map(str::trim).filter(|d| !d.is_empty()) {
        Some(d) => {
            let p = PathBuf::from(d);
            // Pasta inexistente vira `NotFound` no spawn — o mesmo erro de "shell não
            // existe", o que faria o fallback culpar o shell. Checar antes.
            if !p.is_dir() {
                return Err(format!("a pasta de trabalho não existe: {}", d));
            }
            Some(p)
        }
        None => dirs::home_dir(),
    };
    let timeout = timeout.min(MAX_TIMEOUT).max(Duration::from_millis(1));

    let shells = candidates();
    let mut last_err = String::new();

    for (program, args) in &shells {
        let mut cmd = Command::new(program);
        cmd.args(*args)
            .arg(command)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(d) = &dir {
            cmd.current_dir(d);
        }
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0);
        }
        match cmd.spawn() {
            Ok(child) => return Ok(wait_bounded(child, timeout)),
            Err(e) if e.kind() == ErrorKind::NotFound => {
                last_err = format!("shell '{}' não encontrado", program);
                continue;
            }
            Err(e) => return Err(format!("falha ao executar via '{}': {}", program, e)),
        }
    }

    Err(if last_err.is_empty() {
        "nenhum shell disponível nesta plataforma".to_string()
    } else {
        format!(
            "{} (tentados: {})",
            last_err,
            shells
                .iter()
                .map(|(p, _)| *p)
                .collect::<Vec<_>>()
                .join(", ")
        )
    })
}

fn drain<R: Read + Send + 'static>(mut src: R) -> mpsc::Receiver<Vec<u8>> {
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut kept = Vec::new();
        let mut buf = [0u8; 8192];
        let mut dropped = false;
        loop {
            match src.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    let room = OUTPUT_CAP.saturating_sub(kept.len());
                    kept.extend_from_slice(&buf[..n.min(room)]);
                    dropped |= n > room;
                }
            }
        }
        if dropped {
            kept.extend_from_slice(b"\n[neo] saida cortada em 4 MB");
        }
        let _ = tx.send(kept);
    });
    rx
}

fn failed_status() -> ExitStatus {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        ExitStatus::from_raw(9)
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::ExitStatusExt;
        ExitStatus::from_raw(1)
    }
}

fn wait_bounded(mut child: Child, timeout: Duration) -> Output {
    let pid = child.id();
    track_pid(pid);
    let out_rx = child.stdout.take().map(drain);
    let err_rx = child.stderr.take().map(drain);

    let deadline = Instant::now() + timeout;
    let mut timed_out = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) if Instant::now() >= deadline => {
                timed_out = true;
                kill_tree(pid);
                break child.wait().unwrap_or_else(|_| failed_status());
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(_) => {
                kill_tree(pid);
                break child.wait().unwrap_or_else(|_| failed_status());
            }
        }
    };

    // Um filho em background (`cmd &`) herda o pipe e o mantém aberto depois que o
    // shell sai. Espera um pouco; se não soltar, encerra o grupo para liberar.
    let collect = |rx: Option<mpsc::Receiver<Vec<u8>>>| -> Vec<u8> {
        let Some(rx) = rx else { return Vec::new() };
        match rx.recv_timeout(DRAIN_GRACE) {
            Ok(v) => v,
            Err(_) => {
                kill_tree(pid);
                rx.recv_timeout(DRAIN_GRACE).unwrap_or_default()
            }
        }
    };
    let stdout = collect(out_rx);
    let mut stderr = collect(err_rx);
    untrack_pid(pid);

    if timed_out {
        stderr.extend_from_slice(
            format!(
                "\n[neo] tempo limite de {}s estourado — o processo foi encerrado",
                timeout.as_secs().max(1)
            )
            .as_bytes(),
        );
    }
    Output { status, stdout, stderr }
}

/// Separa `Output` em `(success, stdout_ou_stderr, erro)` — a forma que os dois
/// chamadores usam para montar ToolResult/CommandResult.
///
/// Na falha o erro leva os DOIS fluxos e o código de saída. Antes só o stderr
/// voltava e o código sumia: um `grep` sem resultado (exit 1, nada no stderr) chegava
/// ao Neo como erro vazio, e um script que imprime o diagnóstico no stdout perdia o
/// diagnóstico.
pub fn split_output(output: Output) -> (bool, String, String) {
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    if output.status.success() {
        let text = if stdout.is_empty() { stderr } else { stdout };
        return (true, text, String::new());
    }
    let mut text = match (stdout.trim().is_empty(), stderr.trim().is_empty()) {
        (true, true) => String::new(),
        (false, true) => stdout,
        (true, false) => stderr,
        (false, false) => format!("{}\n--- stdout ---\n{}", stderr.trim_end(), stdout.trim_end()),
    };
    if !text.is_empty() && !text.ends_with('\n') {
        text.push('\n');
    }
    text.push_str(&format!("[exit {}]", exit_label(&output.status)));
    (false, String::new(), text)
}

fn exit_label(status: &ExitStatus) -> String {
    if let Some(code) = status.code() {
        return code.to_string();
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            return format!("sinal {}", sig);
        }
    }
    "?".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `echo` existe nos três shells (no PowerShell é alias de Write-Output), então o
    /// teste vale como fumaça em qualquer plataforma onde o build rodar.
    #[test]
    fn runs_a_command_on_this_platform() {
        let output = run("echo neo-shell-ok").expect("nenhum shell disponível");
        let (success, text, err) = split_output(output);
        assert!(success, "comando falhou: {err}");
        assert!(text.contains("neo-shell-ok"), "saída inesperada: {text:?}");
    }

    /// Os passos de plano mandam `working_dir`; ignorá-lo faz um comando relativo
    /// rodar no cwd do app e devolver a saída de outra pasta — errada e plausível.
    #[test]
    fn honors_working_dir_when_given() {
        let dir = std::env::temp_dir();
        let pwd = if cfg!(target_os = "windows") { "Get-Location" } else { "pwd" };
        let output = run_in(pwd, Some(&dir.to_string_lossy())).expect("nenhum shell disponível");
        let (success, text, err) = split_output(output);
        assert!(success, "comando falhou: {err}");
        // O temp dir do macOS é um symlink (/var → /private/var), então compara o
        // último componente em vez do caminho inteiro.
        let leaf = dir.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
        assert!(text.contains(&leaf), "esperava rodar em {leaf:?}, saída: {text:?}");
    }

    /// Sem `working_dir` o comando roda na home — nunca no cwd do app (o código-fonte).
    #[cfg(unix)]
    #[test]
    fn defaults_to_home_not_app_cwd() {
        let output = run("pwd").expect("nenhum shell disponível");
        let (success, text, _) = split_output(output);
        assert!(success);
        let home = dirs::home_dir().unwrap();
        assert_eq!(text.trim(), home.to_string_lossy());
    }

    #[test]
    fn missing_working_dir_is_explained() {
        let err = run_in("echo x", Some("/nao/existe/neo-shell")).unwrap_err();
        assert!(err.contains("não existe"), "{err}");
    }

    /// Comando inexistente é resultado legítimo (success=false), não erro de shell —
    /// se virasse `Err`, o fallback tentaria o próximo shell à toa.
    #[test]
    fn failing_command_is_not_a_shell_error() {
        let output = run("comando-que-nao-existe-neo").expect("nenhum shell disponível");
        let (success, _, err) = split_output(output);
        assert!(!success, "deveria falhar");
        assert!(!err.is_empty(), "erro deveria explicar a falha");
    }

    #[cfg(unix)]
    #[test]
    fn failure_keeps_stdout_and_exit_code() {
        let output = run("echo diagnostico-no-stdout; exit 3").unwrap();
        let (success, _, err) = split_output(output);
        assert!(!success);
        assert!(err.contains("diagnostico-no-stdout"), "{err}");
        assert!(err.contains("[exit 3]"), "{err}");
    }

    #[cfg(unix)]
    #[test]
    fn timeout_kills_the_command() {
        let started = Instant::now();
        let output = run_with("sleep 20", None, Duration::from_millis(300)).unwrap();
        let (success, _, err) = split_output(output);
        assert!(!success);
        assert!(err.contains("tempo limite"), "{err}");
        assert!(started.elapsed() < Duration::from_secs(10), "não matou a tempo");
    }

    #[cfg(unix)]
    #[test]
    fn background_child_does_not_hang_the_call() {
        let started = Instant::now();
        let output = run_with("sleep 30 & echo pronto", None, Duration::from_secs(20)).unwrap();
        let (success, text, _) = split_output(output);
        assert!(success);
        assert!(text.contains("pronto"));
        assert!(started.elapsed() < Duration::from_secs(10), "ficou preso no pipe do filho");
    }
}
