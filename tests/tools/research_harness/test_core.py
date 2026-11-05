import json
import threading
from pathlib import Path

import pytest
from pydantic import JsonValue

from openhands.tools.research_harness.core import ResearchHarness
from openhands.tools.research_harness.inventory import inventory_python


@pytest.fixture
def harness(tmp_path: Path) -> ResearchHarness:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ResearchHarness(workspace=workspace, state_dir=tmp_path / "state")


def observation(ok: bool = True) -> dict[str, JsonValue]:
    return {
        "ok": ok,
        "source": "local-fixture:v1",
        "summary": "Normal operation succeeded" if ok else "Readiness failed",
        "limitations": ["Only the local fixture was checked"],
        "actual_status": 200 if ok else 503,
    }


def test_records_at_execution_boundary_and_preserves_full_result(
    harness: ResearchHarness,
) -> None:
    thread = harness.store.create_thread("Does the fixture work?")

    def work() -> dict[str, JsonValue]:
        assert harness.store.list_events()[-1].kind == "run.started"
        return observation()

    result = harness.observe(thread.id, "fixture", work, session_id="session-1")
    assert result["ok"] is True
    runs = harness.store.list_runs(thread.id)
    assert len(runs) == 1 and runs[0].status == "completed"
    evidence = harness.store.list_evidence(thread.id)[0]
    assert (
        json.loads(harness.store.artifacts.read(evidence.artifact_sha)) == observation()
    )
    assert runs[0].result["evidence_id"] == evidence.id
    assert all(
        event.session_id == "session-1"
        for event in harness.store.list_events(run_id=runs[0].id)
    )


def test_negative_observation_is_a_failed_run_with_evidence(
    harness: ResearchHarness,
) -> None:
    thread = harness.store.create_thread("Check readiness")
    result = harness.observe(thread.id, "fixture", lambda: observation(False))
    assert result["ok"] is False
    assert harness.store.list_runs(thread.id)[0].status == "failed"
    assert len(harness.store.list_evidence(thread.id)) == 1


def test_exception_is_preserved_without_a_successful_claim(
    harness: ResearchHarness,
) -> None:
    thread = harness.store.create_thread("Read configuration")

    def fail() -> dict[str, JsonValue]:
        raise ValueError("Configuration is malformed")

    result = harness.observe(thread.id, "configuration", fail)
    assert result["status"] == "failed"
    evidence = harness.store.list_evidence(thread.id)[0]
    raw = json.loads(harness.store.artifacts.read(evidence.artifact_sha))
    assert raw["error_type"] == "ValueError"
    assert "malformed" in raw["summary"]
    assert not any(
        event.kind == "run.completed" for event in harness.store.list_events()
    )


def test_interrupt_is_recorded_and_propagated(harness: ResearchHarness) -> None:
    thread = harness.store.create_thread("Interrupt the check")

    def interrupt() -> dict[str, JsonValue]:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        harness.observe(thread.id, "fixture", interrupt)
    assert harness.store.list_runs(thread.id)[0].status == "interrupted"


def test_cancellation_before_work_does_not_execute(harness: ResearchHarness) -> None:
    thread = harness.store.create_thread("Cancelled check")
    cancel = threading.Event()
    cancel.set()
    executed: list[bool] = []

    def work() -> dict[str, JsonValue]:
        executed.append(True)
        return observation()

    result = harness.observe(thread.id, "fixture", work, cancel_event=cancel)
    assert executed == []
    assert result["status"] == "interrupted"


def test_invalid_result_and_failed_artifact_write_cannot_finish_successfully(
    harness: ResearchHarness,
) -> None:
    thread = harness.store.create_thread("Validate actual output")
    invalid = harness.observe(thread.id, "invalid", lambda: {"ok": "yes"})
    assert invalid["ok"] is False
    with pytest.raises(ValueError, match="Artifact exceeds"):
        harness.observe(
            thread.id, "oversize", lambda: {**observation(), "raw": "x" * 1_048_576}
        )
    assert [run.status for run in harness.store.list_runs(thread.id)] == [
        "failed",
        "failed",
    ]
    assert len(harness.store.list_evidence(thread.id)) == 1


def test_preview_reports_omissions_and_keeps_full_evidence(
    harness: ResearchHarness,
) -> None:
    thread = harness.store.create_thread("Large observed output")
    raw = observation()
    raw.update(summary="s" * 2000, limitations=["l" * 300 for _ in range(10)])
    result = harness.observe(thread.id, "large", lambda: raw)
    assert result["summary_truncated"] is True
    assert result["limitations_omitted"] == 2
    assert result["limitations_truncated"] is True
    evidence = harness.store.list_evidence(thread.id)[0]
    assert json.loads(harness.store.artifacts.read(evidence.artifact_sha)) == raw


def test_real_inventory_results_are_preserved_across_revisions(
    harness: ResearchHarness,
) -> None:
    source = harness.store.workspace / "sample.py"
    source.write_text("import json\n", encoding="utf-8")
    thread = harness.store.create_thread("Which Python imports are declared?")
    first = harness.observe(
        thread.id, "inventory", lambda: inventory_python(harness.store.workspace)
    )
    source.write_text("def broken(\n", encoding="utf-8")
    second = harness.observe(
        thread.id, "inventory", lambda: inventory_python(harness.store.workspace)
    )
    assert first["ok"] is True and second["ok"] is False
    evidence = harness.store.list_evidence(thread.id)
    first_raw = json.loads(harness.store.artifacts.read(evidence[0].artifact_sha))
    second_raw = json.loads(harness.store.artifacts.read(evidence[1].artifact_sha))
    assert first_raw["entries"][0]["normalized"] == "import json"
    assert second_raw["complete"] is False
    assert second_raw["coverage"]["files_failed"] == 1
    assert evidence[0].artifact_sha != evidence[1].artifact_sha
