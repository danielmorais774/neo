//! Atuadores de controle do device — volume, mídia, bloqueio e energia.
//!
//! Executa os comandos que chegam pelo canal `/ws/devices/control` do neo-core
//! (ver `websocket.rs`). Cada ação devolve `(ok, result_json)` — o `result` é o
//! estado REAL medido depois de agir, não o que foi pedido: é isso que permite
//! ao Neo dizer "ficou em 31" em vez de "mandei botar 30".
//!
//! Volume no macOS sai por CoreAudio (`AudioObjectSetPropertyData`) e não por
//! `osascript`: sem processo novo a cada clique, com leitura de volta exata, e
//! sem depender de permissão de automação. Já as teclas de mídia e o desligar
//! precisam de permissão do sistema (Acessibilidade / Automação) — quando ela
//! falta, a ação devolve erro explícito em vez de silêncio, e o device declara
//! a capability como indisponível.

use serde_json::{json, Value};

/// O que este device sabe fazer. Vai no pareamento e a cada conexão do canal,
/// para o core não oferecer ao cérebro uma ação que vai falhar depois de
/// prometer.
pub fn capabilities() -> Value {
    // Honesto por plataforma: o Windows ainda não tem volume/mídia (precisa da
    // COM IAudioEndpointVolume), e declarar `true` ali faria o cérebro oferecer
    // uma ação que só falha depois de prometer.
    json!({
        "volume": cfg!(any(target_os = "macos", all(unix, not(target_os = "macos")))),
        "media": cfg!(any(target_os = "macos", all(unix, not(target_os = "macos")))),
        "lock": true,
        "power": true,
        // Navegador local (docs/browser-local-plan.md): declarado para o core
        // saber que este device TEM o braço — mas quem decide se dá para usar
        // AGORA é o `browser.health`, porque o host do open-browser-use só
        // existe com o navegador aberto e a extensão carregada.
        "browser_local": crate::services::browser_local::available(),
        // Headless na máquina DELE: mesmo IP residencial do navegador visível,
        // sem tomar a tela. Não depende de extensão nem de navegador aberto —
        // só de ter um Chromium instalado —, então é o degrau que continua
        // disponível quando o `browser_local` não está.
        "browser_headless": crate::services::headless::available(),
    })
}

/// Executa uma ação do canal de controle. Bloqueante — chamar em `spawn_blocking`.
pub fn execute(action: &str, args: &Value) -> Result<Value, String> {
    match action {
        "get_status" => status(),
        "volume_set" => {
            let level = args.get("level").and_then(|v| v.as_i64()).unwrap_or(-1);
            if !(0..=100).contains(&level) {
                return Err(format!("nível inválido: {}", level));
            }
            set_volume(level as u8)?;
            status()
        }
        "volume_up" | "volume_down" => {
            let step = args.get("step").and_then(|v| v.as_i64()).unwrap_or(10) as i64;
            let current = get_volume()? as i64;
            let delta = if action == "volume_up" { step } else { -step };
            set_volume(current.saturating_add(delta).clamp(0, 100) as u8)?;
            status()
        }
        "mute" => {
            set_muted(true)?;
            status()
        }
        "unmute" => {
            set_muted(false)?;
            status()
        }
        "play" | "pause" | "play_pause" => {
            media_key(MediaKey::PlayPause)?;
            Ok(json!({ "sent": "play_pause" }))
        }
        "next" => {
            media_key(MediaKey::Next)?;
            Ok(json!({ "sent": "next" }))
        }
        "previous" => {
            media_key(MediaKey::Previous)?;
            Ok(json!({ "sent": "previous" }))
        }
        "lock" => {
            lock_screen()?;
            Ok(json!({ "locked": true }))
        }
        other => Err(format!("ação não suportada: {}", other)),
    }
}

/// Estado atual do áudio — a "prova" que volta pro Neo.
fn status() -> Result<Value, String> {
    Ok(json!({
        "level": get_volume()?,
        "muted": is_muted().unwrap_or(false),
    }))
}

#[derive(Clone, Copy)]
pub enum MediaKey {
    PlayPause,
    Next,
    Previous,
}

// ── Energia ─────────────────────────────────────────────────────────────────

