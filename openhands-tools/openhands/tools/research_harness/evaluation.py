"""Opt-in operator evaluation of model-selected skills on an owned local example.

No LLM or credentials are discovered automatically. A supplied factory must return
one fresh configured LLM per case. Scripted transport is useful for testing these
mechanics; its results are not evidence of a real model's semantic skill selection.
"""

import math
import os
import re
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import JsonValue, SecretStr

from openhands.sdk import LLM, LocalConversation
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.event import ActionEvent

from .models import HarnessError
from .service import HarnessService
from .session import create_research_conversation


_SKILL_NAME = "research-review"
_PROMPTS = {
    "relevant": (
        "Please resume the saved local Python import review. Read the recorded "
        "evidence, summarize what was observed and its limitations, and suggest "
        "the next useful action."
    ),
    "unrelated": "Hello! Please give me a short friendly greeting.",
}
_BUDGET_NOTE = (
    "Costs are SDK-reported estimates. Limits are checked between provider calls, "
    "not a hard billing cap; one response may exceed its case allocation. "
    "Unknown provider pricing may produce a zero estimate."
)
_CALL_NOTE = (
    "model_calls counts completed responses recorded in SDK latency metrics; "
    "failed provider attempts may not appear in this count."
)


def _validate_bounds(max_budget_usd: float, max_iterations: int) -> float:
    if isinstance(max_budget_usd, bool) or not isinstance(max_budget_usd, int | float):
        raise HarnessError("Maximum model budget must be finite and positive")
    try:
        budget = float(max_budget_usd)
    except (OverflowError, ValueError) as error:
        raise HarnessError(
            "Maximum model budget must be finite and positive"
        ) from error
    if not math.isfinite(budget) or budget / 2 <= 0:
        raise HarnessError("Maximum model budget must be finite and positive")
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or not 1 <= max_iterations <= 500
    ):
        raise HarnessError("Maximum iterations must be an integer in 1..500")
    return budget


def _report(budget: float, max_iterations: int) -> dict[str, JsonValue]:
    return {
        "ok": False,
        "status": "blocked",
        "evaluation": "research-review-skill-selection",
        "max_budget_usd": budget,
        "per_case_budget_usd": budget / 2,
        "max_iterations": max_iterations,
        "cost_usd": 0.0,
        "model_calls": 0,
        "budget_note": _BUDGET_NOTE,
        "call_count_note": _CALL_NOTE,
        "cases": [],
    }


def _observe(conversation: LocalConversation) -> dict[str, JsonValue]:
    metrics = conversation.conversation_stats.get_combined_metrics()
    return {
        "execution_status": conversation.state.execution_status.value,
        "invoked_skills": list(conversation.state.invoked_skills),
        "actions": [
            event.tool_name
            for event in conversation.state.events
            if isinstance(event, ActionEvent)
        ],
        "cost_usd": metrics.accumulated_cost,
        "model_calls": len(metrics.response_latencies),
    }


