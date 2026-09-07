// audio_route -- descobre a rota de audio ativa do macOS (por onde o som SAI e
// por qual transporte) e traduz isso em duas decisoes que hoje sao hardcoded e
// degradam o audio do sistema inteiro:
//
//   1) AEC (o Voice Processing I/O da Apple, `setVoiceProcessingEnabled` no
//      nativo e `echoCancellation` no webview) so serve quando a saida e um
//      ALTO-FALANTE: ai a musica volta pelo ar e vaza no microfone. Em FONE
//      (Bluetooth ou P2) nao ha vazamento -- o som toca no seu ouvido -- e o
//      VPIO so cobra o preco: ele e duplex, abre o dispositivo de SAIDA padrao
//      junto, poe ele em modo "voice chat" e faz ducking do audio dos outros
//      apps. E por isso que a musica "piora" mesmo com entrada != saida.
//
//   2) Microfone Bluetooth: quando qualquer processo abre o mic de um fone BT,
//      o CoreAudio derruba o dispositivo de A2DP (estereo, alta fidelidade)
//      para HFP/SCO (mono, ~16 kHz, qualidade de telefone) -- nos DOIS sentidos.
//      Entao nunca escolhemos um mic BT por conta propria: caimos no mic INTERNO
//      do Mac, que mantem o fone em A2DP e nao vaza (a musica esta no ouvido).
//      Isso vale principalmente para o sidecar da wake word, que segura o
//      microfone 24/7.
//
// `NEO_AUDIO_AEC` = `auto` (default) | `on` | `off` forca a politica quando o
// palpite do transporte erra (ex.: caixa de som Bluetooth, onde a musica VAZA
// mas o transporte parece fone).

use serde::Serialize;

/// Politica de captura resolvida a partir da rota de audio atual.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AudioRoute {
    /// Ligar o cancelamento de eco (VPIO / `echoCancellation`)?
    pub aec: bool,
    /// Transporte da saida padrao: `builtin-speaker`, `headphones`, `bluetooth`,
    /// `usb`, `hdmi`, `airplay`, `other`, `unknown`.
    pub output_transport: String,
    /// Nome CoreAudio do mic interno ("MacBook Pro Microphone"). Vazio se nao
    /// achou. O PortAudio (sidecar da wake word) e o `enumerateDevices` do
    /// webview reportam o MESMO nome, entao da p/ casar por string nos dois.
    pub builtin_input_name: String,
    /// `true` se o microfone padrao do sistema e um dispositivo Bluetooth --
    /// nesse caso preferimos o mic interno para nao derrubar o fone p/ HFP.
    pub default_input_is_bluetooth: bool,
    /// Motivo da decisao, p/ log e para a UI explicar ao usuario.
    pub reason: String,
}

impl AudioRoute {
    /// Mic que a captura deve usar: o interno quando o padrao e Bluetooth
    /// (senao o fone cai p/ HFP), ou `None` = deixa o sistema decidir. So
    /// sobrepomos o caso nocivo -- um mic USB escolhido pelo usuario continua
    /// valendo.
    pub fn preferred_input_name(&self) -> Option<&str> {
        if self.default_input_is_bluetooth && !self.builtin_input_name.is_empty() {
            Some(&self.builtin_input_name)
        } else {
            None
        }
    }

}

impl Default for AudioRoute {
    fn default() -> Self {
        Self {
            aec: true,
            output_transport: "unknown".to_string(),
            builtin_input_name: String::new(),
            default_input_is_bluetooth: false,
            reason: "rota de audio indisponivel; mantendo o comportamento padrao".to_string(),
        }
    }
}

/// `auto` (default) | `on` | `off` -- escape hatch p/ quando o transporte engana
/// (caixa de som Bluetooth, headset USB...).
fn aec_override() -> Option<bool> {
    match std::env::var("NEO_AUDIO_AEC")
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase()
        .as_str()
    {
        "on" | "1" | "true" => Some(true),
        "off" | "0" | "false" => Some(false),
        _ => None,
    }
}

