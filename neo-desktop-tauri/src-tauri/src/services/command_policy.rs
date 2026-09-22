//! Quais comandos de shell rodam sozinhos e quais param para pedir permissão.
//!
//! Antes disto TODO `raw_command` do neo-core virava card de aprovação: `ls`,
//! `find`, `mkdir` — tudo. Num pedido de várias etapas ("lê essa pasta, resume e
//! leva pra outra") isso eram cinco cliques, e cada card parava o turno no meio.
//! A trava real nunca foi "é um comando", e sim **o que o comando faz com os bytes
//! do usuário**: copiar coisa grande, mexer em credencial, sobrescrever arquivo.
//!
//! **Por que a regra mora aqui e não no neo-core** — mesma razão do
//! [`super::file_ops`]: o comando chega montado por um LLM, e quem decide se ele
//! pode rodar tem que ser o lado que o modelo não escreve. Além disso metade dos
//! critérios (tamanho de verdade, symlink resolvido, destino já existe) só existe
//! nesta máquina: o neo-core roda na VPS e não vê este disco.
//!
//! **Desenho: allowlist, nunca blocklist.** Comando fora das tabelas → pergunta.
//! Construção de shell que a política não sabe conferir (redirecionamento,
//! subshell, `$VAR`, crase) → pergunta. Caminho que não dá pra resolver →
//! pergunta. O default é sempre o caro, nunca o perigoso.
//!
//! Três camadas, nesta ordem:
//! 1. **Sintaxe** ([`parse`]) — recusa o que não sabe ler, em vez de adivinhar.
//! 2. **Verbo** — `ls`/`cat`/`find` leem; `cp`/`mv`/`mkdir` escrevem; o resto pergunta.
//! 3. **Alvos** — cada caminho resolvido (symlink incluso) passa por
//!    sensível/sistema/tamanho/sobrescrita.

use log::warn;
use std::path::{Path, PathBuf};

use super::file_ops::{self, Access};

/// Maior volume copiado sem perguntar. Um `cp` de 300 MB dentro do disco é lento,
/// come espaço e não desfaz sozinho — vale o clique. Abaixo disso é rotina.
const MAX_AUTO_COPY_BYTES: u64 = 256 * 1024 * 1024;

/// Teto de arquivos tocados por uma cópia/movimentação automática. Milhares de
/// arquivinhos somam pouco byte e mesmo assim são uma operação grande.
const MAX_AUTO_ENTRIES: usize = 2_000;

/// Quantas entradas a medição de pasta percorre antes de desistir e perguntar.
/// Sem isto, medir `~/Documents` inteiro seguraria o turno.
const MEASURE_SCAN_CAP: usize = 50_000;

/// Mais que isto encadeado num comando só já não é "comando simples".
const MAX_SEGMENTS: usize = 12;

#[derive(Debug, Clone, PartialEq)]
pub enum Verdict {
    /// Pode rodar sem perguntar. Carrega o motivo para o card e para o log.
    Auto(String),
    /// Precisa do clique do usuário. Carrega o motivo, que vai no card.
    Ask(String),
}

impl Verdict {
    pub fn is_auto(&self) -> bool {
        matches!(self, Verdict::Auto(_))
    }

    pub fn reason(&self) -> &str {
        match self {
            Verdict::Auto(r) | Verdict::Ask(r) => r,
        }
    }
}

// ───────────────────────────── tabelas de verbos ─────────────────────────────

/// Só leem: nem criam, nem apagam, nem alteram nada no disco. A saída é capada
/// pelo executor (`shell::OUTPUT_CAP`), então "ler arquivo grande" não é risco.
/// Os que têm uma opção que grava (`sed -i`, `sort -o`, `find -delete`) entram
/// aqui mas passam por [`read_only_flags_ok`] antes.
const READ_ONLY: &[&str] = &[
    "ls", "pwd", "cat", "bat", "head", "tail", "wc", "file", "stat", "du", "df",
    "basename", "dirname", "realpath", "readlink", "echo", "printf", "date",
    "uname", "hostname", "whoami", "id", "groups", "sw_vers", "arch", "uptime",
    "tree", "sort", "uniq", "cut", "tr", "nl", "column", "rev", "grep", "egrep",
    "fgrep", "rg", "jq", "md5", "md5sum", "shasum", "sha256sum", "mdfind", "mdls",
    "sysctl", "ps", "printenv", "which", "type", "locale", "diff", "cmp", "seq",
    "expr", "find", "sed", "awk", "gawk", "defaults", "git", "tar", "unzip",
];

