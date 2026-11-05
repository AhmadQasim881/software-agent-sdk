"""Real pre-compaction checkpoints precede delegates without modifying SDK views."""

import asyncio
import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import PrivateAttr

from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.sdk.context.condenser.base import (
    CondensationRequirement,
    CondenserBase,
    NoCondensationAvailableException,
    RollingCondenser,
)
from openhands.sdk.context.view import View
from openhands.sdk.event.condenser import Condensation
from openhands.sdk.event.llm_convertible import MessageEvent
from openhands.sdk.llm import LLM, Message, TextContent
from openhands.tools.research_harness.condenser import CheckpointCondenser
from openhands.tools.research_harness.models import HarnessError
from openhands.tools.research_harness.store import HarnessStore


class CheckpointTestDelegate(RollingCondenser):
    """Assert durable ordering at the delegate boundary; never call an LLM."""

    workspace: Path
    state_dir: Path
    thread_id: str
    requirement: CondensationRequirement | None = CondensationRequirement.SOFT
    failure: Literal["none", "unavailable", "error"] = "none"
    reset_available: bool = True
    llm: LLM | None = None
    _calls: list[str] = PrivateAttr(default_factory=list)
    _agent_llm: LLM | None = PrivateAttr(default=None)

    def get_llm(self) -> LLM | None:
        return self.llm

    def handles_condensation_requests(self) -> bool:
        return True

    def condensation_requirement(
        self, view: View, agent_llm: LLM | None = None
    ) -> CondensationRequirement | None:
        self._agent_llm = agent_llm
        return self.requirement

    def _attempt(
        self, entry_point: str, view: View, agent_llm: LLM | None
    ) -> Condensation | None:
        self._calls.append(entry_point)
        self._agent_llm = agent_llm
        store = HarnessStore(self.state_dir, self.workspace)
        events = store.list_events()
        checkpoint = events[-1]
        assert events[-2].kind == "context.assembled"
        assert checkpoint.kind == "hook.pre_compact"
        assert checkpoint.payload["entry_point"] == entry_point
        assert checkpoint.payload["status"] == "attempt"
        assert checkpoint.payload["view_events"] == len(view)
        assert checkpoint.thread_id == self.thread_id
        manifest = json.loads(
            store.artifacts.read(str(checkpoint.payload["manifest_sha"]))
        )
        assert "Keep this research question" in manifest["context"]
        assert manifest["scope"]["thread_id"] == self.thread_id
        if "hard_context_reset" not in entry_point:
            if self.failure == "unavailable":
                raise NoCondensationAvailableException("Scripted unavailable summary")
            if self.failure == "error":
                raise RuntimeError("Scripted delegate failure")
        elif not self.reset_available:
            return None
        return Condensation(
            forgotten_event_ids={event.id for event in view.events[:2]},
            summary="Offline summary of the first two events",
            summary_offset=0,
            llm_response_id="offline-condensation",
        )

    def get_condensation(
        self, view: View, agent_llm: LLM | None = None
    ) -> Condensation:
        result = self._attempt("get_condensation", view, agent_llm)
        assert result is not None
        return result

    async def aget_condensation(
        self, view: View, agent_llm: LLM | None = None
    ) -> Condensation:
        await asyncio.sleep(0)
        result = self._attempt("aget_condensation", view, agent_llm)
        assert result is not None
        return result

    def hard_context_reset(
        self, view: View, agent_llm: LLM | None = None
    ) -> Condensation | None:
        return self._attempt("hard_context_reset", view, agent_llm)

    async def ahard_context_reset(
        self, view: View, agent_llm: LLM | None = None
    ) -> Condensation | None:
        await asyncio.sleep(0)
        return self._attempt("ahard_context_reset", view, agent_llm)


@pytest.fixture
def store(tmp_path: Path) -> HarnessStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessStore(tmp_path / "state", workspace)


@pytest.fixture
def delegate(store: HarnessStore) -> CheckpointTestDelegate:
    thread = store.create_thread("Keep this research question")
    return CheckpointTestDelegate(
        workspace=store.workspace, state_dir=store.state_dir, thread_id=thread.id
    )


@pytest.fixture
def checkpoint(delegate: CheckpointTestDelegate) -> CheckpointCondenser:
    return CheckpointCondenser(
        delegate=delegate,
        workspace=delegate.workspace,
        state_dir=delegate.state_dir,
        thread_id=delegate.thread_id,
        session_id="conversation-1",
    )


@pytest.fixture
def view() -> View:
    return View.from_events(
        [
            MessageEvent(
                llm_message=Message(role="user", content=[TextContent(text=text)]),
                source="user",
            )
            for text in ("First event", "Second event", "Third event")
        ]
    )


def test_checkpoint_precedes_real_condensation_and_preserves_sdk_semantics(
    store: HarnessStore,
    delegate: CheckpointTestDelegate,
    checkpoint: CheckpointCondenser,
    view: View,
) -> None:
    original = view.model_dump()
    result = checkpoint.condense(view)
    assert isinstance(result, Condensation)
    assert view.model_dump() == original
    assert delegate._calls == ["get_condensation"]
    restored_view = View.from_events([*view.events, result])
    assert len(restored_view) == 2
    assert restored_view.events[-1].id == view.events[-1].id
    assert store.list_events()[-1].session_id == "conversation-1"
    assert store.list_events()[-1].payload["stage"] == "condensation"


