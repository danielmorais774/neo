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


try:  # FastAPI ≥ 0.141: `include_router` virou PREGUIÇOSO.
    from fastapi.routing import _IncludedRouter
except ImportError:  # versões antigas copiavam as rotas na hora
    _IncludedRouter = ()  # type: ignore[assignment,misc]


def _achatar(rotas, prefixo: str = "", herdadas: tuple = ()) -> list[tuple[str, APIRoute, tuple]]:
    """Todas as `APIRoute` do app, com prefixo e dependências de inclusão juntos.

    Por que isto existe: a partir do FastAPI 0.141 ``app.include_router`` não
    copia mais as rotas para ``app.routes`` — ele guarda um ``_IncludedRouter``
    preguiçoso, e o ``prefix`` e as ``dependencies`` da inclusão ficam no
    ``include_context`` em vez de serem fundidos em cada rota.

    O efeito disso aqui foi pior que uma falha: `isinstance(rota, APIRoute)`
    parou de casar com QUALQUER rota do app, e a varredura passou a rodar sobre
    uma lista vazia. `test_toda_rota_tem_guarda_ou_motivo` — o teste que existe
    para falhar no futuro, quando alguém subir rota nova sem auth — passou a
    aprovar tudo sem olhar nada. Quem denunciou foi o teste irmão, de rota
    fantasma, que falhou dizendo que TODAS as exceções tinham sumido.

    Varredura que silencia quando a biblioteca muda é pior que não ter
    varredura: ela continua dando o selo. Daí o achatamento explícito, que
    funciona nas duas versões.
    """
    saida: list[tuple[str, APIRoute, tuple]] = []
    for rota in rotas:
        if _IncludedRouter and isinstance(rota, _IncludedRouter):
            ctx = rota.include_context
            saida += _achatar(
                rota.original_router.routes,
                prefixo + (getattr(ctx, "prefix", "") or ""),
                tuple(herdadas) + tuple(getattr(ctx, "dependencies", ()) or ()),
            )
        elif isinstance(rota, APIRoute):
            saida.append((prefixo + rota.path, rota, tuple(herdadas)))
        # WebSocket e estáticos têm outra história.
    return saida


def _rotas_reais() -> list[tuple[str, APIRoute, tuple]]:
    saida = []
    for caminho, rota, herdadas in _achatar(app.routes):
        for metodo in sorted(rota.methods or []):
            if metodo in ("HEAD", "OPTIONS"):
                continue
            saida.append((f"{metodo} {caminho}", rota, herdadas))
    return saida


def _por_caminho(caminho: str) -> list[tuple[APIRoute, tuple]]:
    return [(r, h) for c, r, h in _achatar(app.routes) if c == caminho]


def _guardas_de(rota: APIRoute, herdadas: tuple = ()) -> set:
    """Tudo que guarda esta rota: o que veio no decorator e o que veio no include."""
    achados = {getattr(dep, "call", None) for dep in rota.dependant.dependencies}
    achados |= {getattr(d, "dependency", None) for d in (rota.dependencies or [])}
    achados |= {getattr(d, "dependency", None) for d in herdadas}
    return achados


def _tem_guarda(rota: APIRoute, herdadas: tuple = ()) -> bool:
    return bool(_guardas_de(rota, herdadas) & GUARDAS)


def test_a_varredura_enxerga_o_app():
    """Rede que esvazia em silêncio continua dando o selo — ver `_achatar`."""
    rotas = _rotas_reais()
    assert len(rotas) > 50, (
        f"a varredura só achou {len(rotas)} rotas: ela parou de enxergar o app "
        "(o FastAPI mudou como `include_router` guarda as rotas?). Enquanto isso, "
        "`test_toda_rota_tem_guarda_ou_motivo` aprova qualquer coisa."
    )


def test_toda_rota_tem_guarda_ou_motivo():
    nuas = [
        nome
        for nome, rota, herdadas in _rotas_reais()
        if not _tem_guarda(rota, herdadas) and nome not in SEM_GUARDA_POR_DESIGN
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
    existentes = {nome for nome, _, _ in _rotas_reais()}
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
        rotas = _por_caminho(caminho)
        assert rotas, f"rota {caminho} desapareceu do app"
        for rota, herdadas in rotas:
            if require_owner not in _guardas_de(rota, herdadas):
                faltando.append(f"{sorted(rota.methods)} {caminho}")
    assert not faltando, "rotas de dado pessoal sem require_owner: " + ", ".join(faltando)


def test_letta_saiu_do_ar():
    """Proxy catch-all sem auth para um runtime descartado — F5 do plano."""
    caminhos = [c for c, _, _ in _achatar(app.routes)]
    assert not [c for c in caminhos if "letta" in c]


def test_sessions_nao_tem_rota_duplicada():
    """Havia dois `GET /desktop/sessions`; o segundo era código morto."""
    duplicadas = [
        r
        for r, _ in _por_caminho("/api/v1/desktop/sessions")
        if "GET" in (r.methods or set())
    ]
    assert len(duplicadas) == 1
