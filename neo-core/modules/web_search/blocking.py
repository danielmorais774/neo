"""Classifica a RESPOSTA HTTP: o site recusou a origem, ou só falhou?

Sem isso, `fetch.py` jogava tudo acima de 400 num balde só
(``fetch_failed: HTTP 403``), e quem lia não tinha como decidir nada. As três
coisas que um 4xx pode significar pedem reações opostas:

- **403/429 com assinatura de WAF** → a origem foi recusada. Repetir daqui é
  perder tempo; o que muda o resultado é trocar de origem (a máquina do usuário,
  IP residencial) — ou headers coerentes, que já resolvem boa parte.
- **404/410** → a URL está errada. Escalar para navegador aqui é queimar 570s
  por uma página que não existe (sessão 8c449cb8).
- **5xx/timeout** → transitório. Retry resolve; escalar, não.

O caso mais traiçoeiro NÃO é nenhum 4xx: é **HTTP 200 com interstitial**. O
Cloudflare devolve "Just a moment…" e o Akamai devolve "Access Denied" com
status 200 em algumas configurações — parece sucesso, e o texto extraído vem
curto ou vazio. Por isso a classificação olha corpo e cabeçalho, não só status.

Medido em 2026-09-20 contra o CDN do portal de dados (Akamai/EdgeSuite):

    VPS  + UA de bot            → 403  (corpo de 475 bytes com "Reference #")
    VPS  + headers coerentes    → 200  na página do dataset; 403 no CDN do .zip
    Mac  + headers coerentes    → 200  no CDN do .zip (18,2 MB), 3/3

Ou seja: headers coerentes já destravam parte, e a ORIGEM destrava o resto.
"""

from __future__ import annotations

import re

#: Assinaturas no CORPO. Cobrem os quatro WAF que aparecem na web BR.
_BODY_SIGNATURES = (
    # Akamai / EdgeSuite — o do portal de dados, gov.br e bancos.
    r"errors\.edgesuite\.net",
    r"reference\s*#\s*\d+\.",
    r"\bakamai\b",
    # Cloudflare — o interstitial de 200 mais comum.
    r"just a moment",
    r"attention required!\s*\|\s*cloudflare",
    r"__cf_chl",
    r"challenge-platform",
    r"cf-browser-verification",
    # Imperva/Incapsula, PerimeterX, DataDome.
    r"request unsuccessful\.\s*incapsula",
    r"_incapsula_resource",
    r"px-captcha",
    r"\bdatadome\b",
    # Genéricos de anti-bot.
    r"pardon our interruption",
    r"unusual traffic",
    r"tráfego incomum",
    r"are you a (?:human|robot)",
    r"verify (?:you are|you're) (?:a )?human",
    r"enable javascript and cookies to continue",
    r"access denied",
    r"acesso negado",
)
_BODY_RE = re.compile("|".join(_BODY_SIGNATURES), re.I)

#: Cabeçalhos que provam MITIGAÇÃO — o WAF agiu contra esta requisição.
#: `cf-mitigated` só aparece quando o Cloudflare desafiou; os outros idem.
_HEADER_MITIGATION = (
    ("cf-mitigated", None),
    ("x-datadome", re.compile(r"protected|blocked|challenge", re.I)),
    ("x-iinfo", None),            # Incapsula
)

#: Cabeçalhos que só dizem QUEM SERVE a página, não que ela foi barrada.
#:
#: ⚠️ `server: cloudflare` está em metade da web. Tratar isso como sinal de
#: bloqueio classificaria como "bloqueada" toda página atrás de CDN — o Neo
#: abriria um navegador para reler uma página que já tinha lido inteira. Fica
#: aqui só para ENRIQUECER a mensagem quando já houve bloqueio; nunca dispara
#: nada sozinho.
_HEADER_CDN = (
    ("server", re.compile(r"cloudflare|akamai|awselb", re.I)),
)

#: Status que NUNCA sobem a escada: a URL está errada, ponto.
_NOT_FOUND = frozenset({404, 410})
#: Status que pedem retry, não escalada.
_TRANSIENT = frozenset({408, 425, 500, 502, 504})


def _body_text(body: bytes | str | None, limit: int = 8000) -> str:
    if not body:
        return ""
    if isinstance(body, bytes):
        return body[:limit].decode("utf-8", errors="replace")
    return body[:limit]


def _casa(headers: dict | None, regras) -> list[str]:
    if not headers:
        return []
    # httpx.Headers é case-insensitive, dict cru não é — normaliza.
    plano = {str(k).lower(): str(v) for k, v in dict(headers).items()}
    achados = []
    for nome, padrao in regras:
        valor = plano.get(nome)
        if valor is None:
            continue
        if padrao is None or padrao.search(valor):
            achados.append(f"{nome}: {valor[:40]}")
    return achados


