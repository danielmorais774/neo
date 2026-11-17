"""O caminho inteiro da ``neo_transcrever``: sandbox falso, Groq falso, cota de verdade."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from core.transcricao import config
from core.transcricao.cota import Cota
from core.transcricao.servico import Transcritor, _duracao_da_parte, _fatiar, _idioma
from core.transcricao.texto import para_srt, tempo_srt
from shared.ajustes import CotaGratuitaGroq, Transcricao
from tests.transcricao.conftest import FakeDB, FakeGroq, FakeSandbox


def _ajustes(**campos):
    cota = campos.pop("cota", {})
    return Transcricao(**campos, cota=CotaGratuitaGroq(**{"folga_s": 0, **cota}))


class _Relogio:
    def __init__(self) -> None:
        self.t = 5_000_000.0

    def __call__(self) -> float:
        return self.t


def _montar(tmp_path, *, duracao=90.0, partes=None, aj=None, groq=None, db=None):
    sandbox = FakeSandbox(tmp_path, duracao=duracao, partes=partes)
    (tmp_path / "files").mkdir(parents=True, exist_ok=True)
    (tmp_path / "files" / "reuniao.mp4").write_bytes(b"video")
    groq = groq or FakeGroq()
    relogio = _Relogio()
    aj = aj or _ajustes()
    dormidas: list[float] = []

    async def dormir(s):
        dormidas.append(s)
        relogio.t += s

    cota = Cota(db=db, relogio=relogio, limites=lambda: aj.cota)
    t = Transcritor(
        sandbox=sandbox, db=db, workspace_local=str(tmp_path), cota=cota,
        transport=groq.transport, dormir=dormir, ajustes=lambda: aj,
    )
    return SimpleNamespace(t=t, sandbox=sandbox, groq=groq, cota=cota, dormidas=dormidas, raiz=tmp_path)


@pytest.mark.asyncio
async def test_caminho_feliz_grava_txt_e_srt_e_devolve_o_texto(settings, tmp_path):
    m = _montar(tmp_path)
    r = await m.t.transcrever(caminho="files/reuniao.mp4", contexto="Neo, Daniel")

    assert r["ok"] is True, r
    assert r["arquivo_txt"] == "/workspace/files/reuniao.transcricao.txt"
    assert r["arquivo_srt"] == "/workspace/files/reuniao.transcricao.srt"
    txt = (tmp_path / "files" / "reuniao.transcricao.txt").read_text()
    assert txt == "Olá, pedaço 0.\nSegunda frase.\n"
    assert r["texto"] == txt and r["texto_cortado"] is False
    srt = (tmp_path / "files" / "reuniao.transcricao.srt").read_text()
    assert srt.startswith("1\n00:00:00,000 --> 00:00:04,000\nOlá, pedaço 0.\n")

    (pedido,) = m.groq.transcricoes()
    assert pedido.headers["authorization"] == "Bearer gsk_env_1234567890abcd"
    corpo = pedido.content.decode("latin-1")
    for campo, valor in (("model", "whisper-large-v3-turbo"), ("language", "pt"),
                         ("response_format", "verbose_json"), ("prompt", "Neo, Daniel")):
        assert f'name="{campo}"\r\n\r\n{valor}' in corpo

    # o pedaço de trabalho some no fim
    assert not any((tmp_path / ".neo" / "transcricao").iterdir())
    # o sandbox nunca viu a chave
    assert not any("gsk_" in c for c in m.sandbox.comandos)


@pytest.mark.asyncio
async def test_extracao_tira_so_o_audio_em_opus_mono(settings, tmp_path):
    m = _montar(tmp_path)
    await m.t.transcrever(caminho="/workspace/files/reuniao.mp4")
    (ffmpeg,) = [c for c in m.sandbox.comandos if "libopus" in c]
    for trecho in ("-vn", "-ac 1", "-ar 16000", "-c:a libopus", "-b:a 32k", "-segment_time 1200"):
        assert trecho in ffmpeg


@pytest.mark.asyncio
async def test_varios_pedacos_somam_o_deslocamento_nos_tempos(settings, tmp_path):
    m = _montar(tmp_path, duracao=1500.0, partes=[(0.0, 1200.0), (1200.0, 1500.0)])
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] and r["pedacos"] == "2 de 2"
    srt = (tmp_path / "files" / "reuniao.transcricao.srt").read_text()
    assert "00:20:00,000 --> 00:20:04,000\nOlá, pedaço 1." in srt
    assert len(m.groq.transcricoes()) == 2


@pytest.mark.asyncio
async def test_so_gratuita_recusa_antes_de_mandar_o_que_nao_cabe(settings, tmp_path):
    aj = _ajustes(cota={"audio_s_por_hora": 60})
    m = _montar(tmp_path, duracao=90.0, aj=aj)
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is False and "não cabe na cota gratuita" in r["error"]
    assert m.groq.transcricoes() == []
    assert not any("ffmpeg " in c for c in m.sandbox.comandos)


@pytest.mark.asyncio
async def test_so_gratuita_desligado_nao_barra_pela_conta_local(settings, tmp_path):
    db = FakeDB()
    await config.gravar_opcoes(db, so_gratuita=False)
    aj = _ajustes(cota={"audio_s_por_hora": 60})
    m = _montar(tmp_path, duracao=90.0, aj=aj)
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is True
    assert len(m.groq.transcricoes()) == 1


@pytest.mark.asyncio
async def test_cota_ocupada_diz_quando_volta(settings, tmp_path):
    m = _montar(tmp_path, duracao=600.0)
    await m.cota.gastar(7000)  # sobram 200 s na hora: 600 s precisam de ~200 s de espera
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is False and "daqui a ~4 min" in r["error"]
    assert r["volta_em_s"] == 200
    assert m.groq.transcricoes() == []


@pytest.mark.asyncio
async def test_espera_curta_pela_cota_acontece_sem_recusar(settings, tmp_path):
    m = _montar(tmp_path, duracao=100.0)
    await m.cota.gastar(7150)  # faltam 50 s: 25 s de espera, dentro dos 60
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is True
    assert sum(m.dormidas) == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_429_curto_espera_e_repete_429_longo_para(settings, tmp_path):
    groq = FakeGroq()
    groq.fila = [(429, {"error": {"message": "Rate limit reached"}}, {"retry-after": "7"}), groq.resposta_padrao(0)]
    m = _montar(tmp_path, groq=groq)
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is True
    assert sum(m.dormidas) == pytest.approx(7.0)
    assert len(groq.transcricoes()) == 2

    groq2 = FakeGroq()
    groq2.fila = [(429, {"error": {"message": "Rate limit reached"}}, {"retry-after": "900"})]
    m2 = _montar(tmp_path / "b", groq=groq2)
    r2 = await m2.t.transcrever(caminho="files/reuniao.mp4")
    assert r2["ok"] is False and "esperar 900s" in r2["error"]
    # recusado antes de transcrever: a cota volta
    assert (await m2.cota.estado())["audio_hora"]["restante"] == 7200


@pytest.mark.asyncio
async def test_chave_recusada_aponta_para_o_painel(settings, tmp_path):
    groq = FakeGroq()
    groq.fila = [(401, {"error": {"message": "Invalid API Key"}}, {})]
    m = _montar(tmp_path, groq=groq)
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is False and "Configurações → Transcrição" in r["error"]
    assert len(groq.transcricoes()) == 1


@pytest.mark.asyncio
async def test_falha_no_segundo_pedaco_entrega_o_primeiro_como_parcial(settings, tmp_path):
    groq = FakeGroq()
    groq.fila = [groq.resposta_padrao(0), (404, {"error": {"message": "model not found"}}, {})]
    m = _montar(tmp_path, duracao=1500.0, partes=[(0.0, 1200.0), (1200.0, 1500.0)], groq=groq)
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is True and r["parcial"] is True
    assert "INCOMPLETA" in r["aviso"] and "pedaço 2 de 2" in r["aviso"]
    assert r["pedacos"] == "1 de 2"


@pytest.mark.asyncio
async def test_sem_chave_nao_toca_no_sandbox(settings, tmp_path):
    settings.groq_api_key = None
    m = _montar(tmp_path)
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is False and "chave do Groq" in r["error"]
    assert m.sandbox.comandos == []


@pytest.mark.asyncio
async def test_sem_faixa_de_audio(settings, tmp_path):
    m = _montar(tmp_path)
    m.sandbox.sonda["streams"] = [{"codec_type": "video"}]
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is False and "não tem faixa de áudio" in r["error"]


@pytest.mark.asyncio
async def test_arquivo_inexistente_e_caminho_de_fora(settings, tmp_path):
    m = _montar(tmp_path)
    r = await m.t.transcrever(caminho="files/nao-existe.mp4")
    assert r["ok"] is False and "não existe" in r["error"]
    r = await m.t.transcrever(caminho="/Users/daniel/Movies/x.mp4")
    assert r["ok"] is False and "request_user_file" in r["error"]


@pytest.mark.asyncio
async def test_acima_da_duracao_maxima(settings, tmp_path):
    m = _montar(tmp_path, duracao=8000.0, aj=_ajustes(duracao_max_s=7200))
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is False and "acima do máximo" in r["error"]


@pytest.mark.asyncio
async def test_file_id_e_materializado_pelo_catalogo(settings, tmp_path):
    m = _montar(tmp_path)

    class _Store:
        async def stage(self, file_id, *, target="workbench", name=None):
            assert file_id == "abc" and target == "workbench"
            return {"path": "/workspace/files/reuniao.mp4"}

    m.t.get_file_store = lambda: _Store()
    r = await m.t.transcrever(file_id="abc")
    assert r["ok"] is True and r["arquivo_txt"] == "/workspace/files/reuniao.transcricao.txt"


@pytest.mark.asyncio
async def test_pedaco_que_aponta_para_fora_do_workspace_e_ignorado(settings, tmp_path):
    """O sandbox é do modelo: um symlink no lugar do pedaço não faz o core ler fora."""
    m = _montar(tmp_path)
    fora = tmp_path.parent / "segredo.txt"
    fora.write_text("SEGREDO")
    original = m.sandbox.run

    async def run(command, **kw):
        r = await original(command, **kw)
        if "ffmpeg " in command:
            for p in (tmp_path / ".neo" / "transcricao").rglob("parte_000.ogg"):
                p.unlink()
                os.symlink(fora, p)
        return r

    m.sandbox.run = run
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is False
    assert m.groq.transcricoes() == []


@pytest.mark.asyncio
async def test_texto_grande_e_gravado_em_pedacos_e_a_previa_avisa_do_corte(settings, tmp_path):
    groq = FakeGroq()
    frase = "palavra " * 40
    groq.fila = [(200, {"language": "portuguese", "segments": [
        {"start": float(i), "end": float(i) + 1, "text": f"{i} {frase}"} for i in range(400)
    ]}, {})]
    m = _montar(tmp_path, groq=groq, aj=_ajustes(previa_max=1000))
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is True and r["texto_cortado"] is True and len(r["texto"]) == 1000
    txt = (tmp_path / "files" / "reuniao.transcricao.txt").read_text()
    assert txt.count("\n") == 400 and len(txt.encode()) > 60_000
    assert any(c.startswith("cat ") for c in m.sandbox.comandos)
    assert "neo_read_file" in r["nota"]


@pytest.mark.asyncio
async def test_a_cota_e_acertada_pelo_cabecalho_da_resposta(settings, tmp_path):
    groq = FakeGroq()
    groq.restante_hora = 1000
    m = _montar(tmp_path, groq=groq)
    await m.t.transcrever(caminho="files/reuniao.mp4")
    assert (await m.cota.estado())["audio_hora"]["restante"] == 1000


# ── funções puras ──────────────────────────────────────────────────────────


def test_idioma():
    assert _idioma(None, "pt") == ("pt", "")
    assert _idioma("pt-BR", "pt") == ("pt", "")
    assert _idioma("auto", "pt") == (None, "")
    assert _idioma("português", "pt")[1]


def test_pedaco_cabe_no_arquivo_maximo():
    assert _duracao_da_parte(1200, 32, 25) == 1200
    # 128 kbps em 25 MB: ~1339 s cabem; pedido de 7200 cai para isso
    assert _duracao_da_parte(7200, 128, 25) == int(25_000_000 * 0.9 / (16_000 * 1.05))


def test_fatiar_respeita_o_teto_de_bytes_e_nao_perde_nada():
    texto = "".join(f"linha {i} çãõ\n" for i in range(20_000))
    pedacos = _fatiar(texto, 60_000)
    assert "".join(pedacos) == texto
    assert all(len(p.encode()) <= 60_000 for p in pedacos)
    longa = "x" * 200_000
    assert "".join(_fatiar(longa, 60_000)) == longa


def test_srt():
    assert tempo_srt(3723.456) == "01:02:03,456"
    srt = para_srt([{"start": 0, "end": 1.5, "text": " oi "}, {"start": 2, "end": 3, "text": "  "}])
    assert srt == "1\n00:00:00,000 --> 00:00:01,500\noi\n"


# ── fala × silêncio (16/11): a tool confere o próprio áudio ────────────────


@pytest.mark.asyncio
async def test_gravacao_de_tela_sem_som_e_recusada_antes_do_groq(settings, tmp_path):
    m = _montar(tmp_path, duracao=60.0)
    m.sandbox.som = "0.000 1 0.000"  # silence_start em 0 sem silence_end: tudo silêncio
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is False and "quase todo silêncio" in r["error"] and "ocr-video" in r["error"]
    assert r["com_som"] == 0.0
    assert m.groq.transcricoes() == []
    assert not any("libopus" in c for c in m.sandbox.comandos)  # nem extraiu


@pytest.mark.asyncio
async def test_pouco_som_acima_do_limiar_segue(settings, tmp_path):
    m = _montar(tmp_path, duracao=100.0)
    m.sandbox.som = "90.000 0 0.000"  # 10% com som
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is True


@pytest.mark.asyncio
async def test_medicao_ilegivel_nao_barra(settings, tmp_path):
    m = _montar(tmp_path)
    m.sandbox.som = "lixo"
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is True


@pytest.mark.asyncio
async def test_medicao_usa_os_ajustes_e_soma_dentro_do_sandbox(settings, tmp_path):
    m = _montar(tmp_path)
    await m.t.transcrever(caminho="files/reuniao.mp4")
    (medicao,) = [c for c in m.sandbox.comandos if "silencedetect" in c]
    assert "silencedetect=noise=-40dB:d=1" in medicao and "| awk" in medicao and "-vn" in medicao


@pytest.mark.asyncio
async def test_trecho_que_o_whisper_marca_como_nao_fala_sai(settings, tmp_path):
    groq = FakeGroq()
    groq.fila = [(200, {"language": "portuguese", "segments": [
        {"start": 0, "end": 4, "text": " Bom dia.", "no_speech_prob": 0.01, "avg_logprob": -0.2},
        {"start": 4, "end": 9, "text": " Legendas pela comunidade Amara.org",
         "no_speech_prob": 0.92, "avg_logprob": -1.4},
        # no_speech alto, mas confiante: a regra do Whisper mantém
        {"start": 9, "end": 12, "text": " Tchau.", "no_speech_prob": 0.8, "avg_logprob": -0.3},
    ]}, {})]
    m = _montar(tmp_path, groq=groq)
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is True and r["trechos_sem_fala_descartados"] == 1
    assert r["texto"] == "Bom dia.\nTchau.\n"


@pytest.mark.asyncio
async def test_so_nao_fala_vira_erro_que_aponta_o_ocr(settings, tmp_path):
    groq = FakeGroq()
    groq.fila = [(200, {"segments": [
        {"start": 0, "end": 30, "text": " Obrigado por assistir!", "no_speech_prob": 0.95, "avg_logprob": -1.8},
    ]}, {})]
    m = _montar(tmp_path, groq=groq)
    r = await m.t.transcrever(caminho="files/reuniao.mp4")
    assert r["ok"] is False and "não achou fala" in r["error"] and "ocr-video" in r["error"]
