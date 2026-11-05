"""An owned loopback application for demonstrating normal-operation validation."""

import base64
import hashlib
import json
import socket
import time
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Thread
from uuid import uuid4

from pydantic import JsonValue


_MAX_RESPONSE_BYTES = 4096


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        assert isinstance(self.server, _FixtureServer)
        if self.path != "/items/demo":
            self.send_error(404)
            return
        body = json.dumps(
            {
                "application": "openhands-research-fixture",
                "instance_id": self.server.instance_id,
                "item": {"id": "demo", "value": "ready"},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class _FixtureServer(HTTPServer):
    def __init__(self) -> None:
        self.instance_id = uuid4().hex
        super().__init__(("127.0.0.1", 0), _FixtureHandler)

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, address = super().get_request()
        connection.settimeout(0.5)
        return connection, address


def _read_item(port: int) -> tuple[int, bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=1.0)
    try:
        connection.request("GET", "/items/demo", headers={"Connection": "close"})
        response = connection.getresponse()
        return response.status, response.read(_MAX_RESPONSE_BYTES + 1)
    finally:
        connection.close()


def _cleanup(server: _FixtureServer, thread: Thread | None) -> dict[str, JsonValue]:
    errors: list[JsonValue] = []
    started = thread is not None and thread.ident is not None
    try:
        if started:
            server.shutdown()
    except OSError as error:
        errors.append(f"Server shutdown failed: {type(error).__name__}.")
    try:
        server.server_close()
    except OSError as error:
        errors.append(f"Socket close failed: {type(error).__name__}.")
    if thread is not None and started:
        thread.join(timeout=2.0)
    thread_stopped = thread is None or not thread.is_alive()
    server_closed = server.socket.fileno() == -1
    return {
        "required": True,
        "completed": thread_stopped and server_closed and not errors,
        "server_closed": server_closed,
        "thread_stopped": thread_stopped,
        "errors": errors,
    }


def validate_fixture(*, cancel_event: Event | None = None) -> dict[str, JsonValue]:
    """Prove a normal operation on a trusted fixture, then release its resources."""
    started = time.monotonic()
    result: dict[str, JsonValue] = {
        "ok": False,
        "cancelled": False,
        "summary": "Local fixture did not complete validation.",
        "source": "builtin:owned-loopback-fixture",
        "limitations": [
            "Validates only this trusted built-in fixture, not another target.",
            "Loopback binding is not an operating-system sandbox.",
        ],
        "attempted_at": datetime.now(UTC).isoformat(),
        "readiness": {
            "http_ok": False,
            "identity_verified": False,
            "normal_operation_verified": False,
        },
        "cleanup": {"required": False, "completed": True},
    }
    if cancel_event is not None and cancel_event.is_set():
        result.update(
            cancelled=True, summary="Local validation cancelled before start."
        )
        return result

    server: _FixtureServer | None = None
    thread: Thread | None = None
    ready = False
    cleanup: dict[str, JsonValue] = {"required": False, "completed": True}
    try:
        server = _FixtureServer()
        result["instance_id"] = server.instance_id
        port = server.server_address[1]
        result["source"] = f"http://127.0.0.1:{port}/items/demo"
        thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            name="research-harness-fixture",
        )
        thread.start()
        if cancel_event is None or not cancel_event.is_set():
            status, observed = _read_item(port)
            body = observed[:_MAX_RESPONSE_BYTES]
            result["response"] = {
                "status": status,
                "body_base64": base64.b64encode(body).decode("ascii"),
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "truncated": len(observed) > _MAX_RESPONSE_BYTES,
            }
            if len(observed) > _MAX_RESPONSE_BYTES:
                raise ValueError("Fixture response exceeded its body limit.")
            payload: object = json.loads(body.decode("utf-8"))
            identity = (
                isinstance(payload, dict)
                and payload.get("application") == "openhands-research-fixture"
                and payload.get("instance_id") == server.instance_id
            )
            normal = isinstance(payload, dict) and payload.get("item") == {
                "id": "demo",
                "value": "ready",
            }
            result["readiness"] = {
                "http_ok": status == 200,
                "identity_verified": identity,
                "normal_operation_verified": normal,
            }
            ready = status == 200 and identity and normal
            if not ready:
                result["summary"] = (
                    "Local response did not prove the expected application "
                    "and normal operation."
                )
    except (OSError, HTTPException, ValueError, RuntimeError) as error:
        result["summary"] = f"Local validation failed: {type(error).__name__}."
    finally:
        if server is not None:
            cleanup = _cleanup(server, thread)
        result["cleanup"] = cleanup
    if cancel_event is not None and cancel_event.is_set():
        result.update(cancelled=True, summary="Local validation cancelled.")
    elif ready and cleanup["completed"] is True:
        result.update(
            ok=True,
            summary="Owned local fixture completed its normal operation and stopped.",
        )
    elif ready:
        result["summary"] = "Local operation passed, but fixture cleanup failed."
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result
