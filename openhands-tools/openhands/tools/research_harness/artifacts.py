"""Bounded, content-addressed files for observed results and retrieval manifests."""

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from pydantic import JsonValue


def canonical_json(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ArtifactStore:
    """Publish complete files atomically and verify their digest on every read.

    This is integrity checking and an append-only application interface. It is
    not protection against another process with write access to the same files.
    """

    def __init__(self, root: Path, max_bytes: int = 1_048_576) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if root.is_symlink():
            raise ValueError("Artifact directory must not be a symbolic link")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        self.max_bytes = max_bytes

    def put(self, data: bytes) -> str:
        if len(data) > self.max_bytes:
            raise ValueError(f"Artifact exceeds {self.max_bytes} bytes")
        digest = hashlib.sha256(data).hexdigest()
        destination = self.root / digest
        descriptor, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if self.read(digest) != data:
                    raise ValueError("Existing artifact does not match its digest")
            if os.name == "posix":
                directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def read(self, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Artifact reference must be a lowercase SHA-256 digest")
        path = self.root / digest
        if path.is_symlink():
            raise ValueError("Artifact must not be a symbolic link")
        flags = os.O_RDONLY
        if os.name == "posix":
            flags |= os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Artifact must be a regular file")
            if metadata.st_size > self.max_bytes:
                raise ValueError("Stored artifact exceeds the configured size limit")
            data = stream.read(self.max_bytes + 1)
        if len(data) > self.max_bytes or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Artifact integrity check failed")
        return data
