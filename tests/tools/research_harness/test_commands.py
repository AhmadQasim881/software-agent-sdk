"""The shared protocol rejects ambiguity before any operation is dispatched."""

import pytest
from pydantic import JsonValue

from openhands.tools.research_harness.commands import (
    ARGUMENT_BYTE_LIMIT,
    OPERATION_NAMES,
    command_schemas,
    validate_arguments,
)
from openhands.tools.research_harness.models import HarnessError


_SHA = "a" * 64
_EXAMPLES: dict[str, dict[str, JsonValue]] = {
    "thread.create": {"question": "Which static imports are declared?"},
    "thread.get": {"thread_id": "thread-1"},
    "thread.list": {},
    "thread.update": {"thread_id": "thread-1", "expected_version": 1},
    "thread.history": {"thread_id": "thread-1"},
    "run.get": {"run_id": "run-1"},
    "run.list": {"thread_id": "thread-1"},
    "evidence.get": {"evidence_id": "evidence-1"},
    "evidence.list": {"thread_id": "thread-1"},
    "inventory": {"thread_id": "thread-1"},
    "github": {"thread_id": "thread-1", "repository": "OpenHands/software-agent-sdk"},
    "fixture": {"thread_id": "thread-1"},
    "compare": {"thread_id": "thread-1", "before_sha": _SHA, "after_sha": _SHA},
    "query": {"thread_id": "thread-1", "artifact_sha": _SHA},
    "context": {"thread_id": "thread-1"},
    "artifact.read": {"digest": _SHA},
    "events": {},
    "events.export": {},
}


def test_all_published_operations_use_their_actual_validation_schemas() -> None:
    schemas = command_schemas()
    assert set(schemas) == set(OPERATION_NAMES) == set(_EXAMPLES)
    for operation, arguments in _EXAMPLES.items():
        command = validate_arguments(operation, arguments)
        schema = schemas[operation]
        assert isinstance(schema, dict)
        assert schema == command.model_json_schema()
        assert schema["additionalProperties"] is False
        required = schema.get("required", [])
        assert isinstance(required, list)
        assert all(key in arguments for key in required if isinstance(key, str))
    assert validate_arguments("thread.list", {}).model_dump() == {
        "limit": 20,
        "offset": 0,
    }
    assert validate_arguments("events", {}).model_dump()["limit"] == 50
    assert validate_arguments("events.export", {}).model_dump()["limit"] == 100


def test_unexpected_fields_are_rejected_for_every_operation() -> None:
    for operation, arguments in _EXAMPLES.items():
        with pytest.raises(HarnessError, match="Extra inputs are not permitted"):
            validate_arguments(operation, {**arguments, "workspace": "/elsewhere"})


@pytest.mark.parametrize("value", [True, "2", 2.0])
def test_numeric_arguments_are_never_coerced(value: JsonValue) -> None:
    with pytest.raises(HarnessError, match="valid integer"):
        validate_arguments("thread.list", {"limit": value})


@pytest.mark.parametrize(
    ("operation", "field", "value"),
    [
        ("inventory", "max_files", 501),
        ("inventory", "max_file_bytes", 262_145),
        ("inventory", "max_total_bytes", 8_388_609),
        ("context", "max_bytes", 511),
        ("context", "max_bytes", 16_385),
        ("context", "evidence_limit", 51),
        ("thread.list", "limit", 101),
        ("thread.list", "offset", -1),
        ("artifact.read", "limit", 4097),
        ("events", "after_seq", -1),
        ("events.export", "limit", 0),
        ("thread.create", "question", "q" * 4097),
        ("thread.get", "thread_id", "t" * 129),
        ("query", "path", "p" * 4097),
    ],
)
def test_parameter_bounds_are_enforced(
    operation: str, field: str, value: JsonValue
) -> None:
    with pytest.raises(HarnessError, match="Invalid"):
        validate_arguments(operation, {**_EXAMPLES[operation], field: value})


@pytest.mark.parametrize("value", ["", " \t\n"])
def test_ids_and_questions_cannot_be_blank(value: str) -> None:
    with pytest.raises(HarnessError, match="Invalid"):
        validate_arguments("thread.get", {"thread_id": value})
    with pytest.raises(HarnessError, match="Invalid"):
        validate_arguments("thread.create", {"question": value})


@pytest.mark.parametrize("version", [None, 0, True])
def test_closing_a_thread_requires_an_explicit_valid_expected_version(
    version: JsonValue,
) -> None:
    arguments: dict[str, JsonValue] = {"thread_id": "thread-1", "status": "closed"}
    if version is not None:
        arguments["expected_version"] = version
    with pytest.raises(HarnessError, match="expected_version"):
        validate_arguments("thread.update", arguments)


def test_update_null_fields_are_omitted_without_discarding_explicit_empty_values() -> (
    None
):
    command = validate_arguments(
        "thread.update",
        {
            "thread_id": "thread-1",
            "expected_version": 3,
            "assessment": None,
            "next_action": "",
            "conditions": {},
        },
    )
    assert command.model_dump(exclude_none=True) == {
        "thread_id": "thread-1",
        "expected_version": 3,
        "next_action": "",
        "conditions": {},
    }


@pytest.mark.parametrize(
    "repository", ["../project", "https://github.com/x/y", "x/y/z"]
)
def test_github_accepts_only_repository_identity(repository: str) -> None:
    with pytest.raises(HarnessError, match="owner/name"):
        validate_arguments(
            "github", {"thread_id": "thread-1", "repository": repository}
        )


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "../artifact"])
def test_artifact_references_are_lowercase_digests(digest: str) -> None:
    with pytest.raises(HarnessError, match="Invalid"):
        validate_arguments("artifact.read", {"digest": digest})


def test_canonical_argument_budget_counts_utf8_bytes_before_model_validation() -> None:
    text = "ا" * (ARGUMENT_BYTE_LIMIT // 2)
    assert len(text) < ARGUMENT_BYTE_LIMIT
    with pytest.raises(HarnessError, match="arguments exceed 16384 bytes"):
        validate_arguments(
            "thread.create", {"question": "q", "conditions": {"note": text}}
        )


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_conditions_reject_nonfinite_json_numbers(number: float) -> None:
    with pytest.raises(HarnessError, match="finite JSON"):
        validate_arguments(
            "thread.create", {"question": "q", "conditions": {"number": number}}
        )


def test_unknown_operations_and_statuses_are_rejected() -> None:
    with pytest.raises(HarnessError, match="Unknown harness operation"):
        validate_arguments("shell.execute", {})
    with pytest.raises(HarnessError, match="status"):
        validate_arguments(
            "thread.update",
            {"thread_id": "thread-1", "expected_version": 1, "status": "deleted"},
        )


@pytest.mark.parametrize(
    ("operation", "field"),
    [
        ("thread.list", "offset"),
        ("events", "after_seq"),
        ("events.export", "after_seq"),
    ],
)
def test_database_cursors_fit_signed_64_bit_parameters(
    operation: str, field: str
) -> None:
    maximum = 2**63 - 1
    accepted = validate_arguments(operation, {field: maximum})
    assert accepted.model_dump()[field] == maximum
    with pytest.raises(HarnessError, match="less than or equal"):
        validate_arguments(operation, {field: maximum + 1})
