//! Arquivos de TEXTO no disco do usuário, lidos e alterados pelo Neo.
//!
//! Tools despachadas em `ai_agent.rs`: `read_user_file`, `edit_user_file`,
//! `write_file` e `delegate_file_edit` (o opencode como mão de obra, o Neo como
//! orquestrador).
//!
//! **Por que as regras moram aqui e não no neo-core.** Os argumentos chegam montados
//! por um LLM — e LLM lê página web, email e arquivo de terceiro. O lado que decide
//! ONDE pode gravar tem que ser o que o modelo não controla. Então: pastas liberadas,
//! caminhos sensíveis, "o arquivo ainda é o que o Neo leu" (hash), backup e escrita
//! atômica valem mesmo que o pedido chegue torto.
//!
//! **O que o conteúdo NÃO faz:** não sobe pro File Store nem pro sandbox. A leitura
//! vai só no ToolResult do turno — sem cópia persistente na VPS (a análise de
//! 2026-09-15 achou NFSE, extrato e documento do PROCON esquecidos em
//! `/workspace/files`, justamente pelo caminho antigo de edição).
//!
//! Tudo que grava passa por [`commit`]: diff → aprovação quando a regra pede →
//! conferência do hash → backup → escrita atômica → evento `file_change` (o card do
//! chat, com Desfazer).

use log::{info, warn};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::process::Stdio;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};
use tokio::sync::oneshot;

/// Maior arquivo tratado como texto editável.
const MAX_TEXT_BYTES: u64 = 5 * 1024 * 1024;
/// Linhas devolvidas por leitura quando o Neo não diz quantas.
const READ_DEFAULT_LINES: usize = 400;
/// Teto de caracteres de uma leitura (o resto vem com `next_offset`). Igual ao
/// orçamento que o core dá a um arquivo lido no transcript do turno
/// (`_STEP_CHARS_FILE`): maior que isso o Neo veria um arquivo cortado sem saber.
const READ_MAX_CHARS: usize = 40_000;
/// Diff que volta pro modelo (o card recebe um maior).
const DIFF_RESULT_CHARS: usize = 12_000;
const DIFF_UI_CHARS: usize = 200_000;
/// Quanto um pedido de aprovação espera o clique.
const APPROVAL_TIMEOUT: Duration = Duration::from_secs(15 * 60);
/// Alterações guardadas para Desfazer.
const HISTORY_KEPT: usize = 200;
/// Acima disto o diff não roda LCS (memória O(n·m)) e vira um bloco só.
const LCS_MAX_CELLS: usize = 4_000_000;

pub const OPENCODE_DEFAULT_MODEL: &str = "openrouter/deepseek/deepseek-v4.1-flash";
const OPENCODE_AGENT: &str = "neo-editor";
const OPENCODE_DEFAULT_TIMEOUT: Duration = Duration::from_secs(300);

// ───────────────────────────── regras de acesso ─────────────────────────────

#[derive(Debug, Clone, PartialEq)]
pub enum Access {
    Allow,
    Ask(String),
    Deny(String),
}

/// Relativos à home: credencial, chave, autostart e rc de shell. Ler já é
/// exfiltração (o conteúdo vai pro modelo); gravar é persistência.
const SENSITIVE_HOME: &[&str] = &[
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config/gcloud",
    ".config/gh", ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc",
    ".local/share/opencode", "Library/Keychains", "Library/LaunchAgents",
    "Library/Cookies", ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".bashrc",
    ".bash_profile", ".profile", ".config/fish",
];

/// Onde nunca se grava.
const SYSTEM_ROOTS: &[&str] = &[
    "/etc", "/private", "/System", "/usr", "/bin", "/sbin", "/Library",
    "/Applications", "/var", "/opt", "/cores", "/dev",
];

fn looks_like_secret(path: &Path) -> bool {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    (name == ".env" || (name.starts_with(".env.") && name != ".env.example"))
        || name.ends_with(".pem")
        || name.ends_with(".key")
        || name.starts_with("id_rsa")
        || name.starts_with("id_ed25519")
}

/// `~` expandido e absoluto obrigatório. `..` é recusado: ele permitiria escrever
/// `~/Desktop/nova/../../.ssh/x`, que passaria por "dentro do Desktop" num teste de
/// prefixo antes de a pasta existir.
pub fn resolve_path(raw: &str) -> Result<PathBuf, String> {
    let t = raw.trim().trim_matches('"').trim_matches('\'');
    if t.is_empty() {
        return Err("caminho vazio".to_string());
    }
    let home = || dirs::home_dir().ok_or_else(|| "não consegui resolver \"~\"".to_string());
    let p = if t == "~" {
        home()?
    } else if let Some(rest) = t.strip_prefix("~/") {
        home()?.join(rest)
    } else {
        PathBuf::from(t)
    };
    if !p.is_absolute() {
        return Err(format!(
            "caminho relativo não é aceito ({}): use absoluto ou começando com ~/",
            t
        ));
    }
    if p.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err(format!("caminho com '..' não é aceito ({})", t));
    }
    Ok(p)
}

/// O caminho de verdade (symlink resolvido) — do arquivo, ou do ancestral que existe.
/// Público porque a política de comando (services/command_policy.rs) resolve os
/// alvos de um `mv`/`cp` com a MESMA regra — senão um atalho pra `~/.ssh` passaria
/// num lugar e não no outro.
pub fn real_path(p: &Path) -> PathBuf {
    if let Ok(c) = std::fs::canonicalize(p) {
        return c;
    }
    let mut tail = Vec::new();
    let mut cur = p.to_path_buf();
    while let Some(parent) = cur.parent().map(Path::to_path_buf) {
        if let Some(name) = cur.file_name() {
            tail.push(name.to_os_string());
        }
        if let Ok(mut out) = std::fs::canonicalize(&parent) {
            for n in tail.iter().rev() {
                out.push(n);
            }
            return out;
        }
        cur = parent;
    }
    p.to_path_buf()
}

fn allowed_roots() -> Vec<PathBuf> {
    [dirs::desktop_dir(), dirs::document_dir(), dirs::download_dir()]
        .into_iter()
        .flatten()
        .map(|d| std::fs::canonicalize(&d).unwrap_or(d))
        .collect()
}

/// `p` (já canonizado) está numa pasta do sistema? A lista é a mesma que barra a
/// escrita aqui — a política de comando consulta em vez de manter uma cópia.
pub fn in_system_root(p: &Path) -> bool {
    SYSTEM_ROOTS.iter().any(|root| p.starts_with(root))
}

pub fn access_for(path: &Path, write: bool) -> Access {
    let real = real_path(path);
    if let Some(home) = dirs::home_dir() {
        let home = std::fs::canonicalize(&home).unwrap_or(home);
        for s in SENSITIVE_HOME {
            if real.starts_with(home.join(s)) {
                return Access::Ask(format!(
                    "`{}` é caminho sensível (credencial, chave ou configuração do sistema)",
                    real.display()
                ));
            }
        }
    }
    if looks_like_secret(&real) {
        return Access::Ask(format!("`{}` tem cara de segredo (.env / chave)", real.display()));
    }
    if !write {
        return Access::Allow;
    }
    if let Some(root) = SYSTEM_ROOTS.iter().find(|root| real.starts_with(root)) {
        return Access::Deny(format!("gravar em {} (pasta do sistema) não é permitido", root));
    }
    if allowed_roots().iter().any(|r| real.starts_with(r)) {
        return Access::Allow;
    }
    Access::Ask(format!(
        "`{}` fica fora das pastas liberadas (Desktop, Documentos, Downloads)",
        real.display()
    ))
}

