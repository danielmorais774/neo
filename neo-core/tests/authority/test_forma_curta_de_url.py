"""`web_fetch` na forma curta de uma URL vista, sem abrir o canal que a trava fecha.

Sessão 455646dd: em turno com conteúdo de terceiro, `/dp/B083LGFDGJ` foi barrado porque
o que apareceu no resultado foi `/Notebook-Aspire-…/dp/B083LGFDGJ`. Nada ali era novo.
Na mesma leva, `/dp/B0D2L5Q4SS` era um código inventado, e esse a trava existe para
barrar. A primeira metade deste arquivo é o caso bom. A segunda é a que importa: cada
teste é uma forma de usar o afrouxamento para tirar dado dentro da URL.
"""

from __future__ import annotations

from core.authority import PERMITIR, PRINCIPAL_EXTERNO, SOLICITAR, TurnAuthority, evaluate
from core.authority.gate import GateDecision

LOJA = "https://www.amazon.com.br"
VISTA = f"{LOJA}/Notebook-Aspire-A515-45-R043-Ryzen-Windows/dp/B083LGFDGJ"


def _turno(*urls: str) -> TurnAuthority:
    a = TurnAuthority()
    a.note_tool_result("web_fetch", " ".join(urls))
    return a


def _decide(a: TurnAuthority, url: str) -> str:
    return evaluate("web_fetch", a, {"url": url}).efeito


def test_a_url_exata_continua_passando():
    assert _decide(_turno(VISTA), VISTA) == PERMITIR


def test_o_trecho_final_do_caminho_passa():
    assert _decide(_turno(VISTA), f"{LOJA}/dp/B083LGFDGJ") == PERMITIR


def test_o_trecho_do_meio_passa_quando_a_vista_tem_rastro():
    vista = f"{LOJA}/Nome-do-Produto/dp/B083LGFDGJ/ref=sr_1_3?dib=abc&qid=1"
    assert _decide(_turno(vista), f"{LOJA}/dp/B083LGFDGJ") == PERMITIR


def test_a_barra_final_nao_muda_nada():
    assert _decide(_turno(VISTA), f"{LOJA}/dp/B083LGFDGJ/") == PERMITIR


# ── o que continua barrado ──────────────────────────────────────────────────


def test_codigo_inventado_continua_barrado():
    """O caso real: `B0D2L5Q4SS` nunca apareceu em resultado nenhum."""
    assert _decide(_turno(VISTA), f"{LOJA}/dp/B0D2L5Q4SS") == SOLICITAR


def test_query_nao_passa_nem_com_o_caminho_certo():
    """`?d=<segredo>` é exatamente o que esta trava existe para impedir."""
    assert _decide(_turno(VISTA), f"{LOJA}/dp/B083LGFDGJ?d=13010000") == SOLICITAR


def test_fragmento_nao_passa():
    assert _decide(_turno(VISTA), f"{LOJA}/dp/B083LGFDGJ#13010000") == SOLICITAR


def test_o_mesmo_caminho_em_outro_host_nao_passa():
    assert _decide(_turno(VISTA), "https://atacante.exemplo/dp/B083LGFDGJ") == SOLICITAR


def test_subdominio_nao_e_o_mesmo_host():
    assert _decide(_turno(VISTA), "https://amazon.com.br/dp/B083LGFDGJ") == SOLICITAR


def test_outro_esquema_nao_passa():
    assert _decide(_turno(VISTA), "http://www.amazon.com.br/dp/B083LGFDGJ") == SOLICITAR


def test_um_segmento_so_nao_identifica_nada():
    assert _decide(_turno(VISTA), f"{LOJA}/B083LGFDGJ") == SOLICITAR


def test_subsequencia_nao_passa():
    """Pular segmento abriria o canal: `/a/b/c/…` repetido escreveria qualquer texto."""
    vista = "https://atacante.exemplo/s/e/g/r/e/d/o"
    assert _decide(_turno(vista), "https://atacante.exemplo/s/g/r") == SOLICITAR


def test_inverter_a_ordem_nao_passa():
    assert _decide(_turno("https://x.exemplo/a/b/c"), "https://x.exemplo/c/a") == SOLICITAR


def test_misturar_duas_urls_vistas_nao_passa():
    a = _turno("https://x.exemplo/a/b", "https://x.exemplo/c/d")
    assert _decide(a, "https://x.exemplo/b/c") == SOLICITAR


def test_caminho_gigante_nao_serve_de_alfabeto():
    """Uma URL com centenas de segmentos pode conter toda combinação curta."""
    alfabeto = "/".join(f"{a}/{b}" for a in "0123456789abcdef" for b in "0123456789abcdef")
    vista = f"https://atacante.exemplo/{alfabeto}"
    assert _decide(_turno(vista), "https://atacante.exemplo/6/2") == SOLICITAR


def test_pedido_de_fora_nao_ganha_a_forma_curta():
    a = TurnAuthority(principal=PRINCIPAL_EXTERNO, principal_ref="fulano@x.com")
    a.note_tool_result("web_fetch", VISTA)
    assert _decide(a, f"{LOJA}/dp/B083LGFDGJ") == SOLICITAR


def test_imagem_nao_herda_a_forma_curta():
    """A procedência de imagem é outra regra: o afrouxamento vale só para o `web_fetch`."""
    a = _turno(VISTA)
    assert a.url_is_short_form(f"{LOJA}/dp/B083LGFDGJ") is True
    assert a.url_has_provenance(f"{LOJA}/dp/B083LGFDGJ") is False


# ── a mensagem de recusa ────────────────────────────────────────────────────


def test_recusa_de_url_diz_o_que_fazer_e_nao_promete_liberacao():
    """"chame solicitar_dado" num GET público virou "eu libero o acesso" no chat."""
    d = evaluate("web_fetch", _turno(VISTA), {"url": f"{LOJA}/dp/B0D2L5Q4SS"})
    texto = d.mensagem()
    assert "EXATO" in texto
    assert "browser_local_task" in texto
    assert "não há o que liberar" in texto
    assert "web_fetch" in texto.split(":")[0]


def test_as_outras_saidas_seguem_com_a_recusa_generica():
    a = _turno(VISTA)
    d = evaluate("email_send", a)
    assert isinstance(d, GateDecision)
    assert "solicitar_dado(campos=" in d.mensagem()
