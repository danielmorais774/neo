"""SkillManifest — metadata for a skill."""

from typing import ClassVar

from pydantic import BaseModel

from shared.types import IntentType


class SkillManifest(BaseModel):
    """Manifest describing a skill — used for registration and routing."""

    name: str
    description: str
    keywords: list[str] = []
    intent_type: IntentType
    tool_names: list[str] = []  # tool names this skill uses
    version: str = "1.0.0"

    # Registry of all skills — populated by SkillRegistry
    _registry: ClassVar[dict[str, "SkillManifest"]] = {}

    def register(self) -> None:
        """Register this manifest in the global registry."""
        SkillManifest._registry[self.name] = self

    @classmethod
    def get(cls, name: str) -> "SkillManifest":
        """Get a registered skill manifest by name."""
        if name not in cls._registry:
            raise KeyError(f"Skill '{name}' not found in registry. Available: {list(cls._registry.keys())}")
        return cls._registry[name]

    @classmethod
    def all(cls) -> dict[str, "SkillManifest"]:
        """Return all registered skill manifests."""
        return dict(cls._registry)