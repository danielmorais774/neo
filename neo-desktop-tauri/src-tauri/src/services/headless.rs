//! Navegador headless LOCAL — Chromium na máquina do usuário, sem janela.
//!
//! É o mesmo CDP que o `browser_local` já fala; o que muda é só **como se chega
//! nele**. Hoje o caminho é longo e passa pela aba que o usuário está olhando:
//!
//! ```text
//!  browser_local ──socket──▶ host obu ──native msg──▶ extensão ──chrome.debugger──▶ aba VISÍVEL
//!  headless (aqui) ─────────── WebSocket CDP direto ───────────────────────────▶ aba SEM JANELA
//! ```
//!
//! Por que existe: o navegador do SERVIDOR sai por IP de datacenter na França e
//! a web brasileira recusa (medido em 2026-09-20 — CDN do portal de dados devolve 403 para o
//! ASN inteiro, Google e DuckDuckGo servem CAPTCHA). O navegador do usuário
//! resolve, mas usar a janela dele é invasivo — por isso o portão pede licença.
//! Headless na máquina dele tem o IP certo **sem tomar a tela**.
//!
//! O que NÃO muda: o vocabulário de 20 ações, os ~1.100 linhas de JS de página
//! (`js_snapshot` e companhia) e a marcação de imagem continuam valendo iguais —
//! eles rodam por `Runtime.evaluate`, que é o mesmo dos dois lados.
//!
//! ## Memória
//!
//! Chromium não é barato e isto roda no laptop dele, então há teto. A métrica é
//! `phys_footprint` (o que o Activity Monitor chama "Memory"), somada na árvore
//! de processos — **não** soma de RSS. Medido: uma instância com 18 processos
//! deu 2.227 MB de RSS somado contra ~300 MB no maior processo, porque RSS conta
//! os frameworks compartilhados uma vez por processo. Um limitador que lesse RSS
//! estrangularia a ~4× da realidade.
//!
//! Duas coisas que quebram a intuição e moldam a política de liberação:
//! aba ≠ processo (Chromium é process-per-site, abas do mesmo site dividem), e
//! fechar aba não devolve memória na hora. Por isso o último degrau é reiniciar
//! o navegador inteiro — é o único que devolve de verdade, e é barato porque o
//! perfil persiste.

use log::{info, warn};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::{connect_async, tungstenite::Message, MaybeTlsStream, WebSocketStream};

type Socket = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;

/// Teto de uma chamada CDP. O passo inteiro tem prazo do lado do core; este só
/// impede que uma chamada pendurada segure o mutex para sempre.
const CALL_TIMEOUT: Duration = Duration::from_secs(30);

/// Quanto esperar o navegador escrever o `DevToolsActivePort` no boot.
const BOOT_TIMEOUT: Duration = Duration::from_secs(20);

/// UA sem a palavra `HeadlessChrome`.
///
/// Sem isto o headless é **mais fácil de barrar que um curl bem-feito**: a
/// string `HeadlessChrome/…` é a primeira coisa que anti-bot procura, e ela vem
/// no `navigator.userAgent` por padrão (medido em 2026-09-20 no Edge 153).
const SPOOFED_UA: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
     AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36";

// ─── Config ─────────────────────────────────────────────────────────────────

/// Qual perfil de navegador usar.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Profile {
    /// Perfil próprio, limpo, com sync desligado. **Padrão.**
    ///
    /// Medido em 2026-09-20: um `--user-data-dir` novo do Edge no macOS
    /// SINCRONIZOU a conta do usuário sozinho — o perfil apareceu com as abas
    /// e a identidade dele, 477 MB em disco. O agente navegando logado como a
    /// pessoa é decisão dela, não acidente de flag.
    Dedicated,
    /// Perfil do usuário: sites logados funcionam, e o agente age como ele.
    UserProfile,
}

#[derive(Clone, Debug)]
pub struct Limits {
    /// Abas simultâneas. `0` = sem teto próprio.
    pub max_tabs: usize,
    /// Teto de `phys_footprint` da árvore, em MB. `0` = sem teto próprio.
    pub max_ram_mb: u64,
    /// Segundos sem uso até uma aba ser fechada.
    pub idle_ttl_s: u64,
    pub profile: Profile,
    /// Caminho explícito do binário; `None` = descobrir.
    pub browser_path: Option<String>,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_tabs: 3,
            max_ram_mb: 1500,
            idle_ttl_s: 180,
            profile: Profile::Dedicated,
            browser_path: None,
        }
    }
}

static LIMITS: OnceLock<std::sync::Mutex<Limits>> = OnceLock::new();

fn limits_slot() -> &'static std::sync::Mutex<Limits> {
    LIMITS.get_or_init(|| std::sync::Mutex::new(Limits::default()))
}

pub fn limits() -> Limits {
    limits_slot().lock().map(|g| g.clone()).unwrap_or_default()
}

pub fn set_limits(next: Limits) {
    if let Ok(mut g) = limits_slot().lock() {
        info!(
            "[headless] limites: {} aba(s), {} MB, ttl {}s, perfil {:?}",
            next.max_tabs, next.max_ram_mb, next.idle_ttl_s, next.profile
        );
        *g = next;
    }
}

// ─── Memória (macOS: phys_footprint, não RSS) ───────────────────────────────

