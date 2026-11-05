import base64
import json
import socket
from threading import Event
from urllib.parse import urlsplit

import pytest
from pydantic import JsonValue

from openhands.tools.research_harness import fixture


def _assert_port_closed(result: dict[str, JsonValue]) -> None:
    source = result["source"]
    assert isinstance(source, str)
    address = urlsplit(source)
    assert address.hostname == "127.0.0.1"
    assert address.port is not None
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", address.port), timeout=0.1):
            pass


def test_fixture_proves_normal_operation_and_releases_its_port() -> None:
    result = fixture.validate_fixture()
    assert result["ok"] is True
    assert result["readiness"] == {
        "http_ok": True,
        "identity_verified": True,
        "normal_operation_verified": True,
    }
    response = result["response"]
    assert isinstance(response, dict)
    encoded = response["body_base64"]
    assert isinstance(encoded, str)
    payload = json.loads(base64.b64decode(encoded))
    assert payload["instance_id"] == result["instance_id"]
    assert payload["item"] == {"id": "demo", "value": "ready"}
    cleanup = result["cleanup"]
    assert isinstance(cleanup, dict)
    assert cleanup["completed"] is True
    _assert_port_closed(result)


@pytest.mark.parametrize(
    ("fault", "failed_check"),
    [
        ("identity", "identity_verified"),
        ("item", "normal_operation_verified"),
        ("status", "http_ok"),
        ("malformed", "normal_operation_verified"),
    ],
)
def test_fixture_rejects_bad_response_and_still_stops(
    monkeypatch: pytest.MonkeyPatch, fault: str, failed_check: str
) -> None:
    class FaultHandler(fixture._FixtureHandler):
        def do_GET(self) -> None:
            assert isinstance(self.server, fixture._FixtureServer)
            payload = {
                "application": "openhands-research-fixture",
                "instance_id": (
                    "another-instance"
                    if fault == "identity"
                    else self.server.instance_id
                ),
                "item": {
                    "id": "demo",
                    "value": "wrong" if fault == "item" else "ready",
                },
            }
            body = b"not-json" if fault == "malformed" else json.dumps(payload).encode()
            self.send_response(503 if fault == "status" else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    monkeypatch.setattr(fixture, "_FixtureHandler", FaultHandler)
    result = fixture.validate_fixture()
    assert result["ok"] is False
    readiness = result["readiness"]
    assert isinstance(readiness, dict)
    assert readiness[failed_check] is False
    cleanup = result["cleanup"]
    assert isinstance(cleanup, dict)
    assert cleanup["completed"] is True
    assert "response" in result
    _assert_port_closed(result)


def test_fixture_reports_cleanup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    close = fixture._FixtureServer.server_close

    def failing_close(server: fixture._FixtureServer) -> None:
        close(server)
        raise OSError("Controlled close failure after releasing the socket")

    monkeypatch.setattr(fixture._FixtureServer, "server_close", failing_close)
    result = fixture.validate_fixture()
    assert result["ok"] is False
    cleanup = result["cleanup"]
    assert isinstance(cleanup, dict)
    assert cleanup["completed"] is False
    assert cleanup["server_closed"] is True
    assert cleanup["thread_stopped"] is True
    assert cleanup["errors"]
    _assert_port_closed(result)


def test_fixture_cancelled_before_start() -> None:
    cancel = Event()
    cancel.set()
    result = fixture.validate_fixture(cancel_event=cancel)
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["cleanup"] == {"required": False, "completed": True}
    assert "response" not in result


def test_fixture_cancellation_after_response_still_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = Event()
    read = fixture._read_item

    def read_then_cancel(port: int) -> tuple[int, bytes]:
        response = read(port)
        cancel.set()
        return response

    monkeypatch.setattr(fixture, "_read_item", read_then_cancel)
    result = fixture.validate_fixture(cancel_event=cancel)
    assert result["ok"] is False
    assert result["cancelled"] is True
    cleanup = result["cleanup"]
    assert isinstance(cleanup, dict)
    assert cleanup["completed"] is True
    _assert_port_closed(result)
