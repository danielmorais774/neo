"""Skill ocr-video — a lógica pura do script (sem ffmpeg/tesseract).

O que dá errado numa transcrição de chat rolando não é o OCR, é a junção:
- a mesma mensagem lida em 3 quadros sai 3 vezes;
- o rodapé fixo ("Digite uma mensagem") casa no fim de todo quadro e, usado como
  âncora, engole as mensagens novas que vieram antes dele;
- um hash perceptual de 64 bits acha que a tela rolada é "a mesma" e joga o
  quadro fora.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "ocr-video" / "scripts" / "ocr_video.py"
_spec = importlib.util.spec_from_file_location("ocr_video", _SCRIPT)
ocr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ocr)


def _conversa(n: int) -> list[str]:
    return [f"Maria Souza 14:{i:02d} mensagem numero {i} sobre o contrato" for i in range(n)]


def _textos(saida: list[dict]) -> list[str]:
    return [item["texto"] for item in saida]


def test_scroll_down_keeps_each_line_once_and_in_order():
    linhas = _conversa(20)
    quadros = [(0.0, linhas[0:8]), (1.0, linhas[5:13]), (2.0, linhas[10:18]), (3.0, linhas[15:20])]
    assert _textos(ocr.merge_frames(quadros)) == linhas


def test_ocr_noise_between_frames_does_not_duplicate():
    linhas = _conversa(10)
    # Ruído de LETRA (o/a trocados): o que o tesseract erra numa tela limpa.
    ruidoso = [l.replace("contrato", "cantrato") for l in linhas[3:10]]
    saida = _textos(ocr.merge_frames([(0.0, linhas[0:6]), (1.0, ruidoso)]))
    assert len(saida) == 10
    assert saida[:6] == linhas[:6]


def test_fixed_footer_does_not_swallow_new_messages():
    linhas = _conversa(12)
    rodape = "Digite uma mensagem"
    quadros = [
        (0.0, linhas[0:6] + [rodape]),
        (1.0, linhas[4:10] + [rodape]),
        (2.0, linhas[8:12] + [rodape]),
    ]
    saida = _textos(ocr.merge_frames(quadros))
    for linha in linhas:
        assert saida.count(linha) == 1, linha
    assert saida.count(rodape) == 1


def test_chrome_detection_removes_header_and_footer_everywhere():
    linhas = _conversa(30)
    quadros_linhas = [["Conversa com Maria"] + linhas[i:i + 6] + ["Digite uma mensagem"] for i in range(0, 24, 4)]
    chrome = ocr.find_chrome(quadros_linhas)
    assert set(chrome.values()) == {"Conversa com Maria", "Digite uma mensagem"}
    saida = _textos(ocr.merge_frames([(float(i), l) for i, l in enumerate(quadros_linhas)], chrome))
    assert "Conversa com Maria" not in saida and "Digite uma mensagem" not in saida
    assert saida == linhas[0:26]


def test_chrome_needs_enough_frames_to_decide():
    assert ocr.find_chrome([["a b c d"], ["a b c d"], ["a b c d"]]) == {}


def test_short_repeated_messages_are_kept():
    # "ok" aparece duas vezes de verdade: linha curta não é deduplicada nem ancora.
    quadros = [
        (0.0, ["Daniel 10:00 vamos fechar amanha cedo", "ok"]),
        (1.0, ["ok", "Maria 10:02 combinado entao ate amanha", "ok"]),
    ]
    saida = _textos(ocr.merge_frames(quadros))
    assert saida.count("ok") >= 2
    assert "Maria 10:02 combinado entao ate amanha" in saida


def test_scroll_up_reveals_older_lines_before_the_overlap():
    linhas = _conversa(12)
    saida = _textos(ocr.merge_frames([(0.0, linhas[6:12]), (1.0, linhas[2:8])]))
    assert saida == linhas[2:12]


def test_timestamp_is_the_first_frame_where_the_line_appeared():
    linhas = _conversa(6)
    saida = ocr.merge_frames([(0.0, linhas[0:4]), (7.0, linhas[2:6])])
    assert [item["t"] for item in saida] == [0.0, 0.0, 0.0, 0.0, 7.0, 7.0]


def test_tsv_groups_words_by_line_and_drops_low_confidence():
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [
        "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t91\tMaria",
        "5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t88\tSouza",
        "5\t1\t1\t1\t2\t1\t0\t0\t10\t10\t12\t@#",
        "4\t1\t1\t1\t2\t0\t0\t0\t10\t10\t-1\t",
        "5\t1\t2\t1\t1\t1\t0\t0\t10\t10\t80\tOlá,",
        "5\t1\t2\t1\t1\t2\t0\t0\t10\t10\t77\ttudo",
        "5\t1\t2\t1\t1\t3\t0\t0\t10\t10\t75\tbem?",
    ]
    assert ocr.tsv_to_lines("\n".join([header, *rows])) == ["Maria Souza", "Olá, tudo bem?"]


def test_filter_crops_before_scaling_and_never_upscales():
    assert ocr.build_filter(1, 1600, (10, 20, 300, 400)) == (
        "crop=300:400:10:20,fps=1,scale='min(iw,1600)':-2,format=gray"
    )
    assert ocr.build_filter(0.5, 1600, None).startswith("fps=0.5,")


@pytest.mark.parametrize("raw", ["1,2,3", "a,b,c,d", "0,0,0,10"])
def test_parse_crop_rejects_bad_input(raw):
    with pytest.raises(ValueError):
        ocr.parse_crop(raw)


def test_distinct_frames_keeps_scrolled_text_and_drops_static_frames():
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(3)
    tela = rng.integers(0, 255, size=(104, 160)).astype(np.int16)
    rolada = np.roll(tela, -6, axis=0)
    ruido = tela + rng.integers(-1, 2, size=tela.shape)
    assert ocr.distinct_frames([tela, ruido, tela, rolada, rolada]) == [0, 3]


def test_lines_that_differ_only_in_digits_are_different_messages():
    # "14:07" x "14:08" é um caractere em quarenta: pela razão de semelhança seriam
    # a mesma linha, e a mensagem nova sumiria da transcrição.
    a = "Maria Souza 14:07 mensagem sobre o contrato"
    b = "Maria Souza 14:08 mensagem sobre o contrato"
    assert not ocr.similar(ocr.norm_key(a), ocr.norm_key(b))
    assert _textos(ocr.merge_frames([(0.0, [a]), (1.0, [a, b])])) == [a, b]


def test_line_seen_in_a_single_middle_frame_is_dropped_as_edge_cut():
    # Rolagem lenta (10 linhas visíveis, 2 novas por quadro): a linha típica
    # aparece em 3+ quadros, então o filtro está ligado.
    linhas = _conversa(20)
    quadros = [(float(i), linhas[2 * i: 2 * i + 10]) for i in range(6)]
    quadros[2] = (2.0, quadros[2][1] + ["Nanial Morais 14:na"])
    saida = ocr.merge_frames(quadros)
    mantidas, descartadas, suporte = ocr.drop_single_frame_lines(saida, quadros)
    assert suporte >= ocr.ROLAGEM_LENTA_MIN_SUPORTE
    assert [d["texto"] for d in descartadas] == ["Nanial Morais 14:na"]
    assert _textos(mantidas) == linhas


def test_fast_scroll_turns_the_support_filter_off_instead_of_losing_lines():
    # A 1 quadro/s numa rolagem rápida, linha REAL aparece num quadro só: o filtro
    # não pode descartá-la, e o suporte baixo vira o aviso de rodar com mais fps.
    linhas = _conversa(8)
    quadros = [(0.0, linhas[0:3]), (1.0, linhas[2:5]), (2.0, linhas[4:8])]
    saida = ocr.merge_frames(quadros)
    mantidas, descartadas, suporte = ocr.drop_single_frame_lines(saida, quadros)
    assert descartadas == []
    assert _textos(mantidas) == linhas
    assert 0 < suporte < ocr.ROLAGEM_LENTA_MIN_SUPORTE


def test_long_message_that_really_repeats_is_not_swallowed():
    # A mesma frase longa enviada duas vezes, com outra mensagem no meio: rolando
    # para baixo, a segunda cópia está DEPOIS do trecho em comum e é conteúdo novo.
    repetida = "Esta mensagem foi apagada pelo remetente da conversa"
    conversa = [
        "Maria 10:00 bom dia pessoal tudo certo por ai",
        repetida,
        "Daniel 10:01 sim tudo certo e com voces por aqui",
        "Maria 10:02 tudo otimo tambem obrigada",
        repetida,
        "Daniel 10:03 vou mandar o arquivo agora mesmo",
    ]
    quadros = [(0.0, conversa[0:4]), (1.0, conversa[2:6])]
    assert _textos(ocr.merge_frames(quadros)) == conversa
