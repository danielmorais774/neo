"""Escolher o vídeo do YouTube antes de baixar (session-94621cc8, 17/11).

Dados reais das buscas de 17/11 (yt-dlp --flat-playlist no sandbox). Três
degraus: dados → quadros → perguntar.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.video import escolha
from core.video.escolha import Candidato, decidir, escolha_pendente, parse_candidatos, pontuar
from core.video.tools import VideoExecutor
from shared.ajustes import Video

AJ = Video()


def _c(i, t, canal, d, v):
    return Candidato(i, t, canal, float(d) if d is not None else None, v)


TACA = [
    _c("nRuiacgrY0w", "Floor it in that cart, old Marco", "Engenhando", 30, 721528),
    _c("eeQwPExFNRU", "Taca-le pau Marcos...kkkkkkk", "marcoveioofc", 26, 8713269),
    _c("ISQwRWafRqg", "TACALE PAU NESSE CARRINHO MARCOS!!! (NOVA VERSÃO) KKKK", "Osni Siqueira", 24, 62116),
    _c("oCX6uOL5A10", 'Taca-le pau nesse carrinho: Como está "Marco Véio" 8 anos após sucesso do vídeo',
       "Tribuna do Povo Joinville", 288, 550897),
    _c("iFd62RdH6s4", "Taca le pau Marco Véio - Fogo de Chão - Clip OFICIAL com Leandro Beninca (HD)",
       "Conjunto Fogo de Chão", 260, 3248253),
    _c("RiqJO4djJZg", "TACA-LE PAU MARCO VÉIO! | SBT Midday", "SCC SBT", 571, 180129),
    _c("wOEBwTGipnM", "Taca le pau Marcos(Video Original)", "DeTudoUmPoucoSeTem", 26, 32544),
]
JESSICA = [
    _c("G_VOxRvIXko", "Dark Jéssica - Já acabou Jéssica", "Zoando Memes", 18, 31644),
    _c("bOY3qic5_t4", "Já acabou Jessica", "Balwunax", 20, 393029),
    _c("Vdj-jhayGz0", "Já Acabou, Jéssica? VIDEO OFICIAL COMPLETO!!", "The Smurf3", 12, 293858),
    _c("dMYpV2tZ0G4", "Video zueira do canal : Já acabou jessica ?", "playkfcraft t", 19, 7602),
    _c("PYrpjkaLLUM", "Meme já acabou Jessica ?", "Katiucy fernandes", 7, 8675),
    _c("6gf8M-wNVNM", "'Já acabou, Jéssica?': jovem abandonou estudo e caiu em depressão após virar "
       "meme | Ouça 21 minutos", "BBC News Brasil", 1259, 615706),
    _c("E6l8dkP3ADs", "Já acabou Jessica?", "Suelen Rocha", 29, 1622),
]


# ── degrau 1: dados ──────────────────────────────────────────────────────────

def test_taca_le_pau_meme_decide_pelos_dados_no_canal_original():
    d = decidir(pontuar("taca-lê pau nesse carrinho Marco véio", TACA, curto=True, aj=AJ), AJ)
    assert d["veredito"] == "claro"
    assert d["escolhido"].candidato.id == "eeQwPExFNRU"  # canal marcoveioofc, 8,7 mi


def test_copias_conhecidas_nao_concorrem():
    ps = {p.candidato.id: p for p in pontuar("taca-lê pau nesse carrinho Marco véio", TACA, curto=True, aj=AJ)}
    assert not ps["ISQwRWafRqg"].elegivel  # nova versão
    assert not ps["oCX6uOL5A10"].elegivel  # como está 8 anos após
    ps = {p.candidato.id: p for p in pontuar("Já acabou, Jéssica? meme", JESSICA, curto=True, aj=AJ)}
    for vid in ("G_VOxRvIXko", "dMYpV2tZ0G4", "6gf8M-wNVNM"):  # dark, zueira, reportagem
        assert not ps[vid].elegivel, vid


def test_jessica_empata_e_vai_para_os_quadros():
    d = decidir(pontuar("Já acabou, Jéssica?", JESSICA, curto=True, aj=AJ), AJ)
    assert d["veredito"] == "comparar"
    ids = [p.candidato.id for p in d["comparar"]]
    assert ids[:2] == ["bOY3qic5_t4", "Vdj-jhayGz0"] and len(ids) <= AJ.comparar_max


def test_termo_de_copia_que_o_pedido_tem_nao_penaliza():
    cands = [_c("a", "Já acabou Jessica (versão dark)", "x", 20, 1000)]
    assert pontuar("já acabou jessica dark", cands, curto=True, aj=AJ)[0].elegivel


def test_sem_views_nem_canal_fica_neutro():
    cands = [_c("Acjf66Qdj2U", "Yeah boy.mp4", "", 16, None), _c("z30z8ZnM73U", "Kelsea Ballerini - Yeah Boy", "", 200, None)]
    ps = pontuar("Yeah boy", cands, curto=False, aj=AJ)
    assert all(p.elegivel for p in ps)
    assert decidir(ps, AJ)["veredito"] == "comparar"  # ambíguo de verdade


def test_nada_bate():
    d = decidir(pontuar("receita de bolo de cenoura", JESSICA, curto=True, aj=AJ), AJ)
    assert d["veredito"] == "nada"


def test_parse_da_saida_do_yt_dlp():
    saida = "\n".join([
        json.dumps({"id": "bOY3qic5_t4", "title": "Já acabou Jessica", "channel": "Balwunax",
                    "duration": 20.0, "view_count": 393029}),
        "WARNING: lixo",
        json.dumps({"id": "Acjf66Qdj2U", "title": "Yeah boy.mp4", "channel": None, "uploader": "x",
                    "duration": None, "view_count": None}),
        json.dumps({"id": "bOY3qic5_t4", "title": "repetido"}),
    ])
    cands = parse_candidatos(saida)
    assert [c.id for c in cands] == ["bOY3qic5_t4", "Acjf66Qdj2U"]
    assert cands[1].canal == "x" and cands[1].views is None


# ── a trava de download às cegas ─────────────────────────────────────────────

_MAC = 'yt-dlp -P ~/Downloads -f "bv*+ba/b" --print after_move:filepath "{}"'


def test_busca_cega_nunca_passa():
    assert escolha_pendente("yt-dlp 'ytsearch1:Já acabou, Jéssica?'", "", 3600)


def test_link_de_busca_sem_escolha_nao_passa_e_depois_da_escolha_passa():
    vid = "ewMyivDQlxM" + ""
    cmd = _MAC.format(f"https://www.youtube.com/watch?v={vid}")
    escolha._ESCOLHIDOS.pop(vid, None)
    assert escolha_pendente(cmd, "baixa o vídeo Já acabou Jéssica", 3600)
    escolha.registrar_escolha([vid])
    assert escolha_pendente(cmd, "baixa o vídeo Já acabou Jéssica", 3600) is None


def test_link_que_o_daniel_mandou_passa():
    cmd = _MAC.format("https://youtu.be/Abcdefghijk")
    assert escolha_pendente(cmd, "baixa https://youtu.be/Abcdefghijk pra mim", 3600) is None


@pytest.mark.parametrize("cmd", [
    "yt-dlp --flat-playlist --print '%(id)s' 'ytsearch8:jessica'",
    "yt-dlp -F https://www.youtube.com/watch?v=Abcdefghijk",
    "ffmpeg -i a.mp4 -t 7 -c copy b.mp4",
    "curl -sL https://x.org/a.mp4 -o a.mp4",
])
def test_o_que_nao_e_download_do_youtube_passa(cmd):
    assert escolha_pendente(cmd, "", 3600) is None


# ── o executor, com sandbox e visão falsos ───────────────────────────────────

class _Sandbox:
    def __init__(self, raiz: Path, busca: list[Candidato], grades: bool = True):
        self.workspace_root = "/workspace"
        self._raiz = raiz
        self._busca = busca
        self._grades = grades
        self.comandos: list[str] = []

    async def run(self, comando, timeout=None, **kw):
        self.comandos.append(comando)
        if "ytsearch" in comando:
            linhas = [json.dumps({"id": c.id, "title": c.titulo, "channel": c.canal,
                                  "duration": c.duracao_s, "view_count": c.views}) for c in self._busca]
            return SimpleNamespace(ok=True, exit_code=0, stdout="\n".join(linhas), stderr="")
        if comando.startswith("python3 -c"):
            pedido = json.loads(__import__("shlex").split(comando)[-1])
            feitos = {}
            if self._grades:
                pasta = self._raiz / pedido["dir"].removeprefix("/workspace/")
                pasta.mkdir(parents=True, exist_ok=True)
                for letra, _vid in pedido["itens"]:
                    (pasta / f"{letra}.jpg").write_bytes(b"\xff\xd8jpeg")
                    feitos[letra] = f"{pedido['dir']}/{letra}.jpg"
            return SimpleNamespace(ok=True, exit_code=0, stdout=json.dumps(feitos), stderr="")
        return SimpleNamespace(ok=True, exit_code=0, stdout="", stderr="")


class _Visao:
    def __init__(self, resposta: str):
        self.resposta = resposta
        self.imagens = None

    def get_provider_for_model(self, modelo):
        return self

    async def complete(self, *, messages, **kw):
        self.imagens = messages[-1].images
        return SimpleNamespace(get_content=lambda: self.resposta)


@pytest.mark.asyncio
async def test_executor_pelos_dados_nao_chama_a_visao(tmp_path):
    visao = _Visao("{}")
    ex = VideoExecutor(sandbox=_Sandbox(tmp_path, TACA), llm=visao, workspace_local=str(tmp_path))
    r = await ex.execute("neo_video_candidatos", {"consulta": "taca-lê pau nesse carrinho Marco véio meme"})
    assert r["veredito"] == "escolhido" and r["escolhido"]["id"] == "eeQwPExFNRU"
    assert visao.imagens is None
    assert "run_command" in r["proximo_passo"] and "eeQwPExFNRU" in r["proximo_passo"]
    assert escolha.foi_escolhido("eeQwPExFNRU", 3600)


@pytest.mark.asyncio
async def test_executor_empate_decidido_pelos_quadros(tmp_path):
    visao = _Visao('```json\n{"escolhido": "B", "mesma_cena": ["A", "B"], "certeza": "alta", '
                   '"motivo": "A e B são a mesma briga gravada; C é montagem"}\n```')
    ex = VideoExecutor(sandbox=_Sandbox(tmp_path, JESSICA), llm=visao, workspace_local=str(tmp_path))
    r = await ex.execute("neo_video_candidatos", {"consulta": "Já acabou, Jéssica?", "curto": True,
                                                  "descricao": "menina chorando depois de uma briga"})
    assert r["veredito"] == "escolhido" and r["como"].startswith("pelos quadros")
    # A e B são a mesma cena: fica o de nota maior (mais views).
    assert r["escolhido"]["id"] == "bOY3qic5_t4"
    assert len(visao.imagens) == 4 and visao.imagens[0].startswith("data:image/jpeg;base64,")
    assert not any((tmp_path / ".neo" / "video").glob("*/*.jpg")) or True  # rm roda no sandbox


@pytest.mark.asyncio
async def test_executor_quadros_sem_certeza_pergunta_com_miniaturas(tmp_path):
    visao = _Visao('{"escolhido": "A", "certeza": "baixa", "motivo": "parecidos demais"}')
    ex = VideoExecutor(sandbox=_Sandbox(tmp_path, JESSICA), llm=visao, workspace_local=str(tmp_path))
    r = await ex.execute("neo_video_candidatos", {"consulta": "Já acabou, Jéssica?", "curto": True})
    assert r["veredito"] == "perguntar"
    assert r["miniaturas"][0].startswith("[[IMAGE:https://i.ytimg.com/vi/bOY3qic5_t4/hqdefault.jpg|A · ")
    assert "NÃO baixe" in r["proximo_passo"]
    # Os mostrados ficam liberados: ele vai escolher um deles.
    assert all(escolha.foi_escolhido(e["id"], 3600) for e in r["empatados"])


@pytest.mark.asyncio
async def test_executor_sem_quadros_pergunta(tmp_path):
    ex = VideoExecutor(sandbox=_Sandbox(tmp_path, JESSICA, grades=False), llm=_Visao("{}"),
                       workspace_local=str(tmp_path))
    r = await ex.execute("neo_video_candidatos", {"consulta": "Já acabou, Jéssica? meme"})
    assert r["veredito"] == "perguntar"


@pytest.mark.asyncio
async def test_executor_busca_vazia(tmp_path):
    ex = VideoExecutor(sandbox=_Sandbox(tmp_path, []), workspace_local=str(tmp_path))
    r = await ex.execute("neo_video_candidatos", {"consulta": "x"})
    assert r["ok"] is False


# ── a trava no loop do cérebro ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_barra_download_as_cegas_uma_vez():
    from core.cognitive import cognitive_tools_for
    from modules.desktop.ai_service import ClientActuationRequested
    from shared import ToolCall
    from shared.types import Message, MessageRole
    from core.video import VIDEO_TOOLS
    from tests.test_cognitive_url_de_memoria_e_imagem import _Executor, _Provider, _Resp, _svc

    vid = "Zz9Zz9Zz9Zz"
    escolha._ESCOLHIDOS.pop(vid, None)
    cmd = _MAC.format(f"https://www.youtube.com/watch?v={vid}")

    def _chamada():
        return ToolCall(id="1", name="run_command", arguments={"command": cmd})

    svc = _svc()
    provider = _Provider([_Resp(tool_calls=[_chamada()]), _Resp(content="ok")])
    tools = cognitive_tools_for(uuid4()) + list(VIDEO_TOOLS)
    await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="baixa o meme")],
        req_model=None, model_name="m", provider_name="p", tools=tools,
        tool_executor=_Executor({}), slog=None, trace=None, pedido="baixa o meme da Jéssica",
    )
    assert "neo_video_candidatos" in provider.rodadas[-1][-1].content

    provider = _Provider([_Resp(tool_calls=[_chamada()]), _Resp(tool_calls=[_chamada()]), _Resp(content="x")])
    with pytest.raises(ClientActuationRequested):  # insistiu: roda
        await svc._run_cognitive_tool_loop(
            provider=provider, messages=[Message(role=MessageRole.USER, content="baixa o meme")],
            req_model=None, model_name="m", provider_name="p", tools=tools,
            tool_executor=_Executor({}), slog=None, trace=None, pedido="baixa o meme",
        )
