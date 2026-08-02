"""Runtime LLM model selection for neo-core subsystem *roles* (neo-panel → Modelos).

Generalizes ``modules.intent_config`` (which owns the intent classifier) to the
other LLM-driven subsystems whose provider/model should be switchable from the
panel without a restart: the PKS memory planner and the Deep Research volume/writer
models. Each role is a global ``user_preference`` row read on the next job/plan
(memoized in-process) and written from the ``/api/v1/model-config`` route.
"""

from modules.model_config.store import (
    PREF_CATEGORY,
    PREF_SERVICE,
    ROLES,
    RoleSpec,
    build_catalog,
    get_all_configs,
    get_model_config,
    invalidate_cache,
    set_model_config,
)

__all__ = [
    "PREF_CATEGORY",
    "PREF_SERVICE",
    "ROLES",
    "RoleSpec",
    "build_catalog",
    "get_all_configs",
    "get_model_config",
    "invalidate_cache",
    "set_model_config",
]
