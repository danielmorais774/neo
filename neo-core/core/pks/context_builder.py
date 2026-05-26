"""Formats PKS retrieval results for prompt injection."""

from __future__ import annotations

from datetime import date, datetime

from core.pks.ontology import normalize_token
from core.pks.schemas import Neighborhood, RetrievalResult


class ContextBuilder:
    def format(self, result: RetrievalResult, *, user_message: str = "") -> str:
        direct_terms = {normalize_token(k) for k in result.keywords}
        direct_terms.add(normalize_token(user_message))
        lines: list[str] = []

        for neighborhood in result.neighborhoods:
            if not self._can_include(neighborhood, direct_terms):
                continue
            entity = neighborhood.entity
            lines.append(f"### {entity.name} [{entity.type}]")

            visible_fields = [
                field for field in neighborhood.fields
                if field.sensitivity != "secret"
                and (field.sensitivity != "sensitive" or self._direct_match(entity.name, direct_terms))
            ]
            for field in visible_fields:
                value = self._format_field_value(field.key, field.value)
                suffix = f" {field.unit}" if field.unit else ""
                lines.append(f"- {field.key}: {value}{suffix}")

            visible_relations = [
                relation for relation in neighborhood.relations
                if relation.sensitivity != "secret"
                and (relation.sensitivity != "sensitive" or self._direct_match(entity.name, direct_terms))
            ]
            for relation in visible_relations[:8]:
                if relation.from_entity_id == entity.id:
                    other = relation.to_name or str(relation.to_entity_id)
                    lines.append(f"- {relation.relation_type} -> {other}")
                else:
                    other = relation.from_name or str(relation.from_entity_id)
                    lines.append(f"- {other} -> {relation.relation_type}")

        return "\n".join(lines).strip()

    def _can_include(self, neighborhood: Neighborhood, direct_terms: set[str]) -> bool:
        sensitivity = neighborhood.entity.sensitivity
        if sensitivity == "secret":
            return False
        if sensitivity == "sensitive":
            return self._direct_match(neighborhood.entity.name, direct_terms)
        return True

    def _direct_match(self, name: str, direct_terms: set[str]) -> bool:
        normalized_name = normalize_token(name)
        return any(term and (term in normalized_name or normalized_name in term) for term in direct_terms)

    def _format_field_value(self, key: str, value: object) -> object:
        if key == "birthday":
            if isinstance(value, datetime):
                return value.strftime("%d/%m")
            if isinstance(value, date):
                return value.strftime("%d/%m")
        return value.isoformat() if hasattr(value, "isoformat") else value
