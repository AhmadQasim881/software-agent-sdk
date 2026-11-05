"""Exact, bounded queries and content comparisons over recorded import snapshots."""

import keyword
from collections import Counter
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from .artifacts import ArtifactStore, canonical_json
from .models import HarnessError


_RESULT_BYTES = 131_072
_COMPARE_GROUPS = 100
_LIMITATIONS: list[JsonValue] = [
    "Results describe the referenced snapshots, not current workspace contents.",
    "Static imports do not establish runtime behavior or a security verdict.",
]
_StopReason = Literal[
    "cancelled",
    "time_budget",
    "depth_budget",
    "traversal_budget",
    "file_budget",
    "total_bytes_budget",
    "output_budget",
    "unsupported_platform",
]
_ExclusionReason = Literal[
    "symlink",
    "excluded_directory",
    "hidden_directory",
    "non_regular",
    "hidden_file",
    "non_python_file",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


def _relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and len(value) <= 4096
        and "\x00" not in value
        and not path.is_absolute()
        and ".." not in path.parts
        and str(path) == value
        and path.suffix == ".py"
    )


def _identifier(value: str) -> bool:
    return value.isidentifier() and not keyword.iskeyword(value)


def _module_name(value: str) -> bool:
    return bool(value) and all(_identifier(part) for part in value.split("."))


class _Name(_StrictModel):
    name: str = Field(min_length=1)
    alias: str | None


class _Entry(_StrictModel):
    path: str
    line: int = Field(ge=1)
    column: int = Field(ge=0)
    kind: Literal["import", "from"]
    module: str
    level: int = Field(ge=0, le=1_048_576)
    names: list[_Name] = Field(min_length=1)
    normalized: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_content(self) -> Self:
        if not _relative_path(self.path):
            raise ValueError("Import path must be a relative .py path")
        if self.kind == "import":
            if (
                self.module
                or self.level
                or any(not _module_name(name.name) for name in self.names)
            ):
                raise ValueError("Invalid import declaration")
            prefix = "import "
        else:
            if (self.module and not _module_name(self.module)) or not (
                self.module or self.level
            ):
                raise ValueError("Invalid from-import module")
            if any(
                name.name != "*" and not _identifier(name.name) for name in self.names
            ):
                raise ValueError("Invalid imported name")
            if any(name.name == "*" for name in self.names) and (
                len(self.names) != 1 or self.names[0].alias is not None
            ):
                raise ValueError("Invalid wildcard import")
            prefix = f"from {'.' * self.level}{self.module} import "
        if any(
            name.alias is not None and not _identifier(name.alias)
            for name in self.names
        ):
            raise ValueError("Invalid import alias")
        expected = prefix + ", ".join(
            name.name + (f" as {name.alias}" if name.alias is not None else "")
            for name in self.names
        )
        if expected != self.normalized:
            raise ValueError("Normalized import does not match its structured fields")
        return self


class _File(_StrictModel):
    path: str
    status: (
        Literal["parsed", "file_too_large", "parse_error", "read_error"] | _StopReason
    )
    bytes_read: int = Field(ge=0)
    sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    imports: int = Field(ge=0)


class _Excluded(_StrictModel):
    path: str
    reason: str = Field(min_length=1)


class _Error(_Excluded):
    detail: str


class _Limits(_StrictModel):
    max_files: int = Field(ge=1)
    max_file_bytes: int = Field(ge=1)
    max_total_bytes: int = Field(ge=1)
    max_traversal_entries: int = Field(ge=1)
    max_depth: int = Field(ge=1)
    seconds: float = Field(gt=0)
    detail_bytes: int = Field(ge=1)


class _Coverage(_StrictModel):
    directories_visited: int = Field(ge=0)
    entries_examined: int = Field(ge=0)
    python_files_seen: int = Field(ge=0)
    files_parsed: int = Field(ge=0)
    files_failed: int = Field(ge=0)
    files_metadata_omitted: int = Field(ge=0)
    bytes_read: int = Field(ge=0)
    imports_recorded: int = Field(ge=0)
    excluded_counts: dict[_ExclusionReason, int]
    excluded: list[_Excluded]
    excluded_details_omitted: int = Field(ge=0)
    errors: list[_Error]
    errors_omitted: int = Field(ge=0)
    stop_reason: _StopReason | None
    limits: _Limits