// ───────────────────────────── texto ─────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Eol {
    Lf,
    Crlf,
    Mixed,
    NoLines,
}

impl Eol {
    fn label(self) -> &'static str {
        match self {
            Eol::Lf => "lf",
            Eol::Crlf => "crlf",
            Eol::Mixed => "mixed",
            Eol::NoLines => "none",
        }
    }
}

#[derive(Debug, Clone)]
pub struct TextDoc {
    pub bytes: Vec<u8>,
    /// Sem BOM, com as quebras originais.
    pub text: String,
    pub bom: bool,
    pub eol: Eol,
}

const BOM: [u8; 3] = [0xEF, 0xBB, 0xBF];

pub fn sha256_hex(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

pub fn decode_text(bytes: Vec<u8>) -> Result<TextDoc, String> {
    if bytes[..bytes.len().min(8192)].contains(&0) {
        return Err("arquivo binário — só edito texto".to_string());
    }
    let (bom, body) = if bytes.starts_with(&BOM) {
        (true, &bytes[3..])
    } else {
        (false, &bytes[..])
    };
    let text = std::str::from_utf8(body)
        .map_err(|_| {
            "o arquivo não está em UTF-8 — não edito pra não corromper os acentos".to_string()
        })?
        .to_string();
    let crlf = text.matches("\r\n").count();
    let lf = text.matches('\n').count() - crlf;
    let eol = match (crlf, lf) {
        (0, 0) => Eol::NoLines,
        (_, 0) => Eol::Crlf,
        (0, _) => Eol::Lf,
        _ => Eol::Mixed,
    };
    Ok(TextDoc { bytes, text, bom, eol })
}

fn encode_text(text: &str, bom: bool) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len() + 3);
    if bom {
        out.extend_from_slice(&BOM);
    }
    out.extend_from_slice(text.as_bytes());
    out
}

fn to_lf(s: &str) -> String {
    s.replace("\r\n", "\n")
}

fn lines_of(text: &str) -> Vec<&str> {
    // Texto vazio não tem linha nenhuma — `split` devolveria [""] e o diff de um
    // arquivo criado mostraria uma remoção fantasma.
    if text.is_empty() {
        return Vec::new();
    }
    let mut v: Vec<&str> = text.split('\n').collect();
    if text.ends_with('\n') {
        v.pop();
    }
    v
}

pub struct Slice {
    pub content: String,
    pub start_line: usize,
    pub end_line: usize,
    pub total_lines: usize,
    pub next_offset: Option<usize>,
}

/// Linhas `offset..` (1-based), até `limit` linhas e [`READ_MAX_CHARS`].
pub fn read_slice(doc: &TextDoc, offset: usize, limit: usize) -> Slice {
    let lf = to_lf(&doc.text);
    let lines = lines_of(&lf);
    let total = lines.len();
    let start = offset.max(1);
    let mut out = String::new();
    let mut end = start.saturating_sub(1);
    for (i, line) in lines.iter().enumerate().skip(start - 1).take(limit.max(1)) {
        if !out.is_empty() && out.len() + line.len() + 1 > READ_MAX_CHARS {
            break;
        }
        out.push_str(line);
        out.push('\n');
        end = i + 1;
    }
    Slice {
        content: out,
        start_line: start,
        end_line: end,
        total_lines: total,
        next_offset: (end < total).then_some(end + 1),
    }
}

// ───────────────────────────── edição ─────────────────────────────

#[derive(Debug, Clone, Deserialize)]
pub struct EditOp {
    pub old_string: String,
    pub new_string: String,
    #[serde(default)]
    pub replace_all: bool,
}

fn collapse_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Linhas do arquivo parecidas com o começo do trecho que não casou — o erro mais
/// comum é indentação/espaço diferente, e sem pista o modelo chuta de novo igual.
fn near_lines(text: &str, old: &str) -> Value {
    let probe = old.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or("");
    if probe.is_empty() {
        return json!([]);
    }
    let probe_low = probe.to_lowercase();
    let hits: Vec<Value> = lines_of(text)
        .iter()
        .enumerate()
        .filter(|(_, l)| l.to_lowercase().contains(&probe_low))
        .take(3)
        .map(|(i, l)| json!({"line": i + 1, "text": l.chars().take(200).collect::<String>()}))
        .collect();
    json!(hits)
}

/// Aplica os trechos em ordem. `Err` carrega o JSON de status pro modelo.
pub fn apply_edits(doc: &TextDoc, edits: &[EditOp]) -> Result<String, Value> {
    if edits.is_empty() {
        return Err(json!({"status": "error", "message": "nenhum trecho em `edits`"}));
    }
    // Arquivo CRLF: casa e troca em LF e devolve em CRLF — o modelo escreve `\n`.
    let crlf = doc.eol == Eol::Crlf;
    let mut work = if crlf { to_lf(&doc.text) } else { doc.text.clone() };
    for (i, e) in edits.iter().enumerate() {
        let old = if crlf { to_lf(&e.old_string) } else { e.old_string.clone() };
        let new = if crlf { to_lf(&e.new_string) } else { e.new_string.clone() };
        if old.is_empty() {
            return Err(json!({
                "status": "error", "edit_index": i,
                "message": "old_string vazio — pra criar ou trocar o arquivo inteiro use write_file",
            }));
        }
        let count = work.matches(old.as_str()).count();
        if count == 0 {
            let so_espaco = collapse_ws(&work).contains(&collapse_ws(&old));
            return Err(json!({
                "status": "no_match", "edit_index": i,
                "message": if so_espaco {
                    "o trecho existe, mas com espaços/indentação/quebras diferentes — copie exatamente como veio do read_user_file"
                } else {
                    "o trecho não existe no arquivo atual — releia com read_user_file antes de tentar de novo"
                },
                "similar_lines": near_lines(&work, &old),
            }));
        }
        if count > 1 && !e.replace_all {
            return Err(json!({
                "status": "ambiguous", "edit_index": i, "occurrences": count,
                "message": "o trecho aparece mais de uma vez — inclua mais contexto pra ficar único, ou use replace_all=true",
            }));
        }
        work = if e.replace_all {
            work.replace(old.as_str(), &new)
        } else {
            work.replacen(old.as_str(), &new, 1)
        };
    }
    Ok(if crlf { work.replace('\n', "\r\n") } else { work })
}

// ───────────────────────────── diff ─────────────────────────────

#[derive(Debug, Clone, Copy)]
enum Op {
    Eq(usize),
    Del(usize),
    Ins(usize),
}

pub struct Diff {
    pub text: String,
    pub added: usize,
    pub removed: usize,
}