#[cfg(target_os = "macos")]
fn phys_footprint_mb(pid: i32) -> Option<u64> {
    use std::mem::MaybeUninit;
    let mut info = MaybeUninit::<libc::rusage_info_v2>::zeroed();
    // SAFETY: o kernel preenche a struct do flavor pedido; V2 é o que tem
    // `ri_phys_footprint`, e a struct está zerada antes da chamada.
    let rc = unsafe {
        libc::proc_pid_rusage(
            pid,
            libc::RUSAGE_INFO_V2,
            info.as_mut_ptr() as *mut libc::rusage_info_t,
        )
    };
    if rc != 0 {
        return None;
    }
    let info = unsafe { info.assume_init() };
    Some(info.ri_phys_footprint / (1024 * 1024))
}

#[cfg(not(target_os = "macos"))]
fn phys_footprint_mb(_pid: i32) -> Option<u64> {
    None
}

#[cfg(target_os = "macos")]
fn child_pids(ppid: i32) -> Vec<i32> {
    // SAFETY: primeira chamada só mede; a segunda preenche o buffer do tamanho
    // devolvido. `proc_listchildpids` escreve `pid_t` (i32).
    let count = unsafe { libc::proc_listchildpids(ppid, std::ptr::null_mut(), 0) };
    if count <= 0 {
        return Vec::new();
    }
    let mut buf = vec![0i32; count as usize];
    let bytes = (buf.len() * std::mem::size_of::<i32>()) as libc::c_int;
    let got = unsafe {
        libc::proc_listchildpids(ppid, buf.as_mut_ptr() as *mut libc::c_void, bytes)
    };
    if got <= 0 {
        return Vec::new();
    }
    buf.truncate(got as usize);
    buf.into_iter().filter(|p| *p > 0).collect()
}

#[cfg(not(target_os = "macos"))]
fn child_pids(_ppid: i32) -> Vec<i32> {
    Vec::new()
}

/// `phys_footprint` somado na árvore inteira (o pai e os renderers).
///
/// Percorre em largura com teto de profundidade: Chromium cria processos em
/// dois níveis (browser → zygote → renderers) e uma recursão sem teto num
/// processo que reparenta vira laço.
fn tree_footprint_mb(root: i32) -> u64 {
    let mut total = phys_footprint_mb(root).unwrap_or(0);
    let mut fila = vec![(root, 0u8)];
    let mut vistos = vec![root];
    while let Some((pid, nivel)) = fila.pop() {
        if nivel >= 3 {
            continue;
        }
        for filho in child_pids(pid) {
            if vistos.contains(&filho) {
                continue;
            }
            vistos.push(filho);
            total += phys_footprint_mb(filho).unwrap_or(0);
            fila.push((filho, nivel + 1));
        }
    }
    total
}

// ─── Descoberta do binário ──────────────────────────────────────────────────

/// Candidatos em ordem de preferência.
///
/// Brave antes do Edge de propósito: medido em 2026-09-20, o bundle do Edge tem
/// 1,1 GB e o perfil novo dele chegou a 477 MB (`component_crx_cache` 188 MB +
/// `ProvenanceData` 177 MB) — inchaço do Edge, não do headless.
#[cfg(target_os = "macos")]
const CANDIDATES: &[&str] = &[
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
];

#[cfg(target_os = "windows")]
const CANDIDATES: &[&str] = &[
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
];

#[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
const CANDIDATES: &[&str] = &[
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/brave-browser",
];

fn find_browser(explicit: Option<&str>) -> Result<PathBuf, String> {
    if let Some(p) = explicit.filter(|s| !s.trim().is_empty()) {
        let path = PathBuf::from(p);
        if path.exists() {
            return Ok(path);
        }
        return Err(format!("navegador configurado não existe: {}", p));
    }
    for c in CANDIDATES {
        let path = PathBuf::from(c);
        if path.exists() {
            return Ok(path);
        }
    }
    Err("não achei Chrome, Brave, Chromium nem Edge instalado nesta máquina \
         — o navegador headless precisa de um deles"
        .to_string())
}

fn profile_dir(profile: Profile) -> Result<PathBuf, String> {
    match profile {
        Profile::Dedicated => {
            let base = dirs::data_local_dir()
                .or_else(dirs::home_dir)
                .ok_or_else(|| "não achei onde guardar o perfil".to_string())?;
            let dir = base.join("neo-desktop").join("headless-profile");
            std::fs::create_dir_all(&dir)
                .map_err(|e| format!("não consegui criar o perfil: {}", e))?;
            Ok(dir)
        }
        Profile::UserProfile => {
            // O perfil real do usuário não pode ser aberto por DOIS Chromium ao
            // mesmo tempo — o segundo recusa e a sessão dele pode corromper.
            // Copiar também é caro e desatualiza. Então: perfil dedicado com
            // uma marca de que o usuário PEDIU o logado, e o login acontece uma
            // vez, por ele, na primeira vez.
            let base = dirs::data_local_dir()
                .or_else(dirs::home_dir)
                .ok_or_else(|| "não achei onde guardar o perfil".to_string())?;
            let dir = base.join("neo-desktop").join("headless-profile-logado");
            std::fs::create_dir_all(&dir)
                .map_err(|e| format!("não consegui criar o perfil: {}", e))?;
            Ok(dir)
        }
    }
}