class _Snapshot(_StrictModel):
    schema_version: int = Field(ge=1, le=1)
    tool_version: Literal["python-import-inventory/1"]
    python_version: str = Field(
        pattern=r"^\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)$", max_length=128
    )
    ok: bool
    complete: bool
    source: str = Field(min_length=1, max_length=4096)
    summary: str = Field(min_length=1)
    limitations: list[str]
    entries: list[_Entry]
    files: list[_File]
    coverage: _Coverage

    @model_validator(mode="after")
    def check_coverage(self) -> Self:
        coverage = self.coverage
        complete = coverage.stop_reason is None and not (
            coverage.errors or coverage.errors_omitted
        )
        if self.ok != self.complete or self.complete != complete:
            raise ValueError("Snapshot completeness conflicts with recorded coverage")
        if coverage.imports_recorded != len(self.entries):
            raise ValueError("Import count conflicts with recorded entries")
        if any(count < 0 for count in coverage.excluded_counts.values()) or (
            sum(coverage.excluded_counts.values())
            != len(coverage.excluded) + coverage.excluded_details_omitted
        ):
            raise ValueError("Excluded-path counts are inconsistent")
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths) or any(
            not _relative_path(path) for path in paths
        ):
            raise ValueError("File records require unique relative .py paths")
        if self.complete and (
            coverage.files_failed
            or coverage.files_metadata_omitted
            or coverage.files_parsed != len(self.files)
            or any(
                file.status != "parsed" or file.sha256 is None for file in self.files
            )
        ):
            raise ValueError("Complete snapshot has incomplete file records")
        imports = Counter(entry.path for entry in self.entries)
        if any(file.imports != imports[file.path] for file in self.files) or (
            not coverage.files_metadata_omitted and set(imports) - set(paths)
        ):
            raise ValueError("File import counts conflict with recorded entries")
        return self


def _load(store: ArtifactStore, digest: str) -> _Snapshot:
    try:
        raw = store.read(digest)
    except (OSError, ValueError) as error:
        raise HarnessError(
            f"Cannot read inventory artifact: {str(error)[:300]}"
        ) from error
    try:
        return _Snapshot.model_validate_json(raw)
    except ValidationError as error:
        first = error.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(part) for part in first["loc"]) or "snapshot"
        raise HarnessError(
            f"Invalid inventory snapshot at {location[:200]}: {first['msg'][:300]}"
        ) from error


def _provenance(snapshot: _Snapshot, digest: str) -> dict[str, JsonValue]:
    return {
        "artifact_sha": digest,
        "schema_version": snapshot.schema_version,
        "tool_version": snapshot.tool_version,
        "python_version": snapshot.python_version,
        "source": snapshot.source,
        "complete": snapshot.complete,
        "coverage": snapshot.coverage.model_dump(
            mode="json", exclude={"excluded", "errors"}
        ),
        "coverage_details_in_artifact": {
            "excluded": len(snapshot.coverage.excluded),
            "errors": len(snapshot.coverage.errors),
        },
    }


