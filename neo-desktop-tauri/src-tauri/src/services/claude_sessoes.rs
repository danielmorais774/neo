//! As sessões do Claude Code do Daniel nesta máquina: quais estão abertas (no
//! VSCode ou num terminal), do que trata cada uma e quais fecharam há pouco. O
//! Neo usa isso para escolher em qual sessão entregar um pedido
//! (neo-core/docs/ambiente-de-trabalho-plan.md §4 e §9).
//!
//! Só lê arquivos que o próprio Claude Code mantém:
//! - `~/.claude/sessions/<pid>.json`: um por processo interativo vivo;
//! - `~/.claude/projects/<pasta codificada>/<sessão>.jsonl`: a transcrição.
//!
//! Os `.key` e o socket de mensagens que ficam ao lado do registro nunca são
//! lidos. É formato interno do Claude Code, sem documentação (medido na
//! 2.1.314–2.1.319): todo campo é opcional e o que não casar fica de fora, sem erro.

use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

/// Do começo da transcrição só interessa o 1º pedido; do fim, todo o resto. Uma
/// sessão longa passa de 18 MB, e o título se repete a cada turno. As duas
/// janelas começam pequenas e crescem só quando falta o que a ficha quer: ler
/// 4 MB de cada sessão viva custava ~1,3 s no build de debug do `tauri dev`.
const CABECA_INICIAL: u64 = 64 * 1024;
const CABECA_MAX: u64 = 1024 * 1024;
const CAUDA_INICIAL: u64 = 512 * 1024;

/// Uma sessão de 18 MB tinha 16 MB em resultados de ferramenta (08/11): só o
/// começo de cada linha é olhado antes de decidir se ela vale o parse. Medido nas
/// ~31 mil linhas do Mac: o 1º `"type":"` aparece até a posição 123 (`user`), o
/// `"role":"assistant"` até 290 (a linha do assistente começa por `message`; o
/// `"type":"assistant"` dela fica depois do conteúdo, a até 78 KB) e o
/// `"type":"text"` do assistente até 2.275.
const TIPO_ATE: usize = 512;
const CONTEUDO_ATE: usize = 4096;

/// Ferramentas do Claude que escrevem arquivo: o `file_path` delas diz em que a
/// sessão trabalhou. O anexo `edited_text_file` só conta o que mudou FORA dela.
const EDITORES: &[&str] = &["Edit", "Write", "MultiEdit", "NotebookEdit"];

/// Onde o Claude Code guarda as coisas. Nos testes, uma pasta de mentira.
pub struct Raiz {
    pub home: PathBuf,
    pub sessoes: PathBuf,
    pub projetos: PathBuf,
}

impl Raiz {
    pub fn do_usuario() -> Option<Self> {
        let home = dirs::home_dir()?;
        Some(Raiz::em(&home))
    }

    fn em(home: &Path) -> Self {
        Raiz {
            home: home.to_path_buf(),
            sessoes: home.join(".claude").join("sessions"),
            projetos: home.join(".claude").join("projects"),
        }
    }
}

/// Os cortes da ficha. Os de verdade vêm do core (`[trabalho]` do neo.toml);
/// estes só valem quando ele não manda.
struct Cortes {
    objetivo: usize,
    pedido: usize,
    pedidos: usize,
    resposta: usize,
    arquivos: usize,
}

impl Cortes {
    fn de(args: &Value) -> Self {
        let n = |chave: &str, padrao: usize| {
            args.get(chave).and_then(Value::as_u64).map(|v| v as usize).unwrap_or(padrao)
        };
        Cortes {
            objetivo: n("objetivo_max", 160),
            pedido: n("pedido_max", 120),
            pedidos: n("pedidos", 3),
            resposta: n("resposta_max", 200),
            arquivos: n("arquivos_max", 5),
        }
    }
}

/// `~/.claude/sessions/<pid>.json`.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Registro {
    pub pid: Option<i64>,
    pub session_id: Option<String>,
    pub cwd: Option<String>,
    pub entrypoint: Option<String>,
    pub status: Option<String>,
    pub status_updated_at: Option<i64>,
    pub proc_start: Option<String>,
    pub name: Option<String>,
}

// ── quem está vivo ───────────────────────────────────────────────────────

/// Início de cada processo, como o `ps` mostra em UTC (`Sun Nov  8 22:47:35 2026`),
/// que é o formato do `procStart` do registro. Uma chamada só para todos.
fn inicios_dos_processos() -> HashMap<i64, String> {
    let saida = Command::new("/bin/ps")
        .args(["-A", "-o", "pid=", "-o", "lstart="])
        .env("TZ", "UTC")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .output();
    let Ok(saida) = saida else { return HashMap::new() };
    String::from_utf8_lossy(&saida.stdout)
        .lines()
        .filter_map(|linha| {
            let linha = linha.trim();
            let (pid, inicio) = linha.split_once(char::is_whitespace)?;
            Some((pid.parse().ok()?, inicio.trim().to_string()))
        })
        .collect()
}

fn mesmo_inicio(a: &str, b: &str) -> bool {
    a.split_whitespace().eq(b.split_whitespace())
}

/// O processo do registro ainda é o que o escreveu? Depois de um `kill -9` o
/// json fica para trás até outra sessão varrer (F0), e o pid pode ter sido
/// reaproveitado: só vale com o `procStart` igual ao início real.
fn vivo(reg: &Registro, inicios: &HashMap<i64, String>) -> bool {
    let (Some(pid), Some(gravado)) = (reg.pid, reg.proc_start.as_deref()) else {
        return false;
    };
    pid > 0
        && unsafe { libc::kill(pid as i32, 0) } == 0
        && inicios.get(&pid).is_some_and(|real| mesmo_inicio(real, gravado))
}

fn registros_vivos(raiz: &Raiz) -> Vec<Registro> {
    let inicios = inicios_dos_processos();
    let Ok(entradas) = fs::read_dir(&raiz.sessoes) else { return Vec::new() };
    let mut vivos: Vec<Registro> = entradas
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .filter_map(|p| serde_json::from_str::<Registro>(&fs::read_to_string(p).ok()?).ok())
        .filter(|r| r.session_id.is_some() && vivo(r, &inicios))
        .collect();
    vivos.sort_by_key(|r| std::cmp::Reverse(r.status_updated_at.unwrap_or(0)));
    vivos
}