#[cfg(not(target_os = "macos"))]
pub fn resolve() -> AudioRoute {
    let mut route = AudioRoute::default();
    if let Some(forced) = aec_override() {
        route.aec = forced;
        route.reason = format!("NEO_AUDIO_AEC={}", if forced { "on" } else { "off" });
    }
    route
}

/// Um microfone disponivel, para o seletor do painel.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AudioInputDevice {
    /// Chave comum aos tres processos -- ver `mac::list_inputs`.
    pub name: String,
    /// `builtin` | `bluetooth` | `usb` | `other`, so p/ a UI rotular.
    pub transport: String,
}

/// Microfones disponiveis agora (lista vazia fora do macOS).
pub fn list_inputs() -> Vec<AudioInputDevice> {
    #[cfg(target_os = "macos")]
    {
        macos::list_inputs()
    }
    #[cfg(not(target_os = "macos"))]
    {
        Vec::new()
    }
}

// ---------------------------------------------------------------- preferencia

/// Microfone que o usuario escolheu no painel, por NOME. `None`/vazio = padrao
/// do sistema. Persistido em disco (nao em localStorage) porque os TRES
/// processos precisam ler: webview, notch nativo e sidecar da wake word.
fn preference_path() -> std::path::PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| std::path::PathBuf::from("."))
        .join("neo-desktop")
        .join("audio-input.json")
}

pub fn preferred_input() -> Option<String> {
    let raw = std::fs::read_to_string(preference_path()).ok()?;
    let value: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let name = value.get("name")?.as_str()?.trim().to_string();
    if name.is_empty() {
        None
    } else {
        Some(name)
    }
}

/// Grava a escolha; `None`/vazio limpa (volta ao padrao do sistema).
pub fn set_preferred_input(name: Option<String>) -> Result<(), String> {
    let path = preference_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let name = name.map(|n| n.trim().to_string()).filter(|n| !n.is_empty());
    let body = serde_json::json!({ "name": name });
    std::fs::write(&path, body.to_string()).map_err(|e| e.to_string())
}

#[cfg(target_os = "macos")]
pub use macos::resolve;

#[cfg(target_os = "macos")]
mod macos {
    use super::{aec_override, AudioRoute};
    use std::os::raw::{c_char, c_void};

    type OSStatus = i32;
    type AudioObjectID = u32;
    type CFStringRef = *const c_void;

    #[repr(C)]
    struct AudioObjectPropertyAddress {
        selector: u32,
        scope: u32,
        element: u32,
    }

    #[repr(C)]
    struct AudioBuffer {
        number_channels: u32,
        data_byte_size: u32,
        data: *mut c_void,
    }

    /// Cuidado: `buffers` NAO comeca no offset 4. `AudioBuffer` tem um ponteiro
    /// dentro, entao alinha em 8 -- ha 4 bytes de padding depois do contador.
    /// Por isso o offset sai do `offset_of!` e nao de um `size_of::<u32>()`.
    #[repr(C)]
    struct AudioBufferList {
        number_buffers: u32,
        buffers: [AudioBuffer; 1],
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

        fn AudioObjectGetPropertyDataSize(
            object: AudioObjectID,
            address: *const AudioObjectPropertyAddress,
            qualifier_size: u32,
            qualifier: *const c_void,
            data_size: *mut u32,
        ) -> OSStatus;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFStringGetCString(
            string: CFStringRef,
            buffer: *mut c_char,
            buffer_size: isize,
            encoding: u32,
        ) -> bool;
        fn CFRelease(cf: *const c_void);
    }

    const KCF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;

    const fn fourcc(code: &[u8; 4]) -> u32 {
        ((code[0] as u32) << 24)
            | ((code[1] as u32) << 16)
            | ((code[2] as u32) << 8)
            | (code[3] as u32)
    }

    const SYSTEM_OBJECT: AudioObjectID = 1;
    const ELEMENT_MAIN: u32 = 0;