/// Suspende/desliga/reinicia. O core confirma pela DESCONEXÃO do canal, então
/// aqui não há resposta a dar — a máquina cai.
pub fn power(action: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        let script = match action {
            "suspend" => return run("pmset", &["sleepnow"]),
            "shutdown" => "tell application \"System Events\" to shut down",
            "restart" => "tell application \"System Events\" to restart",
            other => return Err(format!("ação de energia desconhecida: {}", other)),
        };
        return run("osascript", &["-e", script]);
    }
    #[cfg(target_os = "windows")]
    {
        return match action {
            // /t 0 = já; sem isso o Windows agenda em 30s e o core lê como falha.
            "shutdown" => run("shutdown", &["/s", "/t", "0"]),
            "restart" => run("shutdown", &["/r", "/t", "0"]),
            "suspend" => run("rundll32.exe", &["powrprof.dll,SetSuspendState", "0,1,0"]),
            other => Err(format!("ação de energia desconhecida: {}", other)),
        };
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        return match action {
            "shutdown" => run("systemctl", &["poweroff"]),
            "restart" => run("systemctl", &["reboot"]),
            "suspend" => run("systemctl", &["suspend"]),
            other => Err(format!("ação de energia desconhecida: {}", other)),
        };
    }
}

fn lock_screen() -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        // Dorme o display; com "exigir senha ao acordar" ligado, é o bloqueio.
        run("pmset", &["displaysleepnow"])
    }
    #[cfg(target_os = "windows")]
    {
        run("rundll32.exe", &["user32.dll,LockWorkStation"])
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        run("loginctl", &["lock-session"])
    }
}

fn run(program: &str, args: &[&str]) -> Result<(), String> {
    let output = std::process::Command::new(program)
        .args(args)
        .output()
        .map_err(|e| format!("falha ao rodar {}: {}", program, e))?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    Err(if stderr.is_empty() {
        format!("{} falhou ({})", program, output.status)
    } else {
        stderr
    })
}

// ── Volume: macOS (CoreAudio) ───────────────────────────────────────────────

#[cfg(target_os = "macos")]
mod platform {
    use std::ffi::c_void;

    type AudioObjectID = u32;
    type OSStatus = i32;

    #[repr(C)]
    struct AudioObjectPropertyAddress {
        selector: u32,
        scope: u32,
        element: u32,
    }

    #[link(name = "CoreAudio", kind = "framework")]
    extern "C" {
        fn AudioObjectGetPropertyData(
            object: AudioObjectID,
            address: *const AudioObjectPropertyAddress,
            qualifier_size: u32,
            qualifier: *const c_void,
            data_size: *mut u32,
            data: *mut c_void,
        ) -> OSStatus;

        fn AudioObjectSetPropertyData(
            object: AudioObjectID,
            address: *const AudioObjectPropertyAddress,
            qualifier_size: u32,
            qualifier: *const c_void,
            data_size: u32,
            data: *const c_void,
        ) -> OSStatus;

        /// `Boolean` do CoreFoundation é `unsigned char` — mapear para o
        /// `bool` do Rust seria UB se o framework devolvesse algo fora de 0/1.
        fn AudioObjectHasProperty(
            object: AudioObjectID,
            address: *const AudioObjectPropertyAddress,
        ) -> u8;
    }

    const fn fourcc(code: &[u8; 4]) -> u32 {
        ((code[0] as u32) << 24)
            | ((code[1] as u32) << 16)
            | ((code[2] as u32) << 8)
            | (code[3] as u32)
    }

    const SYSTEM_OBJECT: AudioObjectID = 1;
    const SCOPE_GLOBAL: u32 = fourcc(b"glob");
    const SCOPE_OUTPUT: u32 = fourcc(b"outp");
    const PROP_DEFAULT_OUTPUT: u32 = fourcc(b"dOut");
    const PROP_VOLUME_SCALAR: u32 = fourcc(b"volm");
    const PROP_MUTE: u32 = fourcc(b"mute");

