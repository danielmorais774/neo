"""Classificação da resposta HTTP: recusa de origem × erro × conteúdo legítimo.

Antes, `fetch.py` jogava tudo acima de 400 num balde só (`fetch_failed: HTTP
403`) e quem lia não tinha como decidir nada. As três coisas que um 4xx pode
significar pedem reações OPOSTAS, e é essa distinção que o arquivo fixa:

- 403/429 com WAF  → trocar de ORIGEM resolve (máquina do usuário)
- 404/410          → a URL está errada; escalar é queimar minutos à toa
                     (sessão 8c449cb8: um job de browser de 570s por uma
                     página que não existia)
- 5xx/timeout      → retry, e só

O corpo do Akamai abaixo é o REAL, capturado do CDN do portal de dados em 2026-09-20.
"""

import pytest

from modules.web_search.blocking import (
    body_signals,
    cdn_signals,
    classify,
    header_signals,
)

AKAMAI_403 = (
    b"<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY>\n"
    b"<H1>Access Denied</H1>\n \nYou don't have permission to access "
    b"&#34;http&#58;&#47;&#47;cdn&#46;exemplo&#46;gov&#46;br&#47;...&#34; on this server.<P>\n"
    b"Reference&#32;&#35;18&#46;b40d0217&#46;1789932076&#46;2bd5ec5\n"
    b"<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;b40d0217</P>\n"
    b"</BODY>\n</HTML>\n"
)

CLOUDFLARE_200 = (
    b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    b"<body><div id='challenge-platform'>Enable JavaScript and cookies to continue"
    b"</div></body></html>"
)


# ── recusa de origem ──────────────────────────────────────────────────────


def test_akamai_403_do_tse():
    codigo, msg = classify(status=403, headers={"server": "AkamaiGHost"}, body=AKAMAI_403)
    assert codigo == "blocked_origin"
    assert "RECUSOU" in msg
    # A mensagem é lida pelo MODELO: tem que dizer o que fazer.
    assert "ORIGEM" in msg
    assert "navegador do usuário" in msg


def test_cloudflare_disfarcado_de_200():
    """O caso mais traiçoeiro: status de sucesso, conteúdo de desafio."""
    codigo, msg = classify(
        status=200, headers={"cf-mitigated": "challenge"}, body=CLOUDFLARE_200
    )
    assert codigo == "blocked_origin"
    assert "200" in msg and "desafio" in msg


def test_429_conta_como_recusa():
    assert classify(status=429, body=b"slow down")[0] == "blocked_origin"


def test_403_sem_assinatura_ainda_e_recusa_de_origem():
    """Sem corpo de WAF, um 403 continua sendo 'a origem foi recusada' — e o
    navegador do usuário (com os cookies dele) também resolve o caso de login."""
    assert classify(status=403, body=b"")[0] == "blocked_origin"


# ── o que NÃO pode virar escalada ─────────────────────────────────────────


@pytest.mark.parametrize("status", [404, 410])
def test_url_errada_nunca_escala(status):
    codigo, msg = classify(status=status, body=b"<h1>Not Found</h1>")
    assert codigo == "not_found"
    assert "NÃO tente por navegador" in msg


@pytest.mark.parametrize("status", [408, 425, 500, 502, 504])
def test_erro_de_servidor_e_transitorio(status):
    codigo, msg = classify(status=status, body=b"")
    assert codigo == "transient"
    assert "trocar de navegador não muda" in msg


def test_503_sem_assinatura_e_manutencao_nao_bloqueio():
    assert classify(status=503, body=b"<h1>Em manutencao</h1>")[0] == "transient"


def test_503_com_assinatura_e_bloqueio():
    assert classify(status=503, body=CLOUDFLARE_200)[0] == "blocked_origin"


# ── falsos positivos: o erro que faria a feature se voltar contra o projeto ──


def test_pagina_legitima_atras_de_cloudflare_passa():
    """`server: cloudflare` está em metade da web. Se isso classificasse, o Neo
    abriria um navegador para reler toda página que já tinha lido inteira."""
    assert classify(status=200, headers={"server": "cloudflare"}, body=b"<h1>Ola</h1>" * 50) is None


