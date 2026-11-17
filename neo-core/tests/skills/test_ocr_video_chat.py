"""Skill ocr-video, modo chat — geometria dos balões e montagem da conversa.

Sem ffmpeg/tesseract: quadros sintéticos desenhados com numpy. O que já quebrou no
vídeo real (WhatsApp do macOS, tema escuro, tela Retina) e fica preso aqui:
- a miniatura de vídeo ocupa o balão inteiro, sem moldura da cor dele, e não era
  achada; e a pilha de balões de larguras diferentes virava "[mídia]" e engolia as
  mensagens de dentro;
- separador de data e aviso do sistema são texto sobre o FUNDO, fora da máscara de cor;
- o botão fixo "rolar para baixo" fatiava um balão de uma linha em dois pedaços;
- o recorte fixo do canto pegava o fim da frase, e o horário saía errado;
- sem a data separando os dias, "o horário só cresce" empurrava 20:06 para 08:xx;
- "[vídeo 1:13]" e "Dá até pra ver … vídeo" viravam a mesma mensagem.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

_SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "ocr-video" / "scripts" / "chat_ocr.py"
_spec = importlib.util.spec_from_file_location("chat_ocr", _SCRIPT)
chat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chat)

FUNDO = (10, 10, 10)
ENTRADA = (32, 33, 35)
SAIDA = (21, 68, 52)
TINTA = (230, 230, 230)
W, H = 2192, 1820


def _tela():
    return np.zeros((H, W, 3), dtype=np.int32) + np.array(FUNDO)


def _texto(img, x0, x1, y, alto=26, passo=18):
    """Uma linha de "texto": traços verticais finos (a abertura apaga, a tinta conta)."""
    for x in range(x0, x1, passo):
        img[y:y + alto, x:x + 3] = TINTA


def _balao(img, x0, y0, x1, y1, cor, *, canto=8, texto=True):
    img[y0:y1, x0:x1] = cor
    for ys in (slice(y0, y0 + canto), slice(y1 - canto, y1)):  # cantos arredondados
        img[ys, x0:x0 + canto] = FUNDO
        img[ys, x1 - canto:x1] = FUNDO
    if texto:
        for y in range(y0 + 18, y1 - 30, 40):
            _texto(img, x0 + 20, x1 - 140, y)


def _quadro_de_conversa():
    img = _tela()
    _balao(img, 20, 200, 900, 262, ENTRADA)
    _balao(img, 20, 262, 900, 364, ENTRADA)  # colado no de cima: só o recuo dos cantos separa
    _balao(img, 1300, 420, 2186, 482, SAIDA)
    ys, xs = np.mgrid[540:1100, 1086:2183]
    v = 60 + (ys - 540) * 60 // 560 + xs % 7
    img[540:1100, 1086:2183] = np.stack([v, v + 3, v + 1], axis=-1)  # miniatura de vídeo, sem moldura
    _balao(img, 1500, 1150, 2186, 1330, SAIDA, texto=False)
    img[1160:1230, 1514:2172] = (15, 45, 35)  # caixa da citação, de outra cor
    _texto(img, 1530, 1900, 1255)
    _balao(img, 20, 1400, 1000, 1500, ENTRADA)
    _texto(img, 1030, 1160, 1560)  # "quarta-feira" direto sobre o fundo
    _balao(img, 20, 1640, 700, 1702, ENTRADA)
    return img


def test_segmentacao_de_um_quadro_de_conversa():
    img = _quadro_de_conversa()
    cores = chat.analisar_cores(img)
    assert cores is not None
    assert cores["u"] == 1.0
    bolhas = chat.segmentar_quadro(img, cores)
    assert [b["kind"] for b in bolhas] == [
        "entrada", "entrada", "saida", "midia_saida", "saida", "entrada", "centro", "entrada",
    ]
    esperadas = [
        [20, 200, 900, 262], [20, 262, 900, 364], [1300, 420, 2186, 482], [1086, 540, 2183, 1100],
        [1500, 1150, 2186, 1330], [20, 1400, 1000, 1500], None, [20, 1640, 700, 1702],
    ]
    for bolha, esperada in zip(bolhas, esperadas):
        if esperada:
            assert all(abs(a - b) <= 2 for a, b in zip(bolha["box"], esperada)), (bolha["box"], esperada)
    assert bolhas[4]["citacao"] == [10, 80]
    assert bolhas[0]["citacao"] is None
    x0, y0, x1, y1 = bolhas[6]["box"]
    assert x0 < 1030 and x1 > 1160 and y0 < 1560 and y1 > 1586


def test_citacao_e_botao_com_tons_proximos_ao_balao():
    """Medido no WhatsApp: a caixa da citação fica a ~10 do verde do balão e o botão
    de resposta rápida a ~14 do cinza. Com a máscara de cor em 20 a citação sumia no
    corpo; em 10 o botão perdia metade do preenchimento e deixava de ser botão."""
    img = _tela()
    _balao(img, 1500, 300, 2186, 480, SAIDA, texto=False)
    img[310:380, 1514:2172] = (15, 56, 40)  # a 12 de SAIDA, fora da paleta
    _texto(img, 1530, 1900, 405)
    _balao(img, 20, 600, 489, 662, ENTRADA)
    _balao(img, 20, 664, 489, 750, (45, 46, 46), texto=False)  # botão, a 13 de ENTRADA
    _texto(img, 200, 300, 690)
    cores = {"paleta": [list(FUNDO), list(ENTRADA), list(SAIDA), list(TINTA)], "grupo_entrada": [1],
             "grupo_saida": [2], "entrada": list(ENTRADA), "saida": list(SAIDA), "bgl": 10.0, "u": 1.0}
    bolhas = chat.segmentar_quadro(img, cores)
    saida = [b for b in bolhas if b["kind"] == "saida"]
    assert len(saida) == 1
    assert saida[0]["citacao"] == [10, 80]
    entrada = [b for b in bolhas if b["kind"] == "entrada"]
    assert len(entrada) == 2
    assert entrada[1]["preench"] >= 0.9


def test_tela_sem_balao_nao_e_conversa():
    img = _tela()
    for y in range(200, 1600, 40):
        _texto(img, 100, 2000, y)
    assert chat.analisar_cores(img) is None


def test_painel_e_a_regiao_que_muda():
    act = np.zeros((200, 300), dtype=np.float32)
    act[20:180, 120:290] = 5.0
    act[5:10, 0:40] = 1.0  # relógio da barra de menu mudando
    assert chat.painel(act) == (120, 20, 290, 180)


def test_rolagem_medida_pelo_perfil_das_linhas():
    rng = np.random.default_rng(0)
    conteudo = (rng.random((900, 400)) > 0.97).astype(np.float32) * 200
    a, b = conteudo[0:500], conteudo[120:620]
    assert chat.best_shift(chat.row_profiles(a, 0.0), chat.row_profiles(b, 0.0), -300, 300)[0] == 120


def test_components_separa_regioes_e_conta_area():
    m = np.zeros((50, 50), dtype=bool)
    m[5:10, 5:20] = True
    m[10:20, 5:8] = True  # perna do L, grudada
    m[30:40, 30:35] = True
    assert sorted(chat.components(m)) == [(5, 5, 20, 20, 105), (30, 30, 35, 40, 50)]


def test_abertura_apaga_traco_fino_e_mantem_bloco():
    m = np.zeros((60, 60), dtype=bool)
    m[10:40, 10:40] = True
    m[50:52, 0:60] = True
    aberto = chat.box_dilate(chat.box_erode(m, 3), 3)
    assert aberto[10:40, 10:40].all()
    assert not aberto[50:52].any()


def test_split_separa_baloes_colados_pelo_recuo_dos_cantos():
    m = np.zeros((300, 600), dtype=bool)
    m[0:220, 0:500] = True
    m[92:108, 0:8] = False
    m[92:108, 492:500] = False
    assert chat.split_by_notches(m, 0, 220, 0, 500) == [(0, 100), (100, 220)]


def test_split_nao_fatia_balao_de_uma_linha_tapado_pelo_botao():
    m = np.zeros((70, 1300), dtype=bool)
    m[0:62, 0:1230] = True
    m[36:44, 1200:1230] = False  # borda direita recortada pelo botão fixo
    assert chat.split_by_notches(m, 0, 62, 0, 1230) == [(0, 62)]


def test_moldura_de_anexo_e_pilha_de_baloes():
    anexo = np.zeros((400, 600), dtype=bool)
    anexo[0:8, :] = anexo[-8:, :] = True
    anexo[:, 0:8] = anexo[:, -8:] = True
    assert chat.moldura(anexo, 0, 0, 600, 400)
    pilha = np.zeros((400, 600), dtype=bool)
    pilha[0:60, 0:600] = True
    pilha[70:130, 0:250] = True
    pilha[140:400, 0:400] = True
    assert not chat.moldura(pilha, 0, 0, 600, 400)


def test_texto_centralizado_sobre_o_fundo():
    img = _tela()
    _texto(img, 1030, 1160, 900)  # separador de data
    _texto(img, 40, 400, 1200)  # encostado à esquerda: não
    _texto(img, 1030, 1160, 40)  # rótulo flutuante do topo: não
    _texto(img, 1030, 1160, 1500)  # dentro de um balão: não
    caixas = chat.centralizados(chat.luma(img), 9.0, [{"box": [900, 1480, 1300, 1560]}], W, H)
    assert len(caixas) == 1
    x0, y0, x1, y1 = caixas[0]
    assert y0 <= 900 and y1 >= 926 and x0 <= 1030 and x1 >= 1160


def test_bloco_por_cima_da_conversa_tapa_o_que_cruza():
    outros = [{"kind": "outro", "box": [1500, 100 + i * 150, 2100, 200 + i * 150]} for i in range(3)]
    entrada = {"kind": "entrada", "box": [20, 100, 600, 160]}
    tampa = chat.cobertura(outros + [entrada])
    assert tampa is not None
    assert chat.cruza([1600, 300, 2186, 360], tampa)
    assert not chat.cruza(entrada["box"], tampa)
    assert chat.cobertura(outros[:2] + [entrada]) is None


def test_local_hora_pega_so_o_grupo_do_horario():
    reg = np.zeros((64, 500, 3), dtype=np.int32) + np.array(ENTRADA)
    reg[30:52, 10:260:9] = TINTA  # fim da frase: letras próximas
    reg[30:52, 300:372:8] = TINTA  # horário, depois de um vão largo
    reg[40:60, 496:498] = TINTA  # borda arredondada do balão
    a, _r0, b, _r1 = chat.local_hora(reg, ENTRADA)
    assert (a, b) == (300, 365)
    so_frase = np.zeros((64, 500, 3), dtype=np.int32) + np.array(ENTRADA)
    so_frase[30:52, 10:400:9] = TINTA
    assert chat.local_hora(so_frase, ENTRADA) is None


def _cand(k, G, box, lado="entrada", corte=False, preench=0.85):
    return {"kind": lado, "cor": lado, "box": box, "citacao": None, "preench": preench, "corte": corte,
            "k": k, "lado": lado, "gy0": G + box[1], "gy1": G + box[3]}


def test_mesmo_balao_em_varios_quadros_vira_um_grupo_lido_no_melhor_exemplar():
    grupos = chat.agrupar([
        _cand(0, 0, [20, 1780, 900, 1820], corte=True),  # cortado na borda de baixo
        _cand(1, 900, [20, 880, 900, 980]),  # inteiro
        _cand(2, 1500, [20, 280, 880, 380]),  # inteiro, borda direita tapada
        _cand(1, 900, [1300, 900, 2186, 962], lado="saida"),
    ])
    assert len(grupos) == 2
    g = next(g for g in grupos if g["lado"] == "entrada")
    assert len(g["membros"]) == 3
    assert g["rep"]["k"] == 1


def test_botao_colado_embaixo_do_balao_de_entrada():
    grupos = chat.agrupar([
        _cand(0, 0, [20, 900, 489, 966]),
        _cand(0, 0, [20, 968, 489, 1054], preench=0.98),
        _cand(0, 0, [13, 1056, 489, 1142], preench=0.95),
        _cand(0, 0, [20, 1150, 489, 1212]),  # mensagem comum de uma linha
    ])
    chat.marcar_botoes(grupos)
    assert [bool(g.get("botao")) for g in sorted(grupos, key=lambda g: g["gy0"])] == [False, True, True, False]


def _msg(hora, votos=2):
    return {"tipo": "entrada", "hora": hora, "votos": votos, "leituras": [hora] * votos}


def test_horario_so_cresce_dentro_do_dia_e_a_data_separa_os_dias():
    itens = [
        _msg("20:06"), _msg("20:03", votos=1), _msg("20:09"),
        {"tipo": "data", "texto": "quarta-feira"},
        _msg("08:35"), _msg("05:37", votos=1), _msg("08:42"),
    ]
    chat.reparar_horas(itens)
    assert [i.get("hora") for i in itens] == ["20:06", "20:08", "20:09", None, "08:35", "08:37", "08:42"]
    assert itens[1].get("reparada") and not itens[0].get("reparada")


def test_horario_e_data_lidos_do_texto():
    assert chat.hora_do_texto("Boa noite 20:06") == "20:06"
    assert chat.hora_do_texto("não é normal 09:4O") == "09:40"
    assert chat.hora_do_texto("Selecione entre as seguintes opções.") is None
    assert chat.eh_data("quarta-feira") and chat.eh_data("Terça-feira") and chat.eh_data("8 de setembro de 2026")
    assert not chat.eh_data("Esta empresa agora usa um serviço seguro da Meta")


def _lido(lado, gy0, gy1, texto, *, hora=None, votos=0, kind=None, botao=False, n=2):
    rep = {"kind": kind or lado, "k": 0}
    g = {"lado": lado, "gy0": gy0, "gy1": gy1, "rep": rep, "membros": [rep] * n}
    lido = {"g": g, "texto": texto, "hora": hora, "votos": votos, "leituras": [hora] * votos if hora else [],
            "cortada": False, "citacao": None}
    if botao:
        lido["botao"] = True
    return lido


def test_montagem_da_conversa():
    itens, descartes = chat.montar([
        _lido("centro", 130, 180, "quarta-feira"),  # rótulo flutuante: nenhuma mensagem até a próxima data
        _lido("centro", 200, 300, "8 As mensagens desta conversa são protegidas"),  # cadeado lido como "8"
        _lido("centro", 880, 920, "terça-feira"),
        _lido("entrada", 950, 1016, "Seu problema foi resolvido?", hora="20:08", votos=3),
        _lido("entrada", 1018, 1104, "a Sim", botao=True),
        _lido("entrada", 1106, 1192, "a Não", botao=True),
        _lido("centro", 1300, 1340, "quarta-feira"),
        _lido("saida", 1400, 1462, "bom dia sunorte", hora="08:35", votos=2),
        _lido("saida", 1400, 1470, "bom dia, suporte", hora="08:35", votos=1),  # o mesmo balão, relido
        _lido("saida", 1500, 2100, "[vídeo 1:13]", hora="08:53", votos=4, kind="midia_saida"),
        _lido("saida", 2150, 2212, "Dá até pra ver pelo frame ghost que eu estava só vendo vídeo", hora="08:58", votos=3),
        _lido("centro", 2150, 2212, "até pra ver pelo frame ghost que eu estava", n=1),  # balão tapado lido como centro
        _lido("entrada", 2300, 2340, "|| — »", n=1),
        _lido("saida", 2400, 2424, "a . .. ta as ava º a a a a", n=1),  # balão esmaecendo na borda
    ])
    assert [(i["tipo"], i["texto"]) for i in itens] == [
        ("aviso", "As mensagens desta conversa são protegidas"),
        ("data", "terça-feira"),
        ("entrada", "Seu problema foi resolvido?"),
        ("data", "quarta-feira"),
        ("saida", "bom dia, suporte"),
        ("saida", "[vídeo 1:13]"),
        ("saida", "Dá até pra ver pelo frame ghost que eu estava só vendo vídeo"),
    ]
    assert itens[2]["botoes"] == ["Sim", "Não"]
    assert itens[4]["hora"] == "08:35"
    assert "|| — »" in descartes
    assert "a . .. ta as ava º a a a a" in descartes


def test_formato_de_saida():
    itens = [
        {"tipo": "data", "texto": "terça-feira", "k": 0},
        {"tipo": "saida", "texto": "Status da garantia\nStatus ou atualização da garantia", "hora": "20:07",
         "votos": 5, "citacao": ["Deli Support - WA", "Opções de atualização"], "cortada": False, "k": 1},
        {"tipo": "entrada", "texto": "Seu problema foi resolvido?", "hora": None, "votos": 0, "citacao": None,
         "cortada": True, "botoes": ["Sim", "Não"], "k": 1},
    ]
    texto, registros = chat.formatar(itens, "Dell Support - WA", [0.0, 12.5])
    assert texto == (
        "== terça-feira ==\n"
        '[20:07] Você (respondendo a "Opções de atualização"): Status da garantia\n'
        "  Status ou atualização da garantia\n"
        '[??:??] Dell Support - WA [cortada em "Ler mais"]: Seu problema foi resolvido?\n'
        "  [botões: Sim / Não]\n"
    )
    assert registros[1]["de"] == "voce" and registros[1]["hora_lida"] and registros[1]["t_video"] == "00:12"
    assert registros[2]["hora"] is None and not registros[2]["hora_lida"]
