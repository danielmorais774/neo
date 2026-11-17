// Supervisor do sidecar de wake word (Estagio 1 do Modo Live).
//
// Sobe e vigia o processo Python `sidecar/wake/neo_wake.py`, que escuta o
// microfone do Mac e dispara o openWakeWord localmente. Cada linha de stdout do
// sidecar e um evento JSON; o `wake` vira um evento Tauri `wake-detected` que o
// frontend usa para acender o Modo Live (useLiveMode, ja existente).
//
// O sidecar e local e offline -- nao depende do PC gamer nem da rede (so o
// Estagio 2, a conversa em si, fala com o neo-voice). Ver sidecar/wake/README.md.
//
// Controle de ciclo de vida:
//   * `watch` channel sinaliza "pare" sem perder o aviso (resolve a corrida do
//     StrictMode, que faz start->stop->start no mount: nada de sidecar dobrado).
//   * `epoch` invalida sessoes antigas, p/ a limpeza de um supervisor que saiu
//     sozinho (falha fatal) nao zerar o `running` de uma sessao mais nova.

use log::{error, info, warn};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::watch;

/// Payload do evento Tauri `wake-detected` enviado ao frontend.
#[derive(Clone, Serialize)]
pub struct WakeDetected {
    pub model: String,
    pub score: f64,
}

// ---- Gate anti-eco (wake suprimido enquanto o Neo fala) ------------------
//
// O mic do sidecar e sempre-ligado e ouve o proprio TTS do Neo pelo alto-
// falante -- e o Neo fala "Neo" o tempo todo, entao as frases de wake casavam
// com a fala dele e o chip Live aparecia "do nada". As fontes de TTS (chip
// Live nativo, notch, webview) reportam speaking on/off e o evento `wake` e
// descartado enquanto alguma estiver ativa, mais uma cauda de 1.5s (a janela
// do detector ainda contem o fim do TTS logo apos ele parar).
//
// O `interrupt` ("pare") NUNCA passa por aqui: e exatamente durante a fala do
// Neo que ele precisa funcionar (barge-in).

struct TtsGate {
    /// Fontes de TTS atualmente falando ("live" | "notch" | "webview").
    speaking: HashSet<String>,
    /// Fim da cauda de supressao apos a ultima fonte silenciar.
    tail_until: Option<Instant>,
}

static TTS_GATE: OnceLock<Mutex<TtsGate>> = OnceLock::new();

fn tts_gate() -> &'static Mutex<TtsGate> {
    TTS_GATE.get_or_init(|| {
        Mutex::new(TtsGate {
            speaking: HashSet::new(),
            tail_until: None,
        })
    })
}

/// Canal de estado agregado de TTS empurrado ao sidecar de wake (uma linha
/// `{"tts": bool}` por mudanca). O sidecar entao roda so o grupo de detectores
/// relevante -- stop enquanto o Neo fala, wake quando ocioso -- cortando ~metade
/// das inferencias sem perder deteccao. So o Sender vive no estatico; cada sidecar
/// novo faz `.subscribe()` e o `watch` retem o ultimo valor para ele.
static TTS_STATE_TX: OnceLock<watch::Sender<bool>> = OnceLock::new();

fn tts_state_tx() -> &'static watch::Sender<bool> {
    TTS_STATE_TX.get_or_init(|| watch::channel(false).0)
}

/// Reporta o estado do TTS de uma fonte ("live", "notch", "webview").
pub fn set_tts_speaking(source: &str, speaking: bool) {
    let now_speaking = {
        let Ok(mut gate) = tts_gate().lock() else {
            return;
        };
        if speaking {
            gate.speaking.insert(source.to_string());
            gate.tail_until = None;
        } else {
            gate.speaking.remove(source);
            if gate.speaking.is_empty() {
                gate.tail_until = Some(Instant::now() + Duration::from_millis(1500));
            }
        }
        !gate.speaking.is_empty()
    };
    // Empurra o estado agregado ao sidecar (dedup/retencao pelo watch). Sem
    // receivers (sidecar parado) o valor fica retido para o proximo sidecar.
    tts_state_tx().send_replace(now_speaking);
}

/// `true` enquanto algum TTS fala (ou acabou de falar): descarta o `wake`.
fn wake_suppressed() -> bool {
    let Ok(gate) = tts_gate().lock() else {
        return false;
    };
    if !gate.speaking.is_empty() {
        return true;
    }
    gate.tail_until.map(|t| Instant::now() < t).unwrap_or(false)
}

/// Modo teste (tela "Teste de Wake"): o detector segue rodando e emitindo score,
/// mas o disparo NAO aciona o Live -- vira o evento `wake-test-fire` (só a tela
/// ouve). Igual "testar microfone" do Discord: você se ouve, mas não vai ao ar.
static WAKE_TEST_MODE: AtomicBool = AtomicBool::new(false);