    fn address(selector: u32, scope: u32, element: u32) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress {
            selector,
            scope,
            element,
        }
    }

    fn default_output() -> Result<AudioObjectID, String> {
        let addr = address(PROP_DEFAULT_OUTPUT, SCOPE_GLOBAL, 0);
        let mut device: AudioObjectID = 0;
        let mut size = std::mem::size_of::<AudioObjectID>() as u32;
        let status = unsafe {
            AudioObjectGetPropertyData(
                SYSTEM_OBJECT,
                &addr,
                0,
                std::ptr::null(),
                &mut size,
                &mut device as *mut _ as *mut c_void,
            )
        };
        if status != 0 || device == 0 {
            return Err("não achei a saída de áudio padrão".to_string());
        }
        Ok(device)
    }

    /// Elementos a tentar, em ordem: 0 é o "master"; muitos dispositivos não o
    /// têm e só expõem volume por canal (1 = esquerdo, 2 = direito).
    const ELEMENTS: [u32; 3] = [0, 1, 2];

    pub fn get_volume() -> Result<u8, String> {
        let device = default_output()?;
        for element in ELEMENTS {
            let addr = address(PROP_VOLUME_SCALAR, SCOPE_OUTPUT, element);
            if unsafe { AudioObjectHasProperty(device, &addr) } == 0 {
                continue;
            }
            let mut value: f32 = 0.0;
            let mut size = std::mem::size_of::<f32>() as u32;
            let status = unsafe {
                AudioObjectGetPropertyData(
                    device,
                    &addr,
                    0,
                    std::ptr::null(),
                    &mut size,
                    &mut value as *mut _ as *mut c_void,
                )
            };
            if status == 0 {
                return Ok((value.clamp(0.0, 1.0) * 100.0).round() as u8);
            }
        }
        Err("esta saída de áudio não expõe controle de volume".to_string())
    }

    pub fn set_volume(level: u8) -> Result<(), String> {
        let device = default_output()?;
        let value: f32 = (level as f32 / 100.0).clamp(0.0, 1.0);
        let mut applied = false;
        for element in ELEMENTS {
            let addr = address(PROP_VOLUME_SCALAR, SCOPE_OUTPUT, element);
            if unsafe { AudioObjectHasProperty(device, &addr) } == 0 {
                continue;
            }
            let status = unsafe {
                AudioObjectSetPropertyData(
                    device,
                    &addr,
                    0,
                    std::ptr::null(),
                    std::mem::size_of::<f32>() as u32,
                    &value as *const _ as *const c_void,
                )
            };
            if status == 0 {
                applied = true;
                // Element 0 é o master: se ele pegou, os canais seguem junto.
                if element == 0 {
                    break;
                }
            }
        }
        if applied {
            Ok(())
        } else {
            Err("esta saída de áudio não aceita mudar o volume".to_string())
        }
    }

    pub fn is_muted() -> Result<bool, String> {
        let device = default_output()?;
        let addr = address(PROP_MUTE, SCOPE_OUTPUT, 0);
        if unsafe { AudioObjectHasProperty(device, &addr) } == 0 {
            return Ok(false);
        }
        let mut value: u32 = 0;
        let mut size = std::mem::size_of::<u32>() as u32;
        let status = unsafe {
            AudioObjectGetPropertyData(
                device,
                &addr,
                0,
                std::ptr::null(),
                &mut size,
                &mut value as *mut _ as *mut c_void,
            )
        };
        if status != 0 {
            return Ok(false);
        }
        Ok(value != 0)
    }

    pub fn set_muted(muted: bool) -> Result<(), String> {
        let device = default_output()?;
        let addr = address(PROP_MUTE, SCOPE_OUTPUT, 0);
        if unsafe { AudioObjectHasProperty(device, &addr) } == 0 {
            // Saída sem mute próprio (comum em HDMI/AirPlay): volume 0 é o
            // equivalente honesto, e o unmute vira responsabilidade do usuário.
            return if muted {
                set_volume(0)
            } else {
                Err("esta saída não tem mudo — ajuste o volume".to_string())
            };
        }
        let value: u32 = if muted { 1 } else { 0 };
        let status = unsafe {
            AudioObjectSetPropertyData(
                device,
                &addr,
                0,
                std::ptr::null(),
                std::mem::size_of::<u32>() as u32,
                &value as *const _ as *const c_void,
            )
        };
        if status == 0 {
            Ok(())
        } else {
            Err("não consegui mudar o mudo".to_string())
        }
    }

    /// Teclas de mídia no macOS.
    ///
    /// Não usamos o evento `NSSystemDefined` (a "tecla de mídia" de verdade):
    /// ele exige objc + permissão de Acessibilidade, e quando a permissão falta
    /// o SO engole o evento em silêncio — o pior resultado possível, porque o
    /// Neo reportaria sucesso sem nada ter acontecido.
    ///
    /// Em vez disso falamos com o player diretamente. Cobre exatamente o que o
    /// Neo toca hoje (Spotify e Music — ver a feature de música); aba de
    /// navegador continua sendo trabalho do `ExtBridge`. Exige permissão de
    /// Automação, que o macOS pede UMA vez e cuja recusa vira erro explícito.
    pub fn media_key(key: super::MediaKey) -> Result<(), String> {
        let verb = match key {
            super::MediaKey::PlayPause => "playpause",
            super::MediaKey::Next => "next track",
            super::MediaKey::Previous => "previous track",
        };
        // Spotify primeiro, Music depois; `is running` evita ABRIR um player
        // parado só para mandar play (efeito colateral que ninguém pediu).
        let script = format!(
            r#"if application "Spotify" is running then
                 tell application "Spotify" to {verb}
                 return "spotify"
               else if application "Music" is running then
                 tell application "Music" to {verb}
                 return "music"
               else
                 return "none"
               end if"#,
            verb = verb
        );

        let output = std::process::Command::new("osascript")
            .args(["-e", &script])
            .output()
            .map_err(|e| format!("falha ao rodar osascript: {}", e))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            return Err(if stderr.contains("-1743") {
                "falta permissão de Automação para controlar o player \
                 (Ajustes → Privacidade e Segurança → Automação)"
                    .to_string()
            } else {
                stderr
            });
        }

        if String::from_utf8_lossy(&output.stdout).trim() == "none" {
            return Err("nenhum player aberto (Spotify ou Music)".to_string());
        }
        Ok(())
    }
}

