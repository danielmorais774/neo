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

use std::collections::{HashSet, VecDeque};
use std::io::{ErrorKind, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Output, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

/// Tempo de um comando quando o chamador não diz outro.
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(120);
/// Teto do `timeout` que o modelo pode pedir no `run_command`.
pub const MAX_TIMEOUT: Duration = Duration::from_secs(30 * 60);
/// Bytes guardados do COMEÇO e do FIM de cada fluxo. O meio é lido e descartado:
/// parar de ler travaria o processo no pipe cheio.
///
/// Eram 4 MiB contínuos, exatamente o limite de mensagem do gRPC do neo-core. Na
/// sessão 1bbdd7c5 um `zip -r` (uma linha por arquivo) encheu os 4 MiB; com o aviso
/// de corte e o envelope, a mensagem passou 224 bytes do limite, o servidor a recusou
/// (`ResourceExhausted`) e o turno morreu sem o Neo saber o que tinha acontecido. O
/// core mostra ao modelo no máximo ~24 mil caracteres, então o resto viajava à toa. E
/// numa saída longa o que importa é o FIM (o erro do build, o total do `du`), que o
/// corte contínuo jogava fora.
pub const HEAD_CAP: usize = 32 * 1024;
pub const TAIL_CAP: usize = 96 * 1024;
/// Teto do log em disco de uma tarefa em segundo plano (services/local_jobs.rs).
/// Passou disso, o arquivo para de crescer; a captura em memória segue com o fim.
const LOG_CAP: u64 = 16 * 1024 * 1024;
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
    let limits = Limits {
        timeout,
        background_after: None,
        max_runtime: timeout,
    };
    match run_limited(command, working_dir, limits)? {
        Ran::Done(output) => Ok(output),
        // Sem `background_after` não existe adoção; o ramo só fecha o `match`.
        Ran::Background(t) => Err(format!("comando virou tarefa {} sem pedido", t.id)),
    }
}

/// Quanto um comando pode durar, e quando ele deixa de prender o turno.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    /// Prazo em primeiro plano. Estourado sem ter virado tarefa, o grupo é encerrado.
    pub timeout: Duration,
    /// Passado este tempo ainda rodando, o comando vira tarefa em segundo plano
    /// (services/local_jobs.rs) em vez de morrer no prazo. `None` = nunca.
    pub background_after: Option<Duration>,
    /// Teto total de uma tarefa em segundo plano, contado do início do comando.
    pub max_runtime: Duration,
}

/// O que sobrou de um comando: a saída, ou a tarefa que ele virou.
pub enum Ran {
    Done(Output),
    Background(crate::services::local_jobs::Adopted),
}

/// Como [`run_with`], mas um comando que passa de `background_after` ainda
/// trabalhando vira tarefa em segundo plano em vez de ser morto. Foi o que faltou na
/// sessão 1bbdd7c5: o `zip` de 5,6 GB morreu aos 120 s, com 2,6 GB escritos.
pub fn run_limited(
    command: &str,
    working_dir: Option<&str>,
    limits: Limits,
) -> Result<Ran, String> {
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
    let limits = Limits {
        timeout: limits.timeout.min(MAX_TIMEOUT).max(Duration::from_millis(1)),
        ..limits
    };

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
            Ok(child) => return Ok(wait_bounded(child, command, dir.as_deref(), limits)),
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

/// "1,2 MB", "340 KB", "12 bytes" — para os avisos de corte.
pub fn human_bytes(n: u64) -> String {
    const KB: f64 = 1024.0;
    let f = n as f64;
    if f >= KB * KB * KB {
        format!("{:.1} GB", f / (KB * KB * KB)).replace('.', ",")
    } else if f >= KB * KB {
        format!("{:.1} MB", f / (KB * KB)).replace('.', ",")
    } else if f >= KB {
        format!("{:.0} KB", f / KB)
    } else {
        format!("{} bytes", n)
    }
}

/// Começo + fim de um fluxo, com a conta do que passou entre os dois.
#[derive(Default)]
pub struct Capture {
    head: Vec<u8>,
    tail: VecDeque<u8>,
    total: u64,
}

impl Capture {
    fn push(&mut self, buf: &[u8]) {
        self.total += buf.len() as u64;
        let room = HEAD_CAP.saturating_sub(self.head.len());
        let (cabeca, resto) = buf.split_at(buf.len().min(room));
        self.head.extend_from_slice(cabeca);
        if resto.is_empty() {
            return;
        }
        if resto.len() >= TAIL_CAP {
            self.tail.clear();
            self.tail.extend(&resto[resto.len() - TAIL_CAP..]);
        } else {
            let excesso = (self.tail.len() + resto.len()).saturating_sub(TAIL_CAP);
            self.tail.drain(..excesso);
            self.tail.extend(resto);
        }
    }

    /// Bytes que o fluxo produziu, inclusive os descartados do meio.
    pub fn total(&self) -> u64 {
        self.total
    }

    fn omitted(&self) -> u64 {
        self.total
            .saturating_sub((self.head.len() + self.tail.len()) as u64)
    }

    /// O que fica para o Neo: tudo, ou começo + aviso + fim.
    pub fn bytes(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.head.len() + self.tail.len() + 96);
        out.extend_from_slice(&self.head);
        let omitidos = self.omitted();
        if omitidos > 0 {
            out.extend_from_slice(
                format!(
                    "\n[neo] … {} do meio da saída foram omitidos …\n",
                    human_bytes(omitidos)
                )
                .as_bytes(),
            );
        }
        out.extend(self.tail.iter());
        out
    }

    /// Os últimos `max` bytes, como texto (o rabo de uma tarefa em segundo plano).
    pub fn last(&self, max: usize) -> String {
        let b = self.bytes();
        let inicio = b.len().saturating_sub(max);
        String::from_utf8_lossy(&b[inicio..]).to_string()
    }
}

