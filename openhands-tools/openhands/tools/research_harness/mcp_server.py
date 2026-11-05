"""Structured MCP access to the shared, bounded research service."""

import asyncio
import sqlite3
import threading

import anyio
from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from mcp.types import CallToolResult, TextContent
from pydantic import JsonValue

from .artifacts import canonical_json
from .models import HarnessError
from .service import HarnessService


class _Outcome(ToolResult):
    """Preserve structured failure details at the MCP protocol boundary."""

    def to_mcp_result(self) -> CallToolResult:
        return CallToolResult(
            content=self.content,
            structuredContent=self.structured_content,
            isError=(self.structured_content or {}).get("ok") is False,
        )


def create_mcp_server(service: HarnessService) -> FastMCP:
    """Create a server; its caller owns transport startup and configuration."""
    server = FastMCP(
        "openhands-research-harness",
        version="1",
        instructions=(
            "Call research_harness with operation='schema' and arguments={} for "
            "operation schemas. The server uses its configured local workspace "
            "and state directory. Observations retain evidence retrieval references."
        ),
        strict_input_validation=True,
    )

    @server.tool(
        name="research_harness",
        output_schema=None,
        description=(
            "Run one bounded research operation with structured arguments. "
            "Use operation='schema', arguments={} to discover supported operations."
        ),
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    )
    async def research_harness(
        operation: str, arguments: dict[str, JsonValue], ctx: Context
    ) -> _Outcome:
        cancel_event = threading.Event()
        session_id = ctx.session_id

        def execute() -> dict[str, JsonValue]:
            try:
                if operation == "schema":
                    if arguments:
                        raise HarnessError("The schema operation takes no arguments")
                    return {"ok": True, "operations": service.schema()}
                return service.execute(
                    operation,
                    arguments,
                    session_id=session_id,
                    cancel_event=cancel_event,
                )
            except (ValueError, OSError, sqlite3.Error) as error:
                return {
                    "ok": False,
                    "error_type": type(error).__name__,
                    "error": str(error)[:2048],
                }

        worker = asyncio.create_task(asyncio.to_thread(execute))
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()
            # Keep ownership until the worker has honored cancellation and
            # persisted its outcome. AnyIO cancellation is level-triggered.
            with anyio.CancelScope(shield=True):
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            # Retrieve any exception after draining; the request's cancellation
            # remains the outcome even if cleanup also encountered an error.
            if not worker.cancelled():
                worker.exception()
            raise
        return _Outcome(
            content=[TextContent(type="text", text=canonical_json(result).decode())],
            structured_content=result,
        )

    return server