/// Limpa o lock de perfil deixado por um navegador que morreu mal.
///
/// O Chromium marca o perfil com `SingletonLock`, um symlink cujo alvo é
/// `hostname-pid`. Enquanto ele existir apontando para um processo VIVO, uma
/// segunda instância recusa o mesmo `--user-data-dir` — e recusa em silêncio:
/// não escreve `DevToolsActivePort`, e do lado de cá isso aparece como
/// "o navegador não abriu a porta de depuração a tempo".
///
/// Isso não é cenário de teste. A sessão vive num `static`, então ela nunca é
/// dropada: um panic ou um `kill -9` no app deixa o navegador órfão segurando o
/// lock, e daí em diante TODA subida falha até alguém limpar à mão. Foi
/// exatamente o que aconteceu ao rodar os testes desta suíte.
///
/// Só remove quando o pid do alvo está morto. Lock de processo vivo é
/// legítimo — e apagá-lo seria pisar em cima de uma sessão que está usando o
/// perfil.
fn clear_stale_profile_lock(dir: &Path) {
    let lock = dir.join("SingletonLock");
    let Ok(alvo) = std::fs::read_link(&lock) else {
        return;
    };
    let alvo = alvo.to_string_lossy().to_string();
    let Some(pid) = alvo.rsplit('-').next().and_then(|p| p.parse::<i32>().ok()) else {
        return;
    };
    // SAFETY: sinal 0 não envia nada — só pergunta se o processo existe e se
    // teríamos permissão de sinalizá-lo.
    let vivo = unsafe { libc::kill(pid, 0) } == 0;
    if vivo {
        // Órfão VIVO. O perfil dedicado é exclusivamente nosso — ninguém mais
        // abre este `--user-data-dir` —, então um navegador de pé nele sem
        // sessão nossa é sobra de um crash. Deixá-lo em paz seria "correto" e
        // inútil: ele segura o lock e TODA subida futura falha, para sempre,
        // até alguém matar à mão. Derrubar é o que torna a feature capaz de se
        // recuperar sozinha.
        warn!(
            "[headless] navegador órfão (pid {}) segurando o perfil — derrubando",
            pid
        );
        // SAFETY: sinais para um pid que acabamos de confirmar existir. TERM
        // primeiro para ele fechar o perfil direito; KILL só se insistir.
        unsafe { libc::kill(pid, libc::SIGTERM) };
        for _ in 0..20 {
            std::thread::sleep(Duration::from_millis(100));
            if unsafe { libc::kill(pid, 0) } != 0 {
                break;
            }
        }
        if unsafe { libc::kill(pid, 0) } == 0 {
            unsafe { libc::kill(pid, libc::SIGKILL) };
            std::thread::sleep(Duration::from_millis(200));
        }
    } else {
        warn!(
            "[headless] lock órfão do pid {} no perfil — limpando para poder subir",
            pid
        );
    }
    for nome in ["SingletonLock", "SingletonSocket", "SingletonCookie"] {
        let _ = std::fs::remove_file(dir.join(nome));
    }
}

// ─── Sessão ─────────────────────────────────────────────────────────────────

struct Tab {
    id: i64,
    target_id: String,
    session_id: String,
    last_used: Instant,
    url: String,
    /// A aba do gerente que a abriu (link `_blank`, `window.open`). `None` = o
    /// gerente abriu. Antes estas abas nem entravam aqui: ficavam fora do teto
    /// de abas e da ociosidade, e só morriam no reinício pelo teto de RAM.
    opener: Option<i64>,
}

struct Headless {
    child: tokio::process::Child,
    pid: i32,
    ws: Socket,
    next_cdp_id: u64,
    next_tab_id: i64,
    tabs: Vec<Tab>,
    started: Instant,
    binario: String,
}

static SESSION: OnceLock<Mutex<Option<Headless>>> = OnceLock::new();

fn session_slot() -> &'static Mutex<Option<Headless>> {
    SESSION.get_or_init(|| Mutex::new(None))
}

