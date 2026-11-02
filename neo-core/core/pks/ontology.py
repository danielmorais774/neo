"""Controlled ontology for the Personal Knowledge Store."""

from __future__ import annotations

import re
import unicodedata
from enum import Enum


class EntityType(str, Enum):
    IDENTITY = "identity"
    PERSON = "person"
    PROJECT = "project"
    COMPANY = "company"
    DEVICE = "device"
    TECHNOLOGY = "technology"
    DOCUMENT = "document"
    WORKOUT = "workout"
    EXERCISE = "exercise"
    MUSCLE_GROUP = "muscle_group"
    MEAL = "meal"
    FOOD = "food"
    SUPPLEMENT = "supplement"
    HEALTH_STATE = "health_state"
    RESTRICTION = "restriction"
    ROUTINE = "routine"
    GOAL = "goal"
    PREFERENCE = "preference"
    VEHICLE = "vehicle"
    PROPERTY = "property"
    ACCOUNT = "account"
    PET = "pet"
    PLACE = "place"
    SOFTWARE = "software"
    HARDWARE_PROBLEM = "hardware_problem"


class RelationType(str, Enum):
    OWNS = "owns"
    WORKS_ON = "works_on"
    USES_STACK = "uses_stack"
    DEVELOPED_ON = "developed_on"
    USED_FOR = "used_for"
    HAS_EXERCISE = "has_exercise"
    TARGETS = "targets"
    USES_EQUIPMENT = "uses_equipment"
    SIBLING_OF = "sibling_of"
    CHILD_OF = "child_of"
    PARENT_OF = "parent_of"
    BELONGS_TO = "belongs_to"
    RELATED_TO = "related_to"
    MENTIONS = "mentions"
    LOCATED_IN = "located_in"
    HAS_MEMBER = "has_member"
    PERFORMED = "performed"


SENSITIVITY_LEVELS = {"public", "internal", "private", "sensitive", "secret"}
VALUE_TYPES = {"text", "number", "boolean", "date", "json"}