def test_artigo_grande_que_FALA_de_cloudflare_passa():
    """Uma doc explicando 'Just a moment' e 'Access Denied' tem as palavras —
    e centenas de KB. Tamanho é o que separa conteúdo de desafio."""
    artigo = (
        b"<html><body><h1>Como funciona o Just a moment do Cloudflare</h1>"
        + b"<p>Access denied acontece quando o WAF decide bloquear. </p>" * 2000
        + b"</body></html>"
    )
    assert len(artigo) > 25_000
    assert classify(status=200, headers={"server": "cloudflare"}, body=artigo) is None


def test_pagina_normal_passa():
    assert classify(status=200, body=b"<html><body>conteudo de verdade</body></html>") is None


def test_redirect_nao_e_problema():
    assert classify(status=301, body=b"") is None


# ── os detectores isolados ────────────────────────────────────────────────


def test_server_cloudflare_nao_e_sinal_de_mitigacao():
    assert header_signals({"server": "cloudflare"}) == []
    assert cdn_signals({"server": "cloudflare"}) != []


def test_cf_mitigated_e_sinal_de_mitigacao():
    assert header_signals({"cf-mitigated": "challenge"}) != []


def test_assinaturas_do_corpo_nao_repetem():
    sinais = body_signals(b"Access Denied ... Access Denied ... reference #12.abc")
    assert sinais.count("access denied") == 1


# ── arquivo não é página, e não sobe a escada de navegador ────────────────


@pytest.mark.asyncio
async def test_binario_vira_download_nao_falha(monkeypatch):
    """Um .zip acessível é `download`, não `fetch_failed`.

    Enquanto era `fetch_failed`, o modelo lia "falhou" e escalava para
    navegador — o pior jeito de pegar um binário (cai na pasta de downloads,
    sem bytes na mão, nome imprevisível). Medido em 2026-09-20: o ZIP de
    propostas do portal de dados baixa em 18,2 MB com headers coerentes, direto da VPS.
    """
    import httpx
    from modules.web_search import fetch as fetch_mod

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/zip", "content-length": "18275613"}
        content = b"PK\x03\x04" + b"\x00" * 100
        encoding = "utf-8"

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(fetch_mod, "_host_is_private", lambda h: _false())

    res = await fetch_mod.fetch_readable("https://cdn.exemplo.gov.br/x.zip", max_chars=500)
    assert res.error == "download"
    assert "application/zip" in res.message
    assert "17847 KB" in res.message or "KB" in res.message
    assert "NÃO use navegador" in res.message
    assert "file_save" in res.message


async def _false():
    return False


# ── o perfil de headers ───────────────────────────────────────────────────


def test_wikimedia_mantem_o_ua_identificado():
    """A política de uso da Wikimedia pede identificação; medido, os dois
    perfis dão 200 lá — então é cumprir a regra, não necessidade técnica."""
    from modules.web_search.fetch import headers_for

    assert headers_for("pt.wikipedia.org")["User-Agent"].startswith("NeoAssistant")
    assert headers_for("commons.wikimedia.org")["User-Agent"].startswith("NeoAssistant")


def test_resto_da_web_recebe_perfil_de_navegador():
    from modules.web_search.fetch import headers_for

    h = headers_for("dadosabertos.exemplo.gov.br")
    assert h["User-Agent"].startswith("Mozilla/5.0")
    # O que destrava não é o UA sozinho: é o conjunto coerente. Testado header a
    # header contra o CDN do portal de dados, nenhum isolado passa.
    for obrigatorio in ("Accept", "Accept-Language", "Sec-Fetch-Dest",
                        "Sec-Fetch-Mode", "Sec-Fetch-Site", "Upgrade-Insecure-Requests"):
        assert obrigatorio in h, obrigatorio


def test_sufixo_nao_casa_dominio_parecido():
    """`naowikipedia.org` não é a Wikipédia."""
    from modules.web_search.fetch import headers_for

    assert headers_for("naowikipedia.org")["User-Agent"].startswith("Mozilla/5.0")
