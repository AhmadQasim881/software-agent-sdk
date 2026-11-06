"""Synchronous hook timeouts stop their owned POSIX process groups."""

import contextlib
import os
import time
from pathlib import Path

import psutil
import pytest

from openhands.sdk.hooks.config import HookDefinition
from openhands.sdk.hooks.executor import HookExecutor
from openhands.sdk.hooks.types import HookEvent, HookEventType
from tests.command_utils import python_command


def _running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() not in (
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        )
    except psutil.NoSuchProcess:
        return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group lifecycle")
@pytest.mark.parametrize("leader_exits", [False, True], ids=["live", "exited"])
def test_sync_timeout_stops_descendant_after_shell_exit(
    tmp_path: Path, leader_exits: bool
) -> None:
    marker = tmp_path / "owned-child.pid"
    script = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(marker)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    command = python_command(script)
    if leader_exits:
        command += " &"
    try:
        started = time.monotonic()
        result = HookExecutor(working_dir=str(tmp_path)).execute(
            HookDefinition(command=command, timeout=1),
            HookEvent(event_type=HookEventType.SESSION_START),
        )
        elapsed = time.monotonic() - started
        assert marker.exists(), "The owned child must start before the timeout"
        pid = int(marker.read_text())
        assert not result.success
        assert result.exit_code == -1
        assert result.error == "Hook timed out after 1 seconds"
        assert elapsed < 3
        deadline = time.monotonic() + 1
        while _running(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _running(pid), "Timed-out hook descendant is still running"
    finally:
        if marker.exists():
            with contextlib.suppress(psutil.NoSuchProcess):
                child = psutil.Process(int(marker.read_text()))
                if script in child.cmdline():
                    child.kill()
