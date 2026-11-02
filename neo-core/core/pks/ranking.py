"""Nota de prioridade do PKS — ver docs/pks-ranking-plan.md.

Substitui "quem chegou primeiro, até 10 entidades" por uma nota calculada para
CADA entidade do workspace, ordena por ela e enche um orçamento de TOKENS.

Funções puras: nada aqui toca banco nem LLM. O retriever carrega o workspace
(entidades, fields, relações) e entrega; isto pontua, agrupa e empacota. É isso
que deixa o mesmo código rodar no turno, na `search_pks` e no benchmark.

Os 5 estágios (§3 do plano):
  E1 consulta     — mensagem + keywords (peso 1) e as falas anteriores (0,6^j)
  E2 candidatos   — o workspace inteiro
  E3 nota         — sinais em [0,1], soma ponderada, propagação pelo grafo
  E4 agrupamento  — folha coberta por contêiner vira sub-item dele
  E5 empacotamento — maior nota primeiro, 3 níveis, nunca passa do orçamento
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable, Sequence
from uuid import UUID

from core.pks.ledger_guard import is_ledger_memory, looks_like_money_query
from core.pks.ontology import (
    INTERNAL_FIELD_KEYS,
    OntologyResolver,
    field_key_terms,
    normalize_token,
)
from core.pks.schemas import EntityRecord, FieldRecord, RelationRecord
from core.pks.sensitivity import SECRET, effective_level, redact_value

# ── Configuração ─────────────────────────────────────────────────────────────

DEFAULT_WEIGHTS: dict[str, float] = {
    "nome": 0.30,
    "lex": 0.25,
    "conv": 0.15,
    "grafo": 0.15,
    "tipo": 0.10,
    "prior": 0.05,
    # Reservado para embeddings (F4 do plano). Peso 0 até existir vetor.
    "sem": 0.0,
}


@dataclass(frozen=True)
class RankConfig:
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    #: Abaixo disto a entidade não entra, mesmo com orçamento sobrando.
    min_score: float = 0.12
    #: Corte relativo: grupo com nota < ``relative_cutoff`` × a melhor nota fica
    #: de fora. É o que faz "treino de quarta" trazer SÓ a quarta, e "a semana"
    #: trazer os 5 dias (notas próximas entre si).
    relative_cutoff: float = 0.45
    #: Saturação do BM25: ``s / (s + kappa)``. Ver `_saturate`.
    kappa: float = 6.0
    history_turns: int = 3
    history_decay: float = 0.6
    #: Itens de uma lista de contenção antes do "+N".
    list_cap: int = 20
    #: Relações que NÃO são de contenção, por entidade, no nível completo.
    other_relations_cap: int = 12
    #: Teto de uma entidade no nível completo; acima disso ela entra compacta.
    #: Limita o estrago de uma inclusão errada (a entidade do usuário custava
    #: ~490 tokens por ter muitos fields longos).
    entity_token_cap: int = 220
    #: Orçamento-base: vale para todo grupo.
    budget_tokens: int = 880
    #: Orçamento ELÁSTICO: grupo FORTE (nota ≥ ``strong_ratio`` × a melhor) que
    #: não coube na base pode usar até este teto. A cauda fraca continua
    #: cortada na base. 0 = sem extensão.
    budget_max_tokens: int = 0
    strong_ratio: float = 0.7


# Tipos carregados sempre, por segurança (saúde/restrição) — como o retriever antigo.
GLOBAL_TYPES = frozenset({"health_state", "restriction"})

# Contenção: qual lado é o contêiner. "from" = quem tem o membro.
_CONTAINER_SIDE: dict[str, str] = {
    "has_member": "from",
    "has_exercise": "from",
    "contains": "from",
    "includes": "from",
    "part_of": "to",
    "belongs_to": "to",
    "member_of": "to",
}
_ASSOCIATIVE = frozenset({"targets", "uses", "used_for"})

# "treino" quer os dias (e o plano), não 33 exercícios soltos.
_TYPE_FAMILY: dict[str, dict[str, float]] = {
    "workout": {"workout": 1.0, "routine": 0.8, "exercise": 0.3},
    "routine": {"routine": 1.0, "workout": 0.8, "exercise": 0.3},
    "exercise": {"exercise": 1.0, "workout": 0.6, "routine": 0.3},
    "meal": {"meal": 1.0, "food": 0.6, "supplement": 0.6},
    "food": {"food": 1.0, "meal": 0.6},
}

_BM25_K1 = 1.2
_BM25_B = 0.75
# Campos do documento de uma entidade e o peso de cada um (o `multi_match`).
# IDENTIDADE (nome, apelido, tipo, vizinhos) e CONTEÚDO (texto dos fields) são
# pontuados à parte: casar só com o texto de um dossiê ("dois inquéritos" ×
# "quanto é dois mais dois") não pode valer o mesmo que casar com o nome.
_W_NAME, _W_ALIAS, _W_TYPE, _W_NEIGHBOR = 3.0, 2.0, 1.0, 0.5
# A CHAVE do campo é identidade, não conteúdo: quem pergunta "qual meu cep"
# está nomeando o FATO, e quem tem o fato é a entidade. Pesa como o tipo —
# abaixo de nome e apelido, porque a chave descreve um pedaço da entidade e
# não ela inteira.
_W_FIELD_KEY = 1.0
_HUB_DEGREE = 12
_CONTENT_FACTOR = 0.6
_FIELD_INDEX_CHARS = 2000
_HISTORY_CHARS = 2000

_STOPWORDS = frozenset(
    """
    a o as os um uma uns umas de da do das dos e em no na nos nas num numa
    por pra pro pras pros para com sem que se me te lhe vos eu tu ele ela eles
    elas voce vc voces meu minha meus minhas teu tua seu sua seus suas isso isto
    esse essa esses essas este esta estes estas aquele aquela aqui ai la ja so
    tb tbm tambem nao sim mais menos muito pouco bem ta to tava ser sou era foi
    ter tem tinha ha como qual quais quando onde quem porque pq ou mas entao
    ate sobre ao aos the of and to in on for is it
    oi ola ei opa neo hoje agora
    mostra mostrar fala falar diz dizer ve ver olha olhar confere conferir lista
    listar devolve faz fazer coloca quero queria preciso pode consegue sabe acha
    """.split()
)
# "neo" é o nome do assistente — ele está em quase toda mensagem e casava com
# "E-mail do Neo". Os verbos de pedido ("mostra", "confere") não dizem SOBRE o
# que é o pedido; quem diz é o resto da frase ou a conversa.

# Substantivo genérico de cada tipo, indexado na identidade da entidade: é o
# que faz "quais os meus dispositivos?" casar com TODOS os aparelhos. Lista
# curta de propósito — os apelidos da ontologia incluem "monitor"/"teclado"
# para device, e pôr isso em todo aparelho faria "monitor" casar com o PC.
_TYPE_WORDS: dict[str, tuple[str, ...]] = {
    "device": ("dispositivo", "aparelho", "device"),
    "workout": ("treino", "workout"),
    "exercise": ("exercicio",),
    "routine": ("rotina", "plano"),
    "muscle_group": ("musculo", "grupo muscular"),
    "person": ("pessoa",),
    "company": ("empresa",),
    "project": ("projeto",),
    "document": ("documento", "arquivo"),
    "place": ("lugar",),
    "preference": ("preferencia",),
    "goal": ("objetivo", "meta"),
    "software": ("software", "aplicativo", "app"),
    "technology": ("tecnologia",),
    "account": ("conta",),
}
# Os fields gravam o dia em inglês (weekday: thursday); a conversa é em pt.
_SYNONYMS = {
    "monday": "segunda", "tuesday": "terca", "wednesday": "quarta",
    "thursday": "quinta", "friday": "sexta", "saturday": "sabado",
    "sunday": "domingo",
}

_ONTOLOGY = OntologyResolver()


# ── E1: texto → termos ──────────────────────────────────────────────────────


def _stem(tok: str) -> str:
    """Plural pt leve: exercícios→exercicio, séries→serie, ombros→ombro.

    Não é um stemmer de verdade, e nem precisa: é aplicado dos DOIS lados
    (documento e consulta), então basta ser consistente.
    """
    if len(tok) <= 3 or tok.isdigit():
        return tok
    for suf, rep in (("oes", "ao"), ("aes", "ao"), ("ais", "al"), ("eis", "el"), ("ois", "ol")):
        if tok.endswith(suf) and len(tok) > len(suf) + 2:
            return tok[: -len(suf)] + rep
    if tok.endswith("ns") and len(tok) > 4:
        return tok[:-2] + "m"
    if len(tok) > 4 and tok.endswith(("res", "zes", "ses")):
        return tok[:-2]
    if tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


_URL = re.compile(r"https?://\S+")
_PATH = re.compile(r"(?:~|/)[\w.\-]+(?:/[\w.\-]+)+")


def _mask(text: str | None) -> str:
    """Caminho e URL viram só o nome do arquivo/último pedaço.

    "/Users/daniel/Downloads/NFSE_novembro.pdf" trazia "daniel" para a consulta,
    e o "Daniel Morais" (400 tokens) entrava no bloco de um pedido sobre nota
    fiscal.
    """
    if not text:
        return ""
    text = _URL.sub(lambda m: " " + m.group(0).rstrip("/").rsplit("/", 1)[-1] + " ", text)
    return _PATH.sub(lambda m: " " + m.group(0).rsplit("/", 1)[-1] + " ", text)


def tokenize(text: str | None) -> list[str]:
    out: list[str] = []
    for raw in normalize_token(_mask(text)).split():
        tok = _SYNONYMS.get(raw, raw)
        if len(tok) < 2 or tok in _STOPWORDS:
            continue
        out.append(_stem(tok))
    return out


def _raw_tokens(text: str | None) -> list[str]:
    return [t for t in normalize_token(_mask(text)).split() if t and t not in _STOPWORDS]


def _phrase_tokens(text: str | None) -> tuple[str, ...]:
    """Tokens para casar FRASE (nome/apelido): sem tirar stopword nem token curto.

    Tirando, o apelido "Dia C" da quarta virava ("dia",) e "esse dispositivo"
    do MacBook virava ("dispositivo",) — e aí "5 dias" e "meus dispositivos"
    casavam como se fossem o NOME, e o corte relativo derrubava o resto.
    """
    return tuple(_stem(_SYNONYMS.get(t, t)) for t in normalize_token(_mask(text)).split() if t)


# ── Documento de cada entidade ──────────────────────────────────────────────


def _field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_field_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_field_text(v) for v in value.values())
    if isinstance(value, datetime):
        return ""
    return str(value)


@dataclass
class _Doc:
    tf: dict[str, float]            # identidade: nome, apelidos, tipo, vizinhos
    length: float
    content_tf: dict[str, float]    # conteúdo: texto dos fields
    content_length: float
    phrases: list[tuple[str, ...]]
    name_tokens: tuple[str, ...]


def _visible(sensitivity: str | None) -> bool:
    return (sensitivity or "private") not in ("secret", "sensitive")


def _build_doc(
    entity: EntityRecord,
    fields: Sequence[FieldRecord],
    neighbor_names: Iterable[str],
) -> _Doc:
    tf: dict[str, float] = {}
    ctf: dict[str, float] = {}
    lengths = {"id": 0.0, "content": 0.0}

    def add(target: dict[str, float], key: str, text: str, weight: float) -> None:
        for tok in tokenize(text):
            target[tok] = target.get(tok, 0.0) + weight
            lengths[key] += weight

    add(tf, "id", entity.name, _W_NAME)
    for alias in entity.aliases:
        add(tf, "id", alias, _W_ALIAS)
    for word in _TYPE_WORDS.get(entity.type, ()):
        add(tf, "id", word, _W_TYPE)
    # Nomes dos vizinhos distinguem entidades parecidas (o treino de sexta tem
    # "Rosca Scott", o de segunda não). Mas HUB fica sem eles: a entidade do
    # usuário tem 39 relações e casava com qualquer coisa que ele possui
    # ("NFSE" → "Daniel owns NFSE"). Para hub, quem leva relevância é o
    # `f_grafo`, que amortece pelo grau.
    neighbor_names = list(neighbor_names)
    if len(neighbor_names) <= _HUB_DEGREE:
        for name in neighbor_names:
            add(tf, "id", name, _W_NEIGHBOR)
    for f in fields:
        # A chave entra mesmo quando o VALOR é redigido: no prompt a entidade
        # já aparece com `cep: «pks:cep»`, então casar pela palavra "cep" não
        # conta nada que a recuperação não fosse contar de qualquer jeito.
        # Credencial fica de fora — o ContextBuilder nem renderiza o campo.
        if effective_level(f.key, f.sensitivity) != SECRET:
            for termo in field_key_terms(f.key):
                add(tf, "id", termo, _W_FIELD_KEY)
        # Já o VALOR de field sensível/secreto não pontua: casar por ele
        # revelaria que o conteúdo existe mesmo sem renderizá-lo.
        if _visible(f.sensitivity):
            add(ctf, "content", _field_text(f.value)[:_FIELD_INDEX_CHARS], 1.0)

    phrases = [_phrase_tokens(entity.name)] + [_phrase_tokens(a) for a in entity.aliases]
    # Apelido feito só de palavra vazia ("me", "eu" do usuário) casaria com
    # qualquer "me mostra…".
    phrases = [p for p in phrases if any(tok not in _STOPWORDS and len(tok) > 1 for tok in p)]
    return _Doc(
        tf=tf,
        length=max(lengths["id"], 1.0),
        content_tf=ctf,
        content_length=max(lengths["content"], 1.0),
        phrases=[p for p in phrases if p],
        name_tokens=tuple(dict.fromkeys(tokenize(entity.name))),
    )


# ── Consulta ────────────────────────────────────────────────────────────────


@dataclass
class _Query:
    tokens: list[str]                 # termos filtrados (BM25, nome fora de ordem)
    weights: dict[str, float]         # termo → peso (inclui expansões fuzzy)
    seqs: list[tuple[str, ...]]       # cada texto em tokens crus, para casar frase
    seq_tokens: set[str] = field(default_factory=set)
    token_set: set[str] = field(default_factory=set)


def _build_query(texts: Iterable[str], vocab: set[str], fuzzy: bool = True) -> _Query:
    tokens: list[str] = []
    seqs: list[tuple[str, ...]] = []
    for text in texts:
        tokens.extend(tokenize(text))
        seqs.append(_phrase_tokens(text))
    weights: dict[str, float] = {}
    for tok in tokens:
        weights[tok] = 1.0
    # Erro de digitação ("pamturilha", "travesio"): termo fora do vocabulário
    # procura um vizinho próximo. Só na mensagem ATUAL — nas falas anteriores
    # (centenas de palavras) isso dominava a latência do turno.
    for tok in (list(weights) if fuzzy else []):
        if tok in vocab or len(tok) < 6:
            continue
        for cand in vocab:
            if cand[0] != tok[0] or abs(len(cand) - len(tok)) > 2:
                continue
            if SequenceMatcher(None, tok, cand).ratio() >= 0.85:
                weights[cand] = max(weights.get(cand, 0.0), 0.7)
    return _Query(
        tokens=tokens, weights=weights, seqs=seqs,
        seq_tokens={tok for s in seqs for tok in s}, token_set=set(tokens),
    )


# ── E3: sinais ──────────────────────────────────────────────────────────────


def _saturate(score: float, kappa: float) -> float:
    """BM25 → [0,1] por saturação, não por "dividir pelo melhor".

    Dividir pelo maior da consulta daria 1,0 a ALGUÉM mesmo quando nada casa
    ("quanto é 2+2?"). A saturação mantém a nota baixa quando a evidência é
    fraca e só se aproxima de 1 quando o casamento é forte.
    """
    return score / (score + kappa) if score > 0 else 0.0


class _Field:
    """Estatística de BM25 de UM grupo de campos (identidade ou conteúdo)."""

    def __init__(self, tfs: list[dict[str, float]], lengths: list[float]) -> None:
        self.n = max(len(tfs), 1)
        self.avglen = (sum(lengths) / self.n) if lengths else 1.0
        df: dict[str, int] = {}
        for tf in tfs:
            for tok in tf:
                df[tok] = df.get(tok, 0) + 1
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def score(self, q: _Query, tf: dict[str, float], length: float) -> float:
        total = 0.0
        norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * length / self.avglen)
        for tok, qw in q.weights.items():
            f = tf.get(tok)
            if f:
                total += qw * self.idf.get(tok, 0.0) * f * (_BM25_K1 + 1) / (f + norm)
        return total


class _Index:
    def __init__(self, docs: dict[UUID, _Doc]) -> None:
        self.docs = docs
        ds = list(docs.values())
        self.ident = _Field([d.tf for d in ds], [d.length for d in ds])
        self.content = _Field([d.content_tf for d in ds], [d.content_length for d in ds])
        self.idf = self.ident.idf
        self.max_idf = max(self.idf.values(), default=1.0)
        self.vocab = set(self.ident.idf) | set(self.content.idf)

    def lex(self, q: _Query, doc: _Doc, kappa: float) -> float:
        ident = _saturate(self.ident.score(q, doc.tf, doc.length), kappa)
        content = _saturate(self.content.score(q, doc.content_tf, doc.content_length), kappa)
        return max(ident, _CONTENT_FACTOR * content)

    def covered(self, q: _Query) -> set[str]:
        """Tokens da consulta cobertos por uma frase de 2+ tokens de ALGUMA
        entidade ("daniel ferreira", "pre treino")."""
        out: set[str] = set()
        for doc in self.docs.values():
            for phrase in doc.phrases:
                if len(phrase) < 2 or phrase[0] not in q.seq_tokens:
                    continue
                for seq in q.seqs:
                    n = len(phrase)
                    if any(seq[i:i + n] == phrase for i in range(len(seq) - n + 1)):
                        out.update(phrase)
        return out

    def name_match(self, q: _Query, doc: _Doc, covered: set[str] | None = None) -> float:
        """1,0 se o nome/alias aparece inteiro na consulta; 0,7 se todos os
        tokens do nome aparecem em qualquer ordem.

        Frase de um token só vale pelo quão rara ela é: "quarta" (só um treino
        tem) pesa quase 1; um alias comum pesa menos. E vale metade quando o
        token faz parte de uma frase MAIOR que outra entidade casou: em "daniel
        ferreira", o "daniel" é do Ferreira, não do usuário ("Daniel Morais").
        """
        covered = covered or set()
        best = 0.0
        for phrase in doc.phrases:
            n = len(phrase)
            if not phrase or phrase[0] not in q.seq_tokens:
                continue
            for seq in q.seqs:
                if n == 0 or n > len(seq):
                    continue
                if any(seq[i:i + n] == phrase for i in range(len(seq) - n + 1)):
                    if n > 1:
                        return 1.0
                    # Frase de um token só vale pelo quão rara ela é.
                    val = 0.6 + 0.4 * self.idf.get(phrase[0], 0.0) / self.max_idf
                    if phrase[0] in covered:
                        val *= 0.5
                    best = max(best, val)
        if best < 0.7 and len(doc.name_tokens) >= 2 and set(doc.name_tokens) <= q.token_set:
            best = 0.7
        return best


def _type_asked(texts: Iterable[str], covered: set[str] | None = None) -> set[str]:
    asked: set[str] = set()
    covered = covered or set()
    for text in texts:
        for tok in _raw_tokens(text):
            # "pré-treino" é nome de coisa, não pedido de "treino".
            if _stem(tok) in covered:
                continue
            etype = _ONTOLOGY.resolve_entity_type(tok)
            if etype is not None:
                asked.add(etype.value)
    return asked


def _type_signal(entity_type: str, asked: set[str]) -> float:
    best = 0.0
    for t in asked:
        family = _TYPE_FAMILY.get(t, {t: 1.0})
        best = max(best, family.get(entity_type, 0.0))
    return best


def _prior(entity: EntityRecord, now: datetime) -> float:
    conf = min(max(entity.confidence or 0.0, 0.0), 1.0)
    updated = getattr(entity, "updated_at", None)
    if updated is None:
        rec = 0.5
    else:
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        days = max(0.0, (now - updated).total_seconds() / 86400)
        rec = 2 ** (-days / 60)
    # Desempate fraco. Foi `updated_at` como ordem PRINCIPAL que pôs 5 folhas
    # da quarta na frente da quinta (session-098211a8).
    return 0.6 * conf + 0.4 * rec


def _alpha(rel_type: str, entity_is_member: bool | None) -> float:
    if rel_type in _CONTAINER_SIDE:
        return 0.9 if entity_is_member else 0.8
    if rel_type in _ASSOCIATIVE:
        return 0.6
    return 0.4


# ── Resultado ───────────────────────────────────────────────────────────────


@dataclass
class RankedEntity:
    entity: EntityRecord
    signals: dict[str, float]
    score: float
    eligible: bool
    reason: str = ""          # por que ficou de fora, quando ficou


@dataclass
class RankGroup:
    head: RankedEntity
    score: float
    members: list[UUID] = field(default_factory=list)   # folhas agrupadas aqui
    level: str = ""                                     # full | compact | minimal
    text: str = ""
    mandatory: bool = False


@dataclass
class RankOutcome:
    text: str
    tokens: int
    groups: list[RankGroup]              # os que ENTRARAM, na ordem do bloco
    ranked: list[RankedEntity]           # todas, por nota
    decisions: dict[UUID, str]           # entidade → decisão legível
    query_terms: list[str]
    asked_types: list[str]

    @property
    def included_ids(self) -> list[UUID]:
        return [g.head.entity.id for g in self.groups]

    def explain(self, limit: int = 15) -> list[dict[str, Any]]:
        rows = []
        for r in self.ranked[:limit]:
            rows.append({
                "entity": r.entity.name,
                "type": r.entity.type,
                "score": round(r.score, 3),
                **{k: round(v, 2) for k, v in r.signals.items()},
                "decision": self.decisions.get(r.entity.id, r.reason or "—"),
            })
        return rows


# ── Pipeline ────────────────────────────────────────────────────────────────


def _other(r: RelationRecord, eid: UUID) -> UUID:
    return r.to_entity_id if r.from_entity_id == eid else r.from_entity_id


def _container_of(r: RelationRecord) -> UUID | None:
    side = _CONTAINER_SIDE.get(r.relation_type)
    if side == "from":
        return r.from_entity_id
    if side == "to":
        return r.to_entity_id
    return None


@dataclass
class WorkspaceIndex:
    """Tudo do ranking que depende SÓ do PKS, não da mensagem.

    Montar isto (tokenizar nome, apelidos e fields de ~150 entidades) era 2/3 do
    tempo do ranking; o retriever guarda em cache enquanto o PKS não muda.
    """
    by_id: dict[UUID, EntityRecord]
    fields_by_entity: dict[UUID, list[FieldRecord]]
    live_rels: list[RelationRecord]
    incident: dict[UUID, list[RelationRecord]]
    docs: dict[UUID, _Doc]
    index: _Index
    containers: dict[UUID, list[UUID]]      # folha → contêineres
    is_container: dict[UUID, bool]


def build_workspace_index(
    entities: Sequence[EntityRecord],
    fields_by_entity: dict[UUID, list[FieldRecord]],
    relations: Sequence[RelationRecord],
) -> WorkspaceIndex:
    # E2 — candidatos: o workspace inteiro, menos segredo e extrato.
    by_id: dict[UUID, EntityRecord] = {}
    for e in entities:
        if (e.sensitivity or "private") == "secret":
            continue
        if is_ledger_memory(e.name, [f.key for f in fields_by_entity.get(e.id, [])]):
            continue
        by_id[e.id] = e

    live_rels = [
        r for r in relations
        if r.from_entity_id in by_id and r.to_entity_id in by_id
        and r.from_entity_id != r.to_entity_id
        and (r.sensitivity or "private") != "secret"
    ]
    incident: dict[UUID, list[RelationRecord]] = {eid: [] for eid in by_id}
    for r in live_rels:
        incident[r.from_entity_id].append(r)
        incident[r.to_entity_id].append(r)

    docs = {
        eid: _build_doc(
            e,
            fields_by_entity.get(eid, []),
            (by_id[_other(r, eid)].name for r in incident[eid]),
        )
        for eid, e in by_id.items()
    }

    containers: dict[UUID, list[UUID]] = {eid: [] for eid in by_id}
    members: dict[UUID, list[UUID]] = {eid: [] for eid in by_id}
    for r in live_rels:
        cont = _container_of(r)
        if cont is None:
            continue
        member = _other(r, cont)
        containers[member].append(cont)
        members[cont].append(member)

    return WorkspaceIndex(
        by_id=by_id,
        fields_by_entity=fields_by_entity,
        live_rels=live_rels,
        incident=incident,
        docs=docs,
        index=_Index(docs),
        containers=containers,
        is_container={eid: bool(members[eid]) for eid in by_id},
    )


def rank_pks(
    *,
    entities: Sequence[EntityRecord] = (),
    fields_by_entity: dict[UUID, list[FieldRecord]] | None = None,
    relations: Sequence[RelationRecord] = (),
    message: str,
    keywords: Sequence[str] | None = None,
    recent_messages: Sequence[str] | None = None,
    config: RankConfig | None = None,
    count_tokens: Callable[[str], int] | None = None,
    now: datetime | None = None,
    workspace: WorkspaceIndex | None = None,
) -> RankOutcome:
    """Pontua, agrupa e empacota o workspace para ESTA mensagem.

    Passe ``workspace`` (de ``build_workspace_index``) para reaproveitar o índice
    entre turnos; sem ele, o índice é montado aqui a partir das listas.
    """
    cfg = config or RankConfig()
    count = count_tokens or (lambda s: max(1, len(s) // 4))
    now = now or datetime.now(timezone.utc)
    w = {**DEFAULT_WEIGHTS, **(cfg.weights or {})}

    ws = workspace or build_workspace_index(entities, fields_by_entity or {}, relations)
    by_id, incident, docs, index = ws.by_id, ws.incident, ws.docs, ws.index
    fields_by_entity = ws.fields_by_entity
    other, container_of = _other, _container_of

    # E1 — consulta.
    q0_texts = [message, *(keywords or [])]
    q0 = _build_query(q0_texts, index.vocab)
    history = [
        (m or "")[:_HISTORY_CHARS]
        for m in list(recent_messages or [])[-cfg.history_turns:]
    ]
    history.reverse()   # j=1 é a fala imediatamente anterior
    hq = [_build_query([h], index.vocab, fuzzy=False) for h in history]
    covered = index.covered(q0)
    asked = _type_asked(q0_texts, covered)
    # Dinheiro não se responde pelo PKS (ledger_guard): o valor guardado aqui é
    # cópia velha do extrato. Só as globais obrigatórias entram.
    money = looks_like_money_query(message)

    # E3 — sinais diretos.
    signals: dict[UUID, dict[str, float]] = {}
    for eid, e in by_id.items():
        d = docs[eid]
        nome = index.name_match(q0, d, covered)
        lex = index.lex(q0, d, cfg.kappa)
        conv = 0.0
        for j, q in enumerate(hq, start=1):
            hit = max(index.name_match(q, d), index.lex(q, d, cfg.kappa))
            conv = max(conv, (cfg.history_decay ** j) * hit)
        signals[eid] = {
            "nome": nome, "lex": lex, "conv": conv, "grafo": 0.0,
            "tipo": _type_signal(e.type, asked), "prior": _prior(e, now), "sem": 0.0,
        }

    def evidence(eid: UUID) -> float:
        s = signals[eid]
        return max(s["nome"], s["lex"], s["conv"], s["sem"])

    degree = {eid: len(rs) for eid, rs in incident.items()}
    for eid in by_id:
        best = 0.0
        for r in incident[eid]:
            n = other(r, eid)
            ev = evidence(n)
            if ev <= 0:
                continue
            cont = container_of(r)
            is_member = None if cont is None else (cont != eid)
            # Amortecimento por grau: "Daniel Morais" (39 relações) não pode
            # puxar as 39 só porque o nome dele apareceu.
            best = max(best, ev * _alpha(r.relation_type, is_member) / math.sqrt(max(1, degree[n])))
        signals[eid]["grafo"] = best

    ranked: list[RankedEntity] = []
    for eid, e in by_id.items():
        s = signals[eid]
        score = sum(w[k] * s[k] for k in w)
        has_evidence = evidence(eid) > 0 or s["grafo"] > 0 or s["tipo"] >= 1.0
        reason = ""
        eligible = True
        if money:
            eligible, reason = False, "pergunta de dinheiro (ledger_guard)"
        elif (e.sensitivity or "private") == "sensitive" and s["nome"] <= 0:
            eligible, reason = False, "sensível sem menção direta"
        elif not has_evidence:
            eligible, reason = False, "sem evidência"
        # O desempate (prior) ordena, mas não decide se entra: somado, ele
        # empurrava para dentro entidade que só tinha a conversa antiga a favor
        # ("qual o valor da fatura" puxando o treino da quarta).
        elif score - w["prior"] * s["prior"] < cfg.min_score:
            eligible, reason = False, "abaixo do limiar"
        ranked.append(RankedEntity(entity=e, signals=s, score=score, eligible=eligible, reason=reason))
    ranked.sort(key=lambda r: (r.score, r.entity.name), reverse=True)
    by_rank = {r.entity.id: r for r in ranked}

    # E4 — agrupamento por contenção.
    containers, is_container = ws.containers, ws.is_container

    groups: dict[UUID, RankGroup] = {}
    decisions: dict[UUID, str] = {}
    for r in ranked:
        eid = r.entity.id
        if r.entity.type in GLOBAL_TYPES:
            groups[eid] = RankGroup(head=r, score=max(r.score, 1.0), mandatory=True)
            continue
        if not r.eligible:
            continue
        # Contêiner sensível sem menção direta não aparece nem puxado por folha.
        parents = [p for p in containers[eid] if by_rank[p].reason != "sensível sem menção direta"]
        if parents and not is_container[eid]:
            # Folha: não ocupa vaga própria — sobe o grupo de cada contêiner.
            for p in parents:
                g = groups.setdefault(p, RankGroup(head=by_rank[p], score=by_rank[p].score if by_rank[p].eligible else 0.0))
                g.members.append(eid)
                g.score = max(g.score, r.score)
            decisions[eid] = "agrupada em " + ", ".join(by_id[p].name for p in parents)
            continue
        g = groups.setdefault(eid, RankGroup(head=r, score=r.score))
        g.score = max(g.score, r.score)

    ordered = sorted(groups.values(), key=lambda g: (not g.mandatory, -g.score, g.head.entity.name))
    top = max((g.score for g in ordered if not g.mandatory), default=0.0)
    corte = cfg.relative_cutoff * top
    kept: list[RankGroup] = []
    for g in ordered:
        if g.mandatory or g.score >= corte:
            kept.append(g)
            continue
        decisions[g.head.entity.id] = f"abaixo do corte relativo ({g.score:.2f} < {corte:.2f})"
        for m in g.members:
            decisions.setdefault(m, "grupo abaixo do corte relativo")
    ordered = kept

    # E5 — empacotamento.
    def neighbor_score(eid: UUID) -> float:
        return by_rank[eid].score if eid in by_rank else 0.0

    direct_hit = {eid for eid, s in signals.items() if s["nome"] > 0}

    def render(g: RankGroup, level: str) -> str:
        return _render_group(
            g, level, by_id=by_id, fields_by_entity=fields_by_entity,
            incident=incident, container_of=container_of, other=other,
            neighbor_score=neighbor_score, is_container=lambda x: is_container.get(x, False),
            direct=g.head.entity.id in direct_hit,
            cfg=cfg,
        )

    budget = cfg.budget_tokens
    ceiling = max(budget, cfg.budget_max_tokens or 0)
    forte = cfg.strong_ratio * top
    taken: list[RankGroup] = []
    spent = 0
    rotulo = {"full": "entrou", "compact": "entrou (compacto)", "minimal": "entrou (só o nome)"}

    def place(g: RankGroup, limit: int) -> bool:
        nonlocal spent
        for level in ("full", "compact", "minimal"):
            text = render(g, level)
            cost = count(text) + 1
            if level == "full" and cost > cfg.entity_token_cap:
                continue
            if spent + cost <= limit:
                g.level, g.text = level, text
                taken.append(g)
                spent += cost
                return True
        return False

    sobras: list[RankGroup] = []
    for g in ordered:
        eid = g.head.entity.id
        if place(g, budget):
            decisions[eid] = rotulo[g.level]
        else:
            sobras.append(g)
    # Extensão: só para grupo FORTE. Medido em 2150 turnos reais: dos grupos
    # que a base cortava, 101 eram cauda (< 70% da melhor nota) e 14 fortes —
    # aumentar o orçamento para todos traria a cauda junto.
    for g in sobras:
        eid = g.head.entity.id
        if ceiling > budget and g.score >= forte and place(g, ceiling):
            decisions[eid] = rotulo[g.level] + " — orçamento estendido"
        else:
            decisions[eid] = "sem orçamento"
            for m in g.members:
                decisions[m] = "sem orçamento (grupo)"
    posicao = {id(g): i for i, g in enumerate(ordered)}
    taken.sort(key=lambda g: posicao[id(g)])
    limite = ceiling if spent > budget else budget

    # O total pode diferir da soma por fronteira de token — confere e corta do fim.
    text = "\n".join(g.text for g in taken).strip()
    while taken and count(text) > limite:
        dropped = taken.pop()
        decisions[dropped.head.entity.id] = "sem orçamento"
        text = "\n".join(g.text for g in taken).strip()

    for r in ranked:
        if r.entity.id not in decisions and r.reason:
            decisions[r.entity.id] = r.reason

    return RankOutcome(
        text=text,
        tokens=count(text) if text else 0,
        groups=taken,
        ranked=ranked,
        decisions=decisions,
        query_terms=sorted(q0.weights),
        asked_types=sorted(asked),
    )


# ── Renderização ────────────────────────────────────────────────────────────


def _fmt_value(value: Any, limit: int) -> str:
    if isinstance(value, datetime):
        text = value.isoformat()
    elif isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value)
    elif isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _visible_fields(fields: Sequence[FieldRecord], direct: bool) -> list[FieldRecord]:
    return [
        f for f in fields
        # O nível EFETIVO, não só o gravado: `senha` gravada como "private"
        # continua sendo segredo (é a mesma régua do índice, acima).
        if effective_level(f.key, f.sensitivity) != SECRET
        and (f.sensitivity != "sensitive" or direct)
        and f.key not in INTERNAL_FIELD_KEYS  # escrituração tem seção própria
        and f.value not in (None, "", {}, [])
    ]


def _shown(f: FieldRecord, limit: int) -> str:
    """O valor que o bloco mostra: o real, ou o ``«pks:campo»`` no lugar dele.

    É a redação do bloco antigo (``ContextBuilder``, F2 do
    docs/release-request-plan.md §7): CPF, CEP, e-mail e endereço não vão em
    claro para prompt nenhum. O v2 mostrava o valor cru — e o respondedor de
    formulário (core/browser_local/respondedor.py) usa este texto como evidência.
    """
    valor, _ = redact_value(f.key, f.value, f.sensitivity)
    return _fmt_value(valor, limit)


#: Aliases por entidade. Em entidade sem campo nenhum eles SÃO o conteúdo: o
#: "Objetivo do treino de Daniel" guarda "hipertrofia e força" só ali, e o
#: bloco sem eles dizia que o objetivo existe sem dizer qual é.
_ALIASES_CAP = 5


def _render_group(
    g: RankGroup,
    level: str,
    *,
    by_id: dict[UUID, EntityRecord],
    fields_by_entity: dict[UUID, list[FieldRecord]],
    incident: dict[UUID, list[RelationRecord]],
    container_of: Callable[[RelationRecord], UUID | None],
    other: Callable[[RelationRecord, UUID], UUID],
    neighbor_score: Callable[[UUID], float],
    is_container: Callable[[UUID], bool],
    direct: bool,
    cfg: RankConfig,
) -> str:
    e = g.head.entity
    lines = [f"### {e.name} [{e.type}]"]
    if level == "minimal":
        return lines[0]

    fields = _visible_fields(fields_by_entity.get(e.id, []), direct)
    rels = [
        r for r in incident.get(e.id, [])
        if r.sensitivity != "secret" and (r.sensitivity != "sensitive" or direct)
    ]
    # Contenção em que ESTA entidade é o contêiner: a lista que não pode ser
    # cortada em silêncio (o `[:8]` antigo tirou exercício da segunda e da terça).
    owned = [r for r in rels if container_of(r) == e.id]
    rest = [r for r in rels if container_of(r) != e.id]

    def member_label(mid: UUID, with_fields: bool) -> str:
        name = by_id[mid].name
        # Só folha leva os próprios fields junto: um membro que é contêiner (o
        # treino dentro do plano) tem bloco próprio e repetiria tudo aqui.
        if not with_fields or is_container(mid):
            return name
        extra = _visible_fields(fields_by_entity.get(mid, []), direct=False)
        if not extra:
            return name
        inline = ", ".join(f"{f.key}: {_shown(f, 40)}" for f in extra[:3])
        return f"{name} ({inline})"

    by_type: dict[str, list[UUID]] = {}
    for r in owned:
        by_type.setdefault(r.relation_type, []).append(other(r, e.id))
    for ids in by_type.values():
        ids.sort(key=lambda mid: (-neighbor_score(mid), by_id[mid].name))

    # As demais relações também saem agrupadas (uma linha por tipo e sentido),
    # ordenadas pela nota do vizinho. Uma linha por relação fazia a entidade do
    # usuário (39 relações) custar ~400 tokens.
    rest_groups: dict[tuple[str, bool], list[UUID]] = {}
    for r in rest:
        rest_groups.setdefault((r.relation_type, r.from_entity_id == e.id), []).append(other(r, e.id))
    for ids in rest_groups.values():
        ids.sort(key=lambda mid: (-neighbor_score(mid), by_id[mid].name))

    def grouped(ids: list[UUID], cap: int, label: Callable[[UUID], str]) -> str:
        shown = ids[:cap]
        more = f" (+{len(ids) - len(shown)}, use search_pks)" if len(ids) > len(shown) else ""
        return "; ".join(label(m) for m in shown) + more

    def list_lines(with_fields: bool) -> list[str]:
        # A lista de contenção sai INTEIRA (ou com "+N" explícito): o `[:8]`
        # antigo cortava exercício da segunda e da terça sem avisar.
        return [
            f"- {rel_type}: " + grouped(ids, cfg.list_cap, lambda m: member_label(m, with_fields))
            for rel_type, ids in by_type.items()
        ]

    def rest_lines(cap: int) -> list[str]:
        out = []
        for (rel_type, outgoing), ids in rest_groups.items():
            arrow = "->" if outgoing else "<-"
            out.append(f"- {rel_type} {arrow} " + grouped(ids, cap, lambda m: by_id[m].name))
        return out

    if level == "full":
        aliases = [
            a for a in (e.aliases or [])
            if a and a.strip().casefold() != e.name.strip().casefold()
        ][:_ALIASES_CAP]
        if aliases:
            lines.append("- também chamado: " + "; ".join(aliases))
        for f in fields:
            unit = f" {f.unit}" if f.unit else ""
            lines.append(f"- {f.key}: {_shown(f, 400)}{unit}")
        lines.extend(list_lines(with_fields=True))
        lines.extend(rest_lines(cfg.other_relations_cap))
        return "\n".join(lines)

    # compact
    if fields:
        lines.append("- " + "; ".join(f"{f.key}: {_shown(f, 80)}" for f in fields))
    lines.extend(list_lines(with_fields=False))
    if rest:
        lines.append(f"- +{len(rest)} outras relações")
    return "\n".join(lines)
