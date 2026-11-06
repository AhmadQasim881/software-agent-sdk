import base64
import hashlib
import json
import os
import subprocess
import time
from collections.abc import Iterator
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Event, Thread

import psutil
import pytest

from openhands.tools.research_harness import _http_worker, network


_METADATA = {
    "full_name": "example/project",
    "private": False,
    "fork": False,
    "archived": True,
    "default_branch": "main",
}


class ResponseServer(HTTPServer):
    def __init__(self) -> None:
        self.body = json.dumps(_METADATA).encode()
        self.status = 200
        self.requests: list[dict[str, str | None]] = []
        super().__init__(("127.0.0.1", 0), ResponseHandler)


class ResponseHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        assert isinstance(self.server, ResponseServer)
        self.server.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "cookie": self.headers.get("Cookie"),
            }
        )
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(self.server.body)))
        self.send_header("Location", "https://unapproved.invalid/redirect")
        self.end_headers()
        self.wfile.write(self.server.body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def github_response(monkeypatch: pytest.MonkeyPatch) -> Iterator[ResponseServer]:
    server = ResponseServer()
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()

    def connect(host: str, *, timeout: float) -> HTTPConnection:
        assert host == "api.github.com"
        return HTTPConnection("127.0.0.1", server.server_address[1], timeout=timeout)

    monkeypatch.setattr(_http_worker, "HTTPSConnection", connect)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def worker_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[subprocess.Popen[bytes]], Event]:
    workers: list[subprocess.Popen[bytes]] = []
    started = Event()
    launch = subprocess.Popen

    def record(*args, **kwargs) -> subprocess.Popen[bytes]:
        worker = launch[bytes](*args, **kwargs)
        workers.append(worker)
        started.set()
        return worker

    monkeypatch.setattr(network.subprocess, "Popen", record)
    return workers, started


def _worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    script = tmp_path / "worker.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(network, "_WORKER_PATH", script)


def test_github_metadata_preserves_the_observed_public_record(
    github_response: ResponseServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-sent")
    result = _http_worker.fetch_metadata("Example/project")
    assert result["ok"] is True
    assert result["metadata"] == _METADATA
    assert result["source"] == "https://api.github.com/repos/Example/project"
    encoded = result["response_body_base64"]
    assert isinstance(encoded, str)
    assert base64.b64decode(encoded) == github_response.body
    assert result["response_sha256"] == hashlib.sha256(github_response.body).hexdigest()
    assert result["retrieved_at"]
    assert github_response.requests == [
        {"path": "/repos/Example/project", "authorization": None, "cookie": None}
    ]


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({**_METADATA, "full_name": "someone/else"}).encode(),
        json.dumps({**_METADATA, "fork": "false"}).encode(),
        b"not-json",
    ],
)
def test_github_metadata_rejects_bad_identity_or_payload(
    github_response: ResponseServer, body: bytes
) -> None:
    github_response.body = body
    result = _http_worker.fetch_metadata("example/project")
    assert result["ok"] is False
    assert result["metadata"] == {}
    encoded = result["response_body_base64"]
    assert isinstance(encoded, str)
    assert base64.b64decode(encoded) == body


@pytest.mark.parametrize("status", [302, 403])
def test_github_metadata_does_not_follow_redirects_or_retry(
    github_response: ResponseServer, status: int
) -> None:
    github_response.status = status
    result = _http_worker.fetch_metadata("example/project")
    assert result["ok"] is False
    assert result["http_status"] == status
    assert len(github_response.requests) == 1


def test_github_metadata_records_oversize_response_as_incomplete(
    github_response: ResponseServer,
) -> None:
    github_response.body = b"x" * (256 * 1024 + 1)
    result = _http_worker.fetch_metadata("example/project")
    assert result["ok"] is False
    assert result["response_truncated"] is True
    assert result["response_bytes"] == 256 * 1024
    encoded = result["response_body_base64"]
    assert isinstance(encoded, str)
    assert len(base64.b64decode(encoded)) == 256 * 1024