pub fn set_wake_test_mode(on: bool) {
    WAKE_TEST_MODE.store(on, Ordering::Relaxed);
}

fn wake_test_mode() -> bool {
    WAKE_TEST_MODE.load(Ordering::Relaxed)
}

/// Estado/config do detector, exposto aos comandos `wake_status`/`wake_start`.
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WakeStatus {
    pub running: bool,
    pub python: String,
    pub script: String,
    pub refs_dir: String,
    pub threshold: f64,
    pub cooldown_ms: u64,
}

#[derive(Clone)]
struct WakeConfig {
    python: String,
    script: String,
    /// Pasta com os `<slug>_ref.json` das frases cadastradas (few-shot).
    refs_dir: String,
    threshold: f64,
    /// Limiar proprio das frases "stop" ("pare"): recall > precisao, porque um
    /// falso positivo de stop so age quando o Neo esta falando/pensando.
    stop_threshold: f64,
    cooldown_ms: u64,
    /// Cooldown proprio (curto) dos slugs de interrupcao ("pare"). Separado do
    /// wake p/ um "Ei Neo" recente nao bloquear o "pare".
    stop_cooldown_ms: u64,
}

impl WakeConfig {
    /// Le a config do ambiente com defaults sensatos. Os caminhos default
    /// assumem o layout do repo com CWD = `src-tauri` (como no `tauri dev`);
    /// prefira caminhos absolutos via env num setup real.
    fn from_env() -> Self {
        let python = std::env::var("NEO_WAKE_PYTHON")
            .ok()
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "../sidecar/wake/.venv-eff/bin/python".to_string());
        let script = std::env::var("NEO_WAKE_SCRIPT")
            .ok()
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "../sidecar/wake/neo_wake_oww.py".to_string());
        let refs_dir = std::env::var("NEO_WAKE_REFS_DIR")
            .ok()
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| refs_dir().to_string_lossy().into_owned());
        // openWakeWord (ei_neo.onnx v3, treinado na 5070 Ti): na voz real do
        // usuario dispara em ~0.6-0.98 e os confusores ("ne o", "-aneo") sobem
        // ate ~0.9. 0.6 + a regra de N frames consecutivos do sidecar foi o
        // ponto testado no wrapper start-wake-oww.sh. Para voltar ao few-shot
        // antigo (ArcLoss cosine, limiar ~0.64), aponte NEO_WAKE_SCRIPT p/
        // neo_wake.py e NEO_WAKE_THRESHOLD=0.64.
        let threshold = std::env::var("NEO_WAKE_THRESHOLD")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(0.6);
        let stop_threshold = std::env::var("NEO_WAKE_STOP_THRESHOLD")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(0.64);
        let cooldown_ms = std::env::var("NEO_WAKE_COOLDOWN_MS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(3000);
        // Interrupcao precisa ser responsiva e poder repetir: cooldown curto.
        let stop_cooldown_ms = std::env::var("NEO_WAKE_STOP_COOLDOWN_MS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(800);
        Self {
            python,
            script,
            refs_dir,
            threshold,
            stop_threshold,
            cooldown_ms,
            stop_cooldown_ms,
        }
    }
}

// ---- Armazenamento das frases (amostras .wav + refs) --------------------
//
// Layout (macOS): ~/Library/Application Support/neo-desktop/wake/
//   manifest.json          -> [{slug, label}]
//   samples/<slug>/NN.wav   -> gravacoes (>=4 p/ cadastrar)
//   refs/<slug>_ref.json    -> referencia gerada (neo_wake_enroll.py)

/// Minimo de amostras exigido pelo EfficientWord-Net por frase.
pub const WAKE_MIN_SAMPLES: usize = 4;

/// Amostra esperada do mic, vinda do frontend: PCM mono 16kHz int16.
pub const WAKE_SAMPLE_RATE: u32 = 16000;

#[derive(Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct WakePhrase {
    pub slug: String,
    pub label: String,
    pub sample_count: usize,
    /// `true` quando a referencia (`<slug>_ref.json`) ja foi gerada.
    pub enrolled: bool,
    pub min_samples: usize,
    /// Papel da frase: `"wake"` (acende o Modo Live) ou `"stop"` (interrompe a
    /// fala/o turno em andamento). Default `"wake"` p/ compat.
    pub role: String,
}

/// Papel padrao de uma frase quando o manifest e antigo (sem o campo `role`).
fn default_role() -> String {
    "wake".to_string()
}

#[derive(Clone, Serialize, Deserialize)]
struct ManifestEntry {
    slug: String,
    label: String,
    #[serde(default = "default_role")]
    role: String,
}