// ── Volume: Windows ─────────────────────────────────────────────────────────

#[cfg(target_os = "windows")]
mod platform {
    /// Windows não tem um binário do sistema que leia/ajuste o volume master —
    /// o caminho real é a COM `IAudioEndpointVolume`. Enquanto essa ligação não
    /// existe, declaramos indisponível: um "sucesso" que não mexe em nada é o
    /// pior resultado possível (o Neo diria "abaixei" sem ter abaixado).
    pub fn get_volume() -> Result<u8, String> {
        Err("controle de volume ainda não implementado no Windows".to_string())
    }
    pub fn set_volume(_level: u8) -> Result<(), String> {
        Err("controle de volume ainda não implementado no Windows".to_string())
    }
    pub fn is_muted() -> Result<bool, String> {
        Ok(false)
    }
    pub fn set_muted(_muted: bool) -> Result<(), String> {
        Err("mudo ainda não implementado no Windows".to_string())
    }
    pub fn media_key(_key: super::MediaKey) -> Result<(), String> {
        Err("teclas de mídia ainda não implementadas no Windows".to_string())
    }
}

// ── Volume: Linux (PulseAudio/PipeWire via pactl) ───────────────────────────

#[cfg(all(unix, not(target_os = "macos")))]
mod platform {
    fn pactl(args: &[&str]) -> Result<String, String> {
        let output = std::process::Command::new("pactl")
            .args(args)
            .output()
            .map_err(|e| format!("pactl indisponível: {}", e))?;
        if !output.status.success() {
            return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
        }
        Ok(String::from_utf8_lossy(&output.stdout).to_string())
    }

    pub fn get_volume() -> Result<u8, String> {
        let out = pactl(&["get-sink-volume", "@DEFAULT_SINK@"])?;
        // "Volume: front-left: 45875 /  70% / -9.29 dB, ..." — o 1º percentual.
        out.split('/')
            .find_map(|part| part.trim().strip_suffix('%'))
            .and_then(|n| n.trim().parse::<u8>().ok())
            .ok_or_else(|| "não consegui ler o volume do pactl".to_string())
    }

    pub fn set_volume(level: u8) -> Result<(), String> {
        pactl(&["set-sink-volume", "@DEFAULT_SINK@", &format!("{}%", level)]).map(|_| ())
    }

    pub fn is_muted() -> Result<bool, String> {
        Ok(pactl(&["get-sink-mute", "@DEFAULT_SINK@"])?.contains("yes"))
    }

    pub fn set_muted(muted: bool) -> Result<(), String> {
        pactl(&[
            "set-sink-mute",
            "@DEFAULT_SINK@",
            if muted { "1" } else { "0" },
        ])
        .map(|_| ())
    }

    pub fn media_key(key: super::MediaKey) -> Result<(), String> {
        let action = match key {
            super::MediaKey::PlayPause => "play-pause",
            super::MediaKey::Next => "next",
            super::MediaKey::Previous => "previous",
        };
        let output = std::process::Command::new("playerctl")
            .arg(action)
            .output()
            .map_err(|e| format!("playerctl indisponível: {}", e))?;
        if output.status.success() {
            Ok(())
        } else {
            Err(String::from_utf8_lossy(&output.stderr).trim().to_string())
        }
    }
}

pub use platform::{get_volume, is_muted, media_key, set_muted, set_volume};
