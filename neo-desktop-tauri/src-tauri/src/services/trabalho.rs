//! O retrato do trabalho do Daniel neste Mac: as janelas do VSCode abertas, o
//! branch de cada repositório delas e as sessões do Claude abertas. O core só
//! pede quando o roteador marca o turno como de trabalho
//! (neo-core/docs/ambiente-de-trabalho-plan.md §7); nada sai daqui sozinho.
//!
//! Sem a extensão `neo-vscode` (F5 do plano), as janelas vêm dos locks que a
//! extensão do Claude grava, um por janela, e que somem quando ela fecha
//! (medido na F0b). Por eles não dá para saber qual janela está focada nem o
//! arquivo ativo: os dois campos vão `null` até a extensão existir.

use crate::services::claude_sessoes::{self, abreviar, Raiz};
use serde::Deserialize;
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};

/// Repositórios procurados nas subpastas de uma janela que não é repositório (a
/// raiz `neo` não é; `neo-core`, `neo-desktop-tauri`… são).
const SUBREPOS_MAX: usize = 20;

/// Executa uma ação `trabalho.*` do canal de controle.
pub async fn execute(action: &str, args: &Value) -> Result<Value, String> {
    let a = args.clone();
    match action {
        "trabalho.contexto" => tauri::async_runtime::spawn_blocking(move || contexto(&a))
            .await
            .map_err(|e| format!("falha interna: {}", e))?,
        _ => Err(format!("ação desconhecida: {}", action)),
    }
}

/// `~/.claude/ide/<porta>.lock`. O `authToken` que mora no mesmo arquivo fica
/// de fora do struct: é lido do disco, mas nunca sai daqui.
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Lock {
    pid: Option<i64>,
    workspace_folders: Option<Vec<String>>,
    ide_name: Option<String>,
}

struct Janela {
    pastas: Vec<PathBuf>,
    ide: Option<String>,
}

fn contexto(args: &Value) -> Result<Value, String> {
    let raiz = Raiz::do_usuario().ok_or("sem home")?;
    let agora = claude_sessoes::agora_ms();
    Ok(retrato(&raiz, &raiz.home.join(".claude").join("ide"), args, agora))
}

fn retrato(raiz: &Raiz, pasta_dos_locks: &Path, args: &Value, agora: i64) -> Value {
    let janelas = janelas_abertas(pasta_dos_locks);
    let sessoes: Vec<Value> = claude_sessoes::fichas_vivas(raiz, args, agora)
        .iter()
        .map(|f| {
            json!({
                "sessionId": f.session_id,
                "nome": f.nome,
                "titulo": f.titulo,
                "origem": f.origem,
                "status": f.status,
                "ultimo_uso_min": f.ultimo_uso_min,
                "pasta": f.pasta.as_deref().map(|p| abreviar(p, &raiz.home)),
                "tarefa_do_neo": f.tarefa_do_neo,
                "janela": f.pasta.as_deref().and_then(|p| janela_da_pasta(&janelas, Path::new(p))),
            })
        })
        .collect();
    let janelas: Vec<Value> = janelas
        .iter()
        .map(|j| {
            json!({
                "pastas": j.pastas.iter().map(|p| abreviar(&p.display().to_string(), &raiz.home)).collect::<Vec<_>>(),
                "ide": j.ide,
                "focada": Value::Null,
                "arquivo_ativo": Value::Null,
                "branch": branches(&j.pastas),
            })
        })
        .collect();
    json!({
        "fonte": "arquivos",
        "gerado_em": agora / 1000,
        "janelas": janelas,
        "sessoes": sessoes,
    })
}