/// Diff unificado por linhas (LCS no miolo, depois de tirar prefixo/sufixo iguais).
pub fn unified_diff(old: &str, new: &str, ctx: usize) -> Diff {
    let (old, new) = (to_lf(old), to_lf(new));
    let a = lines_of(&old);
    let b = lines_of(&new);
    let mut pre = 0;
    while pre < a.len() && pre < b.len() && a[pre] == b[pre] {
        pre += 1;
    }
    let mut suf = 0;
    while suf < a.len() - pre && suf < b.len() - pre && a[a.len() - 1 - suf] == b[b.len() - 1 - suf] {
        suf += 1;
    }
    let (am, bm) = (&a[pre..a.len() - suf], &b[pre..b.len() - suf]);

    let mut ops: Vec<Op> = (0..pre).map(Op::Eq).collect();
    if am.len().saturating_mul(bm.len()) <= LCS_MAX_CELLS {
        let (n, m) = (am.len(), bm.len());
        let w = m + 1;
        let mut dp = vec![0u32; (n + 1) * (m + 1)];
        for i in (0..n).rev() {
            for j in (0..m).rev() {
                dp[i * w + j] = if am[i] == bm[j] {
                    dp[(i + 1) * w + j + 1] + 1
                } else {
                    dp[(i + 1) * w + j].max(dp[i * w + j + 1])
                };
            }
        }
        let (mut i, mut j) = (0, 0);
        while i < n || j < m {
            if i < n && j < m && am[i] == bm[j] {
                ops.push(Op::Eq(pre + i));
                i += 1;
                j += 1;
            } else if j >= m || (i < n && dp[(i + 1) * w + j] >= dp[i * w + j + 1]) {
                ops.push(Op::Del(pre + i));
                i += 1;
            } else {
                ops.push(Op::Ins(pre + j));
                j += 1;
            }
        }
    } else {
        ops.extend((0..am.len()).map(|i| Op::Del(pre + i)));
        ops.extend((0..bm.len()).map(|j| Op::Ins(pre + j)));
    }
    ops.extend((0..suf).map(|k| Op::Eq(a.len() - suf + k)));

    let added = ops.iter().filter(|o| matches!(o, Op::Ins(_))).count();
    let removed = ops.iter().filter(|o| matches!(o, Op::Del(_))).count();
    if added == 0 && removed == 0 {
        return Diff { text: String::new(), added, removed };
    }

    // Posição (linhas antigas, linhas novas) consumidas ANTES de cada op.
    let mut pos = Vec::with_capacity(ops.len());
    let (mut pa, mut pb) = (0usize, 0usize);
    for op in &ops {
        pos.push((pa, pb));
        match op {
            Op::Eq(..) => {
                pa += 1;
                pb += 1;
            }
            Op::Del(_) => pa += 1,
            Op::Ins(_) => pb += 1,
        }
    }

    let is_eq = |k: usize| matches!(ops[k], Op::Eq(..));
    let n = ops.len();
    let mut text = String::new();
    let mut i = 0;
    while i < n {
        while i < n && is_eq(i) {
            i += 1;
        }
        if i >= n {
            break;
        }
        let start = i.saturating_sub(ctx);
        let mut end = i;
        let mut j = i;
        loop {
            while j < n && !is_eq(j) {
                end = j;
                j += 1;
            }
            let mut k = j;
            while k < n && is_eq(k) {
                k += 1;
            }
            if k < n && k - j <= 2 * ctx {
                j = k;
                continue;
            }
            break;
        }
        let stop = (end + 1 + ctx).min(n);
        let (oa, ob) = pos[start];
        let old_count = (start..stop).filter(|&k| !matches!(ops[k], Op::Ins(_))).count();
        let new_count = (start..stop).filter(|&k| !matches!(ops[k], Op::Del(_))).count();
        text.push_str(&format!(
            "@@ -{},{} +{},{} @@\n",
            if old_count > 0 { oa + 1 } else { oa },
            old_count,
            if new_count > 0 { ob + 1 } else { ob },
            new_count
        ));
        for op in &ops[start..stop] {
            match *op {
                Op::Eq(ia) => text.push_str(&format!(" {}\n", a[ia])),
                Op::Del(ia) => text.push_str(&format!("-{}\n", a[ia])),
                Op::Ins(ib) => text.push_str(&format!("+{}\n", b[ib])),
            }
        }
        i = stop;
    }
    Diff { text, added, removed }
}

fn cap_chars(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let mut cut = max;
    while !s.is_char_boundary(cut) {
        cut -= 1;
    }
    format!("{}\n… (diff cortado)", &s[..cut])
}

// ───────────────────────────── histórico / Desfazer ─────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChangeMeta {
    pub change_id: String,
    pub path: String,
    pub created: bool,
    pub sha_before: Option<String>,
    pub sha_after: String,
    pub engine: String,
    pub at: String,
    #[serde(default)]
    pub undone: bool,
}

fn history_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("NEO_FILE_HISTORY_DIR") {
        return PathBuf::from(dir);
    }
    dirs::data_dir()
        .unwrap_or_else(std::env::temp_dir)
        .join("neo-desktop")
        .join("file-history")
}

fn valid_change_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
}

fn prune_history(root: &Path) {
    let Ok(entries) = std::fs::read_dir(root) else { return };
    let mut dirs: Vec<(std::time::SystemTime, PathBuf)> = entries
        .flatten()
        .filter_map(|e| {
            let t = e.metadata().ok()?.modified().ok()?;
            Some((t, e.path()))
        })
        .collect();
    if dirs.len() <= HISTORY_KEPT {
        return;
    }
    dirs.sort_by(|x, y| y.0.cmp(&x.0));
    for (_, p) in dirs.into_iter().skip(HISTORY_KEPT) {
        let _ = std::fs::remove_dir_all(p);
    }
}

/// Grava num temporário da MESMA pasta e renomeia por cima: um crash ou disco cheio
/// no meio deixa o original intacto (o `fs::write` antigo truncava primeiro). O alvo
/// é o arquivo real — renomear por cima de um symlink trocaria o link por um arquivo.
fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let target = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let parent = target
        .parent()
        .ok_or_else(|| format!("caminho sem pasta: {}", target.display()))?;
    std::fs::create_dir_all(parent)
        .map_err(|e| format!("falha ao criar a pasta {}: {}", parent.display(), e))?;
    let name = target
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "arquivo".to_string());
    let tmp = parent.join(format!(
        ".{}.neo-{}.tmp",
        name,
        &uuid::Uuid::new_v4().simple().to_string()[..8]
    ));
    let write = || -> std::io::Result<()> {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
        if let Ok(meta) = std::fs::metadata(&target) {
            let _ = std::fs::set_permissions(&tmp, meta.permissions());
        }
        std::fs::rename(&tmp, &target)
    };
    write().map_err(|e| {
        let _ = std::fs::remove_file(&tmp);
        format!("falha ao gravar {}: {}", target.display(), e)
    })
}

fn read_current(path: &Path) -> Option<Vec<u8>> {
    std::fs::read(path).ok()
}

