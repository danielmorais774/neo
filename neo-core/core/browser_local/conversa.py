"""A tarefa no navegador conversa ENQUANTO roda (docs/browser-conversa-plan.md).

Sessão 2bd028fb (2026-11-11): cada mudança de rumo virou "parar, um turno do
cérebro, job novo", e cada job novo começou sem o contexto do anterior. O chat
não alcançava o job ("JÁ EXISTE uma tarefa… espere"), o job não alcançava o chat
(perguntar era morrer e renascer), e o painel da aba só tinha o PARE.

Aqui mora a CAIXA DE ENTRADA de cada job de navegador VIVO: o que o Daniel
escreve no painel da aba, o recado que o cérebro manda pelo chat
(``job_falar``) e a resposta a uma pergunta pendente. O driver lê a caixa no
começo de cada rodada e, quando pergunta, espera nela.

**Por que em memória e não numa tabela.** O driver roda dentro do processo do
neo-core (o mesmo do gRPC e do JobManager) e MORRE com ele: um reinício derruba
a tarefa, e uma mensagem guardada para um driver que não existe mais não teria
quem a lesse. A caixa vive exatamente o tempo do driver. Para a tarefa que já
parou (``waiting``), o caminho continua sendo o ``continue_job`` / um pedido
novo, que reabrem a aba onde ela estava.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: De onde veio a mensagem. ``painel``: o Daniel escreveu na aba (canal da
#: extensão, nunca o DOM — a página não pode forjar). ``chat``: o cérebro
#: repassou, com ``job_falar`` ou com um ``browser_local_task`` que chegou com
#: esta tarefa rodando.
ORIGENS = ("painel", "chat")

_ONDE = {"painel": "pelo painel da aba", "chat": "pelo chat"}

#: "Pode enviar" escrito à mão também autoriza, mas só se for isso e nada além:
#: TODAS as palavras têm de ser de concordância. "pode, mas antes troca o RF-03"
#: NÃO é autorização, é instrução — e "não envie" tem "não".
_PALAVRAS_SIM = frozenset({
    "sim", "s", "pode", "ok", "okay", "isso", "manda", "mande", "envia", "envie",
    "enviar", "confirma", "confirme", "confirmo", "confirmar", "autorizo", "vai",
    "clica", "clique", "clicar", "mandar", "seguir", "segue", "continua", "claro",
    "beleza", "blz", "certo", "bora", "yes", "tá", "ta", "pode", "já", "ja",
})
_PALAVRA = re.compile(r"[a-záéíóúâêôãõç]+", re.IGNORECASE)
#: Conectivos que não mudam o sentido de "pode seguir PARA O cadastro".
_CONECTIVOS = frozenset({
    "para", "pra", "o", "a", "os", "as", "de", "do", "da", "no", "na", "com", "e",
    "agora", "logo", "seguir", "segue", "ir", "vai", "isso", "esse", "essa",
})


def _so_concordancia(texto: str, rotulo: str = "") -> bool:
    """Só palavras de concordância — e as do próprio rótulo da ação.

    Sessão 58b84b87: "pode seguir para o cadastro" (rótulo "Cadastrar") não
    contava, e ele respondeu três vezes. As palavras do rótulo entram pelo
    radical (5 letras: "cadastr" casa "cadastro"); "mas", "antes" e "não" nunca.
    """
    palavras = _PALAVRA.findall((texto or "").lower())
    radicais = {w[:5] for w in _PALAVRA.findall((rotulo or "").lower()) if len(w) >= 5}

    def vale(p: str) -> bool:
        return p in _PALAVRAS_SIM or p in _CONECTIVOS or (len(p) >= 5 and p[:5] in radicais)

    return 0 < len(palavras) <= 8 and any(p in _PALAVRAS_SIM for p in palavras) and all(
        vale(p) for p in palavras
    )


def _limite() -> int:
    try:
        from shared.ajustes import ajustes

        return int(ajustes().navegador.conversa.mensagem_max)
    except Exception:  # noqa: BLE001 — sem ajustes, o padrão
        return 1500


@dataclass
class Mensagem:
    texto: str
    origem: str
    #: Índice da opção clicada no painel (0 = a primeira), quando foi botão.
    opcao: int | None = None
    #: O cérebro declarou que o Daniel AUTORIZOU a ação que está esperando.
    autoriza: bool = False
    #: A pergunta que esta mensagem respondeu (vazio = falou por conta própria).
    resposta_a: str = ""
    #: Rótulo da ação, quando respondeu a uma CONFERÊNCIA ("Cadastrar"): o "pode
    #: enviar" vale só para aquele clique, não vira instrução solta (sessão
    #: 74408e70: o "Pode enviar" do cadastro do AppDemo foi lido depois como "pode
    #: enviar o formulário do Google").
    confirmou: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    em: datetime = field(default_factory=datetime.utcnow)

    def onde(self) -> str:
        return _ONDE.get(self.origem, self.origem)

    def para_o_agente(self) -> str:
        base = f"[{self.em:%H:%M} UTC, {self.onde()}] {self.texto}"
        if self.confirmou:
            return (
                f"{base} — resposta à conferência do clique em «{self.confirmou}»: vale SÓ "
                "para aquele clique, não para outro envio"
            )
        if self.resposta_a:
            return f"{base} — resposta à sua pergunta «{self.resposta_a[:140]}»"
        return base

    def autoriza_a_acao(self, rotulo: str = "") -> bool:
        """Esta resposta autoriza a ação que a pergunta de confirmação segurou?

        Botão da PRIMEIRA opção (é sempre "Pode …"), o cérebro dizendo que ele
        autorizou, ou um "sim"/"pode enviar" escrito e nada mais.
        """
        if self.autoriza or self.opcao == 0:
            return True
        return _so_concordancia(self.texto, rotulo)


@dataclass
class Pergunta:
    texto: str
    opcoes: list[str]
    desde: datetime = field(default_factory=datetime.utcnow)
    #: Rótulo da ação que espera autorização ("Enviar"), quando é confirmação.
    confirmacao: str = ""


class Conversa:
    """A caixa de entrada de UM job de navegador vivo."""

    def __init__(
        self,
        job_id: str,
        *,
        user_id: Any = None,
        session_id: Any = None,
        device_id: str = "",
        titulo: str = "",
    ) -> None:
        self.job_id = str(job_id)
        self.user_id = user_id
        self.session_id = session_id
        self.device_id = str(device_id or "")
        self.titulo = titulo or ""
        self._fila: list[Mensagem] = []
        self._evento = asyncio.Event()
        #: A pergunta que a tarefa fez e está esperando (``None`` = nenhuma).
        self.pergunta: Pergunta | None = None
        #: Onde a tarefa está, para o bloco do cérebro: passo, teto, aba, url.
        self.estado: dict[str, Any] = {}
        #: Tudo que chegou, em ordem (a fila só guarda o que ainda não foi lido).
        self.recebidas: list[Mensagem] = []
        #: O que a tarefa já disse no chat (avisos e perguntas), para o cérebro.
        self.ditas: list[str] = []

    def entregar(
        self,
        texto: str,
        origem: str,
        *,
        opcao: int | None = None,
        autoriza: bool = False,
    ) -> Mensagem | None:
        """Põe uma mensagem na caixa e acorda quem espera. Vazia é ignorada."""
        texto = str(texto or "").strip()
        if not texto:
            return None
        if origem not in ORIGENS:
            origem = "chat"
        mensagem = Mensagem(
            texto=texto[: _limite()],
            origem=origem,
            opcao=opcao if isinstance(opcao, int) and opcao >= 0 else None,
            autoriza=bool(autoriza),
        )
        self._fila.append(mensagem)
        self.recebidas.append(mensagem)
        self._evento.set()
        return mensagem

    def novas(self) -> list[Mensagem]:
        """Tira da caixa tudo que ainda não foi lido."""
        saida, self._fila = self._fila, []
        self._evento.clear()
        return saida

    def tem_novas(self) -> bool:
        return bool(self._fila)

    async def esperar(self, timeout: float) -> bool:
        """Espera chegar mensagem por até ``timeout`` segundos. True se chegou."""
        if self._fila:
            return True
        try:
            await asyncio.wait_for(self._evento.wait(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            pass
        return bool(self._fila)


#: Título do bloco que o cérebro lê. O roteador procura ESTA linha no
#: `system_context` para saber que há tarefa viva na conversa (`intent_router`).
MARCA_DO_BLOCO = "## Tarefa rodando AGORA no navegador do Daniel"


# ── registro dos jobs vivos ────────────────────────────────────────────────

_VIVAS: dict[str, Conversa] = {}


def abrir(job_id: Any, **kwargs: Any) -> Conversa:
    conversa = Conversa(str(job_id), **kwargs)
    _VIVAS[conversa.job_id] = conversa
    return conversa


def fechar(job_id: Any) -> None:
    _VIVAS.pop(str(job_id), None)


def viva(job_id: Any) -> Conversa | None:
    return _VIVAS.get(str(job_id))


def da_sessao(session_id: Any) -> Conversa | None:
    """A tarefa viva desta conversa (a mais nova, se por acaso houver duas)."""
    if session_id is None:
        return None
    alvo = str(session_id)
    achadas = [c for c in _VIVAS.values() if str(c.session_id or "") == alvo]
    return achadas[-1] if achadas else None


def do_device(device_id: Any) -> Conversa | None:
    """A tarefa viva neste navegador — é uma por device (``executor.py``)."""
    if not device_id:
        return None
    alvo = str(device_id)
    achadas = [c for c in _VIVAS.values() if c.device_id == alvo]
    return achadas[-1] if achadas else None


def bloco_para_o_cerebro(session_id: Any) -> str:
    """O que o cérebro precisa saber da tarefa que está rodando nesta conversa.

    Sem isto ele só descobria que havia uma tarefa ao tentar abrir outra, e aí
    respondia "espere" — foi o "travou, fechei a aba, abre outra" da sessão
    2bd028fb. Montado pelo código, barato, e só existe com tarefa VIVA.
    """
    conversa = da_sessao(session_id)
    if conversa is None:
        return ""
    estado = conversa.estado
    linhas = [
        MARCA_DO_BLOCO,
        f"«{conversa.titulo or 'tarefa no navegador'}» (job_id {conversa.job_id})",
    ]
    onde = []
    if estado.get("passo"):
        onde.append(f"passo {estado['passo']} de {estado.get('teto') or '?'}")
    if estado.get("aba"):
        onde.append(f"aba ativa {estado['aba']}")
    if estado.get("url"):
        onde.append(str(estado["url"])[:120])
    if onde:
        linhas.append(" · ".join(onde))
    if estado.get("abas"):
        linhas.append(f"abas abertas: {estado['abas']}")
    if conversa.ditas:
        linhas.append(f"o que ela disse por último no chat: «{conversa.ditas[-1][:300]}»")
    if conversa.pergunta is not None:
        p = conversa.pergunta
        opcoes = f" (opções: {' / '.join(p.opcoes)})" if p.opcoes else ""
        linhas.append(f"⏸ ELA ESTÁ ESPERANDO A RESPOSTA DO DANIEL: «{p.texto[:400]}»{opcoes}")
    linhas.append(
        "Ela continua rodando enquanto vocês conversam. Para falar com ela — repassar o "
        "que o Daniel disse, responder a pergunta dela, corrigir o rumo — use "
        "`job_falar` com a mensagem: ela lê no próximo passo e se ajusta, sem recomeçar "
        "e sem fechar aba. NÃO abra outra tarefa de navegador para o mesmo trabalho. "
        "Se o Daniel autorizou a ação que ela segurou (enviar, confirmar), mande "
        "`autoriza: true`. Para trocar de tarefa de verdade: `job_cancel` e depois a nova."
    )
    return "\n".join(linhas)
