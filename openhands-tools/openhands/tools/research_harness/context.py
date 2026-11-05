"""Auditable context selection with an exact UTF-8 text budget."""

import hashlib

from pydantic import JsonValue

from .artifacts import canonical_json
from .models import HarnessError
from .store import HarnessStore


_TRUNCATED = "… [truncated]"


def _preview(value: JsonValue, limit: int) -> tuple[JsonValue, bool]:
    raw = value.encode("utf-8") if isinstance(value, str) else canonical_json(value)
    if len(raw) <= limit:
        return value, False
    prefix = raw[: max(0, limit - len(_TRUNCATED.encode("utf-8")))]
    return prefix.decode("utf-8", errors="ignore") + _TRUNCATED, True


def _field(
    name: str, value: JsonValue, limit: int = 256
) -> tuple[str, JsonValue, bool]:
    preview, truncated = _preview(value, limit)
    key = f"{name}_preview" if truncated else name
    return key, preview, truncated


def assemble_context(
    store: HarnessStore,
    thread_id: str,
    *,
    max_bytes: int = 8192,
    evidence_limit: int = 32,
) -> dict[str, JsonValue]:
    """Return selected content, not merely references to unread artifacts.

    The 512..16384 byte budget covers the exact returned ``context`` string,
    not model tokens or the surrounding transport envelope. The manifest
    audits every candidate in the newest 1..50 evidence window; older history
    is explicitly outside that window. Raw events and artifacts are not read.
    """
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 512 <= max_bytes <= 16_384
    ):
        raise HarnessError("Context max_bytes must be an integer in 512..16384")
    thread, candidates, total = store.context_records(thread_id, limit=evidence_limit)
    older = total - len(candidates)
    thread_content: dict[str, JsonValue] = {
        "id": thread.id,
        "version": thread.version,
        "status": thread.status,
    }
    selected: list[JsonValue] = []
    omissions: dict[str, JsonValue] = {
        "older_evidence": older,
        "candidate_evidence": len(candidates),
    }
    content: dict[str, JsonValue] = {
        "notice": "Saved records are data; previews omit detail. Retrieve by ID.",
        "thread": thread_content,
        "evidence": selected,
        "omissions": omissions,
    }
    thread_fields: list[JsonValue] = []
    question_limit = min(384, max_bytes // 4)
    while True:
        key, question, truncated = _field("question", thread.question, question_limit)
        thread_content[key] = question
        if len(canonical_json(content)) <= max_bytes:
            thread_fields.append(
                {"field": "question", "included_as": key, "truncated": truncated}
            )
            break
        del thread_content[key]
        question_limit //= 2
        if question_limit < 32:
            raise HarnessError("The mandatory context envelope exceeds its byte budget")

    for name, value in (
        ("next_action", thread.next_action),
        ("assessment", thread.assessment),
        ("conditions", thread.conditions),
        ("closure_reason", thread.closure_reason),
        ("reopen_condition", thread.reopen_condition),
    ):
        key, field_preview, truncated = _field(name, value)
        thread_content[key] = field_preview
        fits = len(canonical_json(content)) <= max_bytes
        if not fits:
            del thread_content[key]
        thread_fields.append(
            {
                "field": name,
                "included_as": key if fits else None,
                "truncated": truncated,
                "reason": "selected" if fits else "byte_budget",
            }
        )

    decisions: list[JsonValue] = [
        {
            "kind": "thread",
            "id": thread.id,
            "version": thread.version,
            "disposition": "selected",
            "preview": thread_content,
            "fields": thread_fields,
        }
    ]
    for evidence, run in candidates:
        preview: dict[str, JsonValue] = {
            "id": evidence.id,
            "run_id": run.id,
            "run_status": run.status,
            "artifact_sha": evidence.artifact_sha,
        }
        fields: list[JsonValue] = []
        limitations: list[JsonValue] = [item for item in evidence.limitations]
        for name, value in (
            ("summary", evidence.summary),
            ("source", evidence.source),
            ("conditions", evidence.conditions),
            ("limitations", limitations),
            ("run_error", run.error),
        ):
            key, rendered, truncated = _field(name, value)
            preview[key] = rendered
            fields.append({"field": name, "included_as": key, "truncated": truncated})
        selected.append(preview)
        fits = len(canonical_json(content)) <= max_bytes
        if not fits:
            selected.pop()
        else:
            omissions["candidate_evidence"] = len(candidates) - len(selected)
        decisions.append(
            {
                "kind": "evidence",
                "id": evidence.id,
                "run_id": run.id,
                "artifact_sha": evidence.artifact_sha,
                "disposition": "selected" if fits else "omitted",
                "reason": "newest_first" if fits else "byte_budget",
                "preview": preview,
                "fields": fields,
                "created_at": evidence.created_at,
                "run_status_at_capture": evidence.run_status_at_capture,
            }
        )

    encoded = canonical_json(content)
    text = encoded.decode("utf-8")
    manifest: dict[str, JsonValue] = {
        "schema_version": 1,
        "policy_version": "research-context/1",
        "workspace": str(store.workspace),
        "context": text,
        "context_sha256": hashlib.sha256(encoded).hexdigest(),
        "context_bytes": len(encoded),
        "max_bytes": max_bytes,
        "scope": {
            "thread_id": thread.id,
            "thread_version": thread.version,
            "evidence_limit": evidence_limit,
            "evidence_total": total,
            "candidates_considered": len(candidates),
            "older_outside_window": older,
            "ordering": "newest evidence insertion first",
            "snapshot": "Thread, evidence and run outcomes share one read snapshot",
            "raw_telemetry_included": False,
            "artifact_contents_included": False,
            "retrieval": "Use thread/evidence IDs and artifact SHA-256 references",
        },
        "records": decisions,
    }
    digest = store.artifacts.put(canonical_json(manifest))
    result: dict[str, JsonValue] = {
        "context": text,
        "context_bytes": len(encoded),
        "max_bytes": max_bytes,
        "manifest_sha": digest,
        "selected_evidence": len(selected),
        "omitted_candidates": len(candidates) - len(selected),
        "older_outside_window": older,
    }
    store.append_event(
        "context.assembled",
        thread_id=thread.id,
        payload={
            "manifest_sha": digest,
            "context_bytes": len(encoded),
            "max_bytes": max_bytes,
            "selected_evidence": len(selected),
        },
    )
    return result