/// Conferência final + backup + escrita. Separado de [`commit`] (que aprova e emite
/// evento) pra ser testável sem janela.
pub fn apply_change(
    path: &Path,
    before: Option<&[u8]>,
    after: &[u8],
    engine: &str,
) -> Result<ChangeMeta, Value> {
    let current = read_current(path);
    let unchanged = match (before, current.as_deref()) {
        (Some(b), Some(c)) => b == c,
        (None, None) => true,
        _ => false,
    };
    if !unchanged {
        return Err(json!({
            "status": "stale",
            "message": "o arquivo mudou desde a leitura (alguém editou ou ele foi criado/apagado) — releia antes de alterar",
            "current_sha256": current.as_deref().map(sha256_hex),
        }));
    }
    let change_id = uuid::Uuid::new_v4().to_string();
    let meta = ChangeMeta {
        change_id: change_id.clone(),
        path: path.to_string_lossy().to_string(),
        created: before.is_none(),
        sha_before: before.map(sha256_hex),
        sha_after: sha256_hex(after),
        engine: engine.to_string(),
        at: chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
        undone: false,
    };
    let root = history_dir();
    let dir = root.join(&change_id);
    let backup = || -> std::io::Result<()> {
        std::fs::create_dir_all(&dir)?;
        if let Some(b) = before {
            std::fs::write(dir.join("before"), b)?;
        }
        std::fs::write(dir.join("meta.json"), serde_json::to_vec_pretty(&meta).unwrap_or_default())
    };
    // Sem backup não grava: o Desfazer é parte da promessa de "aplica direto".
    backup().map_err(|e| {
        json!({"status": "error", "message": format!("não consegui guardar o backup ({}) — nada foi gravado", e)})
    })?;
    atomic_write(path, after).map_err(|e| {
        let _ = std::fs::remove_dir_all(&dir);
        json!({"status": "error", "message": e})
    })?;
    prune_history(&root);
    info!(
        "[file_ops] {} gravou {} ({} bytes, change={})",
        engine,
        path.display(),
        after.len(),
        change_id
    );
    Ok(meta)
}

/// Desfaz uma alteração — só se o arquivo ainda for exatamente o que o Neo gravou.
pub fn undo_change(change_id: &str) -> Result<ChangeMeta, String> {
    if !valid_change_id(change_id) {
        return Err("id de alteração inválido".to_string());
    }
    let dir = history_dir().join(change_id);
    let raw = std::fs::read(dir.join("meta.json"))
        .map_err(|_| "não achei o backup dessa alteração (pode ter expirado)".to_string())?;
    let mut meta: ChangeMeta =
        serde_json::from_slice(&raw).map_err(|e| format!("backup corrompido: {}", e))?;
    if meta.undone {
        return Err("essa alteração já foi desfeita".to_string());
    }
    let path = PathBuf::from(&meta.path);
    let current = read_current(&path);
    if current.as_deref().map(sha256_hex).as_deref() != Some(meta.sha_after.as_str()) {
        return Err("o arquivo mudou depois dessa alteração — não desfaço por cima".to_string());
    }
    if meta.created {
        std::fs::remove_file(&path).map_err(|e| format!("falha ao remover o arquivo criado: {}", e))?;
    } else {
        let before = std::fs::read(dir.join("before"))
            .map_err(|_| "o backup do conteúdo anterior sumiu".to_string())?;
        atomic_write(&path, &before)?;
    }
    meta.undone = true;
    let _ = std::fs::write(dir.join("meta.json"), serde_json::to_vec_pretty(&meta).unwrap_or_default());
    info!("[file_ops] desfeita a alteração {} em {}", change_id, meta.path);
    Ok(meta)
}

// ───────────────────────────── aprovação + eventos ─────────────────────────────

fn pending() -> &'static Mutex<HashMap<String, oneshot::Sender<bool>>> {
    static PENDING: OnceLock<Mutex<HashMap<String, oneshot::Sender<bool>>>> = OnceLock::new();
    PENDING.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Clique no card (Aprovar/Rejeitar).
pub fn respond(change_id: &str, approved: bool) -> Result<(), String> {
    let tx = pending()
        .lock()
        .map_err(|_| "estado de aprovação corrompido".to_string())?
        .remove(change_id)
        .ok_or_else(|| "esse pedido já expirou ou foi respondido".to_string())?;
    tx.send(approved).map_err(|_| "o pedido já tinha sido encerrado".to_string())
}

/// Parar: todo pedido de aprovação aberto vira rejeição.
pub fn cancel_pending() {
    if let Ok(mut map) = pending().lock() {
        map.clear();
    }
}

fn emit(app: &AppHandle, data: Value) {
    let evt = crate::commands::AgentEvent {
        event_type: "file_change".to_string(),
        data,
        timestamp: chrono::Utc::now().to_rfc3339(),
    };
    if let Err(e) = app.emit("agent-event", &evt) {
        warn!("[file_ops] falha ao emitir file_change: {}", e);
    }
}

async fn ask_user(app: &AppHandle, card: Value) -> bool {
    let id = card["changeId"].as_str().unwrap_or_default().to_string();
    let (tx, rx) = oneshot::channel();
    if let Ok(mut map) = pending().lock() {
        map.insert(id.clone(), tx);
    }
    emit(app, card);
    let approved = matches!(tokio::time::timeout(APPROVAL_TIMEOUT, rx).await, Ok(Ok(true)));
    if let Ok(mut map) = pending().lock() {
        map.remove(&id);
    }
    approved
}

pub fn emit_undone(app: &AppHandle, meta: &ChangeMeta) {
    emit(
        app,
        json!({"changeId": meta.change_id, "path": meta.path, "status": "undone", "engine": meta.engine}),
    );
}

/// Diff → aprovação quando a regra pede → [`apply_change`] → evento. O JSON devolvido
/// é o ToolResult (status saved/denied/rejected/stale/error).
pub async fn commit(
    app: &AppHandle,
    path: &Path,
    before: Option<Vec<u8>>,
    after: Vec<u8>,
    engine: &str,
    summary: Option<String>,
) -> Value {
    let lossy = |b: &[u8]| {
        let body = if b.starts_with(&BOM) { &b[3..] } else { b };
        String::from_utf8_lossy(body).to_string()
    };
    let diff = unified_diff(
        &before.as_deref().map(lossy).unwrap_or_default(),
        &lossy(&after),
        3,
    );
    let path_s = path.to_string_lossy().to_string();
    let card_id = uuid::Uuid::new_v4().to_string();
    let card = |status: &str, reason: Option<&str>| {
        json!({
            // `cardId` é a identidade do card no chat: o `changeId` troca pro id do
            // backup quando grava (é ele que o Desfazer usa), e sem um id estável o
            // "pendente" e o "gravado" virariam dois cards.
            "cardId": card_id, "changeId": card_id, "path": path_s, "status": status, "kind": "write",
            "engine": engine, "reason": reason, "summary": summary,
            "created": before.is_none(), "linesAdded": diff.added,
            "linesRemoved": diff.removed, "diff": cap_chars(&diff.text, DIFF_UI_CHARS),
        })
    };

    match access_for(path, true) {
        Access::Deny(reason) => {
            return json!({"status": "denied", "path": path_s, "message": reason});
        }
        Access::Ask(reason) => {
            if !ask_user(app, card("pending", Some(&reason))).await {
                emit(app, card("rejected", Some(&reason)));
                return json!({
                    "status": "rejected", "path": path_s,
                    "message": "o usuário não aprovou essa alteração — nada foi gravado",
                });
            }
        }
        Access::Allow => {}
    }

    match apply_change(path, before.as_deref(), &after, engine) {
        Ok(meta) => {
            let mut data = card("applied", None);
            data["changeId"] = json!(meta.change_id);
            emit(app, data);
            json!({
                "status": "saved", "path": path_s, "change_id": meta.change_id,
                "created": meta.created, "bytes": after.len(), "bytes_written": after.len(),
                "sha256": meta.sha_after, "sha256_before": meta.sha_before,
                "lines_added": diff.added, "lines_removed": diff.removed,
                "diff": cap_chars(&diff.text, DIFF_RESULT_CHARS),
            })
        }
        Err(mut v) => {
            v["path"] = json!(path_s);
            emit(app, card("failed", v["message"].as_str()));
            v
        }
    }
}

