"""Transactional local state with explicit ownership and append-only evidence."""

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from .agentmemory import (
    MEMORY_RESULT_BYTES,
    MEMORY_SEARCH_POLICY,
    MemoryKind,
    MemoryRecord,
    default_query_terms,
    normalized_tags,
    rank_memory,
    search_text,
    tag_token,
)
from .artifacts import ArtifactStore, canonical_json
from .models import (
    ConflictError,
    EventRecord,
    EvidenceRecord,
    HarnessError,
    NotFoundError,
    Record,
    RunRecord,
    TerminalStatus,
    ThreadRecord,
    ThreadStatus,
)


_SCHEMA_VERSION = 2
_MAX_RECORD_BYTES = 65_536
_MAX_RUN_FIELD_BYTES = 16_384
_SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE threads (id TEXT PRIMARY KEY, version INTEGER NOT NULL, "
    "status TEXT NOT NULL, record TEXT NOT NULL)",
    "CREATE TABLE thread_history (thread_id TEXT REFERENCES threads(id), "
    "version INTEGER NOT NULL, record TEXT NOT NULL, "
    "PRIMARY KEY (thread_id, version))",
    "CREATE TABLE runs (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL "
    "REFERENCES threads(id), status TEXT NOT NULL, record TEXT NOT NULL)",
    "CREATE TABLE evidence (id TEXT PRIMARY KEY, run_id TEXT NOT NULL "
    "REFERENCES runs(id), thread_id TEXT NOT NULL REFERENCES threads(id), "
    "record TEXT NOT NULL)",
    "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "kind TEXT NOT NULL, thread_id TEXT REFERENCES threads(id), "
    "run_id TEXT REFERENCES runs(id), session_id TEXT, "
    "created_at TEXT NOT NULL, payload TEXT NOT NULL)",
    "CREATE INDEX runs_thread ON runs(thread_id)",
    "CREATE INDEX evidence_thread ON evidence(thread_id)",
    "CREATE INDEX events_thread ON events(thread_id, seq)",
    "CREATE INDEX events_run ON events(run_id, seq)",
)
_MEMORY_SCHEMA = (
    "CREATE TABLE memory_notes (id TEXT PRIMARY KEY, "
    "thread_id TEXT NOT NULL REFERENCES threads(id), version INTEGER NOT NULL, "
    "kind TEXT NOT NULL, status TEXT NOT NULL, pinned INTEGER NOT NULL, "
    "expires_at TEXT, updated_at TEXT NOT NULL, search_text TEXT NOT NULL, "
    "tag_tokens TEXT NOT NULL, record TEXT NOT NULL)",
    "CREATE TABLE memory_note_history (memory_id TEXT NOT NULL "
    "REFERENCES memory_notes(id), version INTEGER NOT NULL, record TEXT NOT NULL, "
    "PRIMARY KEY (memory_id, version))",
    "CREATE INDEX memory_notes_thread ON memory_notes(thread_id,status,kind)",
    "CREATE INDEX memory_notes_recent ON memory_notes"
    "(thread_id,pinned DESC,updated_at DESC,id)",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _encode(value: JsonValue) -> str:
    encoded = canonical_json(value)
    if len(encoded) > _MAX_RECORD_BYTES:
        raise HarnessError(f"Record exceeds {_MAX_RECORD_BYTES} UTF-8 bytes")
    return encoded.decode("utf-8")


def _record_json(record: Record) -> str:
    return _encode(record.model_dump(mode="json"))


def _page(limit: int, offset: int = 0) -> None:
    if not 1 <= limit <= 200 or not 0 <= offset <= 9_223_372_036_854_775_807:
        raise HarnessError("Pages require limit 1..200 and a signed-64-bit offset")


def _load[T: Record](
    connection: sqlite3.Connection,
    table: Literal["threads", "runs", "evidence"],
    identifier: str,
    model: type[T],
) -> T:
    row = connection.execute(
        f"SELECT record FROM {table} WHERE id = ?", (identifier,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown {table} record: {identifier}")
    return model.model_validate_json(row[0])


def _memory_load(
    connection: sqlite3.Connection, thread_id: str, memory_id: str
) -> MemoryRecord:
    row = connection.execute(
        "SELECT record FROM memory_notes WHERE thread_id=? AND id=?",
        (thread_id, memory_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown memory record in this thread: {memory_id}")
    return MemoryRecord.model_validate_json(row[0])


def _memory_version(current: MemoryRecord, expected_version: int) -> None:
    if type(expected_version) is not int or expected_version < 1:
        raise HarnessError("Memory writes require a positive expected_version")
    if current.version != expected_version:
        raise ConflictError("Memory version changed; reload before updating")
    if current.status == "forgotten":
        raise ConflictError("Forgotten memory cannot be changed or restored")


def _memory_references(connection: sqlite3.Connection, record: MemoryRecord) -> None:
    _load(connection, "threads", record.thread_id, ThreadRecord)
    for evidence_id in record.evidence_ids:
        evidence = _load(connection, "evidence", evidence_id, EvidenceRecord)
        if evidence.thread_id != record.thread_id:
            raise ConflictError("Memory evidence must belong to the same thread")


def _memory_save(connection: sqlite3.Connection, record: MemoryRecord) -> None:
    encoded = _record_json(record)
    values = (
        record.version,
        record.kind,
        record.status,
        int(record.pinned),
        record.expires_at,
        record.updated_at,
        search_text(record),
        "".join(tag_token(tag) for tag in record.tags),
        encoded,
    )
    if record.version == 1:
        connection.execute(
            "INSERT INTO memory_notes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (record.id, record.thread_id, *values),
        )
    else:
        connection.execute(
            "UPDATE memory_notes SET version=?,kind=?,status=?,pinned=?,"
            "expires_at=?,updated_at=?,search_text=?,tag_tokens=?,record=? "
            "WHERE id=? AND thread_id=?",
            (*values, record.id, record.thread_id),
        )
    connection.execute(
        "INSERT INTO memory_note_history VALUES (?,?,?)",
        (record.id, record.version, encoded),
    )


def _memory_search_arguments(
    query: str,
    kind: MemoryKind | None,
    tags: list[str] | None,
    limit: int,
    candidate_limit: int,
) -> list[str]:
    if not isinstance(query, str) or len(query) > 512:
        raise HarnessError("Memory queries require at most 512 characters")
    if kind is not None and kind not in ("note", "decision", "constraint", "lesson"):
        raise HarnessError("Unknown memory kind")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HarnessError("Memory search limit must be 1..100")
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 500:
        raise HarnessError("Memory candidate_limit must be 1..500")
    return normalized_tags(tags if tags is not None else [])


def _memory_search(
    connection: sqlite3.Connection,
    thread_id: str,
    *,
    query: str,
    kind: MemoryKind | None,
    tags: list[str],
    limit: int,
    candidate_limit: int,
    include_pinned_constraints: bool = False,
    query_terms: list[str] | None = None,
) -> dict[str, JsonValue]:
    as_of = _now()
    normalized_query = query.casefold().strip()
    conditions = ["thread_id=?"]
    parameters: list[str | int] = [thread_id]
    if kind is not None:
        conditions.append("kind=?")
        parameters.append(kind)
    for tag in tags:
        conditions.append("instr(tag_tokens,?)>0")
        parameters.append(tag_token(tag))
    scoped = " AND ".join(conditions)
    counts = connection.execute(
        "SELECT COUNT(*),COALESCE(SUM(status='forgotten'),0),"
        "COALESCE(SUM(status='active' AND expires_at IS NOT NULL "
        f"AND expires_at<=?),0) FROM memory_notes WHERE {scoped}",
        [as_of, *parameters],
    ).fetchone()
    total, forgotten, expired = (int(value) for value in counts)
    conditions.extend(["status='active'", "(expires_at IS NULL OR expires_at>?)"])
    parameters.append(as_of)
    eligible = " AND ".join(conditions)
    match_parameters = [normalized_query] if query_terms is None else query_terms
    match_condition = (
        "(" + " OR ".join("instr(search_text,?)>0" for _ in match_parameters) + ")"
        if match_parameters
        else "0"
    )
    matching = int(
        connection.execute(
            f"SELECT COUNT(*) FROM memory_notes WHERE {eligible} AND {match_condition}",
            [*parameters, *match_parameters],
        ).fetchone()[0]
    )
    if include_pinned_constraints:
        match_condition = f"({match_condition} OR (pinned=1 AND kind='constraint'))"
    selected = f"{eligible} AND {match_condition}"
    parameters.extend(match_parameters)
    candidate_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM memory_notes WHERE {selected}", parameters
        ).fetchone()[0]
    )
    rows = connection.execute(
        f"SELECT record FROM memory_notes WHERE {selected} "
        "ORDER BY pinned DESC,updated_at DESC,id LIMIT ?",
        [*parameters, candidate_limit],
    ).fetchall()
    records = [MemoryRecord.model_validate_json(row[0]) for row in rows]
    ranked = rank_memory(records, query, query_terms=query_terms)
    returned: list[JsonValue] = []
    omissions: dict[str, JsonValue] = {
        "candidate_limit": max(0, candidate_count - len(records)),
        "result_limit": max(0, len(records) - limit),
        "byte_budget": 0,
    }
    result: dict[str, JsonValue] = {
        "ok": True,
        "query": query,
        "records": returned,
        "policy": MEMORY_SEARCH_POLICY,
        "as_of": as_of,
        "scope": {
            "thread_id": thread_id,
            "kind": kind,
            "tags": list(tags),
            "tag_match": "all exact case-folded tags",
            "query_match": (
                "literal case-folded substring of text or tags"
                if query_terms is None
                else "any literal case-folded term substring of text or tags"
            ),
            "query_source": (
                "explicit" if query_terms is None else "thread_question_and_next_action"
            ),
            "query_terms": None if query_terms is None else list(query_terms),
            "term_policy": (
                None
                if query_terms is None
                else "first 32 unique words >=3 characters, common words excluded"
            ),
            "snapshot": "one SQLite read transaction; no access-time writes",
            "ranking_scope": "bounded candidates, pinned first, then lexical RRF",
            "candidate_order": "pinned DESC, updated_at DESC, id ASC",
            "pinned_constraint_supplement": include_pinned_constraints,
        },
        "total_notes": total,
        "eligible_notes": total - forgotten - expired,
        "matching_notes": matching,
        "excluded": {"forgotten": forgotten, "expired": expired},
        "candidate_notes": candidate_count,
        "candidates_scanned": len(records),
        "candidate_limit": candidate_limit,
        "candidates_truncated": candidate_count > len(records),
        "pinned_constraints_supplemented": candidate_count - matching,
        "limit": limit,
        "byte_limit": MEMORY_RESULT_BYTES,
        "omissions": omissions,
    }
    page = ranked[:limit]
    for index, match in enumerate(page):
        returned.append(match)
        # Reserve digits for omission counters before measuring the full envelope.
        omissions["byte_budget"] = len(page) - index - 1
        if len(canonical_json(result)) > MEMORY_RESULT_BYTES:
            returned.pop()
            omissions["byte_budget"] = len(page) - index
            break
    return result


class HarnessStore:
    """One workspace per store; records <=64 KiB and pages <=200 records.

    Connections are owned by each operation and closed on every exit path.
    Directory separation prevents accidental overlap, not hostile host access.
    """

    def __init__(self, state_dir: Path, workspace: Path) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.state_dir = state_dir.resolve()
        if not self.workspace.is_dir():
            raise HarnessError("Workspace must be an existing directory")
        if self.state_dir.is_relative_to(self.workspace) or (
            self.workspace.is_relative_to(self.state_dir)
        ):
            raise HarnessError("State and workspace directories must not overlap")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database_path = self.state_dir / "state.sqlite3"
        if self.database_path.is_symlink():
            raise HarnessError("State database must not be a symbolic link")
        with self._connection() as connection:
            self._check_version(connection)
            connection.execute("PRAGMA journal_mode=WAL")
        with self._connection(write=True) as connection:
            version = self._check_version(connection)
            if version == 0:
                existing = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchone()
                if existing:
                    raise HarnessError("Unversioned nonempty state database")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=1")
                connection.execute(
                    "INSERT INTO metadata VALUES ('workspace', ?)",
                    (str(self.workspace),),
                )
            owner = connection.execute(
                "SELECT value FROM metadata WHERE key='workspace'"
            ).fetchone()
            if owner is None or owner[0] != str(self.workspace):
                raise ConflictError("State directory belongs to another workspace")
            if version in (0, 1):
                for statement in _MEMORY_SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=2")
        self.artifacts = ArtifactStore(self.state_dir / "artifacts")

    @staticmethod
    def _check_version(connection: sqlite3.Connection) -> int:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, 1, _SCHEMA_VERSION):
            raise HarnessError(f"Unsupported state schema version: {version}")
        return version

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _event(
        self,
        connection: sqlite3.Connection,
        kind: str,
        *,
        thread_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        payload: dict[str, JsonValue] | None = None,
    ) -> EventRecord:
        event = EventRecord(
            seq=1,
            kind=kind,
            thread_id=thread_id,
            run_id=run_id,
            session_id=session_id,
            created_at=_now(),
            payload=payload or {},
        )
        _record_json(event)
        cursor = connection.execute(
            "INSERT INTO events(kind,thread_id,run_id,session_id,created_at,payload) "
            "VALUES (?,?,?,?,?,?)",
            (
                kind,
                thread_id,
                run_id,
                session_id,
                event.created_at,
                _encode(event.payload),
            ),
        )
        assert cursor.lastrowid is not None
        return event.model_copy(update={"seq": cursor.lastrowid})

    def create_thread(
        self,
        question: str,
        *,
        conditions: dict[str, JsonValue] | None = None,
        next_action: str = "",
    ) -> ThreadRecord:
        now = _now()
        record = ThreadRecord(
            id=f"thread_{uuid.uuid4().hex}",
            question=question,
            conditions=conditions or {},
            next_action=next_action,
            version=1,
            created_at=now,
            updated_at=now,
        )
        data = _record_json(record)
        with self._connection(write=True) as connection:
            connection.execute(
                "INSERT INTO threads VALUES (?,?,?,?)",
                (record.id, record.version, record.status, data),
            )
            connection.execute(
                "INSERT INTO thread_history VALUES (?,?,?)", (record.id, 1, data)
            )
            self._event(
                connection,
                "thread.created",
                thread_id=record.id,
                payload={"version": 1},
            )
        return record

    def update_thread(
        self,
        thread_id: str,
        *,
        question: str | None = None,
        conditions: dict[str, JsonValue] | None = None,
        assessment: str | None = None,
        next_action: str | None = None,
        status: ThreadStatus | None = None,
        reopen_condition: str | None = None,
        closure_reason: str | None = None,
        expected_version: int | None = None,
    ) -> ThreadRecord:
        updates = {
            "question": question,
            "conditions": conditions,
            "assessment": assessment,
            "next_action": next_action,
            "status": status,
            "reopen_condition": reopen_condition,
            "closure_reason": closure_reason,
        }
        with self._connection(write=True) as connection:
            current = _load(connection, "threads", thread_id, ThreadRecord)
            if expected_version is not None and current.version != expected_version:
                raise ConflictError("Thread version changed; reload before updating")
            data = current.model_dump()
            data.update(
                {key: value for key, value in updates.items() if value is not None}
            )
            data.update(version=current.version + 1, updated_at=_now())
            record = ThreadRecord.model_validate(data)
            encoded = _record_json(record)
            connection.execute(
                "UPDATE threads SET version=?,status=?,record=? WHERE id=?",
                (record.version, record.status, encoded, thread_id),
            )
            connection.execute(
                "INSERT INTO thread_history VALUES (?,?,?)",
                (thread_id, record.version, encoded),
            )
            self._event(
                connection,
                "thread.updated",
                thread_id=thread_id,
                payload={"version": record.version, "status": record.status},
            )
        return record

    def get_thread(self, thread_id: str) -> ThreadRecord:
        with self._connection() as connection:
            return _load(connection, "threads", thread_id, ThreadRecord)

    def list_threads(self, *, limit: int = 50, offset: int = 0) -> list[ThreadRecord]:
        _page(limit, offset)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT record FROM threads ORDER BY rowid LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [ThreadRecord.model_validate_json(row[0]) for row in rows]

    def thread_history(
        self,
        thread_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ThreadRecord]:
        _page(limit, offset)
        with self._connection() as connection:
            _load(connection, "threads", thread_id, ThreadRecord)
            rows = connection.execute(
                "SELECT record FROM thread_history WHERE thread_id=? "
                "ORDER BY version LIMIT ? OFFSET ?",
                (thread_id, limit, offset),
            ).fetchall()
        return [ThreadRecord.model_validate_json(row[0]) for row in rows]

    def start_run(
        self,
        thread_id: str,
        operation: str,
        *,
        inputs: dict[str, JsonValue] | None = None,
        session_id: str | None = None,
    ) -> RunRecord:
        if len(canonical_json(inputs or {})) > _MAX_RUN_FIELD_BYTES:
            raise HarnessError("Run inputs exceed 16384 UTF-8 bytes")
        record = RunRecord(
            id=f"run_{uuid.uuid4().hex}",
            thread_id=thread_id,
            operation=operation,
            inputs=inputs or {},
            session_id=session_id,
            created_at=_now(),
        )
        data = _record_json(record)
        with self._connection(write=True) as connection:
            thread = _load(connection, "threads", thread_id, ThreadRecord)
            if thread.status != "active":
                raise ConflictError("Reopen a closed thread before starting work")
            connection.execute(
                "INSERT INTO runs VALUES (?,?,?,?)",
                (record.id, thread_id, record.status, data),
            )
            self._event(
                connection,
                "run.started",
                thread_id=thread_id,
                run_id=record.id,
                session_id=session_id,
                payload={"operation": operation},
            )
        return record

    def get_run(self, run_id: str) -> RunRecord:
        with self._connection() as connection:
            return _load(connection, "runs", run_id, RunRecord)

    def list_runs(
        self,
        thread_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        _page(limit, offset)
        with self._connection() as connection:
            _load(connection, "threads", thread_id, ThreadRecord)
            rows = connection.execute(
                "SELECT record FROM runs WHERE thread_id=? "
                "ORDER BY rowid LIMIT ? OFFSET ?",
                (thread_id, limit, offset),
            ).fetchall()
        return [RunRecord.model_validate_json(row[0]) for row in rows]

    def finish_run(
        self,
        run_id: str,
        status: TerminalStatus,
        *,
        result: dict[str, JsonValue] | None = None,
        error: str | None = None,
    ) -> RunRecord:
        if len(canonical_json(result or {})) > _MAX_RUN_FIELD_BYTES:
            raise HarnessError("Run result exceeds 16384 UTF-8 bytes")
        with self._connection(write=True) as connection:
            current = _load(connection, "runs", run_id, RunRecord)
            if current.status != "running":
                raise ConflictError("Run already has a final outcome")
            data = current.model_dump()
            data.update(
                status=status, result=result or {}, error=error, finished_at=_now()
            )
            record = RunRecord.model_validate(data)
            connection.execute(
                "UPDATE runs SET status=?,record=? WHERE id=?",
                (record.status, _record_json(record), run_id),
            )
            self._event(
                connection,
                f"run.{status}",
                thread_id=record.thread_id,
                run_id=run_id,
                session_id=record.session_id,
                payload={"status": status},
            )
        return record

    def recover(self, run_id: str, reason: str) -> RunRecord:
        return self.finish_run(run_id, "interrupted", error=reason)

    def record_evidence(
        self,
        run_id: str,
        *,
        source: str,
        summary: str,
        artifact_sha: str,
        conditions: dict[str, JsonValue] | None = None,
        limitations: list[str] | None = None,
        thread_id: str | None = None,
    ) -> EvidenceRecord:
        self.artifacts.read(artifact_sha)
        with self._connection(write=True) as connection:
            run = _load(connection, "runs", run_id, RunRecord)
            if thread_id is not None and run.thread_id != thread_id:
                raise ConflictError("Evidence thread does not match its run")
            record = EvidenceRecord(
                id=f"evidence_{uuid.uuid4().hex}",
                run_id=run_id,
                thread_id=run.thread_id,
                run_status_at_capture=run.status,
                source=source,
                summary=summary,
                artifact_sha=artifact_sha,
                conditions=conditions or {},
                limitations=limitations or [],
                created_at=_now(),
            )
            connection.execute(
                "INSERT INTO evidence VALUES (?,?,?,?)",
                (record.id, run_id, run.thread_id, _record_json(record)),
            )
            self._event(
                connection,
                "evidence.recorded",
                thread_id=run.thread_id,
                run_id=run_id,
                session_id=run.session_id,
                payload={"evidence_id": record.id},
            )
        return record

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        with self._connection() as connection:
            return _load(connection, "evidence", evidence_id, EvidenceRecord)

    def list_evidence(
        self,
        thread_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EvidenceRecord]:
        _page(limit, offset)
        with self._connection() as connection:
            _load(connection, "threads", thread_id, ThreadRecord)
            rows = connection.execute(
                "SELECT record FROM evidence WHERE thread_id=? "
                "ORDER BY rowid LIMIT ? OFFSET ?",
                (thread_id, limit, offset),
            ).fetchall()
        return [EvidenceRecord.model_validate_json(row[0]) for row in rows]

    def create_memory(
        self,
        thread_id: str,
        text: str,
        *,
        source: str,
        kind: MemoryKind = "note",
        tags: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        importance: int = 5,
        pinned: bool = False,
        expires_at: str | None = None,
    ) -> MemoryRecord:
        now = _now()
        record = MemoryRecord(
            id=f"memory_{uuid.uuid4().hex}",
            thread_id=thread_id,
            text=text,
            source=source,
            kind=kind,
            tags=tags if tags is not None else [],
            evidence_ids=evidence_ids if evidence_ids is not None else [],
            importance=importance,
            pinned=pinned,
            expires_at=expires_at,
            version=1,
            created_at=now,
            updated_at=now,
        )
        with self._connection(write=True) as connection:
            _memory_references(connection, record)
            _memory_save(connection, record)
            self._event(
                connection,
                "memory.created",
                thread_id=thread_id,
                payload={"memory_id": record.id, "version": 1},
            )
        return record

    def get_memory(self, thread_id: str, memory_id: str) -> MemoryRecord:
        """Read a note, including an expired note or retained forget tombstone."""
        with self._connection() as connection:
            return _memory_load(connection, thread_id, memory_id)

    def update_memory(
        self,
        thread_id: str,
        memory_id: str,
        *,
        expected_version: int,
        text: str | None = None,
        kind: MemoryKind | None = None,
        source: str | None = None,
        tags: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        importance: int | None = None,
        pinned: bool | None = None,
        expires_at: str | None = None,
        clear_expiry: bool = False,
    ) -> MemoryRecord:
        if type(clear_expiry) is not bool:
            raise HarnessError("clear_expiry must be a boolean")
        if clear_expiry and expires_at is not None:
            raise HarnessError("Set expires_at or clear_expiry, not both")
        updates = {
            "text": text,
            "kind": kind,
            "source": source,
            "tags": tags,
            "evidence_ids": evidence_ids,
            "importance": importance,
            "pinned": pinned,
            "expires_at": expires_at,
        }
        with self._connection(write=True) as connection:
            current = _memory_load(connection, thread_id, memory_id)
            _memory_version(current, expected_version)
            data = current.model_dump()
            data.update(
                {key: value for key, value in updates.items() if value is not None}
            )
            if clear_expiry:
                data["expires_at"] = None
            data.update(version=current.version + 1, updated_at=_now())
            record = MemoryRecord.model_validate(data)
            _memory_references(connection, record)
            _memory_save(connection, record)
            self._event(
                connection,
                "memory.updated",
                thread_id=thread_id,
                payload={"memory_id": memory_id, "version": record.version},
            )
        return record

    def forget_memory(
        self,
        thread_id: str,
        memory_id: str,
        *,
        expected_version: int,
        reason: str,
    ) -> MemoryRecord:
        """Tombstone a note; retain its contents and history for inspection."""
        with self._connection(write=True) as connection:
            current = _memory_load(connection, thread_id, memory_id)
            _memory_version(current, expected_version)
            record = MemoryRecord.model_validate(
                {
                    **current.model_dump(),
                    "status": "forgotten",
                    "forget_reason": reason,
                    "version": current.version + 1,
                    "updated_at": _now(),
                }
            )
            _memory_save(connection, record)
            self._event(
                connection,
                "memory.forgotten",
                thread_id=thread_id,
                payload={"memory_id": memory_id, "version": record.version},
            )
        return record

    def memory_history(
        self,
        thread_id: str,
        memory_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        if type(limit) is not int or type(offset) is not int:
            raise HarnessError("Memory history requires integer page bounds")
        _page(limit, offset)
        with self._connection() as connection:
            connection.execute("BEGIN")
            _memory_load(connection, thread_id, memory_id)
            rows = connection.execute(
                "SELECT record FROM memory_note_history WHERE memory_id=? "
                "ORDER BY version LIMIT ? OFFSET ?",
                (memory_id, limit, offset),
            ).fetchall()
        return [MemoryRecord.model_validate_json(row[0]) for row in rows]

    def search_memory(
        self,
        thread_id: str,
        *,
        query: str = "",
        kind: MemoryKind | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
        candidate_limit: int = 200,
    ) -> dict[str, JsonValue]:
        """Filter literal matches before loading a bounded candidate window."""
        normalized = _memory_search_arguments(query, kind, tags, limit, candidate_limit)
        with self._connection() as connection:
            connection.execute("BEGIN")
            _load(connection, "threads", thread_id, ThreadRecord)
            return _memory_search(
                connection,
                thread_id,
                query=query,
                kind=kind,
                tags=normalized,
                limit=limit,
                candidate_limit=candidate_limit,
            )

    def context_records(
        self,
        thread_id: str,
        *,
        limit: int = 32,
    ) -> tuple[ThreadRecord, list[tuple[EvidenceRecord, RunRecord]], int]:
        """Read the current thread and newest evidence/outcomes in one snapshot."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            return self._context_records(connection, thread_id, limit)

    @staticmethod
    def _context_records(
        connection: sqlite3.Connection,
        thread_id: str,
        limit: int,
    ) -> tuple[ThreadRecord, list[tuple[EvidenceRecord, RunRecord]], int]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise HarnessError("Context records require limit 1..50")
        thread = _load(connection, "threads", thread_id, ThreadRecord)
        total = connection.execute(
            "SELECT COUNT(*) FROM evidence WHERE thread_id=?", (thread_id,)
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT e.record AS evidence_record, r.record AS run_record "
            "FROM evidence AS e JOIN runs AS r ON e.run_id=r.id "
            "WHERE e.thread_id=? ORDER BY e.rowid DESC LIMIT ?",
            (thread_id, limit),
        ).fetchall()
        records = [
            (
                EvidenceRecord.model_validate_json(row["evidence_record"]),
                RunRecord.model_validate_json(row["run_record"]),
            )
            for row in rows
        ]
        return thread, records, int(total)

    def context_memory_records(
        self,
        thread_id: str,
        *,
        evidence_limit: int = 32,
        query: str | None = None,
        memory_limit: int = 8,
    ) -> tuple[
        ThreadRecord, list[tuple[EvidenceRecord, RunRecord]], int, dict[str, JsonValue]
    ]:
        """Read thread, evidence, outcomes and authored memory in one snapshot.

        Active pinned constraints supplement literal matches for context, even
        when their wording differs. The result identifies these supplements.
        Other unmatched notes never enter the candidate window.
        """
        _memory_search_arguments(
            query if query is not None else "", None, None, memory_limit, 200
        )
        with self._connection() as connection:
            connection.execute("BEGIN")
            thread, evidence, total = self._context_records(
                connection, thread_id, evidence_limit
            )
            effective_query = (
                (thread.question + " " + thread.next_action).strip()[:512]
                if query is None
                else query
            )
            memory = _memory_search(
                connection,
                thread_id,
                query=effective_query,
                kind=None,
                tags=[],
                limit=memory_limit,
                candidate_limit=200,
                include_pinned_constraints=True,
                query_terms=(
                    default_query_terms(effective_query) if query is None else None
                ),
            )
        return thread, evidence, total, memory

    def append_event(
        self,
        kind: str,
        *,
        thread_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        payload: dict[str, JsonValue] | None = None,
    ) -> EventRecord:
        if kind.startswith(("run.", "thread.", "evidence.", "memory.")):
            raise HarnessError("Lifecycle events are written by their owning operation")
        with self._connection(write=True) as connection:
            if run_id is not None:
                run = _load(connection, "runs", run_id, RunRecord)
                if thread_id is not None and run.thread_id != thread_id:
                    raise ConflictError("Event thread does not match its run")
                thread_id = run.thread_id
                session_id = session_id if session_id is not None else run.session_id
            if thread_id is not None:
                _load(connection, "threads", thread_id, ThreadRecord)
            return self._event(
                connection,
                kind,
                thread_id=thread_id,
                run_id=run_id,
                session_id=session_id,
                payload=payload,
            )

    def list_events(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> list[EventRecord]:
        if not 0 <= after_seq <= 9_223_372_036_854_775_807:
            raise HarnessError("Event cursor must be a nonnegative signed-64-bit value")
        return self._events(after_seq, limit, thread_id, run_id, tail=False)

    def tail_events(
        self,
        *,
        limit: int = 100,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> list[EventRecord]:
        return self._events(0, limit, thread_id, run_id, tail=True)

    def _events(
        self,
        after_seq: int,
        limit: int,
        thread_id: str | None,
        run_id: str | None,
        *,
        tail: bool,
    ) -> list[EventRecord]:
        _page(limit)
        direction = "DESC" if tail else "ASC"
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE seq>? "
                "AND (? IS NULL OR thread_id=?) AND (? IS NULL OR run_id=?) "
                f"ORDER BY seq {direction} LIMIT ?",
                (after_seq, thread_id, thread_id, run_id, run_id, limit),
            ).fetchall()
        records = [
            EventRecord(
                seq=row["seq"],
                kind=row["kind"],
                thread_id=row["thread_id"],
                run_id=row["run_id"],
                session_id=row["session_id"],
                created_at=row["created_at"],
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]
        return list(reversed(records)) if tail else records
