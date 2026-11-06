import json
from pathlib import Path
from threading import Event

import pytest
from pydantic import JsonValue

from openhands.tools.research_harness.agentmemory import MemoryRecord
from openhands.tools.research_harness.models import (
    ConflictError,
    HarnessError,
    NotFoundError,
)
from openhands.tools.research_harness.service import HarnessService


@pytest.fixture
def service(tmp_path: Path) -> HarnessService:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("import json\n", encoding="utf-8")
    return HarnessService(workspace=workspace, state_dir=tmp_path / "state")


def test_shared_operations_preserve_observations_and_can_resume(
    service: HarnessService,
) -> None:
    created = service.execute(
        "thread.create", {"question": "Which imports are declared?"}
    )
    record = created["record"]
    assert isinstance(record, dict)
    thread_id = str(record["id"])
    inventory = service.execute("inventory", {"thread_id": thread_id})
    assert inventory["ok"] is True
    digest = str(inventory["artifact_sha"])
    for operation, arguments in [
        ("query", {"thread_id": thread_id, "artifact_sha": digest, "module": "json"}),
        (
            "compare",
            {"thread_id": thread_id, "before_sha": digest, "after_sha": digest},
        ),
        ("fixture", {"thread_id": thread_id}),
        ("run.get", {"run_id": inventory["run_id"]}),
        ("evidence.get", {"evidence_id": inventory["evidence_id"]}),
        ("artifact.read", {"digest": digest}),
        ("events", {}),
        ("events.export", {}),
        ("context", {"thread_id": thread_id}),
    ]:
        assert service.execute(operation, arguments)["ok"] is True
    service.execute(
        "thread.update",
        {
            "thread_id": thread_id,
            "expected_version": 1,
            "assessment": "Observed one static import; runtime behavior is untested.",
            "next_action": "Review the saved inventory coverage",
        },
    )
    resumed = HarnessService(
        workspace=service.store.workspace, state_dir=service.store.state_dir
    )
    for operation in ("thread.history", "run.list", "evidence.list"):
        result = resumed.execute(operation, {"thread_id": thread_id})
        assert result["ok"] is True and result["records"]
    assert resumed.execute("thread.get", {"thread_id": thread_id})["record"] == (
        service.store.get_thread(thread_id).model_dump(mode="json")
    )
    assert service.store.get_thread(thread_id).version == 2
    assert resumed.execute("thread.list", {})["has_more"] is False


def test_policy_validation_and_failed_observation_do_not_claim_success(
    service: HarnessService,
) -> None:
    thread = service.store.create_thread("Check coverage")
    with pytest.raises(HarnessError, match="allow_github"):
        service.execute(
            "github", {"thread_id": thread.id, "repository": "example/repo"}
        )
    with pytest.raises(HarnessError):
        service.execute("inventory", {"thread_id": thread.id, "workspace": "/outside"})
    assert service.store.list_runs(thread.id) == []
    (service.store.workspace / "sample.py").write_text(
        "def broken(\n", encoding="utf-8"
    )
    failed = service.execute(
        "inventory", {"thread_id": thread.id}, session_id="session-a"
    )
    assert failed["ok"] is False and failed["status"] == "failed"
    evidence = service.store.get_evidence(str(failed["evidence_id"]))
    raw = json.loads(service.store.artifacts.read(evidence.artifact_sha))
    assert raw["complete"] is False
    assert service.store.list_events()[-1].payload["ok"] is False
    assert service.store.list_events()[-1].session_id == "session-a"


def test_bounded_pages_keep_complete_data_and_a_resumable_cursor(
    service: HarnessService,
) -> None:
    identifiers = [
        service.store.create_thread(
            f"Thread {index}", conditions={"large": "x" * 60_000}
        ).id
        for index in range(12)
    ]
    first = service.execute("thread.list", {"limit": 100})
    assert first["ok"] is True and first["has_more"] is True
    assert "output_artifact_sha" in first
    raw = service.store.artifacts.read(str(first["output_artifact_sha"]))
    page = json.loads(raw)
    assert len(raw) < 1_048_576
    cursor = page["next_offset"]
    assert 0 < cursor < len(identifiers)
    second = service.execute("thread.list", {"limit": 100, "offset": cursor})
    page2 = json.loads(service.store.artifacts.read(str(second["output_artifact_sha"])))
    assert [
        record["id"] for record in page["records"] + page2["records"]
    ] == identifiers
    assert page2["has_more"] is False


def test_context_is_actually_delivered_and_arguments_are_auditable(
    service: HarnessService,
) -> None:
    thread = service.store.create_thread("Remember this exact question")
    result = service.execute("context", {"thread_id": thread.id, "max_bytes": 512})
    assert "Remember this exact question" in str(result["context"])
    called = [
        event
        for event in service.store.list_events()
        if event.kind == "interface.called"
    ]
    saved = service.store.artifacts.read(str(called[0].payload["arguments_sha"]))
    assert json.loads(saved) == {"thread_id": thread.id, "max_bytes": 512}
    assert service.store.list_events()[-1].payload["call_seq"] == called[0].seq