@pytest.mark.parametrize(
    "repository", ["../project", "https://github.com/x/y", "x/y/z", "x/y?token=secret"]
)
def test_github_metadata_rejects_non_repository_inputs(repository: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        network.github_metadata(repository)


def test_worker_receives_no_inherited_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-value")
    monkeypatch.setenv("GITHUB_TOKEN", "private-test-value")
    monkeypatch.setenv("HTTPS_PROXY", "http://private-test-value.invalid")
    _worker(
        tmp_path,
        monkeypatch,
        "import json, os\n"
        "print(json.dumps({'ok': True, 'summary': 'Observed worker environment.', "
        "'source': 'https://api.github.com/repos/example/project', "
        "'limitations': [], 'environment': dict(os.environ)}))\n",
    )
    result = network.github_metadata("example/project")
    assert result["ok"] is True
    environment = result["environment"]
    assert isinstance(environment, dict)
    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "HTTPS_PROXY" not in environment


@pytest.mark.parametrize("cancelled", [False, True])
def test_deadline_and_cancellation_reap_the_running_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_monitor: tuple[list[subprocess.Popen[bytes]], Event],
    cancelled: bool,
) -> None:
    _worker(tmp_path, monkeypatch, "import time\ntime.sleep(30)\n")
    monkeypatch.setattr(network, "REQUEST_SECONDS", 2.0 if cancelled else 0.2)
    workers, launched = worker_monitor
    cancel = Event()

    def cancel_after_start() -> None:
        if launched.wait(timeout=2):
            cancel.set()

    canceller = Thread(target=cancel_after_start) if cancelled else None
    if canceller is not None:
        canceller.start()
    started = time.monotonic()
    try:
        result = network.github_metadata("example/project", cancel_event=cancel)
    finally:
        if canceller is not None:
            canceller.join()
    assert time.monotonic() - started < 5
    assert result["ok"] is False
    assert result["cancelled"] is cancelled
    assert workers and all(worker.poll() is not None for worker in workers)
    if not cancelled:
        assert "deadline" in str(result["summary"])


def test_worker_output_limit_does_not_wait_for_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_monitor: tuple[list[subprocess.Popen[bytes]], Event],
) -> None:
    _worker(
        tmp_path,
        monkeypatch,
        "import sys, time\nsys.stdout.buffer.write(b'x' * 1048576)\n"
        "sys.stdout.flush()\ntime.sleep(30)\n",
    )
    monkeypatch.setattr(network, "REQUEST_SECONDS", 2.0)
    result = network.github_metadata("example/project")
    assert result["ok"] is False
    assert "output limit" in str(result["summary"])
    workers, _ = worker_monitor
    assert workers and all(worker.poll() is not None for worker in workers)


def test_worker_cancelled_before_start() -> None:
    cancel = Event()
    cancel.set()
    result = network.github_metadata("example/project", cancel_event=cancel)
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["http_status"] is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX owned process groups")
@pytest.mark.parametrize("leader_exits", [False, True])
def test_worker_cleanup_terminates_descendant_holding_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leader_exits: bool
) -> None:
    pid_file = tmp_path / "child.pid"
    _worker(
        tmp_path,
        monkeypatch,
        "import json, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-I', '-S', '-c', "
        "'import time; time.sleep(10)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "print(json.dumps({'ok': True, 'summary': 'Owned worker fixture.', "
        "'source': 'https://api.github.com/repos/example/project', "
        "'limitations': []}), flush=True)\n"
        + ("" if leader_exits else "time.sleep(10)\n"),
    )
    monkeypatch.setattr(network, "REQUEST_SECONDS", 2.0 if leader_exits else 0.2)
    done = Event()
    rescued: list[bool] = []

    def rescue() -> None:
        for _ in range(100):
            if pid_file.exists() or done.wait(0.01):
                break
        if not done.wait(1.0) and pid_file.exists():
            rescued.append(True)
            try:
                psutil.Process(int(pid_file.read_text())).kill()
            except psutil.NoSuchProcess:
                pass

    guardian = Thread(target=rescue)
    guardian.start()
    try:
        result = network.github_metadata("example/project")
    finally:
        done.set()
        guardian.join()
        if pid_file.exists():
            try:
                child = psutil.Process(int(pid_file.read_text()))
                alive = child.status() != psutil.STATUS_ZOMBIE
                if alive:
                    child.kill()
            except psutil.NoSuchProcess:
                alive = False
        else:
            alive = False
    assert not rescued, "Worker cleanup needed the test's independent rescue"
    assert not alive, "Worker cleanup returned with a live descendant"
    assert result["ok"] is leader_exits
