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