def test_cancelled_mutation_does_not_create_a_thread(service: HarnessService) -> None:
    cancel = Event()
    cancel.set()
    result = service.execute(
        "thread.create", {"question": "Cancelled creation"}, cancel_event=cancel
    )
    assert result["status"] == "interrupted"
    assert service.store.list_threads() == []


@pytest.mark.parametrize("operation", ["events", "events.export"])
def test_ledger_views_finish_without_generating_their_own_backlog(
    service: HarnessService,
    operation: str,
) -> None:
    service.execute("thread.create", {"question": "A finite ledger"})
    expected = [event.model_dump(mode="json") for event in service.store.list_events()]
    collected = []
    cursor = 0
    for _ in range(len(expected) + 1):
        result = service.execute(operation, {"limit": 1, "after_seq": cursor})
        if operation == "events.export":
            chunk = service.execute("artifact.read", {"digest": result["artifact_sha"]})
            assert chunk["encoding"] == "utf-8" and chunk["eof"] is True
            collected.extend(
                json.loads(line) for line in str(chunk["content"]).splitlines()
            )
        else:
            records = result["records"]
            assert isinstance(records, list)
            collected.extend(records)
        cursor = int(str(result["next_after_seq"]))
        if result["has_more"] is False:
            break
    else:
        pytest.fail("Ledger pagination did not finish")
    assert collected == expected
    assert len(service.store.list_events()) == len(expected)


def test_store_rejects_cursors_outside_sqlite_range(service: HarnessService) -> None:
    with pytest.raises(HarnessError):
        service.store.list_threads(offset=2**63)
    with pytest.raises(HarnessError):
        service.store.list_events(after_seq=2**63)


def test_shared_memory_lifecycle_preserves_provenance_versions_and_tombstones(
    service: HarnessService,
) -> None:
    thread = service.store.create_thread("Retain the json inventory decision")
    observation = service.execute("inventory", {"thread_id": thread.id})
    evidence_id = str(observation["evidence_id"])
    remembered = MemoryRecord.model_validate(
        service.execute(
            "memory.remember",
            {
                "thread_id": thread.id,
                "text": "Use the recorded json import as the comparison baseline.",
                "source": "Operator assessment of the saved inventory",
                "kind": "decision",
                "tags": ["baseline"],
                "evidence_ids": [evidence_id],
                "pinned": True,
            },
        )["record"]
    )
    reference: dict[str, JsonValue] = {
        "thread_id": thread.id,
        "memory_id": remembered.id,
    }
    assert service.execute("memory.get", reference)["record"] == remembered.model_dump(
        mode="json"
    )
    recalled = service.execute(
        "memory.recall", {"thread_id": thread.id, "query": "json"}
    )
    matches = recalled["records"]
    assert isinstance(matches, list) and len(matches) == 1
    match = matches[0]
    assert isinstance(match, dict)
    assert MemoryRecord.model_validate(match["record"]) == remembered
    updated = MemoryRecord.model_validate(
        service.execute(
            "memory.update",
            {
                **reference,
                "expected_version": 1,
                "text": "Compare the next json inventory with the saved baseline.",
                "tags": [],
                "pinned": False,
            },
        )["record"]
    )
    assert updated.version == 2 and updated.tags == [] and updated.pinned is False
    assert updated.source == remembered.source and updated.evidence_ids == [evidence_id]
    with pytest.raises(ConflictError):
        service.execute(
            "memory.update", {**reference, "expected_version": 1, "text": "Stale edit"}
        )
    first = service.execute("memory.history", {**reference, "limit": 1})
    assert first["records"] == [remembered.model_dump(mode="json")]
    assert first["has_more"] is True and first["next_offset"] == 1
    second = service.execute("memory.history", {**reference, "limit": 1, "offset": 1})
    assert second["records"] == [updated.model_dump(mode="json")]
    assert second["has_more"] is False
    tombstone = MemoryRecord.model_validate(
        service.execute(
            "memory.forget",
            {**reference, "expected_version": 2, "reason": "The baseline was replaced"},
        )["record"]
    )
    assert tombstone.version == 3 and tombstone.status == "forgotten"
    assert tombstone.text == updated.text and tombstone.evidence_ids == [evidence_id]
    resumed = HarnessService(
        workspace=service.store.workspace, state_dir=service.store.state_dir
    )
    assert resumed.execute("memory.get", reference)["record"] == tombstone.model_dump(
        mode="json"
    )
    restored = resumed.execute("memory.recall", {"thread_id": thread.id})
    assert restored["records"] == []
    assert restored["excluded"] == {"forgotten": 1, "expired": 0}
    assert resumed.execute("memory.history", reference)["records"] == [
        record.model_dump(mode="json") for record in (remembered, updated, tombstone)
    ]
    other = resumed.store.create_thread("A separate thread")
    with pytest.raises(NotFoundError):
        resumed.execute("memory.get", {**reference, "thread_id": other.id})
    assert len(resumed.store.list_evidence(thread.id)) == 1
