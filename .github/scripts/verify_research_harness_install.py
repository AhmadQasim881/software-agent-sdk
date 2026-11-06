"""Verify a tools wheel in an explicitly selected runtime, without model calls.

The caller creates the runtime and installs its locked dependencies and wheel.
This runner re-execs with that interpreter in isolated mode, checks provenance,
and owns all smoke-test state. The SDK dependency may remain editable.
"""

import argparse
import asyncio
import base64
import hashlib
import importlib
import importlib.metadata
import json
import os
import pkgutil
import re
import subprocess
import sys
import sysconfig
import tomllib
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


PUBLIC_REPOSITORY = "AhmadQasim881/software-agent-sdk"
MEMORY_OPERATIONS = {
    "memory.remember",
    "memory.get",
    "memory.update",
    "memory.forget",
    "memory.recall",
    "memory.history",
}


class VerificationError(RuntimeError):
    """A required acceptance check did not pass."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def verify_installation(checkout: Path, wheel: Path) -> dict[str, Any]:
    """Reject editable tools and verify the shipped files against source and wheel."""
    distribution = importlib.metadata.distribution("openhands-tools")
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    require(
        "archive_info" in direct_url and "dir_info" not in direct_url,
        "openhands-tools must be installed from the wheel, not an editable checkout",
    )
    site = Path(sysconfig.get_path("purelib")).resolve()
    source_root = checkout / "openhands-tools"
    package_roots = ("openhands/tools/research_harness", "openhands/research")
    source_files = {
        path.relative_to(source_root).as_posix(): path
        for relative in package_roots
        for path in (source_root / relative).rglob("*")
        if path.is_file() and path.suffix in {".py", ".md"}
    }
    require(bool(source_files), "No research harness source files were found")
    verified = []
    with zipfile.ZipFile(wheel) as archive:
        for name, source in sorted(source_files.items()):
            require(name in archive.namelist(), f"Wheel is missing {name}")
            installed = Path(distribution.locate_file(name)).resolve()
            require(installed.is_relative_to(site), f"{name} is outside site-packages")
            expected = source.read_bytes()
            require(
                archive.read(name) == expected, f"Wheel differs from source: {name}"
            )
            require(
                installed.read_bytes() == expected, f"Installed file differs: {name}"
            )
            verified.append(name)

    package = importlib.import_module("openhands.tools.research_harness")
    for info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        importlib.import_module(info.name)
    importlib.import_module("openhands.research")
    module_paths = {}
    for name, module in list(sys.modules.items()):
        if name == "openhands.research" or name.startswith("openhands.tools."):
            spec = module.__spec__
            if spec is not None and spec.origin not in {None, "built-in", "frozen"}:
                origin = Path(spec.origin).resolve()
                require(
                    origin.is_relative_to(site), f"Module {name} is outside runtime"
                )
                module_paths[name] = str(origin)

    lock = tomllib.loads((checkout / "uv.lock").read_text(encoding="utf-8"))
    locked: dict[str, set[str]] = {}
    for package in lock["package"]:
        locked.setdefault(normalized(package["name"]), set()).add(package["version"])
    installed_versions = []
    for item in importlib.metadata.distributions():
        name = item.metadata["Name"]
        require(
            item.version in locked.get(normalized(name), set()),
            f"Installed version is absent from uv.lock: {name}=={item.version}",
        )
        installed_versions.append({"name": name, "version": item.version})
    sdk = importlib.metadata.distribution("openhands-sdk")
    return {
        "tools_direct_url": direct_url,
        "sdk_direct_url": json.loads(sdk.read_text("direct_url.json") or "{}"),
        "dependency_scope": "Tools wheel; SDK may be editable from this checkout",
        "version_check_scope": "Installed versions match uv.lock; not artifact hashes",
        "wheel": str(wheel),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "verified_files": verified,
        "installed_module_paths": module_paths,
        "distributions": sorted(installed_versions, key=lambda item: item["name"]),
    }


class Smoke:
    def __init__(self, python: Path, root: Path, report: dict[str, Any]) -> None:
        self.python = python
        self.root = root
        self.workspace = root / "workspace"
        self.state = root / "state"
        self.workspace.mkdir()
        (self.workspace / "sample.py").write_text(
            "import json\nfrom pathlib import Path\n", encoding="utf-8"
        )
        self.report = report
        self.report.update(cli=[], hosts={}, mcp=[])

    def command(
        self,
        arguments: list[str],
        *,
        console: bool = False,
        expected_code: int = 0,
    ) -> dict[str, Any]:
        executable = (
            [str(self.python.parent / "openhands-research")]
            if console
            else [str(self.python), "-I", "-m", "openhands.research"]
        )
        result = subprocess.run(
            [
                *executable,
                "--workspace",
                str(self.workspace),
                "--state",
                str(self.state),
                *arguments,
            ],
            cwd=self.workspace,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.report["cli"].append(
            {
                "arguments": arguments,
                "console_entry_point": console,
                "returncode": result.returncode,
            }
        )
        require(result.returncode == expected_code, f"CLI {arguments[0]} exit status")
        value = json.loads(result.stdout)
        require(isinstance(value, dict), f"CLI {arguments[0]} did not return an object")
        require(value.get("ok") is (expected_code == 0), f"CLI {arguments[0]} outcome")
        return value

    def operation(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.command([name, json.dumps(arguments)])

    def run(self) -> str:
        # These imports must occur only after the selected interpreter is running.
        from openhands.tools.research_harness import MemoryRecord
        from openhands.tools.research_harness.workflow import (
            load_workflow_skill,
            workflow_path,
        )

        schema = self.command(["schema"])
        require(MEMORY_OPERATIONS <= set(schema["schemas"]), "Memory schemas missing")
        console = self.command(["schema"], console=True)
        require(console == schema, "Console and module CLI schemas differ")
        self.report["schema_operations"] = len(schema["schemas"])
        skill_path = Path(self.command(["skill-path"])["path"])
        require(skill_path == workflow_path(), "CLI returned a different skill path")
        skill = load_workflow_skill()
        require(skill.name == "research-review", "Native SDK skill loader failed")
        self.report["skill"] = {
            "path": str(skill_path),
            "name": skill.name,
            "sha256": hashlib.sha256(skill_path.read_bytes()).hexdigest(),
        }

        thread = self.operation(
            "thread.create",
            {"question": "Which imports changed?", "next_action": "Compare inventory"},
        )["record"]
        thread_id = thread["id"]
        inventory = self.operation("inventory", {"thread_id": thread_id})
        require(inventory["status"] == "completed", "Inventory did not complete")
        note = self.operation(
            "memory.remember",
            {
                "thread_id": thread_id,
                "text": "Use the json inventory baseline",
                "source": "installed-wheel acceptance",
                "kind": "decision",
                "tags": ["inventory"],
                "evidence_ids": [inventory["evidence_id"]],
            },
        )["record"]
        require(
            MemoryRecord.model_validate(note).version == 1, "Initial memory version"
        )
        reference = {"thread_id": thread_id, "memory_id": note["id"]}
        require(self.operation("memory.get", reference)["record"] == note, "CLI recall")
        query = {"thread_id": thread_id, "query": "json inventory"}
        recalled = self.operation("memory.recall", query)["records"]
        require(
            [item["record"]["id"] for item in recalled] == [note["id"]], "Recall IDs"
        )
        updated = self.operation(
            "memory.update",
            {
                **reference,
                "expected_version": 1,
                "text": "Use the json inventory baseline and committed lock",
            },
        )["record"]
        require(updated["version"] == 2, "Updated memory version")
        require(
            updated["evidence_ids"] == note["evidence_ids"], "Memory evidence changed"
        )
        self.command(
            [
                "memory.update",
                json.dumps({**reference, "expected_version": 1, "text": "Stale write"}),
            ],
            expected_code=2,
        )
        require(
            self.operation("memory.get", reference)["record"] == updated,
            "Rejected stale write changed memory",
        )
        context = self.operation("context", {"thread_id": thread_id})
        require(note["id"] in context["context"], "Memory missing from context")
        require(context["selected_memory"] == 1, "Context memory selection")
        require(
            len(context["context"].encode()) <= context["max_bytes"], "Context budget"
        )

        for host in ("codex", "claude-code", "claude-desktop"):
            destination = self.root / f"host-{host}"
            arguments = [
                "host-config",
                "--host",
                host,
                "--output",
                str(destination),
                "--python",
                str(self.python),
            ]
            if host == "claude-code":
                arguments.extend(["--thread", thread_id])
            result = self.command(arguments)
            manifest = json.loads((destination / "manifest.json").read_text())
            require(
                result["configuration_installed"] is False, "Unexpected host install"
            )
            require(manifest["host_installation_verified"] is False, "Host scope claim")
            require(
                manifest["python_executable"] == str(self.python), "Host interpreter"
            )
            require(
                (destination / "skills/research-review/SKILL.md").read_bytes()
                == skill_path.read_bytes(),
                f"{host} skill bundle differs",
            )
            self.report["hosts"][host] = manifest
        server = tomllib.loads(
            (self.root / "host-codex/codex.config.toml").read_text()
        )["mcp_servers"]["research_harness"]
        require(server["command"] == str(self.python), "MCP interpreter differs")
        require(server["args"][0] == "-I", "MCP is not configured for isolated mode")
        asyncio.run(self.mcp(server, thread_id, updated))

        forgotten = self.operation(
            "memory.forget",
            {**reference, "expected_version": 2, "reason": "Test complete"},
        )["record"]
        require(forgotten["status"] == "forgotten", "Memory tombstone missing")
        require(forgotten["version"] == 3, "Forgotten memory version")
        require(
            self.operation("memory.recall", query)["records"] == [], "Recall tombstone"
        )
        history = self.operation("memory.history", reference)["records"]
        require(
            [record["version"] for record in history] == [1, 2, 3], "Memory history"
        )
        require(
            self.operation("memory.get", reference)["record"] == forgotten,
            "Persisted tombstone differs",
        )
        self.report["memory"] = {"thread_id": thread_id, "memory_id": note["id"]}
        return thread_id

    async def mcp(
        self, server: dict[str, Any], thread_id: str, updated: dict[str, Any]
    ) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        mcp_note: dict[str, Any] = {}
        async with asyncio.timeout(120):
            for restarting in (False, True):
                parameters = StdioServerParameters(**server, cwd=str(self.workspace))
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=30)
                    ) as session:
                        await session.initialize()
                        discovery = await session.list_tools()
                        require(
                            [tool.name for tool in discovery.tools]
                            == ["research_harness"],
                            "MCP tool discovery differs",
                        )
                        checks: list[str] = []

                        async def invoke(
                            name: str, arguments: dict[str, Any]
                        ) -> dict[str, Any]:
                            response = await session.call_tool(
                                "research_harness",
                                {"operation": name, "arguments": arguments},
                            )
                            value = response.structuredContent
                            require(not response.isError, f"MCP {name} error")
                            require(value is not None, f"MCP {name} structured result")
                            assert value is not None
                            texts = [
                                item.text
                                for item in response.content
                                if item.type == "text"
                            ]
                            require(
                                len(texts) == 1 and json.loads(texts[0]) == value,
                                f"MCP {name} content disagrees",
                            )
                            require(value.get("ok") is True, f"MCP {name} outcome")
                            checks.append(name)
                            return value

                        if not restarting:
                            schemas = await invoke("schema", {})
                            require(
                                MEMORY_OPERATIONS <= set(schemas["operations"]),
                                "MCP schemas",
                            )
                            restored = await invoke(
                                "memory.get",
                                {"thread_id": thread_id, "memory_id": updated["id"]},
                            )
                            require(
                                restored["record"] == updated,
                                "MCP cannot read CLI memory",
                            )
                            mcp_note = (
                                await invoke(
                                    "memory.remember",
                                    {
                                        "thread_id": thread_id,
                                        "text": "MCP memory persists across processes",
                                        "source": "MCP acceptance",
                                        "kind": "lesson",
                                        "tags": ["packaging"],
                                    },
                                )
                            )["record"]
                        else:
                            restored = await invoke(
                                "memory.get",
                                {"thread_id": thread_id, "memory_id": mcp_note["id"]},
                            )
                            require(
                                restored["record"] == mcp_note,
                                "MCP restart lost memory",
                            )
                            recall = await invoke(
                                "memory.recall",
                                {"thread_id": thread_id, "query": "MCP memory"},
                            )
                            require(
                                [item["record"]["id"] for item in recall["records"]]
                                == [mcp_note["id"]],
                                "MCP recall after restart differs",
                            )
                        self.report["mcp"].append(
                            {"restart": restarting, "operations": checks}
                        )

    def github(self, repository: str, thread_id: str, live: dict[str, Any]) -> None:
        """Record one real public metadata observation; all artifact reads are local."""
        from openhands.tools.research_harness.service import HarnessService

        service = HarnessService(
            workspace=self.workspace, state_dir=self.state, allow_github=True
        )
        observation = service.execute(
            "github", {"thread_id": thread_id, "repository": repository}
        )
        live.update(status="failed", observation=observation, requests=1)
        digest = observation["artifact_sha"]
        content = bytearray()
        for _ in range(128):
            page = service.execute(
                "artifact.read",
                {"digest": digest, "offset": len(content), "limit": 4096},
            )
            text = page["content"]
            require(isinstance(text, str), "GitHub artifact slice is not text")
            assert isinstance(text, str)
            content.extend(
                text.encode("utf-8")
                if page["encoding"] == "utf-8"
                else base64.b64decode(text, validate=True)
            )
            if page["eof"] is True:
                break
        else:
            raise VerificationError("GitHub observation exceeded artifact read bound")
        require(hashlib.sha256(content).hexdigest() == digest, "GitHub artifact digest")
        result = json.loads(content)
        live["result"] = {
            key: result[key]
            for key in (
                "ok",
                "source",
                "summary",
                "attempted_at",
                "retrieved_at",
                "http_status",
                "metadata",
                "limitations",
                "response_bytes",
                "response_sha256",
            )
        }
        require(
            result["source"] == f"https://api.github.com/repos/{repository}",
            "GitHub source",
        )
        require(
            observation["ok"] is True
            and result["ok"] is True
            and result["http_status"] == 200
            and result["retrieved_at"] is not None,
            f"Live GitHub observation failed: {result['summary']}",
        )
        live["status"] = "passed"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python", type=Path, required=True, help="Installed runtime interpreter"
    )
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--runtime-kind", choices=("fresh-locked", "copied-runtime"), required=True
    )
    parser.add_argument("--github-repository", choices=(PUBLIC_REPOSITORY,))
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    for name in ("python", "checkout", "wheel", "report"):
        path = vars(args)[name]
        require(path.is_absolute(), f"--{name} must be an absolute path")
    if not args._worker:
        environment = {
            **os.environ,
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
        os.execve(
            args.python,
            [
                str(args.python),
                "-I",
                str(Path(__file__).resolve()),
                *sys.argv[1:],
                "--_worker",
            ],
            environment,
        )
    require(sys.flags.isolated == 1, "Runner must use isolated Python")
    require(
        Path(sys.executable).absolute() == args.python, "Runtime interpreter differs"
    )
    report: dict[str, Any] = {
        "status": "failed",
        "started_at": datetime.now(UTC).isoformat(),
        "runtime_kind": args.runtime_kind,
        "python": sys.executable,
        "isolated": True,
        "local_smoke": {"status": "not_run"},
        "live_github": {
            "status": "not_run" if args.github_repository else "not_requested",
            "repository": args.github_repository,
            "requests": 0,
        },
    }
    original_cwd = Path.cwd()
    temporary: Path | None = None
    code = 1
    try:
        with TemporaryDirectory(prefix="research-harness-install-") as directory:
            temporary = Path(directory)
            smoke = Smoke(args.python, temporary, report["local_smoke"])
            require(
                not temporary.is_relative_to(args.checkout.resolve()),
                "Smoke is inside checkout",
            )
            os.chdir(smoke.workspace)
            report["working_directory"] = str(smoke.workspace)
            report["installation"] = verify_installation(args.checkout, args.wheel)
            thread_id = smoke.run()
            report["local_smoke"]["status"] = "passed"
            if args.github_repository:
                smoke.github(args.github_repository, thread_id, report["live_github"])
            report["status"] = "passed"
            code = 0
    except (Exception, KeyboardInterrupt) as error:
        if report["local_smoke"]["status"] != "passed":
            report["local_smoke"]["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        if isinstance(error, KeyboardInterrupt):
            code = 130
    finally:
        os.chdir(original_cwd)
        report["temporary_root"] = str(temporary) if temporary else None
        report["temporary_root_removed"] = temporary is None or not temporary.exists()
        report["finished_at"] = datetime.now(UTC).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "report": str(args.report),
                    "runtime_kind": args.runtime_kind,
                    "local_smoke": report["local_smoke"]["status"],
                    "live_github": report["live_github"]["status"],
                    "temporary_root_removed": report["temporary_root_removed"],
                }
            )
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
