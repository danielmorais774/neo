"""A tool ``neo_video_candidatos``: escolher o vídeo do YouTube antes de baixar.

Três degraus, do mais barato ao mais caro (decisão do Daniel, 17/11):

1. **Dados.** A busca roda no sandbox (`yt-dlp --flat-playlist ytsearch8:`, ~2 s:
   a página de busca não é barrada pelo IP de datacenter, só o vídeo) e a nota
   é código (``core.video.escolha``). Um candidato se destacou: é ele.
2. **Quadros.** Empate: para cada empatado, a capa e os quadros automáticos do
   YouTube a 25/50/75 % (`hq1..3.jpg`, que também passam pelo bloqueio) viram
   uma grade 2×2 no sandbox, e o modelo de visão compara as grades.
3. **Perguntar.** Nem os quadros decidem: a tool devolve as miniaturas para o
   Neo mostrar no chat e perguntar qual.

O prefixo ``neo_`` deixa a tool só para o cérebro, como a ``neo_transcrever``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import uuid
from pathlib import Path
from typing import Any

from shared import ToolParameter, ToolSchema

from core.video import escolha

logger = logging.getLogger(__name__)

TOOL_NAME = "neo_video_candidatos"

VIDEO_TOOLS: list[ToolSchema] = [
    ToolSchema(
        # Literal: tests/authority/test_risk_cobre_o_registry.py varre o fonte.
        name="neo_video_candidatos",
        description=(
            "Escolhe o vídeo CERTO do YouTube antes de baixar, quando o Daniel descreveu o vídeo "
            "(nome, meme, cena) em vez de mandar o link. Busca 8 resultados, compara título, canal, "
            "duração e views e, se empatar, compara os quadros dos vídeos com um modelo de visão. "
            "Devolve o escolhido (com a URL para baixar) ou, se nem os quadros decidirem, as "
            "miniaturas para você mostrar e perguntar qual. Use SEMPRE antes de baixar vídeo do "
            "YouTube que ele não mandou o link: o 1º resultado de uma busca costuma ser reação, "
            "paródia ou reupload. Não baixa nada."
        ),
        module="video",
        execution="server-side",
        timeout=180,
        parameters={
            "consulta": ToolParameter(
                type="string",
                description=(
                    "O que buscar no YouTube: o nome do vídeo/meme como ele falou "
                    "(ex.: 'Já acabou, Jéssica?'). Sem 'baixa', 'me manda' etc."
                ),
                required=True,
            ),
            "descricao": ToolParameter(
                type="string",
                description=(
                    "Como é a cena, se você souber (ex.: 'menina chorando depois de uma briga na rua'). "
                    "Só ajuda a comparação de quadros; deixe vazio se não souber."
                ),
                required=False,
            ),
            "curto": ToolParameter(
                type="boolean",
                description=(
                    "É um vídeo curto (meme, trecho viral)? Prefere os de até ~1,5 min. "
                    "Padrão: sim quando a consulta ou a descrição fala em meme."
                ),
                required=False,
            ),
        },
    ),
]

VIDEO_TOOL_NAMES = frozenset(t.name for t in VIDEO_TOOLS)

_COMANDO_DO_MAC = (
    'yt-dlp -P ~/Downloads -f "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b" '
    '--merge-output-format mp4 --print after_move:filepath "{url}"'
)

_VISAO_SISTEMA = (
    "Você compara vídeos do YouTube pelos quadros, para um assistente que não enxerga. "
    "Cada imagem é UM candidato, com a letra dele no canto: a capa e três quadros tirados "
    "do meio do vídeo. Responda SÓ com JSON, sem texto em volta."
)

#: Script que roda no sandbox: baixa capa + hq1..3 de cada empresa e monta uma
#: grade 2×2 com a letra no canto. Recebe JSON em argv[1]: {"dir":…, "itens":[[letra,id],…]}.
_GRADE_PY = r"""
import io, json, os, sys, urllib.request
from PIL import Image, ImageDraw, ImageFont
p = json.loads(sys.argv[1]); os.makedirs(p["dir"], exist_ok=True)
try:
    fonte = ImageFont.load_default(size=56)
except TypeError:
    fonte = ImageFont.load_default()