def header_signals(headers: dict | None) -> list[str]:
    """Cabeçalhos que provam que o WAF AGIU (vazio = não provam nada).

    Não inclui `server: cloudflare` de propósito — ver `_HEADER_CDN`.
    """
    return _casa(headers, _HEADER_MITIGATION)


def cdn_signals(headers: dict | None) -> list[str]:
    """Quem serve a página. Só enriquece mensagem; não classifica nada."""
    return _casa(headers, _HEADER_CDN)


def body_signals(body: bytes | str | None) -> list[str]:
    """Assinaturas de WAF encontradas no corpo, sem repetir."""
    texto = _body_text(body)
    if not texto:
        return []
    achados: list[str] = []
    for m in _BODY_RE.finditer(texto):
        s = " ".join(m.group(0).lower().split())
        if s not in achados:
            achados.append(s)
    return achados


#: Teto de tamanho para acreditar numa assinatura vinda de um HTTP 200.
#:
#: Página de desafio é pequena — a do Akamai que mediram no portal de dados tem 475 bytes, e
#: as do Cloudflare ficam na casa das dezenas de KB. Um ARTIGO sobre Cloudflare,
#: ou uma doc que explica "Access Denied", tem as mesmas palavras e centenas de
#: KB. Sem este teto, o classificador mandaria o Neo abrir um navegador para ler
#: uma página que ele já tinha lido inteira.
#:
#: Só vale para 200: num 403 o tamanho não importa, a recusa já é o status.
_MAX_INTERSTITIAL_BYTES = 25_000


def _corpo_pequeno(body: bytes | str | None) -> bool:
    if body is None:
        return True
    return len(body) <= _MAX_INTERSTITIAL_BYTES


def classify(
    *,
    status: int,
    headers: dict | None = None,
    body: bytes | str | None = None,
) -> tuple[str, str] | None:
    """``(codigo, mensagem)`` do problema, ou ``None`` quando a resposta serve.

    A mensagem é escrita para ser LIDA PELO MODELO, que decide o próximo passo
    — então ela diz o que houve E o que fazer, nunca só o código.
    """
    corpo = body_signals(body)
    mitigacao = header_signals(headers)
    sinais = corpo + mitigacao

    if status in _NOT_FOUND:
        return (
            "not_found",
            f"HTTP {status}: esta URL não existe. NÃO tente por navegador nem "
            "repita — o endereço está errado. Procure a URL certa (web_search) "
            "ou diga ao usuário que a página saiu do ar.",
        )

    if status in _TRANSIENT:
        return (
            "transient",
            f"HTTP {status}: falha temporária do servidor. Vale UMA nova "
            "tentativa daqui a pouco; trocar de navegador não muda nada.",
        )

    # Num 200, exigir assinatura NO CORPO e mais uma prova: ou a página é
    # pequena (é um desafio, não conteúdo), ou o cabeçalho prova mitigação.
    # Só um dos dois sozinho gera falso positivo — e falso positivo aqui manda
    # o Neo abrir navegador para reler o que ele já leu.
    disfarcado = bool(corpo) and status == 200 and (
        _corpo_pequeno(body) or bool(mitigacao)
    )
    # 503 é ambíguo: manutenção (transitório) ou desafio de WAF (bloqueio).
    # Quem desempata é a assinatura.
    if status in (401, 403, 429, 503) or disfarcado:
        if status == 503 and not sinais:
            return (
                "transient",
                "HTTP 503: servidor indisponível no momento. Tente de novo "
                "mais tarde.",
            )
        return (
            "blocked_origin",
            _mensagem_bloqueio(status, sinais + cdn_signals(headers)),
        )

    if status >= 400:
        return ("fetch_failed", f"HTTP {status}.")

    return None


def _mensagem_bloqueio(status: int, sinais: list[str]) -> str:
    detalhe = f" (sinais: {', '.join(sinais[:3])})" if sinais else ""
    disfarce = (
        "O status veio 200, mas o conteúdo é uma página de desafio, não a "
        "página pedida. "
        if status == 200
        else f"HTTP {status}. "
    )
    return (
        f"{disfarce}O site RECUSOU esta origem{detalhe} — é anti-bot, não erro "
        "de endereço. Repetir o mesmo fetch daqui vai falhar igual. O que muda "
        "o resultado é trocar de ORIGEM: pedir a página pelo navegador do "
        "usuário (máquina dele, IP residencial). Se a tarefa era baixar um "
        "arquivo, prefira baixá-lo na máquina dele com headers de navegador a "
        "abrir navegador nenhum."
    )
