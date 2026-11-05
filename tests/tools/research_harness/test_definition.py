"""Real SDK dispatch and owned cancellation for the research harness tool."""

import asyncio
import json
import socket
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from threading import Event, Thread, current_thread, local
from typing import Any

import pytest
from litellm import ChatCompletionMessageToolCall
from litellm.types.utils import (
    Choices,
    Function,
    Message as LiteLLMMessage,
    ModelResponse,
    Usage,
)
from pydantic import JsonValue, SecretStr, ValidationError

from openhands.sdk import LLM, Agent, LocalConversation, Tool
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import ActionEvent, AgentErrorEvent, ObservationEvent
from openhands.sdk.tool import list_registered_tools
from openhands.tools.research_harness import fixture, service
from openhands.tools.research_harness.definition import (
    ResearchHarnessAction,
    ResearchHarnessExecutor,
    ResearchHarnessObservation,
    ResearchHarnessTool,
)
from openhands.tools.research_harness.models import HarnessError
from openhands.tools.research_harness.store import HarnessStore


@pytest.fixture
def conversation(tmp_path: Path) -> Iterator[LocalConversation]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = Agent(
        llm=LLM(
            model="gpt-4o",
            api_key=SecretStr("test-key"),
            usage_id="harness-sdk-test",
            num_retries=0,
            stream=False,
        ),
        tools=[
            Tool(
                name=ResearchHarnessTool.name,
                params={"state_dir": str(tmp_path / "state")},
            )
        ],
    )
    instance = LocalConversation(
        agent=agent,
        workspace=str(workspace),
        visualizer=None,
        max_iteration_per_run=5,
    )
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def tool(conversation: LocalConversation) -> ResearchHarnessTool:
    conversation.execute_tool(
        ResearchHarnessTool.name, ResearchHarnessAction(operation="thread.list")
    )
    result = conversation.agent.tools_map[ResearchHarnessTool.name]
    assert isinstance(result, ResearchHarnessTool)
    return result


@pytest.fixture
def executor(tool: ResearchHarnessTool) -> ResearchHarnessExecutor:
    result = tool.executor
    assert isinstance(result, ResearchHarnessExecutor)
    return result


@pytest.fixture
def store(executor: ResearchHarnessExecutor) -> HarnessStore:
    return executor.service.store


def _new_thread(conversation: LocalConversation) -> str:
    observation = conversation.execute_tool(
        ResearchHarnessTool.name,
        ResearchHarnessAction(
            operation="thread.create", arguments={"question": "Observe local inputs"}
        ),
    )
    assert isinstance(observation, ResearchHarnessObservation)
    assert observation.is_error is False
    record = observation.result["record"]
    assert isinstance(record, dict)
    identifier = record["id"]
    assert isinstance(identifier, str)
    return identifier


def _fixture_action(thread_id: str) -> ResearchHarnessAction:
    return ResearchHarnessAction(
        operation="fixture", arguments={"thread_id": thread_id}
    )


def _response(name: str, arguments: dict[str, Any], index: int = 1) -> ModelResponse:
    return ModelResponse(
        id=f"harness-response-{index}",
        model="gpt-4o",
        created=0,
        object="chat.completion",
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
                            id=f"harness-call-{index}",
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


def _tool_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
    message = next(
        message for message in reversed(messages) if message["role"] == "tool"
    )
    content = message["content"]
    text = (
        content
        if isinstance(content, str)
        else "".join(item["text"] for item in content if item["type"] == "text")
    )
    return json.loads(text)


def test_registered_tool_uses_fixed_workspace_and_operator_configuration(
    conversation: LocalConversation,
    tool: ResearchHarnessTool,
    store: HarnessStore,
) -> None:
    assert ResearchHarnessTool.name in list_registered_tools()
    thread_id = _new_thread(conversation)
    assert store.get_thread(thread_id).question == "Observe local inputs"
    assert store.workspace == Path(conversation.workspace.working_dir).resolve()
    assert not store.state_dir.is_relative_to(store.workspace)
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is False
    assert tool.annotations.openWorldHint is False
    assert tool.conversation_local is False

    with pytest.raises(HarnessError, match="allow_github"):
        conversation.execute_tool(
            ResearchHarnessTool.name,
            ResearchHarnessAction(
                operation="github",
                arguments={
                    "thread_id": thread_id,
                    "repository": "OpenHands/software-agent-sdk",
                },
            ),
        )
    with pytest.raises(HarnessError, match="overlap"):
        ResearchHarnessTool.create(
            conversation.state, state_dir=str(store.workspace / "state")
        )
    assert store.list_runs(thread_id) == []


def test_schema_discovery_uses_the_actual_service_contract(
    conversation: LocalConversation,
    tool: ResearchHarnessTool,
    store: HarnessStore,
) -> None:
    before = store.list_events()
    observation = conversation.execute_tool(
        ResearchHarnessTool.name, ResearchHarnessAction(operation="schema")
    )
    assert isinstance(observation, ResearchHarnessObservation)
    assert observation.result == {
        "ok": True,
        "operations": service.HarnessService.schema(),
    }
    assert observation.is_error is False
    assert store.list_events() == before
    assert "schema" in tool.description
    assert len(tool.description) < 2000
    with pytest.raises(HarnessError, match="empty arguments"):
        tool(ResearchHarnessAction(operation="schema", arguments={"unexpected": True}))


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("unknown", {}),
        ("thread.list", {"limit": True}),
        ("thread.list", {"workspace": "/different-workspace"}),
    ],
)
def test_invalid_requests_raise_without_recording_an_observed_operation(
    conversation: LocalConversation,
    store: HarnessStore,
    operation: str,
    arguments: dict[str, JsonValue],
) -> None:
    before = store.list_events()
    with pytest.raises(HarnessError):
        conversation.execute_tool(
            ResearchHarnessTool.name,
            ResearchHarnessAction(operation=operation, arguments=arguments),
        )
    assert store.list_events() == before
    with pytest.raises(ValidationError):
        ResearchHarnessAction.model_validate(
            {"operation": "thread.list", "arguments": {}, "allow_github": True}
        )


