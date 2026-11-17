"""Escolher o vídeo certo do YouTube pelos DADOS antes de baixar.

Nasceu da session-94621cc8 (17/11): "baixa o vídeo do YouTube 'Já acabou,
Jéssica?'" baixou o primeiro resultado do `web_search`, uma paródia do Isaac do
VINE, e a "versão original" só veio porque o `ytsearch1:` do YouTube calhou de
pôr o certo no topo. Ninguém comparava os candidatos.

Aqui a comparação é código, não opinião do modelo: termos da consulta no título
e no canal, a frase inteira, views (log), duração (curta, quando é meme) e
penalidade para o que costuma ser cópia (reação, zoeira, "dark", "nova versão",
"como está hoje", reportagem). Só quando os dados empatam a tool compara os
quadros (`core.video.tools`), e só quando nem isso decide ela pergunta ao Daniel.

Os números vivem em ``[video]`` no ``config/neo.toml``.
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Candidato:
    id: str
    titulo: str
    canal: str = ""
    duracao_s: float | None = None
    views: int | None = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    def miniatura(self, nome: str = "hqdefault") -> str:
        return f"https://i.ytimg.com/vi/{self.id}/{nome}.jpg"

    def resumo(self) -> str:
        """Uma linha legível: título · canal · duração · views."""
        partes = [self.titulo]
        if self.canal:
            partes.append(self.canal)
        if self.duracao_s is not None:
            partes.append(duracao_legivel(self.duracao_s))
        if self.views is not None:
            partes.append(f"{views_legiveis(self.views)} views")
        return " · ".join(partes)


@dataclass
class Pontuado:
    candidato: Candidato
    nota: float
    cobertura: float
    frase: bool
    penalidades: list[str] = field(default_factory=list)
    elegivel: bool = False

    def como_dict(self) -> dict[str, Any]:
        c = self.candidato
        return {
            "id": c.id,
            "url": c.url,
            "titulo": c.titulo,
            "canal": c.canal,
            "duracao": duracao_legivel(c.duracao_s) if c.duracao_s is not None else None,
            "views": c.views,
            "nota": round(self.nota, 3),
            "penalidades": self.penalidades,
        }


# ── texto ────────────────────────────────────────────────────────────────────

def normalizar(texto: str) -> str:
    """Sem acento, minúsculo, só letras e números separados por espaço."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", str(texto or "").lower())
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(re.findall(r"[a-z0-9]+", sem_acento))


def fichas(texto: str) -> list[str]:
    return normalizar(texto).split()


#: Palavras do PEDIDO que não estão no título de vídeo nenhum ("baixa o vídeo
#: meme do YouTube … versão original"). Os termos que sobram são o que procurar.
_META = frozenset(
    """o a os as de do da dos das e um uma uns umas que com para pra no na nos nas
    em esse essa isso este esta aquele aquela me mim manda mandar baixa baixar baixe
    video videos youtube yt meme memes original oficial versao clipe aqui chat quero
    favor por ele ela""".split()
)


def termos_da_consulta(consulta: str) -> list[str]:
    todas = fichas(consulta)
    uteis = [t for t in todas if t not in _META]
    return uteis or todas


def duracao_legivel(segundos: float | None) -> str:
    if segundos is None:
        return "?"
    s = int(round(segundos))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def views_legiveis(views: int) -> str:
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f} mi".replace(".", ",")
    if views >= 1_000:
        return f"{views // 1_000} mil"
    return str(views)


# ── a saída do yt-dlp ─────────────────────────────────────────────────────────

def parse_candidatos(saida: str) -> list[Candidato]:
    """Uma linha JSON por resultado (`--print '%(.{id,title,…})j'`), sem repetir id."""
    vistos: set[str] = set()
    candidatos: list[Candidato] = []
    for linha in (saida or "").splitlines():
        linha = linha.strip()
        if not linha.startswith("{"):
            continue
        try:
            dado = json.loads(linha)
        except json.JSONDecodeError:
            continue
        vid = str(dado.get("id") or "").strip()
        titulo = str(dado.get("title") or "").strip()
        if not vid or not titulo or vid in vistos:
            continue
        vistos.add(vid)
        candidatos.append(
            Candidato(
                id=vid,
                titulo=titulo,
                canal=str(dado.get("channel") or dado.get("uploader") or "").strip(),
                duracao_s=_numero(dado.get("duration")),
                views=_inteiro(dado.get("view_count")),
            )
        )
    return candidatos


def _numero(valor: Any) -> float | None:
    try:
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


def _inteiro(valor: Any) -> int | None:
    numero = _numero(valor)
    return int(numero) if numero is not None else None


# ── a nota ───────────────────────────────────────────────────────────────────

def _casa(termo: str, alvo: list[str]) -> bool:
    """O termo está no título/canal? Curto só exato; 4+ letras vale dentro de outra.

    "marco" casa com "marcos" e com o canal "marcoveioofc"; "taca" com "tacale".
    """
    if termo in alvo:
        return True
    return len(termo) >= 4 and any(termo in ficha for ficha in alvo)