// ───────────────────────────── tools ─────────────────────────────

fn arg_str<'a>(args: &'a Value, key: &str) -> Option<&'a str> {
    args.get(key).and_then(Value::as_str).filter(|s| !s.trim().is_empty())
}

fn arg_bool(args: &Value, key: &str) -> bool {
    match args.get(key) {
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => matches!(s.trim().to_lowercase().as_str(), "true" | "1" | "sim"),
        _ => false,
    }
}

fn modified_at(meta: &std::fs::Metadata) -> Option<String> {
    meta.modified().ok().map(|t| {
        let dt: chrono::DateTime<chrono::Local> = t.into();
        dt.format("%Y-%m-%d %H:%M").to_string()
    })
}

fn load_doc(path: &Path) -> Result<(TextDoc, std::fs::Metadata), Value> {
    let meta = std::fs::metadata(path).map_err(|e| {
        json!({"status": "not_found", "path": path.to_string_lossy(), "message": format!("não consegui abrir: {}", e)})
    })?;
    if !meta.is_file() {
        return Err(json!({"status": "error", "path": path.to_string_lossy(), "message": "não é um arquivo"}));
    }
    if meta.len() > MAX_TEXT_BYTES {
        return Err(json!({
            "status": "too_large", "path": path.to_string_lossy(), "size_bytes": meta.len(),
            "message": "arquivo grande demais pra tratar como texto (limite 5 MB)",
        }));
    }
    let bytes = std::fs::read(path).map_err(|e| {
        json!({"status": "error", "path": path.to_string_lossy(), "message": format!("falha ao ler: {}", e)})
    })?;
    let doc = decode_text(bytes)
        .map_err(|m| json!({"status": "error", "path": path.to_string_lossy(), "message": m}))?;
    Ok((doc, meta))
}

/// `read_user_file`: o texto vai no ToolResult — nada sobe pra VPS.
pub async fn run_read(app: &AppHandle, args: &Value) -> Result<Value, String> {
    let path = resolve_path(arg_str(args, "path").unwrap_or(""))?;
    match access_for(&path, false) {
        Access::Deny(reason) => return Ok(json!({"status": "denied", "message": reason})),
        Access::Ask(reason) => {
            let card = json!({
                "changeId": uuid::Uuid::new_v4().to_string(), "path": path.to_string_lossy(),
                "status": "pending", "kind": "read", "engine": "neo", "reason": reason,
            });
            if !ask_user(app, card.clone()).await {
                let mut rejected = card;
                rejected["status"] = json!("rejected");
                emit(app, rejected);
                return Ok(json!({"status": "rejected", "message": "o usuário não liberou a leitura desse arquivo"}));
            }
        }
        Access::Allow => {}
    }
    let (doc, meta) = match load_doc(&path) {
        Ok(v) => v,
        Err(v) => return Ok(v),
    };
    let offset = args.get("offset").and_then(Value::as_u64).unwrap_or(1) as usize;
    let limit = args.get("limit").and_then(Value::as_u64).unwrap_or(READ_DEFAULT_LINES as u64) as usize;
    let slice = read_slice(&doc, offset, limit);
    Ok(json!({
        "status": "ok", "path": path.to_string_lossy(), "sha256": sha256_hex(&doc.bytes),
        "size_bytes": doc.bytes.len(), "total_lines": slice.total_lines,
        "start_line": slice.start_line, "end_line": slice.end_line,
        "next_offset": slice.next_offset, "eol": doc.eol.label(), "bom": doc.bom,
        "modified_at": modified_at(&meta), "content": slice.content,
    }))
}

/// `edit_user_file`: troca de trechos exatos sobre a versão que o Neo leu.
pub async fn run_edit(app: &AppHandle, args: &Value) -> Result<Value, String> {
    let path = resolve_path(arg_str(args, "path").unwrap_or(""))?;
    let edits: Vec<EditOp> = match args.get("edits") {
        Some(v) if v.is_array() => serde_json::from_value(v.clone())
            .map_err(|e| format!("`edits` inválido: {}", e))?,
        // O modelo às vezes manda um trecho só no topo.
        _ => match (args.get("old_string"), args.get("new_string")) {
            (Some(Value::String(o)), Some(Value::String(n))) => vec![EditOp {
                old_string: o.clone(),
                new_string: n.clone(),
                replace_all: arg_bool(args, "replace_all"),
            }],
            _ => return Err("faltou `edits` (lista de {old_string, new_string})".to_string()),
        },
    };
    let Some(expected) = arg_str(args, "expected_sha256") else {
        return Ok(json!({
            "status": "error",
            "message": "faltou expected_sha256 — leia o arquivo com read_user_file e passe o sha256 que veio",
        }));
    };
    let (doc, _) = match load_doc(&path) {
        Ok(v) => v,
        Err(v) => return Ok(v),
    };
    let current = sha256_hex(&doc.bytes);
    if !current.eq_ignore_ascii_case(expected.trim()) {
        return Ok(json!({
            "status": "stale", "path": path.to_string_lossy(), "current_sha256": current,
            "message": "o arquivo não é mais a versão que você leu — releia antes de editar",
        }));
    }
    let new_text = match apply_edits(&doc, &edits) {
        Ok(t) => t,
        Err(mut v) => {
            v["path"] = json!(path.to_string_lossy());
            return Ok(v);
        }
    };
    let after = encode_text(&new_text, doc.bom);
    if after == doc.bytes {
        return Ok(json!({"status": "unchanged", "path": path.to_string_lossy(), "message": "os trechos não mudaram nada"}));
    }
    Ok(commit(app, &path, Some(doc.bytes), after, "neo", None).await)
}

