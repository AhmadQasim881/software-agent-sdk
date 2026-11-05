import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from openhands.tools.research_harness.artifacts import ArtifactStore, canonical_json


def test_content_addressed_artifacts_survive_reopen(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    data = b'{"observed_status":200}'
    digest = store.put(data)
    assert digest == hashlib.sha256(data).hexdigest()
    assert ArtifactStore(store.root).read(digest) == data
    assert store.put(data) == digest
    assert [p.name for p in store.root.iterdir()] == [digest]


def test_concurrent_publish_preserves_one_complete_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    data = b"observed output\n" * 100
    with ThreadPoolExecutor(max_workers=4) as workers:
        digests = list(workers.map(store.put, [data] * 12))
    assert len(set(digests)) == 1
    assert store.read(digests[0]) == data
    assert len(list(store.root.iterdir())) == 1


def test_corruption_is_reported_without_overwriting_it(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    digest = store.put(b"actual")
    (store.root / digest).write_bytes(b"altered")
    with pytest.raises(ValueError, match="integrity"):
        store.read(digest)
    with pytest.raises(ValueError, match="integrity"):
        store.put(b"actual")
    assert (store.root / digest).read_bytes() == b"altered"
    assert not list(store.root.glob(".pending-*"))


@pytest.mark.parametrize("reference", ["../outside", "a" * 63, "A" * 64, ""])
def test_invalid_artifact_reference(tmp_path: Path, reference: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        ArtifactStore(tmp_path / "artifacts").read(reference)


def test_artifact_limits_and_missing_files(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", max_bytes=4)
    with pytest.raises(ValueError, match="exceeds"):
        store.put(b"12345")
    digest = store.put(b"1234")
    with pytest.raises(ValueError, match="size limit"):
        ArtifactStore(store.root, max_bytes=3).read(digest)
    with pytest.raises(FileNotFoundError):
        store.read("0" * 64)


@pytest.mark.skipif(os.name != "posix", reason="POSIX special file checks")
def test_symlink_and_fifo_are_rejected_without_reading(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    digest = hashlib.sha256(b"outside").hexdigest()
    (store.root / digest).symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic"):
        store.read(digest)
    (store.root / digest).unlink()
    os.mkfifo(store.root / digest)
    with pytest.raises(ValueError, match="regular file"):
        store.read(digest)


def test_canonical_json_uses_bytes_and_rejects_nonfinite_values() -> None:
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})
