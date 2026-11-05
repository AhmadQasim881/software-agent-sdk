"""Checkpoint research records at real SDK condensation attempt boundaries."""

from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from openhands.sdk.context.condenser.base import (
    CondensationRequirement,
    RollingCondenser,
)
from openhands.sdk.context.view import View
from openhands.sdk.event.condenser import Condensation
from openhands.sdk.llm import LLM

from .context import assemble_context
from .store import HarnessStore


class CheckpointCondenser(RollingCondenser):
    """Persist a research-context manifest before invoking a rolling condenser.

    The SDK has no native PreCompact hook. This adapter checkpoints only when
    its inherited rolling strategy actually invokes a condensation or hard-reset
    attempt, including async attempts. Threshold checks do not create checkpoints.

    ``hook.pre_compact`` records an attempt, not a successful condensation. The
    delegate's result or exception is preserved; the SDK owns applying and storing
    the resulting Condensation event. The checkpoint saves selected research
    records, not a copy of the conversation View, and never modifies that View.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    delegate: RollingCondenser
    workspace: Path
    state_dir: Path
    thread_id: str = Field(min_length=1, max_length=128, pattern=r"\S")
    session_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("workspace", "state_dir")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        return value.resolve()

    def get_llm(self) -> LLM | None:
        return self.delegate.get_llm()

    def handles_condensation_requests(self) -> bool:
        return self.delegate.handles_condensation_requests()

    def condensation_requirement(
        self, view: View, agent_llm: LLM | None = None
    ) -> CondensationRequirement | None:
        return self.delegate.condensation_requirement(view, agent_llm=agent_llm)

    def _checkpoint(
        self,
        view: View,
        stage: Literal["condensation", "hard_reset"],
        entry_point: str,
    ) -> None:
        store = HarnessStore(self.state_dir, self.workspace)
        context = assemble_context(store, self.thread_id)
        store.append_event(
            "hook.pre_compact",
            thread_id=self.thread_id,
            session_id=self.session_id,
            payload={
                "stage": stage,
                "entry_point": entry_point,
                "status": "attempt",
                "delegate_kind": self.delegate.kind,
                "manifest_sha": context["manifest_sha"],
                "context_bytes": context["context_bytes"],
                "view_events": len(view),
                "unhandled_condensation_request": view.unhandled_condensation_request,
            },
        )

    def get_condensation(
        self, view: View, agent_llm: LLM | None = None
    ) -> Condensation:
        self._checkpoint(view, "condensation", "get_condensation")
        return self.delegate.get_condensation(view, agent_llm=agent_llm)

    async def aget_condensation(
        self, view: View, agent_llm: LLM | None = None
    ) -> Condensation:
        self._checkpoint(view, "condensation", "aget_condensation")
        return await self.delegate.aget_condensation(view, agent_llm=agent_llm)

    def hard_context_reset(
        self, view: View, agent_llm: LLM | None = None
    ) -> Condensation | None:
        self._checkpoint(view, "hard_reset", "hard_context_reset")
        return self.delegate.hard_context_reset(view, agent_llm=agent_llm)

    async def ahard_context_reset(
        self, view: View, agent_llm: LLM | None = None
    ) -> Condensation | None:
        self._checkpoint(view, "hard_reset", "ahard_context_reset")
        return await self.delegate.ahard_context_reset(view, agent_llm=agent_llm)