/// `write_file`: arquivo inteiro (novo, substituição ou append).
pub async fn run_write(app: &AppHandle, args: &Value) -> Result<Value, String> {
    let path = resolve_path(arg_str(args, "path").unwrap_or(""))?;
    let content = args.get("content").and_then(Value::as_str).unwrap_or("");
    let append = arg_str(args, "mode").map(|m| m.eq_ignore_ascii_case("append")).unwrap_or(false);
    let existing = match std::fs::metadata(&path) {
        Ok(m) if m.is_file() => Some((std::fs::read(&path).map_err(|e| format!("falha ao ler: {}", e))?, m)),
        Ok(_) => return Err(format!("{} não é um arquivo", path.display())),
        Err(_) => None,
    };
    let before = existing.as_ref().map(|(b, _)| b.clone());
    if let (Some((bytes, meta)), false) = (&existing, append) {
        let current = sha256_hex(bytes);
        match arg_str(args, "expected_sha256") {
            Some(exp) if !exp.trim().eq_ignore_ascii_case(&current) => {
                return Ok(json!({
                    "status": "stale", "path": path.to_string_lossy(), "current_sha256": current,
                    "message": "o arquivo não é mais a versão que você leu — releia antes de sobrescrever",
                }));
            }
            Some(_) => {}
            // Legado: sem hash, só com overwrite explícito (o core libera depois de ver
            // o conflito ou a leitura). Sem nada, devolve o conflito como sempre.
            None if !arg_bool(args, "overwrite") => {
                let ts = |t: std::io::Result<std::time::SystemTime>| {
                    t.ok().map(|t| chrono::DateTime::<chrono::Local>::from(t).format("%Y-%m-%d %H:%M").to_string())
                };
                return Ok(json!({
                    "status": "conflict", "path": path.to_string_lossy(),
                    "existing": {
                        "name": path.file_name().map(|n| n.to_string_lossy().to_string()),
                        "size_bytes": meta.len(), "sha256": current,
                        "created_at": ts(meta.created()), "modified_at": ts(meta.modified()),
                    },
                }));
            }
            None => {}
        }
    }
    let after = match (&before, append) {
        (Some(b), true) => {
            let mut v = b.clone();
            v.extend_from_slice(content.as_bytes());
            v
        }
        _ => content.as_bytes().to_vec(),
    };
    if before.as_deref() == Some(after.as_slice()) {
        return Ok(json!({"status": "unchanged", "path": path.to_string_lossy()}));
    }
    let mut result = commit(app, &path, before, after, "neo", None).await;
    result["mode"] = json!(if append { "append" } else { "write" });
    Ok(result)
}

// ───────────────────────────── opencode ─────────────────────────────

/// O binário do opencode. Instalado via nvm ele NÃO está no PATH de um app aberto
/// pelo Finder — daí a busca nos lugares de instalação conhecidos.
pub fn opencode_bin() -> Option<PathBuf> {
    static BIN: OnceLock<Option<PathBuf>> = OnceLock::new();
    BIN.get_or_init(find_opencode).clone()
}

fn find_opencode() -> Option<PathBuf> {
    let usable = |p: &Path| p.is_file();
    if let Ok(p) = std::env::var("NEO_OPENCODE_BIN") {
        let p = PathBuf::from(p);
        if usable(&p) {
            return Some(p);
        }
    }
    if let Some(path) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&path) {
            let p = dir.join("opencode");
            if usable(&p) {
                return Some(p);
            }
        }
    }
    let home = dirs::home_dir()?;
    let nvm = home.join(".nvm/versions/node");
    if let Ok(entries) = std::fs::read_dir(&nvm) {
        let mut found: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path().join("bin/opencode"))
            .filter(|p| usable(p))
            .collect();
        found.sort();
        if let Some(p) = found.pop() {
            return Some(p);
        }
    }
    [
        home.join(".opencode/bin/opencode"),
        home.join(".bun/bin/opencode"),
        PathBuf::from("/opt/homebrew/bin/opencode"),
        PathBuf::from("/usr/local/bin/opencode"),
    ]
    .into_iter()
    .find(|p| usable(p))
}

/// Tools que ESTE cliente executa — vai no metadata (`client_tools`) pro core só
/// oferecer ao Neo o que existe aqui (um app antigo não recebe tool sem braço).
pub fn client_tools() -> Vec<&'static str> {
    let mut tools = vec!["read_user_file", "edit_user_file", "write_file"];
    if opencode_bin().is_some() {
        tools.push("delegate_file_edit");
    }
    tools
}

/// Agente próprio: só ler e editar. Sem shell, sem web, sem sair da pasta, sem
/// `.env`. O opencode avalia a ÚLTIMA regra que casa, e estas vêm depois das
/// padrão — por isso negar tudo com `*` primeiro e liberar o mínimo depois.
fn opencode_config() -> String {
    json!({
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": false,
        "share": "disabled",
        "agent": {
            OPENCODE_AGENT: {
                "mode": "primary",
                "description": "Editor de arquivos do Neo",
                "prompt": "Você edita UM arquivo de texto conforme a instrução. Não rode comandos, não crie outros arquivos e preserve tudo que a instrução não mandou mudar.",
                "permission": {
                    "*": "deny",
                    "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny"},
                    "edit": "allow", "glob": "allow", "grep": "allow", "list": "allow",
                    "bash": "deny", "webfetch": "deny", "external_directory": "deny",
                    "doom_loop": "deny", "question": "deny", "task": "deny",
                },
            },
        },
    })
    .to_string()
}

#[derive(Debug, Default)]
pub struct OpencodeRun {
    pub session_id: Option<String>,
    pub summary: Option<String>,
    pub cost_usd: f64,
    pub edits: usize,
    pub tool_errors: Vec<String>,
}

pub fn parse_opencode_events(lines: &[String]) -> OpencodeRun {
    let mut run = OpencodeRun::default();
    for line in lines {
        let Ok(e) = serde_json::from_str::<Value>(line.trim()) else { continue };
        if let Some(s) = e.get("sessionID").and_then(Value::as_str) {
            run.session_id = Some(s.to_string());
        }
        let part = e.get("part").cloned().unwrap_or(Value::Null);
        match e.get("type").and_then(Value::as_str) {
            Some("text") => {
                if let Some(t) = part.get("text").and_then(Value::as_str).filter(|t| !t.trim().is_empty()) {
                    run.summary = Some(t.trim().to_string());
                }
            }
            Some("step_finish") => {
                run.cost_usd += part.get("cost").and_then(Value::as_f64).unwrap_or(0.0);
            }
            Some("tool_use") => {
                let tool = part.get("tool").and_then(Value::as_str).unwrap_or("?");
                let state = part.get("state").cloned().unwrap_or(Value::Null);
                match state.get("status").and_then(Value::as_str) {
                    Some("completed") if matches!(tool, "edit" | "write" | "patch" | "multiedit") => run.edits += 1,
                    Some("error") => {
                        let msg = state
                            .get("error")
                            .or_else(|| state.get("output"))
                            .and_then(Value::as_str)
                            .unwrap_or("");
                        run.tool_errors.push(format!("{}: {}", tool, msg.chars().take(160).collect::<String>()));
                    }
                    _ => {}
                }
            }
            _ => {}
        }
    }
    run
}

