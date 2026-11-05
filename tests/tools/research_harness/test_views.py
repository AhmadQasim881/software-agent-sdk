import base64
import json
from pathlib import Path

import pytest

from openhands.tools.research_harness.models import HarnessError
from openhands.tools.research_harness.store import HarnessStore
from openhands.tools.research_harness.views import artifact_slice, export_events


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessStore(tmp_path / "state", workspace)


def test_artifact_slices_reassemble_exact_bytes_across_unicode_boundaries(
    store: HarnessStore,
) -> None:
    original = "سجل الملاحظات\n".encode()
    digest = store.artifacts.put(original)
    recovered = bytearray()
    offset = 0
    while offset < len(original):
        result = artifact_slice(store, digest, offset=offset, limit=3)
        content = result["content"]
        assert isinstance(content, str)
        recovered.extend(
            content.encode()
            if result["encoding"] == "utf-8"
            else base64.b64decode(content)
        )
        offset = int(str(result["next_offset"]))
        assert result["eof"] is (offset == len(original))
    assert bytes(recovered) == original
    assert artifact_slice(store, digest, offset=offset)["bytes"] == 0


def test_artifact_views_enforce_bounds_and_digest_integrity(
    store: HarnessStore,
) -> None:
    digest = store.artifacts.put(b"data")
    for offset, limit in [(-1, 10), (5, 10), (0, 4097), (0, 0)]:
        with pytest.raises(HarnessError):
            artifact_slice(store, digest, offset=offset, limit=limit)
    with pytest.raises(ValueError):
        artifact_slice(store, "../../outside")
    (store.artifacts.root / digest).write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        artifact_slice(store, digest)


def test_export_resumes_exact_jsonl_without_missing_or_repeating_events(
    store: HarnessStore,
) -> None:
    thread = store.create_thread("Track independent operations")
    for index in range(5):
        store.append_event(
            "observation.note", thread_id=thread.id, payload={"n": index}
        )
    expected = [event.model_dump(mode="json") for event in store.list_events()]
    exported = []
    cursor = 0
    for _ in range(4):
        result = export_events(store, after_seq=cursor, limit=2)
        digest = result["artifact_sha"]
        assert isinstance(digest, str)
        raw = store.artifacts.read(digest)
        exported.extend(json.loads(line) for line in raw.splitlines())
        cursor = int(str(result["next_after_seq"]))
        if result["has_more"] is False:
            break
    assert exported == expected
    assert [event.model_dump(mode="json") for event in store.list_events()] == expected
    assert export_events(store, after_seq=cursor)["records"] == 0


def test_export_byte_cap_preserves_unexported_cursor(store: HarnessStore) -> None:
    thread = store.create_thread("Bound a large ledger page")
    for _ in range(6):
        store.append_event(
            "observation.note", thread_id=thread.id, payload={"text": "x" * 60_000}
        )
    first = export_events(store, thread_id=thread.id)
    digest = first["artifact_sha"]
    assert isinstance(digest, str)
    assert len(store.artifacts.read(digest)) <= 262_144
    assert first["has_more"] is True
    next_seq = int(str(first["next_after_seq"]))
    second = export_events(store, thread_id=thread.id, after_seq=next_seq)
    assert int(str(first["records"])) + int(str(second["records"])) == 7
    assert second["has_more"] is False


def test_export_filter_and_invalid_requests(store: HarnessStore) -> None:
    first = store.create_thread("First thread")
    second = store.create_thread("Second thread")
    result = export_events(store, thread_id=second.id)
    assert result["records"] == 1
    digest = result["artifact_sha"]
    assert isinstance(digest, str)
    assert json.loads(store.artifacts.read(digest))["thread_id"] == second.id
    for limit in [0, 101]:
        with pytest.raises(HarnessError):
            export_events(store, limit=limit)
    with pytest.raises(HarnessError):
        export_events(store, thread_id=first.id, after_seq=-1)
    with pytest.raises(HarnessError):
        export_events(store, thread_id="unknown")