/// Escrevem, mas são a faxina do dia a dia. Só rodam sozinhos depois que TODOS os
/// caminhos passam por [`write_ok`] (e, para `cp`/`mv`, pelo teto de tamanho e
/// pela conferência de sobrescrita).
const WRITE_OK: &[&str] = &["mkdir", "touch", "rmdir", "cp", "mv"];

/// Onde um programa pode morar para ser reconhecido pelo nome. `./script.sh` não
/// entra: o nome não diz nada sobre o que ele faz.
const BIN_DIRS: &[&str] = &[
    "/bin", "/usr/bin", "/usr/local/bin", "/sbin", "/usr/sbin", "/opt/homebrew/bin",
    "/opt/local/bin",
];

// ───────────────────────────── camada 1: sintaxe ─────────────────────────────

#[derive(Debug, Clone, PartialEq)]
struct Tok {
    text: String,
    /// Tinha `*`/`?` FORA de aspas — ou seja, o shell vai expandir.
    glob: bool,
}

fn end_tok(seg: &mut Vec<Tok>, cur: &mut String, glob: &mut bool, started: &mut bool) {
    if *started {
        seg.push(Tok { text: std::mem::take(cur), glob: *glob });
        *glob = false;
        *started = false;
    }
}

fn end_seg(
    segments: &mut Vec<Vec<Tok>>,
    seg: &mut Vec<Tok>,
    cur: &mut String,
    glob: &mut bool,
    started: &mut bool,
) {
    end_tok(seg, cur, glob, started);
    if !seg.is_empty() {
        segments.push(std::mem::take(seg));
    }
}

/// Quebra o comando em segmentos (`;`, `&&`, `||`, `|`, quebra de linha) e cada
/// segmento em tokens, respeitando aspas e barra invertida.
///
/// Devolve `None` — "não sei ler isto" — para tudo que mudaria o sentido do que a
/// política vê: redirecionamento (`>`), subshell (`(`), substituição (`` ` ``,
/// `$(`), variável (`$VAR`), expansão de chaves (`{a,b}`), classe de glob (`[…]`),
/// background (`&`) e aspas não fechadas. Nenhum deles é necessário num `mv` ou
/// num `ls`, e todos escondem o alvo real da conferência das camadas seguintes.
fn parse(command: &str) -> Option<Vec<Vec<Tok>>> {
    let mut segments: Vec<Vec<Tok>> = Vec::new();
    let mut seg: Vec<Tok> = Vec::new();
    let mut cur = String::new();
    let mut glob = false;
    let mut started = false;

    let mut chars = command.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            ' ' | '\t' => end_tok(&mut seg, &mut cur, &mut glob, &mut started),
            '\n' | '\r' | ';' => {
                end_seg(&mut segments, &mut seg, &mut cur, &mut glob, &mut started)
            }
            '&' => {
                // `&&` separa; `&` sozinho joga pro background e a política perde
                // o processo de vista.
                if chars.peek() == Some(&'&') {
                    chars.next();
                    end_seg(&mut segments, &mut seg, &mut cur, &mut glob, &mut started);
                } else {
                    return None;
                }
            }
            '|' => {
                if chars.peek() == Some(&'|') {
                    chars.next();
                }
                end_seg(&mut segments, &mut seg, &mut cur, &mut glob, &mut started);
            }
            '>' | '<' | '(' | ')' | '`' | '$' | '{' | '[' => return None,
            '*' | '?' => {
                started = true;
                glob = true;
                cur.push(c);
            }
            '\\' => {
                started = true;
                cur.push(chars.next()?);
            }
            '\'' => {
                started = true;
                loop {
                    match chars.next() {
                        Some('\'') => break,
                        Some(ch) => cur.push(ch),
                        None => return None,
                    }
                }
            }
            '"' => {
                started = true;
                loop {
                    match chars.next() {
                        Some('"') => break,
                        // Dentro de aspas duplas o shell ainda expande `$` e crase.
                        Some('$') | Some('`') => return None,
                        Some('\\') => cur.push(chars.next()?),
                        Some(ch) => cur.push(ch),
                        None => return None,
                    }
                }
            }
            _ => {
                started = true;
                cur.push(c);
            }
        }
    }
    end_seg(&mut segments, &mut seg, &mut cur, &mut glob, &mut started);
    Some(segments)
}

// ───────────────────────────── caminhos ─────────────────────────────

fn home() -> PathBuf {
    dirs::home_dir()
        .map(|h| std::fs::canonicalize(&h).unwrap_or(h))
        .unwrap_or_else(|| PathBuf::from("/"))
}

fn temp_root() -> PathBuf {
    let t = std::env::temp_dir();
    std::fs::canonicalize(&t).unwrap_or(t)
}