/// Os dois fluxos de um comando, mais o log em disco quando ele vira tarefa.
#[derive(Default)]
pub struct Streams {
    pub out: Capture,
    pub err: Capture,
    log: Option<(std::fs::File, u64)>,
}

impl Streams {
    fn push(&mut self, which: Which, buf: &[u8]) {
        match which {
            Which::Out => self.out.push(buf),
            Which::Err => self.err.push(buf),
        }
        if let Some((file, written)) = self.log.as_mut() {
            let room = LOG_CAP.saturating_sub(*written) as usize;
            let n = buf.len().min(room);
            if n > 0 && file.write_all(&buf[..n]).is_ok() {
                *written += n as u64;
            }
        }
    }

    /// Liga o log em disco: grava o que já foi capturado e passa a acrescentar o resto
    /// (stdout e stderr juntos, na ordem em que chegam).
    pub fn start_log(&mut self, mut file: std::fs::File) {
        let mut written = 0u64;
        for (rotulo, cap) in [("", &self.out), ("[stderr]\n", &self.err)] {
            let b = cap.bytes();
            if b.is_empty() {
                continue;
            }
            let _ = file.write_all(rotulo.as_bytes());
            if file.write_all(&b).is_ok() {
                written += (rotulo.len() + b.len()) as u64;
            }
            let _ = file.write_all(b"\n");
        }
        self.log = Some((file, written));
    }
}

#[derive(Clone, Copy)]
enum Which {
    Out,
    Err,
}

fn drain<R: Read + Send + 'static>(
    mut src: R,
    shared: Arc<Mutex<Streams>>,
    which: Which,
) -> mpsc::Receiver<()> {
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = [0u8; 8192];
        loop {
            match src.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    if let Ok(mut s) = shared.lock() {
                        s.push(which, &buf[..n]);
                    }
                }
                Err(e) if e.kind() == ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
        let _ = tx.send(());
    });
    rx
}

/// Um processo em curso, com a saída sendo capturada. É o que vira tarefa.
pub struct Running {
    pub child: Child,
    pub pid: u32,
    pub shared: Arc<Mutex<Streams>>,
    drains: Vec<mpsc::Receiver<()>>,
    pub started: Instant,
}