/// Uma janela por lock cujo processo do Code ainda existe (um Code que morreu
/// sem fechar as janelas deixa o lock para trás).
fn janelas_abertas(pasta_dos_locks: &Path) -> Vec<Janela> {
    let Ok(entradas) = fs::read_dir(pasta_dos_locks) else { return Vec::new() };
    let mut janelas: Vec<Janela> = entradas
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("lock"))
        .filter_map(|p| serde_json::from_str::<Lock>(&fs::read_to_string(p).ok()?).ok())
        .filter(|l| l.pid.is_some_and(|pid| pid > 0 && unsafe { libc::kill(pid as i32, 0) } == 0))
        .map(|l| Janela {
            pastas: l.workspace_folders.unwrap_or_default().into_iter().map(PathBuf::from).collect(),
            ide: l.ide_name,
        })
        .filter(|j| !j.pastas.is_empty())
        .collect();
    janelas.sort_by(|a, b| a.pastas.cmp(&b.pastas));
    janelas
}

/// A janela cuja pasta contém a da sessão; com pastas aninhadas, a mais funda.
fn janela_da_pasta(janelas: &[Janela], pasta: &Path) -> Option<usize> {
    janelas
        .iter()
        .enumerate()
        .flat_map(|(i, j)| j.pastas.iter().map(move |p| (i, p)))
        .filter(|(_, p)| pasta.starts_with(p))
        .max_by_key(|(_, p)| p.components().count())
        .map(|(i, _)| i)
}

/// `{"<repo>": "<branch>"}`: a própria pasta (chave `.`) ou, se ela não é
/// repositório, as subpastas que são.
fn branches(pastas: &[PathBuf]) -> Value {
    let mut mapa = serde_json::Map::new();
    for pasta in pastas {
        if let Some(b) = branch_de(pasta) {
            mapa.insert(chave_do_repo(pastas, pasta, pasta), Value::String(b));
            continue;
        }
        let Ok(entradas) = fs::read_dir(pasta) else { continue };
        let mut subs: Vec<PathBuf> = entradas.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect();
        subs.sort();
        for sub in subs.into_iter().filter(|s| s.join(".git").exists()).take(SUBREPOS_MAX) {
            if let Some(b) = branch_de(&sub) {
                mapa.insert(chave_do_repo(pastas, pasta, &sub), Value::String(b));
            }
        }
    }
    Value::Object(mapa)
}

fn chave_do_repo(pastas: &[PathBuf], pasta: &Path, repo: &Path) -> String {
    let nome = |p: &Path| p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    match (repo == pasta, pastas.len() > 1) {
        (true, false) => ".".into(),
        (true, true) => nome(repo),
        (false, false) => nome(repo),
        (false, true) => format!("{}/{}", nome(pasta), nome(repo)),
    }
}

