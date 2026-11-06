"""Failure checks for the container acceptance driver's evidence and ownership."""

import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github/scripts/verify_research_harness_container.py"


@pytest.fixture
def checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("container_acceptance_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def check(checker: ModuleType, tmp_path: Path) -> Any:
    root = tmp_path / "research-harness-container.test"
    root.mkdir(mode=0o700)
    (root / "reports").mkdir()
    return checker.ContainerCheck(REPO, root)


@pytest.mark.parametrize("change", ["missing", "modified"])
def test_source_archive_rejects_missing_or_changed_skill(
    checker: ModuleType, tmp_path: Path, change: str
) -> None:
    checkout = tmp_path / "checkout"
    skill = (
        "openhands-tools/openhands/tools/research_harness/"
        "skills/research-review/SKILL.md"
    )
    paths = [
        skill,
        "openhands-tools/openhands/research/__init__.py",
        "openhands-agent-server/openhands/agent_server/__init__.py",
        "openhands-agent-server/openhands/agent_server/__main__.py",
        "openhands-agent-server/openhands/agent_server/mcp_router.py",
        "openhands-agent-server/openhands/agent_server/docker/Dockerfile",
        "uv.lock",
        "pyproject.toml",
    ]
    archive_path = tmp_path / "source.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for relative in paths:
            source = checkout / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"Expected source bytes\n")
            if relative == skill and change == "missing":
                continue
            content = (
                b"Different packaged skill\n"
                if relative == skill and change == "modified"
                else source.read_bytes()
            )
            member = tarfile.TarInfo(f"source/{relative}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    with pytest.raises(checker.VerificationError, match="SKILL.md"):
        checker.source_manifest(checkout, archive_path)


def test_readiness_retries_connection_failure(
    checker: ModuleType, check: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    check.owner["run"] = "/owned/attached-run"
    results = iter(
        [
            {"ok": False, "error": "Connection refused"},
            {"ok": True, "response": {"body": {"ready": True}}},
        ]
    )
    calls = []

    def invoke(label: str, arguments: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append((label, arguments, kwargs))
        return next(results)

    monkeypatch.setattr(check, "invoke", invoke)
    monkeypatch.setattr(
        checker, "time", SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None)
    )
    assert check.api("ready", "GET", "/ready", readiness=True) == {"ready": True}
    assert len(calls) == 2
    assert all(item[2] == {"check": False} for item in calls)


def test_cleanup_refuses_an_unowned_container(
    checker: ModuleType, check: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    check.owner = {
        "token": "a" * 32,
        "name": "research-memory-" + "a" * 32,
        "image_id": "sha256:expected",
        "image_tag": "research-harness-acceptance:test",
    }
    calls = []

    def docker(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments,
            0,
            json.dumps(
                {
                    "labels": {checker.OWNER_LABEL: "different-owner"},
                    "image": "sha256:expected",
                }
            ),
            "",
        )

    monkeypatch.setattr(check, "docker", docker)
    result = check.cleanup()
    assert result["ok"] is False
    assert "unowned" in result["errors"][0]
    assert all(arguments[0] == "inspect" for arguments in calls)


def test_archive_failure_still_removes_the_verified_image(
    checker: ModuleType, check: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image = "research-harness-acceptance:test"
    (check.reports / "build-metadata.json").write_text(
        json.dumps({"containerimage.config.digest": "sha256:expected"})
    )
    calls = []

    def docker(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[0] == "inspect":
            return subprocess.CompletedProcess(arguments, 1, "", "No such container")
        if arguments[:2] == ["image", "inspect"]:
            output = (
                json.dumps(
                    {"id": "sha256:expected", "os": "linux", "architecture": "amd64"}
                )
                if arguments[3] == checker.IMAGE_FORMAT
                else "sha256:expected\n"
            )
            return subprocess.CompletedProcess(arguments, 0, output, "")
        assert arguments == ["image", "rm", image]
        return subprocess.CompletedProcess(arguments, 0, "removed", "")

    def bad_archive(*args: Any) -> dict[str, Any]:
        raise checker.VerificationError("Source archive is missing: SKILL.md")

    monkeypatch.setattr(check, "docker", docker)
    monkeypatch.setattr(checker, "source_manifest", bad_archive)
    with pytest.raises(checker.VerificationError, match="SKILL.md"):
        check.run(image, tmp_path / "unused.tar.gz")
    assert (check.root / "owner.json").exists()
    result = check.cleanup()
    assert result["ok"] is True and result["image_tag_removed"] is True
    assert ["image", "rm", image] in calls
    assert all(arguments[0] != "create" for arguments in calls)


def test_changed_image_is_never_claimed_or_deleted(
    check: Any, checker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    (check.reports / "build-metadata.json").write_text(
        json.dumps({"containerimage.config.digest": "sha256:expected"})
    )
    calls = []

    def docker(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, json.dumps({"id": "sha256:different"}), ""
        )

    monkeypatch.setattr(check, "docker", docker)
    with pytest.raises(checker.VerificationError, match="differs from build metadata"):
        check.claim_image("research-harness-acceptance:test")
    assert not check.owner
    assert not (check.root / "owner.json").exists()
    assert len(calls) == 1 and calls[0][:2] == ["image", "inspect"]


@contextmanager
def authenticated_ready_server(
    session_key: str, requests: list[int]
) -> Iterator[ThreadingHTTPServer]:
    """Own a real HTTP endpoint that rejects requests without the private key."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            authorized = self.headers.get("X-Session-API-Key") == session_key
            status = 200 if authorized and self.path == "/ready" else 401
            if authorized:
                requests.append(server.server_port)
            body = json.dumps(
                {"ready": status == 200, "port": server.server_port}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.timeout(10)
@pytest.mark.parametrize("changed_port", [True, False])
def test_restart_uses_current_binding_and_keeps_all_authenticated_evidence(
    checker: ModuleType,
    check: Any,
    monkeypatch: pytest.MonkeyPatch,
    changed_port: bool,
) -> None:
    """Use real attach/API profiles and HTTP; only the Docker boundary is faked."""
    session_key = "container-acceptance-test-key"
    check.private.mkdir(mode=0o700)
    key_file = check.private / "session_key"
    key_file.write_text(session_key)
    key_file.chmod(0o600)
    check.owner = {
        "token": "b" * 32,
        "name": "research-memory-" + "b" * 32,
        "image_id": "sha256:expected",
        "image_tag": "research-harness-acceptance:test",
    }
    requests: list[int] = []
    with ExitStack() as stack:
        first = stack.enter_context(authenticated_ready_server(session_key, requests))
        current_server = (
            stack.enter_context(authenticated_ready_server(session_key, requests))
            if changed_port
            else first
        )

        def inspection(port: int, started: str) -> dict[str, Any]:
            return {
                "id": "same-owned-container",
                "image": check.owner["image_id"],
                "labels": {checker.OWNER_LABEL: check.owner["token"]},
                "state": {"Running": True, "StartedAt": started},
                "ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]},
            }

        before = inspection(first.server_port, "2026-11-06T00:00:00Z")
        after = inspection(current_server.server_port, "2026-11-06T00:01:00Z")
        current: dict[str, Any] | None = before

        def docker(
            arguments: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            nonlocal current
            if arguments[0] == "restart":
                if changed_port:
                    # Stale-profile requests now fail against the closed old port.
                    first.shutdown()
                    first.server_close()
                current = after
            elif arguments[0] == "inspect":
                if current is None:
                    return subprocess.CompletedProcess(
                        arguments, 1, "", "No such container"
                    )
                return subprocess.CompletedProcess(
                    arguments, 0, json.dumps(current), ""
                )
            elif arguments[0] == "stop":
                current_server.shutdown()
                current_server.server_close()
            elif arguments[0] == "rm":
                current = None
            elif arguments[:2] == ["image", "inspect"]:
                return subprocess.CompletedProcess(arguments, 1, "", "No such image")
            else:
                assert arguments[0] == "logs"
            return subprocess.CompletedProcess(arguments, 0, "", "")

        monkeypatch.setattr(check, "docker", docker)
        check.attach_endpoint(before, "attach-before")
        first_profile = Path(check.owner["run"])
        assert check.api("endpoint", "GET", "/ready")["port"] == first.server_port

        check.restart_and_wait(before)
        last_profile = Path(check.owner["run"])
        assert check.owner["port"] == current_server.server_port
        assert (last_profile != first_profile) is changed_port
        assert check.report["container_before_restart"] == before
        assert check.report["container_after_restart"] == after
        assert check.api("endpoint", "GET", "/ready")["port"] == (
            current_server.server_port
        )
        assert requests == [
            first.server_port,
            current_server.server_port,
            current_server.server_port,
        ]

        profiles = {first_profile, last_profile}
        for profile in profiles:
            assert json.loads((profile / "run.json").read_text())["auth"] is True
            assert (profile / "private/session_api_key").read_text() == session_key
        result = check.cleanup()
        assert result["ok"] is True and result["port_closed"] is True
        assert set(result["verification_profiles_removed"]) == {
            profile.name for profile in profiles
        }
        assert not check.private.exists()
        assert not (check.root / "verify").exists()
        for profile, expected_port in (
            (first_profile, first.server_port),
            (last_profile, current_server.server_port),
        ):
            evidence = (
                check.reports
                / "server-evidence"
                / profile.name
                / "container/endpoint.json"
            ).read_text()
            assert json.loads(evidence)["response"]["body"]["port"] == expected_port
            assert session_key not in evidence
