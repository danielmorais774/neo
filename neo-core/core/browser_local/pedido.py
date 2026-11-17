"""O pedido do Daniel contra a tarefa que o cérebro escreveu para o navegador.

Três decisões que ficam no CÓDIGO, porque o modelo erra de um turno para o
outro (ver docs/browser-local-plan.md §"Uma tarefa só"):

* ``tarefa_encolhida``: ele mandou ALTERAR (atualizar, preencher, corrigir…) e o
  cérebro mandou o navegador só LER ("NÃO altere nada", "apenas mapeie"). Sessão
  be8d195e: "atualize meu perfil GeekHunter pelo currículo" virou "mapeie e
  reporte", com a promessa de voltar com uma lista para ele aprovar — e ninguém
  voltou. Medido nos 219 jobs ``browser_local`` de produção (16/11): 20 tarefas
  com restrição de só-leitura, a regra dispara em 3, as 3 com esse mesmo padrão
  (GeekHunter, e duas vezes "preenche esse formulário" virando "LER sem
  preencher"), e nenhum falso positivo.
* ``caminhos_citados``: os arquivos do disco DELE que ele citou. Só eles podem
  ser enviados a um site pela ação ``upload`` — a página não escolhe arquivo.
* ``cortar_falas``: as palavras dele cabem no prompt do agente mesmo quando a
  mensagem é longa. Antes a fala que passava do teto era DESCARTADA inteira (a
  de be8d195e tinha 1732 caracteres e o agente não viu nada dela).
"""

from __future__ import annotations

import re
import unicodedata

#: Restrição de só-leitura escrita na TAREFA.
_SO_LEITURA = re.compile(
    r"\b(?:n[aã]o|nunca|sem)\s+(?:altere|alterar|mude|mudar|salve|salvar|edite|editar"
    r"|modifique|modificar|mexa|mexer|preencha|preencher)\s+(?:em\s+)?(?:nada|nenhum\w*|ainda)\b"
    r"|\b(?:apenas|somente|s[oó])\s+(?:mapeie|mapear|leia|ler|liste|listar|reporte|reportar"
    r"|olhe|olhar|veja|ver|consulte|consultar|levante|levantar)\b"
    r"|\bsem\s+preencher\s+nada\b",
    re.IGNORECASE,
)

#: Ordem de ALTERAR no pedido dele, em forma verbal (não "alternativas", "completo").
_MANDA_ALTERAR = re.compile(
    r"\b(?:atualiz(?:e|a|ar|em)|preench(?:a|e|er|am)|corrij(?:a|am)|corrig(?:e|ir)"
    r"|alter(?:e|a|ar)|edit(?:e|a|ar)|cadastr(?:e|a|ar)|inscrev(?:a|e|er)"
    r"|candidat(?:e|ar)|troqu(?:e|em)|troc(?:a|ar)|mud(?:e|a|ar))\b",
    re.IGNORECASE,
)
#: "não altere X" não é ordem de alterar.
_NEGADO_ANTES = re.compile(r"(?:n[aã]o|nunca|sem)\s+(?:\w+\s+)?$", re.IGNORECASE)

#: Ele pediu para ver ANTES — aí ler primeiro é o que ele mandou.
_QUER_VER_ANTES = re.compile(
    r"\bme\s+mostr\w*\s+(?:antes|primeiro)"
    r"|\bantes\s+de\s+(?:alterar|salvar|mudar|mexer|enviar|atualizar|preencher)\b[^.\n]{0,40}"
    r"\bme\s+(?:mostr|mand|diz|fal)"
    r"|\b(?:quero|vou)\s+aprovar\b|\beu\s+aprovo\b"
    r"|\b(?:s[oó]|apenas|somente)\s+(?:olh|v[eê]|mape|list|ler|leia|consult)",
    re.IGNORECASE,
)


