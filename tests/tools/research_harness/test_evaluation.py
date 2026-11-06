"""Evaluator mechanics through real SDK sessions with scripted model transport.

These tests deliberately control model decisions. They are not live-model
evidence that a model naturally selects the relevant skill.
"""

import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
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
from pydantic import JsonValue, SecretStr

from openhands.sdk import LLM, LocalConversation
from openhands.tools.research_harness import evaluation
from openhands.tools.research_harness.models import HarnessError
from openhands.tools.research_harness.store import HarnessStore


def _llm(name: str) -> LLM:
    return LLM(
        model="gpt-4o",
        usage_id=f"evaluation-test-{name}",
        api_key=SecretStr("evaluation-test-key"),
        num_retries=0,
        stream=False,
        input_cost_per_token=0.0001,
        output_cost_per_token=0.0001,
    )


def _response(name: str, arguments: dict[str, str], index: int) -> ModelResponse:
    return ModelResponse(
        id=f"evaluation-response-{index}",
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
                            id=f"evaluation-call-{index}",
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


@pytest.fixture(autouse=True)
def offline_evaluation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        evaluation, "TemporaryDirectory", partial(TemporaryDirectory, dir=tmp_path)
    )

    def unexpected_transport(**kwargs: Any) -> ModelResponse:
        pytest.fail("A test must install scripted transport before a model call")

    monkeypatch.setattr(
        "openhands.sdk.llm.llm.litellm_completion", unexpected_transport
    )


@pytest.mark.parametrize(
    ("relevant_invokes", "unrelated_invokes", "expected_ok"),
    [(True, False, True), (False, False, False), (True, True, False)],
)
def test_classifies_observed_skill_use_in_independent_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relevant_invokes: bool,
    unrelated_invokes: bool,
    expected_ok: bool,
) -> None:
    active = ""
    created: list[LLM] = []
    calls = {"relevant": 0, "unrelated": 0}
    paths: list[Path] = []
    closed: list[LocalConversation] = []
    original_create = evaluation.create_research_conversation
    original_close = LocalConversation.close

    def factory(name: str) -> LLM:
        nonlocal active
        active = name
        llm = _llm(name)
        created.append(llm)
        return llm

    def tracked_create(**kwargs: Any) -> LocalConversation:
        assert kwargs["max_iteration_per_run"] == 4
        assert kwargs["max_budget_per_run"] == 0.05
        assert kwargs["allow_github"] is False
        workspace = kwargs["workspace"]
        paths.append(workspace)
        store = HarnessStore(kwargs["state_dir"], workspace)
        runs = store.list_runs(kwargs["thread_id"])
        assert len(runs) == 1
        assert runs[0].operation == "inventory" and runs[0].status == "completed"
        assert len(store.list_evidence(kwargs["thread_id"])) == 1
        assert sorted(p.name for p in workspace.iterdir()) == ["example.py"]
        return original_create(**kwargs)

    def tracked_close(conversation: LocalConversation) -> None:
        original_close(conversation)
        # GC can finalize conversations from earlier parameterized cases here.
        if Path(conversation.workspace.working_dir).is_relative_to(tmp_path):
            closed.append(conversation)

    def transport(**kwargs: Any) -> ModelResponse:
        calls[active] += 1
        invokes = relevant_invokes if active == "relevant" else unrelated_invokes
        if calls[active] == 1 and invokes:
            return _response(
                "invoke_skill", {"name": "research-review"}, sum(calls.values())
            )
        return _response("finish", {"message": "Done."}, sum(calls.values()))

    monkeypatch.setattr(evaluation, "create_research_conversation", tracked_create)
    monkeypatch.setattr(LocalConversation, "close", tracked_close)
    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", transport)
    result = evaluation.evaluate_skill_selection(
        factory, max_budget_usd=0.1, max_iterations=4
    )
    assert result["ok"] is expected_ok
    assert result["status"] == ("passed" if expected_ok else "failed")
    cases = result["cases"]
    assert isinstance(cases, list) and len(cases) == 2
    for case, invokes in zip(cases, (relevant_invokes, unrelated_invokes), strict=True):
        assert isinstance(case, dict)
        assert case["execution_status"] == "finished"
        assert case["invoked_skills"] == (["research-review"] if invokes else [])
        assert case["actions"] == (
            ["invoke_skill", "finish"] if invokes else ["finish"]
        )
        assert case["cleanup_ok"] is True
        assert "research-review" not in str(case["prompt"])
    assert result["model_calls"] == sum(calls.values())
    assert result["cost_usd"] == pytest.approx(0.0015 * sum(calls.values()))
    assert len(created) == 2 and created[0] is not created[1]
    assert created[0].metrics is not created[1].metrics
    assert len(closed) == 2 and closed[0].id != closed[1].id
    assert len(set(paths)) == 2
    assert all(not path.parent.exists() for path in paths)
    assert not list(tmp_path.glob("research-skill-evaluation-*"))