def test_noop_threshold_does_not_create_a_checkpoint(
    store: HarnessStore,
    delegate: CheckpointTestDelegate,
    checkpoint: CheckpointCondenser,
    view: View,
) -> None:
    delegate.requirement = None
    before = store.list_events()
    assert checkpoint.condense(view) is view
    assert store.list_events() == before
    assert delegate._calls == []
    assert list(store.artifacts.root.iterdir()) == []


def test_delegate_failure_keeps_checkpoint_without_claiming_compaction_success(
    store: HarnessStore,
    delegate: CheckpointTestDelegate,
    checkpoint: CheckpointCondenser,
    view: View,
) -> None:
    delegate.failure = "error"
    original = view.model_dump()
    with pytest.raises(RuntimeError, match="Scripted delegate failure"):
        checkpoint.condense(view)
    assert view.model_dump() == original
    event = store.list_events()[-1]
    assert event.kind == "hook.pre_compact"
    assert event.payload["status"] == "attempt"
    assert "success" not in event.payload
    assert store.artifacts.read(str(event.payload["manifest_sha"]))


def test_soft_unavailable_summary_preserves_the_original_view(
    delegate: CheckpointTestDelegate, checkpoint: CheckpointCondenser, view: View
) -> None:
    delegate.failure = "unavailable"
    assert checkpoint.condense(view) is view
    assert delegate._calls == ["get_condensation"]


@pytest.mark.parametrize("reset_available", [True, False])
def test_hard_fallback_checkpoints_each_attempt_separately(
    store: HarnessStore,
    delegate: CheckpointTestDelegate,
    checkpoint: CheckpointCondenser,
    view: View,
    reset_available: bool,
) -> None:
    delegate.requirement = CondensationRequirement.HARD
    delegate.failure = "unavailable"
    delegate.reset_available = reset_available
    if reset_available:
        assert isinstance(checkpoint.condense(view), Condensation)
    else:
        with pytest.raises(NoCondensationAvailableException):
            checkpoint.condense(view)
    assert delegate._calls == ["get_condensation", "hard_context_reset"]
    assert [
        event.payload["stage"]
        for event in store.list_events()
        if event.kind == "hook.pre_compact"
    ] == ["condensation", "hard_reset"]


@pytest.mark.parametrize("mode", ["noop", "normal", "hard_fallback"])
async def test_async_boundaries_call_async_delegates_and_keep_view_unchanged(
    store: HarnessStore,
    delegate: CheckpointTestDelegate,
    checkpoint: CheckpointCondenser,
    view: View,
    mode: str,
) -> None:
    original = view.model_dump()
    before = store.list_events()
    if mode == "noop":
        delegate.requirement = None
    elif mode == "hard_fallback":
        delegate.requirement = CondensationRequirement.HARD
        delegate.failure = "unavailable"
    result = await checkpoint.acondense(view)
    assert view.model_dump() == original
    if mode == "noop":
        assert result is view
        assert store.list_events() == before
        assert delegate._calls == []
    else:
        assert isinstance(result, Condensation)
        expected = ["aget_condensation"]
        if mode == "hard_fallback":
            expected.append("ahard_context_reset")
        assert delegate._calls == expected


def test_checkpoint_failure_prevents_delegate_execution(
    store: HarnessStore,
    delegate: CheckpointTestDelegate,
    checkpoint: CheckpointCondenser,
    view: View,
) -> None:
    checkpoint.thread_id = "missing-thread"
    before = store.list_events()
    with pytest.raises(HarnessError, match="Unknown"):
        checkpoint.get_condensation(view)
    assert delegate._calls == []
    assert store.list_events() == before


def test_capabilities_and_agent_llm_are_forwarded(
    delegate: CheckpointTestDelegate, checkpoint: CheckpointCondenser, view: View
) -> None:
    llm = LLM(model="test-model")
    delegate.llm = llm
    assert checkpoint.get_llm() is llm
    assert checkpoint.handles_condensation_requests() is True
    assert checkpoint.condensation_requirement(view, agent_llm=llm) == (
        CondensationRequirement.SOFT
    )
    checkpoint.condense(view, agent_llm=llm)
    assert delegate._agent_llm is llm


def test_real_summarizing_delegate_round_trips_through_sdk_condenser_type(
    checkpoint: CheckpointCondenser,
) -> None:
    checkpoint.delegate = LLMSummarizingCondenser(llm=LLM(model="test-model"))
    serialized = checkpoint.model_dump_json()
    restored = CondenserBase.model_validate_json(serialized)
    assert isinstance(restored, CheckpointCondenser)
    assert isinstance(restored.delegate, LLMSummarizingCondenser)
    assert restored.workspace == checkpoint.workspace
    assert restored.state_dir == checkpoint.state_dir
    assert restored.thread_id == checkpoint.thread_id
    assert restored.session_id == checkpoint.session_id
    assert restored.delegate.llm.model == "test-model"