    const SCOPE_GLOBAL: u32 = fourcc(b"glob");
    const SCOPE_INPUT: u32 = fourcc(b"inpt");
    const SCOPE_OUTPUT: u32 = fourcc(b"outp");

    const PROP_DEVICES: u32 = fourcc(b"dev#");
    const PROP_DEFAULT_OUTPUT: u32 = fourcc(b"dOut");
    const PROP_DEFAULT_INPUT: u32 = fourcc(b"dIn ");
    const PROP_TRANSPORT_TYPE: u32 = fourcc(b"tran");
    const PROP_DATA_SOURCE: u32 = fourcc(b"ssrc");
    const PROP_NAME: u32 = fourcc(b"lnam");
    const PROP_STREAM_CONFIG: u32 = fourcc(b"slay");

    const TRANSPORT_BUILTIN: u32 = fourcc(b"bltn");
    const TRANSPORT_BLUETOOTH: u32 = fourcc(b"blue");
    const TRANSPORT_BLUETOOTH_LE: u32 = fourcc(b"blea");
    const TRANSPORT_USB: u32 = fourcc(b"usb ");
    const TRANSPORT_HDMI: u32 = fourcc(b"hdmi");
    const TRANSPORT_DISPLAYPORT: u32 = fourcc(b"dprt");
    const TRANSPORT_AIRPLAY: u32 = fourcc(b"airp");

    /// Data source da saida embutida: fone no P2 ('hdpn') vs alto-falante ('ispk').
    const SOURCE_HEADPHONES: u32 = fourcc(b"hdpn");

