"""O caderno registra o RESULTADO de um job de segundo plano.

O buraco que estes testes fecham (medido na sessão 09cfa763): o subagente do
navegador local apurou o número da reclamação, o modelo do monitor, o status e a
URL exata — e o caderno guardou o comprovante de enfileiramento
(``{'ok': True, 'job_id': ..., 'status': 'queued'}``).

Não era desobediência do modelo. ``browser_local_task`` responde ``queued`` em
milissegundos e o achado só existe minutos depois, quando o ``ProactiveDispatcher``
o entrega com uma chamada de LLM própria. **Entre um e outro não há turno
cognitivo** — o cérebro nunca vê o resultado, então nunca teve como chamar
``wm_write``. Por isso quem anota é o dispatcher.
"""

from __future__ import annotations

import uuid

import pytest

from core.working_memory.render import (
    render_auto_entry,
    render_job_entry,
    render_job_summary,
)

# `result_summary` REAL do job 7c127b3b desta sessão — é o corpus do teste, não
# um exemplo inventado: é exatamente este texto que precisava ter chegado ao
# caderno e não chegou.
RESUMO_REAL = (
    "Reclamação contra a Dell Computadores do Brasil (ID: 258599465) analisada. "
    "Título: 'Dell nega garantia de monitor Alienware Fast IPS alegando que "
    "retenção de imagem é característica da tecnologia e encerra atendimento sem "
    "diagnóstico adequado'. Status: 'Em réplica'. Teor: o consumidor relata "
    "retenção de imagem (image retention) com imagem fantasma roxa em monitor "
    "Alienware Fast IPS de 280 Hz.\n\n"
    "(no caminho, 1 passo falhou e eu contornei)\n\n"
    "(a aba ficou em https://www.reclameaqui.com.br/minha-conta/"
    "responder-avaliar-reclamacao/nEnU9PyEuu3vjhdg/)\n\n"
    "(diário do passo a passo: logs/browser_local/7c127b3b)"
)


def _entrada(status: str = "completed", summary: str = RESUMO_REAL) -> str:
    return render_job_entry(
        title="Analisar queixa Dell no Reclame Aqui",
        kind="browser_local",
        status=status,
        job_id="7c127b3b",
        summary=summary,
    )


# ── a entrada do caderno ────────────────────────────────────────────────────


def test_entrada_preserva_os_achados_que_o_caderno_perdia():
    entrada = _entrada()
    # Cada um destes é uma coisa que o usuário apontou como faltando.
    assert "258599465" in entrada
    assert "Alienware Fast IPS" in entrada
    assert "280 Hz" in entrada
    assert "Em réplica" in entrada
    assert "nEnU9PyEuu3vjhdg" in entrada


def test_entrada_promove_a_url_para_linha_propria():
    # O endereço exato é o que permite retomar sem refazer a navegação, e é o
    # primeiro a se perder quando o resumo é achatado numa linha.
    linhas = _entrada().splitlines()
    assert any(
        l.startswith("Onde parou: https://www.reclameaqui.com.br/") for l in linhas
    ), linhas


def test_entrada_de_job_que_falhou_nao_se_parece_com_sucesso():
    entrada = _entrada(status="failed")
    assert "FALHOU" in entrada
    assert "concluiu" not in entrada


def test_job_sem_resumo_ainda_diz_o_que_houve():
    # Um `failed` mudo continua sendo informação: avisa o próximo turno que não
    # adianta esperar por aquele resultado.
    entrada = _entrada(status="failed", summary="")
    assert "FALHOU" in entrada
    assert "sem devolver resumo" in entrada


def test_resumo_gigante_e_truncado_com_marca_visivel():
    # O teto é generoso de propósito (o corpo da nota é o lugar barato), mas
    # existe: sem ele um job que despeja a página inteira comeria a nota.
    entrada = _entrada(summary="x" * 40000)
    assert "resumo do job truncado" in entrada
    assert len(entrada) < 9000


# ── a linha do índice (o que entra no prompt TODO turno) ────────────────────


def test_linha_do_indice_carrega_o_achado_e_nao_texto_fixo():
    linha = render_job_summary(
        title="Analisar queixa Dell no Reclame Aqui",
        status="completed",
        summary=RESUMO_REAL,
    )
    assert "258599465" in linha
    # Era este texto, idêntico em toda nota automática, que tornava as linhas do
    # índice indistinguíveis entre si.
    assert "registro automático" not in linha


