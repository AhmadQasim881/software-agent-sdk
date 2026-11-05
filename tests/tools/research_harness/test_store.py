"""Persistence, lifecycle, reference integrity, and transaction failure coverage."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from openhands.tools.research_harness import (
    ConflictError,
    HarnessError,
    HarnessStore,
    NotFoundError,
    ThreadRecord,
)


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessStore(tmp_path / "state", workspace)


def test_thread_history_and_closure_survive_restart(store: HarnessStore) -> None:
    thread = store.create_thread("Which manifests exist?", next_action="Inventory")
    updated = store.update_thread(
        thread.id, assessment="One manifest observed", expected_version=1
    )
    with pytest.raises(ValueError, match="Closing requires"):
        store.update_thread(thread.id, status="closed", expected_version=2)
    assert store.get_thread(thread.id) == updated
    store.update_thread(
        thread.id,
        status="closed",
        closure_reason="Inventory recorded",
        reopen_condition="The revision changes",
        expected_version=2,
    )
    resumed = HarnessStore(store.state_dir, store.workspace)
    resumed.update_thread(
        thread.id, status="active", next_action="Compare revision", expected_version=3
    )
    history = resumed.thread_history(thread.id)
    assert [record.version for record in history] == [1, 2, 3, 4]
    assert [record.status for record in history] == [
        "active",
        "active",
        "closed",
        "active",
    ]
    assert history[0].assessment == ""
    assert history[2].closure_reason == "Inventory recorded"
    assert resumed.get_thread(thread.id).next_action == "Compare revision"


def test_concurrent_updates_reject_stale_version(store: HarnessStore) -> None:
    thread = store.create_thread("A concurrent question")

    def update(note: str) -> ThreadRecord | ConflictError:
        another = HarnessStore(store.state_dir, store.workspace)
        try:
            return another.update_thread(thread.id, assessment=note, expected_version=1)
        except ConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["first", "second"]))
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert store.get_thread(thread.id).version == 2
    assert len(store.thread_history(thread.id)) == 2


@pytest.mark.parametrize("capture_before_finish", [True, False])
def test_failed_run_keeps_evidence_and_observed_outcome(
    store: HarnessStore,
    capture_before_finish: bool,
) -> None:
    thread = store.create_thread("Can this manifest be parsed?")
    run = store.start_run(thread.id, "manifest.parse", session_id="session-a")
    digest = store.artifacts.put(b"Invalid manifest at line 2")
    if not capture_before_finish:
        store.finish_run(run.id, "failed", error="Manifest parse failed")
    evidence = store.record_evidence(
        run.id,
        source="local fixture",
        summary="Parsing failed",
        artifact_sha=digest,
        conditions={"revision": "fixture-v1"},
        limitations=["One file only"],
    )
    if capture_before_finish:
        store.finish_run(run.id, "failed", error="Manifest parse failed")
    resumed = HarnessStore(store.state_dir, store.workspace)
    assert resumed.get_run(run.id).status == "failed"
    assert resumed.get_evidence(evidence.id) == evidence
    assert evidence.run_status_at_capture == (
        "running" if capture_before_finish else "failed"
    )
    assert (
        resumed.artifacts.read(evidence.artifact_sha) == b"Invalid manifest at line 2"
    )
    with pytest.raises(ConflictError, match="final outcome"):
        resumed.finish_run(run.id, "completed")
    events = resumed.list_events(run_id=run.id)
    assert all(event.session_id == "session-a" for event in events)
    assert sum(event.kind == "run.failed" for event in events) == 1
    assert not any(event.kind == "run.completed" for event in events)


def test_recovery_is_explicit_and_does_not_interrupt_other_runs(
    store: HarnessStore,
) -> None:
    thread = store.create_thread("Check normal operation")
    abandoned = store.start_run(thread.id, "check")
    live = store.start_run(thread.id, "inventory")
    resumed = HarnessStore(store.state_dir, store.workspace)
    assert resumed.get_run(abandoned.id).status == "running"
    with pytest.raises(ValueError, match="require a reason"):
        resumed.recover(abandoned.id, "")
    resumed.recover(abandoned.id, "Operator confirmed the old worker ended")
    assert store.get_run(abandoned.id).status == "interrupted"
    assert store.get_run(live.id).status == "running"


def test_reference_mismatches_and_unknown_records_are_rejected(
    store: HarnessStore,
) -> None:
    first = store.create_thread("First question")
    second = store.create_thread("Second question")
    run = store.start_run(first.id, "inventory")
    digest = store.artifacts.put(b"observed data")
    with pytest.raises(ConflictError, match="does not match"):
        store.record_evidence(
            run.id,
            thread_id=second.id,
            source="fixture",
            summary="Observed",
            artifact_sha=digest,
        )
    with pytest.raises(ConflictError, match="does not match"):
        store.append_event("hook.observed", thread_id=second.id, run_id=run.id)
    with pytest.raises(NotFoundError):
        store.start_run("unknown", "inventory")
    with pytest.raises(NotFoundError):
        store.record_evidence(
            "unknown", source="fixture", summary="Observed", artifact_sha=digest
        )
    assert store.list_evidence(first.id) == []
    assert store.list_evidence(second.id) == []


def test_evidence_requires_a_verified_artifact(store: HarnessStore) -> None:
    thread = store.create_thread("Check evidence integrity")
    run = store.start_run(thread.id, "inventory")
    with pytest.raises(FileNotFoundError):
        store.record_evidence(
            run.id, source="fixture", summary="Missing", artifact_sha="0" * 64
        )
    digest = store.artifacts.put(b"original")
    (store.artifacts.root / digest).write_bytes(b"modified")
    with pytest.raises(ValueError, match="integrity"):
        store.record_evidence(
            run.id, source="fixture", summary="Changed", artifact_sha=digest
        )
    assert store.list_evidence(thread.id) == []


def test_event_write_failure_rolls_back_success_transition(store: HarnessStore) -> None:
    thread = store.create_thread("Atomic execution result")
    run = store.start_run(thread.id, "inventory")
    with closing(sqlite3.connect(store.database_path)) as connection:
        connection.execute(
            "CREATE TRIGGER fail_finish BEFORE INSERT ON events "
            "WHEN NEW.kind='run.completed' BEGIN "
            "SELECT RAISE(ABORT, 'simulated disk failure'); END"
        )
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated disk failure"):
        store.finish_run(run.id, "completed", result={"count": 1})
    assert store.get_run(run.id).status == "running"
    assert [event.kind for event in store.list_events(run_id=run.id)] == ["run.started"]


def test_workspace_binding_and_directory_separation(store: HarnessStore) -> None:
    other = store.workspace.parent / "other-workspace"
    other.mkdir()
    with pytest.raises(ConflictError, match="another workspace"):
        HarnessStore(store.state_dir, other)
    with pytest.raises(HarnessError, match="overlap"):
        HarnessStore(store.workspace / "state", store.workspace)
    with pytest.raises(HarnessError, match="overlap"):
        HarnessStore(store.workspace.parent, store.workspace)
    assert HarnessStore(store.state_dir, store.workspace).list_threads() == []


def test_newer_schema_is_rejected_without_downgrading(store: HarnessStore) -> None:
    with closing(sqlite3.connect(store.database_path)) as connection:
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(HarnessError, match="schema version: 2"):
        HarnessStore(store.state_dir, store.workspace)
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_invalid_and_oversized_payloads_do_not_create_records(
    store: HarnessStore,
) -> None:
    with pytest.raises(ValueError):
        store.create_thread(" ")
    with pytest.raises(ValueError):
        store.create_thread("NaN", conditions={"value": float("nan")})
    with pytest.raises(HarnessError, match="exceeds"):
        store.create_thread("Oversized", conditions={"data": "x" * 65_536})
    assert store.list_threads() == []
    assert store.list_events() == []


@pytest.mark.parametrize("status", ["running", "unknown"])
def test_invalid_finish_status_keeps_run_running(store: HarnessStore, status) -> None:
    thread = store.create_thread("Run validation")
    with pytest.raises(ValueError):
        store.start_run(thread.id, "invalid operation")
    run = store.start_run(thread.id, "inventory")
    with pytest.raises(ValueError):
        store.finish_run(run.id, status)
    assert store.get_run(run.id).status == "running"


def test_event_pages_and_tail_are_bounded_and_ordered(store: HarnessStore) -> None:
    thread = store.create_thread("Event cursor")
    for index in range(6):
        store.append_event("hook.observed", thread_id=thread.id, payload={"i": index})
    first = store.list_events(limit=2)
    following = store.list_events(after_seq=first[-1].seq, limit=2)
    assert [event.seq for event in first + following] == [1, 2, 3, 4]
    assert [event.seq for event in store.tail_events(limit=2)] == [6, 7]
    with pytest.raises(HarnessError, match="limit"):
        store.list_events(limit=201)
    with pytest.raises(HarnessError, match="nonnegative"):
        store.list_events(after_seq=-1)
    with pytest.raises(HarnessError, match="owning operation"):
        store.append_event("run.completed", thread_id=thread.id)
    assert len(store.list_events()) == 7


def test_closed_thread_cannot_start_new_work(store: HarnessStore) -> None:
    thread = store.create_thread("Finished question")
    store.update_thread(
        thread.id,
        status="closed",
        closure_reason="Answered",
        reopen_condition="New revision",
    )
    with pytest.raises(ConflictError, match="Reopen"):
        store.start_run(thread.id, "inventory")
    assert store.list_runs(thread.id) == []