/// Piso de nivel de uma amostra de cadastro. Calibrado com as amostras reais
/// (2026-07-20): boas ficam em pico 0.385-0.641 / rms 0.040-0.044; a mais fraca
/// que ainda funcionou, em 0.153 / 0.014; as ruins, em 0.037 / 0.003. Os limiares
/// ficam ENTRE a que funcionou e as ruins. Espelha `analyzeLevel` do frontend.
const MIN_SAMPLE_PEAK: f32 = 0.08;
const MIN_SAMPLE_RMS: f32 = 0.006;

/// (pico, rms) de um PCM int16, ambos normalizados em 0..1.
fn pcm_level(pcm: &[i16]) -> (f32, f32) {
    if pcm.is_empty() {
        return (0.0, 0.0);
    }
    let mut peak = 0f32;
    let mut sum_sq = 0f64;
    for &s in pcm {
        let n = s as f32 / 32768.0;
        let a = n.abs();
        if a > peak {
            peak = a;
        }
        sum_sq += (n as f64) * (n as f64);
    }
    (peak, (sum_sq / pcm.len() as f64).sqrt() as f32)
}

fn base_dir() -> PathBuf {
    dirs::config_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("neo-desktop")
        .join("wake")
}

fn refs_dir() -> PathBuf {
    base_dir().join("refs")
}

fn samples_dir() -> PathBuf {
    base_dir().join("samples")
}

fn manifest_path() -> PathBuf {
    base_dir().join("manifest.json")
}

fn read_manifest() -> Vec<ManifestEntry> {
    std::fs::read_to_string(manifest_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn write_manifest(entries: &[ManifestEntry]) -> Result<(), String> {
    let _ = std::fs::create_dir_all(base_dir());
    let json = serde_json::to_string_pretty(entries).map_err(|e| e.to_string())?;
    std::fs::write(manifest_path(), json).map_err(|e| e.to_string())
}

/// Slug seguro p/ nome de arquivo a partir de um rotulo livre.
fn slugify(label: &str) -> String {
    let mut out = String::new();
    let mut prev_us = false;
    for ch in label.trim().to_lowercase().chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch);
            prev_us = false;
        } else if !prev_us {
            out.push('_');
            prev_us = true;
        }
    }
    let s = out.trim_matches('_').to_string();
    if s.is_empty() {
        "frase".to_string()
    } else {
        s
    }
}

fn count_wavs(dir: &Path) -> usize {
    std::fs::read_dir(dir)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| {
                    e.path()
                        .extension()
                        .map(|x| x.eq_ignore_ascii_case("wav"))
                        .unwrap_or(false)
                })
                .count()
        })
        .unwrap_or(0)
}

fn phrase_status(entry: &ManifestEntry) -> WakePhrase {
    let sample_count = count_wavs(&samples_dir().join(&entry.slug));
    let enrolled = refs_dir().join(format!("{}_ref.json", entry.slug)).is_file();
    WakePhrase {
        slug: entry.slug.clone(),
        label: entry.label.clone(),
        sample_count,
        enrolled,
        min_samples: WAKE_MIN_SAMPLES,
        role: entry.role.clone(),
    }
}

/// Slugs das frases com papel `stop` ja cadastradas (ref gerada). Passados ao
/// sidecar p/ ele emitir `interrupt` (em vez de `wake`) quando casarem.
fn stop_slugs() -> Vec<String> {
    read_manifest()
        .into_iter()
        .filter(|e| e.role == "stop")
        .filter(|e| refs_dir().join(format!("{}_ref.json", e.slug)).is_file())
        .map(|e| e.slug)
        .collect()
}

/// Escreve um WAV PCM 16-bit mono 16kHz (header de 44 bytes + amostras).
fn write_wav_16k_mono(path: &Path, samples: &[i16]) -> std::io::Result<()> {
    let data_len = (samples.len() * 2) as u32;
    let mut buf = Vec::with_capacity(44 + data_len as usize);
    buf.extend_from_slice(b"RIFF");
    buf.extend_from_slice(&(36 + data_len).to_le_bytes());
    buf.extend_from_slice(b"WAVE");
    buf.extend_from_slice(b"fmt ");
    buf.extend_from_slice(&16u32.to_le_bytes()); // tamanho do chunk fmt
    buf.extend_from_slice(&1u16.to_le_bytes()); // PCM
    buf.extend_from_slice(&1u16.to_le_bytes()); // mono
    buf.extend_from_slice(&WAKE_SAMPLE_RATE.to_le_bytes());
    buf.extend_from_slice(&(WAKE_SAMPLE_RATE * 2).to_le_bytes()); // byte rate
    buf.extend_from_slice(&2u16.to_le_bytes()); // block align
    buf.extend_from_slice(&16u16.to_le_bytes()); // bits/sample
    buf.extend_from_slice(b"data");
    buf.extend_from_slice(&data_len.to_le_bytes());
    for s in samples {
        buf.extend_from_slice(&s.to_le_bytes());
    }
    std::fs::write(path, buf)
}