/// `~`, relativo e `..` resolvidos; symlink também (via `file_ops::real_path`), que
/// é o que impede `~/atalho -> ~/.ssh` de passar por pasta comum.
fn resolve_arg(cwd: &Path, raw: &str) -> PathBuf {
    let p = if raw == "~" {
        home()
    } else if let Some(rest) = raw.strip_prefix("~/") {
        home().join(rest)
    } else {
        let pb = PathBuf::from(raw);
        if pb.is_absolute() {
            pb
        } else {
            cwd.join(pb)
        }
    };
    file_ops::real_path(&p)
}

/// Ler este caminho pode acontecer sem perguntar?
///
/// Delega o "é sensível?" pro `file_ops`: credencial, chave, rc de shell e `.env`
/// já estão catalogados lá, e ler qualquer um deles é exfiltração (o conteúdo vai
/// pro modelo). Uma lista só, um lugar só.
fn read_ok(p: &Path) -> Result<(), String> {
    match file_ops::access_for(p, false) {
        Access::Allow => Ok(()),
        Access::Ask(why) | Access::Deny(why) => Err(why),
    }
}

/// Escrever/apagar neste caminho pode acontecer sem perguntar?
///
/// De propósito mais largo que o `access_for(write=true)` do `file_ops`, que só
/// libera Desktop/Documentos/Downloads. A diferença é o que está em jogo: lá o
/// conteúdo é ESCRITO PELO MODELO e pode ir parar em qualquer arquivo; aqui são os
/// bytes que já são do usuário mudando de pasta. Arrumar `~/Music` ou `~/Projetos`
/// é faxina, não risco — então vale a pasta pessoal inteira, menos:
///
/// * pasta de sistema (`/etc`, `/usr`, `/Library`… — lista do `file_ops`);
/// * qualquer coisa sensível/segredo (mesma lista do [`read_ok`]);
/// * `~/Library` (estado de app: keychain, LaunchAgents, preferências);
/// * qualquer pasta oculta na raiz da home (`~/.config`, `~/.ssh`, `~/.git-*`).
///
/// Fora da home só o temporário do sistema.
fn write_ok(p: &Path) -> Result<(), String> {
    read_ok(p)?;
    let tmp = temp_root();
    if p.starts_with(&tmp) {
        return Ok(());
    }
    if file_ops::in_system_root(p) {
        return Err(format!(
            "`{}` é pasta do sistema — mexer aí só com a sua autorização",
            p.display()
        ));
    }
    let h = home();
    let rest = p.strip_prefix(&h).map_err(|_| {
        format!(
            "`{}` fica fora da sua pasta pessoal — não mexo aí sozinho",
            p.display()
        )
    })?;
    let first = rest
        .components()
        .next()
        .map(|c| c.as_os_str().to_string_lossy().to_string())
        .unwrap_or_default();
    if first.starts_with('.') {
        return Err(format!(
            "`{}` está em pasta oculta de configuração — não mexo aí sozinho",
            p.display()
        ));
    }
    if first == "Library" {
        return Err(format!(
            "`{}` está em ~/Library (estado dos seus apps) — não mexo aí sozinho",
            p.display()
        ));
    }
    Ok(())
}

/// `*` e `?` no ÚLTIMO componente, expandidos lendo a pasta. Curinga no meio do
/// caminho devolve `None` (vira pergunta): expandir pasta por pasta multiplicaria
/// a varredura sem necessidade — o neo-core manda caminho absoluto.
fn expand(tok: &Tok, cwd: &Path) -> Option<Vec<PathBuf>> {
    if !tok.glob {
        return Some(vec![resolve_arg(cwd, &tok.text)]);
    }
    let (dir_part, name_part) = match tok.text.rfind('/') {
        Some(i) => (&tok.text[..=i], &tok.text[i + 1..]),
        None => ("", tok.text.as_str()),
    };
    if dir_part.contains('*') || dir_part.contains('?') {
        return None;
    }
    let dir = if dir_part.is_empty() {
        cwd.to_path_buf()
    } else {
        resolve_arg(cwd, dir_part)
    };
    let mut out = Vec::new();
    for entry in std::fs::read_dir(&dir).ok()?.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        // O shell não casa oculto com `*` — a política não pode ser mais permissiva.
        if name.starts_with('.') && !name_part.starts_with('.') {
            continue;
        }
        if wildcard_match(name_part, &name) {
            out.push(file_ops::real_path(&dir.join(&name)));
        }
    }
    Some(out)
}

