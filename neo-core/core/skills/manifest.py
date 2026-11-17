"""SkillManifest — metadata for a skill."""

from typing import ClassVar

from pydantic import BaseModel

from shared.types import IntentType


class SkillManifest(BaseModel):
    """Manifest describing a skill — used for registration and routing.

    Nível 4 ("pipeline") da taxonomia de skills (docs/skills-library-plan.md).
    Os campos de unificação abaixo fazem deste manifest a fonte única para o
    handoff parser (``handoff``), a instrução de handoff do cognitivo
    (``handoff_line``) e a factory do SkillExecutor (``pipeline_factory``) —
    uma skill pipeline nova não precisa mais editar esses três lugares.
    """

    name: str
    description: str
    keywords: list[str] = []
    intent_type: IntentType
    tool_names: list[str] = []  # tool names this skill uses
    version: str = "1.0.0"

    # --- unificação com a biblioteca de skills ---
    kind: str = "pipeline"
    # Registro da situação que esta skill impõe enquanto roteada/ativa
    # (ex.: "oficina"). "" = não impõe. Lido pelo _derive_turn_register do
    # ai_service quando decision.skill_name/skill_hint aponta para cá.
    # (Nome ``tone_register`` porque ``register`` colidiria com o método
    # :meth:`register` do registry logo abaixo.)
    tone_register: str = ""
    # Pode ser alvo de [[HANDOFF:<name>]] do núcleo cognitivo?
    handoff: bool = True
    # Linha desta skill na instrução de handoff (curta, em PT). Vazio → usa description.
    handoff_line: str = ""
    # Caminho pontilhado "modulo.submodulo:Classe" do pipeline. O SkillExecutor
    # importa LAZY (na primeira execução) e injeta só os kwargs que o __init__
    # da classe declara (agent_executor/db/broker/pks).
    pipeline_factory: str = ""

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