/// Algum processo interativo vivo está com esta sessão aberta (painel do VSCode
/// ou terminal)? É o que impede um `--resume -p` de dividir o histórico dela.
pub fn sessao_viva(sessao: &str) -> Option<Registro> {
    let raiz = Raiz::do_usuario()?;
    registros_vivos(&raiz).into_iter().find(|r| r.session_id.as_deref() == Some(sessao))
}

// ── a transcrição ────────────────────────────────────────────────────────

/// A pasta codificada do Claude Code: tudo que não é letra ou número vira `-`.
pub fn codificar(pasta: &str) -> String {
    pasta.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect()
}

/// `<projetos>/<pasta codificada>/<sessão>.jsonl`; se a regra mudar, procura a
/// sessão nos outros projetos.
pub fn arquivo_da_sessao(projetos: &Path, cwd: Option<&str>, sessao: &str) -> Option<PathBuf> {
    let arquivo = format!("{}.jsonl", sessao);
    if let Some(cwd) = cwd {
        let direto = projetos.join(codificar(cwd)).join(&arquivo);
        if direto.is_file() {
            return Some(direto);
        }
    }
    fs::read_dir(projetos)
        .ok()?
        .flatten()
        .map(|d| d.path().join(&arquivo))
        .find(|p| p.is_file())
}

#[derive(Default)]
struct Leitura {
    titulo_proprio: Option<String>,
    titulo: Option<String>,
    pasta: Option<String>,
    /// Onde a sessão nasceu: é a pasta do projeto dela em `~/.claude/projects`, e
    /// o `--resume` tem de rodar ali. A `pasta` é onde ela está agora (pode ter
    /// descido para um subprojeto).
    pasta_inicial: Option<String>,
    origem: Option<String>,
    objetivo: Option<String>,
    pedidos: Vec<String>,
    resposta: Option<String>,
    arquivos: Vec<String>,
    artefatos: Vec<String>,
}

impl Leitura {
    fn absorver(&mut self, linha: &[u8]) {
        // Resultado de ferramenta e raciocínio ou chamada de ferramenta do
        // assistente não entram na ficha. Cada bloco é uma linha, então dá para
        // pular sem o parse. No build de debug cada busca custa caro: uma só no
        // começo curto, e a segunda só nas linhas de conversa.
        let comeco = String::from_utf8_lossy(&linha[..linha.len().min(TIPO_ATE)]);
        let conteudo = || String::from_utf8_lossy(&linha[..linha.len().min(CONTEUDO_ATE)]);
        let interessa = match primeiro_tipo(&comeco) {
            Some("user") => !conteudo().contains("\"tool_use_id\""),
            Some("message") if comeco.contains("\"role\":\"assistant\"") => {
                let c = conteudo();
                c.contains("\"type\":\"text\"")
                    || EDITORES.iter().any(|e| c.contains(&format!("\"name\":\"{}\"", e)))
            }
            Some("ai-title" | "custom-title" | "frame-link" | "edited_text_file") => true,
            _ => false,
        };
        if !interessa {
            return;
        }
        let Ok(d) = serde_json::from_slice::<Value>(linha) else { return };
        let lateral = d.get("isSidechain").and_then(Value::as_bool) == Some(true);
        match d.get("type").and_then(Value::as_str) {
            Some("ai-title") => {
                if let Some(t) = texto(&d, "aiTitle") {
                    self.titulo = Some(t);
                }
            }
            // O nome que o Daniel deu à aba vence o do Claude. A chave não foi
            // medida (nenhuma aba renomeada no Mac em 08/11): aceita as duas.
            Some("custom-title") => {
                if let Some(t) = texto(&d, "customTitle").or_else(|| texto(&d, "title")) {
                    self.titulo_proprio = Some(t);
                }
            }
            Some("frame-link") => {
                if let Some(t) = texto(&d, "title") {
                    self.artefatos.push(t);
                }
            }
            Some("attachment") => {
                let anexo = d.get("attachment");
                if anexo.and_then(|a| a.get("type")).and_then(Value::as_str) == Some("edited_text_file") {
                    if let Some(nome) = anexo.and_then(|a| a.get("filename")).and_then(Value::as_str) {
                        self.arquivos.push(nome.to_string());
                    }
                }
            }
            Some("user") if !lateral => {
                if let Some(cwd) = texto(&d, "cwd") {
                    if self.pasta_inicial.is_none() {
                        self.pasta_inicial = Some(cwd.clone());
                    }
                    self.pasta = Some(cwd);
                }
                if self.origem.is_none() {
                    self.origem = texto(&d, "entrypoint");
                }
                if let Some(pedido) = pedido_do_usuario(&d) {
                    if self.objetivo.is_none() {
                        self.objetivo = Some(pedido.clone());
                    }
                    self.pedidos.push(pedido);
                }
            }
            Some("assistant") if !lateral => {
                if let Some(t) = texto_do_assistente(&d) {
                    self.resposta = Some(t);
                }
                self.arquivos.extend(arquivos_escritos(&d));
            }
            _ => {}
        }
    }
}

/// O valor do 1º `"type":"…"` do começo da linha: o tipo da linha, o do anexo
/// (que vem antes do da linha) ou `message` na linha do assistente.
fn primeiro_tipo(comeco: &str) -> Option<&str> {
    let i = comeco.find("\"type\":\"")? + "\"type\":\"".len();
    let resto = &comeco[i..];
    Some(&resto[..resto.find('"')?])
}

fn texto(d: &Value, chave: &str) -> Option<String> {
    d.get(chave).and_then(Value::as_str).map(str::trim).filter(|s| !s.is_empty()).map(String::from)
}

/// O que o Daniel escreveu, sem o que o Claude Code injeta junto: lembretes de
/// sistema, seleção do IDE, saída de comando (blocos que começam com `<`), aviso
/// de interrupção e resultado de ferramenta (que nem é bloco de texto).
fn pedido_do_usuario(d: &Value) -> Option<String> {
    if d.get("isMeta").and_then(Value::as_bool) == Some(true) {
        return None;
    }
    let conteudo = d.get("message")?.get("content")?;
    let blocos: Vec<&str> = match conteudo {
        Value::String(s) => vec![s.as_str()],
        Value::Array(itens) => itens
            .iter()
            .filter(|b| b.get("type").and_then(Value::as_str) == Some("text"))
            .filter_map(|b| b.get("text").and_then(Value::as_str))
            .collect(),
        _ => Vec::new(),
    };
    let partes: Vec<String> = blocos
        .into_iter()
        .map(str::trim)
        .filter_map(|b| {
            if let Some(m) = mensagem_de_outra_sessao(b) {
                return Some(m);
            }
            let ruido = b.is_empty() || b.starts_with('<') || b.starts_with("[Request interrupted");
            (!ruido).then(|| b.to_string())
        })
        .collect();
    let junto = partes.join(" ");
    (!junto.trim().is_empty()).then_some(junto)
}