/// `*` (qualquer trecho) e `?` (um caractere), como o shell.
fn wildcard_match(pattern: &str, name: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let n: Vec<char> = name.chars().collect();
    // DP clássico: casa[i][j] = os i primeiros do padrão casam os j primeiros do nome.
    let mut casa = vec![vec![false; n.len() + 1]; p.len() + 1];
    casa[0][0] = true;
    for i in 1..=p.len() {
        casa[i][0] = casa[i - 1][0] && p[i - 1] == '*';
    }
    for i in 1..=p.len() {
        for j in 1..=n.len() {
            casa[i][j] = match p[i - 1] {
                '*' => casa[i - 1][j] || casa[i][j - 1],
                '?' => casa[i - 1][j - 1],
                c => casa[i - 1][j - 1] && c == n[j - 1],
            };
        }
    }
    casa[p.len()][n.len()]
}

/// Bytes e entradas de `p`, parando cedo. O `bool` é "passou do teto" — medir a
/// pasta inteira só para saber que é grande demais seria desperdício.
fn measure(p: &Path) -> (u64, usize, bool) {
    let md = match std::fs::symlink_metadata(p) {
        Ok(m) => m,
        Err(_) => return (0, 0, false),
    };
    if !md.is_dir() {
        return (md.len(), 1, md.len() > MAX_AUTO_COPY_BYTES);
    }
    let mut bytes = 0u64;
    let mut entries = 0usize;
    let mut scanned = 0usize;
    let mut stack = vec![p.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let rd = match std::fs::read_dir(&dir) {
            Ok(rd) => rd,
            Err(_) => continue,
        };
        for e in rd.flatten() {
            scanned += 1;
            if scanned > MEASURE_SCAN_CAP {
                return (bytes, entries, true);
            }
            let md = match e.metadata() {
                Ok(m) => m,
                Err(_) => continue,
            };
            if md.is_dir() {
                stack.push(e.path());
                continue;
            }
            bytes += md.len();
            entries += 1;
            if bytes > MAX_AUTO_COPY_BYTES || entries > MAX_AUTO_ENTRIES {
                return (bytes, entries, true);
            }
        }
    }
    (bytes, entries, false)
}

/// `mv` no mesmo volume é rename: instantâneo, não copia byte nenhum. Entre
/// volumes vira cópia + remoção, e aí o tamanho volta a importar.
#[cfg(unix)]
fn same_device(a: &Path, b: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    let dev = |p: &Path| std::fs::metadata(p).ok().map(|m| m.dev());
    let mut alvo = b.to_path_buf();
    while dev(&alvo).is_none() {
        match alvo.parent() {
            Some(parent) => alvo = parent.to_path_buf(),
            None => return false,
        }
    }
    match (dev(a), dev(&alvo)) {
        (Some(x), Some(y)) => x == y,
        _ => false,
    }
}

#[cfg(not(unix))]
fn same_device(_a: &Path, _b: &Path) -> bool {
    false
}

fn mostra(p: &Path) -> String {
    p.display().to_string()
}

// ───────────────────────────── camada 2 + 3: por verbo ─────────────────────────────

/// Opções que transformam um comando de leitura em comando de escrita.
fn read_only_flags_ok(prog: &str, args: &[Tok]) -> Result<(), String> {
    let tem = |needles: &[&str]| {
        args.iter()
            .any(|t| needles.iter().any(|n| t.text == *n || t.text.starts_with(n)))
    };
    match prog {
        "find" => {
            let acoes = [
                "-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint",
                "-fprint0", "-fprintf",
            ];
            if args.iter().any(|t| acoes.contains(&t.text.as_str())) {
                return Err("esse `find` executa ou apaga o que encontrar".to_string());
            }
        }
        // `-i` do sed aceita sufixo colado (`-i.bak`), daí o `starts_with`.
        "sed" => {
            if tem(&["-i", "--in-place"]) {
                return Err("`sed -i` reescreve o arquivo no lugar".to_string());
            }
        }
        "awk" | "gawk" => {
            if tem(&["-i", "--include"]) || args.iter().any(|t| t.text == "inplace") {
                return Err("esse `awk` está em modo in-place".to_string());
            }
        }
        "sort" => {
            if tem(&["-o", "--output"]) {
                return Err("`sort -o` grava a saída num arquivo".to_string());
            }
        }
        "sysctl" => {
            if tem(&["-w"]) || args.iter().any(|t| t.text.contains('=')) {
                return Err("`sysctl -w` altera parâmetro do sistema".to_string());
            }
        }
        "defaults" => {
            let sub = args.first().map(|t| t.text.as_str()).unwrap_or("");
            if !matches!(sub, "read" | "read-type" | "domains" | "help") {
                return Err(format!("`defaults {}` altera preferência de app", sub));
            }
        }
        "git" => {
            let sub = args
                .iter()
                .find(|t| !t.text.starts_with('-'))
                .map(|t| t.text.as_str())
                .unwrap_or("");
            let leitura = [
                "status", "log", "diff", "show", "rev-parse", "describe", "blame",
                "shortlog", "ls-files",
            ];
            if !leitura.contains(&sub) {
                return Err(format!("`git {}` pode alterar o repositório", sub));
            }
        }
        "tar" => {
            if !args
                .iter()
                .any(|t| t.text == "-t" || t.text == "--list" || t.text.starts_with("-t"))
            {
                return Err("`tar` fora de `-t` extrai ou cria arquivo".to_string());
            }
        }
        "unzip" => {
            if !args.iter().any(|t| t.text == "-l" || t.text == "-t" || t.text == "-v") {
                return Err("`unzip` fora de `-l` extrai arquivos".to_string());
            }
        }
        _ => {}
    }
    Ok(())
}

