"""Com job aberto no turno, "te aviso quando terminar" não é promessa pendente.

session-30b1e484: o verificador de promessa leu "sigo acompanhando e te aviso
quando tiver resultado" como trabalho a fazer, sintetizou "avisar o usuário
quando a tarefa terminar", o passo seguinte abriu um SEGUNDO job igual e o ciclo
morreu em anti_loop. Os textos abaixo são os de produção.
"""

from types import SimpleNamespace

from modules.desktop.ai_service import _promise_beyond_notice, _reply_with_opened_jobs

RESPOSTA_DO_PASSO_1 = (
    " Entendi — o browser está processando a tarefa em segundo plano. Vou te avisar "
    "assim que terminar.\n\nSó um detalhe que notei enquanto isso: por acaso você "
    "pediu pra eu **salvar algo num arquivo** "
    "(`/Users/daniel/Desktop/plano-de-acao/analise-perfil.md`)? Deu um erro "
    "de \"File exists\" — ou seja, esse arquivo já existe na pasta e eu não consegui "
    "sobrescrever sem autorização.\n\nSe você quiser que eu grave/altere esse arquivo, "
    "me avisa que eu cuido disso com calma. Mas, no momento, a tarefa do browser é o "
    "foco — sigo acompanhando e te aviso quando tiver resultado. 👍"
)

RESPOSTA_DO_PASSO_2 = "Fechou, abri no seu navegador local. Você vê a tela aí. Aviso quando terminar."


def test_aviso_de_fim_de_job_nao_e_promessa():
    assert _promise_beyond_notice(RESPOSTA_DO_PASSO_1) is False
    assert _promise_beyond_notice(RESPOSTA_DO_PASSO_2) is False


def test_promessa_alem_do_aviso_continua_valendo():
    """O aviso sai; o "vou baixar" fica — esse ainda é trabalho sem bloco."""
    assert _promise_beyond_notice("Vou baixar as imagens e te aviso quando terminar.") is True
    assert _promise_beyond_notice("Te conto quando sair. Agora busco a outra fonte.") is True


def test_sem_aviso_o_filtro_e_o_de_sempre():
    assert _promise_beyond_notice("Vou procurar outra fonte.") is True
    assert _promise_beyond_notice("Não consigo fazer isso agora.") is False
    assert _promise_beyond_notice("") is False


def test_turno_que_falhou_depois_de_enfileirar_conta_que_o_job_existe():
    falha = "A ferramenta falhou — run_command: Executor para módulo 'desktop' não registrado"
    job = SimpleNamespace(title="Análise de Portfólio Antigo")

    texto = _reply_with_opened_jobs(falha, [job])

    assert texto.startswith("A tarefa «Análise de Portfólio Antigo» já está na fila")
    assert "não precisa pedir de novo" in texto
    assert falha in texto, "a falha real continua dita"


def test_sem_job_aberto_a_falha_sai_como_era():
    assert _reply_with_opened_jobs("deu ruim", []) == "deu ruim"
