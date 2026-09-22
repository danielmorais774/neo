"""Registro de vínculos de arquivo — a sequência real da sessão f33632b4.

Interação 5: "adiciona ao arquivo final e coloque em ~/Desktop/teste-de-analise"
virou o PDF do Charlie gravado como ``conclusao-...md``. Nas interações 6–10 o Neo
não sabia mais qual cópia estava onde. O registro tem de contar essa história
sozinho, a partir dos eventos.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from core.files.ledger import build_views, render_block, resolve_doc_key

T0 = datetime(2026, 9, 16, 20, 33)
CONC = "conclusao-custos-regulacao-metas.md"
A = "414166c4-a33e-4cb8-8f9d-68dbc7bf4862"
L = "6b22eab5-fe16-4a4d-a65f-1131e7c65ef7"
B = "a6be6066-17ce-4fdd-805f-fd246ab2728b"
DESK = f"/Users/daniel/Desktop/{CONC}"
TESTE = f"/Users/daniel/Desktop/teste-de-analise/{CONC}"
CHARLIE_HOST = "/Users/daniel/Documents/analises/relatorios-anuais-2026/Relatorio_Charlie.pdf"


class _Ledger:
    """Replica o `FileLedger.record` sobre uma lista (sem banco)."""

    def __init__(self):
        self.events: list[dict] = []
        self.t = T0

    def add(self, action, **kw):
        self.t += timedelta(minutes=1)
        key = resolve_doc_key(
            self.events, action=action, host_path=kw.get("host_path"),
            sandbox_path=kw.get("sandbox_path"), file_id=kw.get("file_id"),
        )
        status = kw.pop("status", "ok")
        name = kw.get("name", "")
        if action == "delivered" and kw.get("host_path") and name.rsplit(".", 1)[-1] != kw["host_path"].rsplit(".", 1)[-1]:
            status = "wrong_content"
        ev = {"doc_key": key, "action": action, "created_at": self.t, "status": status, **kw}
        self.events.append(ev)
        return ev


def _sessao_ate_o_arquivo_errado() -> _Ledger:
    g = _Ledger()
    g.add("saved", name=CONC, sandbox_path=f"/workspace/{CONC}", file_id=A, sha256="aaa", size_bytes=8138)
    g.add("delivered", name=CONC, host_path=DESK, file_id=A, sha256="aaa")
    g.add("pulled", name="Relatorio_Charlie.pdf", host_path=CHARLIE_HOST, file_id=L, sha256="lll", size_bytes=1189907)
    g.add("staged", name="Relatorio_Charlie.pdf", sandbox_path="/workspace/files/Relatorio_Charlie.pdf", file_id=L)
    # O classificador mandou o id do PDF com o nome da conclusão.
    g.add("delivered", name="Relatorio_Charlie.pdf", host_path=TESTE, file_id=L, sha256="lll")
    return g


def _doc(views, nome):
    return next(v for v in views if v.name == nome)


def test_arquivo_errado_fica_registrado_no_documento_certo_e_marcado():
    views = build_views(_sessao_ate_o_arquivo_errado().events)
    assert len(views) == 2

    conclusao = _doc(views, CONC)
    assert conclusao.origin == "sandbox"
    assert conclusao.current_file_id == A and conclusao.current_where == "sandbox"
    assert [(c.path, c.state) for c in conclusao.host_copies] == [(DESK, "igual")]

    charlie = _doc(views, "Relatorio_Charlie.pdf")
    assert charlie.origin == "host" and charlie.origin_path == CHARLIE_HOST
    assert charlie.sandbox_path == "/workspace/files/Relatorio_Charlie.pdf" and charlie.sandbox_state == "atual"
    estados = {c.path: c.state for c in charlie.host_copies}
    assert estados == {CHARLIE_HOST: "igual", TESTE: "errada"}

    bloco = render_block(views)
    assert f"catálogo file_id={A}" in bloco
    assert f"⚠ {TESTE} — CONTEÚDO ERRADO: é «Relatorio_Charlie.pdf»" in bloco
    assert "overwrite=true" in bloco


def test_conserto_por_cima_move_a_copia_para_a_conclusao():
    g = _sessao_ate_o_arquivo_errado()
    g.add("delivered", name=CONC, host_path=TESTE, file_id=A, sha256="aaa")
    views = build_views(g.events)
    conclusao, charlie = _doc(views, CONC), _doc(views, "Relatorio_Charlie.pdf")
    assert {c.path: c.state for c in conclusao.host_copies} == {DESK: "igual", TESTE: "igual"}
    assert TESTE not in {c.path for c in charlie.host_copies}
    assert "CONTEÚDO ERRADO" not in render_block(views)


def test_editar_direto_no_disco_torna_a_copia_do_host_a_atual():
    g = _sessao_ate_o_arquivo_errado()
    g.add("delivered", name=CONC, host_path=TESTE, file_id=A, sha256="aaa")
    g.add("read_host", name=CONC, host_path=TESTE, sha256="aaa")
    g.add("edited_host", name=CONC, host_path=TESTE, sha256="bbb")
    conclusao = _doc(build_views(g.events), CONC)

    assert conclusao.current_where == "host" and conclusao.current_host_path == TESTE
    assert {c.path: c.state for c in conclusao.host_copies} == {DESK: "desatualizada", TESTE: "atual"}
    assert conclusao.sandbox_state == "desatualizada"
    bloco = render_block([conclusao])
    assert f"versão atual: no disco dele em {TESTE}" in bloco
    assert "⚠ DESATUALIZADA — puxe de volta" in bloco


def test_alteracao_so_no_sandbox_pede_file_save_antes_de_entregar():
    g = _Ledger()
    g.add("saved", name=CONC, sandbox_path=f"/workspace/{CONC}", file_id=A, sha256="aaa")
    g.add("delivered", name=CONC, host_path=DESK, file_id=A, sha256="aaa")
    g.add("edited_sandbox", name=CONC, sandbox_path=f"/workspace/{CONC}")
    conclusao = build_views(g.events)[0]
    assert conclusao.unpublished and conclusao.current_file_id is None
    assert {c.path: c.state for c in conclusao.host_copies} == {DESK: "desatualizada"}
    assert "NÃO publicada" in render_block([conclusao])


def test_publicar_de_novo_e_nova_versao_do_mesmo_documento():
    g = _Ledger()
    a = g.add("saved", name=CONC, sandbox_path=f"/workspace/{CONC}", file_id=A, sha256="aaa")
    g.add("delivered", name=CONC, host_path=DESK, file_id=A, sha256="aaa")
    b = g.add("saved", name=CONC, sandbox_path=f"/workspace/{CONC}", file_id=B, sha256="bbb")
    assert a["doc_key"] == b["doc_key"]
    conclusao = build_views(g.events)[0]
    assert conclusao.current_file_id == B
    assert {c.path: c.state for c in conclusao.host_copies} == {DESK: "desatualizada"}


def test_mudanca_feita_pelo_usuario_fora_do_neo_aparece_na_leitura():
    g = _Ledger()
    g.add("saved", name=CONC, sandbox_path=f"/workspace/{CONC}", file_id=A, sha256="aaa")
    g.add("delivered", name=CONC, host_path=DESK, file_id=A, sha256="aaa")
    g.add("read_host", name=CONC, host_path=DESK, sha256="zzz")  # ele editou no Obsidian
    conclusao = build_views(g.events)[0]
    assert conclusao.current_where == "host" and conclusao.current_host_path == DESK


def test_apagar_copia_do_disco():
    g = _Ledger()
    g.add("saved", name=CONC, sandbox_path=f"/workspace/{CONC}", file_id=A, sha256="aaa")
    g.add("delivered", name=CONC, host_path=DESK, file_id=A, sha256="aaa")
    g.add("deleted_host", name=CONC, host_path=DESK)
    assert build_views(g.events)[0].host_copies[0].state == "apagada"


def test_puxar_de_volta_um_arquivo_entregue_e_o_mesmo_documento():
    g = _Ledger()
    a = g.add("saved", name=CONC, sandbox_path=f"/workspace/{CONC}", file_id=A, sha256="aaa")
    g.add("delivered", name=CONC, host_path=DESK, file_id=A, sha256="aaa")
    p = g.add("pulled", name=CONC, host_path=DESK, file_id=B, sha256="aaa")
    assert p["doc_key"] == a["doc_key"]


def test_sem_eventos_nao_ha_bloco():
    assert render_block([]) == ""
