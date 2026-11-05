"""Inventory semantics, failure honesty, and bounded filesystem access."""

import ast
import hashlib
import os
import threading
from pathlib import Path

import pytest
from pydantic import JsonValue

from openhands.tools.research_harness import inventory
from openhands.tools.research_harness.artifacts import canonical_json
from openhands.tools.research_harness.inventory import inventory_python


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX anchored reads")


def objects(value: JsonValue) -> list[dict[str, JsonValue]]:
    assert isinstance(value, list)
    result: list[dict[str, JsonValue]] = []
    for item in value:
        assert isinstance(item, dict)
        result.append(item)
    return result


def coverage(result: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = result["coverage"]
    assert isinstance(value, dict)
    return value


def test_imports_are_static_deterministic_and_preserve_aliases(tmp_path: Path) -> None:
    source = (
        "import os as operating, sys\n"
        "from pathlib import Path as P\n"
        "from . import sibling\n"
        "from ..package import thing\n"
        "raise RuntimeError('must never execute')\n"
    )
    (tmp_path / "main.py").write_text(source)
    (tmp_path / "notes.txt").write_text("import not_python\n")
    (tmp_path / "plain.py").write_text("value = 'import inside_string'\n")
    result = inventory_python(tmp_path)
    assert result == inventory_python(tmp_path)
    assert result["ok"] is True
    assert result["complete"] is True
    entries = objects(result["entries"])
    assert [entry["normalized"] for entry in entries] == [
        "import os as operating, sys",
        "from pathlib import Path as P",
        "from . import sibling",
        "from ..package import thing",
    ]
    assert entries[0]["names"] == [
        {"name": "os", "alias": "operating"},
        {"name": "sys", "alias": None},
    ]
    assert [entry["module"] for entry in entries] == ["", "pathlib", "", "package"]
    assert [entry["level"] for entry in entries] == [0, 0, 1, 2]
    assert [entry["line"] for entry in entries] == [1, 2, 3, 4]
    assert (
        objects(result["files"])[0]["sha256"]
        == hashlib.sha256(source.encode()).hexdigest()
    )
    assert coverage(result)["files_parsed"] == 2


def test_comment_and_line_movement_keeps_normalized_import(tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text("from pathlib import Path as P\n")
    before = inventory_python(tmp_path)
    path.write_text("# harmless comment\n\nfrom pathlib import Path as P # note\n")
    after = inventory_python(tmp_path)
    first = objects(before["entries"])[0]
    second = objects(after["entries"])[0]
    assert first["normalized"] == second["normalized"]
    assert first["path"] == second["path"]
    assert first["line"] != second["line"]
    assert objects(before["files"])[0]["sha256"] != objects(after["files"])[0]["sha256"]


def test_syntax_failure_is_not_an_empty_success(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("from broken import (\n")
    (tmp_path / "ordinary.py").write_text("x = 1\n")
    result = inventory_python(tmp_path)
    assert result["ok"] is False
    assert result["complete"] is False
    assert result["entries"] == []
    assert coverage(result)["files_failed"] == 1
    assert objects(coverage(result)["errors"])[0]["reason"] == "parse_error"
    assert objects(result["files"])[0]["sha256"] is not None


def test_exclusions_are_reported_and_tests_are_included(tmp_path: Path) -> None:
    for name in (".git", ".venv", "node_modules", "__pycache__", ".hidden"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "skip.py").write_text("import excluded\n")
    (tmp_path / ".hidden.py").write_text("import excluded\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text("import included\n")
    result = inventory_python(tmp_path)
    assert result["complete"] is True
    assert [entry["path"] for entry in objects(result["entries"])] == [
        "tests/test_example.py"
    ]
    assert coverage(result)["excluded_counts"] == {
        "excluded_directory": 4,
        "hidden_directory": 1,
        "hidden_file": 1,
    }


def test_file_size_limit_reads_only_a_sentinel_byte(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("import many\n" * 100)
    result = inventory_python(tmp_path, max_file_bytes=32)
    assert result["ok"] is False
    assert coverage(result)["bytes_read"] == 33
    assert objects(result["files"])[0] == {
        "path": "large.py",
        "status": "file_too_large",
        "bytes_read": 33,
        "sha256": None,
        "imports": 0,
    }


def test_total_byte_limit_is_strict_and_truthful(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("import many\n" * 100)
    result = inventory_python(tmp_path, max_total_bytes=30)
    assert result["complete"] is False
    assert result["entries"] == []
    assert coverage(result)["bytes_read"] == 30
    assert coverage(result)["stop_reason"] == "total_bytes_budget"


def test_file_limit_uses_stable_order_and_reports_omission(tmp_path: Path) -> None:
    (tmp_path / "z.py").write_text("import last\n")
    (tmp_path / "a.py").write_text("import first\n")
    result = inventory_python(tmp_path, max_files=1)
    assert result["ok"] is False
    assert [entry["path"] for entry in objects(result["entries"])] == ["a.py"]
    assert coverage(result)["stop_reason"] == "file_budget"
    assert coverage(result)["python_files_seen"] == 2


def test_traversal_is_bounded_even_without_python_matches(tmp_path: Path) -> None:
    for index in range(30):
        (tmp_path / f"irrelevant-{index}.txt").touch()
    result = inventory_python(tmp_path, max_files=1)
    assert result == inventory_python(tmp_path, max_files=1)
    assert result["complete"] is False
    assert coverage(result)["stop_reason"] == "traversal_budget"
    assert coverage(result)["entries_examined"] == 21
    assert result["files"] == []


def test_symlinks_and_special_files_are_never_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("import outside_scope\n")
    (workspace / "file.py").symlink_to(outside / "secret.py")
    (workspace / "directory").symlink_to(outside, target_is_directory=True)
    os.mkfifo(workspace / "pipe.py")
    result = inventory_python(workspace)
    assert result["entries"] == []
    assert coverage(result)["bytes_read"] == 0
    assert coverage(result)["excluded_counts"] == {"symlink": 2, "non_regular": 1}


def test_swapped_ancestor_cannot_escape_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (nested / "sample.py").write_text("import allowed\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sample.py").write_text("import outside_scope\n")
    original_open = os.open
    opens = 0

    def swap_ancestor(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal opens
        if path == "nested":
            opens += 1
            if opens == 2:
                nested.rename(workspace / "saved")
                nested.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(inventory.os, "open", swap_ancestor)
    result = inventory_python(workspace)
    assert opens == 2
    assert result["complete"] is False
    assert result["entries"] == []
    assert coverage(result)["bytes_read"] == 0
    assert objects(coverage(result)["errors"])[0]["reason"] == "read_error"


def test_cancellation_before_scan_and_after_parse_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "sample.py").write_text("import pathlib\n")
    cancelled = threading.Event()
    cancelled.set()
    before = inventory_python(tmp_path, cancel_event=cancelled)
    assert before["ok"] is False
    assert coverage(before)["stop_reason"] == "cancelled"
    assert coverage(before)["bytes_read"] == 0
    cancelled.clear()
    original_parse = ast.parse

    def cancel_after_parse(*args, **kwargs):
        tree = original_parse(*args, **kwargs)
        cancelled.set()
        return tree

    monkeypatch.setattr(inventory.ast, "parse", cancel_after_parse)
    during = inventory_python(tmp_path, cancel_event=cancelled)
    assert during["complete"] is False
    assert coverage(during)["stop_reason"] == "cancelled"
    assert objects(during["files"])[0]["status"] == "cancelled"
    assert during["entries"] == []


def test_output_budget_fits_artifact_limit(tmp_path: Path) -> None:
    (tmp_path / "many.py").write_text(
        "".join(f"import module_{index}\n" for index in range(10_000))
    )
    result = inventory_python(tmp_path)
    assert result["ok"] is False
    assert coverage(result)["stop_reason"] == "output_budget"
    assert len(canonical_json(result)) <= 1_048_576
    assert 0 < len(objects(result["entries"])) < 10_000


@pytest.mark.parametrize("limit", [0, -1, True, 10_001])
def test_invalid_limits_are_rejected(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="max_files"):
        inventory_python(tmp_path, max_files=limit)
