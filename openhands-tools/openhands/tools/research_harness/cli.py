"""Named local controls over the same service exposed to agent hosts."""

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import JsonValue, TypeAdapter, ValidationError

from .commands import OPERATION_NAMES, command_schemas
from .models import HarnessError
from .service import HarnessService


_ARGUMENTS = TypeAdapter(dict[str, JsonValue])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent local research harness")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--state", type=Path, help="State directory outside the workspace"
    )
    parser.add_argument("--allow-github", action="store_true")
    subcommands = parser.add_subparsers(dest="operation", required=True)
    for name in OPERATION_NAMES:
        command = subcommands.add_parser(
            name, help=f"Run {name}; see schema for arguments"
        )
        command.add_argument("arguments", nargs="?", default="{}", help="JSON object")
        command.add_argument(
            "--json-stdin", action="store_true", help="Read bounded JSON from stdin"
        )
    schema = subcommands.add_parser("schema", help="Inspect operation argument schemas")
    schema.add_argument("name", nargs="?")
    subcommands.add_parser("mcp", help="Serve the same operations over MCP stdio")
    recover = subcommands.add_parser(
        "recover", help="Mark a stopped worker's abandoned run interrupted"
    )
    recover.add_argument("run_id")
    recover.add_argument("reason")
    return parser


def error_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        details = error.errors(include_input=False, include_url=False)
        first = details[0]
        location = ".".join(str(part) for part in first["loc"])
        return f"Invalid arguments at {location}: {first['msg']}"[:1000]
    return str(error)[:1000]


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.operation == "schema":
            schemas = command_schemas()
            if args.name is not None and args.name not in schemas:
                raise HarnessError("Unknown operation; use schema to list operations")
            result: dict[str, JsonValue] = {
                "ok": True,
                "schemas": schemas
                if args.name is None
                else {args.name: schemas[args.name]},
            }
        else:
            if args.state is None:
                parser.error("--state is required for this operation")
            service = HarnessService(
                workspace=args.workspace,
                state_dir=args.state,
                allow_github=args.allow_github,
            )
            if args.operation == "mcp":
                from .mcp_server import create_mcp_server

                create_mcp_server(service).run(transport="stdio", show_banner=False)
                return 0
            if args.operation == "recover":
                record = service.store.recover(args.run_id, args.reason)
                result = {"ok": True, "record": record.model_dump(mode="json")}
            else:
                raw = (
                    sys.stdin.buffer.read(16_385)
                    if args.json_stdin
                    else args.arguments.encode("utf-8")
                )
                if len(raw) > 16_384:
                    raise HarnessError("Arguments exceed 16384 UTF-8 bytes")
                arguments = _ARGUMENTS.validate_json(raw, strict=True)
                result = service.execute(args.operation, arguments)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result.get("ok") is True else 1
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "error": error_message(error)}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted"}))
        return 130
