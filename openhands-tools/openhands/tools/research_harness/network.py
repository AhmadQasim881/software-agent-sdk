"""Public GitHub metadata with a process-owned total deadline and cancellation."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event, Thread
from typing import BinaryIO

from pydantic import JsonValue, TypeAdapter, ValidationError

from openhands.research.runtime import http_worker_command
from openhands.tools.research_harness._http_worker import (
    MAX_WORKER_BYTES,
    REQUEST_SECONDS,
    initial_result,
)


_WORKER_PATH = Path(__file__).with_name("_http_worker.py")
_RESULT = TypeAdapter(dict[str, JsonValue])


def _capture_output(stream: BinaryIO, output: bytearray, oversized: Event) -> None:
    output.extend(stream.read(MAX_WORKER_BYTES + 1))
    if len(output) > MAX_WORKER_BYTES:
        oversized.set()


def _stop_worker(worker: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(worker.pid, signal.SIGKILL)
        elif worker.poll() is None:
            worker.kill()
    except ProcessLookupError:
        pass
    worker.wait()


def _decode_result(output: bytes, source: JsonValue) -> dict[str, JsonValue]:
    if len(output) > MAX_WORKER_BYTES:
        raise ValueError("GitHub worker exceeded its output limit.")
    try:
        result = _RESULT.validate_json(output)
    except ValidationError as error:
        raise ValueError("GitHub worker returned an invalid result.") from error
    limitations = result.get("limitations")
    summary = result.get("summary")
    if (
        not isinstance(result.get("ok"), bool)
        or result.get("source") != source
        or not isinstance(summary, str)
        or len(summary) > 1024
        or not isinstance(limitations, list)
        or not all(isinstance(item, str) for item in limitations)
    ):
        raise ValueError("GitHub worker returned an invalid result contract.")
    return result


def github_metadata(
    repository: str, *, cancel_event: Event | None = None
) -> dict[str, JsonValue]:
    """Read public metadata without credentials, redirects, retries, or other hosts.

    The isolated worker bounds its captured body and output. Its parent owns the
    total deadline, including DNS, TLS, and response reads, and reaps it before
    returning on timeout or cancellation.
    """
    result = _RESULT.validate_python(initial_result(repository))
    result["cancelled"] = False
    if cancel_event is not None and cancel_event.is_set():
        result.update(cancelled=True, summary="GitHub request cancelled before start.")
        return result

    started = time.monotonic()
    deadline = started + REQUEST_SECONDS
    environment = {"PATH": os.defpath}
    if sys.platform == "win32" and "SystemRoot" in os.environ:
        environment["SystemRoot"] = os.environ["SystemRoot"]
    try:
        with subprocess.Popen(
            http_worker_command(_WORKER_PATH, repository),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=os.name == "posix",
        ) as worker:
            assert worker.stdout is not None
            output = bytearray()
            oversized = Event()
            reader = Thread(
                target=_capture_output,
                args=(worker.stdout, output, oversized),
                name="research-harness-github-output",
            )
            completed = False
            try:
                reader.start()
                while True:
                    if oversized.is_set():
                        result["summary"] = "GitHub worker exceeded its output limit."
                        break
                    if cancel_event is not None and cancel_event.is_set():
                        result.update(
                            cancelled=True, summary="GitHub request cancelled."
                        )
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        result["summary"] = (
                            "GitHub request exceeded its total deadline."
                        )
                        break
                    try:
                        worker.wait(timeout=min(0.1, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                    completed = True
                    break
            finally:
                _stop_worker(worker)
                if reader.ident is not None:
                    reader.join()
            if completed:
                if cancel_event is not None and cancel_event.is_set():
                    result.update(cancelled=True, summary="GitHub request cancelled.")
                elif time.monotonic() > deadline:
                    result["summary"] = "GitHub request exceeded its total deadline."
                elif worker.returncode != 0:
                    result["summary"] = "GitHub request worker failed."
                else:
                    result = _decode_result(bytes(output), result["source"])
                    result["cancelled"] = False
    except OSError as error:
        result["summary"] = f"GitHub worker failed: {type(error).__name__}."
    except ValueError as error:
        result["summary"] = str(error)[:256]
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result
