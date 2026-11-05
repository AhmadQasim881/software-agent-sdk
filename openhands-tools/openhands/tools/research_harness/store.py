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


_SCHEMA_VERSION = 1
_MAX_RECORD_BYTES = 65_536
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
    if not 1 <= limit <= 200 or offset < 0:
        raise HarnessError("Pages require limit 1..200 and a nonnegative offset")


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
        self.artifacts = ArtifactStore(self.state_dir / "artifacts")

    @staticmethod
    def _check_version(connection: sqlite3.Connection) -> int:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, _SCHEMA_VERSION):
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

    def append_event(
        self,
        kind: str,
        *,
        thread_id: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        payload: dict[str, JsonValue] | None = None,
    ) -> EventRecord:
        if kind.startswith(("run.", "thread.", "evidence.")):
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
        if after_seq < 0:
            raise HarnessError("Event cursor must be nonnegative")
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
