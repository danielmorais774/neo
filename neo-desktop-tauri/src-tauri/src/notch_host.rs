#[cfg(target_os = "macos")]
use log::{info, warn};
#[cfg(target_os = "macos")]
use std::process::{Child, Command, Stdio};
#[cfg(target_os = "macos")]
use std::sync::Mutex;

#[cfg(target_os = "macos")]
pub struct NotchHostState {
    pub child: Mutex<Option<Child>>,
}

#[cfg(target_os = "macos")]
impl Default for NotchHostState {
    fn default() -> Self {
        Self {
            child: Mutex::new(None),
        }
    }
}

#[cfg(target_os = "macos")]
pub fn start_notch_host(state: &NotchHostState) {
    let mut guard = match state.child.lock() {
        Ok(guard) => guard,
        Err(_) => return,
    };

    if guard.is_some() {
        return;
    }

    let current_exe = match std::env::current_exe() {
        Ok(path) => path,
        Err(err) => {
            warn!("[NeoNotch] Failed to resolve current executable: {}", err);
            return;
        }
    };

    let Some(exe_dir) = current_exe.parent() else {
        warn!("[NeoNotch] Failed to resolve executable directory");
        return;
    };

    let host_path = exe_dir.join("NeoNotchHost");
    if !host_path.exists() {
        warn!(
            "[NeoNotch] Native notch host not found at {}",
            host_path.display()
        );
        return;
    }

    stop_existing_notch_hosts(&host_path);

    match Command::new(&host_path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => {
            info!("[NeoNotch] Native notch host started");
            *guard = Some(child);
        }
        Err(err) => {
            warn!("[NeoNotch] Failed to start native notch host: {}", err);
        }
    }
}

#[cfg(target_os = "macos")]
fn stop_existing_notch_hosts(host_path: &std::path::Path) {
    let pattern = host_path.to_string_lossy().to_string();
    if pattern.is_empty() {
        return;
    }

    let _ = Command::new("pkill")
        .arg("-f")
        .arg(&pattern)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(target_os = "macos")]
pub fn stop_notch_host(state: &NotchHostState) {
    let mut guard = match state.child.lock() {
        Ok(guard) => guard,
        Err(_) => return,
    };

    if let Some(mut child) = guard.take() {
        let _ = child.kill();
    }
}