def test_actual_observation_failure_sets_sdk_error_flag(
    conversation: LocalConversation,
    store: HarnessStore,
) -> None:
    (store.workspace / "broken.py").write_text("def broken(\n", encoding="utf-8")
    thread_id = _new_thread(conversation)
    observation = conversation.execute_tool(
        ResearchHarnessTool.name,
        ResearchHarnessAction(
            operation="inventory", arguments={"thread_id": thread_id}
        ),
    )
    assert isinstance(observation, ResearchHarnessObservation)
    assert observation.is_error is True
    assert observation.result["ok"] is False
    assert json.loads(observation.text) == observation.result
    assert store.list_runs(thread_id)[0].status == "failed"
    assert len(store.list_evidence(thread_id)) == 1


@pytest.mark.parametrize("asynchronous", [False, True], ids=["run", "arun"])
@pytest.mark.asyncio
async def test_real_conversation_dispatch_persists_evidence_and_returns_it_to_model(
    conversation: LocalConversation,
    store: HarnessStore,
    monkeypatch: pytest.MonkeyPatch,
    asynchronous: bool,
) -> None:
    (store.workspace / "sample.py").write_text("import json\n", encoding="utf-8")
    requests: list[list[dict[str, Any]]] = []
    observed_results: list[dict[str, Any]] = []

    def transport(messages: list[dict[str, Any]], **kwargs: Any) -> ModelResponse:
        requests.append(messages)
        assert messages[0]["role"] == "system"
        assert ResearchHarnessTool.name in {
            item["function"]["name"] for item in kwargs["tools"]
        }
        index = len(requests)
        if index == 1:
            return _response(
                ResearchHarnessTool.name,
                {"operation": "thread.create", "arguments": {"question": "Imports?"}},
                index,
            )
        observed = _tool_result(messages)
        observed_results.append(observed)
        if index == 2:
            return _response(
                ResearchHarnessTool.name,
                {
                    "operation": "inventory",
                    "arguments": {"thread_id": observed["record"]["id"]},
                },
                index,
            )
        assert index == 3
        assert observed["ok"] is True and observed["evidence_id"]
        return _response("finish", {"message": "Inventory evidence recorded."}, index)

    async def async_transport(
        messages: list[dict[str, Any]], **kwargs: Any
    ) -> ModelResponse:
        return transport(messages, **kwargs)

    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", transport)
    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_acompletion", async_transport)
    conversation.send_message("Inspect the local Python imports and retain evidence.")
    if asynchronous:
        await conversation.arun()
    else:
        conversation.run()

    assert len(requests) == 3
    assert conversation.state.execution_status == ConversationExecutionStatus.FINISHED
    events = list(conversation.state.events)
    actions = [
        event
        for event in events
        if isinstance(event, ActionEvent)
        and event.tool_name == ResearchHarnessTool.name
    ]
    observations = [
        event
        for event in events
        if isinstance(event, ObservationEvent)
        and event.tool_name == ResearchHarnessTool.name
    ]
    assert len(actions) == len(observations) == 2
    assert not any(isinstance(event, AgentErrorEvent) for event in events)
    assert all(isinstance(event.action, ResearchHarnessAction) for event in actions)
    thread_id = observed_results[0]["record"]["id"]
    run = store.list_runs(thread_id)[0]
    evidence = store.list_evidence(thread_id)[0]
    assert run.status == "completed" and run.session_id == str(conversation.id)
    assert run.result == observed_results[1]
    assert run.result["evidence_id"] == evidence.id
    raw = json.loads(store.artifacts.read(evidence.artifact_sha))
    assert raw["entries"][0]["normalized"] == "import json"
    assert all(
        event.session_id == str(conversation.id)
        for event in store.list_events(run_id=run.id)
    )


