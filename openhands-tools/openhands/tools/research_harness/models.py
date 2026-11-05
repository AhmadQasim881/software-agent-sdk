"""Versioned records for local research state and observed execution results."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


ThreadStatus = Literal["active", "closed"]
RunStatus = Literal["running", "completed", "failed", "interrupted"]
TerminalStatus = Literal["completed", "failed", "interrupted"]
ShortText = Annotated[str, Field(max_length=4096)]


class HarnessError(ValueError):
    """A request violates the harness's data or execution contract."""


class NotFoundError(HarnessError):
    """A referenced record does not exist in this store."""


class ConflictError(HarnessError):
    """The requested transition conflicts with persisted state."""


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ThreadRecord(Record):
    id: str
    question: str = Field(min_length=1, max_length=4096)
    conditions: dict[str, JsonValue] = Field(default_factory=dict)
    assessment: str = Field(default="", max_length=16384)
    next_action: ShortText = ""
    status: ThreadStatus = "active"
    reopen_condition: ShortText = ""
    closure_reason: ShortText = ""
    version: int = Field(ge=1)
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def validate_thread(self) -> Self:
        if not self.question.strip():
            raise HarnessError("A research question must not be blank")
        if self.status == "closed" and not (
            self.closure_reason.strip() and self.reopen_condition.strip()
        ):
            raise HarnessError("Closing requires a reason and a reopen condition")
        return self


class RunRecord(Record):
    id: str
    thread_id: str
    operation: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")
    status: RunStatus = "running"
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    result: dict[str, JsonValue] = Field(default_factory=dict)
    error: ShortText | None = None
    session_id: str | None = Field(default=None, max_length=128)
    created_at: str
    finished_at: str | None = None

    @model_validator(mode="after")
    def validate_run(self) -> Self:
        if self.status == "running":
            if self.finished_at is not None or self.error is not None or self.result:
                raise HarnessError("Running operations cannot have a final result")
        elif self.finished_at is None:
            raise HarnessError("Finished operations require a finish time")
        elif self.status == "completed" and self.error is not None:
            raise HarnessError("A completed operation cannot have an error")
        elif self.status != "completed" and not (self.error and self.error.strip()):
            raise HarnessError("Failed or interrupted operations require a reason")
        return self


class EvidenceRecord(Record):
    id: str
    run_id: str
    thread_id: str
    run_status_at_capture: RunStatus
    source: str = Field(min_length=1, max_length=2048)
    conditions: dict[str, JsonValue] = Field(default_factory=dict)
    limitations: list[ShortText] = Field(default_factory=list, max_length=50)
    artifact_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(min_length=1, max_length=8192)
    created_at: str

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if not self.source.strip() or not self.summary.strip():
            raise HarnessError("Evidence source and summary must not be blank")
        return self


class EventRecord(Record):
    seq: int = Field(ge=1)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    thread_id: str | None = None
    run_id: str | None = None
    session_id: str | None = Field(default=None, max_length=128)
    created_at: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