/// "Another Claude session sent a message: <cross-session-message from-name="X" …>
/// texto</cross-session-message>…" vira "[de X] texto". É assim que chega o que
/// o Neo entrega pelo SendMessage (F0).
fn mensagem_de_outra_sessao(bloco: &str) -> Option<String> {
    let inicio = bloco.find("<cross-session-message")?;
    let resto = &bloco[inicio..];
    let abre = resto.find('>')?;
    let fecha = resto.find("</cross-session-message>")?;
    if fecha <= abre {
        return None;
    }
    let de = resto[..abre]
        .split("from-name=\"")
        .nth(1)
        .and_then(|s| s.split('"').next())
        .unwrap_or("outra sessão");
    Some(format!("[de {}] {}", de, resto[abre + 1..fecha].trim()))
}

fn texto_do_assistente(d: &Value) -> Option<String> {
    let itens = d.get("message")?.get("content")?.as_array()?;
    let partes: Vec<&str> = itens
        .iter()
        .filter(|b| b.get("type").and_then(Value::as_str) == Some("text"))
        .filter_map(|b| b.get("text").and_then(Value::as_str))
        .map(str::trim)
        .filter(|t| !t.is_empty())
        .collect();
    (!partes.is_empty()).then(|| partes.join(" "))
}

/// Os arquivos que as chamadas `Edit`/`Write` desta linha do assistente escreveram.
fn arquivos_escritos(d: &Value) -> Vec<String> {
    let Some(itens) = d.get("message").and_then(|m| m.get("content")).and_then(Value::as_array) else {
        return Vec::new();
    };
    itens
        .iter()
        .filter(|b| b.get("type").and_then(Value::as_str) == Some("tool_use"))
        .filter(|b| b.get("name").and_then(Value::as_str).is_some_and(|n| EDITORES.contains(&n)))
        .filter_map(|b| {
            let entrada = b.get("input")?;
            entrada.get("file_path").or_else(|| entrada.get("notebook_path"))?.as_str().map(String::from)
        })
        .collect()
}

/// Cada linha inteira de `[inicio, inicio + qtd)`. Um pedaço que começa ou
/// termina no meio de uma linha descarta a linha cortada.
fn cada_linha(arquivo: &Path, inicio: u64, qtd: u64, tamanho: u64, mut visitar: impl FnMut(&[u8])) {
    let Ok(mut f) = File::open(arquivo) else { return };
    if f.seek(SeekFrom::Start(inicio)).is_err() {
        return;
    }
    let mut bytes = Vec::new();
    if f.take(qtd).read_to_end(&mut bytes).is_err() {
        return;
    }
    let mut linhas: Vec<&[u8]> = bytes.split(|b| *b == b'\n').collect();
    if inicio > 0 && !linhas.is_empty() {
        linhas.remove(0);
    }
    if inicio + (bytes.len() as u64) < tamanho {
        linhas.pop();
    }
    for linha in linhas.into_iter().filter(|l| !l.is_empty()) {
        visitar(linha);
    }
}

fn absorver_trecho(leitura: &mut Leitura, arquivo: &Path, inicio: u64, qtd: u64, tamanho: u64) {
    cada_linha(arquivo, inicio, qtd, tamanho, |linha| leitura.absorver(linha));
}

fn ler_transcricao(arquivo: &Path, pedidos: usize) -> Leitura {
    ler_transcricao_com(arquivo, CABECA_INICIAL, CABECA_MAX, CAUDA_INICIAL, pedidos)
}

/// O começo e o fim da transcrição, cada um crescendo ×4 enquanto falta algo:
/// o começo até achar o 1º pedido (ou chegar em `cabeca_max`); o fim até ter os
/// `pedidos` que a ficha quer ou encostar no começo. Numa sessão de muita
/// captura de tela, 4 MB de fim guardavam um pedido só (08/11).
fn ler_transcricao_com(
    arquivo: &Path,
    cabeca_inicial: u64,
    cabeca_max: u64,
    cauda_inicial: u64,
    pedidos: usize,
) -> Leitura {
    let tamanho = arquivo.metadata().map(|m| m.len()).unwrap_or(0);
    if tamanho <= cabeca_inicial + cauda_inicial {
        let mut tudo = Leitura::default();
        absorver_trecho(&mut tudo, arquivo, 0, tamanho, tamanho);
        return tudo;
    }
    let mut fim_cabeca = cabeca_inicial;
    let mut cabeca = loop {
        let mut cabeca = Leitura::default();
        absorver_trecho(&mut cabeca, arquivo, 0, fim_cabeca, tamanho);
        if cabeca.objetivo.is_some() || fim_cabeca >= cabeca_max.min(tamanho) {
            break cabeca;
        }
        fim_cabeca = (fim_cabeca * 4).min(cabeca_max);
    };
    let fim_cabeca = fim_cabeca.min(tamanho);
    let mut janela = cauda_inicial;
    let (mut cauda, encostou) = loop {
        let inicio = tamanho.saturating_sub(janela).max(fim_cabeca);
        let mut cauda = Leitura::default();
        absorver_trecho(&mut cauda, arquivo, inicio, tamanho - inicio, tamanho);
        if cauda.pedidos.len() >= pedidos || inicio == fim_cabeca {
            break (cauda, inicio == fim_cabeca);
        }
        janela *= 4;
    };
    if encostou {
        // Começo e fim, juntos, são a transcrição inteira (menos a linha que a
        // emenda corta ao meio).
        for (de_cabeca, de_cauda) in [
            (&mut cabeca.pedidos, &mut cauda.pedidos),
            (&mut cabeca.arquivos, &mut cauda.arquivos),
            (&mut cabeca.artefatos, &mut cauda.artefatos),
        ] {
            de_cabeca.append(de_cauda);
            std::mem::swap(de_cabeca, de_cauda);
        }
    }
    // O objetivo é o 1º pedido da SESSÃO, não o 1º da cauda.
    cauda.objetivo = cabeca.objetivo;
    cauda.origem = cabeca.origem.or(cauda.origem);
    cauda.titulo = cauda.titulo.or(cabeca.titulo);
    cauda.titulo_proprio = cauda.titulo_proprio.or(cabeca.titulo_proprio);
    cauda.pasta = cauda.pasta.or(cabeca.pasta);
    cauda.pasta_inicial = cabeca.pasta_inicial.or(cauda.pasta_inicial);
    cauda.resposta = cauda.resposta.or(cabeca.resposta);
    cauda
}

