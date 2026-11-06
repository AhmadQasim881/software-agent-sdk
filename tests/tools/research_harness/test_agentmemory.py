"""Scoped memory migration, atomic writes, bounded recall and snapshot behavior."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import JsonValue

from openhands.tools.research_harness import (
    ConflictError,
    HarnessError,
    HarnessStore,
    NotFoundError,
    store as store_module,
)
from openhands.tools.research_harness.agentmemory import (
    MEMORY_RESULT_BYTES,
    MemoryRecord,
    hashing_features,
    reciprocal_rank_fusion,
)
from openhands.tools.research_harness.artifacts import ArtifactStore, canonical_json
from openhands.tools.research_harness.models import (
    EvidenceRecord,
    RunRecord,
    ThreadRecord,
)


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessStore(tmp_path / "state", workspace)


def recalled(result: dict[str, JsonValue]) -> list[MemoryRecord]:
    entries = result["records"]
    assert isinstance(entries, list)
    records: list[MemoryRecord] = []
    for entry in entries:
        assert isinstance(entry, dict)
        records.append(MemoryRecord.model_validate(entry["record"]))
    return records


def legacy_database(tmp_path: Path) -> tuple[Path, Path, bytes]:
    """Materialize the version-one layout without using the current schema code."""
    workspace = tmp_path / "legacy-workspace"
    workspace.mkdir()
    state = tmp_path / "legacy-state"
    state.mkdir()
    record = canonical_json(
        {
            "id": "thread_legacy",
            "question": "Which manifests exist?",
            "conditions": {"revision": "fixture-v1"},
            "assessment": "Keep the historical assessment",
            "next_action": "Read inventory",
            "status": "active",
            "reopen_condition": "",
            "closure_reason": "",
            "version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    with closing(sqlite3.connect(state / "state.sqlite3")) as connection:
        connection.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE threads (id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                status TEXT NOT NULL, record TEXT NOT NULL);
            CREATE TABLE thread_history (thread_id TEXT REFERENCES threads(id),
                version INTEGER NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY (thread_id, version));
            CREATE TABLE runs (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL
                REFERENCES threads(id), status TEXT NOT NULL, record TEXT NOT NULL);
            CREATE TABLE evidence (id TEXT PRIMARY KEY, run_id TEXT NOT NULL
                REFERENCES runs(id), thread_id TEXT NOT NULL REFERENCES threads(id),
                record TEXT NOT NULL);
            CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, thread_id TEXT REFERENCES threads(id),
                run_id TEXT REFERENCES runs(id), session_id TEXT,
                created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX runs_thread ON runs(thread_id);
            CREATE INDEX evidence_thread ON evidence(thread_id);
            CREATE INDEX events_thread ON events(thread_id, seq);
            CREATE INDEX events_run ON events(run_id, seq);
            PRAGMA user_version=1;
        """)
        connection.execute(
            "INSERT INTO metadata VALUES ('workspace',?)", (str(workspace),)
        )
        connection.execute(
            "INSERT INTO threads VALUES ('thread_legacy',1,'active',?)",
            (record.decode(),),
        )
        connection.execute(
            "INSERT INTO thread_history VALUES ('thread_legacy',1,?)",
            (record.decode(),),
        )
        connection.commit()
    return workspace, state, record


def test_version_one_upgrade_preserves_records_and_artifacts(tmp_path: Path) -> None:
    workspace, state, old_record = legacy_database(tmp_path)
    artifacts = ArtifactStore(state / "artifacts")
    digest = artifacts.put(b"Historical immutable bytes")
    migrated = HarnessStore(state, workspace)
    assert (
        migrated.get_thread("thread_legacy").assessment
        == "Keep the historical assessment"
    )
    note = migrated.create_memory("thread_legacy", "Use SQLite", source="operator")
    assert HarnessStore(state, workspace).get_memory("thread_legacy", note.id) == note
    with closing(sqlite3.connect(migrated.database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute("SELECT record FROM threads").fetchone()[0]
            == old_record.decode()
        )
        assert (
            connection.execute("SELECT record FROM thread_history").fetchone()[0]
            == old_record.decode()
        )
    assert migrated.artifacts.read(digest) == b"Historical immutable bytes"


def test_schema_upgrade_rolls_back_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, state, old_record = legacy_database(tmp_path)
    monkeypatch.setattr(
        store_module,
        "_MEMORY_SCHEMA",
        (*store_module._MEMORY_SCHEMA, "INVALID SQL FOR MIGRATION TEST"),
    )
    with pytest.raises(sqlite3.OperationalError):
        HarnessStore(state, workspace)
    with closing(sqlite3.connect(state / "state.sqlite3")) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'memory_%'"
            ).fetchall()
            == []
        )
        assert (
            connection.execute("SELECT record FROM threads").fetchone()[0]
            == old_record.decode()
        )


