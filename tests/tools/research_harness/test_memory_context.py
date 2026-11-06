"""Memory delivery keeps provenance, omission accounting and exact byte limits."""

import hashlib
import json
from pathlib import Path

import pytest

from openhands.tools.research_harness.context import assemble_context
from openhands.tools.research_harness.models import HarnessError
from openhands.tools.research_harness.store import HarnessStore


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessStore(tmp_path / "state", workspace)


def test_memory_context_preserves_provenance_and_current_version(
    store: HarnessStore,
) -> None:
    thread = store.create_thread("Which imports changed?")
    note = store.create_memory(
        thread.id,
        "Use the json inventory baseline",
        source="Operator review",
        kind="decision",
    )
    constraint = store.create_memory(
        thread.id,
        "Keep source files unchanged",
        source="Operator scope",
        kind="constraint",
        pinned=True,
    )
    store.create_memory(thread.id, "Lunch at noon", source="Unrelated note")
    result = assemble_context(store, thread.id, query="json")
    content = json.loads(str(result["context"]))
    assert content["evidence"] == []
    assert content["memory_notice"] == "Authored notes; verify against evidence."
    assert [item["id"] for item in content["memory"]] == [constraint.id, note.id]
    assert content["memory"][1]["source"] == "Operator review"
    assert content["memory"][1]["evidence_ids"] == []
    manifest = json.loads(store.artifacts.read(str(result["manifest_sha"])))
    retrieval = manifest["memory"]["retrieval"]
    assert retrieval["eligible_notes"] == 3
    assert retrieval["matching_notes"] == 1
    assert retrieval["pinned_constraints_supplemented"] == 1
    assert len(manifest["records"]) == 1  # Notes are separate from evidence.
    assert [item["matched_query"] for item in manifest["memory"]["records"]] == [
        False,
        True,
    ]
    store.update_memory(
        thread.id,
        note.id,
        expected_version=1,
        text="Use the updated json inventory baseline",
    )
    resumed = HarnessStore(store.state_dir, store.workspace)
    restored = json.loads(
        str(assemble_context(resumed, thread.id, query="json")["context"])
    )
    assert restored["memory"][1]["version"] == 2
    assert "updated json" in restored["memory"][1]["text"]
    assert resumed.list_evidence(thread.id) == []


@pytest.mark.parametrize("budget", [512, 768, 1024, 4096, 16384])
def test_memory_shares_exact_context_budget_and_reports_every_omission(
    store: HarnessStore, budget: int
) -> None:
    thread = store.create_thread("🌍 سؤال عن ملاحظة " * 80)
    for index in range(12):
        store.create_memory(
            thread.id,
            f"ملاحظة {index}: " + "تفاصيل محفوظة " * 90,
            source="مراجعة بشرية " * 50,
            kind="constraint" if index == 0 else "note",
            pinned=index == 0,
        )
    result = assemble_context(
        store, thread.id, max_bytes=budget, query="ملاحظة", memory_limit=8
    )
    text = str(result["context"])
    assert result["context_bytes"] == len(text.encode("utf-8")) <= budget
    assert "\ufffd" not in text
    content = json.loads(text)
    manifest = json.loads(store.artifacts.read(str(result["manifest_sha"])))
    assert manifest["context"] == text
    assert manifest["context_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    selected = content["memory"]
    if selected:
        assert selected[0]["pinned"] is True
    assert result["selected_memory"] == len(selected)
    assert content["omissions"]["candidate_memory"] + len(selected) == 12
    assert manifest["memory"]["retrieval"]["omissions"]["result_limit"] == 4
    decisions = manifest["memory"]["records"]
    assert len(decisions) == 8
    included = [
        item["preview"] for item in decisions if item["disposition"] == "selected"
    ]
    assert included == selected
    assert manifest["memory"]["preview_byte_allowance"] == budget // 3


def test_inactive_memory_is_audited_but_never_injected(store: HarnessStore) -> None:
    thread = store.create_thread("Resume saved review")
    expired = store.create_memory(
        thread.id,
        "EXPIRED_NOTE_BODY",
        source="Operator",
        expires_at="2000-01-01T00:00:00Z",
    )
    forgotten = store.create_memory(thread.id, "FORGOTTEN_NOTE_BODY", source="Operator")
    store.forget_memory(thread.id, forgotten.id, expected_version=1, reason="Obsolete")
    result = assemble_context(store, thread.id, query="")
    assert "EXPIRED_NOTE_BODY" not in str(result["context"])
    assert "FORGOTTEN_NOTE_BODY" not in str(result["context"])
    assert result["selected_memory"] == 0
    manifest = json.loads(store.artifacts.read(str(result["manifest_sha"])))
    assert manifest["memory"]["retrieval"]["excluded"] == {"expired": 1, "forgotten": 1}
    assert store.get_memory(thread.id, expired.id).status == "active"
    assert len(store.memory_history(thread.id, forgotten.id)) == 2


@pytest.mark.parametrize("limit", [True, 0, 21])
def test_context_rejects_invalid_memory_window(store: HarnessStore, limit: int) -> None:
    thread = store.create_thread("Window validation")
    with pytest.raises(HarnessError, match="memory_limit"):
        assemble_context(store, thread.id, memory_limit=limit)
