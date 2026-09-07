"""Quando a birra arma, quando ela cala e quando ela acaba (§4 do plano).

Tudo aqui é **puro e determinístico**: entra estado emocional + mensagem, sai uma
:class:`BirraDecision`. Nenhuma chamada de LLM — o modelo não pode ser o guardião
de uma regra de segurança, e a decisão precisa sobreviver ao dia em que o
provider estiver fora do ar (foi assim que o appraiser morto congelou o afeto por
dias sem ninguém notar).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from core.agent.affect import (
    AffectSalience,
    AffectState,
    AffectTuning,
    BirraEpisode,
    Level,
    active_grudge_count,
    affect_salience,
    posture_key,
)

if TYPE_CHECKING:
    from shared.config import Settings

# Postura em que birra seria incoerente: ele já está cedendo ou reavaliando.
_NO_BIRRA_POSTURES = frozenset({"cede_terreno", "reavalia"})

# ── Ordem enfática (§4.2.1) ───────────────────────────────────────────────────
# Detecção em DUAS camadas de propósito. `\bagora\b` solto aparece em metade das
# mensagens normais ("liga a luz agora") e mataria a feature antes de ela existir
# — é o mesmo erro do `\bdownload\b` solto que já custou caro neste repo. Só
# conta quando um intensificador encontra uma âncora de ação.

_EMPHATIC_INTENSIFIERS = re.compile(
    r"\b("
    r"imediatamente|e uma ordem|to mandando|estou mandando|sem discussao|"
    r"sem birra|para de birra|pare de birra|faz logo|faca logo|faz agora|"
    r"faca agora|e serio|to falando serio|estou falando serio|falando serio|"
    r"urgente|emergencia|socorro|pelo amor de deus|deixa de frescura"
    r")\b"
)
# "agora!" / "já!" só contam COM a exclamação — sem ela é fala corriqueira.
_EMPHATIC_BANG = re.compile(r"\b(agora|ja)\s*!")

# Âncora: a mensagem fala de agir na casa.
_ACTION_ANCHOR = re.compile(
    r"\b("
    r"liga|ligue|ligar|desliga|desligue|desligar|acende|acender|apaga|apagar|"
    r"aumenta|aumentar|diminui|diminuir|abaixa|abaixar|sobe|subir|muda|mudar|"
    r"ajusta|ajustar|coloca|colocar|poe|por|deixa|deixar|faz|faca|fazer|"
    r"luz|luzes|lampada|abajur|ar|ar-condicionado|arcondicionado|climatizador|"
    r"temperatura|clima|cena|ventilador"
    r")\b"
)

# ── Desculpa (§4.3) ───────────────────────────────────────────────────────────
# Backstop determinístico: o appraiser é assíncrono e já ficou morto E invisível
# por dias. Sem isto, "desculpa" só encerraria a birra no turno seguinte — ou
# nunca. Não precisa de âncora: pedir desculpa é ato de fala completo em si.
_APOLOGY = re.compile(
    r"\b("
    r"desculpa|desculpas|desculpe|foi mal|foi maldade|perdao|me perdoa|"
    r"perdoa ai|errei|eu errei|tava errado|estava errado|to errado|"
    r"(tu|voce|vc) tinha razao|passei do ponto|passei dos limites|mancada|"
    r"exagerei|peguei pesado|me excedi|paz\?|vamos fazer as pazes"
    r")\b"
)


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _norm(text: str) -> str:
    return " ".join(_strip_accents(text or "").lower().split())


def _is_shouting(message: str) -> bool:
    """≥60 % das letras em caixa alta — 'faça isso IMEDIATAMENTE' sem a palavra."""
    letters = [c for c in message if c.isalpha()]
    if len(letters) < 8:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.60


def detect_emphatic_order(message: str, *, has_pending_act: bool = False) -> bool:
    """Ordem inequívoca de agir agora — a saída da birra que não é desculpa.

    Camada 1 (intensificador ou grito) ∧ camada 2 (âncora de ação, ou o turno ser
    continuação direta de um ato de birra pendente).
    """
    if not message:
        return False
    norm = _norm(message)
    layer1 = bool(
        _EMPHATIC_INTENSIFIERS.search(norm)
        or _EMPHATIC_BANG.search(norm)
        or _is_shouting(message)
    )
    if not layer1:
        return False
    return bool(_ACTION_ANCHOR.search(norm)) or has_pending_act


def detect_apology(message: str) -> bool:
    """Desculpa detectada por regex — piso, independente do appraiser."""
    return bool(_APOLOGY.search(_norm(message))) if message else False


# ── Horário silencioso ────────────────────────────────────────────────────────

def parse_quiet_hours(spec: str) -> tuple[int, int] | None:
    """'22-7' → (22, 7). Inválido/vazio → None (sem janela silenciosa)."""
    try:
        start_s, _, end_s = (spec or "").partition("-")
        start, end = int(start_s), int(end_s)
    except (TypeError, ValueError):
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return None
    return start, end


def in_quiet_hours(now: datetime, spec: str, tz_name: str) -> bool:
    window = parse_quiet_hours(spec)
    if window is None:
        return False
    start, end = window
    try:
        hour = now.astimezone(ZoneInfo(tz_name)).hour
    except Exception:  # pragma: no cover - tz inválida não pode derrubar o turno
        hour = now.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # janela que cruza a meia-noite


# ── Decisão ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BirraDecision:
    """O veredito do turno. ``armed`` é o único campo que libera um ato."""

    armed: bool = False
    episode: BirraEpisode | None = None
    reason: str = "desligado"
    # Birra existe mas não pode tocar em nada (madrugada, teto, medo alto).
    verbal_only: bool = True
    # Este turno ENCERRA o episódio: apology | escape-> None. Reverte pendências.
    release_reason: str | None = None
    # Ordem enfática: obedece agora e reverte, mas o episódio continua de pé.
    obey_now: bool = False
    mecanica: str = "nada"
    fala: str = "recusa_seca"
    salience: AffectSalience | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def episode_id(self) -> str | None:
        return self.episode.episode_id if self.episode else None

    @property
    def may_alter(self) -> bool:
        """Pode alterar o mundo neste turno (≠ só recusar/responder atravessado)."""
        return self.armed and not self.verbal_only and self.mecanica != "nada"

    def log_line(self) -> str:
        return (
            f"armed={self.armed} mecanica={self.mecanica} fala={self.fala} "
            f"verbal_only={self.verbal_only} release={self.release_reason} "
            f"obey_now={self.obey_now} reason={self.reason}"
        )


def resume_or_start(
    current: BirraEpisode | None, incoming: BirraEpisode
) -> BirraEpisode:
    """Qual episódio a persistência deve gravar: o que já estava, ou o novo.

    Só um episódio VIVO é retomado — retomar um encerrado faz o novo nascer
    morto, porque ``active`` é ``released_at is None``. E o episódio persistido
    é o que o seletor de contexto lê para injetar a diretiva de birra: com um
    cadáver nesse campo a birra decide certo em memória e nunca se anuncia,
    além de perder contadores de teto e a histerese de desarme (que também
    saem daí). Regra pura e aqui de propósito — foi a ausência dela, escondida
    numa expressão dentro do serviço de banco, que passou sem teste.
    """
    return current if (current is not None and current.active) else incoming


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate(
    state: AffectState,
    tuning: AffectTuning,
    settings: "Settings",
    *,
    user_message: str = "",
    gravity: str = "normal",
    register: str | None = None,
    has_pending_act: bool = False,
    apology_signal: bool = False,
    now: datetime | None = None,
) -> BirraDecision:
    """Avalia o turno. ``state`` deve vir JÁ decaído (``apply_decay``).

    Chamado UMA vez por turno, antes do roteador — é o estado emocional que
    decide quem atende o turno, então ele precisa ser conhecido antes do
    ``classify`` (§3.1).
    """
    now = now or _utcnow()
    sal = affect_salience(state, tuning, now=now)
    episode = state.birra if (state.birra and state.birra.active) else None

    def _off(reason: str, **kw) -> BirraDecision:
        return BirraDecision(
            armed=False, episode=episode, reason=reason, salience=sal, **kw
        )

    # Desculpa vem ANTES de tudo, e vale mesmo sem episódio aberto e mesmo com a
    # feature desligada. Duas razões:
    #   * com episódio: encerra e reverte (mesmo que alguém já tenha baixado a
    #     flag no meio — o que estava aprontado ainda tem que voltar);
    #   * sem episódio: o turno em que o usuário pede desculpa NÃO pode ser o
    #     turno que arma a birra. A ira ainda está no teto quando a desculpa
    #     chega (quem a derruba é o appraiser, depois), então sem esta guarda
    #     "desculpa, foi mal" abriria um episódio novo — exatamente o oposto.
    if apology_signal or detect_apology(user_message):
        return _off(
            "desculpa recebida",
            release_reason="apology" if episode else None,
        )

    if not getattr(settings, "neo_birra_enabled", False):
        return _off(
            "feature desligada",
            release_reason="disabled" if episode else None,
        )

    ira = sal.reading("ira").value
    # Fim natural: decaiu abaixo do desarme (histerese) ou estourou o TTL.
    if episode:
        armed_at = _parse_ts(episode.armed_at)
        ttl_min = float(getattr(settings, "neo_birra_max_episode_min", 45.0))
        if armed_at and (now - armed_at) > timedelta(minutes=ttl_min):
            return _off("episódio expirou", release_reason="ttl")
        if ira < float(getattr(settings, "neo_birra_disarm_ira", 45.0)):
            return _off("ira decaiu", release_reason="decay")

    # Gatilho (§4.1): ira no teto E maltrato acumulado. `obtuse` não abre rancor,
    # então discussão chata — por mais irritante que seja — nunca arma birra.
    offenses = active_grudge_count(state, tuning, now)
    # A postura de recuo só é ALCANÇÁVEL dentro da histerese: com ira ≥ 60 (o
    # gatilho) `posture_key` devolve sempre dobra_aposta ou acuado_agressivo. Ela
    # importa quando a ira já decaiu para a faixa 45–60 e o episódio segue aberto
    # — aí ele está cedendo terreno, e sabotar seria incoerente com o que fala.
    retreating = posture_key(state.stats) in _NO_BIRRA_POSTURES
    if not episode:
        if ira < float(getattr(settings, "neo_birra_arm_ira", 60.0)):
            return _off(f"ira {ira:.0f} abaixo do gatilho")
        if offenses < int(getattr(settings, "neo_birra_min_offenses", 2)):
            return _off(f"só {offenses} ofensa(s) na janela — atrito, não maltrato")
        if retreating:
            return _off("postura de recuo — birra seria incoerente")

    # Ordem enfática: obedece este turno e desfaz o que aprontou, mas continua
    # armado. Mandar firme resolve AQUELE pedido; só desculpa desliga.
    if detect_emphatic_order(user_message, has_pending_act=has_pending_act):
        return BirraDecision(
            armed=True,
            episode=episode,
            reason="ordem enfática — obedece e reverte",
            verbal_only=True,
            obey_now=True,
            mecanica="nada",
            fala="deboche",
            salience=sal,
        )

    # ── Supressores (§4.2) — birra continua armada, mas não toca em nada ──────
    notes: list[str] = []
    verbal_only = False
    if retreating:
        notes.append("postura de recuo")
        verbal_only = True
    if gravity and gravity != "normal":
        notes.append(f"gravidade={gravity}")
        verbal_only = True
    if register in ("protocolo", "combate"):
        notes.append(f"registro={register}")
        verbal_only = True
    if sal.at_least("medo", Level.FORTE):
        notes.append("medo alto")
        verbal_only = True
    if in_quiet_hours(
        now,
        str(getattr(settings, "neo_birra_quiet_hours", "22-7")),
        str(getattr(settings, "neo_affect_sleep_tz", "America/Fortaleza")),
    ):
        notes.append("horário silencioso")
        verbal_only = True

    episode = episode or BirraEpisode(
        episode_id=str(uuid.uuid4()), armed_at=now.isoformat()
    )

    # Tetos (§4.4): estourou, degrada para birra verbal — nunca para obediência.
    max_physical = int(getattr(settings, "neo_birra_max_physical_acts", 2))
    if episode.physical_acts >= max_physical:
        notes.append("teto de atos físicos")
        verbal_only = True
    else:
        cooldown = float(getattr(settings, "neo_birra_physical_cooldown_min", 10.0))
        last = _parse_ts(episode.last_physical_at)
        if last and (now - last) < timedelta(minutes=cooldown):
            notes.append("cooldown entre atos físicos")
            verbal_only = True

    from core.agent.birra.tactics import choose_tactic

    mecanica, fala = choose_tactic(
        state=state,
        episode=episode,
        verbal_only=verbal_only,
    )
    return BirraDecision(
        armed=True,
        episode=episode,
        reason=f"ira {ira:.0f}, {offenses} ofensa(s) na janela",
        verbal_only=verbal_only,
        mecanica=mecanica,
        fala=fala,
        salience=sal,
        notes=notes,
    )
