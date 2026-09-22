"""BrowserLocalDriver — o loop ver→decidir→agir do navegador LOCAL.

Quem dirige é o neo-core: a cada passo pede um ``browser.snapshot`` ao device,
mostra a página ao LLM, recebe UMA ação, valida contra o vocabulário e o
snapshot, e manda executar. O device (neo-desktop) não decide nada.

Quatro princípios, todos aprendidos na marra:

1. **Verdade vem do device.** Um passo só é "feito" se o `ok:true` voltou. O
   resumo final é montado do histórico de resultados REAIS, nunca da narrativa
   do modelo (lição da skill HA — "desliguei" sem desligar).
2. **`ok:true` não é o mesmo que "a página reagiu".** O clique devolve sucesso
   porque o evento foi despachado. Quem julga o efeito é a TELA: se ela não
   mudou, o passo é carimbado como inútil e a repetição literal é recusada
   (`_note_change`). Sem isso o driver clica 25 vezes no mesmo botão lendo o
   histórico como 25 sucessos — foi o que aconteceu no Drive em 2026-08-28.
3. **Ação inventada não vira comando.** Ação fora do vocabulário, ou `ref` que
   não está no snapshot deste passo, é recusada antes de sair — o modelo recebe
   o erro e tenta de novo, a página não é tocada.
4. **A aba é solta no fim, sempre.** Inclusive em falha, timeout ou PARE: deixar
   o `chrome.debugger` grudado estraga a aba do usuário.

Percepção é texto POR PADRÃO e imagem SOB DEMANDA. O snapshot (elementos
numerados + texto dos painéis) vai em todo passo e é o que sustenta a decisão;
a imagem entra pela ação `look`, que o próprio modelo pede quando a dúvida é
visual — e aí um modelo com visão RESPONDE à pergunta dele, sem decidir o passo
(`_look` + `_VISION_SYSTEM`). O print traz os mesmos números do snapshot
desenhados em cima, e é o `ref` que liga os dois.

Os papéis são separados porque juntá-los custou caro: até 2026-09-02 a imagem ia
em TODO passo e, com ela, o modelo de visão virava o raciocinador. Medido nos
jobs 9593f55f/db7adabf/f79d815d, um modelo de visão barato decidiu 36 de 37
passos, teve 33% de recusa e errou o formato do `done` nas 10 vezes que tentou
terminar — descrevendo a tela CORRETAMENTE no `thought`. Enxergar e decidir são
habilidades diferentes, e o modelo bom numa não é o bom na outra.

Sem modelo de visão configurado, ou depois das falhas dele, `look` responde
"não consegui ver" e o driver segue só no texto — nunca derruba a tarefa.

Ver docs/browser-local-plan.md §3.4 e §6.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Vocabulário fechado que o modelo pode escolher. Tem que casar com o que o
# neo-desktop implementa (services/browser_local.rs) — um nome a mais aqui vira
# "ação desconhecida" lá.
ACTIONS = {
    "navigate": ("url",),
    "click": ("ref",),
    # Lista de arquivos (Drive, e-mail, gerenciador) não abre com clique simples
    # e o menu "Baixar" só existe no botão direito: sem estas duas, tarefa de
    # pasta/arquivo é impossível por construção, não por burrice do modelo.
    "dblclick": ("ref",),
    "right_click": ("ref",),
    "fill": ("ref", "value"),
    "select": ("ref", "option"),
    "press": ("key",),
    "scroll": ("delta",),  # `ref` opcional: qual painel rolar
    # A SAÍDA para o que se lê e não se consegue endereçar. Todo inventário de
    # elementos é uma aposta, e aposta erra: no job 9968e68a as 5 alternativas
    # de um questionário não tinham `ref` nenhum, a visão leu as cinco
    # corretamente e, obrigada a citar um número que não existia, INVENTOU — o
    # agente clicou no menu lateral cinco vezes e o job morreu em 25 passos com
    # zero questões respondidas. Com isto, a pergunta deixa de ser "qual o
    # número?" e passa a ser "qual o texto?", que o modelo sempre tem e que é
    # verificável do lado de cá.
    "click_text": ("texto",),
    "extract": (),
    "wait_for": ("text",),
    # A barra de download é do NAVEGADOR, não da página: nenhum snapshot chega
    # nela. Sem esta ação o modelo re-clica em "Baixar" para sempre esperando um
    # "Download completo" que nunca vai aparecer no DOM (Drive, 2026-08-29).
    "downloads": (),
    "screenshot": (),
    # Os OLHOS, sob demanda. O driver tira o print e um modelo com visão
    # RESPONDE sobre ele — não decide o passo. Ver `_look` e `_VISION_SYSTEM`.
    #
    # Existe porque mandar a imagem em todo passo obrigava o modelo de visão a
    # ser também o raciocinador, e um modelo de visão barato é ruim nisso:
    # medido em 2026-09-02 (jobs 9593f55f/db7adabf/f79d815d), o gemini-3.1-flash-lite
    # decidiu 36 de 37 passos, teve 33% de recusa e emitiu `done` SEM o `answer`
    # nas 10 vezes que tentou terminar — com o `pensou` descrevendo a tela
    # corretamente. Ou seja: enxergava e não obedecia ao formato. Separando os
    # papéis, quem decide é o modelo de texto (bom em JSON e em plano) e a visão
    # ganha a única pergunta que ela responde bem: "o que tem nesta tela?".
    "look": (),
    # O CADERNO. Anotar é o que transforma "vi" em "tenho".
    #
    # Medido no job fc47220e (2026-09-03): 73 preços distintos DENTRO da faixa
    # pedida passaram na tela, `extract` foi usado ZERO vezes, e a tarefa
    # terminou sem entregar um único produto. Não era falta de inteligência do
    # modelo — era falta de lugar onde pôr o que ele já tinha visto. A página
    # muda, os `ref` mudam, o histórico é cortado nos últimos 8 passos: sem um
    # caderno, o que ele leu no passo 9 não existe mais no passo 20.
    "note": ("texto",),
    "done": ("answer",),
    "give_up": ("reason",),
}

#: Depois de tantas ações seguidas sem nada mudar na tela, o driver OLHA sozinho.
#: O modelo continua no comando (ele pede `look` quando quer), mas cegueira
#: repetida é justamente onde o texto do DOM já provou não bastar: em 2026-08-28
#: o driver clicou 25 vezes no mesmo botão porque `ok:true` não quer dizer que a
#: página reagiu. Uma imagem por episódio de travamento, não por passo.
_AUTO_LOOK_AFTER_STUCK = 2

#: Passos de atuação com o caderno VAZIO antes de o driver cutucar.
#:
#: Regra no prompt não bastou: no job fc47220e o modelo andou 23 passos, viu 73
#: preços dentro da faixa pedida e não anotou nem leu nada. A cutucada é uma
#: linha, uma vez, e é inofensiva quando a tarefa não é de coleta.
_NUDGE_CADERNO_VAZIO = 6

#: Teto do texto que a visão devolve para o histórico. É resposta a UMA pergunta,
#: não transcrição de tela — o DOM já vai inteiro no prompt.
_LOOK_MAX_CHARS = 1200

#: Quantas TRIAGENS DE PAINEL com visão cabem numa tarefa.
#:
#: A triagem custa um print + uma chamada ao modelo de visão (5 a 8s medidos no
#: job 6785cf70). A camada barata abaixo resolve a maioria dos painéis de graça;
#: este teto é a rede para o site que abre painel a cada passo — sem ele uma
#: tarefa num site assim viraria dois minutos de visão.
_MAX_TRIAGENS = 3

#: Teto do texto de um painel aberto no prompt. Maior que o dos painéis que rolam
#: (1200) porque, quando o painel É a resposta, cortá-lo é recriar o bug.
_PANEL_MAX_CHARS = 2000

#: Léxico de painel INTRUSO — o que aparece para atrapalhar, não para responder.
#: Casar aqui é veredito de graça, sem gastar visão.
_INTRUSO = re.compile(
    r"cookie|aceitar todos|aceito os termos|política de privacidade|termos de uso"
    r"|newsletter|assine|assinatura|inscreva-se|cadastre-se|criar conta|fa[çc]a login"
    r"|permitir notifica|ativar notifica|baixe o app|abrir no app|instale o app"
    r"|n[ãa]o perca|desconto exclusivo|cupom|promo[çc][ãa]o exclusiva|ganhe \d+%"
    r"|avalie (o|a|sua)|pesquisa de satisfa[çc][ãa]o|responda (a|nossa) pesquisa",
    re.IGNORECASE,
)

#: Léxico de painel de PROGRESSO de download. Já existia como regra em prosa no
#: prompt; aqui vira veredito, que é o que a guarda consegue usar.
_PROGRESSO = re.compile(
    r"cancelar o download|cancelar download|download em andamento|baixando"
    r"|preparando (o )?(download|arquivo)|compactando|gerando (o )?arquivo",
    re.IGNORECASE,
)

#: Rótulos de botão que FECHAM um painel. Usado só para reconhecer a intenção de
#: fechar — nunca para clicar sozinho.
_FECHA = re.compile(
    r"^\s*(close|fechar|dispensar|descartar|x|×|✕|✖|⨯|n[ãa]o,? obrigad|agora n[ãa]o"
    r"|mais tarde|pular|voltar)\b",
    re.IGNORECASE,
)

# Ações que mexem com dinheiro/identidade/coisa que não desfaz. O texto do
# elemento é o sinal — imperfeito, mas é o que existe antes de clicar.
_SENSITIVE = re.compile(
    r"\b(comprar|compre|pagar|pague|finalizar (compra|pedido)|confirmar (compra|pagamento|pedido)"
    r"|assinar|contratar|excluir|apagar|deletar|remover conta|cancelar (assinatura|conta)"
    r"|enviar (email|e-mail|mensagem)|publicar|postar|transferir|pix)\b",
    re.IGNORECASE,
)

# Ações de que se ESPERA mudança na tela. `extract`/`screenshot`/`wait_for`
# ficam de fora: não mudar nada é o comportamento correto delas.
_ACTUATING = frozenset(
    {
        "navigate", "click", "click_text", "dblclick", "right_click",
        "fill", "select", "press", "scroll",
    }
)

# Controle que MATA um download em andamento. Não é "sensível" no sentido de
# pedir confirmação — é simplesmente a ação errada quando fomos NÓS que
# mandamos baixar (2026-08-29: o modelo clicou nisto três vezes, sempre logo
# depois de conseguir clicar em "Baixar", e a tarefa acabou com zero arquivos).
_CANCELS_DOWNLOAD = re.compile(r"cancelar\s+(o\s+)?download|cancel\s+download", re.IGNORECASE)

# O painel de PROGRESSO do download, pelo título dele. Compactar/preparar é a
# primeira FASE do download quando se baixa uma PASTA (o site zipa antes de
# mandar) — não é erro e não é popup para dispensar. O modelo lia aquilo como
# "algo travou" e ia fechar, o que no Drive abre o "Cancelar o download?".
_DOWNLOAD_PANEL = re.compile(
    r"preparando o download|compactando|zipando|baixando|preparing download|downloading",
    re.IGNORECASE,
)

# Controles que DISPENSAM um painel: fechar, minimizar, cancelar, o X.
_DISMISSES = re.compile(
    r"^\s*(fechar|close|cancelar|cancel|minimizar|minimize|dispensar|descartar|ocultar|×|x)\s*$",
    re.IGNORECASE,
)

# O pedido manda ESPERAR o fim, ou só mandar baixar? "Baixe o arquivo" está
# cumprido quando o download começa — esperar byte chegar não é ação, é espera, e
# gastar passos do agente nisso é o pior jeito de esperar (quem espera é o job
# `download_watch`). Só estas frases mudam isso.
_ASKS_TO_FOLLOW = re.compile(
    r"aguard\w+|esper\w+|at[ée]\s+(o\s+)?(fim|final|terminar|concluir)"
    r"|garant\w+\s+que|confirm\w+\s+que\s+(o\s+)?(download|arquivo|zip)"
    r"|(download|arquivo|zip)\s+(seja\s+)?(conclu[ií]d\w+|finaliz\w+|complet\w+)"
    r"|acompanh\w+\s+(o\s+)?download|tudo\s+ocorra?\s+bem",
    re.IGNORECASE,
)

# Falha do modelo de VISÃO que não adianta repetir: cota/crédito da chave e
# modelo que simplesmente não aceita imagem. Tudo o mais (timeout, 5xx, rede,
# rate limit temporário) é soluço e ganha nova tentativa no passo seguinte.
# Foi um 429 "no credits remaining" no PASSO 1 que cegou o job inteiro
# (0f87265f) — o tratamento único não distinguia isto de um timeout.
_DOWNLOAD_LABEL = re.compile(r"\bbaixar\b|\bdownload\b", re.IGNORECASE)


def _starts_download(label: str | None) -> bool:
    """O rótulo INICIA um download? "Cancelar o download" contém a palavra e faz
    o oposto — confundir os dois fazia a trava anti-repetição acusar o botão de
    cancelar de "baixar o mesmo arquivo de novo"."""
    text = label or ""
    if not _DOWNLOAD_LABEL.search(text):
        return False
    return not _CANCELS_DOWNLOAD.search(text)


# Terminações que fazem de um token um HOST plausível. Sem esta conferência,
# qualquer coisa com ponto no meio vira domínio — e em 2026-09-01 o nome do
# ARQUIVO ("Palworld-SteamRIP.com.rar") virou o único host autorizado da tarefa,
# trancando o agente fora do próprio Google Drive: três jobs seguidos morreram
# em `about:blank` recusando `drive.google.com` "fora do escopo", sem nunca
# carregar a página.
#
# Não é lista de TLD para validar domínio de verdade — é só o suficiente para
# separar "drive.google.com" de "arquivo.com.rar". Qualquer rótulo de 2 letras
# passa (ccTLD); o resto precisa estar aqui.
_TLDS = frozenset(
    """com net org edu gov mil int info biz name pro app dev io ai co me tv cc
    xyz online site tech store cloud shop blog live news wiki space website
    digital agency studio design email software systems solutions network
    global world today center company group team work zone life fun art"""
    .split()
)

# Extensões que ganham do palpite de ccTLD. `gz`, `md`, `sh`, `db` e `js` SÃO
# domínios de topo de verdade (Moldávia, Santa Helena…), mas num pedido de
# navegador "backup.tar.gz" é nome de arquivo em praticamente 100% dos casos.
#
# A assimetria é o que decide: desde que a primeira navegação passe sempre (ver
# `_check_navigation`), esquecer um host do escopo quase não custa nada — ele
# entra na allowlist no primeiro `navigate`. Já incluir um host inventado custa
# a tarefa inteira. Na dúvida, fora.
_EXTENSOES = frozenset(
    """rar zip tar gz xz bz2 zst iso img bin exe dmg pkg deb rpm apk msi
    pdf doc docx xls xlsx ppt pptx odt txt rtf csv tsv json xml yml yaml toml
    log md png jpg jpeg gif svg webp ico bmp tiff mp3 mp4 mov avi mkv wav flac
    py js ts jsx tsx rb sh bash bat ps1 sql db bak tmp cfg conf ini env lock""".split()
)

_HOST_NO_TEXTO = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:/\S*)?")


def _hosts_citados(text: str) -> set[str]:
    """Hosts que o texto realmente cita — nomes de arquivo ficam de fora."""
    achados = set()
    for candidato in _HOST_NO_TEXTO.findall((text or "").lower()):
        tld = candidato.rsplit(".", 1)[-1]
        if tld in _EXTENSOES:
            continue
        if len(tld) == 2 or tld in _TLDS:
            achados.add(candidato)
    return achados


_VISION_FAILURE_IS_PERMANENT = re.compile(
    r"insufficient[_ ]quota|credit_balance_exhausted|no credits remaining"
    r"|exceeded your current quota|credit balance is too low|limit exceeded"
    r"|does not support image|image input|invalid[_ ]api[_ ]key|unauthorized",
    re.IGNORECASE,
)
# Soluços seguidos que a visão aguenta antes de ser considerada fora do ar.
_VISION_MAX_STRIKES = 3

_STEP_TIMEOUTS = {
    "navigate": 40.0,
    "wait_for": 45.0,
    "screenshot": 25.0,
    "snapshot": 25.0,
    "claim": 40.0,
    "network_activity": 10.0,
}
_DEFAULT_STEP_TIMEOUT = 15.0

# Quanto esperar um download NASCER depois do clique. O item do `chrome.downloads`
# aparece no instante em que o navegador aceita o download — antes do primeiro
# byte no disco —, então isto é folga, não aposta. Estourou a janela sem item:
# ou o clique não pegou, ou o site ainda está preparando o arquivo. Quem separa
# os dois é a checagem de rede (`_pagina_trabalhando`).
_JANELA_ARMAR_S = 4.0

# Erros de download que valem nova tentativa. O resto (usuário cancelou, disco
# cheio, sem permissão, servidor recusou) não melhora clicando de novo — repetir
# só entrega ao usuário o mesmo fracasso duas vezes.
_ERROS_PASSAGEIROS = frozenset(
    {
        "NETWORK_FAILED",
        "NETWORK_TIMEOUT",
        "NETWORK_DISCONNECTED",
        "NETWORK_SERVER_DOWN",
        "SERVER_FAILED",
        "SERVER_NO_RANGE",
        "SERVER_UNREACHABLE",
        "CRASH",
    }
)


class StopRequested(Exception):
    """O usuário apertou PARE na faixa da página."""


class NeedsConfirmation(Exception):
    """Ação irreversível sem OK do usuário — o job para e pergunta."""

    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.description = description


@dataclass
class Step:
    """Um passo executado — é disto que sai o resumo final."""

    action: str
    args: dict
    ok: bool
    detail: str = ""

    def render(self) -> str:
        args = {k: v for k, v in self.args.items() if k != "value"}
        if "value" in self.args:
            args["value"] = "…"  # nunca ecoar o que foi digitado (pode ser senha)
        status = "ok" if self.ok else "FALHOU"
        detail = f" — {self.detail}" if self.detail else ""
        return f"{self.action}({json.dumps(args, ensure_ascii=False)}) → {status}{detail}"


@dataclass
class DriverResult:
    status: str  # completed | failed | stopped | needs_confirmation
    summary: str
    url: str = ""
    steps: list[Step] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)  # base64 jpeg
    #: A aba onde a tarefa parou. `browser.release` DETACHA mas não fecha — a
    #: aba continua aberta na página, e só o id era jogado fora. Guardá-lo é o
    #: que permite a próxima tarefa continuar dali em vez de abrir uma aba nova
    #: em `about:blank` (2026-09-01: pediu confirmação para excluir, o usuário
    #: autorizou, e a tarefa seguinte recomeçou do zero).
    tab_ref: int | None = None
    #: O caderno da tarefa (ver `BrowserLocalDriver.notes`). Já vem colado no
    #: `summary`; separado aqui para quem quiser tratar item a item.
    notes: list[str] = field(default_factory=list)
    #: Rótulo da ação irreversível que fez a tarefa parar e perguntar. É a
    #: CHAVE da autorização: retomar libera este clique, não "clicar em
    #: qualquer coisa perigosa".
    pending: str = ""


#: System prompt dos OLHOS. Um trabalho só: responder sobre a imagem.
#:
#: Nada de vocabulário de ações, nada de JSON, nada de plano — é o que separa
#: este papel do de decidir. A pergunta vem de quem dirige, então a resposta pode
#: (e deve) ser curta: quem lê já tem o DOM inteiro e só está com uma dúvida
#: VISUAL. Pedir "descreva a tela" traria de volta o texto que o snapshot já dá.
_VISION_SYSTEM = """Você é os OLHOS de um agente que dirige o navegador. Ele NÃO
vê a tela; você vê. Ele já tem a lista de elementos e o texto da página — o que
falta é o que só a imagem mostra.

