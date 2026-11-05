"""Record real operation outcomes independently of any model or agent host."""

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import JsonValue

from .artifacts import canonical_json
from .models import HarnessError, RunRecord, TerminalStatus, ThreadRecord
from .store import HarnessStore


_LOGGER = logging.getLogger(__name__)


def _observation_fields(
    value: dict[str, JsonValue],
) -> tuple[bool, str, str, list[str]]:
    ok = value.get("ok")
    source = value.get("source")
    summary = value.get("summary")
    limitations = value.get("limitations", [])
    if not isinstance(ok, bool):
        raise HarnessError("An operation must report an explicit boolean outcome")
    if not isinstance(source, str) or not source.strip():
        raise HarnessError("An observation requires its source")
    if not isinstance(summary, str) or not summary.strip():
        raise HarnessError("An observation requires a summary")
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) for item in limitations
    ):
        raise HarnessError("Observation limitations must be a list of strings")
    return ok, source, summary, [str(item) for item in limitations]


class ResearchHarness:
    """Shared operation boundary for the CLI, SDK tool, and MCP adapters."""

    def __init__(self, *, workspace: Path, state_dir: Path) -> None:
        self.store = HarnessStore(state_dir, workspace)

    def observe(
        self,
        thread_id: str,
        operation: str,
        work: Callable[[], dict[str, JsonValue]],
        *,
        inputs: dict[str, JsonValue] | None = None,
        session_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, JsonValue]:
        thread = self.store.get_thread(thread_id)
        run = self.store.start_run(
            thread_id, operation, inputs=inputs, session_id=session_id
        )
        try:
            return self._observe_run(thread, run, work, cancel_event)
        except BaseException as error:
            # Cleanup spans callback, artifact publication, evidence, and commit.
            # A commit can succeed immediately before an interruption is raised.
            try:
                if self.store.get_run(run.id).status == "running":
                    status = "failed" if isinstance(error, Exception) else "interrupted"
                    self.store.finish_run(run.id, status, error=type(error).__name__)
            except BaseException:
                _LOGGER.warning("Could not finalize run %s during cleanup", run.id)
            raise

    def _observe_run(
        self,
        thread: ThreadRecord,
        run: RunRecord,
        work: Callable[[], dict[str, JsonValue]],
        cancel_event: threading.Event | None,
    ) -> dict[str, JsonValue]:
        started = time.monotonic()
        try:
            if cancel_event is not None and cancel_event.is_set():
                observed: dict[str, JsonValue] = {
                    "ok": False,
                    "cancelled": True,
                    "source": f"operation:{run.operation}",
                    "summary": "Cancelled before execution",
                    "limitations": ["The requested operation did not execute"],
                }
            else:
                observed = work()
            _observation_fields(observed)
        except Exception as error:
            observed = {
                "ok": False,
                "source": f"operation:{run.operation}",
                "summary": f"{type(error).__name__}: {str(error)[:2000]}",
                "error_type": type(error).__name__,
                "limitations": [
                    "The operation failed; no successful result is implied"
                ],
            }

        ok, source, summary, limitations = _observation_fields(observed)
        cancelled = observed.get("cancelled") is True or (
            cancel_event is not None and cancel_event.is_set()
        )
        status: TerminalStatus = (
            "interrupted" if cancelled else "completed" if ok else "failed"
        )
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        try:
            digest = self.store.artifacts.put(canonical_json(observed))
            evidence = self.store.record_evidence(
                run.id,
                source=source,
                summary=summary,
                artifact_sha=digest,
                conditions={
                    "workspace": str(self.store.workspace),
                    "thread_version": thread.version,
                    "thread_conditions": thread.conditions,
                },
                limitations=limitations,
            )
        except Exception as error:
            self.store.finish_run(
                run.id,
                "failed",
                error=f"Evidence persistence failed: {str(error)[:2000]}",
            )
            raise

        result: dict[str, JsonValue] = {
            "ok": status == "completed",
            "status": status,
            "run_id": run.id,
            "evidence_id": evidence.id,
            "artifact_sha": digest,
            "summary": summary[:512],
            "summary_truncated": len(summary) > 512,
            "limitations": [item[:128] for item in limitations[:8]],
            "limitations_omitted": max(0, len(limitations) - 8),
            "limitations_truncated": any(len(item) > 128 for item in limitations[:8]),
            "duration_ms": duration_ms,
            "detail": "Retrieve the referenced evidence for the complete observation",
        }
        error = (
            None
            if status == "completed"
            else ("Operation interrupted" if cancelled else summary[:4096])
        )
        self.store.finish_run(run.id, status, result=result, error=error)
        return result
