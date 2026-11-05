"""Boundary and interruption regressions for persisted run lifecycles."""

from pathlib import Path
from unittest.mock import DEFAULT, Mock

import pytest
from pydantic import JsonValue

from openhands.tools.research_harness import HarnessError, RunRecord
from openhands.tools.research_harness.artifacts import canonical_json
from openhands.tools.research_harness.core import ResearchHarness
from openhands.tools.research_harness.models import TerminalStatus


_RUN_FIELD_BYTES = 16_384


@pytest.fixture
def harness(tmp_path: Path) -> ResearchHarness:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ResearchHarness(workspace=workspace, state_dir=tmp_path / "state")


def _sized_payload(size: int, character: str) -> dict[str, JsonValue]:
    overhead = len(canonical_json({"payload": ""}))
    character_bytes = len(canonical_json(character)) - 2
    repetitions, remainder = divmod(size - overhead, character_bytes)
    payload: dict[str, JsonValue] = {
        "payload": character * repetitions + "x" * remainder
    }
    assert len(canonical_json(payload)) == size
    return payload


def _observation() -> dict[str, JsonValue]:
    return {
        "ok": True,
        "source": "local-test-observation",
        "summary": "Observed the local test value",
        "limitations": ["One local test value was observed"],
    }


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
def test_maximum_run_fields_can_reach_every_terminal_state(
    harness: ResearchHarness,
    status: TerminalStatus,
) -> None:
    store = harness.store
    thread = store.create_thread("Can an accepted run always finish?")
    inputs = _sized_payload(_RUN_FIELD_BYTES, "é")
    result = _sized_payload(_RUN_FIELD_BYTES, "\x00")
    error = None if status == "completed" else "\x00" * 4096
    run = store.start_run(thread.id, "a" * 128, inputs=inputs, session_id="\x00" * 128)

    finished = store.finish_run(run.id, status, result=result, error=error)

    assert store.get_run(run.id) == finished
    assert finished.status == status
    assert finished.finished_at is not None
    assert finished.inputs == inputs
    assert finished.result == result
    assert finished.error == error
    assert [event.kind for event in store.list_events(run_id=run.id)] == [
        "run.started",
        f"run.{status}",
    ]


@pytest.mark.parametrize(
    "character", ["x", "é", "\x00"], ids=["ascii", "utf8", "escaped"]
)
def test_oversized_inputs_are_rejected_before_work_or_run_creation(
    harness: ResearchHarness,
    character: str,
) -> None:
    store = harness.store
    thread = store.create_thread("Reject inputs before beginning work")
    events_before = store.list_events(thread_id=thread.id)
    executed: list[bool] = []

    def work() -> dict[str, JsonValue]:
        executed.append(True)
        return _observation()

    with pytest.raises(HarnessError, match="inputs"):
        harness.observe(
            thread.id,
            "observation",
            work,
            inputs=_sized_payload(_RUN_FIELD_BYTES + 1, character),
        )

    assert executed == []
    assert store.list_runs(thread.id) == []
    assert store.list_evidence(thread.id) == []
    assert store.list_events(thread_id=thread.id) == events_before


@pytest.mark.parametrize(
    "character", ["x", "é", "\x00"], ids=["ascii", "utf8", "escaped"]
)
def test_rejected_final_result_leaves_the_run_recoverable(
    harness: ResearchHarness,
    character: str,
) -> None:
    store = harness.store
    thread = store.create_thread("Can a rejected result be recovered?")
    run = store.start_run(
        thread.id, "observation", inputs=_sized_payload(_RUN_FIELD_BYTES, "é")
    )

    with pytest.raises(HarnessError, match="result"):
        store.finish_run(
            run.id,
            "completed",
            result=_sized_payload(_RUN_FIELD_BYTES + 1, character),
        )

    assert store.get_run(run.id) == run
    assert [event.kind for event in store.list_events(run_id=run.id)] == ["run.started"]
    recovered = store.recover(run.id, "\x00" * 4096)
    assert recovered.status == "interrupted"
    assert recovered.inputs == run.inputs
    assert recovered.result == {}
    assert store.get_run(run.id) == recovered
    assert [event.kind for event in store.list_events(run_id=run.id)] == [
        "run.started",
        "run.interrupted",
    ]