def test_linha_do_indice_descarta_as_muletas_do_runner():
    linha = render_job_summary(
        title="t", status="completed", summary=RESUMO_REAL
    )
    # Não ajudam a decidir se vale abrir a nota, e comem o espaço do que ajuda.
    assert "diário do passo a passo" not in linha
    assert "a aba ficou em" not in linha


def test_linha_do_indice_marca_o_estado_quando_nao_foi_sucesso():
    assert render_job_summary(title="t", status="failed", summary="deu ruim").startswith(
        "[failed] "
    )
    assert not render_job_summary(
        title="t", status="completed", summary="ok"
    ).startswith("[")


# ── o backstop do turno que ABRIU o job ─────────────────────────────────────


def test_turno_que_abriu_job_nao_afirma_mais_que_esta_enfileirado():
    # A linha antiga dizia `status: 'queued'` sobre um job que podia já ter
    # falhado — e foi o que aconteceu na sessão 4a75a655, onde dois jobs
    # morreram 2s depois de a nota dizer "queued".
    linha = render_auto_entry(
        [{
            "tool": "browser_local_task", "ok": True, "async_job": True,
            "job_id": "7c127b3b", "job_title": "Analisar queixa Dell",
        }]
    )
    assert "queued" not in linha
    assert "Analisar queixa Dell" in linha
    assert "7c127b3b" in linha
    assert "RESULTADO entra nesta nota" in linha


def test_recusa_da_tool_assincrona_preserva_o_motivo():
    """`deep_research` desligado devolve `{"ok": False, "error": ...}` — sem job.

    Tratar isso como enfileiramento jogaria fora o motivo da recusa, que é a
    única informação que o turno produziu.
    """
    from modules.desktop.ai_service import _summarize_tool_call

    class _TR:
        error = None
        result = {"ok": False, "error": "deep_research está desligado na config."}

    resumo = _summarize_tool_call("deep_research", {}, _TR())
    assert not resumo.get("async_job")
    assert "desligado" in resumo["summary"]


def test_enfileiramento_de_verdade_vira_linha_de_job():
    from modules.desktop.ai_service import _summarize_tool_call

    class _TR:
        error = None
        result = {"ok": True, "job_id": "7c127b3b", "title": "Analisar queixa Dell"}

    resumo = _summarize_tool_call("browser_local_task", {}, _TR())
    assert resumo["async_job"] is True
    assert resumo["job_id"] == "7c127b3b"


def test_tool_sincrona_continua_com_o_resumo_de_sempre():
    linha = render_auto_entry(
        [{"tool": "neo_bash", "ok": True, "command": "ls", "exit_code": 0}]
    )
    assert "rodei `ls`" in linha


# ── o dispatcher ────────────────────────────────────────────────────────────


class _JobFalso:
    def __init__(self, **kw):
        self.id = kw.get("id", uuid.uuid4())
        self.user_id = kw.get("user_id", uuid.uuid4())
        self.session_id = kw.get("session_id", uuid.uuid4())
        self.title = kw.get("title", "Analisar queixa Dell no Reclame Aqui")
        self.kind = kw.get("kind", "browser_local")
        self.status = kw.get("status", "completed")
        self.result_summary = kw.get("result_summary", RESUMO_REAL)
        self.notified_at = kw.get("notified_at", None)
        self.notify_policy = kw.get("notify_policy", "chat")
        self.spec_json = kw.get("spec_json", {})
        # Presentes no AgentJob real. Faltavam aqui, e o `except` do
        # `_note_result` engoliu o AttributeError em silêncio — a nota
        # simplesmente não era escrita. Ver `test_falha_do_caderno_e_registrada`.
        self.started_at = kw.get("started_at", None)
        self.finished_at = kw.get("finished_at", None)


class _CadernoFalso:
    def __init__(self):
        self.chamadas: list[dict] = []

    async def append_auto(self, **kw):
        self.chamadas.append(kw)
        return object()


def _dispatcher(caderno):
    from core.jobs.dispatcher import ProactiveDispatcher

    return ProactiveDispatcher(
        db=None, broker=None, llm_router=None, working_memory=caderno
    )


@pytest.mark.asyncio
async def test_dispatcher_grava_o_resultado_do_job_no_caderno():
    caderno = _CadernoFalso()
    job = _JobFalso()
    await _dispatcher(caderno)._note_result(job)

    assert len(caderno.chamadas) == 1
    chamada = caderno.chamadas[0]
    assert chamada["user_id"] == job.user_id
    # Mesma nota do backstop do turno (`sessao-<8>` sai do session_id): a linha
    # "abri o job X" e o resultado dele ficam juntos, na ordem em que ocorreram.
    assert chamada["session_id"] == job.session_id
    assert "258599465" in chamada["entry"]
    assert "258599465" in chamada["summary"]


