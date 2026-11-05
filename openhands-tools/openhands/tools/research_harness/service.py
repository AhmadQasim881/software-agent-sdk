"""One validated operation surface shared by human and agent interfaces."""

import logging
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from threading import Event
from typing import Any

from pydantic import JsonValue

from .artifacts import canonical_json
from .commands import command_schemas, validate_arguments
from .context import assemble_context
from .core import ResearchHarness
from .fixture import validate_fixture
from .inventory import inventory_python
from .models import HarnessError, Record
from .network import github_metadata
from .snapshots import compare_inventories, query_inventory
from .views import artifact_slice, export_events


_LOGGER = logging.getLogger(__name__)
LEDGER_READ_OPERATIONS = frozenset({"events", "events.export", "artifact.read"})


class HarnessService:
    """Operator-owned workspace and configuration; requests cannot replace them."""

    def __init__(
        self, *, workspace: Path, state_dir: Path, allow_github: bool = False
    ) -> None:
        self.harness = ResearchHarness(workspace=workspace, state_dir=state_dir)
        self.store = self.harness.store
        self.allow_github = allow_github

    @staticmethod
    def schema() -> dict[str, JsonValue]:
        return command_schemas()

    def execute(
        self,
        operation: str,
        arguments: dict[str, JsonValue],
        *,
        session_id: str | None = None,
        cancel_event: Event | None = None,
    ) -> dict[str, JsonValue]:
        command = validate_arguments(operation, arguments)
        if operation == "github" and not self.allow_github:
            raise HarnessError(
                "GitHub access requires the operator's allow_github option"
            )
        if cancel_event is not None and cancel_event.is_set():
            return {
                "ok": False,
                "status": "interrupted",
                "summary": "Cancelled before dispatch; no operation executed",
            }
        # Inspection must not recursively add to the ledger being paginated.
        if operation in LEDGER_READ_OPERATIONS:
            return self._bounded(
                self._execute(
                    operation,
                    command.model_dump(exclude_none=True),
                    session_id=session_id,
                    cancel_event=cancel_event,
                )
            )
        digest = self.store.artifacts.put(canonical_json(arguments))
        call = self.store.append_event(
            "interface.called",
            session_id=session_id,
            payload={"operation": operation, "arguments_sha": digest},
        )
        try:
            result = self._execute(
                operation,
                command.model_dump(exclude_none=True),
                session_id=session_id,
                cancel_event=cancel_event,
            )
            result = self._bounded(result)
        except BaseException as error:
            try:
                self.store.append_event(
                    "interface.failed",
                    session_id=session_id,
                    payload={
                        "operation": operation,
                        "error_type": type(error).__name__,
                        "call_seq": call.seq,
                    },
                )
            except BaseException:
                _LOGGER.warning("Could not record interface failure for %s", operation)
            raise
        self.store.append_event(
            "interface.finished",
            session_id=session_id,
            payload={
                "operation": operation,
                "ok": result.get("ok") is True,
                "call_seq": call.seq,
                "run_id": result.get("run_id"),
            },
        )
        return result

    def _bounded(self, result: dict[str, JsonValue]) -> dict[str, JsonValue]:
        encoded = canonical_json(result)
        if len(encoded) <= 65_536:
            return result
        digest = self.store.artifacts.put(encoded)
        return {
            "ok": result.get("ok") is True,
            "output_artifact_sha": digest,
            "output_bytes": len(encoded),
            "detail": "Complete response saved; use artifact.read to retrieve it",
            "next_offset": result.get("next_offset"),
            "next_after_seq": result.get("next_after_seq"),
            "has_more": result.get("has_more"),
        }

    def _execute(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        session_id: str | None,
        cancel_event: Event | None,
    ) -> dict[str, JsonValue]:
        single: dict[str, Callable[..., Record]] = {
            "thread.create": self.store.create_thread,
            "thread.get": self.store.get_thread,
            "thread.update": self.store.update_thread,
            "run.get": self.store.get_run,
            "evidence.get": self.store.get_evidence,
        }
        if operation in single:
            record = single[operation](**arguments)
            return {"ok": True, "record": record.model_dump(mode="json")}
        pages: dict[str, Callable[..., Sequence[Record]]] = {
            "thread.list": self.store.list_threads,
            "thread.history": self.store.thread_history,
            "run.list": self.store.list_runs,
            "evidence.list": self.store.list_evidence,
        }
        if operation in pages or operation == "events":
            limit = arguments.pop("limit")
            offset = arguments.pop("offset", 0)
            records: Sequence[Record] = (
                self.store.list_events(**arguments, limit=limit + 1)
                if operation == "events"
                else pages[operation](**arguments, limit=limit + 1, offset=offset)
            )
            selected: list[JsonValue] = []
            size = 0
            for record in records[:limit]:
                value = record.model_dump(mode="json")
                added = len(canonical_json(value)) + 1
                if size + added > 524_288:
                    break
                selected.append(value)
                size += added
            result: dict[str, JsonValue] = {
                "ok": True,
                "records": selected,
                "offset": offset,
                "next_offset": offset + len(selected),
                "has_more": len(records) > len(selected),
            }
            if operation == "events":
                result["next_after_seq"] = (
                    records[len(selected) - 1].model_dump()["seq"]
                    if selected
                    else arguments.get("after_seq", 0)
                )
            return result
        if operation == "context":
            return {"ok": True, **assemble_context(self.store, **arguments)}
        if operation == "artifact.read":
            return artifact_slice(self.store, **arguments)
        if operation == "events.export":
            return export_events(self.store, **arguments)
        thread_id = arguments.pop("thread_id")
        work: Callable[[], dict[str, JsonValue]]
        if operation == "inventory":
            work = partial(
                inventory_python,
                self.store.workspace,
                cancel_event=cancel_event,
                **arguments,
            )
        elif operation == "github":
            work = partial(github_metadata, cancel_event=cancel_event, **arguments)
        elif operation == "fixture":
            work = partial(validate_fixture, cancel_event=cancel_event)
        elif operation == "compare":
            work = partial(compare_inventories, self.store.artifacts, **arguments)
        elif operation == "query":
            work = partial(query_inventory, self.store.artifacts, **arguments)
        else:
            raise HarnessError(f"Unsupported operation: {operation}")
        return self.harness.observe(
            thread_id,
            operation,
            work,
            inputs=arguments,
            session_id=session_id,
            cancel_event=cancel_event,
        )
