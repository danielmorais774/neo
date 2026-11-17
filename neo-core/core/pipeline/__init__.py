"""Pipeline orchestration — chaining of feature pipelines (cascade/handoff).

See ``docs/pipeline-orchestrator-plan.md`` and
``docs/pipeline-orchestrator-implementation.md`` for the design and phases.

Phase 0/1: types + orchestrator running single-step chains (regression-zero).
Handoffs (Phase 2) and cascades (Phase 4) build on the same queue.
"""

from core.pipeline.types import Handoff, PipelineStep, StepOutcome

__all__ = ["Handoff", "PipelineStep", "StepOutcome"]