@pytest.mark.asyncio
async def test_sem_caderno_o_dispatcher_nao_quebra():
    # Flag desligada: o store nem é construído e `working_memory` chega None.
    await _dispatcher(None)._note_result(_JobFalso())


@pytest.mark.asyncio
async def test_caderno_que_falha_nao_derruba_a_entrega():
    class _Explode:
        async def append_auto(self, **kw):
            raise RuntimeError("banco fora")

    # Sem exceção: a resposta do usuário não pode morrer porque o caderno caiu.
    await _dispatcher(_Explode())._note_result(_JobFalso())


@pytest.mark.asyncio
async def test_completed_sem_resumo_nao_suja_o_indice():
    caderno = _CadernoFalso()
    await _dispatcher(caderno)._note_result(_JobFalso(result_summary=""))
    assert caderno.chamadas == []


@pytest.mark.asyncio
async def test_failed_sem_resumo_ainda_e_anotado():
    caderno = _CadernoFalso()
    await _dispatcher(caderno)._note_result(
        _JobFalso(status="failed", result_summary="")
    )
    assert len(caderno.chamadas) == 1
    assert "FALHOU" in caderno.chamadas[0]["entry"]


@pytest.mark.asyncio
async def test_job_silencioso_tambem_vai_para_o_caderno():
    """Anotar não é avisar.

    `notify_policy="silent"` corta a mensagem ao usuário, não a memória do
    servidor — por isso `_note_result` fica ACIMA desse corte no `dispatch`.
    """
    caderno = _CadernoFalso()
    job = _JobFalso(notify_policy="silent")
    dispatcher = _dispatcher(caderno)

    marcados: list = []

    async def _mark(j):
        marcados.append(j)

    dispatcher._mark_notified = _mark
    await dispatcher.dispatch(job)

    assert len(caderno.chamadas) == 1, "job silencioso não anotou no caderno"
    assert marcados == [job]


@pytest.mark.asyncio
async def test_job_ja_entregue_nao_anota_de_novo():
    # A guarda de idempotência do dispatch vale para o caderno também: senão um
    # redispatch duplicaria a entrada.
    caderno = _CadernoFalso()
    import datetime as _dt

    await _dispatcher(caderno).dispatch(_JobFalso(notified_at=_dt.datetime.utcnow()))
    assert caderno.chamadas == []


# ── escrever o MÁXIMO no corpo, ler com teto ────────────────────────────────


def test_entrada_carrega_o_que_foi_PEDIDO_e_nao_so_o_que_voltou():
    # Sem isto, um job que falhou não diz o que tentava fazer — e o turno
    # seguinte repete a mesma tentativa por não saber que ela já foi feita.
    entrada = render_job_entry(
        title="t", kind="browser_local", status="failed", job_id="j",
        summary="não achei", pedido="Abrir o Reclame Aqui e achar a queixa da Dell",
    )
    assert "Pedido: Abrir o Reclame Aqui e achar a queixa da Dell" in entrada


def test_entrada_lista_todas_as_urls_tocadas_sem_repetir():
    entrada = render_job_entry(
        title="t", kind="browser_local", status="completed", job_id="j",
        summary=(
            "vi https://a.com/x e depois https://b.com/y (de novo https://a.com/x).\n"
            "(a aba ficou em https://fim.com/z)"
        ),
        urls=["https://start.com"],
    )
    assert "Onde parou: https://fim.com/z" in entrada
    assert "  - https://a.com/x" in entrada
    assert "  - https://b.com/y" in entrada
    assert "  - https://start.com" in entrada
    # O resumo do runner vai INTEIRO no corpo, então a URL final aparece lá
    # também — o que não pode é a lista "Links vistos" repetir o que já virou
    # "Onde parou", nem repetir a mesma URL duas vezes.
    lista = [l for l in entrada.splitlines() if l.startswith("  - ")]
    assert "  - https://fim.com/z" not in lista
    assert lista.count("  - https://a.com/x") == 1


def test_entrada_registra_arquivos_duracao_e_diario():
    entrada = render_job_entry(
        title="t", kind="browser_local", status="completed", job_id="j",
        summary="ok", arquivos=["nota.pdf · /Users/daniel/Downloads/nota.pdf"],
        duracao_s=74.5, diario="logs/browser_local/j",
    )
    assert "Arquivo: nota.pdf · /Users/daniel/Downloads/nota.pdf" in entrada
    assert "Durou: 74s" in entrada
    assert "(diário completo: logs/browser_local/j)" in entrada