def test_wrong_workspace_cannot_trigger_migration(tmp_path: Path) -> None:
    workspace, state, _ = legacy_database(tmp_path)
    other = tmp_path / "different-workspace"
    other.mkdir()
    with pytest.raises(ConflictError, match="another workspace"):
        HarnessStore(state, other)
    with closing(sqlite3.connect(state / "state.sqlite3")) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert HarnessStore(state, workspace).get_thread("thread_legacy").version == 1


def test_memory_provenance_versions_and_forget_survive_restart(
    store: HarnessStore,
) -> None:
    thread = store.create_thread("Which manifest format should be used?")
    run = store.start_run(thread.id, "inventory")
    digest = store.artifacts.put(b"A pyproject.toml manifest was observed")
    evidence = store.record_evidence(
        run.id, source="local fixture", summary="One manifest", artifact_sha=digest
    )
    note = store.create_memory(
        thread.id,
        "Use the existing TOML manifest",
        source="operator decision",
        kind="decision",
        tags=["Build", "build", " Python "],
        evidence_ids=[evidence.id, evidence.id],
        pinned=True,
        importance=9,
    )
    assert note.tags == ["Build", "Python"]
    assert note.evidence_ids == [evidence.id]
    updated = store.update_memory(
        thread.id,
        note.id,
        expected_version=1,
        text="Keep the manifest and the committed lock",
        pinned=False,
        importance=0,
    )
    forgotten = store.forget_memory(
        thread.id, note.id, expected_version=2, reason="Superseded by a new decision"
    )
    resumed = HarnessStore(store.state_dir, store.workspace)
    assert resumed.get_memory(thread.id, note.id) == forgotten
    assert resumed.memory_history(thread.id, note.id) == [note, updated, forgotten]
    assert resumed.memory_history(thread.id, note.id, offset=1, limit=1) == [updated]
    assert recalled(resumed.search_memory(thread.id)) == []
    assert resumed.search_memory(thread.id)["excluded"] == {
        "forgotten": 1,
        "expired": 0,
    }
    assert resumed.get_evidence(evidence.id) == evidence
    assert resumed.artifacts.read(digest) == b"A pyproject.toml manifest was observed"
    with pytest.raises(ConflictError, match="Forgotten"):
        resumed.update_memory(thread.id, note.id, expected_version=3, text="Restore")
    kinds = [event.kind for event in resumed.list_events(thread_id=thread.id)]
    assert kinds[-3:] == ["memory.created", "memory.updated", "memory.forgotten"]
    with pytest.raises(HarnessError, match="owning operation"):
        resumed.append_event("memory.created", thread_id=thread.id)


def test_memory_and_evidence_references_are_scoped(store: HarnessStore) -> None:
    first = store.create_thread("First workspace question")
    second = store.create_thread("Second workspace question")
    run = store.start_run(second.id, "inventory")
    evidence = store.record_evidence(
        run.id,
        source="fixture",
        summary="Observed",
        artifact_sha=store.artifacts.put(b"Observed bytes"),
    )
    with pytest.raises(ConflictError, match="same thread"):
        store.create_memory(
            first.id,
            "Cannot attach other thread evidence",
            source="operator",
            evidence_ids=[evidence.id],
        )
    with pytest.raises(NotFoundError):
        store.create_memory(
            first.id,
            "Missing evidence",
            source="operator",
            evidence_ids=["evidence_unknown"],
        )
    note = store.create_memory(first.id, "Scoped decision", source="operator")
    with pytest.raises(NotFoundError):
        store.get_memory(second.id, note.id)
    with pytest.raises(NotFoundError):
        store.update_memory(second.id, note.id, expected_version=1, text="Wrong scope")
    with pytest.raises(NotFoundError):
        store.forget_memory(
            second.id, note.id, expected_version=1, reason="Wrong scope"
        )
    with pytest.raises(NotFoundError):
        store.memory_history(second.id, note.id)
    with pytest.raises(ConflictError, match="same thread"):
        store.update_memory(
            first.id, note.id, expected_version=1, evidence_ids=[evidence.id]
        )
    assert recalled(store.search_memory(second.id)) == []
    assert store.get_memory(first.id, note.id) == note