fn eval_read_only(prog: &str, args: &[Tok], cwd: &Path) -> Verdict {
    if let Err(why) = read_only_flags_ok(prog, args) {
        return Verdict::Ask(why);
    }
    for t in args {
        if t.text.starts_with('-') {
            continue;
        }
        // Só confere o que é caminho de verdade: um padrão de `grep` não pode
        // virar "caminho sensível" só porque a palavra parece nome de chave.
        let parece = t.text.contains('/') || t.text.starts_with('~');
        let p = resolve_arg(cwd, &t.text);
        if !parece && !p.exists() {
            continue;
        }
        if let Err(why) = read_ok(&p) {
            return Verdict::Ask(why);
        }
    }
    Verdict::Auto(format!("`{}` só lê", prog))
}

fn eval_create(prog: &str, args: &[Tok], cwd: &Path) -> Verdict {
    for t in args {
        if t.text.starts_with('-') && t.text.len() > 1 {
            let ok = t.text.chars().skip(1).all(|c| match prog {
                "mkdir" => matches!(c, 'p' | 'v'),
                "touch" => matches!(c, 'c' | 'a' | 'm'),
                "rmdir" => matches!(c, 'p'),
                _ => false,
            });
            if !ok {
                return Verdict::Ask(format!("`{} {}`: opção fora da política", prog, t.text));
            }
            continue;
        }
        if t.glob {
            return Verdict::Ask(format!("`{}` com curinga: não sei o que vai ser atingido", prog));
        }
        let p = resolve_arg(cwd, &t.text);
        if let Err(why) = write_ok(&p) {
            return Verdict::Ask(why);
        }
    }
    Verdict::Auto(format!("`{}` em pasta sua", prog))
}

fn eval_copy_move(prog: &str, args: &[Tok], cwd: &Path) -> Verdict {
    let mut operandos: Vec<&Tok> = Vec::new();
    let mut fim_das_flags = false;
    for t in args {
        if !fim_das_flags && t.text == "--" {
            fim_das_flags = true;
            continue;
        }
        if !fim_das_flags && t.text.starts_with('-') && t.text.len() > 1 {
            let ok = t.text.chars().skip(1).all(|c| match prog {
                "cp" => matches!(c, 'r' | 'R' | 'a' | 'p' | 'v' | 'n' | 'f' | 'i'),
                "mv" => matches!(c, 'v' | 'n' | 'f' | 'i'),
                _ => false,
            });
            if !ok {
                return Verdict::Ask(format!("`{} {}`: opção fora da política", prog, t.text));
            }
            continue;
        }
        operandos.push(t);
    }
    let Some((destino_tok, fontes)) = operandos.split_last() else {
        return Verdict::Ask(format!("`{}` sem argumentos", prog));
    };
    if fontes.is_empty() {
        return Verdict::Ask(format!("`{}` precisa de origem e destino", prog));
    }
    if destino_tok.glob {
        return Verdict::Ask("destino com curinga".to_string());
    }

    let destino = resolve_arg(cwd, &destino_tok.text);
    if let Err(why) = write_ok(&destino) {
        return Verdict::Ask(why);
    }
    let destino_eh_pasta = destino.is_dir();

    let mut bytes = 0u64;
    let mut entradas = 0usize;
    for ft in fontes {
        let Some(caminhos) = expand(ft, cwd) else {
            return Verdict::Ask("curinga no meio do caminho".to_string());
        };
        for fonte in caminhos {
            if !fonte.exists() {
                return Verdict::Ask(format!("`{}` não existe aqui", mostra(&fonte)));
            }
            if let Err(why) = read_ok(&fonte) {
                return Verdict::Ask(why);
            }
            // `mv` TIRA o arquivo de onde ele está: é escrita na origem também.
            // É esta linha que segura "mover arquivo delicado sem permissão".
            if prog == "mv" {
                if let Err(why) = write_ok(&fonte) {
                    return Verdict::Ask(why);
                }
            }
            let alvo = if destino_eh_pasta {
                match fonte.file_name() {
                    Some(n) => destino.join(n),
                    None => return Verdict::Ask("origem sem nome de arquivo".to_string()),
                }
            } else {
                if fontes.len() > 1 {
                    return Verdict::Ask(format!("`{}` com várias origens e destino que não é pasta", prog));
                }
                destino.clone()
            };
            if let Err(why) = write_ok(&alvo) {
                return Verdict::Ask(why);
            }
            if alvo.symlink_metadata().is_ok() {
                return Verdict::Ask(format!(
                    "`{}` já existe — sobrescrever é perda de dado, isso eu pergunto",
                    mostra(&alvo)
                ));
            }
            // `mv` no mesmo volume não copia byte nenhum: só o inode muda de nome.
            if prog == "cp" || !same_device(&fonte, &destino) {
                let (b, n, passou) = measure(&fonte);
                bytes += b;
                entradas += n;
                if passou || bytes > MAX_AUTO_COPY_BYTES || entradas > MAX_AUTO_ENTRIES {
                    return Verdict::Ask(format!(
                        "`{}` é grande demais pra copiar sem perguntar (limite: {} MB ou {} arquivos)",
                        mostra(&fonte),
                        MAX_AUTO_COPY_BYTES / (1024 * 1024),
                        MAX_AUTO_ENTRIES
                    ));
                }
            }
        }
    }
    Verdict::Auto(format!("`{}` entre pastas suas, sem sobrescrever nada", prog))
}

