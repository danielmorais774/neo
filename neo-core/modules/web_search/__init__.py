"""Web search module — busca web inline do Neo (camada 1 do plano de pesquisa)."""

from modules.web_search.backends import SearchResult
from modules.web_search.executor import WebSearchExecutor
from modules.web_search.fetch import FetchedPage, FetchError
from modules.web_search.tools import WEB_SEARCH_TOOLS

__all__ = [
    "WEB_SEARCH_TOOLS",
    "WebSearchExecutor",
    "SearchResult",
    "FetchedPage",
    "FetchError",
]