def query_inventory(
    store: ArtifactStore,
    artifact_sha: str,
    *,
    module: str | None = None,
    name: str | None = None,
    path: str | None = None,
    limit: int = 20,
) -> dict[str, JsonValue]:
    """Apply exact filters to a validated snapshot; never run supplied query code.

    Module matches a from-import's module or a bare import's full imported name.
    Name matches the declared name, not its local alias. Empty module selects
    module-less relative from-imports. All supplied filters must match.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise HarnessError("Query limit must be an integer in 1..100")
    for label, value in (("module", module), ("name", name), ("path", path)):
        if value is not None and (
            not isinstance(value, str) or len(value) > 4096 or "\x00" in value
        ):
            raise HarnessError(
                f"Query {label} must be a string of at most 4096 characters"
            )
    if name == "" or (path is not None and not _relative_path(path)):
        raise HarnessError("Query name must be nonempty and path must be relative .py")
    snapshot = _load(store, artifact_sha)
    matches = [
        entry
        for entry in snapshot.entries
        if (path is None or entry.path == path)
        and (name is None or any(item.name == name for item in entry.names))
        and (
            module is None
            or (entry.kind == "from" and entry.module == module)
            or (
                entry.kind == "import"
                and any(item.name == module for item in entry.names)
            )
        )
    ]
    matches.sort(
        key=lambda entry: (entry.path, entry.line, entry.column, entry.normalized)
    )
    selected: list[JsonValue] = []
    used = 0
    for entry in matches[:limit]:
        row = entry.model_dump(mode="json")
        size = len(canonical_json(row)) + 2
        if used + size <= _RESULT_BYTES:
            selected.append(row)
            used += size
    partial = not snapshot.complete
    return {
        "ok": True,
        "source": f"artifact:{artifact_sha}",
        "summary": f"{len(matches)} matching recorded imports; "
        + ("partial dataset" if partial else "dataset complete within inventory scope"),
        "limitations": _LIMITATIONS
        + (
            ["A partial dataset cannot establish the absence of unrecorded imports."]
            if partial
            else []
        ),
        "dataset_complete": snapshot.complete,
        "filters": {"module": module, "name": name, "path": path},
        "match_count": len(matches),
        "entries": selected,
        "entries_omitted": len(matches) - len(selected),
        "selection_limit": limit,
        "selection_byte_limit": _RESULT_BYTES,
        "provenance": _provenance(snapshot, artifact_sha),
    }


def compare_inventories(
    store: ArtifactStore, before_sha: str, after_sha: str
) -> dict[str, JsonValue]:
    """Compare multiplicities of (path, normalized import), ignoring line movement."""
    before, after = _load(store, before_sha), _load(store, after_sha)
    old = Counter((entry.path, entry.normalized) for entry in before.entries)
    new = Counter((entry.path, entry.normalized) for entry in after.entries)
    added, removed = new - old, old - new
    selections: dict[str, JsonValue] = {}
    omissions: dict[str, JsonValue] = {}
    used = 0
    for label, changes in (("added", added), ("removed", removed)):
        selected: list[JsonValue] = []
        selected_occurrences = 0
        for (path, normalized), count in sorted(changes.items()):
            row: dict[str, JsonValue] = {
                "path": path,
                "normalized": normalized,
                "count": count,
            }
            size = len(canonical_json(row)) + 2
            if len(selected) < _COMPARE_GROUPS and used + size <= _RESULT_BYTES:
                selected.append(row)
                selected_occurrences += count
                used += size
        selections[label] = selected
        omissions[label] = {
            "distinct_imports": len(changes) - len(selected),
            "occurrences": sum(changes.values()) - selected_occurrences,
        }
    partial = not (before.complete and after.complete)
    limitations = _LIMITATIONS + ["Comparison ignores line and column positions."]
    if partial:
        limitations.append(
            "Partial datasets may omit changes; equality covers recorded entries only."
        )
    compatible = before.python_version == after.python_version
    if not compatible:
        limitations.append(
            "Python versions differ; parser normalization may also differ."
        )
    return {
        "ok": compatible,
        "source": f"artifacts:{before_sha}:{after_sha}",
        "summary": ("Incompatible parser versions; " if not compatible else "")
        + f"Recorded imports: {sum(added.values())} added, "
        f"{sum(removed.values())} removed; "
        + (
            "partial datasets"
            if partial
            else "datasets complete within inventory scope"
        ),
        "limitations": limitations,
        "dataset_complete": not partial,
        "normalization_compatible": compatible,
        "observed_equal": old == new,
        "added_count": sum(added.values()),
        "removed_count": sum(removed.values()),
        "unchanged_count": sum((old & new).values()),
        **selections,
        "omitted": omissions,
        "selection_group_limit_per_side": _COMPARE_GROUPS,
        "selection_byte_limit": _RESULT_BYTES,
        "before": _provenance(before, before_sha),
        "after": _provenance(after, after_sha),
    }