def test_wm_read_respeita_o_orcamento_de_caracteres():
    """A válvula que torna seguro escrever farto no corpo."""
    from core.working_memory.render import render_tail

    corpo = "# Nota\n\n" + "\n\n".join(f"## dia {i}\n" + "x" * 5000 for i in range(6))
    texto, omitidas = render_tail(corpo, max_entries=4, max_chars=12000)
    assert omitidas >= 2
    assert len(texto) <= 12500, len(texto)
    # O mais RECENTE é o que sobrevive — é o que o próximo turno procura.
    assert "dia 5" in texto
    assert "dia 0" not in texto


def test_wm_read_entrega_o_comeco_quando_nem_a_entrada_mais_nova_cabe():
    from core.working_memory.render import render_tail

    corpo = "# Nota\n\n## dia 1\nCOMECO-IMPORTANTE " + "x" * 40000
    texto, omitidas = render_tail(corpo, max_entries=4, max_chars=3000)
    assert "COMECO-IMPORTANTE" in texto
    assert "entrada truncada por tamanho" in texto
    assert len(texto) <= 3200


def test_caderno_pequeno_continua_voltando_inteiro():
    from core.working_memory.render import render_tail

    corpo = "# Nota\n\n## dia 1\ncurto"
    texto, omitidas = render_tail(corpo)
    assert omitidas == 0
    assert texto == corpo


@pytest.mark.asyncio
async def test_dispatcher_passa_pedido_e_duracao_para_a_nota():
    import datetime as _dt

    caderno = _CadernoFalso()
    job = _JobFalso(
        spec_json={"task": "Achar a queixa da Dell", "start_url": "https://ra.com"},
        started_at=_dt.datetime(2026, 9, 10, 2, 6, 0),
        finished_at=_dt.datetime(2026, 9, 10, 2, 11, 30),
    )
    await _dispatcher(caderno)._note_result(job)

    entrada = caderno.chamadas[0]["entry"]
    assert "Pedido: Achar a queixa da Dell" in entrada
    assert "https://ra.com" in entrada
    assert "Durou: 330s" in entrada
    assert f"logs/browser_local/{job.id}" in entrada


@pytest.mark.asyncio
async def test_falha_do_caderno_e_registrada(caplog):
    """O `except` não pode engolir em silêncio.

    Foi exatamente isso que mascarou um AttributeError durante este trabalho: a
    nota deixava de ser escrita e nada aparecia em lugar nenhum.
    """
    class _Explode:
        async def append_auto(self, **kw):
            raise RuntimeError("banco fora")

    with caplog.at_level("WARNING"):
        await _dispatcher(_Explode())._note_result(_JobFalso())
    assert any("caderno não registrou" in r.getMessage() for r in caplog.records)


# ── o caminho SÍNCRONO (agente principal) ───────────────────────────────────


def test_caderno_recebe_o_stdout_inteiro_mas_o_prompt_nao():
    """A separação que destrava o "não anota nada de útil".

    O mesmo `tool_trace` alimenta o caderno E o `extra_metadata`, que é
    reinjetado no contexto de todo turno seguinte. Era esse segundo consumidor
    que impunha o teto de 200 caracteres — e o caderno herdava o corte.
    """
    from modules.desktop.ai_service import _strip_note_detail, _summarize_tool_call

    class _TR:
        error = None
        result = {"stdout": "LINHA-IMPORTANTE\n" + "x" * 3000,
                  "stderr": "aviso relevante", "exit_code": 0}

    resumo = _summarize_tool_call("neo_bash", {"command": "ls"}, _TR())

    # No caderno: tudo, inclusive o stderr, que a prévia nunca teve.
    assert "LINHA-IMPORTANTE" in resumo["note_detail"]
    assert "aviso relevante" in resumo["note_detail"]
    assert len(resumo["note_detail"]) > 2000

    # No prompt: o compacto de sempre, sem o campo farto.
    enxuto = _strip_note_detail([resumo])[0]
    assert "note_detail" not in enxuto
    assert len(str(enxuto)) < 700


def test_registro_de_passo_ignora_o_campo_farto():
    """`_format_work_note` vai para o contexto da autocontinuação."""
    from modules.desktop.ai_service import _format_work_note

    nota = _format_work_note(
        "Passo 1", "ok",
        [{"tool": "neo_bash", "ok": True, "command": "ls",
          "note_detail": "SEGREDO-GORDO" * 500}],
        None,
    )
    assert "SEGREDO-GORDO" not in nota
    assert "`neo_bash` OK" in nota


