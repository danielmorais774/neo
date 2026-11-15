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


class Navegador(_Secao):
    decisao: TetoDeUso = TetoDeUso(reserva_resposta=600, piso=2000, maximo=6000)
    conversa: ConversaNoNavegador = ConversaNoNavegador()
    julgamento: JulgamentoNoNavegador = JulgamentoNoNavegador()
    janela: JanelaDoNavegador = JanelaDoNavegador()


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