    fn address(selector: u32, scope: u32) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress {
            selector,
            scope,
            element: ELEMENT_MAIN,
        }
    }

    /// Le uma propriedade de tamanho fixo (u32, AudioDeviceID, ...).
    fn get_u32(object: AudioObjectID, selector: u32, scope: u32) -> Option<u32> {
        let addr = address(selector, scope);
        let mut value: u32 = 0;
        let mut size = std::mem::size_of::<u32>() as u32;
        let status = unsafe {
            AudioObjectGetPropertyData(
                object,
                &addr,
                0,
                std::ptr::null(),
                &mut size,
                &mut value as *mut u32 as *mut c_void,
            )
        };
        if status == 0 {
            Some(value)
        } else {
            None
        }
    }

    fn get_name(device: AudioObjectID) -> Option<String> {
        let addr = address(PROP_NAME, SCOPE_GLOBAL);
        let mut cf: CFStringRef = std::ptr::null();
        let mut size = std::mem::size_of::<CFStringRef>() as u32;
        let status = unsafe {
            AudioObjectGetPropertyData(
                device,
                &addr,
                0,
                std::ptr::null(),
                &mut size,
                &mut cf as *mut CFStringRef as *mut c_void,
            )
        };
        if status != 0 || cf.is_null() {
            return None;
        }
        // Nomes de dispositivo sao curtos; 512 bytes cobrem com folga em UTF-8.
        let mut buf = [0_i8; 512];
        let ok = unsafe {
            CFStringGetCString(
                cf,
                buf.as_mut_ptr(),
                buf.len() as isize,
                KCF_STRING_ENCODING_UTF8,
            )
        };
        unsafe { CFRelease(cf) };
        if !ok {
            return None;
        }
        let bytes: Vec<u8> = buf
            .iter()
            .take_while(|&&c| c != 0)
            .map(|&c| c as u8)
            .collect();
        String::from_utf8(bytes).ok()
    }

    /// Quantos canais de ENTRADA o dispositivo tem (0 = so saida).
    fn input_channels(device: AudioObjectID) -> u32 {
        let addr = address(PROP_STREAM_CONFIG, SCOPE_INPUT);
        let mut size: u32 = 0;
        let status = unsafe {
            AudioObjectGetPropertyDataSize(device, &addr, 0, std::ptr::null(), &mut size)
        };
        if status != 0 || size == 0 {
            return 0;
        }
        let mut raw = vec![0_u8; size as usize];
        let status = unsafe {
            AudioObjectGetPropertyData(
                device,
                &addr,
                0,
                std::ptr::null(),
                &mut size,
                raw.as_mut_ptr() as *mut c_void,
            )
        };
        if status != 0 {
            return 0;
        }
        let count = u32::from_ne_bytes([raw[0], raw[1], raw[2], raw[3]]) as usize;
        let mut channels = 0_u32;
        let base = std::mem::offset_of!(AudioBufferList, buffers);
        let stride = std::mem::size_of::<AudioBuffer>();
        for i in 0..count {
            let offset = base + i * stride;
            if offset + 4 > raw.len() {
                break;
            }
            channels += u32::from_ne_bytes([
                raw[offset],
                raw[offset + 1],
                raw[offset + 2],
                raw[offset + 3],
            ]);
        }
        channels
    }

    fn all_devices() -> Vec<AudioObjectID> {
        let addr = address(PROP_DEVICES, SCOPE_GLOBAL);
        let mut size: u32 = 0;
        let status = unsafe {
            AudioObjectGetPropertyDataSize(SYSTEM_OBJECT, &addr, 0, std::ptr::null(), &mut size)
        };
        if status != 0 || size == 0 {
            return Vec::new();
        }
        let count = size as usize / std::mem::size_of::<AudioObjectID>();
        let mut ids = vec![0_u32; count];
        let status = unsafe {
            AudioObjectGetPropertyData(
                SYSTEM_OBJECT,
                &addr,
                0,
                std::ptr::null(),
                &mut size,
                ids.as_mut_ptr() as *mut c_void,
            )
        };
        if status != 0 {
            return Vec::new();
        }
        ids
    }

    /// Todos os microfones visiveis, para o usuario escolher no painel. O NOME e
    /// a chave: e o mesmo string no CoreAudio (aqui), no `localizedName` do
    /// AVCaptureDevice (notch nativo) e no `label` do enumerateDevices (webview),
    /// entao serve de identificador comum aos tres processos -- diferente dos ids
    /// numericos do CoreAudio (mudam a cada reboot) e dos deviceIds do WebKit
    /// (salgados por origem).
    pub fn list_inputs() -> Vec<super::AudioInputDevice> {
        all_devices()
            .into_iter()
            .filter(|id| input_channels(*id) > 0)
            .filter_map(|id| {
                let name = get_name(id)?;
                let transport = match get_u32(id, PROP_TRANSPORT_TYPE, SCOPE_GLOBAL) {
                    Some(TRANSPORT_BUILTIN) => "builtin",
                    Some(TRANSPORT_BLUETOOTH) | Some(TRANSPORT_BLUETOOTH_LE) => "bluetooth",
                    Some(TRANSPORT_USB) => "usb",
                    _ => "other",
                };
                Some(super::AudioInputDevice {
                    name,
                    transport: transport.to_string(),
                })
            })
            .collect()
    }

    /// O mic embutido do Mac: transporte 'bltn' e com canais de entrada.
    fn builtin_input_name() -> Option<String> {
        all_devices().into_iter().find_map(|id| {
            if input_channels(id) == 0 {
                return None;
            }
            if get_u32(id, PROP_TRANSPORT_TYPE, SCOPE_GLOBAL)? != TRANSPORT_BUILTIN {
                return None;
            }
            get_name(id)
        })
    }

    /// Traduz o transporte da saida padrao no rotulo da rota. A saida embutida
    /// se desdobra em duas: fone no P2 (`hdpn`) nao vaza, alto-falante vaza.
    fn output_route(device: AudioObjectID) -> &'static str {
        match get_u32(device, PROP_TRANSPORT_TYPE, SCOPE_GLOBAL) {
            Some(TRANSPORT_BUILTIN) => {
                match get_u32(device, PROP_DATA_SOURCE, SCOPE_OUTPUT) {
                    Some(SOURCE_HEADPHONES) => "headphones",
                    _ => "builtin-speaker",
                }
            }
            Some(TRANSPORT_BLUETOOTH) | Some(TRANSPORT_BLUETOOTH_LE) => "bluetooth",
            Some(TRANSPORT_USB) => "usb",
            Some(TRANSPORT_HDMI) => "hdmi",
            Some(TRANSPORT_DISPLAYPORT) => "hdmi",
            Some(TRANSPORT_AIRPLAY) => "airplay",
            Some(_) => "other",
            None => "unknown",
        }
    }

    pub fn resolve() -> AudioRoute {
        let mut route = AudioRoute::default();

        if let Some(name) = builtin_input_name() {
            route.builtin_input_name = name;
        }

        if let Some(input) = get_u32(SYSTEM_OBJECT, PROP_DEFAULT_INPUT, SCOPE_GLOBAL) {
            route.default_input_is_bluetooth = matches!(
                get_u32(input, PROP_TRANSPORT_TYPE, SCOPE_GLOBAL),
                Some(TRANSPORT_BLUETOOTH) | Some(TRANSPORT_BLUETOOTH_LE)
            );
        }

        let output = get_u32(SYSTEM_OBJECT, PROP_DEFAULT_OUTPUT, SCOPE_GLOBAL);
        route.output_transport = output.map(output_route).unwrap_or("unknown").to_string();

        // Fone (BT ou P2) => o som toca no ouvido, nao volta pro mic: AEC seria
        // so custo (ducking da musica + o VPIO tomando a saida padrao).
        // Alto-falante => a musica vaza no mic: AEC e exatamente o que salva o STT.
        let leaks = !matches!(route.output_transport.as_str(), "headphones" | "bluetooth");
        route.aec = leaks;
        route.reason = match route.output_transport.as_str() {
            "headphones" => "saida em fone com fio: sem vazamento acustico, AEC dispensado".into(),
            "bluetooth" => {
                "saida em fone Bluetooth: sem vazamento acustico; AEC desligado e mic interno \
                 preferido p/ manter o A2DP"
                    .into()
            }
            "unknown" => "rota de saida desconhecida: mantendo o AEC ligado".into(),
            other => format!("saida em alto-falante ({}): AEC ligado p/ nao captar a musica", other),
        };

        if let Some(forced) = aec_override() {
            route.aec = forced;
            route.reason = format!(
                "NEO_AUDIO_AEC={} (forcado; rota detectada: {})",
                if forced { "on" } else { "off" },
                route.output_transport
            );
        }

        route
    }
}