/// Le um WAV 16k mono int16 escrito por `write_wav_16k_mono` (cabecalho fixo de
/// 44 bytes -- nao e um parser geral de WAV, so o par do writer acima).
fn read_wav_16k_mono(path: &Path) -> std::io::Result<Vec<i16>> {
    let bytes = std::fs::read(path)?;
    if bytes.len() <= 44 {
        return Ok(Vec::new());
    }
    Ok(bytes[44..]
        .chunks_exact(2)
        .map(|c| i16::from_le_bytes([c[0], c[1]]))
        .collect())
}

/// Nivel de UMA amostra ja gravada, para a auditoria do cadastro.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SampleAudit {
    pub file: String,
    pub peak: f32,
    pub rms: f32,
    /// `false` = esta contaminando a referencia; regravar.
    pub ok: bool,
}

struct WakeState {
    running: bool,
    /// Incrementado a cada start/stop; identifica a sessao ativa.
    epoch: u64,
    /// Sinaliza `true` para o supervisor da sessao atual encerrar.
    stop_tx: Option<watch::Sender<bool>>,
}

/// Handle clonavel guardado no AppState. Supervisiona um unico sidecar.
#[derive(Clone)]
pub struct WakeManager {
    state: Arc<Mutex<WakeState>>,
    cfg: Arc<Mutex<WakeConfig>>,
}

impl Default for WakeManager {
    fn default() -> Self {
        Self::new()
    }
}

impl WakeManager {
    pub fn new() -> Self {
        Self {
            state: Arc::new(Mutex::new(WakeState {
                running: false,
                epoch: 0,
                stop_tx: None,
            })),
            cfg: Arc::new(Mutex::new(WakeConfig::from_env())),
        }
    }

    /// `true` se o env pede autostart (NEO_WAKE_ENABLED / NEO_WAKE_AUTOSTART truthy).
    pub fn autostart_requested() -> bool {
        let truthy =
            |v: String| matches!(v.trim().to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on");
        std::env::var("NEO_WAKE_ENABLED").map(truthy).unwrap_or(false)
            || std::env::var("NEO_WAKE_AUTOSTART").map(truthy).unwrap_or(false)
    }

    pub fn status(&self) -> WakeStatus {
        let running = self.state.lock().expect("wake state lock").running;
        let cfg = self.cfg.lock().expect("wake cfg lock").clone();
        WakeStatus {
            running,
            python: cfg.python,
            script: cfg.script,
            refs_dir: cfg.refs_dir,
            threshold: cfg.threshold,
            cooldown_ms: cfg.cooldown_ms,
        }
    }

    /// Inicia o supervisor (idempotente). Recarrega a config do env a cada start,
    /// para o toggle da UI refletir mudancas sem reiniciar o app.
    pub fn start(&self, app: AppHandle) {
        let cfg = WakeConfig::from_env();
        let (rx, my_epoch) = {
            let mut st = self.state.lock().expect("wake state lock");
            if st.running {
                return; // ja rodando
            }
            st.running = true;
            st.epoch += 1;
            let (tx, rx) = watch::channel(false);
            st.stop_tx = Some(tx);
            (rx, st.epoch)
        };
        *self.cfg.lock().expect("wake cfg lock") = cfg.clone();

        info!(
            "[Wake] iniciando sidecar: {} {} (refs={}, threshold={})",
            cfg.python, cfg.script, cfg.refs_dir, cfg.threshold
        );

        let state = self.state.clone();
        tauri::async_runtime::spawn(async move {
            let stopped_by_request = supervise(app, cfg, rx).await;
            // Se o supervisor saiu sozinho (falha fatal, nao um stop()), libera o
            // `running` para o usuario poder religar -- mas so se esta ainda for a
            // sessao corrente (epoch), senao clobbamos um start() mais novo.
            if !stopped_by_request {
                if let Ok(mut st) = state.lock() {
                    if st.epoch == my_epoch {
                        st.running = false;
                        st.stop_tx = None;
                    }
                }
            }
        });
    }

    /// Para o supervisor e mata o sidecar.
    pub fn stop(&self) {
        let mut st = self.state.lock().expect("wake state lock");
        if !st.running {
            return;
        }
        st.running = false;
        st.epoch += 1; // invalida a sessao que esta saindo
        if let Some(tx) = st.stop_tx.take() {
            let _ = tx.send(true);
        }
        info!("[Wake] parado");
    }

    // ---- Cadastro de frases (gravar amostras + gerar referencia) --------

    /// Lista as frases cadastradas com contagem de amostras e se ja tem ref.
    pub fn list_phrases(&self) -> Vec<WakePhrase> {
        read_manifest().iter().map(phrase_status).collect()
    }

    /// Audita o nivel das amostras JA gravadas de uma frase. A validacao no
    /// `add_sample` so protege o futuro; isto revela o passivo -- amostras mudas
    /// gravadas antes da trava existir, que seguem contaminando a referencia.
    pub fn audit_samples(&self, slug: &str) -> Vec<SampleAudit> {
        let dir = samples_dir().join(slug);
        let Ok(entries) = std::fs::read_dir(&dir) else {
            return Vec::new();
        };
        let mut files: Vec<_> = entries
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.extension().and_then(|x| x.to_str()) == Some("wav"))
            .collect();
        files.sort();

        files
            .iter()
            .map(|path| {
                let pcm = read_wav_16k_mono(path).unwrap_or_default();
                let (peak, rms) = pcm_level(&pcm);
                SampleAudit {
                    file: path
                        .file_name()
                        .and_then(|f| f.to_str())
                        .unwrap_or("?")
                        .to_string(),
                    peak,
                    rms,
                    ok: peak >= MIN_SAMPLE_PEAK && rms >= MIN_SAMPLE_RMS,
                }
            })
            .collect()
    }

