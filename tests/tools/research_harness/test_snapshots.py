"""Snapshot comparisons and exact queries preserve content and coverage boundaries."""

import os
from pathlib import Path

import pytest
from pydantic import JsonValue

from openhands.tools.research_harness.artifacts import ArtifactStore, canonical_json
from openhands.tools.research_harness.inventory import inventory_python
from openhands.tools.research_harness.models import HarnessError
from openhands.tools.research_harness.snapshots import (
    compare_inventories,
    query_inventory,
)


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX anchored inventory")


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


def _snapshot(store: ArtifactStore, workspace: Path, source: str) -> str:
    (workspace / "main.py").write_text(source)
    return store.put(canonical_json(inventory_python(workspace)))


def _object(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def test_comparison_ignores_comments_and_line_movement(
    store: ArtifactStore, workspace: Path
) -> None:
    before = _snapshot(store, workspace, "from pathlib import Path as P\n")
    after = _snapshot(
        store, workspace, "# a comment\n\nfrom pathlib import Path as P\n"
    )
    assert before != after
    result = compare_inventories(store, before, after)
    assert result["ok"] is True
    assert result["normalization_compatible"] is True
    assert result["observed_equal"] is True
    assert result["added"] == result["removed"] == []
    assert result["unchanged_count"] == 1
    assert _object(result["before"])["artifact_sha"] == before
    assert _object(result["after"])["artifact_sha"] == after


def test_same_count_replacement_and_duplicates_are_content_changes(
    store: ArtifactStore, workspace: Path
) -> None:
    before = _snapshot(store, workspace, "import os\nimport os\nimport sys\n")
    after = _snapshot(store, workspace, "import os\nimport pathlib\nimport pathlib\n")
    result = compare_inventories(store, before, after)
    assert result["observed_equal"] is False
    assert result["added_count"] == result["removed_count"] == 2
    assert result["unchanged_count"] == 1
    assert result["added"] == [
        {"path": "main.py", "normalized": "import pathlib", "count": 2}
    ]
    assert result["removed"] == [
        {"path": "main.py", "normalized": "import os", "count": 1},
        {"path": "main.py", "normalized": "import sys", "count": 1},
    ]


def test_path_changes_are_preserved(store: ArtifactStore, workspace: Path) -> None:
    before = _snapshot(store, workspace, "import os\n")
    (workspace / "main.py").rename(workspace / "moved.py")
    after = store.put(canonical_json(inventory_python(workspace)))
    result = compare_inventories(store, before, after)
    assert result["observed_equal"] is False
    assert result["added"] == [
        {"path": "moved.py", "normalized": "import os", "count": 1}
    ]
    assert result["removed"] == [
        {"path": "main.py", "normalized": "import os", "count": 1}
    ]


def test_exact_query_filters_and_snapshot_only_behavior(
    store: ArtifactStore, workspace: Path
) -> None:
    digest = _snapshot(
        store,
        workspace,
        "import alpha as local, alpha.beta\n"
        "from alpha import thing as renamed\n"
        "from . import sibling\n"
        "from other import thing\n",
    )
    (workspace / "main.py").unlink()
    assert query_inventory(store, digest, module="alpha")["match_count"] == 2
    assert query_inventory(store, digest, module="alpha.beta")["match_count"] == 1
    assert query_inventory(store, digest, name="thing")["match_count"] == 2
    assert query_inventory(store, digest, name="renamed")["match_count"] == 0
    assert query_inventory(store, digest, module="")["match_count"] == 1
    result = query_inventory(
        store, digest, module="alpha", name="thing", path="main.py"
    )
    assert result["match_count"] == 1
    assert result["entries_omitted"] == 0
    assert result["dataset_complete"] is True
    provenance = _object(result["provenance"])
    assert provenance["artifact_sha"] == digest
    assert provenance["tool_version"] == "python-import-inventory/1"
    assert _object(provenance["coverage"])["imports_recorded"] == 4
    assert query_inventory(store, digest, path="other.py")["match_count"] == 0


def test_partial_empty_dataset_is_distinct_from_complete_empty_matches(
    store: ArtifactStore, workspace: Path
) -> None:
    complete = _snapshot(store, workspace, "value = 1\n")
    partial = _snapshot(store, workspace, "from broken import (\n")
    valid = query_inventory(store, complete, name="missing")
    incomplete = query_inventory(store, partial, name="missing")
    assert valid["ok"] is incomplete["ok"] is True
    assert valid["match_count"] == incomplete["match_count"] == 0
    assert valid["dataset_complete"] is True
    assert incomplete["dataset_complete"] is False
    assert "partial dataset" in str(incomplete["summary"])
    provenance = _object(incomplete["provenance"])
    assert _object(provenance["coverage"])["files_failed"] == 1
    assert _object(provenance["coverage_details_in_artifact"])["errors"] == 1
    compared = compare_inventories(store, complete, partial)
    assert compared["observed_equal"] is True
    assert compared["dataset_complete"] is False
    assert "partial datasets" in str(compared["summary"])


def test_selection_counts_and_byte_omissions_are_explicit(
    store: ArtifactStore, workspace: Path
) -> None:
    empty = _snapshot(store, workspace, "value = 1\n")
    many = _snapshot(
        store, workspace, "".join(f"import module_{index}\n" for index in range(130))
    )
    queried = query_inventory(store, many, limit=3)
    assert queried["match_count"] == 130
    assert queried["entries_omitted"] == 127
    rows = queried["entries"]
    assert isinstance(rows, list) and len(rows) == 3
    compared = compare_inventories(store, empty, many)
    assert compared["added_count"] == 130
    assert _object(compared["omitted"])["added"] == {
        "distinct_imports": 30,
        "occurrences": 30,
    }
    long_names = _snapshot(
        store, workspace, "import a" + "x" * 70_000 + "\nimport b" + "x" * 70_000 + "\n"
    )
    queried = query_inventory(store, long_names)
    assert queried["match_count"] == 2
    assert queried["entries"] == []
    assert queried["entries_omitted"] == 2
    compared = compare_inventories(store, empty, long_names)
    assert compared["added_count"] == 2
    assert _object(compared["omitted"])["added"] == {
        "distinct_imports": 1,
        "occurrences": 1,
    }
    assert len(canonical_json(queried)) < store.max_bytes
    assert len(canonical_json(compared)) < store.max_bytes


def test_incompatible_parser_versions_do_not_claim_accepted_comparison(
    store: ArtifactStore, workspace: Path
) -> None:
    before = _snapshot(store, workspace, "import os\n")
    changed = inventory_python(workspace)
    changed["python_version"] = "0.0.1"
    after = store.put(canonical_json(changed))
    result = compare_inventories(store, before, after)
    assert result["ok"] is False
    assert result["normalization_compatible"] is False
    assert "Incompatible parser versions" in str(result["summary"])
    assert result["observed_equal"] is True
    assert result["unchanged_count"] == 1
    assert _object(result["after"])["python_version"] == "0.0.1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("tool_version", "unknown/1"),
        ("complete", False),
        ("entries", None),
    ],
)
def test_invalid_snapshot_schema_is_rejected(
    store: ArtifactStore, workspace: Path, field: str, value: JsonValue
) -> None:
    _snapshot(store, workspace, "import os\n")
    invalid = inventory_python(workspace)
    invalid[field] = value
    digest = store.put(canonical_json(invalid))
    with pytest.raises(HarnessError, match="Invalid inventory snapshot"):
        query_inventory(store, digest)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "../main.py"),
        ("line", True),
        ("names", [{}]),
        ("normalized", "import different"),
    ],
)
def test_invalid_entry_shape_and_content_are_rejected(
    store: ArtifactStore, workspace: Path, field: str, value: JsonValue
) -> None:
    _snapshot(store, workspace, "import os\n")
    invalid = inventory_python(workspace)
    entries = invalid["entries"]
    assert isinstance(entries, list)
    _object(entries[0])[field] = value
    digest = store.put(canonical_json(invalid))
    with pytest.raises(HarnessError, match="Invalid inventory snapshot"):
        query_inventory(store, digest)


@pytest.mark.parametrize("kind", ["missing", "malformed", "tampered"])
def test_unavailable_or_invalid_artifact_is_not_an_empty_answer(
    store: ArtifactStore, workspace: Path, kind: str
) -> None:
    digest = "0" * 64
    if kind == "malformed":
        digest = store.put(b"{incomplete")
    elif kind == "tampered":
        digest = _snapshot(store, workspace, "import os\n")
        (store.root / digest).write_bytes(b"{}")
    with pytest.raises(HarnessError, match="inventory"):
        query_inventory(store, digest)


@pytest.mark.parametrize("limit", [0, -1, True, 101])
def test_query_limit_is_strict(store: ArtifactStore, limit: int) -> None:
    with pytest.raises(HarnessError, match="limit"):
        query_inventory(store, "0" * 64, limit=limit)


def test_invalid_query_path_is_rejected_before_dataset_access(
    store: ArtifactStore,
) -> None:
    with pytest.raises(HarnessError, match="relative .py"):
        query_inventory(store, "0" * 64, path="../outside.py")