def test_concurrent_memory_writes_reject_stale_version(store: HarnessStore) -> None:
    thread = store.create_thread("Concurrent decisions")
    note = store.create_memory(thread.id, "Initial decision", source="operator")

    def update(text: str) -> MemoryRecord | ConflictError:
        another = HarnessStore(store.state_dir, store.workspace)
        try:
            return another.update_memory(
                thread.id, note.id, expected_version=1, text=text
            )
        except ConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, ["First update", "Second update"]))
    assert sum(isinstance(result, ConflictError) for result in outcomes) == 1
    assert store.get_memory(thread.id, note.id).version == 2
    assert len(store.memory_history(thread.id, note.id)) == 2
    with pytest.raises(ConflictError, match="version changed"):
        store.forget_memory(thread.id, note.id, expected_version=1, reason="Stale")
    with pytest.raises(HarnessError, match="positive expected_version"):
        store.update_memory(thread.id, note.id, expected_version=True, text="Invalid")


def test_memory_event_failure_rolls_back_all_state(store: HarnessStore) -> None:
    thread = store.create_thread("Atomic memory update")
    note = store.create_memory(thread.id, "Original", source="operator")
    with closing(sqlite3.connect(store.database_path)) as connection:
        connection.execute(
            "CREATE TRIGGER fail_memory BEFORE INSERT ON events "
            "WHEN NEW.kind='memory.updated' BEGIN "
            "SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
        store.update_memory(thread.id, note.id, expected_version=1, text="Lost update")
    assert store.get_memory(thread.id, note.id) == note
    assert store.memory_history(thread.id, note.id) == [note]
    assert recalled(store.search_memory(thread.id, query="Original")) == [note]


def test_expiry_and_explicit_clearing(store: HarnessStore) -> None:
    thread = store.create_thread("Time limited constraints")
    expired = store.create_memory(
        thread.id,
        "Obsolete constraint",
        source="operator",
        kind="constraint",
        pinned=True,
        expires_at="2000-01-01T03:00:00+03:00",
    )
    assert expired.expires_at == "2000-01-01T00:00:00.000000+00:00"
    assert store.search_memory(thread.id)["excluded"] == {"forgotten": 0, "expired": 1}
    assert recalled(store.context_memory_records(thread.id, query="unrelated")[3]) == []
    unchanged = store.update_memory(
        thread.id, expired.id, expected_version=1, expires_at=None
    )
    assert unchanged.expires_at == expired.expires_at
    active = store.update_memory(
        thread.id, expired.id, expected_version=2, clear_expiry=True
    )
    assert active.expires_at is None
    assert recalled(store.search_memory(thread.id)) == [active]
    with pytest.raises(HarnessError, match="not both"):
        store.update_memory(
            thread.id,
            expired.id,
            expected_version=3,
            expires_at="2099-01-01T00:00:00Z",
            clear_expiry=True,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        store.create_memory(
            thread.id,
            "No timezone",
            source="operator",
            expires_at="2099-01-01T00:00:00",
        )


def test_literal_filter_precedes_candidate_window(store: HarnessStore) -> None:
    thread = store.create_thread("Find the old decision")
    relevant = store.create_memory(
        thread.id, "Use SQLite for persistence", source="operator", importance=0
    )
    for index in range(40):
        store.create_memory(
            thread.id,
            f"Unrelated display preference {index}",
            source="operator",
            importance=10,
        )
    selected = store.search_memory(thread.id, query="sQlItE", candidate_limit=2)
    assert recalled(selected) == [relevant]
    assert selected["eligible_notes"] == 41
    assert selected["matching_notes"] == selected["candidates_scanned"] == 1
    assert recalled(store.search_memory(thread.id, query="unseen term")) == []
    literal = store.create_memory(
        thread.id,
        "The literal token is 25%_done",
        source="operator",
        tags=["Result", "a\nb"],
    )
    assert recalled(store.search_memory(thread.id, query="%_")) == [literal]
    assert recalled(store.search_memory(thread.id, tags=["RESULT", "a\nb"])) == [
        literal
    ]
    assert recalled(store.search_memory(thread.id, tags=["a", "b"])) == []
    assert recalled(store.search_memory(thread.id, kind="lesson")) == []


def test_context_only_supplements_active_pinned_constraints(
    store: HarnessStore,
) -> None:
    thread = store.create_thread("Review the packaging")
    pinned = store.create_memory(
        thread.id,
        "Keep the committed lock file",
        source="operator",
        kind="constraint",
        pinned=True,
    )
    store.create_memory(
        thread.id, "Unrelated pinned note", source="operator", kind="note", pinned=True
    )
    store.create_memory(
        thread.id, "Unrelated ordinary constraint", source="operator", kind="constraint"
    )
    assert recalled(store.search_memory(thread.id, query="packaging")) == []
    result = store.context_memory_records(thread.id, query="packaging")[3]
    assert recalled(result) == [pinned]
    assert result["matching_notes"] == 0
    assert result["pinned_constraints_supplemented"] == 1
    entries = result["records"]
    assert isinstance(entries, list)
    assert isinstance(entries[0], dict)
    assert entries[0]["matched_query"] is False
    assert entries[0]["selection_reason"] == "pinned_constraint"


def test_default_context_uses_terms_and_explicit_empty_query_browses(
    store: HarnessStore,
) -> None:
    thread = store.create_thread(
        "Which imports changed?", next_action="Compare the json inventory"
    )
    relevant = store.create_memory(
        thread.id, "Use the json inventory baseline", source="operator"
    )
    unrelated = store.create_memory(
        thread.id, "Prefer a blue background", source="operator"
    )
    result = store.context_memory_records(thread.id)[3]
    assert recalled(result) == [relevant]
    assert result["query"] == "Which imports changed? Compare the json inventory"
    scope = result["scope"]
    assert isinstance(scope, dict)
    assert scope["query_terms"] == [
        "imports",
        "changed",
        "compare",
        "json",
        "inventory",
    ]
    assert scope["query_source"] == "thread_question_and_next_action"
    assert {
        record.id
        for record in recalled(store.context_memory_records(thread.id, query="")[3])
    } == {relevant.id, unrelated.id}
    assert (
        recalled(store.context_memory_records(thread.id, query="imports changed")[3])
        == []
    )


@pytest.mark.parametrize("action_details", ["", " Routine details" * 40])
def test_default_context_query_preserves_both_long_fields(
    store: HarnessStore, action_details: str
) -> None:
    thread = store.create_thread(
        "Background context " * 40,
        next_action="Compare the json inventory" + action_details,
    )
    question_note = store.create_memory(
        thread.id, "Background context reference", source="operator"
    )
    action_note = store.create_memory(
        thread.id, "Use the json inventory baseline", source="operator"
    )

    result = store.context_memory_records(thread.id)[3]

    assert {record.id for record in recalled(result)} == {
        question_note.id,
        action_note.id,
    }
    assert isinstance(result["query"], str)
    assert len(result["query"]) <= 512
    assert "Background context" in result["query"]
    assert "Compare the json inventory" in result["query"]


def test_default_context_terms_reserve_capacity_for_next_action(
    store: HarnessStore,
) -> None:
    thread = store.create_thread(
        " ".join(f"q{index:02}" for index in range(40)), next_action="json"
    )
    question_note = store.create_memory(thread.id, "q30 decision", source="operator")
    action_note = store.create_memory(thread.id, "json decision", source="operator")

    result = store.context_memory_records(thread.id)[3]

    assert {record.id for record in recalled(result)} == {
        question_note.id,
        action_note.id,
    }
    scope = result["scope"]
    assert isinstance(scope, dict)
    terms = scope["query_terms"]
    assert isinstance(terms, list)
    assert len(terms) == 32
    assert "json" in terms
    assert "q30" in terms
    assert "q31" not in terms


def test_recall_is_read_only_and_rankings_are_deterministic(
    store: HarnessStore,
) -> None:
    thread = store.create_thread("Review the build")
    for text in ["Build Python packages", "Build a local wheel", "Build docs"]:
        store.create_memory(thread.id, text, source="operator")
    events = store.list_events(thread_id=thread.id)
    first = store.search_memory(thread.id, query="build")
    resumed = HarnessStore(store.state_dir, store.workspace)
    second = resumed.search_memory(thread.id, query="build")
    assert first["records"] == second["records"]
    assert resumed.list_events(thread_id=thread.id) == events
    assert [record.version for record in recalled(first)] == [1, 1, 1]
    assert hashing_features("Python package") == hashing_features("Python package")
    assert sum(
        value * value for value in hashing_features("Python package")
    ) == pytest.approx(1.0)
    assert reciprocal_rank_fusion([["a", "a"], ["a"]])["a"] == pytest.approx(2 / 61)


def test_candidate_result_and_byte_omissions_are_explicit(store: HarnessStore) -> None:
    thread = store.create_thread("Bounded recall")
    for index in range(6):
        store.create_memory(thread.id, f"Build option {index}", source="operator")
    limited = store.search_memory(thread.id, query="build", candidate_limit=4, limit=2)
    assert len(recalled(limited)) == 2
    assert limited["candidate_notes"] == 6
    assert limited["candidates_scanned"] == 4
    assert limited["candidates_truncated"] is True
    assert limited["omissions"] == {
        "candidate_limit": 2,
        "result_limit": 2,
        "byte_budget": 0,
    }
    for _ in range(3):
        store.create_memory(thread.id, "🌍" * 8192, source="operator")
    bounded = store.search_memory(thread.id, query="🌍", limit=100)
    assert len(canonical_json(bounded)) <= MEMORY_RESULT_BYTES
    assert len(recalled(bounded)) == 1
    assert bounded["omissions"] == {
        "candidate_limit": 0,
        "result_limit": 0,
        "byte_budget": 2,
    }
    assert len(recalled(bounded)[0].text) == 8192


def test_thread_evidence_and_memory_share_one_read_snapshot(
    store: HarnessStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = store.create_thread("Coherent context")
    old = store.create_memory(thread.id, "Existing context", source="operator")
    original = HarnessStore._context_records

    def interleave(
        connection: sqlite3.Connection, thread_id: str, limit: int
    ) -> tuple[ThreadRecord, list[tuple[EvidenceRecord, RunRecord]], int]:
        result = original(connection, thread_id, limit)
        writer = HarnessStore(store.state_dir, store.workspace)
        writer.update_thread(
            thread_id, assessment="A later version", expected_version=1
        )
        writer.create_memory(thread_id, "New context", source="operator")
        return result

    monkeypatch.setattr(HarnessStore, "_context_records", staticmethod(interleave))
    observed_thread, evidence, total, memory = store.context_memory_records(
        thread.id, query=""
    )
    assert observed_thread.version == 1
    assert evidence == [] and total == 0
    assert recalled(memory) == [old]
    assert store.get_thread(thread.id).version == 2
    assert len(recalled(store.search_memory(thread.id))) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", " "),
        ("text", "x" * 8193),
        ("source", ""),
        ("source", "x" * 2049),
        ("kind", "unrecognized"),
        ("tags", ["x"] * 17),
        ("tags", ["x" * 65]),
        ("tags", [" "]),
        ("evidence_ids", ["id"] * 33),
        ("evidence_ids", ["x" * 129]),
        ("importance", 11),
        ("importance", -1),
        ("importance", True),
        ("pinned", 1),
        ("expires_at", "tomorrow"),
    ],
)
def test_memory_record_bounds(
    store: HarnessStore, field: str, value: JsonValue
) -> None:
    thread = store.create_thread("Bounded records")
    record = store.create_memory(thread.id, "Valid note", source="operator")
    with pytest.raises(ValueError):
        MemoryRecord.model_validate({**record.model_dump(), field: value})
    assert store.get_memory(thread.id, record.id) == record


@pytest.mark.parametrize(
    ("limit", "candidate_limit"),
    [
        (0, 1),
        (101, 1),
        (True, 1),
        (1, 0),
        (1, 501),
        (1, True),
    ],
)
def test_recall_bounds(store: HarnessStore, limit: int, candidate_limit: int) -> None:
    thread = store.create_thread("Bounded queries")
    with pytest.raises(HarnessError):
        store.search_memory(thread.id, limit=limit, candidate_limit=candidate_limit)
    with pytest.raises(HarnessError, match="512"):
        store.search_memory(thread.id, query="x" * 513)


def test_record_size_limit_applies_before_memory_write(store: HarnessStore) -> None:
    thread = store.create_thread("Serialized record bounds")
    with pytest.raises(HarnessError, match="65536"):
        store.create_memory(
            thread.id,
            "\x00" * 8192,
            source="\x00" * 2048,
            tags=["\x00" * 62 + f"{index:02d}" for index in range(16)],
        )
    assert recalled(store.search_memory(thread.id)) == []
    assert [event.kind for event in store.list_events(thread_id=thread.id)] == [
        "thread.created"
    ]