feitos = {}
for letra, vid in p["itens"]:
    quadros = []
    for nome in ("hqdefault", "hq1", "hq2", "hq3"):
        try:
            with urllib.request.urlopen(f"https://i.ytimg.com/vi/{vid}/{nome}.jpg", timeout=10) as r:
                quadros.append(Image.open(io.BytesIO(r.read())).convert("RGB").resize((480, 360)))
        except Exception:
            pass
    if not quadros:
        continue
    grade = Image.new("RGB", (960, 720), "black")
    for i, q in enumerate(quadros[:4]):
        grade.paste(q, ((i % 2) * 480, (i // 2) * 360))
    d = ImageDraw.Draw(grade)
    d.rectangle([0, 0, 90, 80], fill="black")
    d.text((22, 8), letra, fill="yellow", font=fonte)
    destino = os.path.join(p["dir"], letra + ".jpg")
    grade.save(destino, "JPEG", quality=80)
    feitos[letra] = destino
print(json.dumps(feitos))
"""


def _ajustes() -> Any:
    from shared.ajustes import ajustes

    return ajustes().video


class VideoExecutor:
    """Executor de módulo (``execute(nome, args) -> dict``) do ``ToolExecutor`` global."""

    def __init__(self, *, sandbox: Any, llm: Any = None, workspace_local: str = "/workspace") -> None:
        self.sandbox = sandbox
        self.llm = llm
        self.workspace_local = Path(workspace_local)

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name != TOOL_NAME:
            raise ValueError(f"tool desconhecida no módulo video: {tool_name}")
        args = arguments or {}
        consulta = str(args.get("consulta") or "").strip()
        if not consulta:
            return {"ok": False, "erro": "passe 'consulta' (o nome do vídeo que ele descreveu)."}
        descricao = str(args.get("descricao") or "").strip()
        curto = args.get("curto")
        if curto is None:
            curto = escolha.eh_meme(consulta, descricao)
        return await self.escolher(consulta, descricao=descricao, curto=bool(curto))

    async def escolher(self, consulta: str, *, descricao: str = "", curto: bool = False) -> dict[str, Any]:
        aj = _ajustes()
        candidatos, erro = await self._buscar(consulta, aj)
        if erro:
            return {"ok": False, "erro": erro}
        pontuados = escolha.pontuar(consulta, candidatos, curto=curto, aj=aj)
        decisao = escolha.decidir(pontuados, aj)
        lista = [p.como_dict() for p in pontuados]

        if decisao["veredito"] == "claro":
            p = decisao["escolhido"]
            return self._escolhido(p, lista, "dados", self._motivo_dos_dados(p, pontuados))

        if decisao["veredito"] == "nada":
            return {
                "ok": True,
                "veredito": "nenhum",
                "candidatos": lista,
                "proximo_passo": (
                    "Nenhum resultado bate com o pedido (títulos sem os termos, ou só reação/"
                    "reupload). Mostre os mais próximos ao Daniel ou peça mais detalhe "
                    "(canal, ano, uma fala do vídeo). Não baixe nenhum por conta própria."
                ),
            }

        empatados: list[escolha.Pontuado] = decisao["comparar"]
        visto = await self._comparar_quadros(consulta, descricao, empatados, aj)
        if visto.get("escolhido") is not None:
            return self._escolhido(visto["escolhido"], lista, "quadros", visto["motivo"])
        return self._perguntar(empatados, lista, visto.get("motivo") or "")

    # ── degrau 1: busca ──────────────────────────────────────────────────────

    async def _buscar(self, consulta: str, aj: Any) -> tuple[list[escolha.Candidato], str]:
        alvo = f"ytsearch{aj.candidatos}:{consulta}"
        comando = (
            'export PATH="$HOME/.local/bin:$PATH"; '
            "command -v yt-dlp >/dev/null 2>&1 || pip install --user -q yt-dlp >/dev/null 2>&1; "
            "yt-dlp --no-warnings --flat-playlist "
            "--print '%(.{id,title,channel,uploader,duration,view_count})j' "
            f"{shlex.quote(alvo)}"
        )
        r = await self.sandbox.run(comando, timeout=aj.busca_timeout_s)
        candidatos = escolha.parse_candidatos(r.stdout or "")
        if candidatos:
            return candidatos, ""
        detalhe = (r.stderr or r.stdout or "").strip()[-300:]
        return [], f"a busca do YouTube não devolveu resultado ({detalhe or f'saída {r.exit_code}'})."

    # ── degrau 2: quadros ────────────────────────────────────────────────────

    async def _comparar_quadros(
        self, consulta: str, descricao: str, empatados: list[escolha.Pontuado], aj: Any
    ) -> dict[str, Any]:
        """{"escolhido": Pontuado|None, "motivo": str}. Nunca levanta."""
        if self.llm is None:
            return {"escolhido": None, "motivo": "sem modelo de visão neste core"}
        letras = "ABCD"[: len(empatados)]
        por_letra = dict(zip(letras, empatados))
        raiz = getattr(self.sandbox, "workspace_root", "/workspace").rstrip("/")
        pasta = f"{raiz}/.neo/video/{uuid.uuid4().hex[:12]}"
        try:
            pedido = json.dumps({"dir": pasta, "itens": [[l, p.candidato.id] for l, p in por_letra.items()]})
            r = await self.sandbox.run(
                f"python3 -c {shlex.quote(_GRADE_PY)} {shlex.quote(pedido)}",
                timeout=aj.quadros_timeout_s,
            )
            try:
                feitos: dict[str, str] = json.loads((r.stdout or "").strip().splitlines()[-1])
            except (json.JSONDecodeError, IndexError):
                feitos = {}
            if not feitos:
                return {"escolhido": None, "motivo": "não consegui baixar os quadros dos vídeos"}
            imagens, ordem = [], []
            for letra in letras:
                if letra in feitos:
                    dados = self._local(feitos[letra]).read_bytes()
                    imagens.append(f"data:image/jpeg;base64,{base64.b64encode(dados).decode()}")
                    ordem.append(letra)
            resposta = await self._perguntar_visao(consulta, descricao, por_letra, ordem, imagens)
        except Exception as exc:  # noqa: BLE001 — comparar é degrau extra, nunca derruba
            logger.warning("[video] comparação de quadros falhou: %s", exc)
            return {"escolhido": None, "motivo": f"a comparação de quadros falhou ({exc})"}
        finally:
            try:
                await self.sandbox.run(f"rm -rf {shlex.quote(pasta)}", timeout=30)
            except Exception:  # noqa: BLE001
                pass
        return self._ler_visao(resposta, por_letra)

    async def _perguntar_visao(
        self,
        consulta: str,
        descricao: str,
        por_letra: dict[str, escolha.Pontuado],
        ordem: list[str],
        imagens: list[str],
    ) -> str:
        from shared.config import get_settings
        from shared.types import Message, MessageRole

        dados = "\n".join(f"{l} = {por_letra[l].candidato.resumo()}" for l in ordem)
        pergunta = (
            f'O Daniel quer o vídeo ORIGINAL de: "{consulta}".'
            + (f" Como a cena é: {descricao}." if descricao else "")
            + f"\nCandidatos (na ordem das imagens):\n{dados}\n\n"
            "Qual mostra a cena original? Descarte reação (alguém assistindo), montagem, edição com "
            "efeitos, legendas ou música por cima, compilação, reportagem e versão nova. Se mais de "
            "um mostra a MESMA cena original, liste todos em mesma_cena.\n"
            'JSON: {"escolhido": "A"|"B"|"C"|"D"|"nenhum", "mesma_cena": ["A", ...], '
            '"certeza": "alta"|"media"|"baixa", "motivo": "uma frase"}'
        )
        modelo = get_settings().browser_local_vision_model
        provider = self.llm.get_provider_for_model(modelo)
        resposta = await provider.complete(
            messages=[
                Message(role=MessageRole.SYSTEM, content=_VISAO_SISTEMA),
                Message(role=MessageRole.USER, content=pergunta, images=imagens),
            ],
            model=modelo,
            temperature=0.1,
            max_tokens=400,
            disable_thinking=True,
        )
        return (resposta.get_content() or "").strip()

    @staticmethod
    def _ler_visao(texto: str, por_letra: dict[str, escolha.Pontuado]) -> dict[str, Any]:
        """O JSON do modelo de visão → escolhido (só com certeza alta/média)."""
        bloco = re.search(r"\{.*\}", texto or "", re.S)
        try:
            dado = json.loads(bloco.group(0)) if bloco else {}
        except json.JSONDecodeError:
            dado = {}
        motivo = str(dado.get("motivo") or "").strip()
        letra = str(dado.get("escolhido") or "").strip().upper()[:1]
        certeza = str(dado.get("certeza") or "").strip().lower()
        if letra not in por_letra or certeza not in ("alta", "media", "média"):
            return {"escolhido": None, "motivo": motivo or "os quadros não decidiram"}
        # Mesma cena em mais de um: o de nota maior (mais views, título mais fiel)
        # é o upload mais provável do original.
        mesma = [str(x).strip().upper()[:1] for x in (dado.get("mesma_cena") or [])]
        grupo = [por_letra[l] for l in {letra, *mesma} if l in por_letra]
        melhor = max(grupo, key=lambda p: p.nota) if grupo else por_letra[letra]
        return {"escolhido": melhor, "motivo": motivo}

    def _local(self, caminho_no_sandbox: str) -> Path:
        """O arquivo do sandbox pela montagem do core, sem sair do workspace."""
        raiz = getattr(self.sandbox, "workspace_root", "/workspace").rstrip("/")
        if not caminho_no_sandbox.startswith(raiz + "/"):
            raise ValueError(f"fora do workspace: {caminho_no_sandbox}")
        alvo = self.workspace_local / caminho_no_sandbox[len(raiz):].lstrip("/")
        real = Path(os.path.realpath(alvo))
        if Path(os.path.realpath(self.workspace_local)) not in real.parents:
            raise ValueError(f"o caminho sai do workspace: {caminho_no_sandbox}")
        return real

    # ── respostas ────────────────────────────────────────────────────────────

    @staticmethod
    def _motivo_dos_dados(p: escolha.Pontuado, todos: list[escolha.Pontuado]) -> str:
        outros = [q for q in todos if q is not p and q.elegivel]
        if not outros:
            return "é o único resultado que bate com o pedido sem ser reação/reupload"
        return f"nota {p.nota:.2f} contra {outros[0].nota:.2f} do segundo (título, views e duração)"

    @staticmethod
    def _escolhido(p: escolha.Pontuado, lista: list[dict], como: str, motivo: str) -> dict[str, Any]:
        c = p.candidato
        escolha.registrar_escolha([c.id])
        return {
            "ok": True,
            "veredito": "escolhido",
            "como": "pelos dados" if como == "dados" else "pelos quadros (empatou nos dados)",
            "escolhido": p.como_dict(),
            "motivo": motivo,
            "candidatos": lista,
            "proximo_passo": (
                "Baixe ESTE. O YouTube barra o sandbox, então rode na máquina do Daniel: "
                f"`run_command` com `{_COMANDO_DO_MAC.format(url=c.url)}`. A última linha da saída é o "
                "caminho: entregue com `[[FILE:<caminho>]]`. Diga em meia frase qual vídeo foi "
                f"(\"{c.resumo()}\")."
            ),
        }

    @staticmethod
    def _perguntar(empatados: list[escolha.Pontuado], lista: list[dict], motivo: str) -> dict[str, Any]:
        escolha.registrar_escolha([p.candidato.id for p in empatados])
        miniaturas = [
            f"[[IMAGE:{p.candidato.miniatura()}|{l} · {p.candidato.resumo()}]]"
            for l, p in zip("ABCD", empatados)
        ]
        return {
            "ok": True,
            "veredito": "perguntar",
            "motivo": motivo or "os dados empataram e os quadros não decidiram",
            "empatados": [p.como_dict() for p in empatados],
            "candidatos": lista,
            "miniaturas": miniaturas,
            "proximo_passo": (
                "NÃO baixe ainda. Mostre estas miniaturas ao Daniel, uma por linha, exatamente como "
                "estão, e pergunte qual é (A, B, C…). Quando ele responder, baixe o escolhido na "
                f"máquina dele com `{_COMANDO_DO_MAC.format(url='<url do escolhido>')}`."
            ),
        }