    /// Salva uma amostra (PCM mono 16kHz int16, vinda do frontend) de uma frase.
    /// Cria a entrada no manifest na primeira amostra. `role` define se a frase
    /// acende o Live (`"wake"`) ou interrompe (`"stop"`). Retorna o estado da frase.
    pub fn add_sample(&self, label: String, pcm: Vec<i16>, role: String) -> Result<WakePhrase, String> {
        let label = label.trim().to_string();
        if label.is_empty() {
            return Err("rotulo da frase vazio".into());
        }
        if pcm.is_empty() {
            return Err("amostra de audio vazia".into());
        }
        // Trava de nivel (o frontend ja avisa antes, com mensagem melhor; isto e o
        // porteiro). Amostra muda nao e "uma amostra a menos": ela CONTAMINA a
        // referencia da frase, que e calculada em cima do conjunto. O cadastro
        // aceitava qualquer coisa e acumulou uma gravacao em silencio puro
        // (pico 0.001) + tres quase mudas (pico 0.037) -- ver medicao 2026-07-20.
        let (peak, rms) = pcm_level(&pcm);
        if peak < MIN_SAMPLE_PEAK || rms < MIN_SAMPLE_RMS {
            return Err(format!(
                "audio baixo demais para servir de referencia (pico {:.3}, rms {:.4}); \
                 fale mais perto do microfone e regrave",
                peak, rms
            ));
        }
        let role = match role.trim() {
            "stop" => "stop".to_string(),
            _ => "wake".to_string(),
        };
        let slug = slugify(&label);

        let mut manifest = read_manifest();
        if !manifest.iter().any(|e| e.slug == slug) {
            manifest.push(ManifestEntry {
                slug: slug.clone(),
                label: label.clone(),
                role,
            });
            write_manifest(&manifest)?;
        }

        let dir = samples_dir().join(&slug);
        std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
        // proximo indice livre (NN.wav)
        let mut idx = 1usize;
        while dir.join(format!("{:02}.wav", idx)).exists() {
            idx += 1;
        }
        write_wav_16k_mono(&dir.join(format!("{:02}.wav", idx)), &pcm)
            .map_err(|e| format!("falha ao salvar wav: {}", e))?;

        let entry = manifest
            .iter()
            .find(|e| e.slug == slug)
            .cloned()
            .unwrap_or(ManifestEntry {
                slug,
                label,
                role: default_role(),
            });
        Ok(phrase_status(&entry))
    }

    /// Apaga uma frase inteira (amostras + referencia + entrada no manifest).
    pub fn delete_phrase(&self, slug: &str) -> Result<(), String> {
        let _ = std::fs::remove_dir_all(samples_dir().join(slug));
        let _ = std::fs::remove_file(refs_dir().join(format!("{}_ref.json", slug)));
        let mut manifest = read_manifest();
        manifest.retain(|e| e.slug != slug);
        write_manifest(&manifest)
    }