Responda à pergunta dele olhando o print. Regras:
- responda em português, direto, no máximo 6 linhas;
- diga o que ESTÁ na tela, não o que deveria estar. Se não dá para ver, diga
  "não dá para ver" — chutar é pior que não responder, porque ele vai agir em
  cima da sua resposta;
- **os números desenhados na imagem são os ÚNICOS que existem.** Cite um número
  só se ele estiver DESENHADO em cima do elemento de que você está falando ("o
  campo 7 está preenchido com 'São Paulo'"). Se o elemento não tem número, NÃO
  invente e NÃO deduza pela vizinhança: descreva-o pelo TEXTO que está escrito
  nele ("a terceira alternativa, a que começa com 'O racismo sistemático
  resultou'"). Ele tem como clicar por texto; um número errado faz ele clicar em
  OUTRA coisa. Isto já custou uma tarefa inteira: pedido a citar o número de uma
  alternativa sem marcação, um modelo respondeu "ref 18" — o 18 era um item do
  menu lateral, e o agente clicou lá cinco vezes;
- se algo estiver COBRINDO o conteúdo (popup, banner de cookie, modal, overlay),
  isso é a informação mais importante da tela — diga primeiro, e diga qual é o
  número do botão que fecha (ou o texto dele, se não tiver número);
- se a pergunta for se algo deu certo (campo preenchido, menu aberto, item
  selecionado, página trocada), responda começando por SIM ou NÃO;
- não sugira a próxima ação e não dê ordens: quem decide é ele."""


#: System prompt da TRIAGEM DE PAINEL. Papel diferente do `look`: ali a visão
#: responde uma pergunta em prosa; aqui ela CLASSIFICA, e quem age no veredito é
#: o driver.
#:
#: A diferença não é estética. No job 6785cf70 os olhos já tinham visto certo —
#: responderam "o overlay mostra detalhes de rastreamento" — e o cérebro fechou o
#: painel mesmo assim, porque a resposta chegou como mais um parágrafo num
#: contexto que gritava "COBERTO / travado / dispense o que está na frente".
#: Prosa entra em disputa com o resto do prompt; veredito não: ele vira regra
#: aplicada pelo driver (`_panel_verdict`), antes de o modelo opinar.
_TRIAGE_SYSTEM = """Você classifica UM painel que acabou de aparecer no navegador.
A imagem é o recorte dele.

Responda APENAS um objeto JSON:

{"tipo": "conteudo|intruso|consentimento|progresso", "porque": "…",
 "texto": "…", "fechar": "…"}

- `conteudo`: o painel RESPONDE alguma coisa — detalhes de um pedido, uma
  timeline, um formulário que a tarefa precisa preencher, o resultado de algo que
  foi clicado. Preencha `texto` com o que está escrito nele, transcrito, sem
  resumir nem inventar;
- `intruso`: propaganda, anúncio, promoção, newsletter, pedido para instalar app,
  convite para criar conta, pesquisa de satisfação. Não tem a ver com a tarefa;
- `consentimento`: aviso de cookies, termos, privacidade, "permitir
  notificações";
- `progresso`: download/exportação em andamento — barra, porcentagem, "gerando
  arquivo". NÃO é para fechar nem cancelar.

Em `intruso` e `consentimento`, preencha `fechar` com o TEXTO ESCRITO no botão
que dispensa o painel ("Aceitar todos", "Agora não", "×"). Se o botão não tem
texto, descreva onde ele está ("o X no canto superior direito").

**Nunca cite número de elemento.** Nem em `fechar`, nem em `porque`, nem em lugar
nenhum — este recorte não tem numeração, e um número aqui faz o agente clicar em
outra coisa. Já custou uma tarefa inteira: perguntado qual o X de fechar de um
modal, um modelo respondeu "elemento 48"; o 48 era a foto de um produto, o agente
clicou nela e saiu da página.

Na dúvida entre `conteudo` e `intruso`, olhe se o que está escrito tem a ver com
o que o usuário estaria fazendo naquela página. Se não der para decidir, responda
`conteudo` — tratar resposta como lixo é o erro caro; tratar lixo como resposta
custa um passo."""


_SYSTEM = """Você dirige o navegador REAL do usuário — ele está vendo a tela agora.

A cada rodada você recebe o estado da página: URL, título, os elementos
interativos numerados e o texto dos painéis. Você NÃO vê a tela — mas tem olhos
quando quiser: a ação `look` tira um print e um modelo com visão responde a UMA
pergunta sua sobre ele. Use quando a dúvida for visual (ver "Quando usar `look`"
mais abaixo). Escolha UMA ação. Responda APENAS um objeto JSON:

{"thought": "por que esta ação", "action": "<ação>", "args": {...}}

Ações e argumentos:
- navigate  {"url": "https://..."}        abre um endereço
- click     {"ref": 12}                   clica no elemento de número 12
- dblclick  {"ref": 12}                   duplo clique — é assim que se ABRE item de lista
- right_click {"ref": 12}                 botão direito — abre o menu do item
- fill      {"ref": 7, "value": "texto"}  escreve num campo
- select    {"ref": 4, "option": "São Paulo"}
- press     {"key": "Enter"}              Enter|Tab|Escape|Backspace|ArrowDown|
                                          ArrowUp|PageDown|PageUp|Home|End
- scroll    {"delta": 600, "ref": 12}     positivo desce, negativo sobe; o `ref`
                                          é opcional e diz QUAL painel rolar
- click_text {"texto": "O racismo sistemático resultou na exclusão"}
                                          clica no elemento que CONTÉM esse
                                          texto. Use quando você LÊ uma opção no
                                          texto da página e ela não tem número
                                          na lista — é o caso de alternativa de
                                          prova, item de lista custom e qualquer
                                          controle que o site desenhou na mão.
                                          Basta um trecho que só apareça uma vez
- extract   {"ref": 9}                    lê o texto (sem ref = a página toda)
- wait_for  {"text": "Resultados"}        espera algo aparecer NA PÁGINA
- downloads {"wait_s": 30}                pergunta ao NAVEGADOR o que houve com o
                                          download e lista a pasta; `wait_s`
                                          espera o arquivo ficar pronto (0 = só
                                          olha e volta). É esta ação que separa
                                          "ainda preparando" de "o clique não
                                          pegou" — leia a primeira linha da
                                          resposta e siga o que ela manda
- note      {"texto": "Redragon K552 — R$ 189,90 — 4,7★ (1.203) — /dp/B08XY"}
                                          ANOTA no caderno. É a única coisa que
                                          sobrevive à página mudar — e o que
                                          você entrega no fim
- look      {"pergunta": "o campo 7 ficou preenchido?"}
                                          OLHA a tela e responde à sua pergunta.
                                          Sem `pergunta` ele descreve o que há na
                                          tela — prefira perguntar algo específico
- screenshot {}                           guarda um print para mostrar ao usuário
                                          depois (não te devolve nada agora)
- done      {"answer": "a resposta ao usuário"}   quando a tarefa terminou
- give_up   {"reason": "por que não deu"}         quando é impossível continuar

Regras:
- só use `ref` que está na lista DESTE passo (a lista muda a cada página);
- **se o que você quer clicar não tem número, NÃO invente um.** Um número errado
  não falha: ele clica em OUTRA coisa (num caso real, no menu lateral, cinco
  vezes seguidas). Quando o alvo aparece no texto da página mas não na lista de
  elementos, o caminho é `click_text` com um trecho do texto dele;
- **SELECIONAR é diferente de clicar.** Alternativa de prova, opção de rádio e
  caixa de marcar aparecem com `[marcado]`/`[desmarcado]` na lista. Depois de
  clicar, confira: se continuar `[desmarcado]`, o clique não pegou e insistir no
  mesmo lugar não vai mudar isso — tente `click_text` no texto da opção. E se um
  botão de enviar estava `[desabilitado]` e deixou de estar, é a prova de que a
  seleção entrou;
- elemento marcado `⟪COBERTO por …⟫` tem um INTRUSO na frente: banner de cookie,
  anúncio, convite para assinar. Clicar nele não chega no site — dispense o
  intruso primeiro;
- **elemento marcado `⟪atrás do painel aberto …⟫` é outra coisa, e o conselho é o
  OPOSTO.** Esse painel foi aberto por você, e o que você procura quase sempre
  está DENTRO dele. Não feche, não aperte Escape, não clique de novo no que o
  abriu: leia a seção TEXTO COMPLETO DO PAINEL, que traz o que está escrito lá
  dentro por inteiro. Isto já custou uma tarefa: o modal com a timeline de
  rastreio ERA a resposta, e o modelo passou 25 passos tentando fechá-lo;
- **quando a página trouxer `>>> O PAINEL … É CONTEÚDO`, a decisão já foi
  tomada** — leia o painel e responda. Quando trouxer `É INTRUSO` ou
  `É CONSENTIMENTO`, feche pelo texto que a linha indicar e siga a tarefa;
- passo marcado com "ABRIU o painel …" FUNCIONOU: o painel é o efeito do que
  você fez. Fechá-lo desfaz o seu próprio trabalho;
- passo marcado com "a tela NÃO mudou" é ação que não funcionou. NÃO repita:
  troque de abordagem — outro elemento, `dblclick`/`right_click`, rolar a
  página, ou navegar direto pela URL. Se não souber POR QUE não funcionou,
  `look` antes de trocar: quase sempre tem um popup ou um overlay na frente;
- em lista de arquivos/pastas/mensagens, `click` só SELECIONA: para abrir use
  `dblclick`, e para achar opções do item (baixar, renomear) use `right_click`;
- **elemento marcado `⟪CAIXA com N itens dentro⟫` NÃO é um botão** — é a moldura
  que embrulha a lista, e clicar nela não faz absolutamente nada (o site escuta
  no item, não na caixa). O alvo é o elemento que tem o NOME do item que você
  quer. Cada linha traz também o tamanho em px: um controle tem dezenas de px de
  altura; uma caixa tem centenas. Se você está prestes a clicar em algo de
  1000px de largura, é caixa;
- o que vier marcado `[moldura do site]` é menu lateral/cabeçalho/rodapé, não o
  conteúdo da tarefa. Só vá lá se a tarefa for navegar;
- **antes de rolar atrás de uma informação, leia a seção DENTRO DOS PAINÉIS QUE
  ROLAM.** Ela traz o texto COMPLETO de cada painel, inclusive a parte abaixo da
  dobra que o print não mostra. Rolar para "procurar" o que já está escrito ali
  é passo jogado fora;
- `scroll` só funciona no elemento que de fato rola — o que vem marcado
  `⟪ROLA: y de N⟫`. Botão de aba ao lado do painel NÃO rola o painel. Se `y` já
  é igual a `N`, o painel acabou: a informação não está lá;
- se a página trouxer a seção MENU/DIÁLOGO ABERTO com um menu de ESCOLHA, a
  próxima ação é um `ref` de dentro dela. Clicar outra vez no botão que abriu o
  menu só fecha e reabre; se nenhum item serve, `press` Escape e siga por outro
  caminho. MAS um painel de PROGRESSO de download (com "Cancelar o download",
  "Minimizar", "Fechar") não é menu de escolha: nenhuma das opções dele avança a
  tarefa — deixe o painel quieto e espere o arquivo;
- DOWNLOAD: a barra de download é do navegador e NÃO existe na página — nem no
  texto, nem no print. `wait_for` nunca vai ver "Download completo". Depois de
  mandar baixar, confira com `downloads` — e use `wait_s` (20 a 40): baixar uma
  PASTA faz o site compactar tudo antes de mandar, então nos primeiros segundos
  a pasta de downloads está vazia e isso NÃO quer dizer que falhou. Arquivo com
  `incomplete: true` ainda está vindo; sem essa marca, chegou. E NUNCA clique em
  "Baixar" duas vezes "para garantir" — isso baixa o arquivo repetido. Você não
  precisa adivinhar em qual dos casos está: `downloads` responde com o veredito
  pronto — "DOWNLOAD COMEÇOU"/"CONCLUÍDO" (acabou, responda `done`), "AINDA
  PREPARANDO" (o site está montando o arquivo: espere, não clique), "FALHOU"
  (com o motivo, e dizendo se vale tentar de novo) ou "O CLIQUE NÃO VIROU
  DOWNLOAD" (aí sim o clique não pegou e você deve tentar outro caminho);
- **compactar/preparar É o download.** Baixar uma PASTA tem duas fases: o site
  zipa tudo no servidor ("Preparando o download", "Compactando N arquivos") e só
  então o arquivo vem. A primeira fase não é erro, não é travamento e não é
  popup para dispensar — é a tarefa acontecendo. Enquanto ela roda, NÃO feche,
  NÃO minimize e NÃO cancele o painel de progresso: no Drive o X do painel abre
  "Cancelar o download?". Se esse diálogo aparecer, a resposta é CONTINUAR;
- **"baixe o arquivo" está cumprido quando o download COMEÇA.** Assim que o
  `downloads` disser que começou (ou que concluiu), responda `done` com o nome
  do arquivo e pare. Não fique olhando barra de progresso, não arrume a tela e
  não mande baixar de novo — existe um vigia que avisa o usuário sozinho quando
  o arquivo terminar. Só continue esperando se o pedido tiver dito com todas as
  letras para aguardar a conclusão;
- quando um elemento tiver `em "<nome>"`, esse é o item da lista a que ele
  pertence — é assim que se distingue um "Mais ações" de outro;
- **MÍDIA: o estado está na linha "Mídia na página", nunca no rótulo do botão.**
  O rótulo de um play/pause diz a AÇÃO dele, não o estado: o YouTube escreve
  "Pausa" JUSTAMENTE quando o vídeo está tocando, e "Reproduzir" quando está
  parado. Como o botão é um INTERRUPTOR, clicar nele achando que está parado
  PARA o que estava tocando — e o passo seguinte desfaz de novo. Se a linha diz
  TOCANDO, a tarefa de "dar play" já está cumprida: responda `done`. Só clique
  no play quando ela disser PARADO, e depois disso confira a linha em vez de
  clicar de novo. Se o pedido era ouvir e ela disser "SEM SOM", o que falta é
  tirar o mudo, não dar play;
O CADERNO — a regra mais importante quando a tarefa é COLETAR:
- **anote NA HORA em que o dado aparece na tela.** Não "depois de terminar de
  filtrar", não "quando tiver os 5": a próxima ação muda a página, os números
  dos elementos mudam junto e o que você viu deixa de existir. Se a tarefa pede
  5 itens e você está vendo 2 que servem, anote os 2 AGORA;
- uma anotação por item, com TUDO que a tarefa pediu sobre ele (nome, preço,
  nota, link…). Item pela metade também vale — anote o que tem e siga;
- o caderno viaja em todo passo, logo abaixo desta lista, e é ELE que vira sua
  resposta no fim. `done` só precisa fechar o que já está anotado;
- e ele sobrevive ao fim ruim: se a tarefa for interrompida ou acabarem os
  passos, o que estiver anotado é entregue ao usuário assim mesmo. Um caderno
  com 3 itens vale infinitamente mais que uma tarefa "limpa" com zero;
- não anote a mesma coisa duas vezes, e não anote o que a tarefa não pediu.

NUNCA RECOMECE DO ZERO:
- se você já buscou, já filtrou ou já chegou na listagem, voltar para a página
  inicial para "fazer de novo" JOGA FORA todo o progresso e te devolve ao
  primeiro passo — normalmente para repetir o mesmo problema. Isso já queimou
  uma tarefa inteira (job fc47220e: três buscas do zero em 16 passos, nenhum
  produto coletado);
- página que parece vazia logo depois de um clique de busca quase sempre está
  CARREGANDO: use `wait_for` com uma palavra do resultado esperado, ou `look`.
  Não conclua que falhou;
- se um caminho não deu certo, mude o CAMINHO (outro filtro, ordenar, montar a
  URL na mão com `navigate`), não o ponto de partida.

Quando usar `look` (a imagem custa mais que o texto — peça com pontaria):
- **CONFERIR o que você acabou de fazer**, quando o efeito não aparece na lista
  de elementos: campo preenchido de verdade? o menu abriu? o item ficou
  selecionado? a aba trocou? Formulário é o caso clássico — o DOM diz que o
  campo existe, não que o seu texto entrou nele. Confira ANTES de enviar;
- **se situar** ao cair numa página desconhecida ou visualmente densa, antes de
  sair clicando: uma pergunta como "o que é esta tela e onde está o formulário
  de busca?" economiza cinco cliques errados;
- **quando a tela não muda** e você não sabe o motivo (ver a regra acima);
- **quando o snapshot for ambíguo**: dois elementos com o mesmo nome, um botão
  que você não sabe se está ativo, um `⟪CAIXA⟫` que você não sabe se embrulha o
  que quer;
- **antes de um clique caro** (enviar, confirmar, pagar, excluir): olhe e
  confirme que é o botão certo e que o formulário está como você pensa;
- **para LER o que só a imagem mostra** quando o dado que você precisa anotar
  não está no texto da página (preço desenhado, nota em estrelas): pergunte
  direto — "quais os 5 primeiros produtos e seus preços?" — e anote a resposta.

NÃO use `look` para:
- ler texto que já está no snapshot — para isso existe `extract` e a seção
  DENTRO DOS PAINÉIS QUE ROLAM, que traz até o que está abaixo da dobra;
- "dar uma olhada" sem pergunta em mente, dois passos seguidos, ou logo depois
  de um `navigate` que já trouxe a lista de elementos nova;
- descobrir se um vídeo está tocando — isso está na linha "Mídia na página".

- uma ação por vez, sempre baseada no que está na tela agora;
- se a página ainda está carregando, use wait_for em vez de chutar;
- termine com `done` assim que tiver a resposta — não navegue "para conferir";
- nunca invente URL de resultado; navegue e leia."""


class BrowserLocalDriver:
    """Dirige o navegador do usuário via canal de controle do device."""

    def __init__(
        self,
        *,
        act: Callable[[str, dict, float], Awaitable[dict]],
        llm: Any,
        model: str,
        max_steps: int = 25,
        blocked_domains: tuple[str, ...] = (),
        confirm_sensitive: bool = True,
        allow_sensitive: bool = False,
        allow_sensitive_label: str = "",
        tab_ref: int | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        max_screenshots: int = 3,
        vision_model: str | None = None,
        trace: Any = None,
        follow_download: bool | None = None,
        on_download: Callable[[list[dict]], Awaitable[None]] | None = None,
        headless: bool = False,
    ) -> None:
        self._act = act
        # Headless: mesma máquina, mesmo IP residencial, SEM janela.
        #
        # É o degrau que resolve bloqueio por origem sem o custo de tomar a tela
        # do usuário — o navegador do servidor sai por IP de datacenter na
        # França e a web BR recusa (medido 2026-09-20: CDN do portal de dados devolve 403
        # para o ASN inteiro, Google e DuckDuckGo servem CAPTCHA).
        #
        # Tudo daqui para baixo é idêntico nos dois modos: o vocabulário de
        # ações, o JS de página e a visão por passo falam CDP, e CDP é o mesmo
        # dos dois lados. Só o `claim` muda — e é ele que decide, pelo sinal do
        # `tab_ref`, para qual backend o resto das ações vai.
        self.headless = bool(headless)
        self.llm = llm
        self.model = model
        # Modelo COM visão. Vira None na primeira falha (print ou chamada) e a
        # tarefa segue no modelo de texto: cegueira é ruim, tarefa morta é pior.
        self.vision_model = vision_model or None
        self.max_steps = max(1, max_steps)
        self.blocked = tuple(d.lower().strip() for d in blocked_domains if d.strip())
        self.confirm_sensitive = confirm_sensitive
        self.allow_sensitive = allow_sensitive
        # Autorização AMARRADA a um clique específico (o que fez a tarefa
        # anterior parar). `allow_sensitive` sozinho é um cheque em branco pelo
        # resto do job: o usuário diz "pode" para excluir UM arquivo e o agente
        # fica livre para comprar, enviar e apagar o que quiser. Quando o job é
        # uma retomada, é este campo que vale — e só para este rótulo.
        self.allow_sensitive_label = (allow_sensitive_label or "").strip().lower()
        # Aba a reivindicar. None = abre uma nova (comportamento de sempre).
        self.tab_ref_pedido = tab_ref
        self.on_progress = on_progress
        self.max_screenshots = max_screenshots
        # Diário de bordo (core/browser_local/trace.py). Objeto burro e opcional:
        # quando é None, cada chamada vira um no-op via _log.
        self.trace = trace
        # None = decide pelo texto da tarefa em `run`; True/False = o chamador
        # já sabe (a tool pode passar explícito).
        self.follow_download = follow_download
        # Avisado no INSTANTE em que um arquivo novo aparece na pasta — não no
        # fim da tarefa. É o que permite registrar o artefato mesmo quando o job
        # morre no meio (o arquivo já está no disco do usuário de qualquer jeito).
        self.on_download = on_download

        self.steps: list[Step] = []
        self.shots: list[str] = []
        # O que a pasta de downloads GANHOU nesta tarefa. Evidência de fora da
        # página — é a única testemunha de um download, e a que mostra na hora
        # quando o mesmo arquivo veio duas vezes.
        self.downloaded: list[dict] = []
        # Pasta de downloads REAL do navegador dele, como o device reportou. Vem
        # em toda resposta de `browser.downloads` e era descartada aqui — sem ela
        # o registro do artefato teria nome e nenhum caminho, que é justamente o
        # que fazia o Neo remontar o caminho de cabeça no turno seguinte.
        self.downloads_dir: str = ""
        self._downloads_before: list[dict] = []
        # Nomes já anunciados por `on_download` — o modelo chama `downloads`
        # várias vezes na mesma tarefa e o registro não pode ganhar uma linha
        # por conferência.
        self._announced: set[str] = set()
        self._tab_ref: int | None = None
        self._allowed_hosts: set[str] = set()
        # Alguma página já carregou? Enquanto for False o escopo não tranca (ver
        # _check_navigation): antes da primeira página não há de onde vagar.
        self._navegou = False
        # Ligado quando um clique nosso disparou um download — é o que autoriza
        # a guarda a recusar o "Cancelar o download" depois.
        self._download_started = False
        # Arquivos NOVOS que já chegaram completos nesta tarefa, e se o modelo
        # já foi avisado de que mandar baixar de novo duplica.
        self._download_done: list[str] = []
        self._redownload_warned = False
        # Há um clique de download PENDENTE de conferência: disparamos um e
        # ainda não olhamos a pasta desde então. Enquanto isto estiver ligado, o
        # próximo clique de download é recusado — a única saída é chamar
        # `downloads`. Sem isto o agente CEGO clica em "Baixar" a esmo: no job
        # 0f87265f foram 10 cliques de download e apenas 2 conferências, uma
        # delas cedo demais, e o usuário ganhou 5 cópias de um APK de 52 MB.
        # A trava antiga (`_download_done`) só armava DEPOIS de uma conferência
        # bem-sucedida — ou seja, dependia justamente do que não acontecia.
        self._download_unverified = False
        # Arquivo que ficou vindo quando a tarefa acabou — é o que o runner
        # entrega ao job de vigia (core/jobs/runner_download_watch.py).
        self.download_pending = ""
        # Id desse mesmo download na barra do navegador, quando houve um. É o
        # que deixa o vigia casar o item sem depender do nome (que muda quando o
        # sufixo temporário cai) e ler falha/pausa direto do navegador.
        self.download_pending_id = ""
        # Relógio do último clique que dispara download. É o marco zero da
        # janela de nascimento: sem ele não dá para dizer se a pasta está vazia
        # porque o clique falhou ou porque o download ainda vai nascer.
        self._download_click_at: float = 0.0
        # Downloads que o NAVEGADOR confirmou ter aceitado, por id. Enquanto
        # houver um aqui, reclicar em "Baixar" é duplicar — e a trava não abre
        # mesmo depois de conferir.
        self._armados: dict[str, dict] = {}
        # Quem respondeu a última consulta: "browser" (barra do navegador) ou
        # "disk" (leitura da pasta, o sensor antigo). Só o primeiro sabe dizer
        # que um download FALHOU; com o segundo o veredito continua sendo por
        # presença de arquivo, como sempre foi.
        self._download_sensor: str = ""
        # Leitura anterior do termômetro de rede, para comparar contadores
        # dentro de uma mesma conferência (ver _pagina_trabalhando).
        self._rede_marco: dict | None = None
        # O site está montando o arquivo (zipando) e o download ainda vai nascer.
        # Trava o reclique com uma frase própria: dizer "você ainda não conferiu"
        # aqui seria mentira — o modelo acabou de conferir, e foi a conferência
        # que descobriu a preparação.
        self._preparando = False
        # Visão: por que caiu (se caiu) e quantas falhas passageiras seguidas.
        # Cota estourada é definitiva e desliga na hora; timeout do device é
        # passageiro e merece nova tentativa no passo seguinte.
        self._vision_down_reason: str | None = None
        # Já olhamos sozinhos NESTE episódio de travamento? Zera quando a tela
        # muda (ver _note_change) — uma imagem por episódio, não por passo.
        self._auto_looked = False
        # Fingerprints de tela em que o modelo JÁ pediu `look`. Olhar duas vezes
        # a mesma tela sem nada ter mudado é passo e imagem jogados fora — o
        # mesmo raciocínio da trava de `extract` repetido (`_reads_on_screen`).
        self._looked_on_screen: set[str] = set()
        # O caderno da tarefa: o que já foi COLETADO, em ordem. Sobrevive à
        # troca de página, à poda do histórico e — o que mais importa — ao fim
        # ruim: mesmo tarefa parada, estourada ou travada devolve o que achou.
        self.notes: list[str] = []
        # Passos que MEXERAM na página (não conta `look`/`note`) — é a régua da
        # cutucada de caderno vazio.
        self._acting_steps = 0
        self._vision_strikes = 0
        # Números que o device REALMENTE desenhou no último print. `None` = ainda
        # não fotografamos (ou device antigo, que não manda o campo); lista vazia
        # = print sem marcação nenhuma. Vai no prompt da visão para que ela não
        # tenha como citar um número que não existe.
        self._marks_refs: list[int] | None = None
        # `None` até o primeiro snapshot; depois True/False (ver `_snapshot`).
        self._device_novo: bool | None = None
        # Detector de passo inútil (ver _note_change).
        self._last_fingerprint: str | None = None
        self._pending_signature: str | None = None
        self._stuck_signature: str | None = None
        self._stuck = 0
        # Leituras (`extract`) já feitas NESTA tela. Zerado quando a tela muda.
        self._reads_on_screen: set[str] = set()

        # ── painéis abertos (ver _note_panels / _triage_panel) ───────────
        # Painéis abertos no ÚLTIMO snapshot visto, por identidade estável. O
        # diff contra ele responde a pergunta que o driver nunca soube fazer:
        # este painel apareceu por causa da MINHA ação, ou apareceu sozinho?
        self._panels_now: dict[str, dict] = {}
        # Veredito por painel: conteudo | intruso | consentimento | progresso |
        # menu. Vale enquanto o painel estiver na tela — triagem é cara e a
        # resposta não muda sozinha.
        self._panel_verdict: dict[str, dict] = {}
        # O painel que APARECEU neste snapshot (identidade), se algum.
        self._panel_new: str | None = None
        # Quantas vezes cada painel já abriu nesta tarefa. 2+ é vaivém: alguém
        # está abrindo e fechando a mesma coisa (7 vezes no job 6785cf70).
        self._panel_opens: dict[str, int] = {}
        # Última ação de atuação e o rótulo do alvo — a causa que a triagem
        # precisa para dizer "isto apareceu porque VOCÊ clicou aqui".
        self._last_act: tuple[str, str] = ("", "")
        # Triagens com visão já gastas nesta tarefa (teto: _MAX_TRIAGENS).
        self._triagens = 0
        # Quantas vezes já recusamos fechar cada painel (ver _validate).
        self._panel_refusals: dict[str, int] = {}

    # ── canal ────────────────────────────────────────────────────────────

    async def _call(self, action: str, args: dict | None = None) -> dict:
        short = action.replace("browser.", "")
        timeout = _STEP_TIMEOUTS.get(short, _DEFAULT_STEP_TIMEOUT)
        payload = dict(args or {})
        if self._tab_ref is not None:
            payload.setdefault("tab_ref", self._tab_ref)
        return await self._act(action, payload, timeout)

    def _log(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Grava no diário. Erro de log JAMAIS derruba a tarefa."""
        if self.trace is None:
            return None
        try:
            return getattr(self.trace, event)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[BrowserLocal] diário (%s) falhou: %s", event, exc)
            return None

    async def _progress(self, note: str) -> None:
        if self.on_progress:
            try:
                await self.on_progress(note)
            except Exception as exc:  # noqa: BLE001 — progresso nunca derruba o job
                logger.debug("[BrowserLocal] progresso falhou: %s", exc)

    async def _banner(self, text: str) -> None:
        """A faixa na tela do usuário. Melhor esforço: nunca derruba o passo."""
        try:
            await self._call("browser.banner", {"text": text, "active": True})
        except Exception as exc:  # noqa: BLE001
            logger.debug("[BrowserLocal] faixa falhou: %s", exc)

    # ── segurança ────────────────────────────────────────────────────────

    def _host_of(self, url: str) -> str:
        try:
            return (urlparse(url).hostname or "").lower()
        except ValueError:
            return ""

    def _is_blocked(self, url: str) -> bool:
        host = self._host_of(url)
        if not host:
            return False
        return any(host == d or host.endswith("." + d) for d in self.blocked)

    def _check_navigation(self, url: str) -> None:
        """Domínio proibido nunca; fora da allowlist do job, também não."""
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("só navego para http(s)")
        if self._is_blocked(url):
            raise ValueError(
                f"'{self._host_of(url)}' está na lista de domínios proibidos — não entro lá"
            )
        # A PRIMEIRA navegação passa sempre. O escopo existe para o agente não
        # sair vagando DEPOIS de chegar onde devia; antes de a primeira página
        # carregar não há de onde vagar, e travar aqui produz um job que não faz
        # absolutamente nada — foi o que aconteceu com três jobs seguidos em
        # 2026-09-01, mortos em `about:blank`. A lista de domínios PROIBIDOS
        # (`_is_blocked`, acima) é a fronteira de verdade e continua valendo
        # incondicionalmente; esta aqui é palpite tirado do texto da tarefa, e
        # palpite não tranca a porta antes de a tarefa começar.
        host = self._host_of(url)
        if self._allowed_hosts and host and host not in self._allowed_hosts:
            if not self._navegou:
                logger.info(
                    "[BrowserLocal] escopo era %s; primeira navegação para %s liberada",
                    sorted(self._allowed_hosts), host,
                )
                self._allowed_hosts.add(host)
                return
            allowed = ", ".join(sorted(self._allowed_hosts))
            raise ValueError(
                f"'{host}' está fora do escopo desta tarefa (só {allowed}). "
                "Se era pra ir lá, me peça de novo dizendo o site."
            )

    async def _aba_ainda_serve(self, tab_ref: int) -> bool:
        """A aba que a tarefa anterior deixou ainda existe e está utilizável?

        Confiar no id sem conferir seria pior do que não reaproveitar: o usuário
        fecha aba, troca de janela e navega para outro lugar o tempo todo, e um
        claim numa aba morta derruba o job inteiro em vez de simplesmente abrir
        uma nova. Aqui o `não` é barato — cai para aba nova e a tarefa segue.
        """
        try:
            resposta = await self._call(
                "browser.tabs", {"mode": "headless"} if self.headless else {}
            )
        except Exception as exc:  # noqa: BLE001 — sem lista, o seguro é aba nova
            self._log("note", "não consegui listar as abas", f"{exc} — abro uma nova")
            return False
        abas = resposta.get("tabs") or []
        if isinstance(abas, dict):  # o backend pode embrulhar em {tabs: [...]}
            abas = abas.get("tabs") or []
        for aba in abas:
            if not isinstance(aba, dict):
                continue
            if aba.get("tabId") != tab_ref and aba.get("id") != tab_ref:
                continue
            url = str(aba.get("url") or "")
            if self._is_blocked(url):
                self._log("note", "a aba anterior foi para domínio proibido", "abro uma nova")
                return False
            self._log("note", f"reaproveitando a aba {tab_ref}", url or "(sem url)")
            return True
        self._log("note", f"a aba {tab_ref} não existe mais", "abro uma nova")
        return False

    def _check_sensitive(self, label: str) -> None:
        if not self.confirm_sensitive:
            return
        if not label or not _SENSITIVE.search(label):
            return
        # Retomada: libera SÓ o clique que o usuário autorizou. Qualquer outra
        # ação irreversível volta a parar e perguntar, mesmo dentro do job já
        # autorizado.
        if self.allow_sensitive_label:
            if label.strip().lower()[:120] == self.allow_sensitive_label[:120]:
                return
            raise NeedsConfirmation(label.strip()[:120])
        if self.allow_sensitive:
            return
        raise NeedsConfirmation(label.strip()[:120])

    # ── percepção ────────────────────────────────────────────────────────

    async def _snapshot(self) -> dict:
        snapshot = await self._call("browser.snapshot", {})
        if snapshot.get("stop") is True:
            raise StopRequested()
        # O core faz deploy sozinho (Mutagen + --reload); o device só depois de
        # reiniciar o `tauri dev`. Nesse intervalo o core sabe ações que o
        # navegador ainda não implementa — e "ação de navegador desconhecida"
        # num laço de 25 passos é o pior jeito de descobrir isso.
        #
        # O campo `main` existe SEMPRE no device novo (vale `null` quando a
        # página não tem conteúdo principal identificável) e não existe no
        # antigo: é o carimbo de versão que já está em toda resposta.
        if self._device_novo is None:
            self._device_novo = "main" in snapshot
            if not self._device_novo:
                self._log(
                    "note",
                    "device antigo",
                    "o snapshot não traz `main` — o neo-desktop está rodando o Rust "
                    "de antes; `click_text` e a percepção de controle escondido não "
                    "existem nele. Reinicie o `tauri dev`.",
                )
        return snapshot

    @staticmethod
    def _element_line(
        item: dict, filhos: dict[int, list[int]] | None = None, origem: str = ""
    ) -> str:
        tag = item.get("tag", "?")
        kind = f"({item['type']})" if item.get("type") else ""
        text = item.get("text") or ""
        extra = ""
        if item.get("value"):
            extra += f' = "{item["value"]}"'
        if item.get("checked") is not None:
            extra += " [marcado]" if item["checked"] else " [desmarcado]"
        # O elemento é o ROSTO de um controle escondido por CSS (o caso do rádio
        # custom: `<input>` com opacity:0 e um `<label>` desenhado por cima).
        # Dizer isso deixa explícito que clicar ali É marcar a opção.
        if item.get("controla"):
            extra += f' [seleciona {item["controla"]}]'
        if item.get("disabled"):
            extra += " [desabilitado]"
        if item.get("options"):
            extra += f" opções: {', '.join(item['options'][:8])}"
        # O que salva o "Mais ações" nº 33 de ser igual ao nº 34: o nome da
        # LINHA a que ele pertence. Sem isto uma lista de 70 arquivos vira
        # 70 botões indistinguíveis e o modelo clica no mesmo para sempre.
        if item.get("owner"):
            extra += f' em "{item["owner"]}"'
        # De que contêiner aberto este item veio. "Fechar" num menu de escolha e
        # "Fechar" no painel de download são coisas MUITO diferentes.
        if item.get("panel") and item.get("panel") != text:
            extra += f' [painel: "{item["panel"]}"]'
        # Tamanho. Com a visão desligada é a ÚNICA pista de forma que sobra: um
        # controle tem dezenas de px, uma caixa em volta de uma lista tem
        # centenas. Sem isso o modelo tratou um contêiner de 1174×188 como se
        # fosse o botão da pasta (job a0eab88c).
        box = item.get("box")
        if isinstance(box, list) and len(box) == 4:
            extra += f" {int(box[2])}×{int(box[3])}px"
        # A informação mais importante da linha, por isso vem por último e em
        # caixa alta: clicar aqui não faz nada, e o driver vai recusar.
        if item.get("grupo"):
            # Apontar os refs de dentro é a diferença entre "não clique aqui" e
            # "clique ali": sem os números o modelo continua sem alvo e volta a
            # tentar a caixa com outra ação.
            alvos = (filhos or {}).get(item.get("ref"), [])
            onde = f" → use {', '.join(f'[{r}]' for r in alvos[:8])}" if alvos else ""
            extra += (
                f" ⟪CAIXA com {item['grupo']} itens dentro — não é botão{onde}⟫"
            )
        # O ENDEREÇO do link. O device já mandava (`item.href`) e esta linha
        # jogava fora — então "colete o link de cada item", que é metade das
        # tarefas de coleta na web, era impossível por construção: o modelo ou
        # inventava (`/dp/B07...`, medido em 2026-09-03) ou gastava um passo
        # entrando em cada item para ler a barra de endereços.
        #
        # Encurtado de propósito: mesma origem vira só o caminho, e o corte em
        # 100 chars tira o rastro de tracking sem perder o que identifica a
        # página. São até 200 elementos por snapshot — link inteiro aqui
        # dobraria o prompt de toda página de listagem.
        href = item.get("href")
        if href:
            curto = str(href)
            if origem and curto.startswith(origem):
                curto = curto[len(origem):] or "/"
            extra += f" → {curto[:100]}"
        if item.get("fora"):
            extra += " [fora da tela — role antes de clicar]"
        # Alguma coisa NA FRENTE dele. O clique é despachado direto no nó, então
        # ele "funciona" mesmo coberto e devolve ok — a mesma mentira do
        # `ok:true ≠ a página reagiu`, por outra porta.
        if item.get("coberto"):
            # QUEM cobre muda o conselho. Painel aberto na frente não é um
            # obstáculo a remover: quase sempre o que interessa está DENTRO dele.
            # Antes as duas situações saíam com a mesma frase, e foi assim que o
            # modelo passou 11 passos tentando fechar a própria resposta.
            if item.get("coberto_painel"):
                extra += (
                    f' ⟪atrás do painel aberto "{str(item["coberto"])[:40]}" — o que '
                    "interessa provavelmente está DENTRO dele, não aqui⟫"
                )
            else:
                extra += f' ⟪COBERTO por "{str(item["coberto"])[:40]}" — não dá para clicar⟫'
        if item.get("chrome"):
            extra += " [moldura do site]"
        # Painel que rola: quanto já rolou, de quanto. É o que diz ao modelo que
        # existe conteúdo abaixo da dobra — e QUAL ref rolar (ele passou 21
        # passos mandando rolar o botão da aba ao lado do painel).
        rola = item.get("rola")
        if isinstance(rola, list) and len(rola) == 3:
            y, altura, janela = (int(v) for v in rola)
            extra += f" ⟪ROLA: {y} de {max(0, altura - janela)}⟫"
        return f'  [{item.get("ref")}] {tag}{kind} "{text}"{extra}'

    @staticmethod
    def _render(snapshot: dict, vereditos: dict | None = None) -> str:
        lines = [
            f"URL: {snapshot.get('url', '')}",
            f"Título: {snapshot.get('title', '')}",
        ]
        scroll = snapshot.get("scroll") or {}
        if scroll:
            lines.append(f"Rolagem: y={scroll.get('y', 0)} de {scroll.get('height', 0)}")

        # Estado da mídia — perto do topo de propósito: quando a tarefa é sobre
        # tocar/pausar, é a única linha que importa, e o modelo não pode ter que
        # caçá-la no meio de 74 elementos.
        midia = snapshot.get("midia")
        if isinstance(midia, dict):
            estado = "TOCANDO AGORA" if midia.get("tocando") else "PARADO (pausado)"
            mudo = ", SEM SOM (mudo)" if midia.get("mudo") else ""
            lines.append(
                f"Mídia na página: {midia.get('tipo') or 'video'} {estado}{mudo} — "
                f"{midia.get('pos') or '?'} de {midia.get('dur') or '?'}. "
                "ESTA linha é o estado real, lido do próprio player."
            )

        elements = snapshot.get("elements") or []

        # Origem da página atual: link do MESMO site vira só o caminho na
        # listagem (`/dp/B08K1` em vez do endereço inteiro com tracking).
        origem = ""
        try:
            partes = urlparse(str(snapshot.get("url") or ""))
            if partes.scheme and partes.netloc:
                origem = f"{partes.scheme}://{partes.netloc}"
        except Exception:  # noqa: BLE001 — URL estranha não pode derrubar o render
            origem = ""

        # Quem está dentro de qual caixa (o device manda `dentro` = ref do pai).
        # Montado uma vez para as duas listagens abaixo.
        filhos: dict[int, list[int]] = {}
        for item in elements:
            pai = item.get("dentro")
            if pai is not None and not item.get("grupo"):
                filhos.setdefault(pai, []).append(item.get("ref"))

        # Menu/diálogo aberto sobe para o topo. Enterrado no fim de uma lista de
        # 170 elementos — 50 deles chamados "Mais ações" — o item que interessa
        # some: em 2026-08-29 o menu do Drive estava aberto com "Baixar" na
        # lista e o modelo reabriu o menu três vezes até o job morrer.
        opened = [item for item in elements if item.get("menu")]
        if opened:
            # O VEREDITO manda. Antes esta seção descrevia o painel e deixava o
            # modelo concluir o que fazer — e ele concluía "fechar", porque o
            # resto do prompt (⟪COBERTO por⟫, "a tela NÃO mudou") empurrava para
            # lá. Agora quem decidiu foi o driver, e a seção diz a decisão.
            paineis = BrowserLocalDriver._panels(snapshot)

            def _area(par) -> float:
                box = par[1].get("box") or [0, 0, 0, 0]
                return float(box[2]) * float(box[3])

            veredito = None
            rotulo = ""
            # O MAIOR painel manda — mesmo critério do diff. Sem ordenar, com
            # dois painéis abertos o veredito exibido dependeria da ordem em que
            # o device enumerou os elementos.
            for chave, painel in sorted(paineis.items(), key=_area, reverse=True):
                achado = (vereditos or {}).get(chave)
                if achado:
                    veredito, rotulo = achado, painel.get("rotulo") or ""
                    break
            tipo = (veredito or {}).get("tipo") or ""
            porque = (veredito or {}).get("porque") or ""

            if tipo == "conteudo":
                lines.append(
                    f'\n>>> O PAINEL «{rotulo[:60]}» ABRIU E É CONTEÚDO — {porque}.\n'
                    "NÃO feche, NÃO aperte Escape, NÃO clique de novo no que o abriu. Ele é o "
                    "RESULTADO do que você pediu: a resposta está AQUI DENTRO. Leia o texto "
                    "dele abaixo; se precisar de mais, use `extract` no ref do painel. Os "
                    "elementos marcados ⟪atrás do painel⟫ estão embaixo dele de propósito — "
                    "não são o seu alvo agora."
                )
            elif tipo in ("intruso", "consentimento"):
                fechar = (veredito or {}).get("fechar") or ""
                como = (
                    f' Feche por `click_text` com "{fechar[:60]}".'
                    if fechar
                    else " Feche pelo botão de dispensar/aceitar dele."
                )
                lines.append(
                    f'\n>>> O PAINEL «{rotulo[:60]}» É {tipo.upper()} — {porque}.\n'
                    f"Ele não tem nada a ver com a tarefa e está atrapalhando.{como}"
                )
            elif tipo == "progresso":
                lines.append(
                    f'\n>>> O PAINEL «{rotulo[:60]}» É PROGRESSO DE DOWNLOAD — {porque}.\n'
                    "Não é menu e não é obstáculo: não feche e NÃO cancele. Espere com "
                    "`downloads`."
                )
            else:
                lines.append(
                    f"\n>>> MENU/DIÁLOGO ABERTO AGORA ({len(opened)} itens). Se for um menu de "
                    "escolha, a próxima ação está AQUI DENTRO — clicar de novo no que o abriu só "
                    "fecha e reabre. Se for um painel de PROGRESSO (download em andamento), não é "
                    "menu nenhum: ignore, não feche e não cancele — espere com `downloads`:"
                )
            for item in opened:
                lines.append(BrowserLocalDriver._element_line(item, filhos, origem))

            # O TEXTO do painel, inteiro. É a linha que faltava: sem ela o modelo
            # lia um rótulo cortado em ~120 chars e ia procurar do lado de fora
            # a informação que estava dentro (job 6785cf70 — a timeline de
            # rastreio nunca apareceu em nenhum dos 25 prompts).
            for item in opened:
                if not item.get("raiz"):
                    continue
                dentro = str(item.get("texto") or "").strip()
                if len(dentro) < 20:
                    continue
                nome = str(item.get("panel") or item.get("text") or "painel")[:60]
                lines.append(
                    f'\n>>> TEXTO COMPLETO DO PAINEL «{nome}» '
                    f"(ref {item.get('ref')}) — é isto que está escrito nele:\n{dentro}"
                )

        lines.append(f"\nElementos ({len(elements)}):")
        for item in elements:
            lines.append(BrowserLocalDriver._element_line(item, filhos, origem))
        if snapshot.get("truncated"):
            lines.append("  … (lista cortada; role a página para ver o resto)")

        # Texto de dentro dos painéis que rolam. Vem ANTES do texto da página de
        # propósito: é conteúdo que o print NÃO mostra (está abaixo da dobra do
        # painel) e que costuma ser exatamente a resposta. `innerText` não para
        # na borda visível — foi assim que "qual o tamanho do arquivo" deixou de
        # exigir rolagem nenhuma (job 9025d9f8, 21 passos rolando à toa).
        paineis = [i for i in elements if i.get("texto")]
        if paineis:
            lines.append(
                f"\n>>> DENTRO DOS PAINÉIS QUE ROLAM ({len(paineis)}) — isto inclui o que está "
                "ABAIXO da dobra e por isso NÃO aparece no print. Se a resposta estiver aqui, "
                "não role: responda."
            )
            for item in paineis[:4]:
                corpo = str(item.get("texto") or "").strip()
                lines.append(f'\n[{item.get("ref")}] "{item.get("text")}":\n{corpo}')

        # CONTEÚDO PRINCIPAL antes do texto do corpo. `body.innerText` sai em
        # ordem de documento e todo site põe a navegação primeiro: no job
        # 9968e68a os 3000 caracteres de orçamento viraram o menu lateral, o
        # cabeçalho e os metadados da prova, e o bloco "Alternativas" — que ERA
        # a tarefa — ficou depois do corte. Nada é subtraído (recortar "só a
        # área da tarefa" já jogou a resposta fora no job 9025d9f8): o principal
        # é ADICIONADO na frente, com orçamento próprio.
        main = snapshot.get("main")
        if isinstance(main, dict) and (main.get("text") or "").strip():
            titulo = str(main.get("label") or "").strip()
            onde = f' — "{titulo[:60]}"' if titulo else ""
            lines.append(
                f"\n>>> CONTEÚDO PRINCIPAL DA PÁGINA{onde} (fora do menu e do cabeçalho):\n"
                + str(main["text"]).strip()
            )

        text = (snapshot.get("text") or "").strip()
        # Com o principal já renderizado acima, repetir as mesmas linhas no
        # texto do corpo é orçamento jogado fora. Isto NÃO é o recorte que
        # queimou o job 9025d9f8: nada some do prompt — cada linha continua lá,
        # uma vez só. A rede de segurança é o piso de 200 caracteres: se a
        # deduplicação esvaziar o corpo, ele volta inteiro.
        if isinstance(main, dict) and (main.get("text") or "").strip() and text:
            ja = {linha.strip() for linha in str(main["text"]).splitlines() if linha.strip()}
            sobrou = "\n".join(
                linha for linha in text.splitlines() if linha.strip() not in ja
            ).strip()
            if len(sobrou) >= 200:
                text = sobrou
        if text:
            # Corte silencioso é armadilha: numa lista alfabética o item pedido
            # pode estar logo depois do 2500º caractere, e sem o aviso o modelo
            # conclui que ele não existe em vez de rolar a página.
            aviso = "\n… (texto cortado — role a página para ver o resto)" if len(text) > 2500 else ""
            lines.append(f"\nTexto da página:\n{text[:2500]}{aviso}")
        return "\n".join(lines)

    @staticmethod
    def _fingerprint(snapshot: dict) -> str:
        """Assinatura da tela — muda quando a página muda, e só então."""
        elements = snapshot.get("elements") or []
        payload = "|".join(
            (
                str(snapshot.get("url") or ""),
                str(snapshot.get("title") or ""),
                str(len(elements)),
                "·".join(str(item.get("text") or "") for item in elements[:80]),
                # SELEÇÃO. Marcar uma alternativa não muda URL, nem título, nem
                # rótulo, nem texto — então o freio de "a tela não mudou" dava o
                # passo por inútil justamente quando ele tinha funcionado, e a
                # recusa seguinte impedia o modelo de tentar de novo. O estado
                # dos controles (e o disabled do botão de enviar, que costuma
                # mudar junto) entra na assinatura.
                "·".join(
                    f"{item.get('ref')}={item.get('checked')}{item.get('disabled')}"
                    for item in elements[:120]
                    if item.get("checked") is not None or item.get("disabled")
                ),
                # PAINEL ABERTO. Sem esta linha, abrir um modal era invisível
                # para o freio de "a tela não mudou" — e o freio então MENTIA na
                # direção mais cara possível, dizendo "sua ação não funcionou"
                # justamente quando ela tinha funcionado. Job 6785cf70: 16 passos
                # carimbados "NÃO mudou" com o modal escancarado na tela.
                "·".join(
                    f"{item.get('panel') or ''}~{str(item.get('texto') or '')[:120]}"
                    for item in elements
                    if item.get("menu")
                ),
                # O FIM da lista, não só o começo. Os `elements[:80]` acima são o
                # cabeçalho do site em qualquer página grande, e todo DOM novo —
                # modal, toast, dropdown, autocomplete — nasce no FIM do corpo.
                # Numa página que satura o teto de 200 elementos, `len(elements)`
                # também não mexe (200 antes, 200 depois), então sem esta linha
                # não sobra um único canal capaz de ver a mudança.
                "·".join(str(item.get("text") or "") for item in elements[-40:]),
                str(snapshot.get("truncated")),
                (snapshot.get("text") or "")[:1500],
            )
        )
        return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _signature(action: str, args: dict) -> str:
        return f"{action}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"

    @staticmethod
    def _panels(snapshot: dict) -> dict[str, dict]:
        """Painéis abertos AGORA, por identidade ESTÁVEL.

        A chave não pode ser o `ref`: ele migra entre snapshots sem o painel ter
        mudado (o mesmo modal da Amazon foi 188 num passo e 189 no seguinte, e
        foi por isso que o modelo ficou clicando em "Close" de números
        diferentes achando que estava tentando coisas diferentes). Rótulo +
        tamanho arredondado sobrevive ao redesenho e ainda separa dois painéis
        distintos na mesma tela.

        `raiz` é o painel em si (device novo). Sem ele — desktop desatualizado —
        cai para agrupar por `panel`, que existe desde antes: o veredito fica
        pior, mas o diff continua funcionando.
        """
        elements = snapshot.get("elements") or []
        out: dict[str, dict] = {}
        for item in elements:
            if not item.get("raiz"):
                continue
            rotulo = str(item.get("panel") or item.get("text") or "").strip()
            box = item.get("box") or []
            tamanho = (
                f"{int(box[2])}x{int(box[3])}"
                if len(box) == 4 and all(isinstance(v, (int, float)) for v in box)
                else "?"
            )
            out[f"{rotulo[:60]}|{tamanho}"] = {
                "rotulo": rotulo,
                "ref": item.get("ref"),
                "box": box if len(box) == 4 else None,
                "texto": str(item.get("texto") or item.get("text") or ""),
                "tipo": str(item.get("type") or ""),
            }
        if out:
            return out
        # Device antigo: nenhum `raiz`. Agrupa pelos itens que se declaram dentro
        # de um painel e usa o rótulo dele como identidade.
        for item in elements:
            if not item.get("menu"):
                continue
            rotulo = str(item.get("panel") or "").strip()
            if not rotulo:
                continue
            out.setdefault(
                f"{rotulo[:60]}|?",
                {"rotulo": rotulo, "ref": None, "box": None, "texto": "", "tipo": ""},
            )
        return out

    def _triage_cheap(self, painel: dict, *, depois_de_acao: bool) -> dict | None:
        """Classifica o painel SEM gastar visão. `None` = não sei, chame os olhos.

        A ordem importa e não é arbitrária:

        1. menu de escolha — o DOM já lista os itens, não há nada visual a
           resolver;
        2. progresso de download — regra que já existia em prosa no prompt, aqui
           virando veredito, que é o que a guarda consegue usar;
        3. léxico de intruso/consentimento — cookie e newsletter se anunciam por
           escrito, e um site pode abrir um desses no seu primeiro clique, então
           o texto vem ANTES da causalidade;
        4. **causalidade** — painel que aparece junto com a SUA ação é resultado.
           Propaganda não espera você clicar em "Ver todas as atualizações": ela
           entra no load, no timer, no scroll, no exit-intent. É o discriminador
           mais barato que existe e é o que teria resolvido o job 6785cf70
           inteiro sem uma única chamada de visão.
        """
        texto = painel.get("texto") or ""
        rotulo = painel.get("rotulo") or ""
        alvo = f"{rotulo}\n{texto}"

        if painel.get("tipo") == "menu":
            return {"tipo": "menu", "porque": "é um menu de escolha", "fonte": "dom"}
        if _PROGRESSO.search(alvo):
            return {
                "tipo": "progresso",
                "porque": "o texto é de download/exportação em andamento",
                "fonte": "dom",
            }
        if _INTRUSO.search(alvo):
            return {
                "tipo": "intruso",
                "porque": "o texto é de propaganda/consentimento/cadastro",
                "fonte": "dom",
            }
        if depois_de_acao:
            acao, alvo_acao = self._last_act
            causa = f"{acao} em «{alvo_acao[:40]}»" if alvo_acao else acao
            return {
                "tipo": "conteudo",
                "porque": f"apareceu junto com a sua ação ({causa})",
                "fonte": "causa",
            }
        return None

    def _note_panels(self, snapshot: dict, *, depois_de_acao: bool) -> None:
        """Diff dos painéis abertos entre dois snapshots.

        É o gatilho da triagem e, sozinho, já é meia solução: saber que o painel
        NASCEU da sua própria ação responde a pergunta que o driver não sabia
        fazer. Até aqui um modal que você abriu e um banner de cookie que apareceu
        sozinho eram literalmente o mesmo objeto no prompt.
        """
        agora = self._panels(snapshot)
        antes, self._panels_now = self._panels_now, agora  # `antes` é o passo anterior
        self._panel_new = None

        for chave in list(antes):
            if chave in agora:
                continue
            self._panel_verdict.pop(chave, None)
            rotulo = (antes[chave].get("rotulo") or "painel")[:40]
            self._log("panel", "fechou", rotulo, "")
            if self.steps and depois_de_acao:
                last = self.steps[-1]
                last.detail = (
                    f"{last.detail} — e FECHOU o painel «{rotulo}»"
                    if last.detail
                    else f"FECHOU o painel «{rotulo}»"
                )

        novos = [chave for chave in agora if chave not in antes]
        if not novos:
            return
        # O maior painel ganha: quando abrem dois de uma vez (modal + overlay de
        # fundo), quem interessa é o que tem conteúdo.
        def area(chave: str) -> float:
            box = agora[chave].get("box") or [0, 0, 0, 0]
            return float(box[2]) * float(box[3])

        chave = max(novos, key=area)
        self._panel_new = chave
        self._panel_opens[chave] = self._panel_opens.get(chave, 0) + 1
        rotulo = (agora[chave].get("rotulo") or "painel")[:40]

        veredito = self._triage_cheap(agora[chave], depois_de_acao=depois_de_acao)
        if veredito:
            self._panel_verdict[chave] = veredito
            self._log("panel", "abriu", rotulo, f"{veredito['tipo']} — {veredito['porque']}")
        else:
            self._log("panel", "abriu", rotulo, "ambíguo — vai para a triagem visual")

        if self.steps and depois_de_acao:
            last = self.steps[-1]
            last.detail = (
                f"{last.detail} — e ABRIU o painel «{rotulo}»"
                if last.detail
                else f"ABRIU o painel «{rotulo}»"
            )

    async def _triage_panel(self, snapshot: dict) -> None:
        """Os OLHOS decidem o que a camada barata não decidiu.

        Só o painel ambíguo chega aqui: o que apareceu sozinho, sem léxico que o
        entregue. É o caso que o DOM genuinamente não resolve — o interstitial
        que é uma imagem só, sem texto nenhum.

        O print sai RECORTADO no painel (`clip`). A tela inteira é o pior insumo
        para "o que está escrito aqui dentro": o print normal sai a scale 0.75 e
        qualidade 60 de uma tela de 2954px, e um modal de 600px vira borrão.
        """
        chave = self._panel_new
        if not chave or chave in self._panel_verdict:
            return
        painel = self._panels_now.get(chave) or {}
        rotulo = (painel.get("rotulo") or "painel")[:40]

        if not self.vision_model or self._triagens >= _MAX_TRIAGENS:
            # Sem olhos (ou sem orçamento) o painel não vira "intruso" por
            # omissão: fechar a resposta é o erro caro, e é o erro que este
            # trabalho inteiro existe para não cometer.
            self._panel_verdict[chave] = {
                "tipo": "conteudo",
                "porque": (
                    "não deu para triar (sem visão)" if not self.vision_model
                    else f"não deu para triar (já usei as {_MAX_TRIAGENS} triagens)"
                ),
                "fonte": "default",
            }
            self._log("panel", "triagem pulada", rotulo, self._panel_verdict[chave]["porque"])
            return

        self._triagens += 1
        shot = await self._shot(clip=painel.get("box"))
        if not shot:
            self._panel_verdict[chave] = {
                "tipo": "conteudo",
                "porque": "não deu para triar (o print não veio)",
                "fonte": "default",
            }
            return
        # O print do painel vale mais que um print de passo qualquer: é a única
        # imagem que mostra ao usuário o que apareceu na tela dele.
        if len(self.shots) < self.max_screenshots:
            self.shots.append(shot)

        acao, alvo = self._last_act
        contexto = (
            f"A última ação do agente foi `{acao}`" + (f' em "{alvo[:60]}"' if alvo else "")
            if acao
            else "O agente não fez nada antes deste painel aparecer — ele surgiu sozinho"
        )
        veredito = await self._triage_vision(
            f'{contexto}. Classifique o painel "{rotulo}" desta imagem.', shot
        )
        self._panel_verdict[chave] = veredito
        self._log("panel", "triagem", rotulo, f"{veredito['tipo']} — {veredito.get('porque', '')}")
        # Painel que é conteúdo e cuja transcrição veio: vai para o caderno na
        # hora. É o que sobrevive ao painel fechar, à poda do histórico e ao job
        # morrer no meio.
        texto = str(veredito.get("texto") or "").strip()
        if veredito["tipo"] == "conteudo" and len(texto) >= 40:
            self.notes.append(f"painel «{rotulo}»: {texto[:400]}")
            self._log("note", f"anotou ({len(self.notes)})", f"do painel «{rotulo}»")

    async def _triage_vision(self, pedido: str, shot: str) -> dict:
        """Uma chamada ao modelo de visão que devolve VEREDITO, não prosa."""
        from shared.types import Message, MessageRole

        started = time.monotonic()
        raw = ""
        try:
            provider = self.llm.get_provider_for_model(self.vision_model)
            response = await provider.complete(
                messages=[
                    Message(role=MessageRole.SYSTEM, content=_TRIAGE_SYSTEM),
                    Message(role=MessageRole.USER, content=pedido, images=[shot]),
                ],
                model=self.vision_model,
                temperature=0.1,
                max_tokens=700,
            )
            raw = (response.get_content() or "").strip()
        except Exception as exc:  # noqa: BLE001 — triagem que falha não mata a tarefa
            logger.info("[BrowserLocal] triagem de painel falhou: %s", exc)
            self._log("vision", self.vision_model or "?", (time.monotonic() - started) * 1000,
                      pedido, f"FALHOU: {exc}")
            return {"tipo": "conteudo", "porque": f"a triagem falhou ({exc})", "fonte": "erro"}

        self._log("vision", self.vision_model, (time.monotonic() - started) * 1000, pedido, raw)
        try:
            bloco = re.search(r"\{.*\}", raw, re.DOTALL)
            dados = json.loads(bloco.group(0)) if bloco else {}
        except (json.JSONDecodeError, AttributeError):
            dados = {}
        tipo = str(dados.get("tipo") or "").strip().lower()
        if tipo not in ("conteudo", "intruso", "consentimento", "progresso"):
            # JSON ilegível vira "conteúdo" pelo mesmo motivo do default acima:
            # o erro que custa caro é fechar a resposta.
            return {
                "tipo": "conteudo",
                "porque": "a triagem não devolveu um tipo válido",
                "fonte": "erro",
                "texto": raw[:600],
            }
        return {
            "tipo": tipo,
            "porque": str(dados.get("porque") or "")[:200],
            "texto": str(dados.get("texto") or "")[:_PANEL_MAX_CHARS],
            "fechar": str(dados.get("fechar") or "")[:80],
            "fonte": "visao",
        }

    def _conteudo_na_tela(self) -> bool:
        """Há na tela um painel que a triagem julgou ser a RESPOSTA?"""
        return any(
            (self._panel_verdict.get(chave) or {}).get("tipo") == "conteudo"
            for chave in self._panels_now
        )

    def _fecharia_painel(
        self, action: str, args: dict, snapshot: dict
    ) -> tuple[str, str] | None:
        """Esta ação está tentando FECHAR um painel aberto? Qual?

        Três formas de fechar, e o modelo usou todas as três no mesmo job:
        `Escape`, clicar num botão cujo rótulo é de fechar, e clicar no próprio
        painel. Reconhecer a intenção é o que permite recusá-la quando o painel é
        a resposta — e só isso: a guarda nunca fecha nada sozinha.
        """
        if not self._panels_now:
            return None

        def primeiro() -> tuple[str, str]:
            chave = next(iter(self._panels_now))
            return chave, str(self._panels_now[chave].get("rotulo") or "painel")

        if action == "press" and str(args.get("key") or "").lower() == "escape":
            return primeiro()
        if action == "click_text":
            return primeiro() if _FECHA.match(str(args.get("texto") or "")) else None
        if action not in ("click", "dblclick", "right_click"):
            return None

        reference = args.get("ref")
        alvo = next(
            (i for i in (snapshot.get("elements") or []) if i.get("ref") == reference),
            None,
        )
        if not alvo or not alvo.get("menu"):
            return None
        rotulo_painel = str(alvo.get("panel") or "")
        chave = next(
            (
                k
                for k, v in self._panels_now.items()
                if v.get("rotulo") == rotulo_painel or v.get("ref") == reference
            ),
            None,
        )
        if chave is None:
            return None
        nome = str(self._panels_now[chave].get("rotulo") or "painel")
        # O painel INTEIRO como alvo (o modelo tentou isso duas vezes) ou um
        # botão cujo rótulo é de fechar.
        if alvo.get("raiz") or _FECHA.match(str(alvo.get("text") or "")):
            return chave, nome
        return None

    def _note_change(self, snapshot: dict) -> None:
        """A tela mudou depois do último passo de atuação?

        Este é o freio que faltava. `click` devolve `ok:true` porque o evento
        foi despachado — não porque a página reagiu. Em 2026-08-28 isso deixou
        o driver clicar 25 vezes no mesmo botão do Drive com o histórico
        lendo "25 sucessos". Aqui o passo inútil é carimbado no histórico e a
        repetição literal passa a ser recusada em :meth:`_validate`.
        """
        fingerprint = self._fingerprint(snapshot)
        previous, self._last_fingerprint = self._last_fingerprint, fingerprint
        signature, self._pending_signature = self._pending_signature, None

        # Diff de painéis ANTES de qualquer early-return: um painel que abre é a
        # informação mais importante do passo, e ela não pode depender de o
        # passo anterior ter sido de atuação. `depois_de_acao` é a causalidade —
        # o discriminador que separa "modal que eu abri" de "banner que apareceu
        # sozinho".
        self._note_panels(snapshot, depois_de_acao=signature is not None)

        # Leitura só se repete quando há tela nova para ler. Fica ANTES do
        # early-return de propósito: `extract` não é ação de atuação, então nada
        # o cobria — e o mesmo `extract {"ref": 51}` rodou 7 vezes seguidas
        # devolvendo a mesma palavra ("Detalhes") no job f27970d6.
        if previous is None or fingerprint != previous:
            self._reads_on_screen.clear()
            # Tela nova = episódio de travamento encerrado; o olhar automático
            # volta a estar disponível para o PRÓXIMO travamento.
            self._auto_looked = False
            self._looked_on_screen.clear()

        if signature is None:  # o passo anterior não era de atuação
            return
        if previous is None or fingerprint != previous:
            self._stuck = 0
            self._stuck_signature = None
            self._log("changed", True, 0)
            return

        self._stuck += 1
        self._log("changed", False, self._stuck)
        self._stuck_signature = signature
        if self.steps:
            last = self.steps[-1]
            last.detail = (
                f"{last.detail} — mas a tela NÃO mudou" if last.detail else "a tela NÃO mudou"
            )

    def _absorb_listing(self, listing: dict) -> list[dict]:
        """Guarda a pasta reportada pelo device e carimba ``path`` em cada item.

        O device manda ``{ok, dir, count, files:[…]}`` e só os ``files`` eram
        aproveitados. O ``dir`` é a única fonte confiável da pasta de download
        DELE (que não é necessariamente ``~/Downloads``), e sem ele o caminho do
        artefato viraria palpite montado a partir do nome.
        """
        directory = str((listing or {}).get("dir") or "").strip()
        if directory:
            self.downloads_dir = directory.rstrip("/")
        # Device antigo não manda `sensor` — aí é a pasta respondendo, como
        # sempre foi, e o veredito continua sendo por presença de arquivo.
        self._download_sensor = str((listing or {}).get("sensor") or "disk")
        files = list((listing or {}).get("files") or [])
        for item in files:
            name = str(item.get("name") or "").strip()
            if name and self.downloads_dir and not item.get("path"):
                item["path"] = f"{self.downloads_dir}/{name}"
        return files

    async def _announce(self, files: list[dict]) -> None:
        """Avisa o chamador dos arquivos novos, uma vez por nome. Best-effort.

        Roda no meio da tarefa, de propósito: quem registra o artefato não pode
        depender de a tarefa chegar ao fim. Uma falha aqui não pode derrubar um
        download que já aconteceu.
        """
        if not self.on_download:
            return
        novos = [
            item
            for item in files
            if str(item.get("name") or "") and str(item.get("name")) not in self._announced
        ]
        if not novos:
            return
        self._announced.update(str(item.get("name")) for item in novos)
        try:
            await self.on_download(novos)
        except Exception as exc:  # noqa: BLE001 — registro nunca quebra a tarefa
            logger.debug("[BrowserLocal] aviso de download falhou: %s", exc)

    @staticmethod
    def _aceito(item: dict) -> bool:
        """O NAVEGADOR confirmou este download (item existe na barra dele).

        `download_id` só vem do sensor do navegador. Com a leitura de pasta o
        campo não existe e a resposta é sempre False — de propósito: o disco não
        sabe dizer que um download foi aceito, só que um arquivo apareceu.
        """
        return bool(str(item.get("download_id") or ""))

    def _em_curso(self) -> list[dict]:
        """Downloads aceitos que ainda não terminaram. Reclicar aqui duplica."""
        return [
            item
            for item in self._armados.values()
            if str(item.get("state") or "") == "in_progress"
        ]

    async def _pagina_trabalhando(self) -> bool:
        """A página buscou alguma coisa desde o clique?

        É o desempate da fase B. Site que gera zip sob demanda dispara XHR e só
        depois emite o download: nesse intervalo a pasta está vazia e o
        navegador ainda não tem item — igualzinho a um clique que errou o alvo.
        A diferença está na rede.

        Melhor esforço: device sem a ação (ou página que não deixa avaliar)
        devolve False, e aí o veredito volta a ser o de antes. Falso negativo
        aqui custa uma nova tentativa; falso positivo custaria uma espera boba.
        """
        try:
            leitura = await self._call("browser.network_activity", {})
        except Exception as exc:  # noqa: BLE001 — termômetro não derruba o passo
            logger.debug("[BrowserLocal] rede indisponível: %s", exc)
            return False
        if not isinstance(leitura, dict) or leitura.get("ok") is not True:
            return False

        agora = int(leitura.get("now") or 0)
        fim = int(leitura.get("last_response_end") or 0)
        recursos = int(leitura.get("resources") or 0)

        marco = self._rede_marco
        self._rede_marco = leitura
        # Recurso novo desde a leitura anterior: a página está puxando dados
        # AGORA, sem margem para interpretação.
        if marco is not None and recursos > int(marco.get("resources") or 0):
            return True
        # Leitura única: alguma resposta chegou nos últimos 3s.
        return bool(fim and agora and (agora - fim) < 3000)

    async def _conferir_downloads(self, args: dict) -> str:
        """Confere o que aconteceu com o download. É o que destrava o reclique.

        Três fases, nesta ordem:

        * **armar** — o navegador aceitou o download? O item do `chrome.downloads`
          nasce quando o Chrome aceita, muito antes do primeiro byte, então a
          resposta chega em ~1s. Aceitou: reclicar passa a ser proibido.
        * **desempate** — não aceitou dentro da janela: a página ainda está
          trabalhando (site zipando) ou o clique não pegou? Quem separa é a rede.
        * **acompanhar** — aceito: só resta ler `state`. "Falhou" agora existe,
          com motivo, em vez de virar pasta vazia indistinguível de tudo o mais.

        Com o sensor antigo (leitura de pasta) nada disso está disponível e o
        comportamento é o de sempre: arquivo presente = chegou, ausente = nada.
        """
        wait_s = max(0, min(int(args.get("wait_s") or 0), 60))
        # Sem pedido de acompanhamento, esperar muito é desperdício: assim que
        # o arquivo APARECE a tarefa está cumprida. O teto curto evita que o
        # modelo peça 60s "por garantia" e o usuário fique olhando.
        if not self.follow_download:
            wait_s = min(wait_s, 12)

        # A janela de nascimento vale mesmo com `wait_s=0`: conferir no talo do
        # clique responderia "nada" antes de o navegador ter tido chance, e é
        # essa resposta que mandava o modelo clicar de novo.
        piso = 0.0
        if self._download_click_at:
            passou = time.monotonic() - self._download_click_at
            piso = max(0.0, _JANELA_ARMAR_S - passou)
        deadline = time.monotonic() + max(float(wait_s), piso)

        # Conferir é o que destrava o próximo clique de download — inclusive
        # quando o resultado é "nada novo" (aí clicar de novo é legítimo: o
        # download realmente não começou). O que NÃO destrava é um download
        # aceito e em curso: para esse existe guarda própria em `_validate`.
        self._download_unverified = False
        self._rede_marco = None
        antes = {item.get("name") for item in self._downloads_before}

        files: list[dict] = []
        while True:
            listing = await self._call(
                "browser.downloads", {"mode": "headless"} if self.headless else {}
            )
            files = self._absorb_listing(listing)
            novos = [f for f in files if f.get("name") not in antes]
            for item in novos:
                if self._aceito(item):
                    self._armados[str(item.get("download_id"))] = item

            prontos = [f for f in novos if not f.get("incomplete")]
            falhou = [f for f in novos if str(f.get("state") or "") == "interrupted"]
            # Sem acompanhamento, ver o arquivo NASCER já basta para sair do
            # laço — não interessa esperar ele encher.
            if (
                prontos
                or falhou
                or (novos and not self.follow_download)
                or time.monotonic() >= deadline
            ):
                break
            await asyncio.sleep(2.0)

        novos = [f for f in files if f.get("name") not in antes]
        # O arquivo já está no disco DELE neste ponto, mesmo que ainda esteja
        # enchendo. Registrar agora é o que sobrevive a um job que morre no
        # meio — o fim da tarefa só confirma tamanho.
        await self._announce(novos)
        return await self._veredito(novos, files, wait_s)

    async def _veredito(self, novos: list[dict], files: list[dict], wait_s: int) -> str:
        """Traduz o estado bruto em uma frase que fecha a decisão do modelo.

        Falha vem ANTES de "pronto" de propósito: um download interrompido deixa
        um arquivo parcial no disco, e ler isso como conclusão é o falso sucesso
        que a leitura de pasta cometia.
        """
        self._preparando = False
        falhou = [f for f in novos if str(f.get("state") or "") == "interrupted"]
        prontos = [f for f in novos if not f.get("incomplete") and f not in falhou]
        if falhou:
            item = falhou[0]
            motivo = str(item.get("error") or "").strip() or "motivo não informado"
            self._armados.pop(str(item.get("download_id") or ""), None)
            if motivo in _ERROS_PASSAGEIROS:
                return (
                    f"❌ DOWNLOAD FALHOU — {item.get('name')} parou por {motivo}. "
                    "É falha passageira: pode mandar baixar de novo UMA vez."
                )
            return (
                f"❌ DOWNLOAD FALHOU — {item.get('name')} parou por {motivo}. "
                "Isso não melhora tentando de novo: relate ao usuário com `done` "
                "dizendo o motivo, ou procure outro caminho para o arquivo."
            )
        if prontos:
            # Dizer "COMPLETO" com todas as letras. Antes o retorno era só
            # `nome (888218 bytes, 0s atrás)` e o modelo tinha que INFERIR o
            # fim pela ausência de "AINDA BAIXANDO" — não inferiu, foi
            # baixar de novo (job 6142ed04).
            self._download_done = [str(f.get("name")) for f in prontos]
            nomes = "; ".join(
                f"{f.get('name')} ({f.get('bytes', 0)} bytes)" for f in prontos[:5]
            )
            return (
                f"✅ DOWNLOAD CONCLUÍDO — {nomes} já está no disco do usuário. "
                "NÃO mande baixar de novo: a tarefa acabou, responda `done` com esse nome."
            )

        # A partir daqui só o sensor do navegador tem o que dizer; com a leitura
        # de pasta `state` não existe e estas ramificações não disparam.
        pausados = [f for f in novos if f.get("paused")]
        if pausados:
            return (
                f"⏸ DOWNLOAD PAUSADO — {pausados[0].get('name')} está pausado no navegador. "
                "Não é falha e clicar de novo não retoma: relate ao usuário com `done`."
            )

        if novos and not self.follow_download:
            # O usuário pediu para BAIXAR. O download começou: está feito.
            # Quem avisa quando o byte terminar é o job de vigia, sem gastar
            # passo nenhum daqui.
            self.download_pending = str(novos[0].get("name") or "")
            self.download_pending_id = str(novos[0].get("download_id") or "")
            nomes = "; ".join(f"{f.get('name')}" for f in novos[:3])
            return (
                f"⬇ DOWNLOAD COMEÇOU — {nomes} está entrando na pasta de downloads. "
                "Você pediu para BAIXAR e o navegador está baixando: a tarefa está "
                "cumprida. Responda `done` dizendo que o download começou (eu aviso "
                "sozinho quando terminar). NÃO fique esperando nem mande baixar de novo."
            )

        if not novos and self._download_click_at:
            # Fase B: nada nasceu. Antes de liberar um reclique (que pode
            # duplicar), pergunta à rede se a página ainda está trabalhando.
            if await self._pagina_trabalhando():
                self._preparando = True
                return (
                    "⏳ AINDA PREPARANDO — o navegador ainda não aceitou nenhum download, "
                    "mas a página está buscando dados agora: é o site montando o arquivo "
                    "(compactar uma pasta leva de segundos a minutos). NÃO clique em "
                    'baixar de novo — espere com `downloads` {"wait_s": 30}.'
                )
            if self._download_sensor == "browser":
                return (
                    "❌ O CLIQUE NÃO VIROU DOWNLOAD — o navegador não registrou nenhum "
                    "download e a página não está carregando nada. Não adianta esperar: "
                    "ou o clique errou o alvo, ou o site barrou (aviso de vírus, login, "
                    "permissão). Olhe a tela com `look` e tente outro caminho."
                )

        if not files:
            espera = f" (esperei {wait_s}s)" if wait_s else ""
            return f"nada novo na pasta de downloads{espera}"
        return "; ".join(
            f"{f.get('name')} ({f.get('bytes', 0)} bytes"
            + (", AINDA BAIXANDO" if f.get("incomplete") else "")
            + f", {f.get('seconds_ago', 0)}s atrás)"
            for f in files[:5]
        )

    async def _downloads_snapshot(self, label: str) -> list[dict]:
        """Fotografa a pasta de downloads. Device antigo simplesmente não tem."""
        try:
            pedido_dl: dict = {"since_s": 900}
            if self.headless:
                pedido_dl["mode"] = "headless"
            listing = await self._call("browser.downloads", pedido_dl)
        except Exception as exc:  # noqa: BLE001
            self._log("downloads", label, None, str(exc))
            return []
        self._log("downloads", label, listing)
        return self._absorb_listing(listing)

    async def _shot(self, clip: list | None = None) -> str | None:
        """Print da tela com os números dos elementos desenhados em cima.

        Melhor esforço: navegador que não deixa fotografar não pode derrubar a
        tarefa. Uma falha desliga a visão pelo resto do job — repetir o timeout
        a cada passo custaria mais que a cegueira.

        Com `clip` (retângulo em px de CSS, o `box` de um elemento) o print sai
        RECORTADO, sem marcação e sem redução: é o print da triagem de painel,
        cujo trabalho é ser lido, não clicado. Nesse caso `_marks_refs` não é
        tocado — ele pertence ao caminho do `look`, e sobrescrevê-lo aqui faria
        a próxima olhada achar que a tela não tem número nenhum desenhado.
        """
        if not self.vision_model:
            self._log("no_shot", "visão desligada nesta tarefa")
            return None
        pedido: dict = {"marks": True, "quality": 60, "scale": 0.75}
        recorte = (
            [float(v) for v in clip]
            if isinstance(clip, list) and len(clip) == 4
            else None
        )
        if recorte and recorte[2] > 0 and recorte[3] > 0:
            pedido = {"marks": False, "quality": 80, "clip": recorte}
        else:
            recorte = None
        try:
            shot = await self._call("browser.screenshot", pedido)
        except Exception as exc:  # noqa: BLE001
            logger.info("[BrowserLocal] visão desligada — o print falhou: %s", exc)
            self._log("no_shot", f"o device recusou: {exc} — visão desligada daqui em diante")
            self._vision_down_reason = "o navegador recusou tirar print da tela"
            self.vision_model = None
            return None
        data = (shot.get("data") or "").strip() or None
        if recorte:
            # Device antigo ignora `clip` e devolve a tela inteira. Não é erro —
            # é print pior, e print pior ainda vale mais que nenhum.
            if data:
                self._log("shot", len(self.steps) + 1, data, shot.get("marks"),
                          recorte=bool(shot.get("clipped")))
            else:
                self._log("no_shot", "o device devolveu imagem vazia")
            return data
        # QUAIS números saíram desenhados. É o que permite dizer aos olhos "só
        # existem estes" em vez de confiar que eles não vão inventar — e eles
        # inventam: ver a regra dos números em `_VISION_SYSTEM`.
        refs = shot.get("marks_refs")
        self._marks_refs = [int(r) for r in refs if isinstance(r, (int, float))] if isinstance(refs, list) else None
        if data:
            # `marks` só existe no device novo: a ausência do campo é o sintoma
            # de desktop desatualizado, e é isso que o diário precisa mostrar.
            self._log("shot", len(self.steps) + 1, data, shot.get("marks"))
        else:
            self._log("no_shot", "o device devolveu imagem vazia")
        return data

    async def _look(self, question: str, snapshot: dict) -> str:
        """Tira um print e faz o modelo de visão RESPONDER sobre ele.

        Devolve texto para o histórico — nunca levanta. Olho que falha não pode
        matar a tarefa: quem dirige tem o DOM inteiro e segue sem a imagem, que
        é exatamente o que acontecia antes de existir visão nenhuma.

        A pergunta é do modelo que dirige. Quando ele não faz nenhuma, cai numa
        genérica — pior, mas ainda útil (é o caso do olhar automático de
        travamento, onde a pergunta certa é sempre "o que está na frente?").
        """
        if not self.vision_model:
            return f"não consegui ver — {self._vision_down_reason or 'visão desligada nesta tarefa'}"
        shot = await self._shot()
        if not shot:
            return f"não consegui ver — {self._vision_down_reason or 'o print não veio'}"

        pergunta = question.strip() or (
            "O que há nesta tela? Se algo estiver cobrindo o conteúdo, diga primeiro."
        )
        # O contexto é MÍNIMO de propósito: os olhos não precisam da tarefa nem
        # do histórico para responder o que está na tela, e mandar tudo isso só
        # convidaria o modelo de visão a opinar sobre a próxima ação — que é o
        # papel que acabamos de tirar dele.
        # A lista dos números DESENHADOS vai junto. Sem ela, "cite o número" é um
        # convite a inventar quando o elemento não tem marcação — e inventar
        # aqui não falha, clica em outra coisa (job 9968e68a).
        numeros = ""
        if self._marks_refs:
            visiveis = sorted(self._marks_refs)
            mostra = ", ".join(str(r) for r in visiveis[:60])
            numeros = (
                f"\n\nNÚMEROS DESENHADOS NESTA IMAGEM (os únicos que existem): {mostra}"
                f"{' …' if len(visiveis) > 60 else ''}. "
                "Qualquer elemento fora desta lista você descreve pelo TEXTO dele, nunca "
                "por um número."
            )
        elif self._marks_refs is not None:
            numeros = (
                "\n\nESTA IMAGEM NÃO TEM NENHUM NÚMERO DESENHADO — descreva todo elemento "
                "pelo TEXTO que está escrito nele."
            )
        user = (
            f"PERGUNTA: {pergunta}\n\n"
            f"(a página é {snapshot.get('url') or 'desconhecida'} — "
            f"{snapshot.get('title') or 'sem título'})"
            f"{numeros}"
        )
        from shared.types import Message, MessageRole

        started = time.monotonic()
        try:
            provider = self.llm.get_provider_for_model(self.vision_model)
            response = await provider.complete(
                messages=[
                    Message(role=MessageRole.SYSTEM, content=_VISION_SYSTEM),
                    Message(role=MessageRole.USER, content=user, images=[shot]),
                ],
                model=self.vision_model,
                temperature=0.1,
                max_tokens=400,
                disable_thinking=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._log("llm_failed", self.vision_model, exc)
            # Cota/chave não volta sozinha: desliga a visão pelo resto do job.
            # Qualquer outra falha (timeout, 5xx, rede) é passageira e merece
            # nova tentativa — matar a visão no primeiro soluço foi o que cegou
            # o job 0f87265f por 20 passos.
            if _VISION_FAILURE_IS_PERMANENT.search(str(exc)):
                logger.warning("[BrowserLocal] visão desligada (definitivo): %s", exc)
                self._vision_down_reason = "a chave do modelo de visão está sem crédito/cota"
                self.vision_model = None
            else:
                self._vision_strikes += 1
                if self._vision_strikes >= _VISION_MAX_STRIKES:
                    logger.warning(
                        "[BrowserLocal] visão desligada após %d falhas seguidas: %s",
                        self._vision_strikes, exc,
                    )
                    self._vision_down_reason = (
                        f"o modelo de visão falhou {self._vision_strikes} vezes seguidas"
                    )
                    self.vision_model = None
            return f"não consegui ver — o modelo de visão falhou ({str(exc)[:120]})"

        # Voltou a responder: o contador de soluços recomeça do zero, senão três
        # timeouts espalhados pela tarefa somariam e desligariam a visão que
        # está funcionando.
        self._vision_strikes = 0
        answer = (response.get_content() or "").strip()
        self._log("vision", self.vision_model, (time.monotonic() - started) * 1000, pergunta, answer)
        if not answer:
            return "não consegui ver — o modelo de visão respondeu vazio"
        return answer[:_LOOK_MAX_CHARS]

    async def _auto_look_if_stuck(self, snapshot: dict) -> None:
        """Olha sozinho quando a tela empacou — o ponto cego conhecido do DOM.

        O modelo continua no comando do `look`; isto é a rede. `ok:true` de um
        clique não quer dizer que a página reagiu, e sem imagem o driver não tem
        como saber a diferença entre "o site ignorou" e "tem um popup na frente".

        Uma vez por episódio: o flag só volta a valer quando a tela muda
        (`_note_change` zera o `_stuck`), senão um travamento longo viraria uma
        imagem por passo — exatamente o custo que este desenho veio eliminar.
        """
        if self._stuck < _AUTO_LOOK_AFTER_STUCK or self._auto_looked or not self.vision_model:
            return
        self._auto_looked = True
        answer = await self._look(
            "A tela parece travada: as últimas ações não mudaram nada. O que está "
            "na frente do conteúdo (popup, banner, modal, overlay)? Se houver, qual "
            "o número do botão que fecha?",
            snapshot,
        )
        self.steps.append(Step("look", {"automático": True}, True, answer[:400]))

    # ── decisão ──────────────────────────────────────────────────────────

    async def _decide(self, task: str, context: str, snapshot: dict) -> dict:
        history = "\n".join(f"{i + 1}. {s.render()}" for i, s in enumerate(self.steps[-8:]))
        stuck = ""
        if self._stuck:
            plural = "ações" if self._stuck > 1 else "ação"
            stuck = (
                f"ATENÇÃO: a{'s' if self._stuck > 1 else ''} última{'s' if self._stuck > 1 else ''} "
                f"{self._stuck} {plural} NÃO mudou nada na tela. Repetir não vai funcionar — "
                "mude de abordagem agora."
            )
        user = "\n\n".join(
            part
            for part in (
                f"TAREFA: {task}",
                f"CONTEXTO: {context}" if context else "",
                f"PASSOS JÁ DADOS:\n{history}" if history else "PASSOS JÁ DADOS: nenhum ainda.",
                stuck,
                (
                    "SEU CADERNO ({}):\n{}".format(
                        len(self.notes),
                        "\n".join(f"- {n}" for n in self.notes),
                    )
                    if self.notes
                    else "SEU CADERNO: vazio — nada coletado ainda."
                ),
                (
                    f"ATENÇÃO: {self._acting_steps} passos e o caderno continua VAZIO. "
                    "Se a tarefa pede coletar/listar/comparar qualquer coisa, o que "
                    "está na tela AGORA que serve? Anote com `note` antes de clicar "
                    "de novo — a próxima ação apaga isto. Se não pede, ignore."
                    if not self.notes and self._acting_steps >= _NUDGE_CADERNO_VAZIO
                    else ""
                ),
                f"PÁGINA AGORA:\n{self._render(snapshot, self._panel_verdict)}",
                # Sem olhos, dizer isso UMA vez por passo é mais barato que
                # deixá-lo gastar um passo pedindo `look` para ouvir "não deu".
                (
                    f"SEM VISÃO nesta tarefa ({self._vision_down_reason or 'desligada na config'}) "
                    "— não use `look`, decida pelo texto da página."
                    if not self.vision_model
                    else ""
                ),
                f"Passo {len(self.steps) + 1} de no máximo {self.max_steps}. Responda só o JSON.",
            )
            if part
        )

        # Quem decide é SEMPRE o modelo de texto. A imagem não entra aqui: ela
        # entra pela ação `look`, quando o próprio modelo pede (ver _look).
        model = self.model
        provider = self.llm.get_provider_for_model(model)
        from shared.types import Message, MessageRole

        started = time.monotonic()
        response = await provider.complete(
            messages=[
                Message(role=MessageRole.SYSTEM, content=_SYSTEM),
                Message(role=MessageRole.USER, content=user),
            ],
            model=model,
            temperature=0.1,
            max_tokens=700,
            # Saída curta e estruturada: sem isto o raciocínio come o orçamento e
            # o content volta vazio (deepseek-empty-response-reasoning).
            disable_thinking=True,
        )
        elapsed = (time.monotonic() - started) * 1000
        raw = response.get_content() or ""
        try:
            decision = self._parse(raw)
        except ValueError:
            # O cru é justamente o que interessa quando o JSON não deu para ler.
            self._log("thought", model, elapsed, {"action": "(ilegível)"}, raw[:1500])
            raise
        self._log("thought", model, elapsed, decision, "" if decision.get("action") else raw[:1500])
        return decision

    @staticmethod
    def _parse(raw: str) -> dict:
        """Extrai o JSON do passo. Modelo às vezes embrulha em ```json."""
        text = (raw or "").strip()
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        try:
            decision = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"não entendi a resposta do modelo: {exc}") from exc
        if not isinstance(decision, dict):
            raise ValueError("a resposta do modelo não é um objeto")
        return decision

    def _validate(self, decision: dict, snapshot: dict) -> tuple[str, dict]:
        action = str(decision.get("action") or "").strip()
        args = decision.get("args")
        args = dict(args) if isinstance(args, dict) else {}

        if action not in ACTIONS:
            raise ValueError(
                f"ação '{action}' não existe. Use uma de: {', '.join(sorted(ACTIONS))}"
            )
        for required in ACTIONS[action]:
            if required not in args or args[required] in ("", None):
                # O `done` sem `answer` merece tratamento próprio: em 2026-09-02
                # ele apareceu 10 vezes em 10 tentativas de terminar e derrubou
                # 3 jobs inteiros, sempre com o `thought` já contendo a resposta
                # que faltava no campo. Repetir "precisa do argumento" não
                # ensinava nada — dizer o que escrever, sim.
                if action == "done":
                    raise ValueError(
                        "`done` sem `answer` não termina nada: o `answer` É a resposta "
                        "que o usuário vai ler. Repita a ação com o texto dentro dele — "
                        'ex.: {"action":"done","args":{"answer":"achei 5 teclados: …"}}. '
                        "Se ainda não tem o que responder, não use `done`: leia a página "
                        "com `extract` ou use `give_up` explicando o que faltou"
                    )
                raise ValueError(f"a ação '{action}' precisa do argumento '{required}'")

        if "ref" in args:
            try:
                reference = int(args["ref"])
            except (TypeError, ValueError):
                raise ValueError("'ref' tem que ser o número do elemento") from None
            refs = {item.get("ref") for item in (snapshot.get("elements") or [])}
            if reference not in refs:
                raise ValueError(
                    f"o elemento {reference} não está na página deste passo — "
                    "escolha um número da lista atual"
                )
            args["ref"] = reference
            # Clique em CAIXA: recusar ANTES de tocar na página. O navegador
            # despacha o evento com sucesso num contêiner sem handler e devolve
            # "ok" — a mentira que fez o modelo repetir a mesma ação 23 vezes no
            # job a0eab88c. A guarda de "a tela não mudou" não pegava porque ele
            # alternava click/dblclick/right_click no mesmo ref, e cada abre-e-
            # fecha de menu zerava o contador. Aqui a recusa é imediata e vem com
            # os alvos de dentro, que é a informação que faltava.
            if action in ("click", "dblclick", "right_click"):
                alvo = next(
                    (i for i in snapshot["elements"] if i.get("ref") == reference), None
                )
                if alvo and alvo.get("grupo"):
                    # Filiação vem do device (campo `dentro`), não de heurística
                    # de texto: o rótulo de uma caixa é a concatenação dos filhos,
                    # então "Mais ações" É substring dele e "palworld Pasta" não é
                    # — casar por substring listava exatamente os alvos errados.
                    dentro = [
                        f'[{i.get("ref")}] "{i.get("text")}"'
                        for i in snapshot["elements"]
                        if i.get("dentro") == reference and not i.get("grupo")
                    ]
                    dica = (
                        " Os itens dentro dela: " + ", ".join(dentro[:8])
                        if dentro
                        else " Procure na lista o elemento com o NOME do item, não a caixa."
                    )
                    raise ValueError(
                        f"o elemento {reference} é a CAIXA em volta de "
                        f"{alvo['grupo']} itens, não um botão — clicar nela não faz "
                        f"nada.{dica}"
                    )
            if action in ("click", "dblclick", "right_click"):
                alvo = next(
                    (i for i in snapshot["elements"] if i.get("ref") == reference), None
                )
                if alvo and alvo.get("coberto"):
                    # Recusar aqui é o que transforma um "ok" mentiroso em
                    # informação: o evento seria despachado com sucesso no nó e
                    # a página, que escuta na camada de cima, ignoraria.
                    #
                    # Mas o CONSELHO depende de quem cobre. Quando é um painel
                    # que a triagem já classificou como conteúdo, mandar
                    # "dispense o que está na frente" é mandar fechar a resposta
                    # — literalmente o loop do job 6785cf70.
                    if alvo.get("coberto_painel") and self._conteudo_na_tela():
                        raise ValueError(
                            f"o elemento {reference} está ATRÁS do painel aberto "
                            f'"{str(alvo["coberto"])[:50]}" — e esse painel é a RESPOSTA, '
                            "não um obstáculo. Não feche: o que você procura está dentro "
                            "dele. O texto completo dele já está no snapshot (seção TEXTO "
                            "COMPLETO DO PAINEL); se faltar algo, `extract` no ref do painel"
                        )
                    fecha = [
                        f'[{i.get("ref")}] "{i.get("text")}"'
                        for i in snapshot["elements"]
                        if i.get("menu") and i.get("text")
                    ]
                    dica = (
                        " O que está aberto na frente: " + ", ".join(fecha[:6])
                        if fecha
                        else " Procure o botão de fechar/aceitar/dispensar dessa camada."
                    )
                    raise ValueError(
                        f'o elemento {reference} está COBERTO por '
                        f'"{str(alvo["coberto"])[:50]}" — o clique não chega nele. '
                        f"Dispense o que está na frente primeiro.{dica}"
                    )

            if action in ("click", "dblclick"):
                label = next(
                    (
                        item.get("text", "")
                        for item in snapshot["elements"]
                        if item.get("ref") == reference
                    ),
                    "",
                )
                painel = next(
                    (
                        item.get("panel", "")
                        for item in snapshot["elements"]
                        if item.get("ref") == reference
                    ),
                    "",
                )
                is_download_label = _starts_download(label)
                # Já disparamos um download e ainda não olhamos a pasta: NÃO
                # dispara outro. Diferente do aviso abaixo, esta trava não é de
                # uma vez só — mas também não prende: chamar `downloads` a
                # libera, e é exatamente isso que o agente deveria fazer. É a
                # única guarda que funciona com a visão desligada, porque não
                # depende de enxergar a tela para saber que já clicou.
                # O navegador ACEITOU um download e ele ainda está vindo.
                # Diferente da trava abaixo, esta não abre conferindo: conferir
                # é justamente o que descobriu que o download está em curso.
                # Sem ela, o passo 6 do job 0f87265f (conferir → clicar) voltava
                # a duplicar assim que a conferência acontecesse cedo.
                if self._preparando and is_download_label:
                    raise ValueError(
                        f'o site AINDA ESTÁ MONTANDO o arquivo (você acabou de conferir e a '
                        f'página está carregando). Clicar em "{(label or "").strip()[:40]}" '
                        "agora pede um segundo arquivo em vez de esperar o primeiro. "
                        'Espere com `downloads` {"wait_s": 30}.'
                    )
                em_curso = self._em_curso()
                if em_curso and is_download_label:
                    nome = str(em_curso[0].get("name") or "o arquivo")
                    raise ValueError(
                        f'"{nome}" JÁ está baixando agora (o navegador aceitou o download). '
                        f'Clicar em "{(label or "").strip()[:40]}" baixa o mesmo arquivo de '
                        "novo. Responda `done` dizendo que o download começou — eu aviso "
                        "sozinho quando terminar."
                    )
                if self._download_unverified and is_download_label:
                    raise ValueError(
                        f'você JÁ disparou um download e ainda não conferiu. Clicar em '
                        f'"{(label or "").strip()[:40]}" agora baixa o mesmo arquivo de novo. '
                        'Antes de qualquer outro clique de download, chame '
                        '`downloads` {"wait_s": 30} e leia o resultado.'
                    )
                if (
                    self._download_done
                    and not self._redownload_warned
                    and is_download_label
                ):
                    # Aviso ÚNICO: se o modelo insistir, deixa passar — pode ser
                    # uma tarefa de vários arquivos, e travar de vez seria pior.
                    self._redownload_warned = True
                    raise ValueError(
                        f"'{self._download_done[0]}' JÁ está no disco do usuário — clicar em "
                        f'"{(label or "").strip()[:40]}" de novo baixa o mesmo arquivo repetido. '
                        "Se era esse o arquivo pedido, responda `done` agora. Só insista se for "
                        "um arquivo DIFERENTE"
                    )
                if self._download_started and _CANCELS_DOWNLOAD.search(label or ""):
                    raise ValueError(
                        f'"{label.strip()[:60]}" CANCELA o download que você acabou de '
                        "iniciar — é o oposto da tarefa. Espere com "
                        '`downloads` {"wait_s": 30}'
                    )
                # Fechar/minimizar o painel de progresso é o mesmo erro por outra
                # porta: no Drive o X do painel abre "Cancelar o download?".
                if (
                    self._download_started
                    and _DOWNLOAD_PANEL.search(painel or "")
                    and _DISMISSES.search(label or "")
                ):
                    raise ValueError(
                        f'"{label.strip()[:40]}" fecha o painel "{painel.strip()[:40]}", que é '
                        "o SEU download acontecendo. Compactar/preparar é a primeira fase do "
                        "download de uma pasta — o site zipa antes de mandar. Não é erro, não é "
                        "popup para dispensar e fechar costuma cancelar. Deixe o painel em paz "
                        'e espere com `downloads` {"wait_s": 30}'
                    )
                self._check_sensitive(label)

        # `click_text` não tem `ref`, mas atua igual: as guardas de dinheiro e de
        # download têm que valer para ela também, senão bastaria trocar de ação
        # para furar todas. O sinal aqui é o texto que o modelo escreveu — que é
        # justamente o rótulo do que ele quer clicar.
        if action == "click_text":
            if self._device_novo is False:
                raise ValueError(
                    "`click_text` não existe no navegador desta máquina ainda (o "
                    "neo-desktop precisa ser reiniciado). Use `click` com um `ref` da "
                    "lista, e se o alvo não estiver nela, procure o elemento que o "
                    "contém ou role a página"
                )
            texto = str(args.get("texto") or "").strip()
            if len(texto) < 3:
                raise ValueError(
                    "`texto` curto demais para achar com segurança — use um trecho de pelo "
                    "menos 3 letras que só apareça no elemento que você quer clicar"
                )
            args["texto"] = texto[:200]
            if self._download_started and _CANCELS_DOWNLOAD.search(texto):
                raise ValueError(
                    f'"{texto[:50]}" CANCELA o download que você acabou de iniciar — é o '
                    'oposto da tarefa. Espere com `downloads` {"wait_s": 30}'
                )
            if _starts_download(texto) and (self._download_unverified or self._em_curso()):
                raise ValueError(
                    f'você JÁ disparou um download e ainda não conferiu. Clicar em '
                    f'"{texto[:40]}" agora baixa o mesmo arquivo de novo. Chame '
                    '`downloads` {"wait_s": 30} antes'
                )
            self._check_sensitive(texto)

        if action == "navigate":
            self._check_navigation(str(args["url"]))

        # FECHAR o painel que É a resposta. A guarda mais importante deste
        # arquivo: no job 6785cf70 o modelo abriu e fechou o mesmo modal 7 vezes,
        # e no passo 19 chegou a extrair a timeline inteira dele — para no passo
        # seguinte voltar a tentar fechá-lo. Recusar aqui é barato e a saída vem
        # junto (é o que faltava nas recusas que mataram três jobs em 2026-09-02).
        alvo_painel = self._fecharia_painel(action, args, snapshot)
        if alvo_painel:
            chave, rotulo = alvo_painel
            veredito = self._panel_verdict.get(chave) or {}
            aberturas = self._panel_opens.get(chave, 0)
            # Só recusa com EVIDÊNCIA. `fonte: default` quer dizer "não deu para
            # triar" (sem visão, sem orçamento) — ali o veredito serve para não
            # chamar o painel de intruso no prompt, mas bloquear um fechamento
            # explícito em cima de um palpite seria trocar um loop por outro.
            sabe = veredito.get("fonte") in ("causa", "visao", "dom")
            if sabe and veredito.get("tipo") == "conteudo":
                # Veredito por CAUSA é heurística, não leitura: recusa UMA vez.
                # Se o modelo insistir depois de ler o painel, ele pode estar
                # certo e a heurística errada — e prender aqui seria repetir, do
                # outro lado, exatamente o erro que este trabalho veio corrigir.
                # O vaivém continua coberto pela guarda de aberturas abaixo.
                gastou = self._panel_refusals.get(chave, 0)
                if veredito.get("fonte") != "causa" or gastou == 0:
                    self._panel_refusals[chave] = gastou + 1
                    raise ValueError(
                        f"não feche o painel «{rotulo[:50]}» — ele É a resposta "
                        f"({veredito.get('porque', 'apareceu por causa da sua ação')}). "
                        "O texto completo dele já está no snapshot, na seção TEXTO COMPLETO "
                        "DO PAINEL. Leia de lá, anote com `note` o que a tarefa pediu e "
                        "termine com `done` — ou use `extract` no ref do painel se faltar algo"
                    )
            if aberturas >= 2:
                raise ValueError(
                    f"você já abriu e fechou o painel «{rotulo[:50]}» {aberturas} vezes — "
                    "isso é um vaivém, não progresso. A informação está DENTRO dele: leia "
                    "a seção TEXTO COMPLETO DO PAINEL, ou `extract` no ref do painel"
                )

        # Pedir de novo exatamente o que acabou de não fazer nada é o loop que
        # queimou os 25 passos. Recusar aqui não toca na página e obriga o
        # modelo a escolher outra coisa na rodada seguinte. Vem no fim de
        # propósito: os `args` já estão normalizados (ref virou int), senão
        # {"ref": "33"} passaria como se fosse ação diferente de {"ref": 33}.
        # Reler o mesmo elemento na MESMA tela devolve o mesmo texto — é passo
        # jogado fora, e a guarda de atuação não alcança `extract` porque ele não
        # muda a tela por definição. O painel que ele tenta ler já vem inteiro no
        # snapshot (seção DENTRO DOS PAINÉIS QUE ROLAM); é para lá que a recusa
        # aponta.
        if action == "extract" and self._signature(action, args) in self._reads_on_screen:
            raise ValueError(
                "você já leu esse elemento nesta tela e o texto é o mesmo. O conteúdo "
                "dos painéis já vem inteiro no snapshot (inclusive a parte abaixo da "
                "dobra) — leia de lá, ou escolha outro elemento/caminho"
            )

        # Mesma tela, segundo `look`: a imagem não mudou, a resposta seria a
        # mesma e a imagem é o insumo CARO deste desenho. A recusa aponta a saída
        # em vez de só barrar — recusa muda sem alternativa foi o que matou três
        # jobs em 2026-09-02.
        if action == "look" and (self._last_fingerprint or "") in self._looked_on_screen:
            raise ValueError(
                "você já olhou ESTA tela e ela não mudou desde então — a resposta "
                "seria a mesma. Aja com o que já sabe: se a dúvida for texto, use "
                "`extract`; se for o que fazer, escolha uma ação e olhe DEPOIS dela"
            )

        if self._stuck and self._stuck_signature == self._signature(action, args):
            # Recusar sem dizer o que fazer só empurra o modelo para a mesma
            # escolha na rodada seguinte — foi assim que três recusas seguidas
            # mataram o job 706ff8a8 com o menu ABERTO e "Baixar" na lista.
            opened = [
                f'[{item.get("ref")}] "{item.get("text")}"'
                for item in (snapshot.get("elements") or [])
                if item.get("menu")
            ]
            if opened:
                saida = (
                    "o menu já está ABERTO — clicar de novo no que o abriu só fecha e "
                    "reabre. Escolha um item DELE: " + ", ".join(opened[:10])
                )
            else:
                saida = (
                    "escolha outra: outro elemento, dblclick/right_click, rolar a "
                    "página ou navegar direto pela URL"
                )
            raise ValueError(
                f"essa mesma ação acabou de rodar e a tela não mudou nada — {saida}"
            )
        return action, args

    # ── execução ─────────────────────────────────────────────────────────

    async def _retry_stale(self, action: str, args: dict, label: str) -> str:
        """Refaz UMA vez a ação cujo elemento sumiu entre o snapshot e o clique.

        Numa SPA que redesenha sozinha (o Drive redesenha), passam ~1,5s entre
        fotografar a página e agir — o tempo de o modelo decidir. Quando o
        elemento é recriado nesse intervalo, o `ref` vira `stale` e o passo
        morria por um motivo que não tem nada a ver com a decisão. Aqui a
        âncora é o RÓTULO: se o mesmo nome ainda está na tela, é ele.
        """
        if not label:
            raise RuntimeError("stale")
        snapshot = await self._snapshot()
        novo = next(
            (
                item.get("ref")
                for item in (snapshot.get("elements") or [])
                if (item.get("text") or "") == label
            ),
            None,
        )
        if novo is None:
            raise RuntimeError(f"stale — e '{label[:40]}' não está mais na tela")
        self._log(
            "note", "stale", f"'{label[:40]}' mudou de {args.get('ref')} para {novo}; refazendo"
        )
        return await self._perform(action, {**args, "ref": novo})

    async def _perform(self, action: str, args: dict, snapshot: dict | None = None) -> str:
        """Executa e devolve o detalhe para o histórico. Erro sobe como exceção."""
        if action == "note":
            texto = str(args.get("texto") or "").strip()[:400]
            self.notes.append(texto)
            self._log("note", f"anotou ({len(self.notes)})", texto)
            # A confirmação diz o TOTAL de propósito: é o que deixa o modelo
            # saber que já tem 5 dos 5 que a tarefa pediu, sem ter que recontar
            # o histórico.
            return f"anotado ({len(self.notes)} no caderno)"

        if action == "look":
            # Não é `browser.look`: é print + uma chamada ao modelo de visão. A
            # resposta VIRA o detalhe do passo, então entra no histórico da
            # próxima decisão sem nenhuma costura extra.
            self._looked_on_screen.add(self._last_fingerprint or "")
            return await self._look(str(args.get("pergunta") or ""), snapshot or {})

        if action == "screenshot":
            shot = await self._call("browser.screenshot", {})
            data = shot.get("data") or ""
            if data and len(self.shots) < self.max_screenshots:
                self.shots.append(data)
            return "print guardado"

        if action == "downloads":
            return await self._conferir_downloads(args)

        result = await self._call(f"browser.{action}", args)
        if action == "navigate":
            host = self._host_of(str(args.get("url", "")))
            if host:
                self._allowed_hosts.add(host)
            self._navegou = True
            return str(result.get("url") or args.get("url", ""))
        if action == "extract":
            texto = (result.get("text") or "")[:1500]
            # AUTO-CADERNO. No job 6785cf70 o `extract` do passo 19 devolveu a
            # timeline inteira — a resposta da tarefa — e ela morreu no
            # histórico: `steps[-8:]` a empurrou para fora, o modelo voltou a
            # tentar fechar o painel e o resumo final saiu com "não cheguei ao
            # fim", com o caderno vazio. Leitura substancial vai para o caderno
            # sozinha: é o que sobrevive à poda do histórico, à troca de página e
            # ao job morrer no meio.
            if len(texto.strip()) >= 200:
                self.notes.append(texto.strip()[:400])
                self._log("note", f"anotou ({len(self.notes)})", "leitura do `extract`")
            return texto
        if action in ("click", "click_text", "dblclick", "right_click"):
            label = str(result.get("label") or "")
            # SELEÇÃO é o único efeito de clique que o freio de tela repetida não
            # enxerga sozinho (marcar um rádio não muda URL, título nem texto).
            # O device lê o estado do controle DEPOIS do clique e devolve aqui —
            # é a diferença entre "cliquei" e "ficou marcado".
            marcado = result.get("checked")
            sufixo = ""
            if marcado is True:
                sufixo = " → SELECIONADO"
            elif marcado is False:
                sufixo = (
                    " → mas NÃO ficou selecionado (o clique não pegou nesse elemento; "
                    "tente `click_text` com o texto da opção)"
                )
            if action == "click_text":
                achados = result.get("achados")
                if isinstance(achados, int) and achados > 1:
                    label = f"{label} ({achados} trechos casaram, cliquei no mais específico)"
            # O rótulo vem do device (verdade), não da intenção do modelo.
            if _starts_download(label):
                self._download_started = True
                # A partir daqui o próximo clique de download só passa depois de
                # uma conferência na pasta (ver a trava em _validate).
                self._download_unverified = True
                self._download_click_at = time.monotonic()
            return label + sufixo
        return ""

    def _honest(self, answer: str) -> str:
        """Cola no "pronto" o que NÃO deu certo no caminho.

        O usuário estava OLHANDO a tela: entregar um "pronto" limpo depois de
        dois erros visíveis destrói a confiança mais do que o erro em si. Fica
        no driver (e não no runner) para valer para qualquer chamador.

        A CEGUEIRA entra aqui junto com os passos falhos, e é a mais importante
        das duas: no job 0f87265f o fecho disse "1 passo falhou e eu contornei"
        — o passo era um clique em elemento stale, irrelevante — e não disse que
        a visão tinha morrido no passo 1 e a tarefa toda saiu no escuro. O
        usuário só descobriu pela pasta de Downloads com 5 cópias de 52 MB.
        """
        notas: list[str] = []
        if self._vision_down_reason:
            notas.append(
                f"trabalhei SEM ver a tela — {self._vision_down_reason}; "
                "confira o resultado"
            )
        failed = [s for s in self.steps if not s.ok]
        if failed:
            plural = "passos falharam" if len(failed) > 1 else "passo falhou"
            notas.append(f"{len(failed)} {plural} e eu contornei")
        if not notas:
            return answer
        return f"{answer}\n\n(no caminho, {'; '.join(notas)})"

    # ── laço ─────────────────────────────────────────────────────────────

    async def run(self, task: str, *, context: str = "", start_url: str = "") -> DriverResult:
        """Roda a tarefa e FECHA o diário, aconteça o que acontecer.

        O rodapé fica aqui e não no runner porque é o único ponto por onde todos
        os finais passam — inclusive o device caindo no meio e a exceção que
        ninguém previu. Diário sem rodapé é diário que não se sabe se terminou.
        """
        started = time.monotonic()
        outcome: DriverResult | None = None
        try:
            outcome = await self._run(task, context=context, start_url=start_url)
            outcome.summary = self._com_caderno(outcome.summary)
            outcome.notes = list(self.notes)
            return outcome
        finally:
            self._log(
                "footer",
                outcome.status if outcome else "crash",
                outcome.summary if outcome else "a tarefa morreu antes de produzir resultado",
                len(self.steps),
                time.monotonic() - started,
            )

    def _com_caderno(self, summary: str) -> str:
        """Cola o caderno no resumo — inclusive quando a tarefa deu errado.

        Fica no `run` (e não em cada `return`) porque ali é o funil por onde
        TODOS os desfechos passam: concluído, falhou, estourou, o usuário
        apertou PARE, o device caiu. É justamente no fim ruim que isto vale
        mais: no job fc47220e o usuário parou uma tarefa que tinha visto 73
        preços dentro da faixa e recebeu de volta "Parei porque você apertou
        PARE" — nada do que foi visto sobrou. Com o caderno, uma tarefa
        interrompida ainda entrega o que já tinha achado.
        """
        if not self.notes:
            return summary
        # O que o `done` já contou não se repete. Quando a tarefa termina bem, o
        # modelo normalmente escreve a resposta A PARTIR do caderno — colar tudo
        # de novo entregaria a mesma lista duas vezes. O que sobra aqui é
        # exatamente o que faltou: o caso da tarefa parada no meio, e o do item
        # que ele coletou e esqueceu de citar.
        base = summary.casefold()
        faltando = [n for n in self.notes if n[:40].casefold() not in base]
        if not faltando:
            return summary
        itens = "\n".join(f"- {n}" for n in faltando)
        return f"{summary}\n\nO que consegui coletar ({len(faltando)}):\n{itens}".strip()

    async def _run(self, task: str, *, context: str = "", start_url: str = "") -> DriverResult:
        # Escopo do job: o site que a tarefa cita é o que fica liberado. Sem
        # nenhum, a allowlist só nasce no primeiro navigate (§6.1).
        if start_url:
            if self._is_blocked(start_url):
                return DriverResult(
                    tab_ref=self._tab_ref,
                    status="failed",
                    summary=f"'{self._host_of(start_url)}' está na lista de domínios proibidos.",
                )
            self._allowed_hosts.add(self._host_of(start_url))
        self._allowed_hosts.update(_hosts_citados(task))

        pedido: dict = {}
        if self.headless:
            pedido["mode"] = "headless"
        if start_url:
            pedido["url"] = start_url
        reaproveitou = False
        if self.tab_ref_pedido is not None and await self._aba_ainda_serve(self.tab_ref_pedido):
            pedido["tab_id"] = self.tab_ref_pedido
            reaproveitou = True

        claim = await self._call("browser.claim", pedido)
        # O claim com `url` já navega; reivindicar uma aba que já estava numa
        # página também significa que há página carregada. Nos dois casos o
        # escopo passa a valer integralmente.
        self._navegou = bool(start_url) or reaproveitou
        self._tab_ref = claim.get("tab_ref")
        if self._tab_ref is None:
            onde = "no navegador headless desta máquina" if self.headless else "no seu navegador"
            return DriverResult(status="failed", summary=f"Não consegui abrir uma aba {onde}.")

        last_url = start_url
        if self.follow_download is None:
            self.follow_download = bool(_ASKS_TO_FOLLOW.search(f"{task}\n{context}"))
        self._log(
            "note",
            "acompanhar o download até o fim?",
            "sim — o pedido pede isso" if self.follow_download
            else "não — o pedido é só baixar; quem espera é o job de vigia",
        )
        self._downloads_before = await self._downloads_snapshot("antes da tarefa")
        try:
            await self._banner("preparando…")
            # `look` NÃO consome o orçamento de passos. Se conferir custasse um
            # passo, o desenho estaria cobrando justamente o comportamento que
            # quer incentivar — e o modelo aprenderia a agir no escuro para não
            # gastar. O que limita as olhadas é a trava de tela repetida
            # (`_looked_on_screen`): no máximo uma por tela distinta.
            #
            # O teto de RODADAS existe mesmo assim: sem ele, um modelo que só
            # pedisse `look` giraria para sempre entre telas diferentes.
            step_number = 0
            rodadas = 0
            while step_number < self.max_steps and rodadas < self.max_steps * 2:
                rodadas += 1
                snapshot = await self._snapshot()
                last_url = snapshot.get("url") or last_url
                self._log("step_open", step_number + 1, self.max_steps)
                self._note_change(snapshot)
                # A triagem roda ANTES do render: o veredito é o que decide o
                # cabeçalho do painel no prompt, e sem ele o render voltaria a
                # descrever o painel como obstáculo.
                await self._triage_panel(snapshot)
                self._log("saw", self._render(snapshot, self._panel_verdict), snapshot)
                await self._auto_look_if_stuck(snapshot)

                # Inicializado FORA do try: `_decide` também levanta ValueError
                # (JSON ilegível), e o handler lê `decision` para saber que ação
                # foi tentada — sem isto seria NameError em cima de um erro.
                decision: dict | None = None
                try:
                    decision = await self._decide(task, context, snapshot)
                    action, args = self._validate(decision, snapshot)
                except NeedsConfirmation:
                    raise
                except ValueError as exc:
                    # Passo inválido NÃO toca na página: vira histórico para o
                    # modelo se corrigir na rodada seguinte.
                    # A ação TENTADA vai no histórico. Gravar "(inválido)" apagava
                    # justamente o que o modelo precisa ver para se corrigir: ele
                    # lia "algo inválido" em vez de "seu `done` foi recusado".
                    tentada = str((decision or {}).get("action") or "").strip() or "(inválido)"
                    self.steps.append(Step(tentada, {"recusado": True}, False, str(exc)))
                    self._log("refused", str(exc))
                    logger.info("[BrowserLocal] passo %d recusado: %s", step_number + 1, exc)
                    if sum(1 for s in self.steps[-4:] if not s.ok) >= 4:
                        # O motivo importa: "me embananei" esconde se o problema
                        # foi ref inventada, repetição barrada ou ação que não
                        # existe — e é isso que diz onde consertar.
                        return DriverResult(
                            tab_ref=self._tab_ref,
                            status="failed",
                            summary=(
                                "Travei: quatro tentativas seguidas foram recusadas antes de "
                                f"chegar na página. A última foi: {str(exc)[:180]}"
                            ),
                            url=last_url,
                            steps=self.steps,
                            screenshots=self.shots,
                        )
                    continue

                # Passo só é cobrado quando NÃO é olhada: ver não avança a
                # tarefa, então também não pode encurtá-la.
                if action not in ("look", "note"):
                    step_number += 1
                    self._acting_steps += 1

                thought = str(decision.get("thought") or "").strip()

                if action == "done":
                    answer = str(args.get("answer") or "").strip()
                    self.steps.append(Step("done", {}, True))
                    self._log("note", "done", answer)
                    return DriverResult(
                        tab_ref=self._tab_ref,
                        status="completed",
                        summary=self._honest(answer or "Terminei a tarefa."),
                        url=last_url,
                        steps=self.steps,
                        screenshots=self.shots,
                    )
                if action == "give_up":
                    reason = str(args.get("reason") or "").strip()
                    self.steps.append(Step("give_up", {}, True, reason))
                    self._log("note", "give_up", reason)
                    return DriverResult(
                        tab_ref=self._tab_ref,
                        status="failed",
                        summary=f"Não consegui: {reason}" if reason else "Não consegui concluir.",
                        url=last_url,
                        steps=self.steps,
                        screenshots=self.shots,
                    )

                await self._banner(thought[:90] or f"{action}…")
                await self._progress(f"passo {step_number}/{self.max_steps}: {thought[:80] or action}")

                started = time.monotonic()
                alvo = next(
                    (
                        item.get("text") or ""
                        for item in (snapshot.get("elements") or [])
                        if item.get("ref") == args.get("ref")
                    ),
                    "",
                )
                try:
                    try:
                        detail = await self._perform(action, args, snapshot)
                    except Exception as exc:  # noqa: BLE001
                        if "stale" not in str(exc).lower() or "ref" not in args:
                            raise
                        detail = await self._retry_stale(action, args, alvo)
                    self.steps.append(Step(action, args, True, detail))
                    self._log("acted", action, args, True, detail, (time.monotonic() - started) * 1000)
                    # A CAUSA do que vier a aparecer no próximo snapshot. É o que
                    # deixa o driver dizer "este painel abriu porque VOCÊ clicou
                    # aqui" em vez de tratar todo painel como intruso.
                    if action in _ACTUATING:
                        self._last_act = (
                            action,
                            alvo or str(args.get("texto") or args.get("url") or ""),
                        )
                    if action == "extract":
                        self._reads_on_screen.add(self._signature(action, args))
                    # Só ação de atuação é cobrada por mudança na tela — o
                    # próximo snapshot é quem julga (ver _note_change).
                    if action in _ACTUATING:
                        self._pending_signature = self._signature(action, args)
                except StopRequested:
                    raise
                except Exception as exc:  # noqa: BLE001 — falha do passo é dado, não crash
                    self.steps.append(Step(action, args, False, str(exc)))
                    self._log("acted", action, args, False, str(exc), (time.monotonic() - started) * 1000)
                    logger.info("[BrowserLocal] %s falhou: %s", action, exc)
                    # Uma ação que FALHOU é, por definição, um passo que não mudou
                    # nada — e merece a mesma trava do clique inútil. Faltava: o
                    # `_pending_signature` só era atribuído no caminho de sucesso,
                    # então `_note_change` saía cedo, `_stuck` nunca subia e a
                    # guarda anti-repetição nunca disparava. Foi assim que o mesmo
                    # `scroll` falhou 21 vezes seguidas sem UMA recusa (job
                    # 9025d9f8). Aqui a trava é armada na hora: não dá para
                    # esperar o próximo snapshot julgar o que já se sabe que não
                    # aconteceu.
                    if action in _ACTUATING:
                        self._stuck += 1
                        self._stuck_signature = self._signature(action, args)
                        self._pending_signature = None

            return DriverResult(
                tab_ref=self._tab_ref,
                status="failed",
                summary=(
                    f"Dei os {self.max_steps} passos que eu tinha e não cheguei ao fim. "
                    f"Parei em {last_url}."
                ),
                url=last_url,
                steps=self.steps,
                screenshots=self.shots,
            )

        except StopRequested:
            return DriverResult(
                tab_ref=self._tab_ref,
                status="stopped",
                summary="Parei porque você apertou PARE na faixa da página.",
                url=last_url,
                steps=self.steps,
                screenshots=self.shots,
            )
        except NeedsConfirmation as exc:
            return DriverResult(
                pending=exc.description,
                tab_ref=self._tab_ref,
                status="needs_confirmation",
                summary=(
                    f'Parei antes de clicar em "{exc.description}" — isso não desfaz. '
                    "Se era pra ir em frente, me diga que pode confirmar."
                ),
                url=last_url,
                steps=self.steps,
                screenshots=self.shots,
            )
        finally:
            # Delta da pasta de downloads: o que ESTA tarefa trouxe. Vem antes do
            # release porque não depende da aba, e é o dado que explica sozinho
            # um "baixou duas vezes".
            try:
                antes = {item.get("name") for item in self._downloads_before}
                self.downloaded = [
                    item
                    for item in await self._downloads_snapshot("depois da tarefa")
                    if item.get("name") not in antes
                ]
                incompletos = [i for i in self.downloaded if i.get("incomplete")]
                if incompletos and not self.download_pending:
                    self.download_pending = str(incompletos[0].get("name") or "")
                    self.download_pending_id = str(incompletos[0].get("download_id") or "")
                if self.downloaded:
                    nomes = ", ".join(
                        f"{item.get('name')} ({item.get('bytes', 0)} bytes"
                        + (", AINDA BAIXANDO)" if item.get("incomplete") else ")")
                        for item in self.downloaded
                    )
                    self._log("note", f"a tarefa trouxe {len(self.downloaded)} arquivo(s)", nomes)
                # Rede de segurança do registro: se o modelo nunca chamou
                # `downloads`, este é o primeiro (e único) momento em que se sabe
                # o que entrou na pasta. `_announce` deduplica por nome, então
                # anunciar de novo aqui não duplica o que já foi registrado.
                await self._announce(self.downloaded)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[BrowserLocal] delta de downloads falhou: %s", exc)

            # Soltar a aba é obrigatório: debugger grudado estraga a aba do usuário.
            try:
                await self._call("browser.release", {})
            except Exception as exc:  # noqa: BLE001
                logger.warning("[BrowserLocal] não consegui soltar a aba: %s", exc)
