"""Native OpenHands lifecycle hooks for context, policy, and minimal audit data."""

import json
import re
import shlex
import sqlite3
import sys
from pathlib import Path

from pydantic import JsonValue, TypeAdapter, ValidationError

from openhands.research.runtime import harness_command
from openhands.sdk.hooks.config import HookConfig, HookDefinition, HookMatcher
from openhands.sdk.hooks.types import HookEvent, HookEventType

from .commands import OPERATION_NAMES, validate_arguments
from .context import assemble_context
from .models import HarnessError
from .service import LEDGER_READ_OPERATIONS
from .store import HarnessStore


_MAX_INPUT_BYTES = 262_144
_ALLOWED_TOOLS = frozenset({"research_harness", "finish", "think", "invoke_skill"})
_ACTION_FIELDS = frozenset(
    {"operation", "arguments", "kind", "security_risk", "summary"}
)
_ARGUMENTS = TypeAdapter(dict[str, JsonValue])
_TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse"})


def make_hook_config(*, workspace: Path, state_dir: Path, thread_id: str) -> HookConfig:
    """Build six native synchronous hooks from trusted operator configuration."""
    if not thread_id.strip() or len(thread_id) > 128:
        raise HarnessError("A hook requires a nonblank thread ID of at most 128 chars")
    command = shlex.join(
        [
            *harness_command(),
            "--workspace",
            str(workspace.resolve(strict=True)),
            "--state",
            str(state_dir.resolve()),
            "hook",
            "--thread",
            thread_id,
        ]
    )
    return HookConfig(
        **{
            field: [
                HookMatcher(
                    hooks=[
                        HookDefinition(
                            name="research-harness", command=command, timeout=30
                        )
                    ]
                )
            ]
            for field in (
                "session_start",
                "user_prompt_submit",
                "pre_tool_use",
                "post_tool_use",
                "stop",
                "session_end",
            )
        }
    )


def _validate_tool(event: HookEvent) -> None:
    if event.tool_name not in _ALLOWED_TOOLS:
        raise HarnessError("Tool is outside the configured research harness tool set")
    if event.tool_name != "research_harness":
        return
    action = event.tool_input or {}
    if set(action) - _ACTION_FIELDS:
        raise HarnessError("Unexpected research harness action fields")
    operation = action.get("operation")
    if not isinstance(operation, str):
        raise HarnessError("Research harness operation must be a string")
    arguments = _ARGUMENTS.validate_python(action.get("arguments", {}), strict=True)
    if operation == "schema":
        if arguments:
            raise HarnessError("The schema operation requires empty arguments")
    else:
        validate_arguments(operation, arguments)


def _handle_event(
    event: HookEvent, *, workspace: Path, state_dir: Path, thread_id: str
) -> tuple[dict[str, JsonValue], int]:
    store = HarnessStore(state_dir, workspace)
    store.get_thread(thread_id)
    if event.working_dir is not None and (
        Path(event.working_dir).resolve(strict=True) != store.workspace
    ):
        raise HarnessError("Hook workspace does not match the configured workspace")
    if event.session_id is not None and len(event.session_id) > 128:
        raise HarnessError("Hook session ID exceeds 128 characters")
    output: dict[str, JsonValue] = {"decision": "allow"}
    if event.event_type == HookEventType.PRE_TOOL_USE:
        try:
            _validate_tool(event)
        except (HarnessError, ValidationError) as error:
            output = {"decision": "deny", "reason": _failure_reason(error)}

    operation = (event.tool_input or {}).get("operation")
    if event.tool_name != "research_harness" or operation not in (
        *OPERATION_NAMES,
        "schema",
    ):
        operation = None
    tool_name = event.tool_name
    if tool_name is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tool_name):
        tool_name = "<invalid>"
    # Reads of the ledger and its exported pages must not append new ledger
    # entries themselves, or a client paging until EOF could never finish.
    ledger_read = (
        event.event_type in _TOOL_EVENTS
        and event.tool_name == "research_harness"
        and operation in LEDGER_READ_OPERATIONS
        and output["decision"] == "allow"
    )
    if not ledger_read:
        store.append_event(
            "hook.observed",
            thread_id=thread_id,
            session_id=event.session_id,
            payload={
                "event_type": event.event_type,
                "tool_name": tool_name,
                "operation": operation,
                "decision": output["decision"],
            },
        )
    if event.event_type == HookEventType.SESSION_START:
        context = assemble_context(store, thread_id)
        output["additionalContext"] = context["context"]
    return output, 2 if output["decision"] == "deny" else 0


def _failure_reason(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "Invalid native hook event or research arguments"
    if isinstance(error, HarnessError):
        return str(error)[:512]
    return (
        "Research hook could not read its input or configured state "
        f"({type(error).__name__})"
    )


def run_hook(*, workspace: Path, state_dir: Path, thread_id: str) -> int:
    """Consume <=256 KiB of native event JSON; return native output and exit 0/2.

    Errors deliberately use blocking exit code 2. Native SDK lifecycle hooks
    such as SessionStart remain advisory; the service and restricted tool set
    independently enforce operation and workspace limits. The harness ledger
    excludes raw prompts, arguments, responses, and arbitrary metadata. The
    SDK's own conversation history retains its normal hook/input records.
    """
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            raise HarnessError("Native hook input exceeds 262144 bytes")
        event = HookEvent.model_validate_json(raw)
        output, status = _handle_event(
            event, workspace=workspace, state_dir=state_dir, thread_id=thread_id
        )
    except (ValueError, OSError, sqlite3.Error) as error:
        output = {"decision": "deny", "reason": _failure_reason(error)}
        status = 2
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    if status:
        print(output["reason"], file=sys.stderr)
    return status
