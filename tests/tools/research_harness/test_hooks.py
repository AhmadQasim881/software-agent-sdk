"""Native subprocess hooks and real conversation routing without a live model."""

import io
import json
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest
from litellm import ChatCompletionMessageToolCall
from litellm.types.utils import (
    Choices,
    Function,
    Message as LiteLLMMessage,
    ModelResponse,
    Usage,
)
from pydantic import SecretStr

from openhands.sdk import LLM, Agent, LocalConversation, Tool
from openhands.sdk.conversation.state import (
    ConversationExecutionStatus,
    ConversationState,
)
from openhands.sdk.event import HookExecutionEvent
from openhands.sdk.hooks.executor import HookExecutor
from openhands.sdk.hooks.manager import HookManager
from openhands.sdk.hooks.types import HookEvent, HookEventType
from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from openhands.tools.research_harness.definition import ResearchHarnessTool
from openhands.tools.research_harness.hooks import make_hook_config, run_hook
from openhands.tools.research_harness.service import LEDGER_READ_OPERATIONS
from openhands.tools.research_harness.store import HarnessStore


@pytest.fixture
def state(tmp_path: Path) -> tuple[HarnessStore, str]:
    workspace = tmp_path / "workspace with ' quote"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "state with ' quote", workspace)
    thread = store.create_thread(
        "SESSION_CONTEXT_QUESTION", next_action="Inventory local imports"
    )
    return store, thread.id


def test_config_contains_only_native_events_and_quotes_operator_paths(state) -> None:
    store, thread_id = state
    config = make_hook_config(
        workspace=store.workspace, state_dir=store.state_dir, thread_id=thread_id
    )
    assert set(config.model_dump()) == {
        "session_start",
        "user_prompt_submit",
        "pre_tool_use",
        "post_tool_use",
        "stop",
        "session_end",
    }
    command = config.pre_tool_use[0].hooks[0].command
    assert shlex.split(command) == [
        sys.executable,
        "-I",
        "-m",
        "openhands.research",
        "--workspace",
        str(store.workspace),
        "--state",
        str(store.state_dir),
        "hook",
        "--thread",
        thread_id,
    ]


def test_hook_imports_ignore_workspace_shadow_module(state) -> None:
    store, thread_id = state
    (store.workspace / "openhands.py").write_text(
        "raise RuntimeError('Workspace code must not execute')\n"
    )
    config = make_hook_config(
        workspace=store.workspace, state_dir=store.state_dir, thread_id=thread_id
    )
    proceed, results = HookManager(
        config=config, working_dir=str(store.workspace), session_id="native-test"
    ).run_pre_tool_use(tool_name="think", tool_input={"thought": "Plan"})
    assert proceed is True
    assert results[0].exit_code == 0
    assert results[0].success is True


@pytest.mark.parametrize(
    ("tool", "operation", "extra", "allowed"),
    [
        ("finish", None, {}, True),
        ("think", None, {}, True),
        ("invoke_skill", None, {}, True),
        ("outside_tool", None, {}, False),
        ("research_harness", "schema", {}, True),
        ("research_harness", "schema", {"extra": "RAW_INPUT_SECRET"}, False),
        ("research_harness", "inventory", {}, True),
        ("research_harness", "inventory", {"workspace": "RAW_INPUT_SECRET"}, False),
        ("research_harness", "unknown", {}, False),
    ],
)
def test_native_pretool_allowlist_and_argument_validation(
    state, tool: str, operation: str | None, extra, allowed: bool
) -> None:
    store, thread_id = state
    config = make_hook_config(
        workspace=store.workspace, state_dir=store.state_dir, thread_id=thread_id
    )
    manager = HookManager(
        config=config, working_dir=str(store.workspace), session_id="native-test"
    )
    arguments = {"thread_id": thread_id, **extra} if operation == "inventory" else extra
    action = (
        {"operation": operation, "arguments": arguments}
        if operation is not None
        else {"value": "RAW_INPUT_SECRET"}
    )
    proceed, results = manager.run_pre_tool_use(tool_name=tool, tool_input=action)
    assert proceed is allowed
    assert results[0].exit_code == (0 if allowed else 2)
    assert results[0].blocked is not allowed
    assert "RAW_INPUT_SECRET" not in results[0].stdout + results[0].stderr
    audit = store.list_events()[-1]
    assert audit.kind == "hook.observed"
    assert audit.payload["decision"] == ("allow" if allowed else "deny")
    assert "RAW_INPUT_SECRET" not in audit.model_dump_json()


