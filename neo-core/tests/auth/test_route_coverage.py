"""Varredura: nenhuma rota do app sobe sem guarda (docs/api-auth-plan.md §8).

**Por que um teste que varre ``app.routes`` em vez de um teste por rota.** A
promessa "nada passa sem auth" não é sobre as rotas de hoje; é sobre a próxima,
escrita daqui a três features por alguém (ou por mim) que não vai lembrar deste
plano. Um teste por rota cobre o passado. Este falha no futuro, com o nome da
rota nova na mensagem, e obriga uma decisão explícita: guarda, ou entrada na
lista de exceções **com motivo escrito**.

Mesmo truque de ``tests/authority/test_risk_cobre_o_registry.py``, que falha
quando existe tool fora da tabela de risco.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from api.auth.principal import require_admin_read, require_owner, require_principal
from api.main import app
from api.security import require_panel_jwt

#: As três dependências do plano + o JWT do painel (telas de administração).
GUARDAS = {require_owner, require_principal, require_admin_read, require_panel_jwt}

#: Rotas que NÃO têm guarda, cada uma com o motivo. Entrar aqui é decisão, não
#: esquecimento — e o teste de baixo garante que a lista não envelhece.
SEM_GUARDA_POR_DESIGN: dict[str, str] = {
    "GET /health": "liveness probe do Docker; não revela nada",
    "GET /": "raiz, só diz o nome e a versão",
    "GET /api/v1/auth/mode": "o painel precisa saber se login é obrigatório ANTES de ter credencial",
    "POST /api/v1/auth/login": "quem faz login ainda não tem credencial",
    "POST /api/v1/auth/refresh": "a credencial É o refresh token, no corpo",
    "POST /api/v1/auth/logout": "idem; revogar o próprio token não precisa de mais nada",
    "POST /api/v1/whatsapp/webhook": "HMAC do gateway (X-Signature), não JWT",
    "GET /api/v1/files/{file_id}/download": (
        "aceita URL assinada porque <a href download> não manda header (§7.3); "
        "a rota resolve o principal por dentro"
    ),
    "POST /api/v1/devices/pair": "a autorização é o código de uso único do pareamento",
    "GET /api/v1/spotify/authorize": "redirect de navegador; identidade no state assinado",
    "GET /api/v1/spotify/callback": "redirect do Spotify; idem",
    "GET /api/v1/spotify/status": "só diz se há conexão, sem token nem dado",
    "POST /api/v1/spotify/disconnect": "apaga o refresh do Spotify do próprio usuário",
    # Liberações: a credencial é o token de APARELHO, exigido dentro da rota
    # (release.py), porque o JWT do painel não identifica quem decide.
    "GET /api/v1/liberacoes/minhas": "token de aparelho obrigatório no próprio handler",
    "POST /api/v1/liberacoes/{release_id}/decidir": "token de aparelho no handler",
    "GET /api/v1/liberacoes/{release_id}": "token de aparelho no handler",
    # Mesmo padrão: a credencial é o token do próprio aparelho, exigido dentro do
    # handler (`_device_from_token`), porque o desktop não tem o JWT do painel.
    "GET /api/v1/devices/mine": "token de aparelho obrigatório no handler",
    "PATCH /api/v1/devices/mine/{target_device_id}": "token de aparelho no handler",
    "DELETE /api/v1/devices/mine/{target_device_id}": "token de aparelho no handler",
    "GET /api/v1/devices/resolve": "token de aparelho obrigatório no handler",
    "POST /api/v1/devices/extension-code": (
        "token de aparelho obrigatório no handler — é DELE que sai o host_device_id "
        "(docs/browser-surface-plan.md §4)"
    ),
}


def _rotas_reais() -> list[tuple[str, APIRoute]]:
    saida = []
    for rota in app.routes:
        if not isinstance(rota, APIRoute):
            continue  # WebSocket e estáticos têm outra história
        for metodo in sorted(rota.methods or []):
            if metodo in ("HEAD", "OPTIONS"):
                continue
            saida.append((f"{metodo} {rota.path}", rota))
    return saida


def _tem_guarda(rota: APIRoute) -> bool:
    return any(
        getattr(dep, "call", None) in GUARDAS
        for dep in rota.dependant.dependencies
    ) or any(getattr(d, "dependency", None) in GUARDAS for d in (rota.dependencies or []))


def test_toda_rota_tem_guarda_ou_motivo():
    nuas = [
        nome
        for nome, rota in _rotas_reais()
        if not _tem_guarda(rota) and nome not in SEM_GUARDA_POR_DESIGN
    ]
    assert not nuas, (
        "rotas sem guarda de autenticação:\n  - "
        + "\n  - ".join(sorted(nuas))
        + "\n\nAdicione `Depends(require_owner)` (dado pessoal), "
        "`require_principal` (leitura genérica) ou `require_panel_jwt` "
        "(administração) — ou registre em SEM_GUARDA_POR_DESIGN com o motivo."
    )


def test_a_lista_de_excecoes_nao_envelhece():
    """Exceção para rota que não existe mais vira permissão esquecida."""
    existentes = {nome for nome, _ in _rotas_reais()}
    fantasmas = sorted(set(SEM_GUARDA_POR_DESIGN) - existentes)
    assert not fantasmas, (
        "SEM_GUARDA_POR_DESIGN cita rotas que não existem mais: " + ", ".join(fantasmas)
    )


def test_rotas_de_dado_pessoal_exigem_dono():
    """As que o plano nomeia não podem se contentar com `require_principal`."""
    exigem_dono = (
        "/api/v1/desktop/chat",
        "/api/v1/desktop/chat/stream",
        "/api/v1/desktop/chat/command-response",
        "/api/v1/desktop/sessions",
        "/api/v1/desktop/session/{session_id}",
        "/api/v1/desktop/session/{session_id}/history",
        "/api/v1/desktop/jobs",
        "/api/v1/desktop/working-memory/notes",
        "/api/v1/context/retrieve",
        "/api/v1/context/prompt-preview",
        "/api/v1/quick-help/ask",
    )
    faltando = []
    for caminho in exigem_dono:
        rotas = [r for r in app.routes if isinstance(r, APIRoute) and r.path == caminho]
        assert rotas, f"rota {caminho} desapareceu do app"
        for rota in rotas:
            tem = any(
                getattr(dep, "call", None) is require_owner
                for dep in rota.dependant.dependencies
            )
            if not tem:
                faltando.append(f"{sorted(rota.methods)} {caminho}")
    assert not faltando, "rotas de dado pessoal sem require_owner: " + ", ".join(faltando)


def test_letta_saiu_do_ar():
    """Proxy catch-all sem auth para um runtime descartado — F5 do plano."""
    caminhos = [r.path for r in app.routes if isinstance(r, APIRoute)]
    assert not [c for c in caminhos if "letta" in c]


def test_sessions_nao_tem_rota_duplicada():
    """Havia dois `GET /desktop/sessions`; o segundo era código morto."""
    duplicadas = [
        r for r in app.routes
        if isinstance(r, APIRoute)
        and r.path == "/api/v1/desktop/sessions"
        and "GET" in (r.methods or set())
    ]
    assert len(duplicadas) == 1
