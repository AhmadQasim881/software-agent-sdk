"""Real MCP messages, process restarts, and cooperative request cancellation."""

import asyncio
import json
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolRequestParams, CallToolResult
from pydantic import JsonValue

from openhands.tools.research_harness import service as service_module
from openhands.tools.research_harness.agentmemory import MemoryRecord
from openhands.tools.research_harness.mcp_server import create_mcp_server
from openhands.tools.research_harness.service import HarnessService


@asynccontextmanager
async def server_session(workspace: Path, state: Path) -> AsyncIterator[ClientSession]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-I",
            "-m",
            "openhands.research",
            "--workspace",
            str(workspace),
            "--state",
            str(state),
            "mcp",
        ],
        env={"OPENHANDS_SUPPRESS_BANNER": "1"},
        cwd=workspace,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=30)
        ) as session:
            await session.initialize()
            yield session


async def invoke(
    session: ClientSession, operation: str, arguments: dict[str, JsonValue]
) -> CallToolResult:
    return await session.call_tool(
        "research_harness", {"operation": operation, "arguments": arguments}
    )


def payload(result: CallToolResult) -> dict[str, JsonValue]:
    assert result.structuredContent is not None
    return result.structuredContent


async def test_stdio_schema_fixture_failure_and_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    (workspace / "example.py").write_text("import pathlib\n")
    async with server_session(workspace, state) as session:
        tools = await session.list_tools()
        assert [tool.name for tool in tools.tools] == ["research_harness"]
        assert set(tools.tools[0].inputSchema["properties"]) == {
            "operation",
            "arguments",
        }
        schema = await invoke(session, "schema", {})
        assert not schema.isError
        operations = payload(schema)["operations"]
        assert isinstance(operations, dict)
        assert {"fixture", "inventory", "context", "artifact.read"} <= set(operations)

        created = await invoke(session, "thread.create", {"question": "Check fixture"})
        record = payload(created)["record"]
        assert isinstance(record, dict)
        thread_id = str(record["id"])
        fixture = await invoke(session, "fixture", {"thread_id": thread_id})
        assert not fixture.isError
        observed = payload(fixture)
        assert observed["status"] == "completed"
        artifact = await invoke(
            session,
            "artifact.read",
            {"digest": observed["artifact_sha"], "limit": 4096},
        )
        saved = payload(artifact)
        assert saved["encoding"] == "utf-8" and saved["eof"] is True
        fixture_detail = json.loads(str(saved["content"]))
        assert fixture_detail["readiness"]["normal_operation_verified"] is True
        assert fixture_detail["cleanup"]["completed"] is True
        assert fixture_detail["cleanup"]["thread_stopped"] is True

        (workspace / "broken.py").write_text("from invalid import (\n")
        failed = await invoke(session, "inventory", {"thread_id": thread_id})
        assert failed.isError is True
        failure = payload(failed)
        assert failure["ok"] is False and failure["status"] == "failed"
        assert failure["evidence_id"] and failure["artifact_sha"]
        invalid = await invoke(
            session,
            "inventory",
            {"thread_id": thread_id, "workspace": str(tmp_path)},
        )
        assert invalid.isError is True
        assert payload(invalid)["ok"] is False
        assert "workspace" in str(payload(invalid)["error"])
        unknown = await invoke(session, "invented.operation", {})
        assert unknown.isError is True
        schema_error = await invoke(session, "schema", {"unexpected": True})
        assert schema_error.isError is True

    async with server_session(workspace, state) as session:
        restored = payload(
            await invoke(session, "thread.get", {"thread_id": thread_id})
        )
        assert restored["record"] == record
        evidence = payload(
            await invoke(
                session, "evidence.get", {"evidence_id": failure["evidence_id"]}
            )
        )["record"]
        assert isinstance(evidence, dict)
        assert evidence["artifact_sha"] == failure["artifact_sha"]
        run = payload(await invoke(session, "run.get", {"run_id": failure["run_id"]}))[
            "record"
        ]
        assert isinstance(run, dict)
        assert run["status"] == "failed"
        assert isinstance(run["session_id"], str) and run["session_id"]


