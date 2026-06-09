"""Phase 3 — pipeline-step observability on MessageTrace."""

from observability.logger import MessageTrace


def test_add_pipeline_step_records_and_serializes():
    trace = MessageTrace(session_id="s1", user_message="oi")
    assert trace.pipeline_steps == []
    # No pipeline steps → key omitted from the persisted document.
    assert "pipeline_steps" not in trace.to_document()

    trace.add_pipeline_step(
        index=1, intent="cognitive", origin="classifier",
        input_text="se eu costumo, pode ligar", final_text="Vou ligar.",
        handoffs=["home_assistant"],
    )
    trace.add_pipeline_step(
        index=2, intent="home_assistant", origin="handoff",
        input_text="ligar o ar do quarto", final_text="Liguei.",
    )

    steps = trace.pipeline_steps
    assert [s["index"] for s in steps] == [1, 2]
    assert steps[0]["origin"] == "classifier"
    assert steps[0]["handoffs"] == ["home_assistant"]
    assert steps[1]["origin"] == "handoff"
    assert steps[1]["handoffs"] == []

    doc = trace.to_document()
    assert len(doc["pipeline_steps"]) == 2


def test_long_fields_are_truncated():
    trace = MessageTrace(session_id="s1", user_message="oi")
    trace.add_pipeline_step(
        index=1, intent="cognitive", origin="classifier",
        input_text="x" * 1000, final_text="y" * 1000,
    )
    step = trace.pipeline_steps[0]
    assert len(step["input"]) == 500
    assert len(step["final_text"]) == 500
