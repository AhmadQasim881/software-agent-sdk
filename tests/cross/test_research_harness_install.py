"""Failure checks for the installed-wheel acceptance entry point."""

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / ".github/scripts/verify_research_harness_install.py"


def arguments(report: Path) -> list[str]:
    return [
        sys.executable,
        "-I",
        str(RUNNER),
        "--python",
        sys.executable,
        "--checkout",
        str(ROOT),
        "--wheel",
        str(report.parent / "not-installed.whl"),
        "--report",
        str(report),
        "--runtime-kind",
        "copied-runtime",
    ]


def test_source_runtime_is_rejected_and_owned_workspace_is_removed(
    tmp_path: Path,
) -> None:
    """The normal editable test environment must not pass wheel acceptance."""
    report_path = tmp_path / "report.json"
    completed = subprocess.run(
        arguments(report_path), cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 1, completed.stderr
    report = json.loads(report_path.read_text())
    assert report["status"] == "failed"
    assert report["local_smoke"]["status"] == "failed"
    assert report["local_smoke"]["cli"] == []
    assert report["error"] == {
        "type": "VerificationError",
        "message": (
            "openhands-tools must be installed from the wheel, not an editable checkout"
        ),
    }
    assert report["temporary_root_removed"] is True
    assert not Path(report["temporary_root"]).exists()
    assert report["live_github"]["requests"] == 0


def test_live_check_rejects_repository_outside_this_fork(tmp_path: Path) -> None:
    """A caller cannot turn this acceptance runner into an arbitrary URL probe."""
    report_path = tmp_path / "report.json"
    completed = subprocess.run(
        [*arguments(report_path), "--github-repository", "other/repository"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2
    assert "invalid choice" in completed.stderr
    assert not report_path.exists()
