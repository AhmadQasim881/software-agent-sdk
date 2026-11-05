"""Real CLI failures preserve the bounded JSON error contract."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from openhands.tools.research_harness.store import HarnessStore


def _run(
    directory: Path, workspace: Path, state: Path, operation: str, arguments: str = "{}"
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "OPENHANDS_SUPPRESS_BANNER": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "openhands.research",
            "--workspace",
            str(workspace),
            "--state",
            str(state),
            operation,
            arguments,
        ],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_corrupted_sqlite_returns_json_error_and_exit_two(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.sqlite3").write_bytes(b"not a sqlite database" * 100)
    result = _run(tmp_path, workspace, state, "thread.list")
    assert result.returncode == 2, result.stderr
    observed = json.loads(result.stdout)
    assert observed["ok"] is False
    assert "database" in observed["error"].lower()
    assert len(observed["error"]) <= 1000
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("operation", "field"),
    [
        ("thread.list", "offset"),
        ("events", "after_seq"),
        ("events.export", "after_seq"),
    ],
)
def test_huge_cursors_are_rejected_before_ledger_changes(
    tmp_path: Path, operation: str, field: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    result = _run(tmp_path, workspace, state, operation, json.dumps({field: 2**63}))
    assert result.returncode == 2, result.stderr
    observed = json.loads(result.stdout)
    assert observed["ok"] is False
    assert field in observed["error"]
    assert "less than or equal" in observed["error"]
    assert "Traceback" not in result.stderr
    assert HarnessStore(state, workspace).list_events() == []
