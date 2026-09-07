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

use std::io::ErrorKind;
use std::process::{Command, Output};

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

/// Roda `command` no primeiro shell disponível da plataforma e devolve a saída bruta.
///
/// Só cai para o próximo candidato quando o shell em si não existe (`NotFound`); erro
/// do comando executado é resultado legítimo e volta no `Output`, não vira retry.
pub fn run(command: &str) -> Result<Output, String> {
    run_in(command, None)
}

/// Igual a [`run`], mas executando dentro de `working_dir` quando informado.
///
/// Os passos de plano do neo-core mandam `{"command": ..., "working_dir": ...}`; sem
/// honrar o diretório, um comando com caminho relativo roda no cwd do app e devolve a
/// saída de outra pasta — errada, mas plausível, que é o pior tipo de resultado.
/// Diretório inexistente vira erro do `spawn`, não silêncio.
pub fn run_in(command: &str, working_dir: Option<&str>) -> Result<Output, String> {
    let shells = candidates();
    let mut last_err = String::new();

    for (program, args) in &shells {
        let mut cmd = Command::new(program);
        cmd.args(*args).arg(command);
        if let Some(dir) = working_dir.filter(|d| !d.trim().is_empty()) {
            cmd.current_dir(dir);
        }
        match cmd.output() {
            Ok(output) => return Ok(output),
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

    /// Comando inexistente é resultado legítimo (success=false), não erro de shell —
    /// se virasse `Err`, o fallback tentaria o próximo shell à toa.
    #[test]
    fn failing_command_is_not_a_shell_error() {
        let output = run("comando-que-nao-existe-neo").expect("nenhum shell disponível");
        let (success, _, err) = split_output(output);
        assert!(!success, "deveria falhar");
        assert!(!err.is_empty(), "erro deveria explicar a falha");
    }
}

/// Separa `Output` em `(success, stdout_ou_stderr, erro)` — a forma que os dois
/// chamadores usam para montar ToolResult/CommandResult.
pub fn split_output(output: Output) -> (bool, String, String) {
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    if output.status.success() {
        let text = if stdout.is_empty() { stderr } else { stdout };
        (true, text, String::new())
    } else {
        let text = if stderr.is_empty() { stdout } else { stderr };
        (false, String::new(), text)
    }
}
