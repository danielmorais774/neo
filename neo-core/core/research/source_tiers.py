"""Ranking determinístico de fontes por domínio (F4 do deep-research-v2-plan).

O v1 deixava a diversidade/qualidade das fontes por conta de uma frase no prompt
do triage — e o relatório acabava dominado por sites educacionais genéricos.
Aqui o tier sai do domínio, em código, ANTES do LLM: o triage ordena por tier e
o modelo só desempata respeitando diversidade. O tier também define o
``source_type`` da evidência e quanto texto vale a pena baixar da página.

Puro: só stdlib (o pacote core/research não importa app nenhum).
"""

from __future__ import annotations

from urllib.parse import urlparse

# Tier 1 — fonte primária/científica/oficial. Casa por sufixo de host.
_TIER1_SUFFIXES = (
    ".edu", ".edu.br", ".gov", ".gov.br", ".mil", ".int",
    "arxiv.org", "doi.org", "nature.com", "science.org", "sciencedirect.com",
    "springer.com", "link.springer.com", "wiley.com", "onlinelibrary.wiley.com",
    "tandfonline.com", "sagepub.com", "jstor.org", "cambridge.org", "oup.com",
    "ieee.org", "ieeexplore.ieee.org", "acm.org", "dl.acm.org",
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "nih.gov", "who.int",
    "scholar.google.com", "semanticscholar.org", "crossref.org", "ssrn.com",
    "biorxiv.org", "medrxiv.org", "plos.org", "journals.plos.org",
    "scielo.br", "scielo.org", "redalyc.org", "capes.gov.br", "cnpq.br",
    "ibge.gov.br", "bcb.gov.br", "ipea.gov.br", "fiocruz.br", "embrapa.br",
    "usp.br", "unicamp.br", "ufrj.br", "ufmg.br", "unesp.br", "fgv.br",
    "oecd.org", "worldbank.org", "imf.org", "un.org", "europa.eu",
    "nasa.gov", "noaa.gov", "ipcc.ch", "iea.org",
)

# Tier 2 — enciclopédia/imprensa de referência.
_TIER2_SUFFIXES = (
    "wikipedia.org", "britannica.com", "stanford.edu", "plato.stanford.edu",
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com",
    "washingtonpost.com", "theguardian.com", "ft.com", "economist.com",
    "wsj.com", "bloomberg.com", "nationalgeographic.com", "scientificamerican.com",
    "newscientist.com", "folha.uol.com.br", "estadao.com.br", "oglobo.globo.com",
    "g1.globo.com", "globo.com", "uol.com.br", "nexojornal.com.br",
    "valor.globo.com", "bbc.com/portuguese", "dw.com", "france24.com",
    "agenciabrasil.ebc.com.br", "cartacapital.com.br", "piaui.folha.uol.com.br",
)

# Academia brasileira não cabe numa lista de domínios: periódicos rodam em OJS
# (emnuvens.com.br, periodicos.*) e repositórios institucionais em subdomínios
# próprios (repositorio.ufpi.br). Sem estes padrões, um artigo revisado por pares
# era rotulado "other" e o REFLECT achava que faltava fonte forte — foi o que
# aconteceu no primeiro relatório em prod.
_ACADEMIC_HOST_PATTERNS = (
    "repositorio.", "repositorios.", "periodicos.", "revistas.",
    "emnuvens.com.br", "hdl.handle.net", "ojs.", "seer.", "teses.", "biblioteca.",
)


TIER_ACADEMIC = 1
TIER_REFERENCE = 2
TIER_GENERIC = 3


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, suffixes: tuple[str, ...]) -> bool:
    for suffix in suffixes:
        if "/" in suffix:  # entrada com caminho não casa por host
            continue
        if suffix.startswith("."):  # TLD/sufixo institucional (.edu, .gov.br)
            # Casa subdomínio (ibge.gov.br) e o domínio nu (gov.br).
            if host.endswith(suffix) or host == suffix[1:]:
                return True
        elif host == suffix or host.endswith("." + suffix):
            return True
    return False


def _is_academic_host(host: str) -> bool:
    """Periódico/repositório acadêmico reconhecido por padrão de host."""
    return any(
        host.startswith(p) or f".{p}" in f".{host}" or host.endswith(p)
        for p in _ACADEMIC_HOST_PATTERNS
    )


def tier_for(url: str) -> int:
    """1 = científica/oficial, 2 = enciclopédia/imprensa de referência, 3 = resto."""
    host = _host(url)
    if not host:
        return TIER_GENERIC
    if _matches(host, _TIER1_SUFFIXES):
        return TIER_ACADEMIC
    # Imprensa de referência ANTES dos padrões acadêmicos: revistas de grande
    # circulação vivem em subdomínios que casariam com "revistas."/"seer." etc.
    if _matches(host, _TIER2_SUFFIXES):
        return TIER_REFERENCE
    if _is_academic_host(host):
        return TIER_ACADEMIC
    return TIER_GENERIC


_OFFICIAL_SUFFIXES = (".gov", ".gov.br", ".mil", ".int", "europa.eu", "who.int", "un.org")
_OFFICIAL_BODIES = (
    "ibge.gov.br", "bcb.gov.br", "ipea.gov.br", "oecd.org", "worldbank.org",
    "imf.org", "iea.org", "ipcc.ch", "nasa.gov", "noaa.gov",
)


def source_type_for(url: str) -> str:
    """Rótulo do tipo de fonte (vira ``Evidence.source_type``)."""
    host = _host(url)
    tier = tier_for(url)
    if tier == TIER_ACADEMIC:
        if _matches(host, _OFFICIAL_SUFFIXES) or _matches(host, _OFFICIAL_BODIES):
            return "official"
        return "academic"
    if tier == TIER_REFERENCE:
        if "wikipedia" in host or "britannica" in host or "stanford.edu" in host:
            return "encyclopedia"
        return "news"
    return "other"


def domain_of(url: str) -> str:
    """Domínio nu — usado para limitar N páginas do mesmo site no triage."""
    return _host(url)
