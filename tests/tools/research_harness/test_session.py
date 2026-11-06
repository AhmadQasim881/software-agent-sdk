"""Real SDK session mechanics with scripted model transport.

These tests verify disclosure, routing, and persistence, not a model's semantic
choice of whether a user request calls for the research workflow.
"""

import json
from pathlib import Path
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
from pydantic import SecretStr

from openhands.sdk import LLM
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import (
    ActionEvent,
    AgentErrorEvent,
    HookExecutionEvent,
    ObservationEvent,
)
from openhands.sdk.plugin import load_available_plugins
from openhands.sdk.tool.builtins import BUILT_IN_TOOLS, InvokeSkillObservation
from openhands.tools.research_harness.condenser import CheckpointCondenser
from openhands.tools.research_harness.definition import ResearchHarnessTool
from openhands.tools.research_harness.models import (
    HarnessError,
    NotFoundError,
    ThreadRecord,
)
from openhands.tools.research_harness.session import create_research_conversation
from openhands.tools.research_harness.store import HarnessStore
from openhands.tools.research_harness.workflow import load_workflow_skill
from tests.command_utils import touch_command


_BODY_MARKER = "Workflow version: research-review/1."
_HOOK_TYPES = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
}


@pytest.fixture
def llm() -> LLM:
    return LLM(
        model="gpt-4o",
        api_key=SecretStr("test-key"),
        usage_id="research-session-test",
        num_retries=0,
        stream=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessStore(tmp_path / "state", workspace)


@pytest.fixture
def thread(store: HarnessStore) -> ThreadRecord:
    return store.create_thread(
        "Which Python imports are present in this workspace?",
        conditions={"scope": "local Python source"},
        next_action="Record a Python import inventory.",
    )


def _response(name: str, arguments: dict[str, Any], index: int) -> ModelResponse:
    return ModelResponse(
        id=f"research-session-response-{index}",
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
                            id=f"research-session-call-{index}",
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


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(item["text"] for item in content or [] if item["type"] == "text")


def _last_tool_text(messages: list[dict[str, Any]]) -> str:
    return _text(next(item for item in reversed(messages) if item["role"] == "tool"))


@pytest.mark.parametrize("custom_condenser", [False, True])
def test_construction_validates_and_persists_without_a_model_request(
    llm: LLM,
    store: HarnessStore,
    thread: ThreadRecord,
    monkeypatch: pytest.MonkeyPatch,
    custom_condenser: bool,
) -> None:
    def unexpected_transport(**kwargs: Any) -> ModelResponse:
        pytest.fail("Constructing a research conversation must not call the model")

    monkeypatch.setattr(
        "openhands.sdk.llm.llm.litellm_completion", unexpected_transport
    )
    delegate = (
        LLMSummarizingCondenser(llm=llm, max_size=40) if custom_condenser else None
    )
    conversation = create_research_conversation(
        llm=llm,
        workspace=store.workspace,
        state_dir=store.state_dir,
        thread_id=thread.id,
        condenser=delegate,
    )
    try:
        context = conversation.agent.agent_context
        assert context is not None
        assert [skill.name for skill in context.skills] == ["research-review"]
        assert not any(
            (
                context.load_user_skills,
                context.load_public_skills,
                context.load_project_skills,
                context.load_memory,
            )
        )
        checkpoint = conversation.agent.condenser
        assert isinstance(checkpoint, CheckpointCondenser)
        assert checkpoint.thread_id == thread.id
        assert checkpoint.session_id == str(conversation.id)
        assert checkpoint.workspace == store.workspace
        assert checkpoint.state_dir == store.state_dir
        assert isinstance(checkpoint.delegate, LLMSummarizingCondenser)
        assert checkpoint.delegate.llm.usage_id == llm.usage_id
        assert checkpoint.delegate.max_size == (40 if custom_condenser else 240)
        persistence = conversation.state.persistence_dir
        assert persistence is not None
        assert Path(persistence).parent == store.state_dir / "conversations"
        assert (Path(persistence) / "base_state.json").is_file()
        assert Path(conversation.workspace.working_dir) == store.workspace
        assert store.list_runs(thread.id) == []
    finally:
        conversation.close()


def test_missing_thread_fails_before_creating_a_conversation(
    llm: LLM, store: HarnessStore
) -> None:
    with pytest.raises(NotFoundError):
        create_research_conversation(
            llm=llm,
            workspace=store.workspace,
            state_dir=store.state_dir,
            thread_id="thread_missing",
        )
    assert not (store.state_dir / "conversations").exists()


def test_operator_instructions_and_run_limits_are_persisted_without_model_calls(
    llm: LLM,
    store: HarnessStore,
    thread: ThreadRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_transport(**kwargs: Any) -> ModelResponse:
        pytest.fail("Configuration must not call a model")

    monkeypatch.setattr(
        "openhands.sdk.llm.llm.litellm_completion", unexpected_transport
    )
    instructions = "Review the owned local Python fixture. المراجعة المحلية فقط."
    conversation = create_research_conversation(
        llm=llm,
        workspace=store.workspace,
        state_dir=store.state_dir,
        thread_id=thread.id,
        operator_instructions=instructions,
        max_iteration_per_run=7,
        max_budget_per_run=0.03,
    )
    try:
        context = conversation.agent.agent_context
        assert context is not None and context.system_message_suffix == instructions
        assert (
            conversation.max_iteration_per_run == conversation.state.max_iterations == 7
        )
        assert (
            conversation.max_budget_per_run
            == conversation.state.max_budget_per_run
            == 0.03
        )
        persistence = conversation.state.persistence_dir
        assert persistence is not None
        saved = json.loads((Path(persistence) / "base_state.json").read_text())
        assert saved["agent"]["agent_context"]["system_message_suffix"] == instructions
        assert saved["max_iterations"] == 7 and saved["max_budget_per_run"] == 0.03
        metrics = conversation.conversation_stats.get_combined_metrics()
        assert metrics.response_latencies == [] and metrics.accumulated_cost == 0
        assert store.list_runs(thread.id) == []
    finally:
        conversation.close()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("operator_instructions", " \t\n"),
        ("operator_instructions", "ا" * 8193),
        ("max_iteration_per_run", 0),
        ("max_iteration_per_run", 501),
        ("max_iteration_per_run", True),
        ("max_budget_per_run", 0),
        ("max_budget_per_run", float("nan")),
        ("max_budget_per_run", float("inf")),
        ("max_budget_per_run", True),
    ],
)
def test_invalid_operator_limits_fail_before_conversation_creation(
    setting: str,
    value: Any,
    llm: LLM,
    store: HarnessStore,
    thread: ThreadRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_transport(**kwargs: Any) -> ModelResponse:
        pytest.fail("Rejected configuration must not call a model")

    monkeypatch.setattr(
        "openhands.sdk.llm.llm.litellm_completion", unexpected_transport
    )
    configuration = {setting: value}
    before = store.list_events()
    with pytest.raises(HarnessError):
        create_research_conversation(
            llm=llm,
            workspace=store.workspace,
            state_dir=store.state_dir,
            thread_id=thread.id,
            **configuration,
        )
    assert not (store.state_dir / "conversations").exists()
    assert store.list_events() == before


def test_state_must_remain_outside_the_workspace(
    llm: LLM, store: HarnessStore, thread: ThreadRecord
) -> None:
    with pytest.raises(HarnessError, match="overlap"):
        create_research_conversation(
            llm=llm,
            workspace=store.workspace,
            state_dir=store.workspace / "state",
            thread_id=thread.id,
        )


@pytest.mark.parametrize("plugin_count", [1, 10])
def test_ambient_plugin_preflight_rejects_before_hook_or_model_execution(
    llm: LLM,
    store: HarnessStore,
    thread: ThreadRecord,
    monkeypatch: pytest.MonkeyPatch,
    plugin_count: int,
) -> None:
    marker = store.state_dir / "ambient-hook-executed"
    for index in range(plugin_count):
        plugin_dir = store.workspace / ".agents" / "plugins" / f"ambient-{index:02}"
        manifest = plugin_dir / ".plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps({"name": f"ambient-{index:02}", "version": "1.0.0"}),
            encoding="utf-8",
        )
        hooks = plugin_dir / "hooks" / "hooks.json"
        hooks.parent.mkdir()
        hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": touch_command(marker),
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
    discovered = load_available_plugins(work_dir=store.workspace, include_project=True)
    assert len(discovered) == plugin_count
    assert discovered["ambient-00"].hooks is not None
    assert discovered["ambient-00"].hooks.session_start

    def unexpected_transport(**kwargs: Any) -> ModelResponse:
        pytest.fail("Ambient plugin rejection must precede any model request")

    monkeypatch.setattr(
        "openhands.sdk.llm.llm.litellm_completion", unexpected_transport
    )
    with pytest.raises(HarnessError, match="without ambient plugins") as raised:
        create_research_conversation(
            llm=llm,
            workspace=store.workspace,
            state_dir=store.state_dir,
            thread_id=thread.id,
        )
    assert "ambient-00" in str(raised.value)
    if plugin_count == 10:
        assert "and 2 more" in str(raised.value)
        assert "ambient-08" not in str(raised.value)
    assert not marker.exists()
    assert not (store.state_dir / "conversations").exists()
    assert (
        len(load_available_plugins(work_dir=store.workspace, include_project=True))
        == plugin_count
    )


def test_real_run_loads_selected_skill_records_evidence_and_resumes_thread(
    llm: LLM,
    store: HarnessStore,
    thread: ThreadRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (store.workspace / "sample.py").write_text("import json\n", encoding="utf-8")
    (store.workspace / "AGENTS.md").write_text(
        "AMBIENT_MEMORY_SHOULD_NOT_LOAD", encoding="utf-8"
    )
    ambient_skill = store.workspace / ".agents" / "skills" / "ambient" / "SKILL.md"
    ambient_skill.parent.mkdir(parents=True)
    ambient_skill.write_text(
        "---\nname: ambient\ndescription: AMBIENT_SKILL_SHOULD_NOT_LOAD\n---\n"
        "AMBIENT_SKILL_BODY_SHOULD_NOT_LOAD\n",
        encoding="utf-8",
    )
    skill = load_workflow_skill()
    requests: list[list[dict[str, Any]]] = []
    inventory_result: dict[str, Any] = {}
    assessment = ""

    def transport(messages: list[dict[str, Any]], **kwargs: Any) -> ModelResponse:
        nonlocal inventory_result, assessment
        requests.append(messages)
        assert messages[0]["role"] == "system"
        prompt = "\n".join(_text(message) for message in messages)
        assert "AMBIENT_MEMORY_SHOULD_NOT_LOAD" not in prompt
        assert "AMBIENT_SKILL_SHOULD_NOT_LOAD" not in prompt
        assert "AMBIENT_SKILL_BODY_SHOULD_NOT_LOAD" not in prompt
        assert {item["function"]["name"] for item in kwargs["tools"]} == {
            ResearchHarnessTool.name,
            "invoke_skill",
            *(tool.name for tool in BUILT_IN_TOOLS),
        }
        index = len(requests)
        if index == 1:
            assert "<available_skills>" in _text(messages[0])
            assert skill.name in _text(messages[0])
            assert "Resume a local research thread" in _text(messages[0])
            assert _BODY_MARKER not in prompt
            assert thread.id in prompt and thread.question in prompt
            return _response("invoke_skill", {"name": skill.name}, index)
        if index == 2:
            assert skill.content in _last_tool_text(messages)
            assert _BODY_MARKER in _last_tool_text(messages)
            return _response(
                ResearchHarnessTool.name,
                {"operation": "inventory", "arguments": {"thread_id": thread.id}},
                index,
            )
        if index == 3:
            inventory_result = json.loads(_last_tool_text(messages))
            assert inventory_result["ok"] is True
            evidence_id = inventory_result["evidence_id"]
            assert store.get_evidence(evidence_id).thread_id == thread.id
            assessment = f"Recorded local import inventory as evidence {evidence_id}."
            return _response(
                ResearchHarnessTool.name,
                {
                    "operation": "thread.update",
                    "arguments": {
                        "thread_id": thread.id,
                        "expected_version": thread.version,
                        "assessment": assessment,
                        "next_action": "Compare with the next saved inventory.",
                    },
                },
                index,
            )
        assert index == 4
        update = json.loads(_last_tool_text(messages))
        assert update["ok"] is True
        assert update["record"]["assessment"] == assessment
        return _response(
            "finish", {"message": "Inventory and assessment saved."}, index
        )

    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", transport)
    conversation = create_research_conversation(
        llm=llm,
        workspace=store.workspace,
        state_dir=store.state_dir,
        thread_id=thread.id,
    )
    prompt = "Inspect Python imports and retain evidence. USER_PROMPT_NOT_LEDGERED"
    try:
        conversation.send_message(prompt)
        conversation.run()
        assert (
            conversation.state.execution_status == ConversationExecutionStatus.FINISHED
        )
        assert conversation.state.invoked_skills == [skill.name]
        assert len(requests) == 4
    finally:
        conversation.close()

    events = list(conversation.state.events)
    assert not any(isinstance(event, AgentErrorEvent) for event in events)
    skill_observations = [
        event.observation
        for event in events
        if isinstance(event, ObservationEvent) and event.tool_name == "invoke_skill"
    ]
    assert len(skill_observations) == 1
    assert isinstance(skill_observations[0], InvokeSkillObservation)
    assert skill.content in skill_observations[0].text
    hooks = [event for event in events if isinstance(event, HookExecutionEvent)]
    assert {event.hook_event_type for event in hooks} == _HOOK_TYPES
    assert all(event.success and not event.blocked for event in hooks)
    start = next(event for event in hooks if event.hook_event_type == "SessionStart")
    assert start.additional_context is not None
    assert thread.id in start.additional_context
    assert thread.question in start.additional_context
    assert _BODY_MARKER not in start.additional_context
    telemetry = [
        event
        for event in store.list_events(thread_id=thread.id)
        if event.kind == "hook.observed"
    ]
    telemetry_types: set[str] = set()
    for event in telemetry:
        event_type = event.payload["event_type"]
        assert isinstance(event_type, str)
        telemetry_types.add(event_type)
    assert telemetry_types == _HOOK_TYPES
    assert all(event.session_id == str(conversation.id) for event in telemetry)
    assert "USER_PROMPT_NOT_LEDGERED" not in json.dumps(
        [event.model_dump(mode="json") for event in telemetry]
    )
    run = store.list_runs(thread.id)[0]
    evidence = store.get_evidence(inventory_result["evidence_id"])
    assert run.status == "completed" and run.session_id == str(conversation.id)
    assert run.result == inventory_result
    assert evidence.run_id == run.id
    raw = json.loads(store.artifacts.read(evidence.artifact_sha))
    assert raw["entries"][0]["normalized"] == "import json"
    assert store.get_thread(thread.id).assessment == assessment
    persistence = conversation.state.persistence_dir
    assert persistence is not None
    assert (Path(persistence) / "base_state.json").is_file()
    assert any((Path(persistence) / "events").iterdir())

    resumed_requests: list[list[dict[str, Any]]] = []

    def resumed_transport(
        messages: list[dict[str, Any]], **kwargs: Any
    ) -> ModelResponse:
        resumed_requests.append(messages)
        assert messages[0]["role"] == "system"
        prompt_text = "\n".join(_text(message) for message in messages)
        assert thread.question in prompt_text
        assert assessment in prompt_text
        assert evidence.id in prompt_text and evidence.artifact_sha in prompt_text
        assert _BODY_MARKER not in prompt_text
        return _response("finish", {"message": "Saved research thread restored."}, 5)

    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", resumed_transport)
    resumed = create_research_conversation(
        llm=llm,
        workspace=store.workspace,
        state_dir=store.state_dir,
        thread_id=thread.id,
    )
    try:
        assert resumed.id != conversation.id
        resumed.send_message("Show the saved assessment for this research thread.")
        resumed.run()
        assert resumed.state.execution_status == ConversationExecutionStatus.FINISHED
        assert resumed.state.invoked_skills == []
        assert len(resumed_requests) == 1
    finally:
        resumed.close()


def test_unrelated_scripted_turn_keeps_the_skill_body_unloaded(
    llm: LLM,
    store: HarnessStore,
    thread: ThreadRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scripted finish verifies disclosure mechanics, not semantic selection."""
    requests: list[list[dict[str, Any]]] = []

    def transport(messages: list[dict[str, Any]], **kwargs: Any) -> ModelResponse:
        requests.append(messages)
        assert messages[0]["role"] == "system"
        assert "research-review" in _text(messages[0])
        assert _BODY_MARKER not in "\n".join(_text(message) for message in messages)
        return _response("finish", {"message": "Hello there."}, len(requests))

    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", transport)
    conversation = create_research_conversation(
        llm=llm,
        workspace=store.workspace,
        state_dir=store.state_dir,
        thread_id=thread.id,
    )
    try:
        conversation.send_message("Write a short greeting.")
        conversation.run()
        assert (
            conversation.state.execution_status == ConversationExecutionStatus.FINISHED
        )
        assert len(requests) == 1
        assert conversation.state.invoked_skills == []
        assert not any(
            isinstance(event, ActionEvent) and event.tool_name == "invoke_skill"
            for event in conversation.state.events
        )
        assert store.list_runs(thread.id) == []
    finally:
        conversation.close()