def _evaluate_case(
    name: str,
    llm_factory: Callable[[str], LLM],
    previous_llms: list[LLM],
    *,
    budget: float,
    max_iterations: int,
) -> dict[str, JsonValue]:
    expected = name == "relevant"
    result: dict[str, JsonValue] = {
        "name": name,
        "prompt": _PROMPTS[name],
        "expected_skill_invocation": expected,
        "max_budget_usd": budget,
        "max_iterations": max_iterations,
        "cost_usd": 0.0,
        "model_calls": 0,
        "execution_status": None,
        "invoked_skills": [],
        "actions": [],
    }
    root: Path | None = None
    try:
        with TemporaryDirectory(prefix="research-skill-evaluation-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "example.py").write_text(
                "import json\nfrom pathlib import Path\n", encoding="utf-8"
            )
            service = HarnessService(workspace=workspace, state_dir=root / "state")
            thread = service.store.create_thread(
                "Which Python imports were observed in this local example?",
                conditions={"scope": "owned disposable Python example"},
                next_action="Read the saved import inventory and explain its limits.",
            )
            inventory = service.execute("inventory", {"thread_id": thread.id})
            if inventory.get("ok") is not True:
                raise HarnessError("Could not prepare the local inventory")
            llm = llm_factory(name)
            if not isinstance(llm, LLM) or any(
                llm is previous or llm.metrics is previous.metrics
                for previous in previous_llms
            ):
                raise HarnessError("The factory must return a fresh LLM for each case")
            previous_llms.append(llm)
            result["model"] = llm.model
            conversation = create_research_conversation(
                llm=llm,
                workspace=workspace,
                state_dir=service.store.state_dir,
                thread_id=thread.id,
                allow_github=False,
                operator_instructions=(
                    "This session uses an evaluator-owned disposable Python example. "
                    "For a review, use its saved local observations. Do not start "
                    "services, execute source, or access external targets. Respond "
                    "to the current user request."
                ),
                max_iteration_per_run=max_iterations,
                max_budget_per_run=budget,
            )
            try:
                conversation.send_message(_PROMPTS[name])
                conversation.run()
            finally:
                try:
                    result.update(_observe(conversation))
                finally:
                    conversation.close()
    except Exception as error:
        # Provider exceptions and hook errors may contain keys or request bodies.
        result["error_type"] = type(error).__name__
    result["cleanup_ok"] = root is None or not root.exists()
    skills = result["invoked_skills"]
    invoked = isinstance(skills, list) and _SKILL_NAME in skills
    cost = result["cost_usd"]
    budget_exceeded = isinstance(cost, int | float) and cost > budget
    result["budget_exceeded"] = budget_exceeded
    result["ok"] = (
        "error_type" not in result
        and result["cleanup_ok"] is True
        and result["execution_status"] == ConversationExecutionStatus.FINISHED.value
        and invoked == expected
        and not budget_exceeded
    )
    result["status"] = "passed" if result["ok"] else "failed"
    return result


def evaluate_skill_selection(
    llm_factory: Callable[[str], LLM] | None,
    *,
    max_budget_usd: float,
    max_iterations: int = 8,
) -> dict[str, JsonValue]:
    """Evaluate relevant and unrelated requests in independent local sessions.

    The explicit configured LLM factory receives "relevant" or "unrelated".
    Each case gets half the requested budget and the iteration limit. Limits
    use SDK-reported cost, checked between provider calls, and are not a hard
    billing cap. Caller-supplied LLM retry, timeout and logging configuration
    remains the caller's responsibility.

    Results retain actual skill invocations, action names, completion status and
    SDK metrics, not model-authored claims. Transcripts and temporary state are
    removed after each session closes. Exception details are never returned.
    """
    budget = _validate_bounds(max_budget_usd, max_iterations)
    result = _report(budget, max_iterations)
    if llm_factory is None:
        result["reason"] = "An explicitly configured LLM factory is required"
        return result
    if not callable(llm_factory):
        raise HarnessError("LLM factory must be callable")
    previous_llms: list[LLM] = []
    cases = [
        _evaluate_case(
            name,
            llm_factory,
            previous_llms,
            budget=budget / 2,
            max_iterations=max_iterations,
        )
        for name in _PROMPTS
    ]
    result["cases"] = list(cases)
    result["ok"] = all(case["ok"] is True for case in cases)
    result["status"] = "passed" if result["ok"] else "failed"
    result["cost_usd"] = sum(
        value for case in cases if isinstance(value := case["cost_usd"], int | float)
    )
    result["model_calls"] = sum(
        calls for case in cases if isinstance(calls := case["model_calls"], int)
    )
    return result


def evaluate_from_environment(
    *,
    model: str,
    api_key_env: str,
    max_budget_usd: float,
    max_iterations: int = 8,
) -> dict[str, JsonValue]:
    """Read only the operator-named key variable; never load dotenv or defaults.

    This is an explicit opt-in to provider requests when the selected key exists.
    Missing or blank keys return a blocked result without constructing an LLM.
    """
    budget = _validate_bounds(max_budget_usd, max_iterations)
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise HarnessError("Model must be a nonblank name of at most 256 characters")
    if not isinstance(api_key_env, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]{0,127}", api_key_env
    ):
        raise HarnessError("API key environment variable name is invalid")
    key = os.environ.get(api_key_env)
    if not key or not key.strip():
        result = _report(budget, max_iterations)
        result["reason"] = (
            "The selected API key environment variable is missing or blank"
        )
        return result

    def factory(name: str) -> LLM:
        return LLM(
            model=model,
            api_key=SecretStr(key),
            usage_id=f"research-skill-evaluation-{name}",
            num_retries=0,
            timeout=30,
            max_output_tokens=1024,
            stream=False,
        )

    return evaluate_skill_selection(
        factory, max_budget_usd=budget, max_iterations=max_iterations
    )