/// O navegador escreve porta e caminho do WS aqui assim que sobe.
async fn read_devtools_endpoint(dir: &Path) -> Result<String, String> {
    let arquivo = dir.join("DevToolsActivePort");
    let prazo = Instant::now() + BOOT_TIMEOUT;
    loop {
        if let Ok(raw) = std::fs::read_to_string(&arquivo) {
            let mut linhas = raw.lines();
            let porta = linhas.next().unwrap_or("").trim().to_string();
            let caminho = linhas.next().unwrap_or("").trim().to_string();
            if !porta.is_empty() && !caminho.is_empty() {
                return Ok(format!("ws://127.0.0.1:{}{}", porta, caminho));
            }
        }
        if Instant::now() >= prazo {
            return Err(
                "o navegador não abriu a porta de depuração a tempo (DevToolsActivePort \
                 não apareceu)"
                    .to_string(),
            );
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

impl Headless {
    async fn launch() -> Result<Self, String> {
        let cfg = limits();
        let bin = find_browser(cfg.browser_path.as_deref())?;
        let dir = profile_dir(cfg.profile)?;
        // Um `DevToolsActivePort` velho faria a gente falar com um navegador
        // morto; apagar antes é o que garante que a porta lida é a desta boot.
        let _ = std::fs::remove_file(dir.join("DevToolsActivePort"));
        clear_stale_profile_lock(&dir);

        let mut cmd = tokio::process::Command::new(&bin);
        cmd.arg("--headless=new")
            .arg("--remote-debugging-port=0")
            .arg(format!("--user-data-dir={}", dir.display()))
            .arg(format!("--user-agent={}", SPOOFED_UA))
            // Sem isto, `navigator.webdriver` vem `true` e entrega a automação
            // na primeira linha de qualquer anti-bot. Medido em 2026-09-20 no
            // Brave 153: com o flag, `false`. Junto com o UA acima, é o que
            // impede o headless de ser MAIS fácil de barrar que um curl bem
            // feito — que seria o cúmulo, já que ele existe para passar onde o
            // curl não passa.
            .arg("--disable-blink-features=AutomationControlled")
            .arg("--no-first-run")
            .arg("--no-default-browser-check")
            .arg("--disable-gpu")
            .arg("--window-size=1280,900")
            // Sync ligado foi o que trouxe a conta do usuário para dentro de um
            // perfil que devia ser limpo (medido: 477 MB e as abas dele).
            .arg("--disable-sync")
            // Corta o que engorda o perfil e não serve para automação.
            .arg("--disable-component-update")
            .arg("--disable-background-networking")
            .arg("--disable-breakpad")
            .arg("--disable-domain-reliability")
            .arg("--no-service-autorun")
            .arg("--password-store=basic")
            .arg("--use-mock-keychain")
            // Menos ruído de render sem janela.
            .arg("--disable-dev-shm-usage")
            .arg("--disable-backgrounding-occluded-windows")
            .arg("--disable-renderer-backgrounding")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true);

        let child = cmd
            .spawn()
            .map_err(|e| format!("não consegui abrir o navegador {}: {}", bin.display(), e))?;
        let pid = child.id().unwrap_or(0) as i32;

        let endpoint = match read_devtools_endpoint(&dir).await {
            Ok(u) => u,
            Err(e) => {
                let mut c = child;
                let _ = c.kill().await;
                return Err(e);
            }
        };

        // `connect_async` não manda `Origin`, e é bom que não mande: o CDP
        // recusa handshake com origem (403 "Rejected an incoming WebSocket
        // connection ... Use --remote-allow-origins").
        let (ws, _) = match connect_async(&endpoint).await {
            Ok(v) => v,
            Err(e) => {
                let mut c = child;
                let _ = c.kill().await;
                return Err(format!("não consegui falar CDP com o navegador: {}", e));
            }
        };

        info!(
            "[headless] {} de pé (pid {}, perfil {})",
            bin.display(),
            pid,
            dir.display()
        );
        Ok(Headless {
            child,
            pid,
            ws,
            next_cdp_id: 1,
            next_tab_id: 1,
            tabs: Vec::new(),
            started: Instant::now(),
            binario: bin.display().to_string(),
        })
    }

    /// Uma chamada CDP. Eventos que chegarem no meio são descartados até a
    /// resposta com o nosso id (protocolo "flat": a sessão vai no envelope).
    async fn request(
        &mut self,
        method: &str,
        params: Value,
        session_id: Option<&str>,
    ) -> Result<Value, String> {
        let id = self.next_cdp_id;
        self.next_cdp_id += 1;
        let mut envelope = json!({ "id": id, "method": method, "params": params });
        if let Some(sid) = session_id {
            envelope["sessionId"] = json!(sid);
        }
        let texto = serde_json::to_string(&envelope).map_err(|e| e.to_string())?;
        self.ws
            .send(Message::Text(texto.into()))
            .await
            .map_err(|e| format!("socket do navegador falhou na escrita: {}", e))?;

        let prazo = tokio::time::Instant::now() + CALL_TIMEOUT;
        loop {
            let proxima = tokio::time::timeout_at(prazo, self.ws.next())
                .await
                .map_err(|_| format!("o navegador não respondeu a {} em 30s", method))?;
            let msg = match proxima {
                Some(Ok(m)) => m,
                Some(Err(e)) => return Err(format!("socket do navegador falhou: {}", e)),
                None => return Err("socket do navegador fechou".to_string()),
            };
            let texto = match msg {
                Message::Text(t) => t.to_string(),
                Message::Close(_) => return Err("socket do navegador fechou".to_string()),
                _ => continue,
            };
            let valor: Value = match serde_json::from_str(&texto) {
                Ok(v) => v,
                Err(_) => continue,
            };
            if valor.get("id").and_then(|v| v.as_u64()) != Some(id) {
                // O único evento que interessa: a aba pediu outra janela. É o
                // gatilho de "este clique abriu uma aba" (ver browser_local.rs).
                if valor.get("method").and_then(|v| v.as_str()) == Some("Page.windowOpen") {
                    let sessao = valor.get("sessionId").and_then(|v| v.as_str()).unwrap_or("");
                    if let Some(tab) = self.tabs.iter().find(|t| t.session_id == sessao) {
                        let ev = valor.get("params").cloned().unwrap_or(Value::Null);
                        super::browser_local::anotar_janela(
                            -tab.id,
                            ev.get("url").and_then(|v| v.as_str()).unwrap_or(""),
                            ev.get("userGesture").and_then(|v| v.as_bool()).unwrap_or(false),
                        );
                    }
                }
                continue; // evento ou resposta de outra chamada
            }
            if let Some(erro) = valor.get("error") {
                let m = erro
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("erro no CDP");
                return Err(m.to_string());
            }
            return Ok(valor.get("result").cloned().unwrap_or(Value::Null));
        }
    }

    async fn open_tab(&mut self, url: &str) -> Result<i64, String> {
        let alvo = self
            .request("Target.createTarget", json!({ "url": url }), None)
            .await?;
        let target_id = alvo
            .get("targetId")
            .and_then(|v| v.as_str())
            .ok_or("o navegador não devolveu targetId")?
            .to_string();
        let anexo = self
            .request(
                "Target.attachToTarget",
                json!({ "targetId": target_id, "flatten": true }),
                None,
            )
            .await?;
        let session_id = anexo
            .get("sessionId")
            .and_then(|v| v.as_str())
            .ok_or("o navegador não devolveu sessionId")?
            .to_string();

        let id = self.next_tab_id;
        self.next_tab_id += 1;
        self.tabs.push(Tab {
            id,
            target_id,
            session_id,
            last_used: Instant::now(),
            url: url.to_string(),
            opener: None,
        });
        Ok(id)
    }

    /// As abas que a página da aba `opener` abriu e o gerente ainda não conhece.
    ///
    /// Pelo `Target.getTargets`, que traz o `openerId` de cada alvo: não depende
    /// de ter lido o evento `targetCreated` (o `request` descarta eventos) e já
    /// é a chamada que bombeia o `Page.windowOpen` (ver `request`).
    async fn take_new_tabs(&mut self, opener: i64) -> Result<Vec<(i64, String, String)>, String> {
        let Some(alvo_origem) = self
            .tabs
            .iter()
            .find(|t| t.id == opener)
            .map(|t| t.target_id.clone())
        else {
            return Ok(Vec::new());
        };
        let lista = self.request("Target.getTargets", json!({}), None).await?;
        let conhecidos: Vec<String> = self.tabs.iter().map(|t| t.target_id.clone()).collect();
        let mut novas = Vec::new();
        for info in lista
            .get("targetInfos")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default()
        {
            if info.get("type").and_then(|v| v.as_str()) != Some("page") {
                continue;
            }
            if info.get("openerId").and_then(|v| v.as_str()) != Some(alvo_origem.as_str()) {
                continue;
            }
            let Some(target_id) = info.get("targetId").and_then(|v| v.as_str()) else {
                continue;
            };
            if conhecidos.iter().any(|c| c == target_id) {
                continue;
            }
            let anexo = self
                .request(
                    "Target.attachToTarget",
                    json!({ "targetId": target_id, "flatten": true }),
                    None,
                )
                .await?;
            let Some(session_id) = anexo.get("sessionId").and_then(|v| v.as_str()) else {
                continue;
            };
            let url = info.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let title = info.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let id = self.next_tab_id;
            self.next_tab_id += 1;
            self.tabs.push(Tab {
                id,
                target_id: target_id.to_string(),
                session_id: session_id.to_string(),
                last_used: Instant::now(),
                url: url.clone(),
                opener: Some(opener),
            });
            novas.push((id, url, title));
        }
        Ok(novas)
    }

    /// As abas do gerente que ainda existem — e esquece as que sumiram (o popup
    /// que se fechou sozinho, a aba que a página fechou).
    async fn live_tabs(&mut self) -> Result<Vec<(i64, String, String, Option<i64>)>, String> {
        let lista = self.request("Target.getTargets", json!({}), None).await?;
        let vivos: Vec<(String, String, String)> = lista
            .get("targetInfos")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter_map(|i| {
                Some((
                    i.get("targetId")?.as_str()?.to_string(),
                    i.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                    i.get("title").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                ))
            })
            .collect();
        self.tabs.retain(|t| vivos.iter().any(|(id, _, _)| id == &t.target_id));
        Ok(self
            .tabs
            .iter()
            .map(|t| {
                let (_, url, title) = vivos
                    .iter()
                    .find(|(id, _, _)| id == &t.target_id)
                    .cloned()
                    .unwrap_or_default();
                (t.id, url, title, t.opener)
            })
            .collect())
    }

    async fn close_tab(&mut self, tab_id: i64) -> Result<(), String> {
        let Some(pos) = self.tabs.iter().position(|t| t.id == tab_id) else {
            return Ok(());
        };
        let target_id = self.tabs[pos].target_id.clone();
        self.tabs.remove(pos);
        let _ = self
            .request("Target.closeTarget", json!({ "targetId": target_id }), None)
            .await;
        Ok(())
    }

    fn session_of(&mut self, tab_id: i64) -> Option<String> {
        self.tabs.iter_mut().find(|t| t.id == tab_id).map(|t| {
            t.last_used = Instant::now();
            t.session_id.clone()
        })
    }

    /// Fecha abas ociosas e, se ainda estiver acima do teto, as mais antigas.
    ///
    /// `reserva` é quantas abas estão prestes a ser abertas. Sem ela o teto
    /// vazava por uma: `enforce` roda ANTES do `open_tab`, então com
    /// `max_tabs = 2` e duas abas abertas, `len() > max_tabs` era `2 > 2` =
    /// falso, ninguém saía, e a terceira entrava — teto 2, três abas. Pego
    /// pelo teste `teto_de_abas_fecha_a_mais_antiga`, não pelo `cargo check`.
    ///
    /// Devolve `true` quando ainda está acima do teto de RAM depois de tudo —
    /// aí só reiniciar resolve, porque fechar aba não devolve memória na hora.
    async fn enforce(&mut self, cfg: &Limits, reserva: usize) -> bool {
        if cfg.idle_ttl_s > 0 {
            let ttl = Duration::from_secs(cfg.idle_ttl_s);
            let ociosas: Vec<i64> = self
                .tabs
                .iter()
                .filter(|t| t.last_used.elapsed() > ttl)
                .map(|t| t.id)
                .collect();
            for id in ociosas {
                info!("[headless] fechando aba {} por ociosidade", id);
                let _ = self.close_tab(id).await;
            }
        }
        if cfg.max_tabs > 0 {
            while self.tabs.len() + reserva > cfg.max_tabs && !self.tabs.is_empty() {
                // LRU: a de uso mais antigo sai primeiro.
                if let Some(id) = self
                    .tabs
                    .iter()
                    .min_by_key(|t| t.last_used)
                    .map(|t| t.id)
                {
                    info!("[headless] teto de abas — fechando a mais antiga ({})", id);
                    let _ = self.close_tab(id).await;
                } else {
                    break;
                }
            }
        }
        if cfg.max_ram_mb == 0 {
            return false;
        }
        if tree_footprint_mb(self.pid) <= cfg.max_ram_mb {
            return false;
        }
        // Acima do teto: derruba as ociosas primeiro, da mais antiga para a mais
        // nova, medindo de novo a cada uma.
        while self.tabs.len() > 1 {
            let Some(id) = self.tabs.iter().min_by_key(|t| t.last_used).map(|t| t.id) else {
                break;
            };
            warn!("[headless] acima do teto de RAM — fechando aba {}", id);
            let _ = self.close_tab(id).await;
            if tree_footprint_mb(self.pid) <= cfg.max_ram_mb {
                return false;
            }
        }
        true
    }

    async fn shutdown(mut self) {
        // Processo que já saiu não responde ao `Browser.close`, e esperar por ele
        // custaria o prazo inteiro da chamada.
        if self.vivo() {
            let _ = self.request("Browser.close", json!({}), None).await;
        }
        let _ = self.ws.close(None).await;
        let _ = self.child.kill().await;
    }

    /// O processo ainda está de pé? `try_wait` devolve o status de quem já saiu.
    fn vivo(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }
}

/// Erro de TRANSPORTE com o navegador (a conexão caiu), e não da página.
fn conexao_caiu(erro: &str) -> bool {
    erro.contains("socket do navegador")
}

// ─── API pública ────────────────────────────────────────────────────────────

/// Sobe o navegador se ainda não estiver de pé. Idempotente.
///
/// "De pé" é o PROCESSO vivo, não haver um registro guardado. O registro
/// sobrevive ao navegador: na sessão cb41834a o Chrome sem janela morreu nas
/// três horas paradas, e as duas tarefas seguintes escreveram no socket dele e
/// morreram com "Broken pipe" na largada. Nada subia outro.
pub async fn ensure_running() -> Result<(), String> {
    let slot = session_slot();
    let mut guard = slot.lock().await;
    if let Some(h) = guard.as_mut() {
        if h.vivo() {
            return Ok(());
        }
        warn!("[headless] o navegador (pid {}) não está mais de pé — subindo outro", h.pid);
        if let Some(morto) = guard.take() {
            morto.shutdown().await;
        }
    }
    *guard = Some(Headless::launch().await?);
    Ok(())
}

/// Esta máquina TEM como rodar o headless? (existe Chromium instalado)
///
/// Diferente do `browser_local::available()`, que depende de o usuário estar
/// com o navegador aberto E a extensão carregada. O headless não depende de
/// nada disso — por isso ele é o degrau que funciona quando o outro não está
/// disponível, desde que a máquina dele esteja ligada.
pub fn available() -> bool {
    find_browser(limits().browser_path.as_deref()).is_ok()
}

/// Abre uma aba, respeitando os tetos. Reinicia o navegador quando a memória
/// não cede de outro jeito.
pub async fn open_tab(url: &str) -> Result<i64, String> {
    let cfg = limits();
    ensure_running().await?;
    let slot = session_slot();
    let mut guard = slot.lock().await;

    let precisa_reiniciar = match guard.as_mut() {
        // reserva=1: a aba que está prestes a entrar precisa de vaga.
        Some(h) => h.enforce(&cfg, 1).await,
        None => false,
    };
    if precisa_reiniciar {
        warn!("[headless] memória não cedeu fechando abas — reiniciando o navegador");
        if let Some(h) = guard.take() {
            h.shutdown().await;
        }
        *guard = Some(Headless::launch().await?);
    }

    let h = guard.as_mut().ok_or("navegador headless não está de pé")?;
    match h.open_tab(url).await {
        // O processo pode estar vivo com a conexão morta (o WebSocket caiu e o
        // Chrome ficou). Aba é coisa nova: subir outro navegador e tentar UMA
        // vez não perde nada, e é a diferença entre a tarefa rodar ou morrer
        // na largada.
        Err(e) if conexao_caiu(&e) => {
            warn!("[headless] conexão caiu ao abrir aba ({}) — reiniciando o navegador", e);
            if let Some(morto) = guard.take() {
                morto.shutdown().await;
            }
            *guard = Some(Headless::launch().await?);
            let h = guard.as_mut().ok_or("navegador headless não está de pé")?;
            h.open_tab(url).await
        }
        outro => outro,
    }
}

pub async fn close_tab(tab_id: i64) -> Result<(), String> {
    let slot = session_slot();
    let mut guard = slot.lock().await;
    match guard.as_mut() {
        Some(h) => h.close_tab(tab_id).await,
        None => Ok(()),
    }
}

/// As filhas de `opener` que o gerente ainda não conhecia, já conectadas.
pub async fn take_new_tabs(opener: i64) -> Result<Vec<(i64, String, String)>, String> {
    let slot = session_slot();
    let mut guard = slot.lock().await;
    match guard.as_mut() {
        Some(h) => h.take_new_tabs(opener).await,
        None => Ok(Vec::new()),
    }
}

/// `(id, url, título, quem abriu)` de cada aba viva do gerente.
pub async fn live_tabs() -> Result<Vec<(i64, String, String, Option<i64>)>, String> {
    let slot = session_slot();
    let mut guard = slot.lock().await;
    match guard.as_mut() {
        Some(h) => h.live_tabs().await,
        None => Ok(Vec::new()),
    }
}

/// Uma chamada CDP numa aba — o mesmo contrato do `browser_local::cdp`.
pub async fn cdp(tab_id: i64, method: &str, params: Value) -> Result<Value, String> {
    let slot = session_slot();
    let mut guard = slot.lock().await;
    let h = guard
        .as_mut()
        .ok_or("o navegador headless não está de pé")?;
    let sid = h
        .session_of(tab_id)
        .ok_or_else(|| format!("aba {} não existe mais no navegador headless", tab_id))?;
    h.request(method, params, Some(&sid)).await
}

/// Derruba tudo. Chamado no encerramento e quando o usuário desliga a feature.
pub async fn shutdown() {
    let slot = session_slot();
    let mut guard = slot.lock().await;
    if let Some(h) = guard.take() {
        info!("[headless] encerrando");
        h.shutdown().await;
    }
}

/// Estado para a tela de Config e para diagnóstico.
pub async fn stats() -> Value {
    let cfg = limits();
    let slot = session_slot();
    let guard = slot.lock().await;
    match guard.as_ref() {
        None => json!({
            "running": false,
            "limits": {
                "max_tabs": cfg.max_tabs,
                "max_ram_mb": cfg.max_ram_mb,
                "idle_ttl_s": cfg.idle_ttl_s,
            },
        }),
        Some(h) => json!({
            "running": true,
            "browser": h.binario,
            "pid": h.pid,
            "uptime_s": h.started.elapsed().as_secs(),
            // phys_footprint da árvore — NÃO soma de RSS (ver o doc do módulo).
            "ram_mb": tree_footprint_mb(h.pid),
            "tabs": h.tabs.len(),
            "tab_urls": h.tabs.iter().map(|t| t.url.clone()).collect::<Vec<_>>(),
            "limits": {
                "max_tabs": cfg.max_tabs,
                "max_ram_mb": cfg.max_ram_mb,
                "idle_ttl_s": cfg.idle_ttl_s,
            },
        }),
    }
}


// ─── Testes ─────────────────────────────────────────────────────────────────
//
// Sobem um Chromium DE VERDADE e falam CDP pelo código deste módulo — é o
// único jeito de provar o que `cargo check` não prova: que os flags estão
// certos, que a porta é descoberta pelo `DevToolsActivePort`, que o protocolo
// flat (sessionId no envelope) funciona e que o gerente de aba fecha o que
// abriu.
//
// Ficam `#[ignore]` porque dependem de um navegador instalado e levam segundos:
//     cargo test --lib headless -- --ignored --test-threads=1
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn acha_um_navegador_ou_diz_por_que_nao() {
        match find_browser(None) {
            Ok(p) => assert!(p.exists(), "o caminho devolvido tem que existir"),
            // Máquina sem Chromium é um cenário legítimo — o que não pode é
            // mentir dizendo que achou.
            Err(e) => assert!(e.contains("não achei"), "erro precisa explicar: {e}"),
        }
    }

    #[test]
    fn caminho_explicito_inexistente_falha_claro() {
        let e = find_browser(Some("/nao/existe/navegador")).unwrap_err();
        assert!(e.contains("não existe"), "{e}");
    }

    #[test]
    fn footprint_do_processo_atual_e_plausivel() {
        // Prova que estamos lendo `phys_footprint` de verdade, e não zero.
        let pid = std::process::id() as i32;
        let mb = phys_footprint_mb(pid);
        if cfg!(target_os = "macos") {
            let mb = mb.expect("no macOS o rusage tem que responder");
            assert!(mb < 8192, "footprint absurdo: {mb} MB");
        }
    }

    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn abre_aba_le_a_pagina_e_fecha() {
        if find_browser(None).is_err() {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        set_limits(Limits { max_tabs: 3, max_ram_mb: 0, idle_ttl_s: 0, ..Limits::default() });

        let tab = open_tab("about:blank").await.expect("abrir aba");

        // `Runtime.evaluate` é o mesmo verbo que as 20 ações usam — se ele
        // funciona aqui, o vocabulário inteiro funciona neste backend.
        let r = cdp(
            tab,
            "Runtime.evaluate",
            json!({ "expression": "1 + 1", "returnByValue": true }),
        )
        .await
        .expect("evaluate");
        assert_eq!(r["result"]["value"], 2);

        // O UA NÃO pode dizer HeadlessChrome: é a primeira string que anti-bot
        // procura, e o headless existe justamente para passar onde o resto não.
        let ua = cdp(
            tab,
            "Runtime.evaluate",
            json!({ "expression": "navigator.userAgent", "returnByValue": true }),
        )
        .await
        .expect("ua");
        let ua = ua["result"]["value"].as_str().unwrap_or("");
        assert!(!ua.contains("HeadlessChrome"), "UA entrega a automação: {ua}");

        // `navigator.webdriver` idem.
        let wd = cdp(
            tab,
            "Runtime.evaluate",
            json!({ "expression": "navigator.webdriver", "returnByValue": true }),
        )
        .await
        .expect("webdriver");
        assert_eq!(wd["result"]["value"], false, "webdriver entrega a automação");

        let s = stats().await;
        assert_eq!(s["running"], true);
        assert_eq!(s["tabs"], 1);
        assert!(s["ram_mb"].as_u64().unwrap_or(0) > 0, "memória tem que ser medida");

        close_tab(tab).await.expect("fechar aba");
        assert_eq!(stats().await["tabs"], 0);

        // Aba fechada não responde mais — o registro e o navegador concordam.
        assert!(cdp(tab, "Runtime.evaluate", json!({ "expression": "1" })).await.is_err());

        shutdown().await;
        assert_eq!(stats().await["running"], false);
    }

    /// Sessão cb41834a: o Chrome sem janela morreu nas horas paradas e as
    /// tarefas seguintes morriam com "Broken pipe" na largada.
    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn navegador_que_morreu_sobe_outro_na_proxima_aba() {
        if find_browser(None).is_err() {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        shutdown().await;
        set_limits(Limits { max_tabs: 3, max_ram_mb: 0, idle_ttl_s: 0, ..Limits::default() });
        open_tab("about:blank").await.expect("aba 1");
        let pid = stats().await["pid"].as_i64().expect("pid do navegador");
        let _ = std::process::Command::new("kill").args(["-9", &pid.to_string()]).status();
        tokio::time::sleep(Duration::from_millis(500)).await;

        let tab = open_tab("about:blank").await.expect("abrir aba depois de o navegador morrer");
        let r = cdp(tab, "Runtime.evaluate", json!({ "expression": "1 + 1", "returnByValue": true }))
            .await
            .expect("evaluate no navegador novo");
        assert_eq!(r["result"]["value"], 2);
        assert_ne!(stats().await["pid"].as_i64(), Some(pid), "tinha de ser outro processo");
        shutdown().await;
    }

    /// O outro jeito de morrer: o processo vivo e a conexão caída.
    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn conexao_que_caiu_com_o_navegador_vivo_tambem_se_refaz() {
        if find_browser(None).is_err() {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        shutdown().await;
        set_limits(Limits { max_tabs: 3, max_ram_mb: 0, idle_ttl_s: 0, ..Limits::default() });
        open_tab("about:blank").await.expect("aba 1");
        {
            let slot = session_slot();
            let mut guard = slot.lock().await;
            let h = guard.as_mut().expect("navegador de pé");
            let _ = h.ws.close(None).await;
        }
        let tab = open_tab("about:blank").await.expect("abrir aba depois de a conexão cair");
        let r = cdp(tab, "Runtime.evaluate", json!({ "expression": "2 + 2", "returnByValue": true }))
            .await
            .expect("evaluate depois de refazer");
        assert_eq!(r["result"]["value"], 4);
        shutdown().await;
    }

    #[tokio::test]
    #[ignore = "sobe um Chromium de verdade"]
    async fn teto_de_abas_fecha_a_mais_antiga() {
        if find_browser(None).is_err() {
            eprintln!("sem navegador nesta máquina — pulando");
            return;
        }
        shutdown().await;
        set_limits(Limits { max_tabs: 2, max_ram_mb: 0, idle_ttl_s: 0, ..Limits::default() });

        let a = open_tab("about:blank").await.expect("aba 1");
        let _b = open_tab("about:blank").await.expect("aba 2");
        // A terceira força o LRU: a primeira é a de uso mais antigo.
        let _c = open_tab("about:blank").await.expect("aba 3");

        let s = stats().await;
        assert!(s["tabs"].as_u64().unwrap() <= 2, "teto não foi respeitado: {s}");
        assert!(cdp(a, "Runtime.evaluate", json!({ "expression": "1" })).await.is_err(),
                "a aba mais antiga devia ter sido fechada");

        shutdown().await;
    }
}
