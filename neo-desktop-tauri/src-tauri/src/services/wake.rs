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
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::watch;

/// Payload do evento Tauri `wake-detected` enviado ao frontend.
#[derive(Clone, Serialize)]
pub struct WakeDetected {
    pub model: String,
    pub score: f64,
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
    cooldown_ms: u64,
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
            .unwrap_or_else(|| "../sidecar/wake/neo_wake.py".to_string());
        let refs_dir = std::env::var("NEO_WAKE_REFS_DIR")
            .ok()
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| refs_dir().to_string_lossy().into_owned());
        // Few-shot (ArcLoss cosine): 0.7 e um ponto de partida razoavel.
        let threshold = std::env::var("NEO_WAKE_THRESHOLD")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(0.7);
        let cooldown_ms = std::env::var("NEO_WAKE_COOLDOWN_MS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(3000);
        Self {
            python,
            script,
            refs_dir,
            threshold,
            cooldown_ms,
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
}

#[derive(Clone, Serialize, Deserialize)]
struct ManifestEntry {
    slug: String,
    label: String,
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
    }
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

    /// Salva uma amostra (PCM mono 16kHz int16, vinda do frontend) de uma frase.
    /// Cria a entrada no manifest na primeira amostra. Retorna o estado da frase.
    pub fn add_sample(&self, label: String, pcm: Vec<i16>) -> Result<WakePhrase, String> {
        let label = label.trim().to_string();
        if label.is_empty() {
            return Err("rotulo da frase vazio".into());
        }
        if pcm.is_empty() {
            return Err("amostra de audio vazia".into());
        }
        let slug = slugify(&label);

        let mut manifest = read_manifest();
        if !manifest.iter().any(|e| e.slug == slug) {
            manifest.push(ManifestEntry {
                slug: slug.clone(),
                label: label.clone(),
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
            .unwrap_or(ManifestEntry { slug, label });
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

fn spawn_sidecar(cfg: &WakeConfig) -> std::io::Result<Child> {
    let mut cmd = Command::new(&cfg.python);
    cmd.arg(&cfg.script)
        .env("NEO_WAKE_REFS_DIR", &cfg.refs_dir)
        .env("NEO_WAKE_THRESHOLD", cfg.threshold.to_string())
        .env("NEO_WAKE_COOLDOWN_MS", cfg.cooldown_ms.to_string())
        .env("PYTHONUNBUFFERED", "1")
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
            info!("[Wake] disparo: model={} score={:.3}", model, score);
            if let Err(e) = app.emit("wake-detected", WakeDetected { model, score }) {
                warn!("[Wake] falha ao emitir wake-detected: {}", e);
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