fn eval_segment(seg: &[Tok], cwd: &Path) -> Verdict {
    let Some(prog_tok) = seg.first() else {
        return Verdict::Ask("segmento vazio".to_string());
    };
    let bruto = prog_tok.text.as_str();
    let prog = match bruto.rsplit_once('/') {
        Some((dir, nome)) => {
            if !BIN_DIRS.contains(&dir) {
                return Verdict::Ask(format!("`{}` é um executável fora dos caminhos padrão", bruto));
            }
            nome
        }
        None => bruto,
    };
    let args = &seg[1..];

    if READ_ONLY.contains(&prog) {
        eval_read_only(prog, args, cwd)
    } else if prog == "cp" || prog == "mv" {
        eval_copy_move(prog, args, cwd)
    } else if WRITE_OK.contains(&prog) {
        eval_create(prog, args, cwd)
    } else {
        Verdict::Ask(format!("`{}` não está na lista de comandos simples", prog))
    }
}

/// O veredito para um comando inteiro. `working_dir` é o mesmo que o executor vai
/// usar (`shell::run_with`), para que caminho relativo seja conferido onde ele vai
/// de fato cair.
pub fn evaluate(command: &str, working_dir: Option<&str>) -> Verdict {
    // O parser lê sintaxe POSIX. No Windows o executor é PowerShell, com outras
    // regras — conferir com o leitor errado seria pior que perguntar.
    if cfg!(target_os = "windows") {
        return Verdict::Ask("no Windows a política ainda não lê PowerShell".to_string());
    }
    let command = command.trim();
    if command.is_empty() {
        return Verdict::Ask("comando vazio".to_string());
    }
    let cwd = working_dir
        .map(str::trim)
        .filter(|d| !d.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(home);

    let Some(segmentos) = parse(command) else {
        return Verdict::Ask(
            "o comando usa construção de shell que eu não confiro (redirecionamento, \
             subshell, variável ou aspas soltas)"
                .to_string(),
        );
    };
    if segmentos.is_empty() {
        return Verdict::Ask("comando vazio".to_string());
    }
    if segmentos.len() > MAX_SEGMENTS {
        return Verdict::Ask(format!("{} comandos encadeados de uma vez", segmentos.len()));
    }

    let mut motivos: Vec<String> = Vec::new();
    for seg in &segmentos {
        match eval_segment(seg, &cwd) {
            Verdict::Auto(motivo) => {
                if !motivos.contains(&motivo) {
                    motivos.push(motivo);
                }
            }
            ask => return ask,
        }
    }
    Verdict::Auto(motivos.join("; "))
}

// ───────────────────────────── aprovação local ─────────────────────────────
//
// Um `run_command` que chega como **tool_request** (passo de plano) não tem card:
// o neo-core mandou executar e fica esperando o resultado. Quando a política diz
// que esse comando precisa de aprovação, recusar em silêncio seria pior — some o
// passo e o modelo inventa o motivo. Então o card é desenhado AQUI, pelo cliente,
// com um id próprio (`local:`), e o `respond_to_command` responde a este canal em
// vez de mandar `CommandApproval` pro core (que não está esperando nenhum).

/// Quanto um card local espera o clique. Igual ao do `file_ops`.
const APPROVAL_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(15 * 60);

/// Prefixo que distingue o card desenhado pelo cliente do `command_request` do core.
pub const LOCAL_PREFIX: &str = "local:";

fn pending() -> &'static std::sync::Mutex<
    std::collections::HashMap<String, tokio::sync::oneshot::Sender<bool>>,
> {
    static PENDING: std::sync::OnceLock<
        std::sync::Mutex<std::collections::HashMap<String, tokio::sync::oneshot::Sender<bool>>>,
    > = std::sync::OnceLock::new();
    PENDING.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()))
}