    /// Gera a referencia (`<slug>_ref.json`) da frase rodando o neo_wake_enroll.py.
    /// Bloqueia ate terminar (e rapido). Requer >= WAKE_MIN_SAMPLES amostras.
    pub async fn enroll(&self, slug: &str) -> Result<WakePhrase, String> {
        let entry = read_manifest()
            .into_iter()
            .find(|e| e.slug == slug)
            .ok_or_else(|| format!("frase '{}' nao encontrada", slug))?;

        let samples = samples_dir().join(slug);
        let n = count_wavs(&samples);
        if n < WAKE_MIN_SAMPLES {
            return Err(format!(
                "a frase precisa de pelo menos {} amostras (tem {}).",
                WAKE_MIN_SAMPLES, n
            ));
        }
        let refs = refs_dir();
        std::fs::create_dir_all(&refs).map_err(|e| e.to_string())?;

        let cfg = WakeConfig::from_env();
        let enroll_script = enroll_script_path(&cfg.script);

        let output = Command::new(&cfg.python)
            .arg(&enroll_script)
            .env("NEO_WAKE_ENROLL_SAMPLES", &samples)
            .env("NEO_WAKE_ENROLL_OUT", &refs)
            .env("NEO_WAKE_ENROLL_WORD", slug)
            .env("PYTHONUNBUFFERED", "1")
            .output()
            .await
            .map_err(|e| format!("falha ao rodar o enroll ({}): {}", cfg.python, e))?;

        let stdout = String::from_utf8_lossy(&output.stdout);
        // Procura a linha JSON de evento (o EfficientWord-Net imprime barras de progresso).
        let mut enrolled_ok = false;
        for line in stdout.lines() {
            let line = line.trim();
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                match v.get("event").and_then(|e| e.as_str()) {
                    Some("enrolled") => enrolled_ok = true,
                    Some("error") => {
                        return Err(v
                            .get("message")
                            .and_then(|m| m.as_str())
                            .unwrap_or("erro no enroll")
                            .to_string());
                    }
                    _ => {}
                }
            }
        }
        if !enrolled_ok {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(format!(
                "enroll nao confirmou (code {:?}). stderr: {}",
                output.status.code(),
                stderr.trim()
            ));
        }
        Ok(phrase_status(&entry))
    }
}

/// Caminho do neo_wake_enroll.py: irmao do script de deteccao.
fn enroll_script_path(detect_script: &str) -> PathBuf {
    let p = Path::new(detect_script);
    match p.parent() {
        Some(dir) => dir.join("neo_wake_enroll.py"),
        None => PathBuf::from("neo_wake_enroll.py"),
    }
}

/// Loop de supervisao. Retorna `true` se encerrou por pedido (stop), `false` se
/// foi uma falha fatal (ex.: sidecar nao pode ser iniciado).
async fn supervise(app: AppHandle, cfg: WakeConfig, mut rx: watch::Receiver<bool>) -> bool {
    loop {
        if *rx.borrow() {
            return true;
        }

        let mut child = match spawn_sidecar(&cfg) {
            Ok(c) => c,
            Err(e) => {
                error!("[Wake] falha ao iniciar o sidecar ({}): {}", cfg.python, e);
                emit_status(
                    &app,
                    "error",
                    &format!(
                        "nao foi possivel iniciar o sidecar ({}): {}. Confira NEO_WAKE_PYTHON/SCRIPT e o venv.",
                        cfg.python, e
                    ),
                );
                return false; // desiste: provavel config errada; a UI religa apos corrigir.
            }
        };

        // Drena o stderr do sidecar para o log (erros do Python aparecem aqui).
        if let Some(stderr) = child.stderr.take() {
            tauri::async_runtime::spawn(async move {
                let mut lines = BufReader::new(stderr).lines();
                while let Ok(Some(line)) = lines.next_line().await {
                    warn!("[Wake/py] {}", line);
                }
            });
        }

        // Empurra o estado de TTS ao sidecar (stdin): escreve o valor atual na hora
        // e a cada mudanca, para ele rodar so o grupo de detectores relevante.
        if let Some(mut stdin) = child.stdin.take() {
            let mut rx = tts_state_tx().subscribe();
            tauri::async_runtime::spawn(async move {
                loop {
                    let speaking = *rx.borrow_and_update();
                    let line = if speaking {
                        "{\"tts\": true}\n"
                    } else {
                        "{\"tts\": false}\n"
                    };
                    if stdin.write_all(line.as_bytes()).await.is_err() {
                        break; // sidecar saiu
                    }
                    let _ = stdin.flush().await;
                    if rx.changed().await.is_err() {
                        break; // sender global nunca dropa; guard defensivo
                    }
                }
            });
        }

        let stdout = match child.stdout.take() {
            Some(s) => s,
            None => {
                error!("[Wake] sidecar sem stdout");
                let _ = child.kill().await;
                let _ = child.wait().await;
                return false;
            }
        };
        let mut lines = BufReader::new(stdout).lines();

        // Le linhas ate EOF (sidecar morreu) ou ate stop().
        loop {
            tokio::select! {
                changed = rx.changed() => {
                    // `watch` retem o ultimo valor: nada de wakeup perdido.
                    // Err = sender dropado (app encerrando) -> tratamos como stop.
                    if changed.is_err() || *rx.borrow() {
                        let _ = child.kill().await;
                        let _ = child.wait().await;
                        return true;
                    }
                }
                next = lines.next_line() => {
                    match next {
                        Ok(Some(line)) => handle_line(&app, &line),
                        Ok(None) => break,            // EOF -> sidecar saiu
                        Err(e) => { warn!("[Wake] erro lendo stdout: {}", e); break; }
                    }
                }
            }
        }

        // Sidecar caiu (apos ter iniciado): reap e reinicia com backoff de 2s.
        let _ = child.wait().await;
        if *rx.borrow() {
            return true;
        }
        warn!("[Wake] sidecar encerrou; reiniciando em 2s");
        tokio::select! {
            changed = rx.changed() => {
                if changed.is_err() || *rx.borrow() {
                    return true;
                }
            }
            _ = tokio::time::sleep(Duration::from_secs(2)) => {}
        }
    }
}

