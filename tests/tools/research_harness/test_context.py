"""Exact context budgets, selection provenance, and coherent persisted reads."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from openhands.tools.research_harness import HarnessStore
from openhands.tools.research_harness.context import assemble_context
from openhands.tools.research_harness.models import EvidenceRecord, TerminalStatus


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessStore(tmp_path / "state", workspace)


def observation(
    store: HarnessStore,
    thread_id: str,
    summary: str,
    *,
    status: TerminalStatus = "completed",
) -> EvidenceRecord:
    run = store.start_run(thread_id, "fixture.check")
    digest = store.artifacts.put(b"RAW_ARTIFACT_DO_NOT_INJECT")
    evidence = store.record_evidence(
        run.id,
        source="local fixture",
        summary=summary,
        artifact_sha=digest,
        conditions={"fixture": "v1"},
        limitations=["One bounded observation"],
    )
    store.finish_run(
        run.id, status, error=None if status == "completed" else "Observed failure"
    )
    return evidence


def test_restart_selects_latest_evidence_and_current_failed_outcome(
    store: HarnessStore,
) -> None:
    thread = store.create_thread("Which import declarations exist?")
    old = observation(store, thread.id, "Older observation")
    latest = observation(store, thread.id, "Latest failed observation", status="failed")
    store.update_thread(thread.id, next_action="Inspect the parse failure")
    before = assemble_context(store, thread.id)
    resumed = HarnessStore(store.state_dir, store.workspace)
    after = assemble_context(resumed, thread.id)
    assert before == after
    content = json.loads(str(after["context"]))
    assert content["thread"]["version"] == 2
    assert content["thread"]["next_action"] == "Inspect the parse failure"
    assert [item["id"] for item in content["evidence"]] == [latest.id, old.id]
    assert content["evidence"][0]["run_status"] == "failed"
    assert content["evidence"][0]["run_error"] == "Observed failure"
    assert latest.run_status_at_capture == "running"


@pytest.mark.parametrize("budget", [512, 768, 1024, 4096, 16384])
def test_utf8_budget_measures_delivered_text_and_audits_previews(
    store: HarnessStore, budget: int
) -> None:
    question = "🌍 إيه الخطوة الجاية؟\n" * 100
    thread = store.create_thread(
        question, conditions={"description": "معلومات طويلة " * 100}
    )
    store.update_thread(
        thread.id,
        assessment="ملاحظة محفوظة " * 500,
        next_action="افتح الملخص " * 100,
    )
    result = assemble_context(store, thread.id, max_bytes=budget)
    text = str(result["context"])
    assert result["context_bytes"] == len(text.encode("utf-8"))
    assert len(text.encode("utf-8")) <= budget
    assert len(text.encode("utf-8")) > len(text)
    assert "\ufffd" not in text
    content = json.loads(text)
    assert content["thread"]["question_preview"].startswith("🌍 إيه")
    manifest = json.loads(store.artifacts.read(str(result["manifest_sha"])))
    assert manifest["context"] == text
    assert manifest["context_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert manifest["context_bytes"] == result["context_bytes"]
    assert manifest["records"][0]["preview"] == content["thread"]
    assert manifest["records"][0]["fields"][0]["truncated"] is True
    assert len(store.artifacts.read(str(result["manifest_sha"]))) <= 1_048_576


def test_manifest_accounts_for_every_candidate_and_older_history(
    store: HarnessStore,
) -> None:
    thread = store.create_thread("Select recent observations")
    evidence = [
        observation(store, thread.id, f"Observation {index}: " + "detail " * 80)
        for index in range(6)
    ]
    result = assemble_context(store, thread.id, max_bytes=1300, evidence_limit=3)
    content = json.loads(str(result["context"]))
    manifest = json.loads(store.artifacts.read(str(result["manifest_sha"])))
    candidates = manifest["records"][1:]
    assert [item["id"] for item in candidates] == [
        item.id for item in reversed(evidence[-3:])
    ]
    assert manifest["scope"]["evidence_total"] == 6
    assert manifest["scope"]["older_outside_window"] == 3
    assert result["older_outside_window"] == 3
    selected = [item for item in candidates if item["disposition"] == "selected"]
    omitted = [item for item in candidates if item["disposition"] == "omitted"]
    assert len(selected) == 1
    assert len(omitted) == 2
    assert result["selected_evidence"] == len(selected)
    assert result["omitted_candidates"] == len(omitted)
    assert [item["preview"] for item in selected] == content["evidence"]
    assert all(item["reason"] == "byte_budget" for item in omitted)
    assert content["omissions"] == {"older_evidence": 3, "candidate_evidence": 2}


def test_context_does_not_inject_raw_data_or_create_recursive_evidence(
    store: HarnessStore,
) -> None:
    thread = store.create_thread("Summarize recorded observations")
    evidence = observation(store, thread.id, "Safe observation summary")
    store.append_event(
        "fixture.telemetry",
        thread_id=thread.id,
        payload={"raw": "RAW_EVENT_DO_NOT_INJECT"},
    )
    result = assemble_context(store, thread.id)
    assemble_context(store, thread.id)
    manifest = store.artifacts.read(str(result["manifest_sha"]))
    assert b"RAW_ARTIFACT_DO_NOT_INJECT" not in manifest
    assert b"RAW_EVENT_DO_NOT_INJECT" not in manifest
    assert "Safe observation summary" in str(result["context"])
    assert store.list_evidence(thread.id) == [evidence]
    assert len(store.list_runs(thread.id)) == 1
    assert (
        sum(
            event.kind == "context.assembled"
            for event in store.list_events(thread_id=thread.id)
        )
        == 2
    )


def test_context_records_share_one_read_snapshot(
    store: HarnessStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = store.create_thread("Snapshot consistency")
    run = store.start_run(thread.id, "fixture.check")
    digest = store.artifacts.put(b"observed")
    first = store.record_evidence(
        run.id, source="fixture", summary="First", artifact_sha=digest
    )
    original_connect = sqlite3.connect
    changed = False
    errors: list[Exception] = []

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)

        def interleave(statement: str) -> None:
            nonlocal changed
            if changed or not statement.startswith("SELECT COUNT(*) FROM evidence"):
                return
            changed = True
            try:
                store.finish_run(run.id, "failed", error="Later failure")
                observation(store, thread.id, "Later observation")
                store.update_thread(thread.id, assessment="Later assessment")
            except Exception as error:
                errors.append(error)

        connection.set_trace_callback(interleave)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    snapshot_thread, records, total = store.context_records(thread.id)
    assert changed and not errors
    assert snapshot_thread.version == 1
    assert total == 1
    assert len(records) == 1
    assert records[0][0].id == first.id
    assert records[0][1].status == "running"
    assert store.get_thread(thread.id).version == 2
    assert store.get_run(run.id).status == "failed"
    assert len(store.list_evidence(thread.id)) == 2


@pytest.mark.parametrize("budget", [True, 511, 16385])
def test_invalid_byte_budget_is_rejected(store: HarnessStore, budget: int) -> None:
    thread = store.create_thread("Budget validation")
    with pytest.raises(ValueError, match="max_bytes"):
        assemble_context(store, thread.id, max_bytes=budget)


@pytest.mark.parametrize("limit", [True, 0, 51])
def test_invalid_candidate_window_is_rejected(store: HarnessStore, limit: int) -> None:
    thread = store.create_thread("Window validation")
    with pytest.raises(ValueError, match="limit"):
        assemble_context(store, thread.id, evidence_limit=limit)