impl Running {
    /// Depois que o shell sai, espera os fluxos fecharem e devolve (stdout, stderr).
    ///
    /// Um filho em background (`cmd &`) herda o pipe e o mantém aberto depois que o
    /// shell sai. Espera um pouco; se não soltar, encerra o grupo para liberar.
    pub fn collect(&self) -> (Vec<u8>, Vec<u8>) {
        for rx in &self.drains {
            if rx.recv_timeout(DRAIN_GRACE).is_err() {
                kill_tree(self.pid);
                let _ = rx.recv_timeout(DRAIN_GRACE);
            }
        }
        match self.shared.lock() {
            Ok(s) => (s.out.bytes(), s.err.bytes()),
            Err(_) => (Vec::new(), Vec::new()),
        }
    }
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

fn wait_bounded(mut child: Child, command: &str, cwd: Option<&Path>, limits: Limits) -> Ran {
    let pid = child.id();
    track_pid(pid);
    let shared = Arc::new(Mutex::new(Streams::default()));
    let mut drains = Vec::new();
    if let Some(s) = child.stdout.take() {
        drains.push(drain(s, shared.clone(), Which::Out));
    }
    if let Some(s) = child.stderr.take() {
        drains.push(drain(s, shared.clone(), Which::Err));
    }

    let started = Instant::now();
    let deadline = started + limits.timeout;
    // Adoção só faz sentido antes do prazo: um `timeout` explícito menor que o
    // orçamento de primeiro plano é o modelo dizendo "mate se passar disso".
    let mut adopt_at = limits
        .background_after
        .filter(|b| *b < limits.timeout)
        .map(|b| started + b);
    let mut timed_out = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) => {
                let now = Instant::now();
                if adopt_at.is_some_and(|at| now >= at) {
                    adopt_at = None;
                    if crate::services::local_jobs::has_room() {
                        // Sai da lista do Parar: o turno acaba agora e a tarefa não é
                        // dele. Quem para uma tarefa é a janela Tarefas.
                        untrack_pid(pid);
                        let running = Running { child, pid, shared, drains, started };
                        return Ran::Background(crate::services::local_jobs::adopt(
                            running,
                            command,
                            cwd,
                            limits.max_runtime,
                        ));
                    }
                    log::warn!(
                        "[shell] {} tarefas já rodando: o comando segue em primeiro plano até o prazo",
                        crate::services::local_jobs::MAX_RUNNING
                    );
                }
                if now >= deadline {
                    timed_out = true;
                    kill_tree(pid);
                    break child.wait().unwrap_or_else(|_| failed_status());
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(_) => {
                kill_tree(pid);
                break child.wait().unwrap_or_else(|_| failed_status());
            }
        }
    };

    let running = Running { child, pid, shared, drains, started };
    let (stdout, mut stderr) = running.collect();
    untrack_pid(pid);

    if timed_out {
        stderr.extend_from_slice(
            format!(
                "\n[neo] tempo limite de {}s estourado — o processo foi encerrado",
                limits.timeout.as_secs().max(1)
            )
            .as_bytes(),
        );
    }
    Ran::Done(Output { status, stdout, stderr })
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

    #[test]
    fn short_output_is_kept_whole() {
        let mut c = Capture::default();
        c.push(b"abc\n");
        assert_eq!(c.bytes(), b"abc\n");
    }

    /// Sessão 1bbdd7c5: a saída do `zip -r` passou de 4 MiB e derrubou o stream.
    /// Agora o que volta tem começo + aviso + fim, bem longe do limite do gRPC.
    #[test]
    fn long_output_keeps_head_and_tail_under_the_cap() {
        let mut c = Capture::default();
        let linha = b"  adding: projeto-loja/acme-mobile/ios/Pods/x.h (deflated 60%)\n";
        for _ in 0..80_000 {
            c.push(linha);
        }
        c.push(b"FIM-DA-SAIDA\n");
        let b = c.bytes();
        assert!(b.len() < HEAD_CAP + TAIL_CAP + 200, "{}", b.len());
        let t = String::from_utf8_lossy(&b);
        assert!(t.starts_with("  adding: "));
        assert!(t.ends_with("FIM-DA-SAIDA\n"));
        assert!(t.contains("omitidos"));
        assert_eq!(c.total(), (linha.len() * 80_000 + 13) as u64);
    }

    #[cfg(unix)]
    #[test]
    fn five_megabytes_of_stdout_come_back_small() {
        let output = run_with(
            "yes 'adding: a/b/c.txt (stored 0%)' | head -c 5000000; echo FIM",
            None,
            Duration::from_secs(20),
        )
        .unwrap();
        let (success, text, _) = split_output(output);
        assert!(success);
        assert!(text.len() < 200 * 1024, "{}", text.len());
        assert!(text.trim_end().ends_with("FIM"));
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
