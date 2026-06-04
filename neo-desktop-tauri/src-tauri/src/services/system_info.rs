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
