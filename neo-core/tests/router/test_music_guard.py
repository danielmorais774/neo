"""O guard de música do roteador — as frases REAIS que ele deixava passar.

Três sessões de produção pagaram por estes casos:

  • ``session-ace000b8`` (07/09 16:48) — "consegue mutar essa aba do super
    xandao". O verbo estava na lista de controle, mas "aba" não era substantivo
    de mídia e nenhum serviço foi citado, então o guard não pegou. O
    classificador improvisou `osascript` para o **Google Chrome** (o usuário usa
    Edge), errou a sintaxe e repetiu 5 vezes — enquanto a extensão reportava a
    aba, com `tabId`, no `media_context` daquele mesmo turno.

  • 07/09 13:36 — "toca duality do slipknot agora". A frase nomeia a FAIXA, não
    a palavra "música": o guard exigia substantivo/serviço, passou reto, e o
    classificador devolveu `browser_local_task` (rebaixado para cognitivo sem
    `skill_hint`). O cérebro dirigiu o Edge passo a passo: 26 s contra o
    instantâneo do bloco [[HANDOFF:music]].

  • 06/09 15:01 — "coloca uma musica do meu mix para tocar ai": esta o guard
    pegava (tem "musica"), e mesmo assim o turno virou job de navegador. Isso é
    tratado no toolset do turno de música, não aqui.

Os NÃO-música vêm do mesmo corpus (1865 mensagens do banco): são exatamente os
falsos positivos que a versão ingênua ("verbo de play na abertura") produzia.
"""

from __future__ import annotations

import pytest

from core.router.intent_router import IntentRouter


MUSICA = [
    # controle — mudo dispensa substantivo (só existe para som de mídia)
    "consegue mutar essa aba do super xandao",
    "muta essa guia ai",
    "desmuta",
    "silencia essa guia do youtube",
    "abaixa o volume dessa aba",
    "pausa esse video",
    # play que nomeia a faixa/artista, sem a palavra "música"
    "toca duality do slipknot agora",
    "toca a 10 do exies",
    "toca essa 10 ai da lista, essa do exies",
    "toca um iron maiden para nos agora",
    "coloca um sertenejo para tocar",
    "coloca a 11 para tocar",
    "Coloque um C de C para tocar.",
    # pergunta sobre as abas em inglês (o Daniel escreve "tab" também) —
    # ver test_media_status_query abaixo, esta lista é só de play/controle
]

NAO_MUSICA = [
    # verbo de play, objeto de outro domínio — os falsos positivos medidos
    "coloca 300 de frete ai",
    "coloca mais 4 colunas nela",
    "coloca a coluna estado tb",
    "coloca essa alternativa tb nas nossas anotacoes",
    "Bota a piada aí pra mim.",
    "coloca o arquivo nessa pasta",
    "coloca rosa a cor",
    # verbo forte, mas com preposição: não é ordem de tocar mídia
    "toca no assunto do trabalho",
    # aba/guia sem verbo de mídia continua fora
    "fecha essa aba",
    "abre uma aba nova",
    # declarativa, não ordem
    "Ela não botou pra tocar, não. Acho que um.",
]


#: O pedido de 12/11 que virou turno de música em TRÊS sessões (41a690ba,
#: f6a0be50, 294e4ec5): "PARA avaliar" (preposição) + "ABA aberta" (outra frase).
#: O turno de música tira o `browser_local_task` da mesa, e o cérebro respondeu
#: "a extensão do Edge está offline" — com o navegador ligado.
PEDIDO_DO_FORMULARIO = (
    "Preenche esse formulário no meu navegador local, quero ver acontecendo:\n"
    "https://docs.google.com/forms/d/e/1FAIpQLSfFaF2zgl2RJ2LO9WtEtpQTVkuSwCq_0YkzK5v9zfMm_q3YXg/viewform\n\n"
    "Me avisa aqui do que ele trata assim que entender. Qualquer decisão que for minha, me "
    "pergunta e espera com a aba aberta. Os requisitos que ele avalia são do sistema AppDemo: para "
    "avaliar de verdade, cria uma conta de teste no AppDemo com e-mail descartável, entra no sistema "
    "e confere cada RF lá antes de responder. Não clica em Enviar no final."
)

NAVEGACAO_COM_ABA = [
    PEDIDO_DO_FORMULARIO,
    # o recado que ele manda com a tarefa do navegador rodando
    "volta para a aba do formulário e responde o RF-03",
    "pode continuar na outra aba",
    "para de mexer nessa aba, deixa que eu faço",
    "passa pra aba do AppDemo e confere o cadastro",
    # "para" + infinitivo é preposição, mesmo com o objeto de mídia na frase
    "abre o formulário para ver o vídeo da aula",
]

MUSICA_COM_OBJETO_PERTO = [
    "para a musica",
    "volta o video",
    "aumenta o som ai",
    "passa pra proxima musica",
    "abaixa um pouco o volume do spotify",
    "quero ouvir uma musica do linkin park",
    "toca alguma coisa do metallica no spotify",
    "coloca uma musica do meu mix para tocar ai",
    "silencia essa guia",
]


@pytest.mark.parametrize("frase", NAVEGACAO_COM_ABA)
def test_aba_e_para_soltos_no_pedido_nao_viram_musica(frase):
    assert IntentRouter._is_music_request(frase) is False