def test_missing_factory_is_blocked_without_creating_state(tmp_path: Path) -> None:
    result = evaluation.evaluate_skill_selection(None, max_budget_usd=0.1)
    assert result["status"] == "blocked" and result["ok"] is False
    assert result["model_calls"] == 0 and result["cost_usd"] == 0
    assert result["cases"] == []
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("key", [None, "", "   "])
def test_only_explicit_environment_key_is_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str | None
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-be-used")
    monkeypatch.setenv("LLM_API_KEY", "ambient-key-must-not-be-used")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("EVALUATION_SELECTED_KEY=dotenv-key-must-not-load\n")
    if key is None:
        monkeypatch.delenv("EVALUATION_SELECTED_KEY", raising=False)
    else:
        monkeypatch.setenv("EVALUATION_SELECTED_KEY", key)
    result = evaluation.evaluate_from_environment(
        model="gpt-4o",
        api_key_env="EVALUATION_SELECTED_KEY",
        max_budget_usd=0.1,
    )
    assert result["status"] == "blocked" and result["ok"] is False
    assert result["model_calls"] == 0 and result["cost_usd"] == 0
    assert result["cases"] == []
    assert not list(tmp_path.glob("research-skill-evaluation-*"))


def test_environment_factory_has_explicit_bounded_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVALUATION_SELECTED_KEY", "configured-test-secret")
    configured: list[LLM] = []

    def capture(
        factory: Callable[[str], LLM] | None,
        *,
        max_budget_usd: float,
        max_iterations: int,
    ) -> dict[str, JsonValue]:
        assert factory is not None
        assert max_budget_usd == 0.2 and max_iterations == 5
        configured.extend(factory(name) for name in ("relevant", "unrelated"))
        return {"ok": True, "status": "configuration-only"}

    monkeypatch.setattr(evaluation, "evaluate_skill_selection", capture)
    result = evaluation.evaluate_from_environment(
        model="operator-selected-model",
        api_key_env="EVALUATION_SELECTED_KEY",
        max_budget_usd=0.2,
        max_iterations=5,
    )
    assert configured[0] is not configured[1]
    assert configured[0].usage_id != configured[1].usage_id
    for llm in configured:
        assert llm.model == "operator-selected-model"
        assert isinstance(llm.api_key, SecretStr)
        assert llm.api_key.get_secret_value() == "configured-test-secret"
        assert llm.num_retries == 0 and llm.timeout == 30
        assert llm.max_output_tokens == 1024 and llm.stream is False
    assert "configured-test-secret" not in json.dumps(result)


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf"), True, "1"])
def test_invalid_budgets_fail_before_factory_calls(budget: Any) -> None:
    with pytest.raises(HarnessError, match="budget"):
        evaluation.evaluate_skill_selection(None, max_budget_usd=budget)


@pytest.mark.parametrize("iterations", [0, -1, 501, 2.5, True, "8"])
def test_invalid_iteration_limits_fail_before_factory_calls(iterations: Any) -> None:
    with pytest.raises(HarnessError, match="iterations"):
        evaluation.evaluate_skill_selection(
            None, max_budget_usd=0.1, max_iterations=iterations
        )


@pytest.mark.parametrize("phase", ["factory", "provider"])
def test_failure_closes_sessions_removes_state_and_redacts_error_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    closed: list[LocalConversation] = []
    original_close = LocalConversation.close

    def factory(name: str) -> LLM:
        if phase == "factory":
            raise RuntimeError("provider-secret-must-not-be-returned")
        return _llm(name)

    def transport(**kwargs: Any) -> ModelResponse:
        raise RuntimeError("provider-secret-must-not-be-returned")

    def tracked_close(conversation: LocalConversation) -> None:
        original_close(conversation)
        if Path(conversation.workspace.working_dir).is_relative_to(tmp_path):
            closed.append(conversation)

    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", transport)
    monkeypatch.setattr(LocalConversation, "close", tracked_close)
    result = evaluation.evaluate_skill_selection(factory, max_budget_usd=0.1)
    assert result["status"] == "failed" and result["ok"] is False
    cases = result["cases"]
    assert isinstance(cases, list) and len(cases) == 2
    assert all(
        isinstance(case, dict) and case["cleanup_ok"] and case.get("error_type")
        for case in cases
    )
    assert len(closed) == (2 if phase == "provider" else 0)
    assert "provider-secret-must-not-be-returned" not in json.dumps(result)
    assert not list(tmp_path.glob("research-skill-evaluation-*"))


@pytest.mark.parametrize(("budget", "iterations"), [(0.1, 1), (0.0001, 4)])
def test_skill_invocation_without_finished_run_cannot_pass(
    monkeypatch: pytest.MonkeyPatch, budget: float, iterations: int
) -> None:
    count = 0

    def transport(**kwargs: Any) -> ModelResponse:
        nonlocal count
        count += 1
        return _response("invoke_skill", {"name": "research-review"}, count)

    monkeypatch.setattr("openhands.sdk.llm.llm.litellm_completion", transport)
    result = evaluation.evaluate_skill_selection(
        _llm, max_budget_usd=budget, max_iterations=iterations
    )
    assert result["ok"] is False
    cases = result["cases"]
    assert isinstance(cases, list)
    relevant = cases[0]
    assert isinstance(relevant, dict)
    assert relevant["invoked_skills"] == ["research-review"]
    assert relevant["execution_status"] != "finished"
    assert relevant["ok"] is False
    assert relevant["cleanup_ok"] is True
    if budget < 0.001:
        assert relevant["budget_exceeded"] is True
    assert "not a hard billing cap" in str(result["budget_note"])