def pontuar(
    consulta: str, candidatos: list[Candidato], *, curto: bool, aj: Any
) -> list[Pontuado]:
    """Nota de cada empresa, da maior para a menor."""
    termos = termos_da_consulta(consulta)
    frase_da_consulta = " ".join(termos)
    consulta_norm = f" {normalizar(consulta)} "
    conhecidas = [c.views for c in candidatos if c.views is not None]
    maior = max(conhecidas) if conhecidas else None
    resultado: list[Pontuado] = []
    for c in candidatos:
        alvo = fichas(f"{c.titulo} {c.canal}")
        cobertura = sum(_casa(t, alvo) for t in termos) / len(termos) if termos else 0.0
        frase = len(termos) >= 2 and frase_da_consulta in normalizar(c.titulo)
        texto = f" {normalizar(f'{c.titulo} {c.canal}')} "
        ruins = [
            p for p in aj.penalidades
            if f" {normalizar(p)} " in texto and f" {normalizar(p)} " not in consulta_norm
        ]
        nota = (
            aj.peso_titulo * cobertura
            + aj.peso_frase * (1.0 if frase else 0.0)
            + aj.peso_views * _nota_views(c.views, maior)
            + aj.peso_duracao * _nota_duracao(c.duracao_s, curto, aj)
            - aj.peso_penalidade * (1.0 if ruins else 0.0)
        )
        resultado.append(
            Pontuado(
                candidato=c,
                nota=nota,
                cobertura=cobertura,
                frase=frase,
                penalidades=ruins,
                elegivel=cobertura >= aj.cobertura_min and not ruins,
            )
        )
    resultado.sort(key=lambda p: p.nota, reverse=True)
    return resultado


def _nota_views(views: int | None, maior: int | None) -> float:
    """log das views relativo ao mais visto. Sem o dado, neutro (0,5)."""
    if views is None or not maior:
        return 0.5
    return math.log10(views + 1) / math.log10(maior + 1)


def _nota_duracao(segundos: float | None, curto: bool, aj: Any) -> float:
    """Meme é curto. Fora de meme a duração não conta (neutro)."""
    if not curto or segundos is None:
        return 0.5
    if segundos <= aj.curto_max_s:
        return 1.0
    if segundos <= aj.medio_max_s:
        return 0.5
    return 0.0


def decidir(pontuados: list[Pontuado], aj: Any) -> dict[str, Any]:
    """`claro` (um se destaca), `comparar` (empate entre elegíveis) ou `nada`."""
    elegiveis = [p for p in pontuados if p.elegivel]
    if not elegiveis:
        return {"veredito": "nada", "escolhido": None, "comparar": []}
    if len(elegiveis) == 1 or elegiveis[0].nota - elegiveis[1].nota >= aj.margem:
        return {"veredito": "claro", "escolhido": elegiveis[0], "comparar": []}
    return {
        "veredito": "comparar",
        "escolhido": None,
        "comparar": elegiveis[: aj.comparar_max],
    }


def eh_meme(*textos: str | None) -> bool:
    return any("meme" in fichas(t or "") for t in textos)


# ── o que já foi escolhido (para a trava de download às cegas) ───────────────

#: id → quando foi escolhido. Em memória: é uma trava de turno, não um registro.
_ESCOLHIDOS: dict[str, float] = {}


def registrar_escolha(ids: list[str]) -> None:
    agora = time.monotonic()
    for vid in ids:
        if vid:
            _ESCOLHIDOS[vid] = agora


def foi_escolhido(vid: str, validade_s: float) -> bool:
    quando = _ESCOLHIDOS.get(vid)
    return quando is not None and time.monotonic() - quando <= validade_s


# ── o comando de download ────────────────────────────────────────────────────

_YTDLP = re.compile(r"(?:^|[\s;&|(`$/])(?:yt-dlp|youtube-dl)(?=$|[\s'\"])", re.I)
#: Só lê (lista, metadado, formatos): não baixa nada, não escolhe nada.
_SO_LE = re.compile(
    r"--flat-playlist|--skip-download|--list-formats|--dump-json|--dump-single-json"
    r"|--simulate|--get-(?:title|id|url|duration|filename)|(?:^|\s)-(?:F|j|J|s)(?=\s|$)"
)
_ID_DO_YOUTUBE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^\s'\"]*&)?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)
_BUSCA_CEGA = re.compile(r"\bytsearch\d*:", re.I)


def download_de_youtube(comando: str) -> tuple[bool, list[str], bool]:
    """(é download pelo yt-dlp, ids do YouTube no comando, é busca às cegas)."""
    texto = str(comando or "")
    if not _YTDLP.search(texto) or _SO_LE.search(texto):
        return False, [], False
    return True, _ID_DO_YOUTUBE.findall(texto), bool(_BUSCA_CEGA.search(texto))


def escolha_pendente(comando: str, pedido: str | None, validade_s: float) -> str | None:
    """Por que este download ainda não pode rodar (ou None se pode).

    Pode: o vídeo foi escolhido pela `neo_video_candidatos`, ou o Daniel mandou
    o link/id na mensagem dele. Não pode: busca às cegas (`ytsearch1:` baixa o
    1º resultado) e link que veio de busca sem comparação.
    """
    eh, ids, cega = download_de_youtube(comando)
    if not eh:
        return None
    if cega:
        return "o `ytsearch` baixa o primeiro resultado da busca do YouTube, sem comparar"
    pedido = pedido or ""
    soltos = [i for i in ids if not foi_escolhido(i, validade_s) and i not in pedido]
    if soltos:
        return "o link veio de uma busca, e ninguém comparou os candidatos"
    return None