@pytest.mark.parametrize("operation", sorted(LEDGER_READ_OPERATIONS))
def test_ledger_read_hooks_do_not_append_to_the_ledger(
    state,
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    store, thread_id = state
    arguments = (
        {"digest": store.artifacts.put(b"saved artifact")}
        if operation == "artifact.read"
        else {"thread_id": thread_id}
    )
    before = store.list_events()
    for event_type in (HookEventType.PRE_TOOL_USE, HookEventType.POST_TOOL_USE):
        event = HookEvent(
            event_type=event_type,
            tool_name="research_harness",
            tool_input={"operation": operation, "arguments": arguments},
            tool_response={"raw": "RAW_RESPONSE_SECRET"},
            working_dir=str(store.workspace),
            session_id="native-test",
        )
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(event.model_dump_json().encode()))
        )
        assert (
            run_hook(
                workspace=store.workspace,
                state_dir=store.state_dir,
                thread_id=thread_id,
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["decision"] == "allow"
    assert store.list_events() == before


@pytest.mark.parametrize(
    "raw",
    [b"invalid JSON RAW_INPUT_SECRET", b"x" * 262_145, b'{"event_type":"PreCompact"}'],
    ids=["malformed", "oversized", "unsupported-native-event"],
)
def test_invalid_native_input_is_bounded_and_blocking(
    state, raw: bytes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    store, thread_id = state
    before = store.list_events()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    assert (
        run_hook(
            workspace=store.workspace, state_dir=store.state_dir, thread_id=thread_id
        )
        == 2
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["decision"] == "deny"
    assert output.err and "RAW_INPUT_SECRET" not in output.out + output.err
    assert store.list_events() == before


def test_damaged_state_returns_native_deny_and_exit_two(state) -> None:
    store, thread_id = state
    config = make_hook_config(
        workspace=store.workspace, state_dir=store.state_dir, thread_id=thread_id
    )
    store.database_path.write_bytes(b"not a SQLite database")
    result = HookExecutor(working_dir=str(store.workspace)).execute(
        config.pre_tool_use[0].hooks[0],
        HookEvent(
            event_type=HookEventType.PRE_TOOL_USE,
            tool_name="think",
            working_dir=str(store.workspace),
        ),
    )
    assert result.exit_code == 2
    assert result.blocked is True
    assert json.loads(result.stdout)["decision"] == "deny"
    assert "DatabaseError" in result.stderr
    assert "Traceback" not in result.stderr


class _ProbeAction(Action):
    value: str


class _ProbeObservation(Observation):
    pass


class _ProbeExecutor(ToolExecutor[_ProbeAction, _ProbeObservation]):
    calls: ClassVar[int] = 0

    def __call__(self, action: _ProbeAction, conversation=None) -> _ProbeObservation:
        type(self).calls += 1
        return _ProbeObservation.from_text("The denied probe unexpectedly executed")


class HarnessHookProbeTool(ToolDefinition[_ProbeAction, _ProbeObservation]):
    @classmethod
    def create(cls, conv_state: ConversationState | None = None) -> Sequence[Self]:
        return [
            cls(
                description="Test whether native hooks block an unapproved tool",
                action_type=_ProbeAction,
                observation_type=_ProbeObservation,
                executor=_ProbeExecutor(),
                annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
            )
        ]


register_tool(HarnessHookProbeTool.name, HarnessHookProbeTool)


def _response(name: str, arguments: dict[str, Any], index: int) -> ModelResponse:
    return ModelResponse(
        id=f"hook-response-{index}",
        model="gpt-4o",
        created=0,
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        choices=[
            Choices(
                index=0,
                finish_reason="tool_calls",
                message=LiteLLMMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ChatCompletionMessageToolCall(
                            id=f"hook-call-{index}",
                            type="function",
                            function=Function(
                                name=name, arguments=json.dumps(arguments)
                            ),
                        )
                    ],
                ),
            )
        ],
    )


def test_real_conversation_routes_native_hooks_and_preserves_ledger_eof(
    state, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, thread_id = state
    (store.workspace / "sample.py").write_text("import json\n")
    _ProbeExecutor.calls = 0
    requests: list[list[dict[str, Any]]] = []
    before_read = []

    def transport(messages: list[dict[str, Any]], **kwargs: Any) -> ModelResponse:
        nonlocal before_read
        requests.append(messages)
        index = len(requests)
        if index == 1:
            assert messages[0]["role"] == "system"
            assert "SESSION_CONTEXT_QUESTION" in json.dumps(messages[1:])
            return _response(
                HarnessHookProbeTool.name, {"value": "RAW_TOOL_SECRET"}, index
            )
        if index == 2:
            return _response(
                ResearchHarnessTool.name,
                {"operation": "inventory", "arguments": {"thread_id": thread_id}},
                index,
            )
        if index == 3:
            before_read = store.list_events()
            return _response(
                ResearchHarnessTool.name,
                {
                    "operation": "events",
                    "arguments": {"thread_id": thread_id, "limit": 100},
                },
                index,
            )
        assert index == 4
        assert store.list_events() == before_read
        return _response("finish", {"message": "Inspection recorded"}, index)

    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", transport)
    conversation = LocalConversation(
        agent=Agent(
            llm=LLM(
                model="gpt-4o",
                api_key=SecretStr("unused-test-key"),
                num_retries=0,
                stream=False,
            ),
            tools=[
                Tool(
                    name=ResearchHarnessTool.name,
                    params={"state_dir": str(store.state_dir)},
                ),
                Tool(name=HarnessHookProbeTool.name),
            ],
        ),
        workspace=str(store.workspace),
        hook_config=make_hook_config(
            workspace=store.workspace, state_dir=store.state_dir, thread_id=thread_id
        ),
        visualizer=None,
        max_iteration_per_run=6,
    )
    try:
        conversation.send_message("Inspect local imports. RAW_PROMPT_SECRET")
        conversation.run()
        assert (
            conversation.state.execution_status == ConversationExecutionStatus.FINISHED
        )
    finally:
        conversation.close()
    assert len(requests) == 4
    assert _ProbeExecutor.calls == 0
    hook_events = [
        event
        for event in conversation.state.events
        if isinstance(event, HookExecutionEvent)
    ]
    assert {event.hook_event_type for event in hook_events} == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }
    assert any(
        event.blocked and event.tool_name == HarnessHookProbeTool.name
        for event in hook_events
    )
    audit = store.list_events()
    persisted = json.dumps([event.model_dump(mode="json") for event in audit])
    assert "RAW_PROMPT_SECRET" not in persisted
    assert "RAW_TOOL_SECRET" not in persisted
    assert all(
        event.session_id == str(conversation.id)
        for event in audit
        if event.kind == "hook.observed"
    )
    assert store.list_runs(thread_id)[0].status == "completed"
