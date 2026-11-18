"""Ajustes do Neo: os números de comportamento que cada instalação adapta.

Duas casas de configuração, cada uma com o seu papel:

- ``.env`` (``shared/config.py``): segredos, endereços e liga/desliga de
  feature. Muda com ``docker compose up -d``.
- ``config/neo.toml`` (este módulo): constantes de comportamento — tetos,
  limiares, fatores. É o arquivo que alguém edita para adaptar o Neo à própria
  realidade: modelo que raciocina mais, conta com menos crédito, máquina mais
  lenta. Segredo NUNCA vem para cá: este arquivo é para ser lido e copiado.

Os padrões e a validação moram aqui, tipados. O arquivo só precisa ter o que
muda: ele é MESCLADO por cima dos padrões, então tirar uma linha volta ao
padrão em vez de quebrar.

Editar vale na próxima chamada, sem reiniciar: o arquivo é relido quando o
mtime muda (um ``stat`` por consulta). E editar nunca derruba o core: arquivo
inválido — TOML quebrado, chave com erro de digitação, valor fora da faixa —
mantém a última versão boa e avisa no log com o prefixo ``[ajustes]``.

Regra da casa: constante de comportamento nova (teto, limiar, fator, prazo)
nasce aqui, com o padrão no modelo e a linha documentada no ``neo.toml``. O
teste ``tests/test_ajustes.py`` falha se o arquivo não listar alguma.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

#: No container o neo-core é montado em ``/app``, então o arquivo é
#: ``/app/config/neo.toml``. A variável ``NEO_AJUSTES`` aponta para outro.
ARQUIVO_PADRAO = Path(__file__).resolve().parents[1] / "config" / "neo.toml"


class _Secao(BaseModel):
    # Chave desconhecida é erro de digitação até prova em contrário: aceitar
    # calado faria o ajuste "não pegar" sem ninguém saber por quê.
    model_config = ConfigDict(extra="forbid", frozen=True)


class AprendizadoDoTeto(_Secao):
    """Como o teto adaptativo aprende (vale para todo uso dele).

    É o cálculo do timeout do TCP (RFC 6298) aplicado a tokens de raciocínio:
    ``teto = média + desvios × desvio + reserva da resposta``.
    """

    peso_media: float = Field(0.125, gt=0, le=1)
    peso_desvio: float = Field(0.25, gt=0, le=1)
    desvios: float = Field(4.0, ge=0)
    fator_corte: float = Field(1.5, ge=1)
    fator_escada: float = Field(2.0, gt=1)


class TetoDeUso(_Secao):
    """Os limites de UM uso do teto adaptativo, em tokens."""

    reserva_resposta: int = Field(ge=1)
    piso: int = Field(ge=1)
    maximo: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordem(self) -> TetoDeUso:
        if not self.reserva_resposta < self.piso <= self.maximo:
            raise ValueError(
                "precisa valer reserva_resposta < piso <= maximo "
                f"(veio {self.reserva_resposta} < {self.piso} <= {self.maximo})"
            )
        return self


class ConversaNoNavegador(_Secao):
    """A tarefa no navegador conversa com o Daniel ENQUANTO roda: recebe
    mensagens (do painel da aba e do chat), avisa e pergunta sem morrer
    (docs/browser-conversa-plan.md)."""

    #: Quanto a tarefa espera, viva e com a aba presa, a resposta de uma
    #: pergunta. Passou disso, ela para como antes: a aba fica aberta e o
    #: próximo pedido continua dali.
    espera_min: float = Field(10.0, gt=0, le=120)
    #: Esperando, de quanto em quanto tempo olha o painel da aba.
    espera_intervalo_s: float = Field(2.0, ge=0.2, le=30)
    #: Avisos no chat por tarefa, e o mínimo de passos entre dois seguidos.
    avisos_max: int = Field(6, ge=0, le=50)
    avisos_intervalo_passos: int = Field(3, ge=0, le=50)
    #: Passos que cada mensagem do Daniel devolve ao orçamento (instrução nova é
    #: trabalho novo), e o teto absoluto de passos de uma tarefa.
    passos_por_mensagem: int = Field(15, ge=0, le=200)
    passos_teto: int = Field(150, ge=10, le=1000)
    #: Maior mensagem aceita, em caracteres (painel ou chat).
    mensagem_max: int = Field(1500, ge=50, le=20000)
    #: Enquanto ele escreve no painel o agente não mexe na página (roubaria o
    #: foco do campo). Teto dessa espera por ação, em segundos.
    digitando_espera_max_s: float = Field(90.0, ge=5, le=600)
    #: Na conferência antes do Salvar/Enviar, cada valor aparece inteiro até este
    #: tamanho; maior, sai começo e fim com o total de caracteres.
    conferencia_valor_max: int = Field(1500, ge=200, le=8000)
    #: Maior pergunta que o painel da aba mostra, em caracteres (a conferência de
    #: um formulário inteiro é a mais longa).
    pergunta_painel_max: int = Field(4000, ge=600, le=20000)


class JulgamentoNoNavegador(_Secao):
    """Quem julga uma GRADE DE AVALIAÇÃO no navegador (`julgar`, docs/browser-conversa-plan.md
    §4.6). O modelo do passo é barato e marcava "Conforme" em tudo; o juiz é outro."""

    #: O MODELO do juiz não mora aqui: é o slot "Navegador local — juiz da grade"
    #: da aba Modelos do neo-panel (`browser_local_judge`).
    #: Tokens que o juiz pode gastar raciocinando, por chamada (0 = sem raciocínio).
    raciocinio_max: int = Field(4000, ge=0, le=32000)
    #: Teto da resposta (o JSON com os vereditos), em tokens.
    resposta_max: int = Field(4000, ge=500, le=16000)
    #: Quanto do texto da página julgada entra, em caracteres.
    pagina_max: int = Field(12000, ge=1000, le=60000)
    #: Quanto do texto de cada OUTRA aba entra como evidência, em caracteres.
    evidencia_por_aba: int = Field(4000, ge=0, le=20000)
    #: Teto do tempo de uma chamada ao juiz, em segundos.
    timeout_s: float = Field(150.0, ge=10, le=600)


class JanelaDoNavegador(_Secao):
    """Com janela ou sem janela (headless): quem decide é o CÓDIGO, pelo que a
    conversa já fez (`_modo_da_sessao` em core/jobs/executor.py), não o modelo."""

    #: Quantas mensagens dele, desta conversa, são lidas para achar o último
    #: "no meu navegador" ou "sem janela". A conversa do desktop não acaba
    #: sozinha, então isto é o alcance da memória da escolha.
    mensagens_lidas: int = Field(300, ge=10, le=5000)
    #: Quantas tarefas recentes dele (de todas as conversas) são lidas.
    tarefas_lidas: int = Field(60, ge=10, le=500)
    #: Por quanto tempo (h) a última tarefa num SITE diz onde está o trabalho
    #: nele, mesmo vindo de OUTRA conversa: aba com janela → a próxima no mesmo
    #: site também; sem janela e parou no login → a próxima vai com janela.
    site_vale_por_horas: float = Field(12.0, gt=0, le=168)


class TarefaNoNavegador(_Secao):
    """O que a tarefa leva para o agente do navegador e o que ele pode enviar a um
    site (core/browser_local/pedido.py)."""

    #: As palavras do Daniel que o agente lê em todo passo, em caracteres. Mensagem
    #: maior perde o MEIO (começo e fim ficam).
    falas_max: int = Field(4000, ge=500, le=20000)
    #: Material de referência que o cérebro manda junto (o `context`: o texto de
    #: um currículo, os dados de um pedido), em caracteres.
    contexto_max: int = Field(8000, ge=500, le=40000)
    #: Maior arquivo do disco dele que a ação `upload` envia a um site, em MB.
    arquivo_max_mb: float = Field(25.0, gt=0, le=500)
    #: Maior arquivo do CATÁLOGO do Neo (gerado no sandbox, anexado no chat) que o
    #: `upload` envia, em MB. O conteúdo vai pelo canal do desktop em base64 (+33%)
    #: e o quadro do websocket do desktop aceita até 16 MiB.
    catalogo_max_mb: float = Field(10.0, gt=0, le=11)


class VisaoNoNavegador(_Secao):
    """Prints de CONFIRMAÇÃO no navegador local: o código tira print nos momentos
    em que errar custa (campo editado, envio, modal, navegação, upload). Plano em
    docs/browser-visao-confirmacao-plan.md. Nesta fase os prints só vão para o
    diário; a análise pela visão vem depois."""

    #: O caminho que o Daniel escolhe: com prints de confirmação (mais seguro e
    #: mais lento — cada print custa ~2 s hoje) ou só pela DOM, como sempre foi.
    confirmacao_visual: bool = False
    #: Teto de prints de confirmação por tarefa (o `look` e o print do fim não contam).
    prints_max: int = Field(60, ge=0, le=500)
    #: No print recortado de um campo, quanto da área ABAIXO dele entra (é onde o
    #: site escreve "campo obrigatório"), em px de CSS.
    recorte_margem_px: int = Field(120, ge=0, le=600)
    #: Análises pela visão por tarefa (o print continua até `prints_max`).
    analises_max: int = Field(40, ge=0, le=300)
    #: Análises rodando em paralelo enquanto o agente segue.
    paralelas_max: int = Field(2, ge=1, le=8)
    #: Largura máxima do print de tela inteira, em px: medido em 17/11, a 1276 px a
    #: visão lê o formulário igual e a imagem tem 1/3 dos bytes.
    largura_max_px: int = Field(1280, ge=480, le=4000)
    #: Teto do raciocínio do modelo de visão por conferência (o glm-5.3-flash não
    #: deixa desligar).
    teto_raciocinio: int = Field(200, ge=0, le=4000)


class Navegador(_Secao):
    decisao: TetoDeUso = TetoDeUso(reserva_resposta=600, piso=2000, maximo=6000)
    conversa: ConversaNoNavegador = ConversaNoNavegador()
    julgamento: JulgamentoNoNavegador = JulgamentoNoNavegador()
    janela: JanelaDoNavegador = JanelaDoNavegador()
    tarefa: TarefaNoNavegador = TarefaNoNavegador()
    visao: VisaoNoNavegador = VisaoNoNavegador()


class Trabalho(_Secao):
    """O Neo no ambiente de trabalho do Daniel: as sessões do Claude Code dele
    no Mac (docs/ambiente-de-trabalho-plan.md §9 e §11)."""

    #: A lista (`claude_sessoes`): as abertas sempre; as fechadas cuja conversa
    #: mexeu nas últimas horas, até o máximo.
    sessoes_recentes_horas: float = Field(24.0, gt=0, le=720)
    sessoes_max: int = Field(15, ge=1, le=50)
    #: Cortes da ficha de cada sessão, em caracteres, e quantos entram.
    ficha_objetivo: int = Field(160, ge=20, le=2000)
    ficha_pedido: int = Field(120, ge=20, le=2000)
    ficha_pedidos: int = Field(3, ge=1, le=20)
    ficha_resposta: int = Field(200, ge=20, le=4000)
    ficha_arquivos: int = Field(5, ge=0, le=50)
    #: Quanto o Neo espera o Mac entregar o pedido (o mensageiro leva ~10 s).
    envio_timeout_s: int = Field(120, ge=20, le=600)
    #: Depois de entregar: de quanto em quanto tempo olha a sessão, até quando
    #: acompanha, quanto da resposta vai ao chat e quanto aguenta o Mac fora.
    retorno_intervalo_s: float = Field(5.0, ge=1, le=120)
    retorno_timeout_min: int = Field(30, ge=1, le=1440)
    retorno_resposta_max: int = Field(6000, ge=200, le=50000)
    fora_do_ar_min: int = Field(10, ge=1, le=1440)


class Cerebro(_Secao):
    """O laço do cérebro: a conversa e as rodadas de tool de cada turno."""

    #: Temperatura do primeiro round (a resposta direta, a conversa).
    temperatura: float = Field(0.9, ge=0, le=2)
    #: Temperatura dos rounds DEPOIS que uma tool rodou no turno: é onde o modelo
    #: escreve código, comando e argumento de tool, e variação ali é erro, não
    #: estilo. Na session-20cb02b6 as 52 rodadas foram a 0.9.
    temperatura_com_ferramentas: float = Field(0.3, ge=0, le=2)
    #: Rodadas SEGUIDAS em que o modelo só repete chamada já feita no turno antes
    #: de a próxima rodada ir sem tools, com o aviso de responder com o que tem.
    #: Na session-3229310a foram 12 rodadas assim (3, 1 e 8 seguidas).
    repeticoes_seguidas_max: int = Field(3, ge=1, le=50)
    #: Quanto do resultado anterior volta no aviso de chamada repetida (a chamada
    #: em si leva a metade disto).
    repeticao_trecho_max: int = Field(800, ge=100, le=20000)
    #: Imagens do sandbox (`[[IMAGE:/workspace/…]]`) que a resposta mostra SEM ter
    #: publicado: quantas o servidor publica sozinho por resposta, para o chat
    #: conseguir desenhar. 0 = desligado (volta o cartão de erro). Na
    #: session-6e483f5d o recorte da notinha não apareceu por isso.
    imagens_publicadas_max: int = Field(6, ge=0, le=50)
    #: Turno que bate o teto de rodadas SEM ter entregado arquivo nem aberto job
    #: segue sozinho num passo de continuação, em vez de acabar oferecendo o
    #: trabalho ("me dá um 'vai' e eu gero"). Uma vez por pedido. Na
    #: session-3b19d27a a nota já estava lida e o Daniel precisou mandar "gera ai".
    continuar_no_teto: bool = True
    #: Planilha (.xlsx) publicada com `file_save` passa pela conferência de contas
    #: (`core/files/contas.py`): tabela de itens com quantidade × unitário e o
    #: TOTAL contra o valor pago. Se não fecha, o aviso vai colado ao resultado.
    conferir_planilhas: bool = True


class Roteador(_Secao):
    """O classificador de intenção, que roda antes de todo turno."""

    #: Teto de raciocínio para o modelo do classificador quando ele NÃO deixa
    #: desligar o raciocínio (GLM-5.3-flash, MiniMax m3.1-flash). Sem teto, o
    #: raciocínio comia os 2048 tokens da resposta e o turno ia a 44 s só de
    #: classificação (session-a20f4722). 0 = sem teto.
    teto_raciocinio: int = Field(300, ge=0, le=8000)


class OpenRouter(_Secao):
    """O gateway OpenRouter."""

    #: Modelos que recusam ``reasoning.enabled=false`` ("Reasoning is mandatory").
    #: O provider aprende na primeira recusa, mas só na memória do processo:
    #: cada reload pagava um 400. Listados aqui, nem tentam desligar.
    raciocinio_obrigatorio: tuple[str, ...] = (
        "z-ai/glm-5.3-flash",
        "minimax/minimax-m3.1-flash",
    )
    #: Provedores que o roteamento do OpenRouter NUNCA usa (``provider.ignore``),
    #: pelo slug da tag dele (``open-inference/fp4`` → ``open-inference``). Não
    #: fixa provedor: o gateway segue escolhendo entre os outros. Chamada com pin
    #: explícito (``modelo@slug``) não recebe o filtro, quem fixou já escolheu.
    #: O OpenInference levou 20–40 s por rodada do cérebro nas sessions b3ccdf21 e
    #: 10988735 (p99 de 26 s no próprio OpenRouter, contra 1–2 s dos outros).
    provedores_ignorados: tuple[str, ...] = ("open-inference",)


class Web(_Secao):
    """Leitura de páginas da web (``web_fetch``)."""

    #: Quantos links da página o ``web_fetch`` devolve junto com o texto. O texto
    #: extraído não traz endereço nenhum: sem a lista, o cérebro raspava o HTML
    #: no sandbox com ``curl`` + ``grep`` para achar a próxima página
    #: (session-a20f4722, 3 rodadas). 0 = não devolve links.
    links_max: int = Field(40, ge=0, le=200)
    #: Quantos caracteres de TABELA da página o ``web_fetch`` devolve, somando as
    #: tabelas, depois dos links. O trafilatura descarta tabela com muito link
    #: (as listas da Wikipédia) e o cérebro raspava o wikitext com regex: 24 das
    #: 29 rodadas da session-607c5313. 0 = não devolve tabelas.
    tabelas_max: int = Field(4000, ge=0, le=50000)
    #: Idioma que o ``web_search`` pede ao SearXNG. ``all`` deixa a própria
    #: consulta puxar o idioma: em português continua vindo UOL/G1/gov.br, e em
    #: inglês volta o que existe. ``pt-BR`` filtrava páginas em português e a busca
    #: "Windows XP error sound wav" voltava ZERO (session-34d7f528, 14/11).
    idioma_busca: str = Field("all", min_length=2, max_length=10)


class Faxina(_Secao):
    """A limpeza automática do ``/workspace`` do sandbox (``core/workbench/faxina.py``).

    Nada mais apaga o que o Neo baixa ou gera no sandbox: o teste do Sintel
    (session-34788907) deixou 1,6 GB ali com a VPS a 94% de disco.
    """

    #: Liga a limpeza periódica.
    ligada: bool = True
    #: De quanto em quanto tempo passa.
    intervalo_horas: float = Field(6.0, ge=0.25, le=168)
    #: Arquivo a partir deste tamanho é "grande" (download, vídeo, zip)...
    grande_mb: int = Field(20, ge=1, le=100_000)
    #: ...e sai quando fica este tempo sem ser modificado.
    grande_horas: float = Field(24.0, ge=1, le=8760)
    #: Anexos do Daniel copiados para ``/workspace/files`` saem depois deste
    #: tempo, de qualquer tamanho: é conteúdo pessoal. 0 = regra desligada. Em
    #: 14/11, 19 dos 31 arquivos ali NÃO tinham original disponível no catálogo
    #: (10 expirados, 9 nunca catalogados): ligar é decisão do Daniel.
    anexos_horas: float = Field(0.0, ge=0, le=8760)


class ComandoNoComputador(_Secao):
    """O ``run_command`` no computador do usuário (o shell do desktop).

    Até a session-1bbdd7c5 todo comando tinha 120 s de vida: o ``zip`` de 5,6 GB
    morreu no meio. Agora o comando que passa do orçamento ainda trabalhando vira
    tarefa em segundo plano no desktop (``services/local_jobs.rs``), e o Neo a
    acompanha (``core/jobs/runner_comando_local.py``).
    """

    #: Segundos em primeiro plano antes de um comando ainda rodando virar tarefa.
    #: 0 desliga: o comando morre no prazo de 120 s, como antes.
    segundo_plano_apos_s: int = Field(90, ge=0, le=1800)
    #: Teto total de uma tarefa, em minutos, contado do início do comando. O
    #: desktop não passa de 6 h.
    teto_min: int = Field(120, ge=1, le=360)
    #: Intervalo da conferência da tarefa no computador: começa no mínimo e dobra
    #: até o máximo enquanto ela roda.
    conferir_min_s: float = Field(5.0, ge=1, le=120)
    conferir_max_s: float = Field(30.0, ge=1, le=600)
    #: Minutos que o computador pode ficar sem responder no meio da tarefa antes de
    #: o Neo desistir de acompanhar.
    fora_do_ar_min: int = Field(10, ge=1, le=1440)


class Grpc(_Secao):
    """O servidor gRPC que conversa com o desktop."""

    #: Maior mensagem que o servidor aceita e manda, em MB. É lido na subida do
    #: servidor (mudar exige reiniciar o neo-core). O padrão do gRPC é 4 MB, e na
    #: session-1bbdd7c5 uma saída de 4.194.528 bytes derrubou o turno inteiro. O
    #: desktop já corta cada resultado em 1 MB: isto é a rede de segurança.
    mensagem_max_mb: int = Field(16, ge=4, le=128)


class CotaGratuitaGroq(_Secao):
    """Os limites do plano gratuito do Groq para o Whisper (docs/transcricao-groq.md).

    Com "só a cota gratuita" ligado no painel, o Neo para ANTES de passar destes
    números, mesmo que a conta aceite mais. Os de hora e de pedidos por dia
    vieram dos cabeçalhos da conta do Daniel em 15/11/2026; o de dia e o de
    pedidos por minuto, da documentação do Groq.
    """

    audio_s_por_hora: int = Field(7200, ge=0, le=1_000_000)
    audio_s_por_dia: int = Field(28800, ge=0, le=10_000_000)
    pedidos_por_minuto: int = Field(20, ge=0, le=10_000)
    pedidos_por_dia: int = Field(2000, ge=0, le=1_000_000)
    #: Maior arquivo que um pedido aceita no plano gratuito, em MB.
    arquivo_max_mb: int = Field(25, ge=1, le=100)
    #: Segundos de áudio que ficam sem usar em cada balde, para não encostar no
    #: limite por causa de arredondamento entre a conta do Neo e a do Groq.
    folga_s: int = Field(30, ge=0, le=3600)


class Transcricao(_Secao):
    """A tool ``neo_transcrever``: o sandbox tira o áudio e o Groq transcreve."""

    #: Duração máxima de cada pedaço mandado ao Groq, em segundos. Também cai
    #: sozinho se o pedaço passaria do tamanho máximo de arquivo da cota.
    parte_max_s: int = Field(1200, ge=60, le=7200)
    #: Bitrate do opus em kbps. 32 kbps a 16 kHz mono dá ~4 KB/s.
    bitrate_kbps: int = Field(32, ge=12, le=128)
    #: Vídeo mais longo que isto é recusado antes de gastar cota, em segundos.
    duracao_max_s: int = Field(7200, ge=60, le=86_400)
    #: Quanto a tool espera a cota encher no meio do trabalho, em segundos. Acima
    #: disso ela para e diz quando dá para continuar.
    espera_max_s: int = Field(60, ge=0, le=600)
    #: Idioma padrão do áudio (código ISO-639-1). "auto" deixa o Whisper detectar.
    idioma_padrao: str = Field("pt", min_length=2, max_length=8)
    #: Prazo de cada pedido ao Groq, em segundos.
    pedido_timeout_s: float = Field(120.0, ge=10, le=600)
    #: Prazo do ffprobe e do ffmpeg no sandbox, em segundos.
    extracao_timeout_s: int = Field(600, ge=30, le=1800)
    #: Quanto da transcrição volta para o cérebro, em caracteres. O texto
    #: inteiro fica no arquivo, e a tool diz quando cortou.
    previa_max: int = Field(6000, ge=500, le=50_000)
    #: Abaixo disto (fração do áudio com som), a tool recusa antes de gastar cota:
    #: é gravação de tela sem fala, e o Whisper inventaria frases. Medido em
    #: 16/11: vídeos com fala dão 100%, gravação de tela com ruído de fundo, 0%.
    som_min_fracao: float = Field(0.05, ge=0, le=1)
    #: O que conta como silêncio no ``silencedetect`` (dB) e por quanto tempo (s).
    silencio_db: float = Field(-40.0, ge=-90, le=0)
    silencio_min_s: float = Field(1.0, ge=0.1, le=30)
    #: Trecho que o Whisper marca como não-fala é descartado: a regra padrão da
    #: implementação de referência dele (no_speech_prob acima de 0,6 E
    #: avg_logprob abaixo de −1). Corta a alucinação em música e silêncio.
    sem_fala_prob: float = Field(0.6, ge=0, le=1)
    sem_fala_logprob: float = Field(-1.0, ge=-20, le=0)
    cota: CotaGratuitaGroq = CotaGratuitaGroq()


class Video(_Secao):
    """A tool ``neo_video_candidatos``: escolher o vídeo do YouTube antes de baixar."""

    #: Quantos resultados da busca do YouTube entram na comparação.
    candidatos: int = Field(8, ge=2, le=20)
    #: Prazo da busca no sandbox, em segundos (a busca leva ~2 s).
    busca_timeout_s: int = Field(45, ge=5, le=300)
    #: Fração dos termos do pedido que precisa aparecer no título ou no canal
    #: para o vídeo concorrer.
    cobertura_min: float = Field(0.5, ge=0, le=1)
    #: Vantagem de nota que o 1º precisa ter sobre o 2º para ser escolhido só
    #: pelos dados. Abaixo disso, a tool compara os quadros.
    margem: float = Field(0.15, ge=0, le=1)
    #: Quantos candidatos empatados vão para a comparação de quadros (1 imagem
    #: por candidato, com 4 quadros).
    comparar_max: int = Field(4, ge=2, le=4)
    #: Meme: até isto, duração nota cheia; até `medio_max_s`, meia; acima, zero.
    curto_max_s: int = Field(90, ge=5, le=3600)
    medio_max_s: int = Field(240, ge=10, le=7200)
    #: Pesos da nota. Penalidade é subtraída quando o título/canal tem um termo
    #: de cópia (lista abaixo) que o pedido não tem.
    peso_titulo: float = Field(0.45, ge=0, le=1)
    peso_frase: float = Field(0.10, ge=0, le=1)
    peso_views: float = Field(0.30, ge=0, le=1)
    peso_duracao: float = Field(0.15, ge=0, le=1)
    peso_penalidade: float = Field(0.50, ge=0, le=2)
    #: Termos de cópia/derivado. Comparados sem acento e por palavra inteira.
    penalidades: list[str] = Field(
        default_factory=lambda: [
            "reação", "reagindo", "react", "reaction", "zoeira", "zueira", "dark",
            "remix", "paródia", "compilação", "compilation", "mashup", "edit",
            "editado", "explicando", "entrevista", "reportagem", "documentário",
            "nova versão", "como está", "anos depois", "anos após", "por onde anda",
            "ouça", "podcast", "dublado", "karaokê", "cover", "lyric", "lyrics",
            "slowed", "reverb", "sped up", "8d",
        ]
    )
    #: Prazo para baixar as miniaturas e montar as grades no sandbox, em segundos.
    quadros_timeout_s: int = Field(60, ge=10, le=300)
    #: Quanto tempo um vídeo escolhido pela tool pode ser baixado sem nova
    #: comparação, em segundos ("baixa de novo" horas depois).
    escolha_validade_s: int = Field(21600, ge=60, le=604_800)


class Ajustes(_Secao):
    teto_adaptativo: AprendizadoDoTeto = AprendizadoDoTeto()
    navegador: Navegador = Navegador()
    trabalho: Trabalho = Trabalho()
    cerebro: Cerebro = Cerebro()
    roteador: Roteador = Roteador()
    openrouter: OpenRouter = OpenRouter()
    web: Web = Web()
    faxina: Faxina = Faxina()
    comando_no_computador: ComandoNoComputador = ComandoNoComputador()
    grpc: Grpc = Grpc()
    transcricao: Transcricao = Transcricao()
    video: Video = Video()


def caminho() -> Path:
    return Path(os.environ.get("NEO_AJUSTES") or ARQUIVO_PADRAO)


def _mesclar(base: dict[str, Any], por_cima: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for chave, valor in por_cima.items():
        if isinstance(valor, dict) and isinstance(out.get(chave), dict):
            out[chave] = _mesclar(out[chave], valor)
        else:
            out[chave] = valor
    return out


def ler(arquivo: Path) -> Ajustes:
    """O arquivo mesclado por cima dos padrões. Levanta se for inválido."""
    with arquivo.open("rb") as f:
        dados = tomllib.load(f)
    return Ajustes.model_validate(_mesclar(Ajustes().model_dump(), dados))


#: (caminho, mtime) → ajustes. O mtime ``None`` é "arquivo não existe".
_cache: tuple[tuple[str, int | None], Ajustes] | None = None


def ajustes() -> Ajustes:
    """Os ajustes em vigor. Barato: só relê o arquivo quando ele muda."""
    global _cache
    arquivo = caminho()
    try:
        mtime: int | None = arquivo.stat().st_mtime_ns
    except FileNotFoundError:
        mtime = None
    except OSError as exc:
        logger.warning("[ajustes] não deu para ler %s (%s) — seguindo como estava", arquivo, exc)
        return _cache[1] if _cache else Ajustes()
    chave = (str(arquivo), mtime)
    if _cache is not None and _cache[0] == chave:
        return _cache[1]

    anterior = _cache[1] if _cache else None
    if mtime is None:
        atual = Ajustes()
    else:
        try:
            atual = ler(arquivo)
            logger.info("[ajustes] %s carregado", arquivo)
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
            atual = anterior or Ajustes()
            logger.warning(
                "[ajustes] %s inválido — seguindo com a %s. Motivo: %s",
                arquivo,
                "versão anterior" if anterior else "configuração padrão",
                exc,
            )
    # Cacheia mesmo o inválido: sem isto, cada consulta reabriria o arquivo e
    # repetiria o aviso até alguém consertar.
    _cache = (chave, atual)
    return atual
