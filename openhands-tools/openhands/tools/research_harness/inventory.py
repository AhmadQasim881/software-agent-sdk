"""Bounded Python import inventory; target source is parsed and never executed."""

import ast
import hashlib
import os
import platform
import stat
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

from pydantic import JsonValue

from .artifacts import canonical_json


_EXCLUDED_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__"}
_OUTPUT_BUDGET = 750_000
_DETAIL_LIMIT = 100
_SCAN_SECONDS = 5.0


class _Stopped(Exception):
    pass


class _Inventory:
    def __init__(
        self,
        workspace: Path,
        max_files: int,
        max_file_bytes: int,
        max_total_bytes: int,
        cancel_event: threading.Event | None,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be an existing directory")
        for name, value, upper in (
            ("max_files", max_files, 10_000),
            ("max_file_bytes", max_file_bytes, 1_048_576),
            ("max_total_bytes", max_total_bytes, 67_108_864),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= upper
            ):
                raise ValueError(f"{name} must be an integer in 1..{upper}")
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.traversal_limit = min(100_000, max_files * 20)
        self.cancel_event = cancel_event
        self.deadline = monotonic() + _SCAN_SECONDS
        self.entries: list[JsonValue] = []
        self.files: list[JsonValue] = []
        self.excluded: list[JsonValue] = []
        self.errors: list[JsonValue] = []
        self.excluded_counts: Counter[str] = Counter()
        self.error_count = 0
        self.files_metadata_omitted = 0
        self.output_bytes = 0
        self.directories_visited = 0
        self.entries_examined = 0
        self.python_files_seen = 0
        self.files_parsed = 0
        self.files_failed = 0
        self.bytes_read = 0
        self.stop_reason: str | None = None
        self.root_fd: int | None = None

    def _stop(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason
        raise _Stopped

    def _check(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            self._stop("cancelled")
        if monotonic() >= self.deadline:
            self._stop("time_budget")

    def _add(self, destination: list[JsonValue], value: JsonValue) -> bool:
        size = len(canonical_json(value)) + 2
        if self.output_bytes + size > _OUTPUT_BUDGET:
            return False
        destination.append(value)
        self.output_bytes += size
        return True

    def _exclude(self, path: str, reason: str) -> None:
        self.excluded_counts[reason] += 1
        if len(self.excluded) < _DETAIL_LIMIT:
            self._add(self.excluded, {"path": path, "reason": reason})

    def _error(self, path: str, reason: str, detail: str) -> None:
        self.error_count += 1
        if len(self.errors) < _DETAIL_LIMIT:
            self._add(
                self.errors, {"path": path, "reason": reason, "detail": detail[:512]}
            )

    @contextmanager
    def _directory(self, parts: tuple[str, ...]) -> Iterator[int]:
        assert self.root_fd is not None
        descriptor = os.dup(self.root_fd)
        try:
            for part in parts:
                self._check()
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            yield descriptor
        finally:
            os.close(descriptor)

    def _open_root(self) -> int:
        descriptor = os.open(self.workspace.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.workspace.parts[1:]:
                self._check()
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _walk(self, parts: tuple[str, ...]) -> None:
        self._check()
        if len(parts) > 64:
            self._stop("depth_budget")
        path = "/".join(parts) or "."
        try:
            with self._directory(parts) as descriptor:
                self.directories_visited += 1
                names: list[str] = []
                with os.scandir(descriptor) as iterator:
                    for entry in iterator:
                        self._check()
                        self.entries_examined += 1
                        if self.entries_examined > self.traversal_limit:
                            self._stop("traversal_budget")
                        names.append(entry.name)
                for name in sorted(names):
                    self._check()
                    relative = parts + (name,)
                    relative_path = "/".join(relative)
                    try:
                        mode = os.stat(
                            name, dir_fd=descriptor, follow_symlinks=False
                        ).st_mode
                    except OSError as error:
                        self._error(relative_path, "stat_error", str(error))
                        continue
                    if stat.S_ISLNK(mode):
                        self._exclude(relative_path, "symlink")
                    elif stat.S_ISDIR(mode):
                        if name in _EXCLUDED_DIRECTORIES:
                            self._exclude(relative_path, "excluded_directory")
                        elif name.startswith("."):
                            self._exclude(relative_path, "hidden_directory")
                        else:
                            self._walk(relative)
                    elif not stat.S_ISREG(mode):
                        self._exclude(relative_path, "non_regular")
                    elif name.startswith("."):
                        self._exclude(relative_path, "hidden_file")
                    elif not name.endswith(".py"):
                        self._exclude(relative_path, "non_python_file")
                    else:
                        self.python_files_seen += 1
                        if self.python_files_seen > self.max_files:
                            self._stop("file_budget")
                        self._scan_file(relative)
        except OSError as error:
            self._error(path, "directory_read_error", str(error))

    def _read_file(self, parts: tuple[str, ...]) -> bytes:
        remaining = self.max_total_bytes - self.bytes_read
        if remaining <= 0:
            self._stop("total_bytes_budget")
        with self._directory(parts[:-1]) as parent:
            descriptor = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("Source is no longer a regular file")
            data = stream.read(min(self.max_file_bytes + 1, remaining))
        self.bytes_read += len(data)
        return data

    def _scan_file(self, parts: tuple[str, ...]) -> None:
        path = "/".join(parts)
        record: dict[str, JsonValue] = {
            "path": path,
            "status": "pending",
            "bytes_read": 0,
            "sha256": None,
            "imports": 0,
        }
        try:
            remaining = self.max_total_bytes - self.bytes_read
            data = self._read_file(parts)
            record["bytes_read"] = len(data)
            if len(data) > self.max_file_bytes:
                record["status"] = "file_too_large"
                self.files_failed += 1
                self._error(path, "file_size_limit", "File exceeds per-file byte limit")
                return
            if len(data) == remaining:
                self._stop("total_bytes_budget")
            record["sha256"] = hashlib.sha256(data).hexdigest()
            self._check()
            tree = ast.parse(data, filename=path)
            imports: list[ast.Import | ast.ImportFrom] = []
            for node in ast.walk(tree):
                self._check()
                if isinstance(node, ast.Import | ast.ImportFrom):
                    imports.append(node)
            imports.sort(key=lambda node: (node.lineno, node.col_offset))
            count = 0
            for node in imports:
                self._check()
                entry: dict[str, JsonValue] = {
                    "path": path,
                    "line": node.lineno,
                    "column": node.col_offset,
                    "kind": "from" if isinstance(node, ast.ImportFrom) else "import",
                    "module": (node.module or "")
                    if isinstance(node, ast.ImportFrom)
                    else "",
                    "level": node.level if isinstance(node, ast.ImportFrom) else 0,
                    "names": [
                        {"name": alias.name, "alias": alias.asname}
                        for alias in node.names
                    ],
                    "normalized": ast.unparse(node),
                }
                if not self._add(self.entries, entry):
                    self._stop("output_budget")
                count += 1
                record["imports"] = count
            record["status"] = "parsed"
            self.files_parsed += 1
        except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
            record["status"] = "parse_error"
            self.files_failed += 1
            self._error(path, "parse_error", str(error))
        except OSError as error:
            record["status"] = "read_error"
            self.files_failed += 1
            self._error(path, "read_error", str(error))
        except _Stopped:
            record["status"] = self.stop_reason
            raise
        finally:
            if not self._add(self.files, record):
                self.files_metadata_omitted += 1
                self._stop("output_budget")

    def run(self) -> dict[str, JsonValue]:
        try:
            self._check()
            if os.name != "posix":
                self._stop("unsupported_platform")
            self.root_fd = self._open_root()
            self._walk(())
            self._check()
        except _Stopped:
            pass
        except OSError as error:
            self._error(".", "workspace_read_error", str(error))
        finally:
            if self.root_fd is not None:
                os.close(self.root_fd)
        complete = self.stop_reason is None and self.error_count == 0
        return {
            "schema_version": 1,
            "tool_version": "python-import-inventory/1",
            "python_version": platform.python_version(),
            "ok": complete,
            "complete": complete,
            "source": str(self.workspace),
            "summary": (
                f"Recorded {len(self.entries)} imports from "
                f"{self.files_parsed} parsed Python files; "
                f"{'complete within scope' if complete else 'partial inventory'}"
            ),
            "limitations": [
                "Inventory of static Python import declarations; no security verdict.",
                "Completeness covers regular .py files; hidden paths, symlinks, "
                "special files, .git, .venv, node_modules and __pycache__ "
                "are excluded.",
                "Dynamic imports and runtime behavior are not evaluated; source is "
                "never executed. Tests directories are included.",
                "Each file is observed separately; this is not an atomic repository "
                "snapshot. The time/cancellation budget is cooperative between "
                "bounded reads and parsing operations.",
            ],
            "entries": self.entries,
            "files": self.files,
            "coverage": {
                "directories_visited": self.directories_visited,
                "entries_examined": self.entries_examined,
                "python_files_seen": self.python_files_seen,
                "files_parsed": self.files_parsed,
                "files_failed": self.files_failed,
                "files_metadata_omitted": self.files_metadata_omitted,
                "bytes_read": self.bytes_read,
                "imports_recorded": len(self.entries),
                "excluded_counts": dict(self.excluded_counts),
                "excluded": self.excluded,
                "excluded_details_omitted": sum(self.excluded_counts.values())
                - len(self.excluded),
                "errors": self.errors,
                "errors_omitted": self.error_count - len(self.errors),
                "stop_reason": self.stop_reason,
                "limits": {
                    "max_files": self.max_files,
                    "max_file_bytes": self.max_file_bytes,
                    "max_total_bytes": self.max_total_bytes,
                    "max_traversal_entries": self.traversal_limit,
                    "max_depth": 64,
                    "seconds": _SCAN_SECONDS,
                    "detail_bytes": _OUTPUT_BUDGET,
                },
            },
        }


def inventory_python(
    workspace: Path,
    *,
    max_files: int = 500,
    max_file_bytes: int = 262_144,
    max_total_bytes: int = 8_388_608,
    cancel_event: threading.Event | None = None,
) -> dict[str, JsonValue]:
    """Inventory imports with bounded traversal, source bytes, time and result size.

    A directory listing is collected within the traversal budget before sorting.
    If it exceeds that budget, none of its arbitrary OS-order subset is scanned.
    One additional entry may be examined to establish traversal-limit exhaustion.
    Exhausting the total byte budget is reported conservatively, even if it
    coincides with EOF. POSIX no-follow directory descriptors anchor every read.
    """
    return _Inventory(
        workspace, max_files, max_file_bytes, max_total_bytes, cancel_event
    ).run()
