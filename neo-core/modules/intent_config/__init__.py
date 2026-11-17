"""Intent classifier configuration — which LLM provider/model runs the router.

The IntentRouter classifies every incoming message into an execution path. The
provider/model that does this classification used to be hardcoded to DeepSeek V4
Flash; this module makes it a runtime setting persisted in ``user_preference``
(service="neo-core", category="intent_router", key="classifier", global/user_id
NULL) and editable from the neo-panel config page.

``get_classifier_config`` is read by :class:`IntentRouter` on every classify call
(memoized in-process); ``set_classifier_config`` persists a new choice and
invalidates the cache so the next message uses it without a restart.
"""

from modules.intent_config.store import (
    DEFAULT_CLASSIFIER,
    PREF_CATEGORY,
    PREF_KEY,
    PREF_SERVICE,
    build_catalog,
    get_classifier_config,
    invalidate_cache,
    set_classifier_config,
)

__all__ = [
    "DEFAULT_CLASSIFIER",
    "PREF_CATEGORY",
    "PREF_KEY",
    "PREF_SERVICE",
    "build_catalog",
    "get_classifier_config",
    "invalidate_cache",
    "set_classifier_config",
]
