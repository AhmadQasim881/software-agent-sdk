"""Subprocess commands for installed Python and the bundled Agent Server."""

import sys
from pathlib import Path


HARNESS_MODE = "--research-harness"
HTTP_WORKER_MODE = "--research-harness-http-worker"
HTTP_WORKER_SOURCE = (
    Path(__file__).parent.parent / "tools/research_harness/_http_worker.py"
)


def is_frozen() -> bool:
    # PyInstaller adds this marker at runtime; ordinary Python has no such field.
    return bool(getattr(sys, "frozen", False))


def harness_command(python_executable: Path | None = None) -> list[str]:
    """Use an explicit interpreter when supplied, preserving venv symlinks."""
    if python_executable is None and is_frozen():
        return [sys.executable, HARNESS_MODE]
    python = str(python_executable) if python_executable is not None else sys.executable
    return [python, "-I", "-m", "openhands.research"]


def http_worker_command(script: Path, repository: str) -> list[str]:
    if is_frozen():
        return [sys.executable, HTTP_WORKER_MODE, repository]
    return [sys.executable, "-I", "-S", str(script), repository]