/// Lê o `HEAD` direto, sem rodar `git`: `ref: refs/heads/<branch>`, ou o sha
/// curto entre parênteses se estiver destacado. `.git` pode ser um arquivo
/// (`gitdir: …`) em worktree e submódulo.
fn branch_de(repo: &Path) -> Option<String> {
    let git = repo.join(".git");
    let pasta_git = if git.is_dir() {
        git
    } else {
        let apontado = fs::read_to_string(&git).ok()?;
        let destino = apontado.trim().strip_prefix("gitdir:")?.trim().to_string();
        let destino = PathBuf::from(destino);
        if destino.is_absolute() { destino } else { repo.join(destino) }
    };
    let head = fs::read_to_string(pasta_git.join("HEAD")).ok()?;
    let head = head.trim();
    match head.strip_prefix("ref: refs/heads/") {
        Some(branch) => Some(branch.to_string()),
        None if head.len() >= 7 => Some(format!("({})", &head[..7])),
        None => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pasta_de_teste(nome: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("neo-teste-trabalho-{}-{}", nome, std::process::id()));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn repo(pasta: &Path, head: &str) {
        fs::create_dir_all(pasta.join(".git")).unwrap();
        fs::write(pasta.join(".git").join("HEAD"), head).unwrap();
    }

    #[test]
    fn lock_vira_janela_sem_o_token() {
        let locks = pasta_de_teste("locks");
        let eu = std::process::id();
        fs::write(
            locks.join("111.lock"),
            json!({"pid": eu, "workspaceFolders": ["/Users/d/proj"], "ideName": "Visual Studio Code",
                   "transport": "ws", "authToken": "SEGREDO-DO-LOCK"})
                .to_string(),
        )
        .unwrap();
        fs::write(
            locks.join("222.lock"),
            json!({"pid": 999999, "workspaceFolders": ["/Users/d/velha"], "authToken": "x"}).to_string(),
        )
        .unwrap();
        fs::write(locks.join("333.lock"), "não é json").unwrap();
        let janelas = janelas_abertas(&locks);
        assert_eq!(janelas.len(), 1, "a do Code morto e a quebrada ficam de fora");
        assert_eq!(janelas[0].pastas, vec![PathBuf::from("/Users/d/proj")]);

        let home = pasta_de_teste("home-lock");
        let raiz = Raiz { sessoes: home.join("s"), projetos: home.join("p"), home: home.clone() };
        let r = retrato(&raiz, &locks, &json!({}), claude_sessoes::agora_ms());
        let texto = r.to_string();
        assert!(!texto.contains("SEGREDO"), "{texto}");
        assert_eq!(r["janelas"][0]["ide"], "Visual Studio Code");
        assert_eq!(r["janelas"][0]["focada"], Value::Null);
        assert_eq!(r["fonte"], "arquivos");
        let _ = fs::remove_dir_all(&locks);
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn branch_vem_do_head_sem_rodar_git() {
        let base = pasta_de_teste("branch");
        let raiz_sem_repo = base.join("neo");
        repo(&raiz_sem_repo.join("neo-core"), "ref: refs/heads/claude-code\n");
        repo(&raiz_sem_repo.join("neo-panel"), "0123456789abcdef0123456789abcdef01234567\n");
        let worktree = raiz_sem_repo.join("neo-wt");
        fs::create_dir_all(&worktree).unwrap();
        let gitdir = base.join("gitdir-do-wt");
        fs::create_dir_all(&gitdir).unwrap();
        fs::write(gitdir.join("HEAD"), "ref: refs/heads/feature/x\n").unwrap();
        fs::write(worktree.join(".git"), format!("gitdir: {}\n", gitdir.display())).unwrap();
        fs::create_dir_all(raiz_sem_repo.join("docs")).unwrap();

        let b = branches(&[raiz_sem_repo.clone()]);
        assert_eq!(b["neo-core"], "claude-code", "{b}");
        assert_eq!(b["neo-panel"], "(0123456)", "{b}");
        assert_eq!(b["neo-wt"], "feature/x", "{b}");
        assert!(b.get("docs").is_none(), "{b}");

        let sozinho = base.join("solo");
        repo(&sozinho, "ref: refs/heads/main\n");
        assert_eq!(branches(&[sozinho.clone()])["."], "main");
        let duas = branches(&[sozinho.clone(), raiz_sem_repo.clone()]);
        assert_eq!(duas["solo"], "main", "{duas}");
        assert_eq!(duas["neo/neo-core"], "claude-code", "{duas}");
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn sessao_cai_na_janela_mais_funda_que_contem_a_pasta() {
        let janelas = vec![
            Janela { pastas: vec![PathBuf::from("/Users/d/neo")], ide: None },
            Janela { pastas: vec![PathBuf::from("/Users/d/neo/neo-core")], ide: None },
            Janela { pastas: vec![PathBuf::from("/Users/d/outro")], ide: None },
        ];
        assert_eq!(janela_da_pasta(&janelas, Path::new("/Users/d/neo/neo-core/core")), Some(1));
        assert_eq!(janela_da_pasta(&janelas, Path::new("/Users/d/neo/docs")), Some(0));
        assert_eq!(janela_da_pasta(&janelas, Path::new("/Users/d/neo-x")), None, "prefixo de texto não conta");
        assert_eq!(janela_da_pasta(&janelas, Path::new("/tmp")), None);
    }

    /// No Mac de verdade: `cargo test -- --ignored retrato_real --nocapture`.
    #[test]
    #[ignore]
    fn retrato_real() {
        let inicio = std::time::Instant::now();
        let r = contexto(&json!({})).unwrap();
        println!("{}", serde_json::to_string_pretty(&r).unwrap());
        println!("em {:?}", inicio.elapsed());
        assert!(!r.to_string().contains("authToken"));
    }
}
