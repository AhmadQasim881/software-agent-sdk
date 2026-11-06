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
_EVALUATION_OUTPUT_BYTES = 65_536


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
    subcommands.add_parser("skill-path", help="Locate the packaged research workflow")
    subcommands.add_parser("mcp", help="Serve the same operations over MCP stdio")
    hook = subcommands.add_parser("hook", help="Handle one native SDK hook event")
    hook.add_argument("--thread", required=True)
    host_config = subcommands.add_parser(
        "host-config", help="Write a new, reviewable host configuration bundle"
    )
    host_config.add_argument(
        "--host", choices=("codex", "claude-code", "claude-desktop"), required=True
    )
    host_config.add_argument("--output", type=Path, required=True)
    host_config.add_argument("--python", type=Path)
    host_config.add_argument("--thread")
    host_hook = subcommands.add_parser(
        "host-hook", help="Handle one host-specific lifecycle event"
    )
    host_hook.add_argument("--host", choices=("claude-code",), required=True)
    host_hook.add_argument("--thread", required=True)
    evaluation = subcommands.add_parser(
        "evaluate-skill", help="Run a bounded evaluation with an explicit model budget"
    )
    evaluation.add_argument("--model", required=True)
    evaluation.add_argument("--api-key-env", default="LLM_API_KEY")
    evaluation.add_argument("--max-budget-usd", type=float, required=True)
    evaluation.add_argument("--max-iterations", type=int, default=8)
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
        if args.operation == "skill-path":
            from .workflow import workflow_path

            result: dict[str, JsonValue] = {"ok": True, "path": str(workflow_path())}
        elif args.operation == "schema":
            schemas = command_schemas()
            if args.name is not None and args.name not in schemas:
                raise HarnessError("Unknown operation; use schema to list operations")
            result = {
                "ok": True,
                "schemas": schemas
                if args.name is None
                else {args.name: schemas[args.name]},
            }
        elif args.operation == "evaluate-skill":
            from .evaluation import evaluate_from_environment

            result = evaluate_from_environment(
                model=args.model,
                api_key_env=args.api_key_env,
                max_budget_usd=args.max_budget_usd,
                max_iterations=args.max_iterations,
            )
        else:
            if args.state is None:
                parser.error("--state is required for this operation")
            if args.operation == "hook":
                from .hooks import run_hook

                return run_hook(
                    workspace=args.workspace,
                    state_dir=args.state,
                    thread_id=args.thread,
                )
            if args.operation == "host-hook":
                from .hosts import run_host_hook

                return run_host_hook(
                    host=args.host,
                    workspace=args.workspace,
                    state_dir=args.state,
                    thread_id=args.thread,
                )
            if args.operation == "host-config":
                from .hosts import write_host_bundle

                result = write_host_bundle(
                    host=args.host,
                    workspace=args.workspace,
                    state_dir=args.state,
                    destination=args.output,
                    python_executable=args.python,
                    thread_id=args.thread,
                    allow_github=args.allow_github,
                )
            else:
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
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if args.operation == "evaluate-skill":
            if len(encoded.encode("utf-8")) > _EVALUATION_OUTPUT_BYTES:
                raise HarnessError("Evaluation result exceeds 65536 UTF-8 bytes")
            print(encoded)
            return (
                0
                if result.get("ok") is True and result.get("status") != "blocked"
                else 1
            )
        print(encoded)
        return 0 if result.get("ok") is True else 1
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "error": error_message(error)}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted"}))
        return 130
