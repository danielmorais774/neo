"""Navegador LOCAL do usuário — o Neo agindo na tela que ele está vendo.

Ver docs/browser-local-plan.md. O irmão headless (Chromium no servidor) é o
``core/jobs/runner_browser.py``; a escolha entre os dois é do :mod:`gate`.
"""

from core.browser_local.gate import local_browser_requested

__all__ = ["local_browser_requested"]