/// Clique no card de um pedido local. Devolve erro se o id não é local ou já venceu.
pub fn respond(command_id: &str, approved: bool) -> Result<(), String> {
    let tx = pending()
        .lock()
        .map_err(|_| "estado de aprovação corrompido".to_string())?
        .remove(command_id)
        .ok_or_else(|| "esse pedido já expirou ou foi respondido".to_string())?;
    tx.send(approved)
        .map_err(|_| "o pedido já tinha sido encerrado".to_string())
}

/// Parar: todo card local aberto vira recusa.
pub fn cancel_pending() {
    if let Ok(mut map) = pending().lock() {
        map.clear();
    }
}

/// Desenha o card e espera o clique. `false` também no timeout e no Parar.
pub async fn ask_user(
    app: &tauri::AppHandle,
    session_id: &str,
    command: &str,
    reason: &str,
) -> bool {
    use tauri::Emitter;
    let id = format!("{}{}", LOCAL_PREFIX, uuid::Uuid::new_v4());
    let (tx, rx) = tokio::sync::oneshot::channel();
    if let Ok(mut map) = pending().lock() {
        map.insert(id.clone(), tx);
    }
    let evt = crate::commands::AgentEvent {
        event_type: "command_request".to_string(),
        data: serde_json::json!({
            "sessionId": session_id,
            "id": id,
            "cmd": command,
            "command": command,
            "explanation": format!("Preciso da sua ok: {}", reason),
            "isBackground": false,
            "status": "pending",
        }),
        timestamp: chrono::Utc::now().to_rfc3339(),
    };
    if let Err(e) = app.emit("agent-event", &evt) {
        warn!("[command_policy] falha ao emitir o card local: {}", e);
    }
    let aprovado = matches!(
        tokio::time::timeout(APPROVAL_TIMEOUT, rx).await,
        Ok(Ok(true))
    );
    if let Ok(mut map) = pending().lock() {
        map.remove(&id);
    }
    aprovado
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(cmd: &str) -> Verdict {
        evaluate(cmd, None)
    }

    #[test]
    fn leitura_roda_sozinha() {
        for cmd in [
            "ls -la ~/Documents",
            "pwd",
            "cat ~/Documents/notas.txt",
            "wc -l ~/Desktop/a.csv",
            "du -sh ~/Downloads",
            "grep -ri politica ~/Documents",
            "stat ~/Desktop",
            "file ~/Desktop/x.pdf",
        ] {
            assert!(v(cmd).is_auto(), "devia rodar sozinho: {cmd} -> {:?}", v(cmd));
        }
    }

    #[test]
    fn find_do_log_de_producao_roda_sozinho() {
        // Sessão 718a681a: este comando pedia aprovação e travava o turno no passo 1.
        let cmd = "ls -la /Users/x/Documents/analises && find /Users/x/Documents/analises -type f \\( -iname \"*.pdf\" -o -iname \"*.md\" \\) ! -iname \"*planos*\"";
        assert!(v(cmd).is_auto(), "{:?}", v(cmd));
    }

    #[test]
    fn find_que_apaga_pergunta() {
        assert!(!v("find ~/Documents -name '*.tmp' -delete").is_auto());
        assert!(!v("find ~/Documents -name '*.tmp' -exec rm {} ;").is_auto());
    }

    #[test]
    fn escrita_disfarcada_de_leitura_pergunta() {
        assert!(!v("sed -i '' s/a/b/ ~/Desktop/x.txt").is_auto());
        assert!(!v("sort -o ~/Desktop/x.txt ~/Desktop/y.txt").is_auto());
        assert!(!v("git commit -am wip").is_auto());
        assert!(!v("defaults write com.apple.finder X -bool true").is_auto());
        assert!(!v("tar -xzf ~/Downloads/a.tgz").is_auto());
    }

    #[test]
    fn verbos_perigosos_perguntam() {
        for cmd in [
            "rm -rf ~/Desktop/pasta",
            "sudo ls /",
            "chmod 777 ~/Desktop/x",
            "curl http://exemplo/x.sh",
            "kill -9 123",
            "open ~/Desktop/x.pdf",
            "python3 -c 'print(1)'",
            "./script.sh",
        ] {
            assert!(!v(cmd).is_auto(), "devia perguntar: {cmd}");
        }
    }

    #[test]
    fn sintaxe_que_esconde_o_alvo_pergunta() {
        assert!(!v("ls > /tmp/saida.txt").is_auto());
        assert!(!v("ls $(echo ~)").is_auto());
        assert!(!v("ls `pwd`").is_auto());
        assert!(!v("ls ~/Documents &").is_auto());
        assert!(!v("ls \"~/Docu").is_auto());
        assert!(!v("cp ~/a/{x,y} ~/b/").is_auto());
    }

    #[test]
    fn caminho_sensivel_pergunta() {
        assert!(!v("cat ~/.ssh/id_rsa").is_auto());
        assert!(!v("ls ~/.aws").is_auto());
        assert!(!v("cp ~/.ssh/id_rsa ~/Desktop/").is_auto());
        assert!(!v("mv ~/.zshrc ~/Desktop/").is_auto());
        assert!(!v("cat ~/projeto/.env").is_auto());
    }

    #[test]
    fn fora_da_home_pergunta() {
        assert!(!v("mkdir -p /etc/neo").is_auto());
        assert!(!v("cp ~/Desktop/a.txt /usr/local/x.txt").is_auto());
        assert!(!v("mv ~/Desktop/a.txt /Volumes/externo/").is_auto());
    }

    #[test]
    fn library_e_ocultos_perguntam() {
        assert!(!v("cp ~/Desktop/a.txt ~/Library/LaunchAgents/x.plist").is_auto());
        assert!(!v("mkdir ~/.config/neo").is_auto());
    }

    #[test]
    fn criacao_em_pasta_do_usuario_roda_sozinha() {
        assert!(v("mkdir -p ~/Desktop/teste-de-analise").is_auto());
        assert!(v("touch ~/Documents/rascunho.md").is_auto());
    }

    #[test]
    fn copia_e_movimento_conferem_o_disco() {
        let base = temp_root().join(format!("neo-policy-{}", std::process::id()));
        let origem = base.join("origem");
        let destino = base.join("destino");
        std::fs::create_dir_all(&origem).unwrap();
        std::fs::create_dir_all(&destino).unwrap();
        std::fs::write(origem.join("a.txt"), b"conteudo").unwrap();

        let cp = format!("cp {}/a.txt {}/", origem.display(), destino.display());
        assert!(v(&cp).is_auto(), "{:?}", v(&cp));

        let glob = format!("cp {}/*.txt {}/", origem.display(), destino.display());
        assert!(v(&glob).is_auto(), "{:?}", v(&glob));

        // Destino já existente: sobrescrever é perda de dado → pergunta.
        std::fs::write(destino.join("a.txt"), b"ja estava aqui").unwrap();
        assert!(!v(&cp).is_auto(), "sobrescrita devia perguntar");

        // Origem inexistente não passa: a política não avaliza o que não viu.
        let fantasma = format!("cp {}/nao-existe.txt {}/", origem.display(), destino.display());
        assert!(!v(&fantasma).is_auto());

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn copia_grande_pergunta() {
        let base = temp_root().join(format!("neo-policy-big-{}", std::process::id()));
        std::fs::create_dir_all(&base).unwrap();
        let grande = base.join("grande.bin");
        let f = std::fs::File::create(&grande).unwrap();
        f.set_len(MAX_AUTO_COPY_BYTES + 1).unwrap();
        drop(f);
        let destino = base.join("saida");
        std::fs::create_dir_all(&destino).unwrap();

        let cmd = format!("cp {} {}/", grande.display(), destino.display());
        let verdict = v(&cmd);
        assert!(!verdict.is_auto(), "cópia gigante devia perguntar: {:?}", verdict);
        assert!(verdict.reason().contains("grande demais"), "{:?}", verdict);

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn curinga_casa_como_o_shell() {
        assert!(wildcard_match("*.pdf", "plano.pdf"));
        assert!(!wildcard_match("*.pdf", "plano.txt"));
        assert!(wildcard_match("a?c", "abc"));
        assert!(!wildcard_match("a?c", "ac"));
        assert!(wildcard_match("*", "qualquer"));
    }

    #[test]
    fn encadeado_so_passa_se_todos_passarem() {
        assert!(v("ls ~/Desktop && pwd").is_auto());
        assert!(!v("ls ~/Desktop && rm -rf ~/Desktop").is_auto());
        assert!(!v("ls ~/Desktop | xargs rm").is_auto());
    }
}