/// Microfone do sidecar. Ele segura o mic 24/7 (`sd.InputStream` num loop), e
/// abrir o mic de um fone Bluetooth derruba o dispositivo de A2DP p/ HFP/SCO --
/// ou seja, o fone tocaria musica em qualidade de telefone o tempo todo so
/// porque o Neo esta esperando "Ei Neo". Quando o mic padrao e Bluetooth,
/// fixamos o sidecar no mic INTERNO (o PortAudio casa o dispositivo por nome, o
/// mesmo que o CoreAudio reporta). `NEO_WAKE_INPUT_DEVICE` explicito ganha.
fn wake_input_device() -> Option<String> {
    if let Ok(explicit) = std::env::var("NEO_WAKE_INPUT_DEVICE") {
        let explicit = explicit.trim().to_string();
        if !explicit.is_empty() {
            return Some(explicit);
        }
    }
    let route = crate::services::audio_route::resolve();
    let name = route.preferred_input_name()?.to_string();
    info!(
        "[Wake] mic padrao e Bluetooth; fixando o sidecar em \"{}\" p/ manter o fone em A2DP",
        name
    );
    Some(name)
}

fn spawn_sidecar(cfg: &WakeConfig) -> std::io::Result<Child> {
    let mut cmd = Command::new(&cfg.python);
    if let Some(device) = wake_input_device() {
        cmd.env("NEO_WAKE_INPUT_DEVICE", device);
    }
    cmd.arg(&cfg.script)
        .env("NEO_WAKE_REFS_DIR", &cfg.refs_dir)
        .env("NEO_WAKE_THRESHOLD", cfg.threshold.to_string())
        .env("NEO_WAKE_STOP_THRESHOLD", cfg.stop_threshold.to_string())
        .env("NEO_WAKE_COOLDOWN_MS", cfg.cooldown_ms.to_string())
        // Slugs de interrupcao ("pare"): o sidecar emite `interrupt` (nao `wake`)
        // quando casarem, e os scoreia com cooldown proprio + unsafe=True.
        .env("NEO_WAKE_STOP_SLUGS", stop_slugs().join(","))
        .env("NEO_WAKE_STOP_COOLDOWN_MS", cfg.stop_cooldown_ms.to_string())
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    cmd.spawn()
}

/// Converte uma linha JSON do sidecar em evento Tauri / log.
fn handle_line(app: &AppHandle, line: &str) {
    let line = line.trim();
    if line.is_empty() {
        return;
    }
    let val: serde_json::Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(_) => {
            warn!("[Wake/py] {}", line); // nao-JSON: trata como log
            return;
        }
    };
    match val.get("event").and_then(|v| v.as_str()) {
        Some("wake") => {
            let model = val.get("model").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let score = val.get("score").and_then(|v| v.as_f64()).unwrap_or(0.0);
            if wake_test_mode() {
                // Modo teste: mostra o disparo na tela, mas NAO aciona o Live.
                info!("[Wake] disparo (TESTE, sem acionar): model={} score={:.3}", model, score);
                let _ = app.emit("wake-test-fire", WakeDetected { model, score });
                return;
            }
            if wake_suppressed() {
                info!(
                    "[Wake] disparo SUPRIMIDO (Neo falando): model={} score={:.3}",
                    model, score
                );
                return;
            }
            info!("[Wake] disparo: model={} score={:.3}", model, score);
            if let Err(e) = app.emit("wake-detected", WakeDetected { model, score }) {
                warn!("[Wake] falha ao emitir wake-detected: {}", e);
            }
        }
        Some("score") => {
            // Monitor da tela "Teste de Wake": score por frame tipo-wake (inclui
            // near-misses). Frequente, entao NAO loga -- so repassa ao frontend.
            let model = val.get("model").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let score = val.get("score").and_then(|v| v.as_f64()).unwrap_or(0.0);
            let _ = app.emit("wake-score", WakeDetected { model, score });
        }
        Some("interrupt") => {
            let model = val.get("model").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let score = val.get("score").and_then(|v| v.as_f64()).unwrap_or(0.0);
            info!("[Wake] interrupcao: model={} score={:.3}", model, score);
            if let Err(e) = app.emit("wake-interrupt", WakeDetected { model, score }) {
                warn!("[Wake] falha ao emitir wake-interrupt: {}", e);
            }
        }
        Some("ready") => info!("[Wake] sidecar pronto: {}", val),
        Some("error") => {
            let msg = val.get("message").and_then(|v| v.as_str()).unwrap_or("").to_string();
            error!("[Wake] sidecar reportou erro: {}", msg);
            emit_status(app, "error", &msg);
        }
        Some("log") => info!(
            "[Wake/py] {}",
            val.get("message").and_then(|v| v.as_str()).unwrap_or("")
        ),
        _ => warn!("[Wake/py] evento desconhecido: {}", line),
    }
}

