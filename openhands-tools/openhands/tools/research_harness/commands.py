"""One bounded command contract shared by the CLI, SDK tool and MCP adapter."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

from ._http_worker import repository_url
from .artifacts import canonical_json
from .models import HarnessError


ARGUMENT_BYTE_LIMIT = 16_384
RecordId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")]
Digest = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
Question = Annotated[str, Field(min_length=1, max_length=4096, pattern=r"\S")]
ShortText = Annotated[str, Field(max_length=4096)]
PageLimit = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]


class CommandBase(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class ThreadCreate(CommandBase):
    question: Question
    conditions: dict[str, JsonValue] = Field(default_factory=dict)
    next_action: ShortText = ""


class ThreadReference(CommandBase):
    thread_id: RecordId


class Page(CommandBase):
    limit: PageLimit = 20
    offset: Offset = 0


class ThreadPage(Page):
    thread_id: RecordId


class ThreadUpdate(ThreadReference):
    expected_version: int = Field(ge=1)
    question: Question | None = None
    conditions: dict[str, JsonValue] | None = None
    assessment: Annotated[str, Field(max_length=16_384)] | None = None
    next_action: ShortText | None = None
    status: Literal["active", "closed"] | None = None
    reopen_condition: ShortText | None = None
    closure_reason: ShortText | None = None


class RunReference(CommandBase):
    run_id: RecordId


class EvidenceReference(CommandBase):
    evidence_id: RecordId


class Inventory(ThreadReference):
    max_files: int = Field(default=500, ge=1, le=500)
    max_file_bytes: int = Field(default=262_144, ge=1, le=262_144)
    max_total_bytes: int = Field(default=8_388_608, ge=1, le=8_388_608)


class GitHubMetadata(ThreadReference):
    repository: str = Field(min_length=3, max_length=140)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        repository_url(value)
        return value


class Compare(ThreadReference):
    before_sha: Digest
    after_sha: Digest


class Query(ThreadReference):
    artifact_sha: Digest
    module: ShortText | None = None
    name: ShortText | None = None
    path: ShortText | None = None
    limit: PageLimit = 20


class Context(ThreadReference):
    max_bytes: int = Field(default=8192, ge=512, le=16_384)
    evidence_limit: int = Field(default=32, ge=1, le=50)


class ArtifactRead(CommandBase):
    digest: Digest
    offset: Offset = 0
    limit: int = Field(default=2048, ge=1, le=4096)


class EventsExport(CommandBase):
    after_seq: Offset = 0
    limit: PageLimit = 100
    thread_id: RecordId | None = None


class Events(EventsExport):
    limit: PageLimit = 50
    run_id: RecordId | None = None


_COMMANDS: dict[str, type[CommandBase]] = {
    "thread.create": ThreadCreate,
    "thread.get": ThreadReference,
    "thread.list": Page,
    "thread.update": ThreadUpdate,
    "thread.history": ThreadPage,
    "run.get": RunReference,
    "run.list": ThreadPage,
    "evidence.get": EvidenceReference,
    "evidence.list": ThreadPage,
    "inventory": Inventory,
    "github": GitHubMetadata,
    "fixture": ThreadReference,
    "compare": Compare,
    "query": Query,
    "context": Context,
    "artifact.read": ArtifactRead,
    "events": Events,
    "events.export": EventsExport,
}
OPERATION_NAMES: tuple[str, ...] = tuple(_COMMANDS)


def validate_arguments(operation: str, arguments: dict[str, JsonValue]) -> CommandBase:
    """Validate finite JSON and its byte budget before constructing a command.

    Thread updates use ``model_dump(exclude_none=True)`` when applying changes;
    omitted fields and explicit null both mean that the field is unchanged.
    Record transitions and optimistic-version checks remain store responsibilities.
    """
    if not isinstance(operation, str) or operation not in _COMMANDS:
        raise HarnessError("Unknown harness operation")
    if not isinstance(arguments, dict):
        raise HarnessError("Command arguments must be a JSON object")
    try:
        encoded = canonical_json(arguments)
    except (TypeError, ValueError, RecursionError) as error:
        raise HarnessError(
            "Command arguments must contain finite JSON values"
        ) from error
    if len(encoded) > ARGUMENT_BYTE_LIMIT:
        raise HarnessError(f"Command arguments exceed {ARGUMENT_BYTE_LIMIT} bytes")
    try:
        return _COMMANDS[operation].model_validate(arguments)
    except ValidationError as error:
        first = error.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(part) for part in first["loc"]) or "arguments"
        raise HarnessError(
            f"Invalid {operation} arguments at {location[:128]}: {first['msg'][:300]}"
        ) from error


def command_schemas() -> dict[str, JsonValue]:
    """Generate published schemas from the exact models used for validation."""
    return {
        operation: model.model_json_schema() for operation, model in _COMMANDS.items()
    }