def _ordem_de_alterar(texto: str) -> str | None:
    for m in _MANDA_ALTERAR.finditer(texto):
        if not _NEGADO_ANTES.search(texto[max(0, m.start() - 20) : m.start()]):
            return m.group(0)
    return None


def tarefa_encolhida(task: str, falas: str) -> str | None:
    """O verbo de alterar que ele usou, quando a tarefa manda só ler. ``None`` = ok."""
    task = str(task or "")
    falas = str(falas or "")
    if not task or not falas or not _SO_LEITURA.search(task):
        return None
    if _QUER_VER_ANTES.search(falas):
        return None
    return _ordem_de_alterar(falas)


def recusa_tarefa_encolhida(verbo: str) -> str:
    return (
        f"Recusado: o Daniel mandou «{verbo}», e a tarefa manda o navegador só LER (não "
        "alterar). Ele não pediu para aprovar uma lista antes — só para perguntar se "
        "faltar algo. Mande a tarefa INTEIRA numa chamada só: o agente lê a página, "
        "compara com o material, altera e salva. Dúvida, ele pergunta AO VIVO na aba "
        "(o Daniel responde no painel) e todo Salvar/Enviar com campo escrito pelo Neo "
        "para numa conferência antes. Material de referência (ex.: o texto do "
        "currículo) vai no `context`; arquivo do disco dele que o site precisa receber "
        "(foto, PDF) vai na tarefa pelo caminho que ele citou — o agente envia com a "
        "ação `upload`, sem abrir a janela de arquivos."
    )


# ── caminhos de arquivo citados ────────────────────────────────────────────

#: Entre crases ou aspas: aceita espaço no nome.
_CAMINHO_CITADO = re.compile(r"[`\"“'‘]((?:~|/)[^`\"”'’\n]{1,400})[`\"”'’]")
#: Solto no texto: até a extensão (o nome pode ter espaço).
_CAMINHO_SOLTO = re.compile(
    r"(?<![\w/])((?:~/|/Users/|/Volumes/|/home/|/tmp/)[^\n`\"'<>|]{0,300}?\.[A-Za-z0-9]{2,5})(?!\w)(?!\.\w)"
)


def _normalizar(caminho: str) -> str:
    return unicodedata.normalize("NFC", caminho.strip().rstrip(".,;:)"))


def caminhos_citados(texto: str, *, maximo: int = 10) -> list[str]:
    """Caminhos de arquivo (absolutos ou com ``~``) que aparecem no texto DELE."""
    texto = str(texto or "")
    achados: list[str] = []
    for m in list(_CAMINHO_CITADO.finditer(texto)) + list(_CAMINHO_SOLTO.finditer(texto)):
        c = _normalizar(m.group(1))
        if len(c) < 3 or c.endswith("/") or "://" in c:
            continue
        if c not in achados:
            achados.append(c)
        if len(achados) >= maximo:
            break
    return achados


def caminho_autorizado(pedido: str, autorizados: list[str] | None) -> str | None:
    """O caminho da lista que o ``pedido`` cita (igual, ou pelo nome do arquivo)."""
    alvo = _normalizar(str(pedido or ""))
    if not alvo:
        return None
    lista = [_normalizar(a) for a in (autorizados or []) if str(a or "").strip()]
    if alvo in lista:
        return alvo
    # O agente às vezes cita só o nome do arquivo: vale se for UM só na lista.
    nome = alvo.rsplit("/", 1)[-1].casefold()
    mesmos = [a for a in lista if a.rsplit("/", 1)[-1].casefold() == nome]
    return mesmos[0] if len(mesmos) == 1 else None


# ── as falas no prompt ─────────────────────────────────────────────────────

_RETICENCIA = "\n[…]\n"


def cortar_meio(texto: str, maximo: int) -> str:
    """Começo e fim do texto em até ``maximo`` caracteres (o meio sai)."""
    texto = str(texto or "")
    if len(texto) <= maximo:
        return texto
    if maximo <= len(_RETICENCIA) + 20:
        return texto[:maximo]
    resto = maximo - len(_RETICENCIA)
    cabeca = resto * 3 // 4
    return texto[:cabeca] + _RETICENCIA + texto[len(texto) - (resto - cabeca):]


