use std::process::Command;

#[cfg(target_os = "linux")]
use std::fs;

#[derive(Debug, Clone)]
pub struct SystemInfo {
    pub hardware_id: String,
}

pub fn collect_system_info() -> SystemInfo {
    SystemInfo {
        hardware_id: detect_hardware_id().unwrap_or_default(),
    }
}

fn detect_hardware_id() -> Option<String> {
    #[cfg(target_os = "macos")]
    {
        detect_macos_hardware_id()
    }

    #[cfg(target_os = "linux")]
    {
        detect_linux_hardware_id()
    }

    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        None
    }
}

#[cfg(target_os = "macos")]
fn detect_macos_hardware_id() -> Option<String> {
    parse_keyed_output(
        &run_command("ioreg", &["-rd1", "-c", "IOPlatformExpertDevice"])?,
        "IOPlatformUUID",
    )
    .or_else(|| {
        parse_keyed_output(
            &run_command("system_profiler", &["SPHardwareDataType"])?,
            "Hardware UUID",
        )
    })
    .map(normalize_hardware_id)
}

#[cfg(target_os = "linux")]
fn detect_linux_hardware_id() -> Option<String> {
    [
        "/sys/class/dmi/id/product_uuid",
        "/sys/class/dmi/id/product_serial",
        "/sys/class/dmi/id/board_serial",
        "/etc/machine-id",
    ]
    .iter()
    .find_map(|path| fs::read_to_string(path).ok())
    .map(|value| normalize_hardware_id(value.trim()))
    .filter(|value| !value.is_empty())
}

/// Versão do SO para o bloco de ambiente do prompt (`os_release` no metadata).
///
/// O neo-core já lia esse campo em `_build_system_context`, mas o desktop nunca o
/// mandava — a linha saía "macOS (aarch64)", sem versão. Cacheado num `OnceLock`
/// porque é subprocesso e o metadata é remontado a cada mensagem.
pub fn os_release() -> String {
    static CACHE: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    CACHE
        .get_or_init(|| detect_os_release().unwrap_or_default())
        .clone()
}

#[cfg(target_os = "macos")]
fn detect_os_release() -> Option<String> {
    run_command("sw_vers", &["-productVersion"])
}

#[cfg(target_os = "linux")]
fn detect_os_release() -> Option<String> {
    // PRETTY_NAME ("Ubuntu 24.04.1 LTS") diz mais ao modelo que a versão do kernel.
    fs::read_to_string("/etc/os-release").ok().and_then(|txt| {
        txt.lines()
            .find_map(|line| line.strip_prefix("PRETTY_NAME="))
            .map(|value| value.trim().trim_matches('"').to_string())
            .filter(|value| !value.is_empty())
    })
}

#[cfg(target_os = "windows")]
fn detect_os_release() -> Option<String> {
    // `ver` devolve "Microsoft Windows [Version 10.0.22631.4317]" — só o número interessa,
    // o nome do SO já vem do platform.
    let raw = run_command("cmd", &["/C", "ver"])?;
    let parsed = raw
        .split('[')
        .nth(1)
        .and_then(|s| s.trim_end_matches(']').strip_prefix("Version "))
        .unwrap_or_else(|| raw.trim())
        .to_string();
    Some(parsed).filter(|v| !v.is_empty())
}

#[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
fn detect_os_release() -> Option<String> {
    None
}

#[cfg_attr(target_os = "linux", allow(dead_code))]
fn run_command(program: &str, args: &[&str]) -> Option<String> {
    let output = Command::new(program).args(args).output().ok()?;
    if !output.status.success() {
        return None;
    }

    let stdout = String::from_utf8(output.stdout).ok()?;
    let trimmed = stdout.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn parse_keyed_output(output: &str, key: &str) -> Option<String> {
    output.lines().find_map(|line| {
        let trimmed = line.trim();
        let (found_key, value) = trimmed.split_once(':').or_else(|| trimmed.split_once('='))?;
        if found_key.trim().trim_matches('"') != key {
            return None;
        }

        let cleaned = value.trim().trim_matches('"').trim();
        if cleaned.is_empty() {
            None
        } else {
            Some(cleaned.to_string())
        }
    })
}

fn normalize_hardware_id(value: impl AsRef<str>) -> String {
    value.as_ref().trim().to_uppercase()
}

/// Nome da máquina — vira o `display_name` padrão do device no pareamento
/// ("MacBook-Pro-de-Daniel"). É bem melhor que "unknown" como primeira
/// impressão, e o usuário renomeia depois para "PC da sala".
pub fn hostname() -> Option<String> {
    let output = if cfg!(target_os = "windows") {
        std::process::Command::new("cmd")
            .args(["/C", "hostname"])
            .output()
            .ok()?
    } else {
        std::process::Command::new("hostname").output().ok()?
    };
    let name = String::from_utf8_lossy(&output.stdout).trim().to_string();
    // .local do Bonjour só polui o nome exibido.
    let name = name.trim_end_matches(".local").to_string();
    if name.is_empty() {
        None
    } else {
        Some(name)
    }
}

/// Modelo do hardware ("MacBookPro18,3"). Diferente do `metadata["model"]` que
/// o gRPC carrega — aquele é o modelo do LLM, não da máquina.
pub fn hardware_model() -> String {
    #[cfg(target_os = "macos")]
    {
        if let Ok(output) = std::process::Command::new("sysctl")
            .args(["-n", "hw.model"])
            .output()
        {
            let model = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !model.is_empty() {
                return model;
            }
        }
    }
    #[cfg(target_os = "linux")]
    {
        if let Ok(model) = std::fs::read_to_string("/sys/devices/virtual/dmi/id/product_name") {
            let model = model.trim().to_string();
            if !model.is_empty() {
                return model;
            }
        }
    }
    hostname().unwrap_or_else(|| "unknown".to_string())
}