def normalize_token(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFD", value.strip().lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9_ ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ── Busca pela CHAVE do campo ────────────────────────────────────────────────
# Medido em 2026-10-22 (sessão 369d812c): "ta lembrado do meu cep?" não achava
# `Daniel Morais.cep` porque NADA no índice associa a palavra "cep" à entidade —
# o `search_text` é nome + apelidos + metadata, e o ranking v2 indexa só o VALOR
# do campo. A única coisa que a palavra "cep" encontrava era "trí-cep-s".
# Daqui saem os dois sentidos da correção: que palavras indexar por causa de uma
# chave, e que chaves procurar por causa de uma palavra.

#: Escrituração da máquina, não conhecimento do usuário. Mora num campo do PKS
#: por conveniência de armazenamento e tem o próprio caminho até o prompt —
#: repetir o JSON cru no bloco de conhecimento é duplicata cara: sozinho, o
#: `neo_affect` ocupava 2152 caracteres (~900 tokens) e estourava o orçamento
#: de 900, derrubando o bloco INTEIRO, CEP junto.
INTERNAL_FIELD_KEYS = frozenset({"neo_affect"})

#: Chave genérica demais para IDENTIFICAR uma entidade. Perguntar "descrição"
#: não nomeia um fato — e traria as 11 entidades que têm uma.
GENERIC_FIELD_KEYS = frozenset({
    "description", "descricao", "content", "conteudo", "label", "rotulo",
    "note", "notes", "nota", "notas", "observacao", "obs", "comment",
    "properties", "propriedades", "metadata", "value", "valor",
    "type", "tipo", "name", "nome", "full_name", "title", "titulo",
    "id", "key", "chave", "status", "source", "fonte", "date", "data",
    "summary", "resumo", "text", "texto", "info",
    # `os` é artigo em português antes de ser sistema operacional: "quais OS
    # meus treinos" traria todo aparelho que tem o campo. Quem pergunta de
    # verdade diz "sistema operacional", que não é um token só.
    "os",
})

#: Como o usuário PERGUNTA por um campo, além do nome da chave em si. À
#: esquerda a chave canônica (como está na `pks_entity_field`), à direita as
#: palavras da frase dele. Lista curta de propósito: termo genérico aqui faz a
#: entidade casar com qualquer pergunta — "documento" ficou de fora de `cpf`
#: justamente por ser também um tipo de entidade.
FIELD_KEY_TERMS: dict[str, tuple[str, ...]] = {
    "cep": ("cep", "endereco", "postal"),
    "address": ("address", "endereco", "logradouro"),
    "endereco": ("endereco", "address", "logradouro", "cep"),
    "bairro": ("bairro",),
    "cidade": ("cidade", "city"),
    "cpf": ("cpf",),
    "cnpj": ("cnpj",),
    "rg": ("rg",),
    "email": ("email",),
    "phone": ("phone", "telefone", "celular", "fone"),
    "telefone": ("telefone", "phone", "celular", "fone"),
    "celular": ("celular", "telefone", "phone", "fone"),
    "whatsapp": ("whatsapp", "zap"),
    "birthday": ("birthday", "aniversario", "nascimento"),
    "cpu": ("cpu", "processador"),
    "gpu": ("gpu", "placa de video"),
    # "memoria" NÃO entra: nesta casa a palavra quase sempre quer dizer a
    # memória do Neo, e ela fazia "lembra do meu cep?" puxar os computadores
    # que têm RAM. "ram" sozinho não é ambíguo.
    "memory": ("memory", "ram"),
    "ram_gb": ("ram_gb", "ram"),
    "weight": ("weight", "peso"),
    "weight_kg": ("weight_kg", "peso"),
    "height": ("height", "altura"),
    "schedule": ("schedule", "horario"),
    "frequency": ("frequency", "frequencia"),
    "sets": ("sets", "series"),
    "reps": ("reps", "repeticoes"),
}


def field_key_terms(key: str | None) -> tuple[str, ...]:
    """Palavras que devem ACHAR a entidade por ela ter este campo.

    Vazio para chave genérica: `description` não identifica ninguém.
    """
    norm = normalize_token(key)
    if not norm or norm in GENERIC_FIELD_KEYS or norm in INTERNAL_FIELD_KEYS:
        return ()
    termos = [norm, *FIELD_KEY_TERMS.get(norm, ())]
    return tuple(dict.fromkeys(t for t in termos if t))


def _build_term_index() -> dict[str, tuple[str, ...]]:
    """Inverso de `FIELD_KEY_TERMS`: palavra do usuário → chaves que ela nomeia."""
    idx: dict[str, list[str]] = {}
    for chave in FIELD_KEY_TERMS:
        for termo in field_key_terms(chave):
            alvos = idx.setdefault(termo, [])
            if chave not in alvos:
                alvos.append(chave)
    return {termo: tuple(chaves) for termo, chaves in idx.items()}


_TERM_TO_FIELD_KEYS = _build_term_index()


def field_keys_for_term(term: str | None) -> tuple[str, ...]:
    """Chaves da `pks_entity_field` que esta palavra pode estar nomeando.

    Palavra fora do mapa vale por si mesma (`headquarters` acha `headquarters`);
    palavra genérica não vale nada. Termo de 1–2 letras também não: "os" já é
    tratado pelo mapa, o resto seria ruído.
    """
    norm = normalize_token(term)
    if not norm or norm in GENERIC_FIELD_KEYS or norm in INTERNAL_FIELD_KEYS:
        return ()
    conhecidas = _TERM_TO_FIELD_KEYS.get(norm)
    if conhecidas:
        return conhecidas
    return (norm,) if len(norm) >= 3 else ()


class OntologyResolver:
    """Maps natural-language labels to canonical ontology values."""

    ENTITY_ALIASES: dict[str, EntityType] = {
        "identidade": EntityType.IDENTITY,
        "personalidade": EntityType.IDENTITY,
        "persona": EntityType.IDENTITY,
        "pessoa": EntityType.PERSON,
        "contato": EntityType.PERSON,
        "amigo": EntityType.PERSON,
        "familia": EntityType.PERSON,
        "projeto": EntityType.PROJECT,
        "empresa": EntityType.COMPANY,
        "trabalho": EntityType.COMPANY,
        "pc": EntityType.DEVICE,
        "notebook": EntityType.DEVICE,
        "laptop": EntityType.DEVICE,
        "computador": EntityType.DEVICE,
        "computer": EntityType.DEVICE,
        "desktop": EntityType.DEVICE,
        "celular": EntityType.DEVICE,
        "telefone": EntityType.DEVICE,
        "phone": EntityType.DEVICE,
        "dispositivo": EntityType.DEVICE,
        "device": EntityType.DEVICE,
        "hardware": EntityType.DEVICE,
        "maquina": EntityType.DEVICE,
        "monitor": EntityType.DEVICE,
        "teclado": EntityType.DEVICE,
        "keyboard": EntityType.DEVICE,
        "mouse": EntityType.DEVICE,
        "stack": EntityType.TECHNOLOGY,
        "framework": EntityType.TECHNOLOGY,
        "orm": EntityType.TECHNOLOGY,
        "runtime": EntityType.TECHNOLOGY,
        "linguagem": EntityType.TECHNOLOGY,
        "database": EntityType.TECHNOLOGY,
        "banco de dados": EntityType.TECHNOLOGY,
        "documento": EntityType.DOCUMENT,
        "treino": EntityType.WORKOUT,
        "academia": EntityType.WORKOUT,
        "workout": EntityType.WORKOUT,
        "musculacao": EntityType.WORKOUT,
        "exercicio": EntityType.EXERCISE,
        "exercise": EntityType.EXERCISE,
        "musculo": EntityType.MUSCLE_GROUP,
        "grupo muscular": EntityType.MUSCLE_GROUP,
        "refeicao": EntityType.MEAL,
        "dieta": EntityType.MEAL,
        "diet": EntityType.MEAL,
        "alimentacao": EntityType.MEAL,
        "comida": EntityType.FOOD,
        "alimento": EntityType.FOOD,
        "food": EntityType.FOOD,
        "suplemento": EntityType.SUPPLEMENT,
        "supplement": EntityType.SUPPLEMENT,
        "saude": EntityType.HEALTH_STATE,
        "restricao": EntityType.RESTRICTION,
        "rotina": EntityType.ROUTINE,
        "objetivo": EntityType.GOAL,
        "meta": EntityType.GOAL,
        "preferencia": EntityType.PREFERENCE,
        "veiculo": EntityType.VEHICLE,
        "carro": EntityType.VEHICLE,
        "imovel": EntityType.PROPERTY,
        "casa": EntityType.PROPERTY,
        "conta": EntityType.ACCOUNT,
        "pet": EntityType.PET,
        "animal": EntityType.PET,
        "lugar": EntityType.PLACE,
        "local": EntityType.PLACE,
        "software": EntityType.SOFTWARE,
        "app": EntityType.SOFTWARE,
        "aplicativo": EntityType.SOFTWARE,
        "problema": EntityType.HARDWARE_PROBLEM,
        "defeito": EntityType.HARDWARE_PROBLEM,
        "bug": EntityType.HARDWARE_PROBLEM,
        "erro": EntityType.HARDWARE_PROBLEM,
    }

    RELATION_ALIASES: dict[str, RelationType] = {
        "possui": RelationType.OWNS,
        "tem": RelationType.OWNS,
        "dono": RelationType.OWNS,
        "trabalha em": RelationType.WORKS_ON,
        "participa": RelationType.WORKS_ON,
        "usa stack": RelationType.USES_STACK,
        "stack": RelationType.USES_STACK,
        "desenvolvido em": RelationType.DEVELOPED_ON,
        "usado para": RelationType.USED_FOR,
        "contém": RelationType.HAS_EXERCISE,
        "contem": RelationType.HAS_EXERCISE,
        "tem exercicio": RelationType.HAS_EXERCISE,
        "atinge": RelationType.TARGETS,
        "trabalha": RelationType.TARGETS,
        "usa equipamento": RelationType.USES_EQUIPMENT,
        "irmao": RelationType.SIBLING_OF,
        "filho": RelationType.CHILD_OF,
        "pai": RelationType.PARENT_OF,
        "pertence": RelationType.BELONGS_TO,
        "relacionado": RelationType.RELATED_TO,
        "menciona": RelationType.MENTIONS,
        "localizado": RelationType.LOCATED_IN,
        "membro": RelationType.HAS_MEMBER,
        "realizou": RelationType.PERFORMED,
    }

    FIELD_ALIASES: dict[str, str] = {
        "processador": "cpu",
        "processor": "cpu",
        "memoria": "memory",
        "ram": "memory",
        "placa de video": "gpu",
        "gpu": "gpu",
        "sistema operacional": "os",
        "so": "os",
        "series": "sets",
        "séries": "sets",
        "repeticoes": "reps",
        "repetições": "reps",
        "peso": "weight",
        "peso kg": "weight_kg",
        "altura": "height",
        "horario": "schedule",
        "horário": "schedule",
        "frequencia": "frequency",
        "frequência": "frequency",
    }

    @staticmethod
    def _depluralize(token: str) -> list[str]:
        """Candidate singular forms for a (PT/EN) plural token, longest-strip first."""
        forms: list[str] = []
        if token.endswith("es") and len(token) > 4:
            forms.append(token[:-2])  # monitores -> monitor, devices -> devic
        if token.endswith("s") and len(token) > 3:
            forms.append(token[:-1])  # dispositivos -> dispositivo, devices -> device
        return forms

    def resolve_entity_type(self, value: str | None) -> EntityType | None:
        norm = normalize_token(value)
        if not norm:
            return None
        # 1. canonical type name ("device", "project", ...)
        try:
            return EntityType(norm.replace(" ", "_"))
        except ValueError:
            pass
        # 2. explicit PT/EN natural-language alias
        alias = self.ENTITY_ALIASES.get(norm)
        if alias:
            return alias
        # 3. plural fallback: "devices" -> "device", "metas" -> "meta"
        for singular in self._depluralize(norm):
            try:
                return EntityType(singular.replace(" ", "_"))
            except ValueError:
                pass
            alias = self.ENTITY_ALIASES.get(singular)
            if alias:
                return alias
        return None

    def resolve_relation_type(self, value: str | None) -> RelationType | None:
        token = normalize_token(value).replace(" ", "_")
        if not token:
            return None
        try:
            return RelationType(token)
        except ValueError:
            return self.RELATION_ALIASES.get(normalize_token(value))

    def resolve_field_key(self, value: str | None) -> str | None:
        token = normalize_token(value)
        if not token:
            return None
        return self.FIELD_ALIASES.get(token, token.replace(" ", "_"))


class OntologyValidator:
    """Canonicalizes LLM-proposed PKS values and rejects unknown types."""

    def __init__(self, resolver: OntologyResolver | None = None) -> None:
        self.resolver = resolver or OntologyResolver()

    def entity_type(self, value: str | None) -> str | None:
        resolved = self.resolver.resolve_entity_type(value)
        return resolved.value if resolved else None

    def relation_type(self, value: str | None) -> str | None:
        resolved = self.resolver.resolve_relation_type(value)
        return resolved.value if resolved else None

    def field_key(self, value: str | None) -> str | None:
        return self.resolver.resolve_field_key(value)

    def value_type(self, value: str | None) -> str:
        token = normalize_token(value)
        return token if token in VALUE_TYPES else "text"

    def sensitivity(self, value: str | None) -> str:
        token = normalize_token(value)
        return token if token in SENSITIVITY_LEVELS else "private"