@dataclass
class _HeldFixture:
    cancel: Event
    worker: Thread
    release: Event = field(default_factory=Event)
    port: int | None = None
    result: dict[str, JsonValue] | None = None


@pytest.fixture
def held_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    tool: ResearchHarnessTool,
) -> Iterator[Queue[_HeldFixture]]:
    """Hold an actual fixture read; validation, persistence and cleanup stay real."""
    waiting: Queue[_HeldFixture] = Queue()
    calls: list[_HeldFixture] = []
    current = local()
    validate = service.validate_fixture
    read_item = fixture._read_item

    def tracked_fixture(*, cancel_event: Event | None = None) -> dict[str, JsonValue]:
        assert cancel_event is not None
        call = _HeldFixture(cancel_event, current_thread())
        calls.append(call)
        current.call = call
        call.result = validate(cancel_event=cancel_event)
        return call.result

    def held_read(port: int) -> tuple[int, bytes]:
        call: _HeldFixture = current.call
        call.port = port
        waiting.put(call)
        assert call.release.wait(5), "Test did not release the owned fixture read"
        return read_item(port)

    monkeypatch.setattr(service, "validate_fixture", tracked_fixture)
    monkeypatch.setattr(fixture, "_read_item", held_read)
    try:
        yield waiting
    finally:
        for call in calls:
            call.release.set()


def _assert_fixture_stopped(call: _HeldFixture) -> None:
    assert not call.worker.is_alive()
    assert call.result is not None
    cleanup = call.result["cleanup"]
    assert isinstance(cleanup, dict) and cleanup["completed"] is True
    assert call.port is not None
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", call.port), timeout=0.1):
            pass


@pytest.mark.parametrize("close", [False, True], ids=["interrupt", "close"])
def test_interrupt_signals_all_calls_before_draining_owned_workers(
    conversation: LocalConversation,
    tool: ResearchHarnessTool,
    executor: ResearchHarnessExecutor,
    store: HarnessStore,
    held_fixtures: Queue[_HeldFixture],
    close: bool,
) -> None:
    thread_id = _new_thread(conversation)
    with ThreadPoolExecutor(max_workers=3) as callers:
        results = [
            callers.submit(tool, _fixture_action(thread_id), conversation)
            for _ in range(2)
        ]
        held = [held_fixtures.get(timeout=2) for _ in range(2)]
        stop = callers.submit(executor.close if close else executor.interrupt)
        try:
            assert all(call.cancel.wait(2) for call in held)
            assert not stop.done()
            assert all(call.worker.is_alive() for call in held)
        finally:
            for call in held:
                call.release.set()
        stop.result(timeout=3)
        for result in results:
            assert result.result(timeout=3).is_error is True
    for call in held:
        _assert_fixture_stopped(call)
    assert [run.status for run in store.list_runs(thread_id)] == [
        "interrupted",
        "interrupted",
    ]
    next_action = ResearchHarnessAction(
        operation="inventory", arguments={"thread_id": thread_id}
    )
    if close:
        with pytest.raises(HarnessError, match="closed"):
            tool(next_action, conversation)
    else:
        assert tool(next_action, conversation).is_error is False