async def test_stdio_memory_restart_preserves_provenance_and_lifecycle(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("import json\n")
    state = tmp_path / "state"
    async with server_session(workspace, state) as session:
        created = payload(
            await invoke(
                session, "thread.create", {"question": "Keep the json baseline"}
            )
        )["record"]
        assert isinstance(created, dict)
        thread_id = str(created["id"])
        observed = payload(await invoke(session, "inventory", {"thread_id": thread_id}))
        evidence_id = str(observed["evidence_id"])
        remembered_result = await invoke(
            session,
            "memory.remember",
            {
                "thread_id": thread_id,
                "text": "Use the json inventory as the baseline.",
                "source": "Operator review over MCP",
                "evidence_ids": [evidence_id],
                "kind": "decision",
            },
        )
        assert not remembered_result.isError
        remembered = MemoryRecord.model_validate(payload(remembered_result)["record"])

    reference: dict[str, JsonValue] = {
        "thread_id": thread_id,
        "memory_id": remembered.id,
    }
    async with server_session(workspace, state) as session:
        fetched = await invoke(session, "memory.get", reference)
        assert not fetched.isError
        assert MemoryRecord.model_validate(payload(fetched)["record"]) == remembered
        recalled = payload(
            await invoke(
                session, "memory.recall", {"thread_id": thread_id, "query": "json"}
            )
        )
        matches = recalled["records"]
        assert isinstance(matches, list) and len(matches) == 1
        match = matches[0]
        assert isinstance(match, dict)
        restored = MemoryRecord.model_validate(match["record"])
        assert restored.source == "Operator review over MCP"
        assert restored.evidence_ids == [evidence_id]
        updated_result = await invoke(
            session,
            "memory.update",
            {
                **reference,
                "expected_version": 1,
                "text": "Compare new json imports to this baseline.",
            },
        )
        assert not updated_result.isError
        updated = MemoryRecord.model_validate(payload(updated_result)["record"])
        assert updated.version == 2
        stale = await invoke(
            session,
            "memory.update",
            {**reference, "expected_version": 1, "text": "Stale edit"},
        )
        assert stale.isError and payload(stale)["ok"] is False
        forgotten = await invoke(
            session,
            "memory.forget",
            {
                **reference,
                "expected_version": 2,
                "reason": "Replaced by the next inventory",
            },
        )
        assert not forgotten.isError
        tombstone = MemoryRecord.model_validate(payload(forgotten)["record"])
        assert tombstone.status == "forgotten" and tombstone.version == 3
        history = payload(await invoke(session, "memory.history", reference))
        assert history["records"] == [
            record.model_dump(mode="json")
            for record in (remembered, updated, tombstone)
        ]
        hidden = payload(
            await invoke(session, "memory.recall", {"thread_id": thread_id})
        )
        assert hidden["records"] == []
    resumed = HarnessService(workspace=workspace, state_dir=state)
    assert resumed.execute("memory.get", reference)["record"] == tombstone.model_dump(
        mode="json"
    )


class _FinishedMiddleware(Middleware):
    def __init__(self, finished: asyncio.Event) -> None:
        self.finished = finished

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        finally:
            self.finished.set()


async def test_protocol_cancellation_signals_and_drains_owned_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = HarnessService(workspace=workspace, state_dir=tmp_path / "state")
    thread = service.store.create_thread("Cancel a pending local operation")
    started = threading.Event()
    cancellation_seen = threading.Event()
    release_cleanup = threading.Event()
    worker_finished = threading.Event()
    handler_finished = asyncio.Event()
    request_ids: list[str | int] = []
    real_fixture = service_module.validate_fixture

    def delayed_fixture(*, cancel_event: threading.Event | None = None):
        assert cancel_event is not None
        request_context = get_context().request_context
        assert request_context is not None
        request_ids.append(request_context.request_id)
        started.set()
        try:
            if not cancel_event.wait(5):
                raise TimeoutError("The worker did not receive cancellation")
            cancellation_seen.set()
            if not release_cleanup.wait(5):
                raise TimeoutError("Test did not release cleanup")
            return real_fixture(cancel_event=cancel_event)
        finally:
            worker_finished.set()

    monkeypatch.setattr(service_module, "validate_fixture", delayed_fixture)
    server = create_mcp_server(service)
    server.add_middleware(_FinishedMiddleware(handler_finished))
    async with Client(server) as client:
        call = asyncio.create_task(
            client.call_tool_mcp(
                "research_harness",
                {"operation": "fixture", "arguments": {"thread_id": thread.id}},
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await client.cancel(request_ids[0], "Test cancellation")
            assert await asyncio.to_thread(cancellation_seen.wait, 5)
            assert not worker_finished.is_set()
            assert not handler_finished.is_set()
            # MCP acknowledges cancellation immediately; the handler must still
            # own and drain its worker before unwinding middleware/transport.
            with pytest.raises(McpError, match="cancel"):
                await asyncio.wait_for(call, timeout=5)
        finally:
            release_cleanup.set()
            await asyncio.wait_for(handler_finished.wait(), timeout=5)
        assert worker_finished.is_set()
    runs = service.store.list_runs(thread.id)
    assert len(runs) == 1 and runs[0].status == "interrupted"
    assert len(service.store.list_evidence(thread.id)) == 1
    assert not any(
        event.kind == "run.completed"
        for event in service.store.list_events(run_id=runs[0].id)
    )