@pytest.mark.parametrize("frase", MUSICA_COM_OBJETO_PERTO)
def test_verbo_com_objeto_de_midia_perto_continua_musica(frase):
    assert IntentRouter._is_music_request(frase) is True


@pytest.mark.parametrize("frase", MUSICA)
def test_pedido_de_musica_e_reconhecido(frase):
    assert IntentRouter._is_music_request(frase) is True


@pytest.mark.parametrize("frase", NAO_MUSICA)
def test_frase_de_outro_dominio_nao_vira_musica(frase):
    assert IntentRouter._is_music_request(frase) is False


RECLAMACAO = [
    # sessão 7687b737: o mute voltou success=True sem mutar; a reclamação caiu
    # em `device_control` e virou agente de navegador clicando na tela.
    "nao mutou ela",
    "não tocou",
    "nao pausou nao",
]

NAO_RECLAMACAO = [
    "o container nao parou desde ontem e ta consumindo cpu",
    "você não tocou na ferida do assunto ontem no trabalho",
]


@pytest.mark.parametrize("frase", RECLAMACAO)
def test_reclamacao_de_controle_volta_para_a_skill(frase):
    """Errar aqui é o pior momento: a ação anterior já falhou."""
    assert IntentRouter._is_music_request(frase) is True


@pytest.mark.parametrize("frase", NAO_RECLAMACAO)
def test_narrativa_com_verbo_no_passado_nao_e_reclamacao_de_midia(frase):
    assert IntentRouter._is_music_request(frase) is False


def test_pergunta_de_abas_aceita_tab_em_ingles():
    """A frase real que respondeu "não tem navegador rodando" com a aba tocando.

    Sem "tab" no vocabulário, a pergunta virou `browser_screenshot` do navegador
    DO SERVIDOR e o Neo concluiu que nada estava aberto — com o `media_context`
    daquele mesmo turno trazendo a aba do YouTube, com som.
    """
    assert IntentRouter._is_media_status_query("quais tab aberta com youtube no momento?") is True
    assert IntentRouter._is_media_status_query("quais abas do youtube estao abertas?") is True
    # imperativo continua fora: "fecha" não é pergunta de status
    assert IntentRouter._is_media_status_query("fecha as tabs do youtube") is False


def test_verbo_forte_sozinho_nao_basta_sem_objeto_de_midia():
    """A camada de afordância vale para a ABERTURA da ordem, não no meio dela."""
    assert IntentRouter._is_music_request("depois a gente toca nisso") is False


# ---------------------------------------------------------------------------
# A skill de música é handoff-only — nem o classificador pode despachar direto
# ---------------------------------------------------------------------------


def _classificar(payload: dict) -> "object":
    """Roda o parser do classificador sobre uma resposta LLM crua."""
    import json

    from shared.types import IntentType

    router = IntentRouter.__new__(IntentRouter)
    router.tool_registry = None
    return router._parse_classification(json.dumps(payload), payload.get("_msg", ""))


def test_classificador_despachando_music_volta_para_o_cognitivo():
    """Sessão e8170c41 (08/09 17:39): "Sim, inicia a minha fila para tocar" saiu
    como SERVER_TOOL/tool_name="music". Naquele trilho as tools rodam pelo
    executor GLOBAL, sem usuário — `data_list` e `event_list` recusaram e o turno
    morreu em "Todas as ferramentas falharam". Fila e cadastros só existem no
    cognitivo, que é quem lê a skill."""
    from shared.types import IntentType

    decisao = _classificar({
        "intent": "SERVER_TOOL",
        "tool_name": "music",
        "tool_arguments": {"task": "iniciar fila de música"},
        "skill_name": "music",
        "confidence": 0.95,
        "_msg": "Sim, inicia a minha fila para tocar",
    })
    assert decisao.intent == IntentType.DIRECT_RESPONSE
    assert decisao.skill_hint == "music"


def test_skill_name_music_sozinho_tambem_volta():
    from shared.types import IntentType

    decisao = _classificar({
        "intent": "SKILL",
        "skill_name": "music",
        "confidence": 0.9,
        "_msg": "manda tocar a fila",
    })
    assert decisao.intent == IntentType.DIRECT_RESPONSE
    assert decisao.skill_hint == "music"


def test_outra_skill_nao_e_afetada():
    decisao = _classificar({
        "intent": "SERVER_TOOL",
        "tool_name": "get_weather",
        "confidence": 0.9,
        "_msg": "como vai estar o tempo amanha",
    })
    assert decisao.skill_hint != "music"


# ── Arquivo de áudio não é música (session-367b0103, 14/11) ─────────────────
# "baixa" é verbo de controle ("baixa o som" = volume), e o pedido de baixar e
# juntar dois MP3 virava música: pulava o classificador e perdia as tools.


@pytest.mark.parametrize("msg", [
    "baixa o som de connect e de disconnect USB do Windows 10, junta os dois num mp3 "
    "só com 250 ms entre eles e me manda aqui",
    "baixa essa musica pra mim em mp3",
    "converte esse audio pra wav",
    "corta os 10 primeiros segundos desse som",
])
def test_trabalho_com_arquivo_de_audio_nao_e_musica(msg):
    assert IntentRouter._is_music_request(msg) is False


@pytest.mark.parametrize("msg", [
    "baixa o som",
    "abaixa o volume do PC da Sala pra 20",
    "coloca um rock para tocar no youtube",
    "pausa a musica agora do youtube",
])
def test_controle_e_play_continuam_musica(msg):
    assert IntentRouter._is_music_request(msg) is True