def test_interrupt_from_owned_worker_does_not_join_itself(
    conversation: LocalConversation,
    executor: ResearchHarnessExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = _new_thread(conversation)
    validate = service.validate_fixture
    returned = Event()

    def interrupt_here(*, cancel_event: Event | None = None) -> dict[str, JsonValue]:
        executor.interrupt()
        returned.set()
        return validate(cancel_event=cancel_event)

    monkeypatch.setattr(service, "validate_fixture", interrupt_here)
    observation = executor(_fixture_action(thread_id), conversation)
    assert returned.is_set()
    assert observation.result["status"] == "interrupted"


def _script_fixture_call(monkeypatch: pytest.MonkeyPatch, thread_id: str) -> None:
    responses = iter(
        [
            _response(
                ResearchHarnessTool.name,
                {"operation": "fixture", "arguments": {"thread_id": thread_id}},
            ),
            _response("finish", {"message": "Done"}, 2),
        ]
    )

    def transport(**kwargs: Any) -> ModelResponse:
        return next(responses)

    async def async_transport(**kwargs: Any) -> ModelResponse:
        return next(responses)

    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", transport)
    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_acompletion", async_transport)


def test_sync_conversation_interrupt_bridges_its_token_to_the_running_operation(
    conversation: LocalConversation,
    store: HarnessStore,
    held_fixtures: Queue[_HeldFixture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = _new_thread(conversation)
    _script_fixture_call(monkeypatch, thread_id)
    conversation.send_message("Validate the owned fixture")
    with ThreadPoolExecutor(max_workers=2) as callers:
        running = callers.submit(conversation.run)
        held = held_fixtures.get(timeout=2)
        stopping = callers.submit(conversation.interrupt)
        try:
            assert held.cancel.wait(2)
            assert not running.done()
        finally:
            held.release.set()
        running.result(timeout=3)
        stopping.result(timeout=3)
    _assert_fixture_stopped(held)
    assert store.list_runs(thread_id)[0].status == "interrupted"
    observations = [
        event.observation
        for event in conversation.state.events
        if isinstance(event, ObservationEvent)
        and event.tool_name == ResearchHarnessTool.name
    ]
    assert len(observations) == 1 and observations[0].is_error is True


@pytest.mark.asyncio
async def test_async_conversation_interrupt_drains_fixture_before_returning(
    conversation: LocalConversation,
    store: HarnessStore,
    held_fixtures: Queue[_HeldFixture],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_id = _new_thread(conversation)
    _script_fixture_call(monkeypatch, thread_id)
    conversation.send_message("Validate the owned fixture")
    running = asyncio.create_task(conversation.arun())
    held = await asyncio.to_thread(held_fixtures.get, True, 2)

    def release_after_cancellation() -> None:
        assert held.cancel.wait(2)
        held.release.set()

    with ThreadPoolExecutor(max_workers=1) as releaser:
        released = releaser.submit(release_after_cancellation)
        try:
            conversation.interrupt()
            await asyncio.wait_for(running, timeout=3)
        finally:
            held.release.set()
        released.result(timeout=3)
    _assert_fixture_stopped(held)
    assert store.list_runs(thread_id)[0].status == "interrupted"
    assert conversation.state.execution_status == ConversationExecutionStatus.PAUSED


@pytest.mark.asyncio
async def test_direct_async_cancellation_is_per_call_and_drains_repeated_cancellation(
    conversation: LocalConversation,
    tool: ResearchHarnessTool,
    store: HarnessStore,
    held_fixtures: Queue[_HeldFixture],
) -> None:
    thread_ids = [_new_thread(conversation) for _ in range(2)]
    first = asyncio.create_task(
        tool.acall(_fixture_action(thread_ids[0]), conversation)
    )
    second = asyncio.create_task(
        tool.acall(_fixture_action(thread_ids[1]), conversation)
    )
    held = [await asyncio.to_thread(held_fixtures.get, True, 2) for _ in range(2)]
    try:
        first.cancel()
        for _ in range(100):
            if any(call.cancel.is_set() for call in held):
                break
            await asyncio.sleep(0.01)
        cancelled = [call for call in held if call.cancel.is_set()]
        assert len(cancelled) == 1
        assert not first.done() and not second.done()
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        cancelled[0].release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=3)
        _assert_fixture_stopped(cancelled[0])
        other = next(call for call in held if call is not cancelled[0])
        assert not other.cancel.is_set()
        other.release.set()
        assert (await asyncio.wait_for(second, timeout=3)).is_error is False
        _assert_fixture_stopped(other)
        assert store.list_runs(thread_ids[0])[0].status == "interrupted"
        assert store.list_runs(thread_ids[1])[0].status == "completed"
    finally:
        for call in held:
            call.release.set()
        await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_async_call_keeps_sdk_secret_masking(
    conversation: LocalConversation,
    tool: ResearchHarnessTool,
) -> None:
    secret = "harness-output-mask-test-value"
    conversation.update_secrets({"HARNESS_TEST_VALUE": secret})
    observation = await tool.acall(
        ResearchHarnessAction(
            operation="thread.create", arguments={"question": secret}
        ),
        conversation,
    )
    assert isinstance(observation, ResearchHarnessObservation)
    assert secret not in observation.model_dump_json()


def test_worker_start_failure_releases_registration(
    conversation: LocalConversation,
    executor: ResearchHarnessExecutor,
    store: HarnessStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_start(worker: Thread) -> None:
        raise RuntimeError("Controlled thread start failure")

    before = store.list_events()
    with monkeypatch.context() as patches:
        patches.setattr(Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="thread start failure"):
            executor(ResearchHarnessAction(operation="thread.list"), conversation)
    assert store.list_events() == before
    executor.interrupt()
    assert (
        executor(ResearchHarnessAction(operation="thread.list"), conversation).is_error
        is False
    )