// ── a ficha ──────────────────────────────────────────────────────────────

/// Uma sessão como o cérebro do Neo a vê. `pasta` fica completa aqui (o
/// `trabalho.contexto` liga sessão à janela por ela) e abreviada no JSON.
pub struct Ficha {
    pub session_id: String,
    pub nome: Option<String>,
    pub origem: Option<String>,
    pub status: String,
    pub viva: bool,
    pub ultimo_uso_min: i64,
    pub titulo: Option<String>,
    pub pasta: Option<String>,
    pub pasta_inicial: Option<String>,
    pub objetivo: Option<String>,
    pub ultimos_pedidos: Vec<String>,
    pub ultima_resposta: Option<String>,
    pub arquivos: Vec<String>,
    pub artefatos: Vec<String>,
    pub tarefa_do_neo: bool,
}

impl Ficha {
    pub fn vazia(&self) -> bool {
        self.titulo.is_none() && self.ultimos_pedidos.is_empty()
    }

    pub fn json(&self, home: &Path) -> Value {
        json!({
            "sessionId": self.session_id,
            "nome": self.nome,
            "origem": self.origem,
            "status": self.status,
            "viva": self.viva,
            "ultimo_uso_min": self.ultimo_uso_min,
            "titulo": self.titulo,
            "pasta": self.pasta.as_deref().map(|p| abreviar(p, home)),
            "objetivo": self.objetivo,
            "ultimos_pedidos": self.ultimos_pedidos,
            "ultima_resposta": self.ultima_resposta,
            "arquivos": self.arquivos,
            "artefatos": self.artefatos,
            "tarefa_do_neo": self.tarefa_do_neo,
            "vazia": self.vazia(),
        })
    }
}

fn corta(texto: &str, max: usize) -> String {
    let limpo = texto.split_whitespace().collect::<Vec<_>>().join(" ");
    if limpo.chars().count() <= max {
        return limpo;
    }
    let mut curto: String = limpo.chars().take(max.saturating_sub(1)).collect();
    curto.push('…');
    curto
}

/// `/Users/daniel/x` → `~/x`.
pub fn abreviar(caminho: &str, home: &Path) -> String {
    let home = home.display().to_string();
    match caminho.strip_prefix(&home) {
        Some(resto) if resto.is_empty() || resto.starts_with('/') => format!("~{}", resto),
        _ => caminho.to_string(),
    }
}

/// Rascunho (scratchpad em `/private/tmp`) e memória do próprio Claude
/// (`~/.claude/projects/…/memory`) não dizem nada sobre o trabalho.
fn arquivo_de_rascunho(caminho: &str) -> bool {
    caminho.starts_with("/private/tmp/")
        || caminho.starts_with("/tmp/")
        || caminho.contains("/scratchpad/")
        || caminho.contains("/.claude/")
}

