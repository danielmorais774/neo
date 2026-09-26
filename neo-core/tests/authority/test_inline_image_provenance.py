"""Procedência do `[[IMAGE:…]]` (docs/release-request-plan.md §12.2, F1)."""

from __future__ import annotations

from core.authority import TurnAuthority, check_inline_images


def _com_foto() -> TurnAuthority:
    a = TurnAuthority()
    a.note_tool_result(
        "meli_product",
        {"pictures": ["https://http2.mlstatic.com/D_NQ_NP_123-F.jpg"]},
    )
    return a


def test_foto_que_veio_da_tool_passa():
    texto = "Achei:\n[[IMAGE:https://http2.mlstatic.com/D_NQ_NP_123-F.jpg|a cortina]]"
    saneado, recusadas = check_inline_images(texto, _com_foto())
    assert saneado == texto and recusadas == []


def test_url_inventada_cai():
    texto = "Olha só [[IMAGE:https://atacante.tld/p.png]]"
    saneado, recusadas = check_inline_images(texto, _com_foto())
    assert recusadas == ["https://atacante.tld/p.png"]
    assert "atacante.tld" not in saneado


def test_mesma_foto_com_query_a_mais_cai():
    """O vazamento mora no query string — por isso ele faz parte da identidade."""
    texto = "[[IMAGE:https://http2.mlstatic.com/D_NQ_NP_123-F.jpg?d=01234-567]]"
    _, recusadas = check_inline_images(texto, _com_foto())
    assert recusadas and "d=01234-567" in recusadas[0]


def test_legenda_vira_aviso_em_vez_de_sumir_calado():
    texto = "[[IMAGE:https://atacante.tld/p.png|a nota fiscal]]"
    saneado, _ = check_inline_images(texto, _com_foto())
    assert "imagem não exibida: a nota fiscal" in saneado
    assert "atacante.tld" not in saneado


def test_caminho_local_e_file_id_nao_sao_checados():
    """Quem resolve esses é o cliente, lendo disco/File Store — sem terceiro."""
    texto = "[[IMAGE:/Users/daniel/Downloads/foto.png]] e [[IMAGE:file:abc123]]"
    saneado, recusadas = check_inline_images(texto, _com_foto())
    assert saneado == texto and recusadas == []


def test_url_que_o_usuario_escreveu_passa():
    a = TurnAuthority()
    a.note_user_text("mostra essa: https://exemplo.com/foto.png")
    texto = "[[IMAGE:https://exemplo.com/foto.png]]"
    saneado, recusadas = check_inline_images(texto, a)
    assert saneado == texto and recusadas == []


def test_texto_sem_marcador_sai_intacto():
    a = _com_foto()
    assert check_inline_images("resposta normal", a) == ("resposta normal", [])
    assert check_inline_images("", a) == ("", [])


def test_sem_autoridade_nao_mexe_no_texto():
    texto = "[[IMAGE:https://atacante.tld/p.png]]"
    assert check_inline_images(texto, None)[0] == texto