def test_bloco_farto_entra_no_corpo_da_nota():
    from modules.desktop.ai_service import _summarize_tool_call

    class _TR:
        error = None
        result = {"stdout": "NFSE_novembro.pdf", "exit_code": 0}

    linha = render_auto_entry([_summarize_tool_call("neo_bash", {"command": "ls"}, _TR())])
    assert "NFSE_novembro.pdf" in linha
    # A prévia não repete o que o bloco já diz.
    assert linha.count("NFSE_novembro.pdf") == 1


# ── payload relegível: ponteiro, nunca o conteúdo ───────────────────────────
#
# Sessão 58a5854a: um `file_read` de placa.json pôs o JSON INTEIRO no caderno,
# duas vezes (linha curta + bloco farto) — 10 KB de nota em 2 entradas. Dois
# problemas, e o segundo é o grave: custo de tokens, e sobretudo VALIDADE — uma
# cópia do arquivo envelhece, e o caderno passa a fazer o modelo afirmar o que
# já não é verdade. O endereço não envelhece.


def _resultado_file_read(conteudo: str) -> dict:
    return {
        "content": conteudo, "start_line": 1, "end_line": 158,
        "has_more": False, "next_start_line": None, "truncated_by_cap": False,
        "file_id": "002b3431-3c8d-45c0-a1e6-0eb636c0d1f0", "name": "placa.json",
    }


def _resumo(nome: str, resultado):
    from modules.desktop.ai_service import _summarize_tool_call

    class _TR:
        error = None
        result = resultado

    return _summarize_tool_call(nome, {}, _TR())


def test_file_read_nao_copia_o_conteudo_para_o_caderno():
    conteudo = '{"chainName": "Rede Michells", "canvas": {"w": 1200}}' * 400
    resumo = _resumo("file_read", _resultado_file_read(conteudo))

    assert "note_detail" not in resumo
    assert "Rede Michells" not in str(resumo)
    # O que sobra é o ENDEREÇO e a extensão do que foi lido.
    assert resumo["name"] == "placa.json"
    assert resumo["file_id"] == "002b3431-3c8d-45c0-a1e6-0eb636c0d1f0"
    assert resumo["linhas"] == "1-158"
    assert resumo["chars"] == len(conteudo)


def test_linha_do_file_read_diz_onde_esta_e_como_reler():
    linha = render_auto_entry([_resumo("file_read", _resultado_file_read("x" * 5000))])
    assert "li 'placa.json'" in linha
    assert "linhas 1-158" in linha
    assert "NÃO copiados" in linha
    assert "file_stage" in linha  # o caminho para o sandbox
    assert len(linha) < 600, "a linha do caderno virou bolha de novo"


def test_file_stage_promove_o_caminho_do_workspace():
    """É o bom exemplo que já existia — agora é campo, não texto solto."""
    resumo = _resumo("file_stage", {
        "file_id": "3bd8dcbc", "target": "workbench",
        "path": "/workspace/files/placa.png",
        "note": "Arquivo disponível em /workspace/files/placa.png ...",
    })
    assert resumo["path"] == "/workspace/files/placa.png"

    linha = render_auto_entry([resumo])
    assert "/workspace/files/placa.png" in linha
    assert "neo_bash" in linha


def test_payload_de_tool_desconhecida_tambem_e_podado():
    """A regra é geral: tool nova com `content` gordo não fura o caderno."""
    resumo = _resumo("tool_do_futuro", {"content": "y" * 9000, "url": "https://x.com"})
    assert "yyyy" not in resumo["note_detail"]
    assert "9000 caracteres — não copiados" in resumo["note_detail"]
    # O que NÃO é payload sobrevive: é o dado que localiza a fonte.
    assert "https://x.com" in resumo["note_detail"]


def test_campo_pequeno_de_texto_nao_e_podado():
    # Poda cega comeria uma mensagem curta, que é resultado e não payload.
    resumo = _resumo("tool_qualquer", {"content": "ok, 3 linhas alteradas"})
    assert "ok, 3 linhas alteradas" in resumo["note_detail"]


def test_stdout_de_comando_continua_indo_inteiro():
    """Efêmero se guarda: `stdout` some para sempre se não for anotado."""
    resumo = _resumo("neo_bash", {"stdout": "RESULTADO-UNICO\n" + "z" * 3000, "exit_code": 0})
    assert "RESULTADO-UNICO" in resumo["note_detail"]
    assert len(resumo["note_detail"]) > 2000