fn ms_de(t: SystemTime) -> i64 {
    t.duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

pub fn agora_ms() -> i64 {
    ms_de(SystemTime::now())
}

fn montar_ficha(
    raiz: &Raiz,
    sessao: &str,
    reg: Option<&Registro>,
    arquivo: Option<&Path>,
    agora: i64,
    c: &Cortes,
) -> Ficha {
    let leitura = arquivo.map(|a| ler_transcricao(a, c.pedidos)).unwrap_or_default();
    let mexida = arquivo
        .and_then(|a| a.metadata().ok())
        .and_then(|m| m.modified().ok())
        .map(ms_de);
    let uso = reg.and_then(|r| r.status_updated_at).or(mexida).unwrap_or(agora);
    let pasta = leitura.pasta.clone().or_else(|| reg.and_then(|r| r.cwd.clone()));
    let tarefas_do_neo = raiz.home.join("Neo").join("claude");
    let pedidos = &leitura.pedidos;
    let mut arquivos: Vec<String> = Vec::new();
    for nome in leitura.arquivos.iter().rev() {
        if arquivo_de_rascunho(nome) {
            continue;
        }
        let curto = abreviar(nome, &raiz.home);
        if !arquivos.contains(&curto) {
            arquivos.push(curto);
        }
        if arquivos.len() >= c.arquivos {
            break;
        }
    }
    let mut artefatos: Vec<String> = Vec::new();
    for t in leitura.artefatos.iter().rev() {
        if !artefatos.contains(t) {
            artefatos.push(t.clone());
        }
    }
    artefatos.truncate(3);
    Ficha {
        session_id: sessao.to_string(),
        nome: reg.and_then(|r| r.name.clone()),
        origem: reg.and_then(|r| r.entrypoint.clone()).or(leitura.origem),
        status: reg.and_then(|r| r.status.clone()).unwrap_or_else(|| "fechada".into()),
        viva: reg.is_some(),
        ultimo_uso_min: ((agora - uso) / 60_000).max(0),
        titulo: leitura.titulo_proprio.or(leitura.titulo),
        tarefa_do_neo: pasta.as_deref().is_some_and(|p| Path::new(p).starts_with(&tarefas_do_neo)),
        pasta_inicial: leitura.pasta_inicial.clone().or_else(|| reg.and_then(|r| r.cwd.clone())),
        pasta,
        objetivo: leitura.objetivo.as_deref().map(|t| corta(t, c.objetivo)),
        ultimos_pedidos: pedidos[pedidos.len().saturating_sub(c.pedidos)..]
            .iter()
            .map(|p| corta(p, c.pedido))
            .collect(),
        ultima_resposta: leitura.resposta.as_deref().map(|t| corta(t, c.resposta)),
        arquivos,
        artefatos,
    }
}

/// As sessões abertas agora, da usada mais recentemente para a mais antiga.
pub fn fichas_vivas(raiz: &Raiz, args: &Value, agora: i64) -> Vec<Ficha> {
    let c = Cortes::de(args);
    let mut vistas = HashSet::new();
    registros_vivos(raiz)
        .iter()
        .filter(|r| vistas.insert(r.session_id.clone()))
        .map(|r| {
            let id = r.session_id.as_deref().unwrap_or_default();
            let arquivo = arquivo_da_sessao(&raiz.projetos, r.cwd.as_deref(), id);
            montar_ficha(raiz, id, Some(r), arquivo.as_deref(), agora, &c)
        })
        .collect()
}

/// A ficha de UMA sessão, viva ou fechada (o `claude.enviar` precisa do título
/// e, se estiver fechada, da pasta onde ela nasceu).
pub fn ficha_da_sessao(sessao: &str) -> Option<Ficha> {
    let raiz = Raiz::do_usuario()?;
    let reg = registros_vivos(&raiz).into_iter().find(|r| r.session_id.as_deref() == Some(sessao));
    let arquivo = arquivo_da_sessao(&raiz.projetos, reg.as_ref().and_then(|r| r.cwd.as_deref()), sessao);
    if reg.is_none() && arquivo.is_none() {
        return None;
    }
    let c = Cortes::de(&Value::Null);
    Some(montar_ficha(&raiz, sessao, reg.as_ref(), arquivo.as_deref(), agora_ms(), &c))
}

// ── a volta de um pedido ─────────────────────────────────────────────────

/// Corta sem desmanchar as linhas: a resposta é markdown para o Daniel ler.
fn corta_mantendo(texto: &str, max: usize) -> String {
    if texto.chars().count() <= max {
        return texto.to_string();
    }
    let mut curto: String = texto.chars().take(max.saturating_sub(1)).collect();
    curto.push('…');
    curto
}

fn ms_do_horario(d: &Value) -> Option<i64> {
    let horario = d.get("timestamp").and_then(Value::as_str)?;
    chrono::DateTime::parse_from_rfc3339(horario).ok().map(|t| t.timestamp_millis())
}

/// Os textos do assistente escritos a partir de `desde_ms`: `(o último, quantos)`.
/// A janela do fim cresce até ver uma resposta de ANTES (a janela cobre o
/// pedido) ou chegar ao começo do arquivo.
fn respostas_desde(arquivo: &Path, desde_ms: i64) -> (Option<String>, usize) {
    let tamanho = arquivo.metadata().map(|m| m.len()).unwrap_or(0);
    let mut janela = CAUDA_INICIAL;
    loop {
        let inicio = tamanho.saturating_sub(janela);
        let mut textos: Vec<String> = Vec::new();
        let mut viu_antes = false;
        cada_linha(arquivo, inicio, tamanho - inicio, tamanho, |linha| {
            let comeco = String::from_utf8_lossy(&linha[..linha.len().min(TIPO_ATE)]);
            let conteudo = String::from_utf8_lossy(&linha[..linha.len().min(CONTEUDO_ATE)]);
            let do_assistente = primeiro_tipo(&comeco) == Some("message")
                && comeco.contains("\"role\":\"assistant\"")
                && conteudo.contains("\"type\":\"text\"");
            if !do_assistente {
                return;
            }
            let Ok(d) = serde_json::from_slice::<Value>(linha) else { return };
            if d.get("isSidechain").and_then(Value::as_bool) == Some(true) {
                return;
            }
            match ms_do_horario(&d) {
                Some(ms) if ms < desde_ms => viu_antes = true,
                Some(_) => textos.extend(texto_do_assistente(&d)),
                None => {}
            }
        });
        if viu_antes || inicio == 0 {
            let n = textos.len();
            return (textos.pop(), n);
        }
        janela *= 4;
    }
}

/// `claude.situacao`: a sessão está viva, livre ou ocupada, desde quando, e o
/// que ela respondeu desde `desde_ms` (o horário da entrega, no relógio do Mac).
pub fn situacao(args: &Value) -> Result<Value, String> {
    let sessao = args.get("sessionId").and_then(Value::as_str).unwrap_or("").trim();
    if sessao.is_empty() || !sessao.chars().all(|c| c.is_ascii_hexdigit() || c == '-') {
        return Err(format!("sessão inválida: {:?}", sessao));
    }
    let desde = args.get("desde_ms").and_then(Value::as_i64).unwrap_or(0);
    let max = args.get("resposta_max").and_then(Value::as_u64).unwrap_or(6000) as usize;
    let raiz = Raiz::do_usuario().ok_or("sem home")?;
    let reg = registros_vivos(&raiz).into_iter().find(|r| r.session_id.as_deref() == Some(sessao));
    let arquivo = arquivo_da_sessao(&raiz.projetos, reg.as_ref().and_then(|r| r.cwd.as_deref()), sessao);
    let (resposta, n) = arquivo.as_deref().map(|a| respostas_desde(a, desde)).unwrap_or((None, 0));
    Ok(json!({
        "viva": reg.is_some(),
        "status": reg.as_ref().and_then(|r| r.status.clone()).unwrap_or_else(|| "fechada".into()),
        "status_desde_ms": reg.as_ref().and_then(|r| r.status_updated_at),
        "resposta": resposta.map(|r| corta_mantendo(&r, max)),
        "respostas": n,
    }))
}

/// `claude.sessoes`: as vivas e, até `max`, as fechadas cuja transcrição mexeu
/// nas últimas `recentes_horas`.
pub fn listar(args: &Value) -> Result<Value, String> {
    let raiz = Raiz::do_usuario().ok_or("sem home")?;
    Ok(listar_em(&raiz, args, agora_ms()))
}

fn listar_em(raiz: &Raiz, args: &Value, agora: i64) -> Value {
    let c = Cortes::de(args);
    let horas = args.get("recentes_horas").and_then(Value::as_f64).unwrap_or(24.0);
    let max = args.get("max").and_then(Value::as_u64).unwrap_or(20) as usize;
    let vivas = fichas_vivas(raiz, args, agora);
    let abertas: HashSet<String> = vivas.iter().map(|f| f.session_id.clone()).collect();

    let limite = agora - (horas * 3_600_000.0) as i64;
    let mut fechadas: Vec<(i64, String, PathBuf)> = Vec::new();
    for projeto in fs::read_dir(&raiz.projetos).into_iter().flatten().flatten() {
        for entrada in fs::read_dir(projeto.path()).into_iter().flatten().flatten() {
            let caminho = entrada.path();
            if caminho.extension().and_then(|e| e.to_str()) != Some("jsonl") {
                continue;
            }
            let Some(id) = caminho.file_stem().and_then(|s| s.to_str()).map(String::from) else {
                continue;
            };
            let mexida = entrada.metadata().ok().and_then(|m| m.modified().ok()).map(ms_de).unwrap_or(0);
            if mexida >= limite && mexida <= agora && !abertas.contains(&id) {
                fechadas.push((mexida, id, caminho));
            }
        }
    }
    fechadas.sort_by_key(|(mexida, ..)| std::cmp::Reverse(*mexida));

    let mut fichas: Vec<Value> = vivas.iter().map(|f| f.json(&raiz.home)).collect();
    for (_, id, caminho) in fechadas {
        if fichas.len() >= max {
            break;
        }
        let ficha = montar_ficha(raiz, &id, None, Some(&caminho), agora, &c);
        if !ficha.vazia() {
            fichas.push(ficha.json(&raiz.home));
        }
    }
    json!({ "sessoes": fichas })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn pasta_de_teste(nome: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!("neo-teste-sessoes-{}-{}", nome, std::process::id()));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        p
    }

    fn linha(v: Value) -> String {
        format!("{}\n", v)
    }

    /// Linhas de conversa na ordem de chaves do Claude Code, que o `json!`
    /// (alfabético) não reproduz: na do usuário o `type` vem antes de `message`;
    /// na do assistente, `message` vem primeiro, com `"type":"message"` e o
    /// `role` no começo, e o `"type":"assistant"` fica no fim.
    fn linha_do_usuario(conteudo: Value) -> String {
        format!(
            "{{\"parentUuid\":null,\"isSidechain\":false,\"cwd\":\"/Users/d/proj/sub\",\
             \"entrypoint\":\"claude-vscode\",\"type\":\"user\",\"message\":{{\"role\":\"user\",\"content\":{}}}}}\n",
            conteudo
        )
    }

    fn linha_do_assistente(conteudo: Value) -> String {
        format!(
            "{{\"parentUuid\":\"p\",\"isSidechain\":false,\"message\":{{\"model\":\"m\",\"id\":\"i\",\
             \"type\":\"message\",\"role\":\"assistant\",\"content\":{}}},\"type\":\"assistant\"}}\n",
            conteudo
        )
    }

    fn pedido(texto: &str) -> String {
        linha_do_usuario(json!([{"type": "text", "text": texto}]))
    }

    fn resposta(texto: &str) -> String {
        linha_do_assistente(json!([{"type": "thinking", "thinking": ""}, {"type": "text", "text": texto}]))
    }

    fn transcricao_de_exemplo() -> String {
        [
            linha(json!({"type": "user", "isMeta": true, "message": {"content": "<local-command-caveat>x"}})),
            pedido("  faz o plano   do neo  "),
            linha_do_usuario(json!([
                {"type": "text", "text": "<system-reminder>lembrete</system-reminder>"},
                {"type": "text", "text": "<ide_selection>linha 3</ide_selection>"}])),
            linha_do_usuario(json!([{"type": "tool_result", "tool_use_id": "t1",
                "content": [{"type": "text", "text": "saída que parece pedido"}]}])),
            linha_do_assistente(json!([{"type": "tool_use", "name": "Bash",
                "input": {"command": "echo \"type\":\"text\""}}])),
            linha(json!({"type": "ai-title", "aiTitle": "Título velho"})),
            linha(json!({"type": "attachment", "attachment": {"type": "edited_text_file",
                "filename": "/private/tmp/claude-501/x/scratchpad/rascunho.py"}})),
            linha(json!({"type": "attachment", "attachment": {"type": "edited_text_file",
                "filename": "/Users/d/proj/a.rs"}})),
            linha(json!({"type": "attachment", "attachment": {"type": "edited_text_file",
                "filename": "/Users/d/proj/b.rs"}})),
            linha_do_assistente(json!([{"type": "tool_use", "id": "t2", "name": "Edit",
                "input": {"file_path": "/Users/d/proj/c.rs", "old_string": "a", "new_string": "b"}}])),
            linha_do_assistente(json!([{"type": "tool_use", "id": "t3", "name": "Write",
                "input": {"file_path": "/private/tmp/claude-501/x/scratchpad/nota.md", "content": "x"}},
                {"type": "tool_use", "id": "t4", "name": "Write",
                "input": {"file_path": "/Users/d/.claude/projects/-Users-d-proj/memory/fato.md", "content": "x"}}])),
            linha(json!({"type": "api-request-blob", "message": {"content": "\"type\":\"user\" enorme"}})),
            linha(json!({"type": "user", "isSidechain": true, "message": {"content": "do subagente"}})),
            resposta("resposta do meio"),
            pedido("Another Claude session sent a message: <cross-session-message from=\"uds:/tmp/x.sock\" \
                    from-name=\"relay-7\" from-mode=\"prompting\">\nroda os testes\n</cross-session-message>\n\n\
                    This came from another Claude session"),
            pedido("[Request interrupted by user]"),
            pedido("e o deploy?"),
            linha(json!({"type": "ai-title", "aiTitle": "Título novo"})),
            linha(json!({"type": "frame-link", "title": "Painel X"})),
            resposta("Pronto: o deploy subiu."),
        ]
        .concat()
    }

    #[test]
    fn ficha_tira_titulo_pedidos_e_resposta_sem_o_ruido() {
        let home = pasta_de_teste("ficha");
        let arquivo = home.join("s.jsonl");
        fs::write(&arquivo, transcricao_de_exemplo()).unwrap();
        let raiz = Raiz::em(Path::new("/Users/d"));
        let c = Cortes::de(&json!({ "pedidos": 3 }));
        let f = montar_ficha(&raiz, "s", None, Some(&arquivo), agora_ms(), &c);

        assert_eq!(f.titulo.as_deref(), Some("Título novo"));
        assert_eq!(f.objetivo.as_deref(), Some("faz o plano do neo"));
        assert_eq!(f.ultimos_pedidos, vec!["faz o plano do neo", "[de relay-7] roda os testes", "e o deploy?"]);
        assert_eq!(f.ultima_resposta.as_deref(), Some("Pronto: o deploy subiu."));
        assert_eq!(
            f.arquivos,
            vec!["~/proj/c.rs", "~/proj/b.rs", "~/proj/a.rs"],
            "o Edit da sessão e os mexidos fora dela, sem o scratchpad, o mais novo primeiro"
        );
        assert_eq!(f.artefatos, vec!["Painel X"]);
        assert_eq!(f.origem.as_deref(), Some("claude-vscode"));
        assert_eq!(f.pasta.as_deref(), Some("/Users/d/proj/sub"));
        assert_eq!(f.status, "fechada");
        assert!(!f.viva && !f.vazia() && !f.tarefa_do_neo);
        assert_eq!(f.json(&raiz.home)["pasta"], "~/proj/sub");
        let _ = fs::remove_dir_all(&home);
    }

    /// Na ordem de chaves do Claude Code (`type` antes de `message`), que o
    /// `json!` não reproduz: o pedido longo entra inteiro, a saída de ferramenta
    /// de 200 KB nem é parseada.
    #[test]
    fn so_o_comeco_da_linha_decide_o_parse() {
        let home = pasta_de_teste("prefixo");
        let arquivo = home.join("s.jsonl");
        let longo = "palavra ".repeat(2000);
        let imagem = "A".repeat(200_000);
        let conteudo = format!(
            "{{\"parentUuid\":null,\"isSidechain\":false,\"cwd\":\"/Users/d/p\",\"type\":\"user\",\"message\":{{\"role\":\"user\",\"content\":[{{\"type\":\"text\",\"text\":\"{longo}fim do pedido\"}}]}}}}\n\
             {{\"parentUuid\":\"a\",\"isSidechain\":false,\"type\":\"user\",\"message\":{{\"role\":\"user\",\"content\":[{{\"tool_use_id\":\"t\",\"type\":\"tool_result\",\"content\":[{{\"type\":\"text\",\"text\":\"{imagem}\"}}]}}]}}}}\n"
        );
        fs::write(&arquivo, conteudo).unwrap();
        let l = ler_transcricao(&arquivo, 3);
        assert_eq!(l.pedidos.len(), 1);
        assert!(l.pedidos[0].ends_with("fim do pedido"));
        assert_eq!(l.pasta.as_deref(), Some("/Users/d/p"));
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn cortes_do_core_valem() {
        let home = pasta_de_teste("cortes");
        let arquivo = home.join("s.jsonl");
        fs::write(&arquivo, transcricao_de_exemplo()).unwrap();
        let raiz = Raiz::em(Path::new("/Users/d"));
        let c = Cortes::de(&json!({ "pedidos": 1, "pedido_max": 6, "resposta_max": 8, "arquivos_max": 1 }));
        let f = montar_ficha(&raiz, "s", None, Some(&arquivo), agora_ms(), &c);
        assert_eq!(f.ultimos_pedidos, vec!["e o d…"]);
        assert_eq!(f.ultima_resposta.as_deref(), Some("Pronto:…"));
        assert_eq!(f.arquivos.len(), 1);
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn transcricao_grande_le_o_comeco_e_o_fim() {
        let home = pasta_de_teste("grande");
        let arquivo = home.join("s.jsonl");
        let mut f = File::create(&arquivo).unwrap();
        f.write_all(pedido("o primeiro pedido").as_bytes()).unwrap();
        f.write_all(linha(json!({"type": "ai-title", "aiTitle": "Só no começo"})).as_bytes()).unwrap();
        for i in 0..200 {
            f.write_all(pedido(&format!("pedido do meio {i}")).as_bytes()).unwrap();
        }
        f.write_all(pedido("o último pedido").as_bytes()).unwrap();
        f.write_all(resposta("fim").as_bytes()).unwrap();
        drop(f);
        let l = ler_transcricao_com(&arquivo, 400, 1600, 600, 1);
        assert_eq!(l.objetivo.as_deref(), Some("o primeiro pedido"));
        assert_eq!(l.pedidos.last().map(String::as_str), Some("o último pedido"));
        assert!(l.pedidos.len() < 20, "não leu o meio: {}", l.pedidos.len());
        assert_eq!(l.titulo.as_deref(), Some("Só no começo"), "o título que só a cabeça tinha");
        assert_eq!(l.resposta.as_deref(), Some("fim"));

        // Querendo mais pedidos do que a cauda tem, a janela cresce.
        let l = ler_transcricao_com(&arquivo, 400, 1600, 600, 30);
        assert!(l.pedidos.len() >= 30, "{}", l.pedidos.len());
        assert_eq!(l.pedidos.last().map(String::as_str), Some("o último pedido"));
        // Até encostar no começo: aí são todos, sem repetir nenhum.
        let l = ler_transcricao_com(&arquivo, 400, 1600, 600, 10_000);
        assert_eq!(l.pedidos.first().map(String::as_str), Some("o primeiro pedido"));
        let unicos: HashSet<&String> = l.pedidos.iter().collect();
        assert_eq!(unicos.len(), l.pedidos.len(), "repetiu pedido na emenda");
        assert!(l.pedidos.len() >= 200, "{}", l.pedidos.len());
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn volta_so_o_que_a_sessao_respondeu_depois_da_entrega() {
        let home = pasta_de_teste("respostas");
        let arquivo = home.join("s.jsonl");
        let em = |texto: &str, horario: &str| {
            format!(
                "{{\"parentUuid\":\"p\",\"isSidechain\":false,\"message\":{{\"type\":\"message\",\"role\":\"assistant\",\
                 \"content\":[{{\"type\":\"text\",\"text\":{}}}]}},\"type\":\"assistant\",\"timestamp\":\"{}\"}}\n",
                json!(texto),
                horario
            )
        };
        let conteudo = [
            em("resposta velha", "2026-11-08T22:00:00.000Z"),
            pedido("roda os testes"),
            em("Rodando os testes.", "2026-11-08T22:10:05.000Z"),
            em("Os 12 testes passaram.\n\n- a\n- b", "2026-11-08T22:10:40.000Z"),
        ]
        .concat();
        fs::write(&arquivo, conteudo).unwrap();
        let entrega = chrono::DateTime::parse_from_rfc3339("2026-11-08T22:10:00.000Z").unwrap().timestamp_millis();
        let (ultima, n) = respostas_desde(&arquivo, entrega);
        assert_eq!(n, 2, "a de antes da entrega não conta");
        assert_eq!(ultima.as_deref(), Some("Os 12 testes passaram.\n\n- a\n- b"), "as linhas ficam");
        let (nada, zero) = respostas_desde(&arquivo, entrega + 3_600_000);
        assert_eq!((nada, zero), (None, 0));
        assert_eq!(corta_mantendo("linha 1\nlinha 2", 9), "linha 1\n…");
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn comeco_cresce_ate_achar_o_primeiro_pedido() {
        let home = pasta_de_teste("comeco");
        let arquivo = home.join("s.jsonl");
        let mut f = File::create(&arquivo).unwrap();
        for _ in 0..10 {
            f.write_all(linha(json!({"type": "attachment", "attachment": {"type": "deferred_tools_record",
                "lista": "x".repeat(200)}})).as_bytes()).unwrap();
        }
        f.write_all(pedido("o pedido que abre a sessão").as_bytes()).unwrap();
        for i in 0..50 {
            f.write_all(pedido(&format!("depois {i}")).as_bytes()).unwrap();
        }
        drop(f);
        let l = ler_transcricao_com(&arquivo, 300, 20_000, 600, 1);
        assert_eq!(l.objetivo.as_deref(), Some("o pedido que abre a sessão"));
        assert_eq!(l.pedidos.last().map(String::as_str), Some("depois 49"));
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn registro_so_vale_com_o_mesmo_inicio() {
        let inicios = inicios_dos_processos();
        let eu = std::process::id() as i64;
        let meu_inicio = inicios.get(&eu).cloned().expect("o ps mostra este processo");
        let reg = |pid: i64, inicio: &str| Registro {
            pid: Some(pid),
            session_id: Some("s".into()),
            cwd: None,
            entrypoint: None,
            status: None,
            status_updated_at: None,
            proc_start: Some(inicio.into()),
            name: None,
        };
        assert!(vivo(&reg(eu, &format!("  {}  ", meu_inicio)), &inicios));
        assert!(!vivo(&reg(eu, "Sun Jan  1 00:00:00 2000"), &inicios), "pid reaproveitado");
        let mut filho = Command::new("/usr/bin/true").spawn().unwrap();
        let morto = filho.id() as i64;
        filho.wait().unwrap();
        assert!(!vivo(&reg(morto, &meu_inicio), &inicios_dos_processos()), "kill -9 deixa o json");
    }

    #[test]
    fn acha_a_transcricao_pela_pasta_ou_procurando() {
        let projetos = pasta_de_teste("projetos");
        assert_eq!(codificar("/Users/d/my_proj.x"), "-Users-d-my-proj-x");
        let certa = projetos.join("-Users-d-proj");
        fs::create_dir_all(&certa).unwrap();
        fs::write(certa.join("a.jsonl"), "").unwrap();
        let outra = projetos.join("-outro-lugar");
        fs::create_dir_all(&outra).unwrap();
        fs::write(outra.join("b.jsonl"), "").unwrap();
        assert_eq!(arquivo_da_sessao(&projetos, Some("/Users/d/proj"), "a"), Some(certa.join("a.jsonl")));
        assert_eq!(arquivo_da_sessao(&projetos, Some("/Users/d/proj"), "b"), Some(outra.join("b.jsonl")));
        assert_eq!(arquivo_da_sessao(&projetos, None, "c"), None);
        let _ = fs::remove_dir_all(&projetos);
    }

    #[test]
    fn viva_vem_do_registro_e_fechada_so_se_recente() {
        let home = pasta_de_teste("listar");
        let raiz = Raiz::em(&home);
        fs::create_dir_all(&raiz.sessoes).unwrap();
        let projeto = raiz.projetos.join("-Users-d-proj");
        fs::create_dir_all(&projeto).unwrap();
        fs::write(projeto.join("viva.jsonl"), transcricao_de_exemplo()).unwrap();
        fs::write(projeto.join("fechada.jsonl"), pedido("um pedido antigo")).unwrap();
        fs::write(projeto.join("aba-vazia.jsonl"), "").unwrap();
        let eu = std::process::id() as i64;
        let inicio = inicios_dos_processos().get(&eu).cloned().unwrap();
        fs::write(
            raiz.sessoes.join(format!("{eu}.json")),
            json!({"pid": eu, "sessionId": "viva", "cwd": "/Users/d/proj", "entrypoint": "cli",
                   "status": "busy", "statusUpdatedAt": agora_ms(), "procStart": inicio, "name": "proj-1"})
                .to_string(),
        )
        .unwrap();
        fs::write(raiz.sessoes.join(format!("{eu}.abc.key")), "segredo").unwrap();
        fs::write(
            raiz.sessoes.join("999999.json"),
            json!({"pid": 999999, "sessionId": "morta", "procStart": inicio}).to_string(),
        )
        .unwrap();

        let agora = agora_ms();
        let r = listar_em(&raiz, &json!({}), agora);
        let ids: Vec<&str> = r["sessoes"].as_array().unwrap().iter().map(|s| s["sessionId"].as_str().unwrap()).collect();
        assert_eq!(ids, vec!["viva", "fechada"], "{r}");
        let viva = &r["sessoes"][0];
        assert_eq!(viva["viva"], true);
        assert_eq!(viva["status"], "busy");
        assert_eq!(viva["nome"], "proj-1");
        assert_eq!(viva["origem"], "cli");
        assert_eq!(r["sessoes"][1]["status"], "fechada");

        let dois_dias = agora + 48 * 3_600_000;
        let r = listar_em(&raiz, &json!({ "recentes_horas": 24 }), dois_dias);
        assert_eq!(r["sessoes"].as_array().unwrap().len(), 1, "a fechada saiu da janela: {r}");
        let _ = fs::remove_dir_all(&home);
    }

    /// No Mac de verdade: as vivas têm de bater com o `ListAgents` de uma sessão
    /// do Claude. `cargo test -- --ignored sessoes_reais --nocapture`.
    #[test]
    #[ignore]
    fn sessoes_reais() {
        let inicio = std::time::Instant::now();
        let r = listar(&json!({ "recentes_horas": 24 })).unwrap();
        println!("{}", serde_json::to_string_pretty(&r).unwrap());
        println!("em {:?}", inicio.elapsed());
        for s in r["sessoes"].as_array().unwrap().iter().filter(|s| s["viva"] == true) {
            assert!(s["nome"].is_string(), "{s}");
            // a trava do `claude.start` acha a mesma sessão
            let id = s["sessionId"].as_str().unwrap();
            assert!(sessao_viva(id).is_some(), "{id}");
        }
    }
}