/// `delegate_file_edit`: o opencode edita uma CÓPIA numa pasta temporária; o
/// resultado volta pelo mesmo [`commit`] (regras, hash, backup, card, Desfazer).
/// Assim ele nunca toca o disco do usuário direto nem enxerga os vizinhos do arquivo.
pub async fn run_delegate(app: &AppHandle, args: &Value) -> Result<Value, String> {
    let path = resolve_path(arg_str(args, "path").unwrap_or(""))?;
    let instruction = arg_str(args, "instruction").ok_or("faltou `instruction`")?.trim().to_string();
    if let Access::Deny(reason) = access_for(&path, true) {
        return Ok(json!({"status": "denied", "path": path.to_string_lossy(), "message": reason}));
    }
    let Some(bin) = opencode_bin() else {
        return Ok(json!({"status": "unavailable", "message": "o opencode não está instalado nesta máquina"}));
    };
    let (doc, _) = match load_doc(&path) {
        Ok(v) => v,
        Err(v) => return Ok(v),
    };
    if let Some(exp) = arg_str(args, "expected_sha256") {
        let current = sha256_hex(&doc.bytes);
        if !exp.trim().eq_ignore_ascii_case(&current) {
            return Ok(json!({"status": "stale", "current_sha256": current, "message": "o arquivo mudou desde a leitura"}));
        }
    }
    let model = arg_str(args, "model").unwrap_or(OPENCODE_DEFAULT_MODEL).to_string();
    let timeout = args
        .get("timeout_s")
        .and_then(Value::as_u64)
        .map(|s| Duration::from_secs(s.clamp(30, 1800)))
        .unwrap_or(OPENCODE_DEFAULT_TIMEOUT);
    let file_name = path
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .ok_or("caminho sem nome de arquivo")?;

    let work = std::env::temp_dir().join("neo-opencode").join(uuid::Uuid::new_v4().to_string());
    std::fs::create_dir_all(&work).map_err(|e| format!("falha ao preparar a pasta de trabalho: {}", e))?;
    let copy = work.join(&file_name);
    if let Err(e) = std::fs::write(&copy, &doc.bytes) {
        let _ = std::fs::remove_dir_all(&work);
        return Err(format!("falha ao copiar o arquivo: {}", e));
    }
    let prompt = format!(
        "{}\n\n---\nArquivo a editar: `{}` (está nesta pasta; é uma cópia de trabalho).\n\
         Regras: edite SOMENTE esse arquivo com a ferramenta de edição; não crie nem apague \
         arquivos; preserve tudo que a instrução não mandou mudar. Ao terminar, responda em \
         até 3 frases o que mudou.",
        instruction, file_name
    );

    let mut cmd = tokio::process::Command::new(&bin);
    cmd.args(["run", "--format", "json", "--agent", OPENCODE_AGENT, "--model", &model, "--dir"])
        .arg(&work);
    if let Some(session) = arg_str(args, "session_id") {
        cmd.args(["--session", session]);
    }
    cmd.arg(&prompt)
        .current_dir(&work)
        .env("OPENCODE_CONFIG_CONTENT", opencode_config())
        // Um opencode.json numa pasta do usuário não pode afrouxar as regras acima,
        // nem um CLAUDE.md de terceiro virar instrução.
        .env("OPENCODE_DISABLE_PROJECT_CONFIG", "1")
        .env("OPENCODE_DISABLE_CLAUDE_CODE", "1")
        .env("OPENCODE_DISABLE_AUTOUPDATE", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    #[cfg(unix)]
    cmd.process_group(0);

    info!("[file_ops] opencode ({}) editando {} — {}", model, path.display(), instruction.chars().take(120).collect::<String>());
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            let _ = std::fs::remove_dir_all(&work);
            return Ok(json!({"status": "error", "message": format!("falha ao iniciar o opencode: {}", e)}));
        }
    };
    let pid = child.id();
    if let Some(p) = pid {
        crate::services::shell::track_pid(p);
    }
    let stdout = child.stdout.take();
    let mut stderr = child.stderr.take();
    let stderr_task = tokio::spawn(async move {
        let mut buf = String::new();
        if let Some(s) = stderr.as_mut() {
            let _ = s.take(64 * 1024).read_to_string(&mut buf).await;
        }
        buf
    });
    let run = async {
        let mut lines = Vec::new();
        if let Some(out) = stdout {
            let mut reader = BufReader::new(out).lines();
            while let Ok(Some(l)) = reader.next_line().await {
                lines.push(l);
            }
        }
        (lines, child.wait().await)
    };
    let outcome = tokio::time::timeout(timeout, run).await;
    if let Some(p) = pid {
        crate::services::shell::untrack_pid(p);
    }
    let (lines, status) = match outcome {
        Ok(v) => v,
        Err(_) => {
            if let Some(p) = pid {
                crate::services::shell::kill_tree(p);
            }
            let _ = std::fs::remove_dir_all(&work);
            return Ok(json!({
                "status": "timeout",
                "message": format!("o opencode passou de {}s e foi encerrado — nada foi gravado", timeout.as_secs()),
            }));
        }
    };
    let stderr_text = stderr_task.await.unwrap_or_default();
    let oc = parse_opencode_events(&lines);
    let edited = std::fs::read(&copy);
    let _ = std::fs::remove_dir_all(&work);

    let base = json!({
        "engine": "opencode", "model": model, "session_id": oc.session_id,
        "summary": oc.summary, "cost_usd": (oc.cost_usd * 1e6).round() / 1e6,
        "tool_errors": oc.tool_errors,
    });
    let with = |mut v: Value| {
        if let (Some(dst), Some(src)) = (v.as_object_mut(), base.as_object()) {
            for (k, val) in src {
                dst.entry(k.clone()).or_insert(val.clone());
            }
        }
        v
    };
    let Ok(after) = edited else {
        return Ok(with(json!({"status": "error", "message": "o opencode apagou ou renomeou o arquivo — nada foi gravado"})));
    };
    if status.map(|s| !s.success()).unwrap_or(true) && oc.edits == 0 {
        let tail: String = stderr_text.chars().rev().take(400).collect::<Vec<_>>().into_iter().rev().collect();
        return Ok(with(json!({"status": "error", "message": format!("o opencode falhou: {}", tail.trim())})));
    }
    if after == doc.bytes {
        return Ok(with(json!({"status": "unchanged", "path": path.to_string_lossy(), "message": "o opencode terminou sem mudar o arquivo"})));
    }
    if let Err(m) = decode_text(after.clone()) {
        return Ok(with(json!({"status": "error", "message": format!("o resultado do opencode não é texto válido ({}) — nada foi gravado", m)})));
    }
    let result = commit(app, &path, Some(doc.bytes), after, "opencode", oc.summary.clone()).await;
    Ok(with(result))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn doc(s: &str) -> TextDoc {
        decode_text(s.as_bytes().to_vec()).unwrap()
    }

    fn op(o: &str, n: &str) -> EditOp {
        EditOp { old_string: o.into(), new_string: n.into(), replace_all: false }
    }

    #[test]
    fn resolve_rejects_relative_and_parent_dir() {
        assert!(resolve_path("notas.md").is_err());
        assert!(resolve_path("~/Desktop/nova/../../.ssh/x").is_err());
        assert!(resolve_path("~/Desktop/x.md").unwrap().is_absolute());
    }

    #[test]
    fn policy_sensitive_asks_system_denies_desktop_allows() {
        let home = dirs::home_dir().unwrap();
        assert!(matches!(access_for(&home.join(".ssh/config"), false), Access::Ask(_)));
        assert!(matches!(access_for(&home.join(".zshrc"), true), Access::Ask(_)));
        assert!(matches!(access_for(Path::new("/etc/hosts"), true), Access::Deny(_)));
        assert_eq!(access_for(Path::new("/etc/hosts"), false), Access::Allow);
        assert!(matches!(access_for(&home.join("projeto/.env"), false), Access::Ask(_)));
        if let Some(desk) = dirs::desktop_dir() {
            assert_eq!(access_for(&desk.join("neo-teste-inexistente/x.md"), true), Access::Allow);
        }
        assert!(matches!(access_for(&home.join("neo-fora-das-raizes.txt"), true), Access::Ask(_)));
    }

    #[test]
    fn decode_detects_bom_crlf_binary_and_encoding() {
        let d = decode_text(b"\xEF\xBB\xBFa\r\nb\r\n".to_vec()).unwrap();
        assert!(d.bom);
        assert_eq!(d.eol, Eol::Crlf);
        assert_eq!(d.text, "a\r\nb\r\n");
        assert!(decode_text(b"a\0b".to_vec()).unwrap_err().contains("binário"));
        assert!(decode_text(vec![0xC3, 0x28]).unwrap_err().contains("UTF-8"));
    }

    #[test]
    fn edit_unique_ambiguous_missing_and_replace_all() {
        let d = doc("- banana\n- uva\n- banana\n");
        assert_eq!(apply_edits(&d, &[op("- uva", "- maçã")]).unwrap(), "- banana\n- maçã\n- banana\n");
        assert_eq!(apply_edits(&d, &[op("banana", "x")]).unwrap_err()["status"], "ambiguous");
        assert_eq!(apply_edits(&d, &[op("abacaxi", "x")]).unwrap_err()["status"], "no_match");
        let all = EditOp { old_string: "banana".into(), new_string: "kiwi".into(), replace_all: true };
        assert_eq!(apply_edits(&d, &[all]).unwrap(), "- kiwi\n- uva\n- kiwi\n");
    }

    #[test]
    fn edit_no_match_explains_whitespace() {
        let d = doc("def f():\n    return 1\n");
        let err = apply_edits(&d, &[op("def f():\n  return 1", "x")]).unwrap_err();
        assert!(err["message"].as_str().unwrap().contains("espaços"));
    }

    #[test]
    fn edit_preserves_crlf_and_bom() {
        let d = decode_text(b"\xEF\xBB\xBFum\r\ndois\r\n".to_vec()).unwrap();
        let out = apply_edits(&d, &[op("um\ndois", "um\nDOIS")]).unwrap();
        assert_eq!(out, "um\r\nDOIS\r\n");
        assert_eq!(encode_text(&out, d.bom), b"\xEF\xBB\xBFum\r\nDOIS\r\n".to_vec());
    }

    #[test]
    fn slice_pages_by_line() {
        let d = doc("1\n2\n3\n4\n5\n");
        let s = read_slice(&d, 2, 2);
        assert_eq!((s.content.as_str(), s.start_line, s.end_line, s.total_lines, s.next_offset), ("2\n3\n", 2, 3, 5, Some(4)));
        assert_eq!(read_slice(&d, 4, 10).next_offset, None);
    }

    #[test]
    fn diff_marks_changes_with_context_and_splits_far_hunks() {
        let old = (1..=30).map(|i| format!("l{}", i)).collect::<Vec<_>>().join("\n") + "\n";
        let new = old.replace("l2\n", "L2\n").replace("l28\n", "L28\n");
        let d = unified_diff(&old, &new, 3);
        assert_eq!((d.added, d.removed), (2, 2));
        assert_eq!(d.text.matches("@@").count(), 4, "{}", d.text); // 2 hunks
        assert!(d.text.contains("-l2\n+L2\n"));
        assert!(d.text.starts_with("@@ -1,5 +1,5 @@"), "{}", d.text);
        assert!(unified_diff("a\n", "a\n", 3).text.is_empty());
        let created = unified_diff("", "novo\n", 3);
        assert!(created.text.starts_with("@@ -0,0 +1,1 @@"), "{}", created.text);
    }

    #[test]
    fn apply_change_backup_stale_and_undo_roundtrip() {
        let root = std::env::temp_dir().join(format!("neo-fileops-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        std::env::set_var("NEO_FILE_HISTORY_DIR", root.join("history"));
        let f = root.join("notas.md");
        std::fs::write(&f, "antes\n").unwrap();

        // Alguém mudou o arquivo entre a leitura e a gravação → não grava.
        let stale = apply_change(&f, Some(b"outra coisa\n"), b"depois\n", "neo").unwrap_err();
        assert_eq!(stale["status"], "stale");
        assert_eq!(std::fs::read_to_string(&f).unwrap(), "antes\n");

        let meta = apply_change(&f, Some(b"antes\n"), b"depois\n", "neo").unwrap();
        assert_eq!(std::fs::read_to_string(&f).unwrap(), "depois\n");
        undo_change(&meta.change_id).unwrap();
        assert_eq!(std::fs::read_to_string(&f).unwrap(), "antes\n");
        assert!(undo_change(&meta.change_id).unwrap_err().contains("já foi desfeita"));

        // Arquivo criado: Desfazer apaga; editado depois: não desfaz por cima.
        let novo = root.join("novo.txt");
        let m2 = apply_change(&novo, None, b"oi\n", "opencode").unwrap();
        std::fs::write(&novo, "mexido pelo usuário\n").unwrap();
        assert!(undo_change(&m2.change_id).unwrap_err().contains("mudou"));
        assert!(undo_change("../../etc").is_err());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn parses_opencode_json_events() {
        let lines = vec![
            r#"{"type":"step_start","sessionID":"ses_1","part":{}}"#.to_string(),
            r#"{"type":"tool_use","sessionID":"ses_1","part":{"tool":"edit","state":{"status":"completed"}}}"#.to_string(),
            r#"{"type":"tool_use","sessionID":"ses_1","part":{"tool":"read","state":{"status":"error","error":"The user has specified a rule"}}}"#.to_string(),
            r#"{"type":"step_finish","sessionID":"ses_1","part":{"cost":0.0003}}"#.to_string(),
            r#"{"type":"text","sessionID":"ses_1","part":{"text":"Troquei banana por maçã."}}"#.to_string(),
            "lixo que não é json".to_string(),
        ];
        let run = parse_opencode_events(&lines);
        assert_eq!(run.session_id.as_deref(), Some("ses_1"));
        assert_eq!(run.edits, 1);
        assert_eq!(run.tool_errors.len(), 1);
        assert_eq!(run.summary.as_deref(), Some("Troquei banana por maçã."));
        assert!((run.cost_usd - 0.0003).abs() < 1e-9);
    }

    #[test]
    fn opencode_config_denies_shell_and_env() {
        let cfg: Value = serde_json::from_str(&opencode_config()).unwrap();
        let perm = &cfg["agent"][OPENCODE_AGENT]["permission"];
        assert_eq!(perm["bash"], "deny");
        assert_eq!(perm["*"], "deny");
        assert_eq!(perm["read"]["*.env"], "deny");
        assert_eq!(perm["external_directory"], "deny");
    }
}
