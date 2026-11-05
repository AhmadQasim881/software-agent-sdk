"""Bounded views of exact artifacts and the authoritative event ledger."""

import base64

from pydantic import JsonValue

from .artifacts import canonical_json
from .models import HarnessError
from .store import HarnessStore


def artifact_slice(
    store: HarnessStore, digest: str, *, offset: int = 0, limit: int = 2048
) -> dict[str, JsonValue]:
    """Read a verified artifact by digest, never by an arbitrary filesystem path."""
    if offset < 0 or not 1 <= limit <= 4096:
        raise HarnessError("Artifact slices require offset >=0 and limit 1..4096")
    data = store.artifacts.read(digest)
    if offset > len(data):
        raise HarnessError("Artifact offset exceeds its length")
    chunk = data[offset : offset + limit]
    try:
        content = chunk.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(chunk).decode("ascii")
        encoding = "base64"
    next_offset = offset + len(chunk)
    return {
        "ok": True,
        "artifact_sha": digest,
        "offset": offset,
        "bytes": len(chunk),
        "total_bytes": len(data),
        "next_offset": next_offset,
        "eof": next_offset == len(data),
        "encoding": encoding,
        "content": content,
    }


def export_events(
    store: HarnessStore,
    *,
    after_seq: int = 0,
    limit: int = 100,
    thread_id: str | None = None,
) -> dict[str, JsonValue]:
    """Materialize a resumable JSONL page from SQLite, without a second ledger."""
    if not 1 <= limit <= 100:
        raise HarnessError("Event export limit must be 1..100")
    if thread_id is not None:
        store.get_thread(thread_id)
    records = store.list_events(
        after_seq=after_seq, limit=limit + 1, thread_id=thread_id
    )
    lines: list[bytes] = []
    size = 0
    next_seq = after_seq
    for record in records[:limit]:
        line = canonical_json(record.model_dump(mode="json")) + b"\n"
        if size + len(line) > 262_144:
            break
        lines.append(line)
        size += len(line)
        next_seq = record.seq
    digest = store.artifacts.put(b"".join(lines))
    return {
        "ok": True,
        "format": "jsonl",
        "artifact_sha": digest,
        "bytes": size,
        "records": len(lines),
        "after_seq": after_seq,
        "next_after_seq": next_seq,
        "has_more": len(records) > len(lines),
        "scope": "Events visible during this read; later events may exist",
        "source": "sqlite:event-ledger",
    }
