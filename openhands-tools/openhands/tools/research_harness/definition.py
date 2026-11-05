"""OpenHands tool adapter for the bounded research harness service."""

import asyncio
import contextvars
from collections.abc import Sequence
from concurrent.futures import Future
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from typing import TYPE_CHECKING, ClassVar

from pydantic import ConfigDict, Field, JsonValue

from openhands.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)

from .artifacts import canonical_json
from .models import HarnessError
from .service import HarnessService


if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation
    from openhands.sdk.conversation.state import ConversationState


_CALL_CANCELLATION: contextvars.ContextVar[Event | None] = contextvars.ContextVar(
    "research_harness_call_cancellation", default=None
)
_CANCEL_POLL_SECONDS = 0.02


class ResearchHarnessAction(Action):
    """Select a service operation and supply its strictly validated arguments."""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)

    operation: str = Field(min_length=1, max_length=128)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ResearchHarnessObservation(Observation):
    """Compact observed result with references to the complete persisted evidence."""

    result: dict[str, JsonValue]


class ResearchHarnessExecutor(
    ToolExecutor[ResearchHarnessAction, ResearchHarnessObservation]
):
    """Own service workers and drain their cleanup before an interruption returns.

    SDK async conversations call the synchronous executor from a pool thread and
    invoke ``interrupt()`` when their task is cancelled. Sync conversations only
    set their cancellation token. A short join interval bridges that captured
    token to the invocation's event without joining any SDK-owned thread.
    """

    def __init__(self, service: HarnessService) -> None:
        self.service = service
        self._lock = Lock()
        self._calls: dict[Thread, Event] = {}
        self._closed = False

    def __call__(
        self,
        action: ResearchHarnessAction,
        conversation: "LocalConversation | None" = None,
    ) -> ResearchHarnessObservation:
        cancel = _CALL_CANCELLATION.get() or Event()
        token = conversation.cancel_token if conversation is not None else None
        session_id = str(conversation.id) if conversation is not None else None
        if token is not None and token.is_cancelled:
            cancel.set()
        result: Future[dict[str, JsonValue]] = Future()
        context = contextvars.copy_context()

        def execute() -> None:
            try:
                if action.operation == "schema":
                    if action.arguments:
                        raise HarnessError("schema requires empty arguments")
                    result.set_result({"ok": True, "operations": self.service.schema()})
                    return
                result.set_result(
                    self.service.execute(
                        action.operation,
                        action.arguments,
                        session_id=session_id,
                        cancel_event=cancel,
                    )
                )
            except BaseException as error:
                result.set_exception(error)

        worker = Thread(
            target=context.run,
            args=(execute,),
            name="research-harness-tool",
        )
        with self._lock:
            if self._closed:
                raise HarnessError("Research harness executor is closed")
            self._calls[worker] = cancel
            try:
                worker.start()
            except BaseException:
                self._calls.pop(worker, None)
                raise

        try:
            while worker.is_alive():
                if token is not None and token.is_cancelled:
                    cancel.set()
                worker.join(_CANCEL_POLL_SECONDS)
            observed = result.result()
            return ResearchHarnessObservation.from_text(
                canonical_json(observed).decode("utf-8"),
                result=observed,
                is_error=observed.get("ok") is False,
            )
        finally:
            cancel.set()
            worker.join()
            with self._lock:
                self._calls.pop(worker, None)

    def _cancel_active(self, *, close: bool = False) -> None:
        with self._lock:
            if close:
                self._closed = True
            calls = tuple(self._calls.items())
        # Signal every invocation before waiting for any one of them.
        for _, cancel in calls:
            cancel.set()
        for worker, _ in calls:
            if worker is not current_thread():
                worker.join()

    def interrupt(self) -> None:
        """Signal and drain the currently active invocations; allow later calls."""
        self._cancel_active()

    def close(self) -> None:
        """Reject new invocations, then signal and drain every owned worker."""
        self._cancel_active(close=True)


class ResearchHarnessTool(
    ToolDefinition[ResearchHarnessAction, ResearchHarnessObservation]
):
    """Persist research state and run bounded observations in a fixed workspace."""

    catalog_description: ClassVar[str] = (
        "Track research questions, inspect Python declarations, and retain evidence."
    )

    @classmethod
    def create(
        cls,
        conv_state: "ConversationState",
        *,
        state_dir: str,
        allow_github: bool = False,
    ) -> Sequence["ResearchHarnessTool"]:
        service = HarnessService(
            workspace=Path(conv_state.workspace.working_dir),
            state_dir=Path(state_dir),
            allow_github=allow_github,
        )
        description = (
            "Maintain versioned research questions and retrieve recorded evidence. "
            "The workspace is fixed by the conversation; state is stored separately. "
            "Inventory reads Python declarations without running workspace code. "
            "Fixture validation runs only the harness-owned local fixture. "
            "GitHub public metadata requires the operator's allow_github option. "
            "Observations report failures and limitations; they do not establish "
            "vulnerabilities or protect state from other host processes. "
            "Call schema with empty arguments to discover the argument schemas. "
            "Operations: schema, " + ", ".join(service.schema()) + "."
        )
        return [
            cls(
                description=description,
                action_type=ResearchHarnessAction,
                observation_type=ResearchHarnessObservation,
                annotations=ToolAnnotations(
                    title="Research harness",
                    readOnlyHint=False,
                    destructiveHint=False,
                    idempotentHint=False,
                    openWorldHint=allow_github,
                ),
                executor=ResearchHarnessExecutor(service),
            )
        ]

    async def acall(
        self,
        action: ResearchHarnessAction,
        conversation: "LocalConversation | None" = None,
    ) -> Observation:
        """Cancel only this direct async call and await its actual worker cleanup.

        Context propagation supplies the invocation's event while keeping the
        usual ToolDefinition call path, including SDK observation secret masking.
        Conversation.arun uses the executor's interrupt hook instead of acall.
        """
        cancel = Event()
        token = _CALL_CANCELLATION.set(cancel)
        try:
            work = asyncio.create_task(asyncio.to_thread(self, action, conversation))
        finally:
            _CALL_CANCELLATION.reset(token)
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            cancel.set()
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not work.cancelled():
                work.exception()
            raise


register_tool(ResearchHarnessTool.name, ResearchHarnessTool)