#[cfg(test)]
mod preference_tests {
    use super::*;

    /// O round-trip tem de sobreviver ao disco: e assim que o notch nativo (outro
    /// PROCESSO) le a escolha feita no painel do webview.
    #[test]
    fn preference_round_trip() {
        let original = preferred_input();

        set_preferred_input(Some("Microfone (MacBook Air)".to_string())).unwrap();
        assert_eq!(preferred_input().as_deref(), Some("Microfone (MacBook Air)"));

        // vazio e so-espaco limpam (= padrao do sistema), nunca viram nome valido
        set_preferred_input(Some("   ".to_string())).unwrap();
        assert_eq!(preferred_input(), None);

        set_preferred_input(Some("USB Mic".to_string())).unwrap();
        assert_eq!(preferred_input().as_deref(), Some("USB Mic"));

        set_preferred_input(None).unwrap();
        assert_eq!(preferred_input(), None);

        set_preferred_input(original).unwrap(); // nao estraga a config real
    }

    /// A lista tem de conter o mic embutido com o MESMO nome que o AVCaptureDevice
    /// reporta -- e o nome que casa as tres superficies.
    #[test]
    #[cfg(target_os = "macos")]
    fn lists_builtin_input() {
        let inputs = list_inputs();
        assert!(!inputs.is_empty(), "nenhum microfone listado");
        assert!(
            inputs.iter().any(|d| d.transport == "builtin"),
            "sem mic embutido na lista: {inputs:?}"
        );
    }
}
