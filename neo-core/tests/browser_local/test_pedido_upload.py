"""Uma tarefa só, com o pedido inteiro, e o arquivo do disco dele (sessão be8d195e)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.browser_local.pedido import (
    caminho_autorizado,
    caminhos_citados,
    cortar_falas,
    cortar_meio,
    tarefa_encolhida,
)

PEDIDO_GEEKHUNTER = (
    "# Atualizar perfil GeekHunter com base no currículo\n\nQuero que você atualize meu "
    "perfil profissional no GeekHunter utilizando meu currículo anexado como fonte "
    "principal.\n\n**Site:** https://www.geekhunter.com/candidates/preview\n\n**Foto de "
    "perfil:** `/Users/daniel/Downloads/Retrato Masculino em Estúdio Cinza.png`\n\n"
    "- Não altere configurações de privacidade ou preferências profissionais sem "
    "autorização.\n- Se alguma informação importante estiver ausente ou exigir "
    "confirmação, pergunte antes de preencher.\n"
)
TAREFA_SO_MAPEAR = (
    "Abrir https://www.geekhunter.com/candidates/preview usando a sessão já autenticada. "
    "Uma vez dentro do perfil de candidato, NÃO altere nada ainda: apenas mapeie e reporte "
    "o que está preenchido hoje."
)


# ── tarefa encolhida ───────────────────────────────────────────────────────


def test_geekhunter_atualize_contra_so_mapear_e_recusado():
    assert tarefa_encolhida(TAREFA_SO_MAPEAR, PEDIDO_GEEKHUNTER) is not None


@pytest.mark.parametrize(
    "tarefa,pedido",
    [
        # os outros dois do corpus de produção (jobs 42a4ffd4 e edce52a7)
        (
            "Abrir a URL do formulário e LER o formulário inteiro, sem preencher nada e sem "
            "clicar em Enviar.",
            "Preenche esse formulário no meu navegador local, quero ver acontecendo",
        ),
        (
            "Abra a URL do formulário e APENAS LEIA a página, sem preencher nada.",
            "rodar browser_local_task com visible=true para preencher e enviar o formulário",
        ),
    ],
)
def test_os_outros_casos_do_corpus_tambem(tarefa, pedido):
    assert tarefa_encolhida(tarefa, pedido) is not None


@pytest.mark.parametrize(
    "tarefa,pedido",
    [
        # leitura de verdade: ele não mandou alterar (job 88f07ce8, rede social)
        (
            "Abrir o perfil da rede social e extrair TUDO, sem alterar nada.",
            "anote todas informacoes sobre a minha conta, preciso disso para repensar e melhorar",
        ),
        # "alternativas" não é "alterar" (job d73fa72a)
        (
            "Abrir o questionário e apenas leia as questões.",
            "extrair questões e alternativas para arquivo .txt",
        ),
        # ele pediu para ver antes
        (TAREFA_SO_MAPEAR, "atualize meu perfil, mas me mostra antes o que vai mudar"),
        (TAREFA_SO_MAPEAR, "só olha o meu perfil do geekhunter, depois eu decido"),
        # "não altere" com objeto não é ordem de alterar
        (TAREFA_SO_MAPEAR, "veja meu perfil e não altere nada"),
        # tarefa que altera não é encolhida
        ("Abrir o perfil, comparar com o currículo e atualizar os campos.", PEDIDO_GEEKHUNTER),
    ],
)
def test_nao_recusa_leitura_pedida_nem_tarefa_inteira(tarefa, pedido):
    assert tarefa_encolhida(tarefa, pedido) is None


# ── caminhos citados ───────────────────────────────────────────────────────


def test_caminho_com_espaco_e_acento_entre_crases():
    assert caminhos_citados(PEDIDO_GEEKHUNTER) == [
        "/Users/daniel/Downloads/Retrato Masculino em Estúdio Cinza.png"
    ]


def test_caminho_solto_com_til_e_sem_url():
    texto = "manda o ~/Documents/cv final.pdf no site https://x.com/a/b.png, e /Users/d/a.jpg."
    assert caminhos_citados(texto) == ["~/Documents/cv final.pdf", "/Users/d/a.jpg"]


def test_caminho_autorizado_pelo_caminho_ou_pelo_nome_unico():
    lista = ["/Users/daniel/Downloads/Retrato Masculino em Estúdio Cinza.png"]
    assert caminho_autorizado(lista[0], lista) == lista[0]
    assert caminho_autorizado("Retrato Masculino em Estúdio Cinza.png", lista) == lista[0]
    assert caminho_autorizado("/Users/daniel/.ssh/id_ed25519", lista) is None
    # NFD (como o macOS devolve) e NFC são o mesmo caminho
    import unicodedata

    assert caminho_autorizado(unicodedata.normalize("NFD", lista[0]), lista) == lista[0]
    dois = ["/a/foto.png", "/b/foto.png"]
    assert caminho_autorizado("foto.png", dois) is None


# ── falas no prompt ────────────────────────────────────────────────────────


def test_fala_longa_perde_o_meio_e_nao_some():
    longa = "COMEÇO " + "x" * 3000 + " FIM"
    saida = cortar_falas(["antiga", longa], 1500)
    assert len(saida) <= 1500
    assert "COMEÇO" in saida and "FIM" in saida and "antiga" not in saida


def test_falas_curtas_ficam_todas():
    assert cortar_falas(["a", "b"], 100) == "- a\n- b"
    assert cortar_meio("abc", 10) == "abc"


# ── executor ───────────────────────────────────────────────────────────────


class _Historico:
    def __init__(self, msgs):
        self.msgs = msgs

    async def fetch_session_messages(self, session_id, limit=50, roles=None, **kw):
        return self.msgs[-limit:]


def _msg(texto):
    from datetime import datetime

    return SimpleNamespace(content=texto, created_at=datetime.utcnow(), extra_metadata={})


@pytest.mark.asyncio
async def test_falas_do_daniel_com_pedido_de_1732_caracteres_chegam_ao_agente():
    from core.jobs.executor import JobToolExecutor

    pedido = PEDIDO_GEEKHUNTER + "y" * (1732 - len(PEDIDO_GEEKHUNTER))
    ex = JobToolExecutor.__new__(JobToolExecutor)
    ex.session_id = "s-be8d"
    ex.user_message = pedido
    ex.db = _Historico([_msg(pedido)])
    falas = await ex._falas_do_daniel()
    assert "Atualizar perfil GeekHunter" in falas


@pytest.mark.asyncio
async def test_executor_recusa_a_tarefa_so_mapear(monkeypatch):
    from core.jobs.executor import JobToolExecutor

    ex = JobToolExecutor.__new__(JobToolExecutor)
    ex.session_id = "s-be8d"
    ex.user_message = PEDIDO_GEEKHUNTER
    ex.device_id = "dev-1"
    ex.user_id = "u-1"
    ex.db = _Historico([_msg(PEDIDO_GEEKHUNTER)])

    async def _sem_retomada(_):
        return None

    monkeypatch.setattr(ex, "_retomada", _sem_retomada, raising=False)
    monkeypatch.setattr(
        "shared.config.get_settings", lambda: SimpleNamespace(browser_local_enabled=True)
    )

    async def _superficie(*a, **k):
        raise RuntimeError("sem superfície no teste")

    monkeypatch.setattr("core.browser_local.surfaces.resolve_browser_surface", _superficie)
    r = await ex._browser_local_task(
        {"task": TAREFA_SO_MAPEAR, "title": "GeekHunter", "visible": True}
    )
    assert r["ok"] is False
    assert "Recusado" in r["error"] and "upload" in r["error"]


# ── driver ─────────────────────────────────────────────────────────────────


def _driver(arquivos):
    from core.browser_local.driver import BrowserLocalDriver

    d = BrowserLocalDriver.__new__(BrowserLocalDriver)
    d._arquivos = list(arquivos)
    d._arquivo_max_bytes = 0
    d._catalogo = []
    d._ler_catalogo = None
    return d


def test_upload_so_de_arquivo_citado():
    lista = ["/Users/daniel/Downloads/Retrato Masculino em Estúdio Cinza.png"]
    d = _driver(lista)
    assert d._validar_upload({"arquivo": lista[0], "ref": "15"}) == {"arquivo": lista[0], "ref": 15}
    with pytest.raises(ValueError, match="não é um arquivo que eu possa enviar"):
        d._validar_upload({"arquivo": "/Users/daniel/.ssh/id_ed25519"})
    with pytest.raises(ValueError, match="não citou nenhum arquivo"):
        _driver([])._validar_upload({"arquivo": lista[0]})


def test_campo_de_arquivo_aparece_na_linha_do_elemento():
    from core.browser_local.driver import BrowserLocalDriver

    linha = BrowserLocalDriver._element_line({"ref": 15, "tag": "button", "text": "Alterar foto", "arquivo": True})
    assert "upload" in linha and "não clique" in linha


def test_upload_esta_no_vocabulario_e_no_prompt():
    from core.browser_local import driver

    assert driver.ACTIONS["upload"] == ("arquivo",)
    assert "- upload    {" in driver._SYSTEM


# ── driver de ponta a ponta (fakes do test_conversa) ───────────────────────

FOTO = "/Users/daniel/Downloads/Retrato Masculino em Estúdio Cinza.png"


class _DevicePerfil:
    def __init__(self):
        self.acted = []

    async def act(self, action, args, timeout):
        self.acted.append((action, dict(args)))
        if action == "browser.claim":
            return {"tab_ref": 42}
        if action == "browser.snapshot":
            return {
                "url": "https://www.geekhunter.com/candidates/preview",
                "title": "Perfil", "ready": "complete", "stop": False,
                "elements": [{"ref": 15, "tag": "button", "text": "Alterar foto", "arquivo": True}],
                "text": "Daniel Morais — Apresentação",
            }
        if action == "browser.click":
            return {"ok": True, "label": "Alterar foto", "arquivo_pedido": True}
        if action == "browser.upload":
            return {"ok": True, "nome": FOTO.rsplit("/", 1)[-1], "bytes": 1956096, "via": "pagina",
                    "campo": "Alterar foto"}
        return {"ok": True}


@pytest.mark.asyncio
async def test_clique_que_abre_a_janela_vira_upload_do_arquivo_citado():
    from tests.browser_local.test_conversa import _driver as _driver_falso

    device = _DevicePerfil()
    driver, ctx = _driver_falso(
        [
            {"action": "click", "args": {"ref": 15}},
            {"action": "upload", "args": {"ref": 15, "arquivo": FOTO}},
            {"action": "done", "args": {"answer": "foto trocada"}},
        ],
        device,
        arquivos=[FOTO],
        arquivo_max_bytes=25 * 1024 * 1024,
    )
    await driver.run("trocar a foto do perfil", start_url="https://www.geekhunter.com/candidates/preview")

    prompts = [c["messages"][-1].content for c in ctx.provider.calls]
    assert "ARQUIVOS DO DANIEL" in prompts[0] and FOTO in prompts[0]
    assert "⟪ARQUIVO" in prompts[0]
    # o clique segurado diz o caminho certo no passo seguinte
    assert "JANELA DE ARQUIVOS" in prompts[1]
    enviados = [a for a in device.acted if a[0] == "browser.upload"]
    assert enviados == [("browser.upload", {"arquivo": FOTO, "ref": 15, "max_bytes": 25 * 1024 * 1024, "tab_ref": 42})]
    assert "está no campo" in prompts[2]


# ── arquivo do CATÁLOGO (sessão d7ad32a0) ──────────────────────────────────

QUADRADO = "245d3f7d-b4f4-4113-8558-60f2f274bc59"


def _view(nome, fid, *, in_session=True, unpublished=False, origin="sandbox", size=167936, aliases=()):
    return SimpleNamespace(
        name=nome, current_file_id=fid, in_session=in_session, unpublished=unpublished,
        origin=origin, current_size=size, aliases=set(aliases),
    )


def test_catalogo_so_desta_conversa_e_publicado():
    from core.browser_local.pedido import arquivos_do_catalogo

    views = [
        _view("retrato_quadrado.png", QUADRADO, aliases={"/workspace/retrato_quadrado.png"}),
        _view("perfil-rede.md", "4632cc61-184a-4787-b856-90741b0cd749", in_session=False),
        _view("rascunho.png", "11111111-1111-1111-1111-111111111111", unpublished=True),
        _view("sem_catalogo.png", None),
        _view("retrato.png", "b3609e7d-e345-411c-b82d-84c5a7ee5a76", origin="host"),
    ]
    lista = arquivos_do_catalogo(views)
    assert [c["nome"] for c in lista] == ["retrato_quadrado.png", "retrato.png"]
    assert lista[0]["file_id"] == QUADRADO and lista[0]["origem"] == "sandbox"


def test_catalogo_autorizado_pelo_nome_id_ou_caminho_inventado():
    from core.browser_local.pedido import catalogo_autorizado

    cat = [
        {"file_id": QUADRADO, "nome": "retrato_quadrado.png", "nomes": ["retrato_quadrado.png"]},
        {"file_id": "b3609e7d-e345-411c-b82d-84c5a7ee5a76", "nome": "retrato.png", "nomes": ["retrato.png"]},
    ]
    assert catalogo_autorizado("retrato_quadrado.png", cat)["file_id"] == QUADRADO
    # o cérebro inventou a pasta no disco dele (d7ad32a0): vale pelo nome
    assert catalogo_autorizado("/Users/daniel/Downloads/retrato_quadrado.png", cat)["file_id"] == QUADRADO
    assert catalogo_autorizado(f"file:{QUADRADO}", cat)["nome"] == "retrato_quadrado.png"
    assert catalogo_autorizado("outro.png", cat) is None
    assert catalogo_autorizado("00000000-0000-0000-0000-000000000000", cat) is None


def test_upload_do_catalogo_quando_o_disco_nao_tem():
    d = _driver(["/Users/daniel/Downloads/retrato.png"])
    d._catalogo = [{"file_id": QUADRADO, "nome": "retrato_quadrado.png", "nomes": []}]
    d._ler_catalogo = object()
    # o do disco continua indo pelo caminho
    assert d._validar_upload({"arquivo": "/Users/daniel/Downloads/retrato.png"}) == {
        "arquivo": "/Users/daniel/Downloads/retrato.png"
    }
    assert d._validar_upload({"arquivo": "/Users/daniel/Downloads/retrato_quadrado.png", "ref": 9}) == {
        "arquivo": "retrato_quadrado.png", "file_id": QUADRADO, "ref": 9,
    }
    with pytest.raises(ValueError, match="retrato_quadrado.png» \\(do Neo\\)"):
        d._validar_upload({"arquivo": "/Users/daniel/.ssh/id_ed25519"})


@pytest.mark.asyncio
async def test_upload_do_catalogo_manda_o_conteudo_ao_desktop():
    import base64

    from tests.browser_local.test_conversa import _driver as _driver_falso

    lidos = []

    async def ler(fid):
        lidos.append(fid)
        return {"nome": "retrato_quadrado.png", "mime": "image/png", "bytes": b"\x89PNG quadrado"}

    device = _DevicePerfil()
    driver, ctx = _driver_falso(
        [
            {"action": "upload", "args": {"ref": 15, "arquivo": "/Users/daniel/Downloads/retrato_quadrado.png"}},
            {"action": "done", "args": {"answer": "foto trocada"}},
        ],
        device,
        arquivos=["/Users/daniel/Downloads/retrato.png"],
        arquivo_max_bytes=25 * 1024 * 1024,
        catalogo=[{"file_id": QUADRADO, "nome": "retrato_quadrado.png", "nomes": [], "bytes": 167936,
                   "origem": "sandbox"}],
        ler_catalogo=ler,
    )
    await driver.run("trocar a foto do perfil", start_url="https://www.geekhunter.com/candidates/preview")

    prompts = [c["messages"][-1].content for c in ctx.provider.calls]
    assert "ARQUIVOS DO NEO" in prompts[0] and "retrato_quadrado.png (164 KB) — feito pelo Neo" in prompts[0]
    assert lidos == [QUADRADO]
    enviados = [a[1] for a in device.acted if a[0] == "browser.upload"]
    assert len(enviados) == 1
    env = enviados[0]
    assert "arquivo" not in env and "file_id" not in env
    assert env["nome"] == "retrato_quadrado.png" and env["ref"] == 15 and env["tab_ref"] == 42
    assert base64.b64decode(env["conteudo_b64"]) == b"\x89PNG quadrado"


def test_driver_sem_leitor_nao_oferece_o_catalogo():
    from tests.browser_local.test_conversa import _driver as _driver_falso

    cat = [{"file_id": QUADRADO, "nome": "retrato_quadrado.png"}]
    sem, _ = _driver_falso([], _DevicePerfil(), catalogo=cat)
    assert sem._catalogo == []

    async def ler(fid):
        return {}

    com, _ = _driver_falso([], _DevicePerfil(), catalogo=cat, ler_catalogo=ler)
    assert [c["file_id"] for c in com._catalogo] == [QUADRADO]


@pytest.mark.asyncio
async def test_executor_le_o_catalogo_da_conversa_pelo_registro(monkeypatch):
    from core.files.ledger import FileLedger
    from core.jobs.executor import JobToolExecutor

    async def views(self, session_id, user_id=None):
        assert (session_id, user_id) == ("s-d7ad", "u-1")
        return [_view("retrato_quadrado.png", QUADRADO)]

    monkeypatch.setattr(FileLedger, "views", views)
    ex = JobToolExecutor.__new__(JobToolExecutor)
    ex.session_id, ex.user_id, ex.db = "s-d7ad", "u-1", object()
    assert [c["file_id"] for c in await ex._arquivos_do_catalogo()] == [QUADRADO]

    async def quebra(self, *a, **k):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(FileLedger, "views", quebra)
    assert await ex._arquivos_do_catalogo() == []


# ── 98c8bac7: adotado não vai a site; conferência mostra o texto inteiro ──


def test_documento_so_adotado_nao_entra_no_catalogo_do_upload():
    from datetime import datetime, timedelta

    from core.browser_local.pedido import arquivos_do_catalogo
    from core.files.ledger import build_views

    t0 = datetime(2026, 11, 17, 2, 0)
    aqui, outra = "98c8bac7-ebad-48c3-ae19-21e699aa37e1", "d7ad32a0-36e5-4f4b-866c-2702d687c37f"

    def ev(i, doc, acao, sess, nome, fid):
        return SimpleNamespace(
            doc_key=doc, action=acao, session_id=sess, name=nome, file_id=fid, sha256=None,
            host_path=None, sandbox_path=None, size_bytes=1000, status="ok", note="",
            created_at=t0 + timedelta(minutes=i),
        )

    eventos = [
        ev(0, "foto", "attached", outra, "Imagem JPEG.jpeg", "38660435-7fd5-4989-a4e9-2c005776e5ef"),
        ev(1, "foto", "adopted", aqui, "Imagem JPEG.jpeg", "38660435-7fd5-4989-a4e9-2c005776e5ef"),
        ev(2, "cv", "attached", aqui, "curriculo.docx", "f1c004a4-d0a0-446d-9e3b-79c9bac4342f"),
    ]
    views = build_views(eventos, session_id=aqui)
    assert all(v.in_session for v in views)  # o bloco do prompt continua igual
    assert [c["nome"] for c in arquivos_do_catalogo(views)] == ["curriculo.docx"]


def test_conferencia_mostra_o_texto_longo_inteiro_ou_avisa_o_corte():
    from core.browser_local.driver import BrowserLocalDriver

    apresentacao = (
        "Desenvolvedor Full Stack com 5 anos de experiência em TypeScript, Node.js, React e React "
        "Native. Atuei em produto de fidelidade para postos de combustível em escala nacional. "
    ) * 5
    assert len(apresentacao) > 300
    assert BrowserLocalDriver._valor_na_conferencia(apresentacao, 1500) == apresentacao
    cortado = BrowserLocalDriver._valor_na_conferencia(apresentacao * 4, 1500)
    assert cortado.startswith("Desenvolvedor Full Stack") and "[…]" in cortado
    assert f"texto de {len(apresentacao * 4)} caracteres" in cortado


# ── tempo do print no diário (plano da visão de confirmação, §7) ──────────


def test_diario_separa_device_e_transporte_no_tempo_do_print():
    from core.browser_local.trace import RunTrace as Trace

    linha = Trace._tempos_do_print({
        "ida_e_volta_ms": 1900, "total_ms": 450, "captura_ms": 300, "esconder_ms": 40,
        "mostrar_ms": 30, "rects_ms": 20, "imagem_ms": 60,
        "bytes_captura": 266240, "bytes_saida": 185344,
    })
    assert linha.startswith("1900 ms de ida e volta = device 450 ms (captura 300")
    assert "+ transporte 1450 ms" in linha and "260 KB capturado → 181 KB enviado" in linha
    assert Trace._tempos_do_print({"ida_e_volta_ms": 2100}) == "2100 ms de ida e volta (o desktop não mandou as etapas)"
    assert Trace._tempos_do_print(None) == ""
