"""Frozen acceptance must reject leaked children even when its guardian cleans up."""

import contextlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / ".github/scripts/verify_research_harness_frozen.py"


@pytest.mark.skipif(os.name != "posix", reason="POSIX owned process groups")
def test_frozen_acceptance_rejects_child_surviving_normal_completion(
    tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location("frozen_acceptance_test", RUNNER)
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    report: dict[str, Any] = {}
    check = checker.FrozenCheck(Path(sys.executable), tmp_path, report)
    marker = tmp_path / "owned-child.pid"
    child_arguments = [
        sys.executable,
        "-I",
        "-c",
        "import time; time.sleep(30)",
        str(marker),
    ]
    program = (
        "import json, subprocess; from pathlib import Path; "
        f"child = subprocess.Popen({child_arguments!r}, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True); "
        f"Path({str(marker)!r}).write_text(str(child.pid)); "
        "print(json.dumps({'ok': True}))"
    )
    try:
        with pytest.raises(
            checker.VerificationError,
            match="owned process survived normal completion",
        ):
            check.invoke(
                [sys.executable, "-I", "-c", program], label="owned-leak-fixture"
            )
        pid = int(marker.read_text())
        observation = report["commands"][0]
        assert observation["returncode"] == 0
        assert pid in observation["survivors_before_cleanup"]
        assert observation["cleanup"]["ok"] is True
        assert observation["cleanup"]["remaining_pids"] == []
        with contextlib.suppress(psutil.NoSuchProcess):
            assert psutil.Process(pid).status() in (
                psutil.STATUS_ZOMBIE,
                psutil.STATUS_DEAD,
            )
    finally:
        if marker.exists():
            with contextlib.suppress(psutil.NoSuchProcess):
                child = psutil.Process(int(marker.read_text()))
                if str(marker) in child.cmdline():
                    child.kill()
                    psutil.wait_procs([child], timeout=1)