/// Notifica o frontend sobre estado do sidecar (ex.: erro de inicializacao),
/// para a UI poder avisar o usuario sem precisar abrir o log.
fn emit_status(app: &AppHandle, kind: &str, message: &str) {
    let _ = app.emit(
        "wake-status",
        serde_json::json!({ "kind": kind, "message": message }),
    );
}

#[cfg(test)]
mod level_tests {
    use super::*;

    /// Gera PCM senoidal com o pico pedido (proxy simples de "tem sinal").
    fn tone(peak: f32, n: usize) -> Vec<i16> {
        (0..n)
            .map(|i| {
                let t = i as f32 / 16000.0;
                ((t * 440.0 * std::f32::consts::TAU).sin() * peak * 32767.0) as i16
            })
            .collect()
    }

    #[test]
    fn level_matches_measured_values() {
        let (peak, rms) = pcm_level(&tone(0.5, 16000));
        assert!((peak - 0.5).abs() < 0.01, "pico medido {peak}");
        // RMS de uma senoide = pico/sqrt(2)
        assert!((rms - 0.5 / 2f32.sqrt()).abs() < 0.01, "rms medido {rms}");
        assert_eq!(pcm_level(&[]), (0.0, 0.0));
    }

    /// Os limiares tem de SEPARAR as amostras reais medidas em 2026-07-20:
    /// as ruins (pico 0.037 e 0.001) reprovam, a fraca-que-funcionou (0.153) passa.
    #[test]
    fn thresholds_separate_real_samples() {
        let bad_silent = pcm_level(&tone(0.001, 16000));
        let bad_quiet = pcm_level(&tone(0.037, 16000));
        let weak_ok = pcm_level(&tone(0.153, 16000));
        let good = pcm_level(&tone(0.55, 16000));

        let passes = |(p, r): (f32, f32)| p >= MIN_SAMPLE_PEAK && r >= MIN_SAMPLE_RMS;

        assert!(!passes(bad_silent), "amostra MUDA deveria reprovar");
        assert!(!passes(bad_quiet), "amostra quase muda deveria reprovar");
        assert!(passes(weak_ok), "a fraca que funcionou deveria passar");
        assert!(passes(good), "amostra boa deveria passar");
    }

    #[test]
    fn add_sample_rejects_silent_audio() {
        let svc = WakeManager::default();
        let res = svc.add_sample("teste nivel".into(), tone(0.001, 16000), "wake".into());
        let err = match res {
            Err(e) => e,
            Ok(_) => panic!("amostra muda foi ACEITA — a trava nao funcionou"),
        };
        assert!(err.contains("baixo demais"), "erro inesperado: {err}");
    }
}

#[cfg(test)]
mod audit_real_samples {
    use super::*;

    /// Roda a auditoria contra o cadastro REAL do usuario e imprime o veredito.
    /// Nao afirma nada sobre o conteudo (a maquina de outra pessoa nao tem esses
    /// arquivos) -- serve p/ enxergar o passivo com o mesmo codigo que a UI usa.
    #[test]
    fn report_existing_enrollment() {
        let svc = WakeManager::default();
        let mut total = 0;
        let mut bad = 0;
        for entry in read_manifest() {
            let audits = svc.audit_samples(&entry.slug);
            if audits.is_empty() {
                continue;
            }
            println!("\n{} ({})", entry.label, entry.slug);
            for a in &audits {
                total += 1;
                if !a.ok {
                    bad += 1;
                }
                println!(
                    "  {} {}  pico={:.3} rms={:.4}",
                    if a.ok { "OK  " } else { "RUIM" },
                    a.file,
                    a.peak,
                    a.rms
                );
            }
        }
        println!("\n=> {bad} de {total} amostras precisam ser regravadas");
    }
}