@pytest.mark.parametrize("boundary", ["put", "record_evidence", "finish_run"])
def test_interruption_at_each_persistence_boundary_finalizes_the_run(
    harness: ResearchHarness,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    store = harness.store
    thread = store.create_thread("Track an interrupted persistence operation")
    targets = {
        "put": (store.artifacts, store.artifacts.put),
        "record_evidence": (store, store.record_evidence),
        "finish_run": (store, store.finish_run),
    }
    owner, method = targets[boundary]
    interruption = KeyboardInterrupt("interrupted at the persistence boundary")
    fault = Mock(wraps=method, side_effect=[interruption, DEFAULT])
    monkeypatch.setattr(owner, boundary, fault)
    executed: list[bool] = []

    def work() -> dict[str, JsonValue]:
        executed.append(True)
        return _observation()

    with pytest.raises(KeyboardInterrupt) as caught:
        harness.observe(thread.id, "observation", work)

    assert caught.value is interruption
    assert executed == [True]
    run = store.list_runs(thread.id)[0]
    assert run.status == "interrupted"
    assert run.finished_at is not None
    assert run.error
    assert run.result == {}
    evidence = store.list_evidence(thread.id)
    assert len(evidence) == (1 if boundary == "finish_run" else 0)
    expected_events = ["run.started"]
    if evidence:
        assert store.artifacts.read(evidence[0].artifact_sha) == canonical_json(
            _observation()
        )
        expected_events.append("evidence.recorded")
    expected_events.append("run.interrupted")
    assert [event.kind for event in store.list_events(run_id=run.id)] == expected_events


def test_interruption_after_successful_commit_does_not_rewrite_the_outcome(
    harness: ResearchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = harness.store
    thread = store.create_thread("Preserve an outcome committed before interruption")
    finish_run = store.finish_run
    attempts: list[TerminalStatus] = []
    interruption = KeyboardInterrupt("interrupted after the final commit")

    def finish_then_interrupt(
        run_id: str,
        status: TerminalStatus,
        *,
        result: dict[str, JsonValue] | None = None,
        error: str | None = None,
    ) -> RunRecord:
        attempts.append(status)
        finish_run(run_id, status, result=result, error=error)
        raise interruption

    monkeypatch.setattr(store, "finish_run", finish_then_interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        harness.observe(thread.id, "observation", _observation)

    assert caught.value is interruption
    assert attempts == ["completed"]
    run = store.list_runs(thread.id)[0]
    assert run.status == "completed"
    assert run.error is None
    assert run.result["ok"] is True
    evidence = store.list_evidence(thread.id)[0]
    assert run.result["evidence_id"] == evidence.id
    assert store.artifacts.read(evidence.artifact_sha) == canonical_json(_observation())
    assert [event.kind for event in store.list_events(run_id=run.id)] == [
        "run.started",
        "evidence.recorded",
        "run.completed",
    ]


@pytest.mark.parametrize("cleanup_method", ["get_run", "finish_run"])
@pytest.mark.parametrize("cleanup_exception", [OSError, KeyboardInterrupt])
def test_cleanup_failure_preserves_the_original_interruption(
    harness: ResearchHarness,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_method: str,
    cleanup_exception: type[BaseException],
) -> None:
    store = harness.store
    thread = store.create_thread("Preserve the original interruption during cleanup")
    interruption = KeyboardInterrupt("original interruption")
    cleanup_error = cleanup_exception("cleanup could not complete")

    def work() -> dict[str, JsonValue]:
        raise interruption

    with monkeypatch.context() as patches:
        patches.setattr(store, cleanup_method, Mock(side_effect=cleanup_error))
        with pytest.raises(KeyboardInterrupt) as caught:
            harness.observe(thread.id, "observation", work)

    assert caught.value is interruption
    run = store.list_runs(thread.id)[0]
    assert run.status == "running"
    assert store.list_evidence(thread.id) == []
    assert [event.kind for event in store.list_events(run_id=run.id)] == ["run.started"]
    assert store.recover(run.id, "The interrupted worker has exited").status == (
        "interrupted"
    )