def cortar_falas(falas: list[str], maximo: int) -> str:
    """As falas em lista ``- …``, as mais recentes primeiro a caber.

    A mais recente nunca some: se sozinha passa do teto, perde o MEIO.
    """
    linhas = [f"- {f}" for f in falas if str(f or "").strip()]
    while len(linhas) > 1 and len("\n".join(linhas)) > maximo:
        linhas.pop(0)
    if linhas and len("\n".join(linhas)) > maximo:
        linhas = [cortar_meio(linhas[-1], maximo)]
    return "\n".join(linhas)


# ── arquivos do catálogo do Neo ────────────────────────────────────────────
#
# Sessão d7ad32a0: o Neo recortou o retrato no sandbox (catálogo
# 245d3f7d…) e ele pediu "pega essa imagem e coloca no meu GeekHunter". O
# recorte não existe no disco dele; o cérebro inventou
# `/Users/daniel/Downloads/retrato_quadrado.png`, o `upload` recusou (não era
# caminho que ele citou) e a foto que subiu foi a original, vertical. Os
# arquivos DESTA conversa no catálogo (o Neo gerou, ou ele anexou) passam a
# poder ir para o site: o core manda o conteúdo e o desktop o põe no campo.

_FILE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def arquivos_do_catalogo(views: list, *, maximo: int = 8) -> list[dict]:
    """Os documentos desta conversa que têm versão no catálogo, o mais recente antes.

    ``views`` são ``DocView`` do registro de arquivos (``core/files/ledger.py``).
    Fica fora o de outra conversa, o que só foi ADOTADO por esta (o nome bateu
    com uma palavra da mensagem: na 98c8bac7 "imagem" e "foto" puseram na lista a
    foto de outra pessoa e uma planilha) e o que tem versão nova só no sandbox (o
    catálogo estaria atrás: subiria o arquivo velho).
    """
    achados: list[dict] = []
    vistos: set[str] = set()
    # ``build_views`` já devolve do movimento mais recente para o mais antigo.
    for v in views or []:
        if not getattr(v, "in_session", False) or not getattr(v, "current_file_id", None):
            continue
        if not getattr(v, "worked_in_session", True):
            continue
        if getattr(v, "unpublished", False):
            continue
        fid = str(v.current_file_id)
        if fid in vistos:
            continue
        vistos.add(fid)
        nomes = {str(v.name or "")} | {str(a or "") for a in (getattr(v, "aliases", None) or set())}
        achados.append({
            "file_id": fid,
            "nome": str(v.name or "arquivo"),
            "nomes": sorted(n.rsplit("/", 1)[-1] for n in nomes if n),
            "bytes": int(getattr(v, "current_size", None) or 0),
            "origem": str(getattr(v, "origin", "") or ""),
        })
        if len(achados) >= maximo:
            break
    return achados


def catalogo_autorizado(pedido: str, catalogo: list[dict] | None) -> dict | None:
    """O arquivo do catálogo que o ``pedido`` cita: pelo id, pelo nome, ou pelo
    nome no fim de um caminho (o cérebro às vezes inventa a pasta)."""
    alvo = _normalizar(str(pedido or ""))
    if not alvo or not catalogo:
        return None
    m = _FILE_ID.search(alvo)
    if m:
        fid = m.group(0).lower()
        return next((c for c in catalogo if str(c.get("file_id", "")).lower() == fid), None)
    nome = alvo.rsplit("/", 1)[-1].casefold()
    mesmos = [
        c for c in catalogo
        if nome in {_normalizar(n).casefold() for n in [c.get("nome", ""), *c.get("nomes", [])]}
    ]
    return mesmos[0] if len(mesmos) == 1 else None
