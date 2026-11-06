"""Verify the frozen harness on POSIX using disposable state and no model calls."""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PyInstaller.archive.readers import CArchiveReader


CHECKOUT = Path(__file__).resolve().parents[2]
SKILL = "openhands/tools/research_harness/skills/research-review/SKILL.md"
WORKER = "openhands/tools/research_harness/_http_worker.py"
SOURCE = "Owned frozen acceptance fixture"
LABEL = "Display label: Cedar Lantern 48"
MEMORY_OPERATIONS = {
    "memory.remember",
    "memory.get",
    "memory.update",
    "memory.forget",
    "memory.recall",
    "memory.history",
}
NATIVE_PROBE = """import json
from pathlib import Path

from openhands.sdk.hooks.manager import HookManager
from openhands.tools.research_harness.hooks import make_hook_config

fixture = json.loads(Path(__file__).with_name("fixture.json").read_text())
config = make_hook_config(
    workspace=Path(fixture["workspace"]), state_dir=Path(fixture["state"]),
    thread_id=fixture["thread_id"],
)
manager = HookManager(
    config=config, working_dir=fixture["workspace"], session_id="frozen-acceptance",
)
start = manager.run_session_start()
allowed, allow_results = manager.run_pre_tool_use(tool_name="think", tool_input={})
denied, deny_results = manager.run_pre_tool_use(tool_name="outside_tool", tool_input={})
invalid, invalid_results = manager.run_pre_tool_use(
    tool_name="research_harness",
    tool_input={"operation": "memory.get", "arguments": {"unexpected": True}},
)
print(json.dumps({
    "commands": [hook.command for matcher in config.session_start
                 for hook in matcher.hooks],
    "start": [result.model_dump(mode="json") for result in start],
    "allowed": allowed, "allow_codes": [result.exit_code for result in allow_results],
    "denied": denied, "deny_codes": [result.exit_code for result in deny_results],
    "invalid": invalid,
    "invalid_codes": [result.exit_code for result in invalid_results],
}))
raise SystemExit(0)
"""


class VerificationError(RuntimeError):
    """An observed result did not satisfy the acceptance contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def owned_processes(workspace: Path) -> list[psutil.Process]:
    processes = []
    for process in psutil.process_iter():
        try:
            if (
                process.cwd() == str(workspace)
                and process.status() != psutil.STATUS_ZOMBIE
            ):
                processes.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return processes


def stop_owned(workspace: Path) -> dict[str, Any]:
    processes = owned_processes(workspace)
    for process in processes:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=2)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=2)
    remaining = [process.pid for process in owned_processes(workspace)]
    return {
        "terminated_pids": [process.pid for process in processes],
        "remaining_pids": remaining,
        "ok": not remaining,
    }


class FrozenCheck:
    def __init__(self, binary: Path, root: Path, report: dict[str, Any]) -> None:
        self.binary = binary
        self.root = root
        self.workspace = root / "workspace"
        self.state = root / "state"
        self.workspace.mkdir()
        (self.workspace / "sample.py").write_text("import json\n", encoding="utf-8")
        temporary = root / "temporary"
        temporary.mkdir()
        self.environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "TMPDIR": str(temporary),
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
        self.prefix = [
            str(binary),
            "--research-harness",
            "--workspace",
            str(self.workspace),
            "--state",
            str(self.state),
        ]
        self.report = report
        report.update(commands=[], mcp=[])

    def invoke(
        self,
        arguments: list[str],
        *,
        label: str,
        input_value: dict[str, Any] | None = None,
        expected_code: int | None = 0,
    ) -> dict[str, Any]:
        observation: dict[str, Any] = {"label": label, "expected_code": expected_code}
        self.report["commands"].append(observation)
        started = time.monotonic()
        with subprocess.Popen(
            arguments,
            cwd=self.workspace,
            env=self.environment,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as process:
            try:
                stdout, stderr = process.communicate(
                    None if input_value is None else json.dumps(input_value),
                    timeout=60,
                )
                observation["survivors_before_cleanup"] = [
                    child.pid for child in owned_processes(self.workspace)
                ]
            finally:
                # The binary's bootloader and native hooks may own descendants.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                observation["cleanup"] = stop_owned(self.workspace)
                observation["seconds"] = round(time.monotonic() - started, 3)
                observation["returncode"] = process.returncode
        observation["stdout_bytes"] = len(stdout.encode("utf-8"))
        observation["stderr_bytes"] = len(stderr.encode("utf-8"))
        observation["stderr_sha256"] = hashlib.sha256(stderr.encode()).hexdigest()
        value = json.loads(stdout)
        require(isinstance(value, dict), f"{label}: stdout is not one JSON object")
        observation["result"] = value
        require(
            observation["cleanup"]["ok"], f"{label}: owned process survived cleanup"
        )
        require(
            not observation["survivors_before_cleanup"],
            f"{label}: owned process survived normal completion",
        )
        if expected_code is not None:
            require(
                process.returncode == expected_code, f"{label}: unexpected exit status"
            )
        return value

    def cli(self, arguments: list[str], *, expected_code: int = 0) -> dict[str, Any]:
        value = self.invoke(
            [*self.prefix, *arguments],
            label=arguments[0],
            expected_code=expected_code,
        )
        require(
            value.get("ok") is (expected_code == 0), "CLI outcome disagrees with exit"
        )
        return value

    def operation(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.cli([name, json.dumps(arguments)])

    def artifact(self, sha: str) -> dict[str, Any]:
        require(bool(re.fullmatch(r"[0-9a-f]{64}", sha)), "Invalid artifact reference")
        path = self.state / "artifacts" / sha
        require(path.stat().st_size <= 1_048_576, "Oversized persisted artifact")
        content = path.read_bytes()
        require(hashlib.sha256(content).hexdigest() == sha, "Artifact digest mismatch")
        result = json.loads(content)
        require(isinstance(result, dict), "Artifact is not an object")
        return result

    def memory(self) -> tuple[str, dict[str, Any]]:
        schema = self.cli(["schema"])
        require(MEMORY_OPERATIONS <= set(schema["schemas"]), "Missing memory schema")
        self.cli(["schema", "unknown.operation"], expected_code=2)
        thread = self.operation(
            "thread.create",
            {
                "question": "Which display label did documentation review choose?",
            },
        )["record"]["id"]
        inventory = self.operation("inventory", {"thread_id": thread})
        require(inventory["status"] == "completed", "Inventory did not complete")
        original = self.operation(
            "memory.remember",
            {
                "thread_id": thread,
                "text": "Display label: Cedar Lantern 47",
                "source": SOURCE,
                "kind": "decision",
                "tags": ["display"],
                "evidence_ids": [inventory["evidence_id"]],
            },
        )["record"]
        reference = {"thread_id": thread, "memory_id": original["id"]}
        updated = self.operation(
            "memory.update",
            {
                **reference,
                "expected_version": 1,
                "text": LABEL,
            },
        )["record"]
        require(original["version"] == 1 and updated["version"] == 2, "Memory versions")
        require(
            updated["text"] == LABEL and updated["source"] == SOURCE,
            "Memory provenance",
        )
        require(
            updated["evidence_ids"] == [inventory["evidence_id"]], "Evidence reference"
        )
        self.cli(
            ["memory.update", json.dumps({**reference, "expected_version": 1})],
            expected_code=2,
        )
        self.cli(
            ["memory.get", json.dumps({**reference, "unexpected": True})],
            expected_code=2,
        )
        require(
            self.operation("memory.get", reference)["record"] == updated,
            "Reopened memory differs",
        )
        history = self.operation("memory.history", reference)
        require(
            sorted(history["records"], key=lambda note: note["version"])
            == [original, updated],
            "Memory history differs",
        )
        require(history["has_more"] is False, "Unexpected history continuation")
        other = self.operation("thread.create", {"question": "Separate fixture"})[
            "record"
        ]["id"]
        foreign = self.operation(
            "memory.remember",
            {"thread_id": other, "text": LABEL, "source": "Other thread"},
        )["record"]
        recalled = self.operation(
            "memory.recall", {"thread_id": thread, "query": "Display label"}
        )
        require(
            [item["record"] for item in recalled["records"]] == [updated],
            "Recall leaked or lost a note",
        )
        context = self.operation("context", {"thread_id": thread, "max_bytes": 4096})
        require(
            LABEL in context["context"] and foreign["id"] not in context["context"],
            "Context memory scope",
        )
        require(
            context["context_bytes"] == len(context["context"].encode()) <= 4096,
            "Context byte bound",
        )
        manifest = self.artifact(context["manifest_sha"])
        require(manifest["context"] == context["context"], "Context manifest text")
        require(
            manifest["context_sha256"]
            == hashlib.sha256(context["context"].encode()).hexdigest(),
            "Context text digest",
        )
        self.report["memory"] = {
            "thread_id": thread,
            "record": updated,
            "history": history["records"],
            "context": context,
        }
        return thread, updated

    def native_hooks(self, thread: str) -> None:
        probe = self.root / "probe"
        probe.mkdir()
        (probe / "frozen_harness_probe.py").write_text(NATIVE_PROBE, encoding="utf-8")
        (probe / "fixture.json").write_text(
            json.dumps(
                {
                    "workspace": str(self.workspace),
                    "state": str(self.state),
                    "thread_id": thread,
                }
            ),
            encoding="utf-8",
        )
        result = self.invoke(
            [
                str(self.binary),
                "--extra-python-path",
                str(probe),
                "--import-modules",
                "frozen_harness_probe",
            ],
            label="native-hooks",
        )
        require(len(result["commands"]) == 1, "Native hook command missing")
        require(
            shlex.split(result["commands"][0])[:2]
            == [str(self.binary), "--research-harness"],
            "Native hook uses another runtime",
        )
        require(
            len(result["start"]) == 1 and result["start"][0]["success"] is True,
            "Native SessionStart failed",
        )
        require(
            LABEL in json.loads(result["start"][0]["stdout"])["additionalContext"],
            "Native hook lost memory",
        )
        require(
            result["allowed"] is True and result["allow_codes"] == [0],
            "Native allowed action",
        )
        require(
            result["denied"] is False and result["deny_codes"] == [2],
            "Native outside-tool denial",
        )
        require(
            result["invalid"] is False and result["invalid_codes"] == [2],
            "Native invalid-argument denial",
        )
        self.report["native_hooks"] = result

    def host(self, thread: str, skill_sha: str) -> dict[str, Any]:
        destination = self.root / "claude-code"
        outcome = self.cli(
            [
                "host-config",
                "--host",
                "claude-code",
                "--thread",
                thread,
                "--output",
                str(destination),
            ]
        )
        require(
            outcome["configuration_installed"] is False, "Host profile was modified"
        )
        manifest = json.loads((destination / "manifest.json").read_text())
        require(manifest["runtime"] == "frozen-agent-server", "Host runtime differs")
        require(
            manifest["host_installation_verified"] is False,
            "Incorrect native-host claim",
        )
        require(
            digest(destination / "skills/research-review/SKILL.md")
            == skill_sha
            == manifest["skill_sha256"],
            "Bundled skill differs from source",
        )
        server = json.loads((destination / "claude-code.mcp.json").read_text())[
            "mcpServers"
        ]["research_harness"]
        require(
            [server["command"], *server["args"]] == [*self.prefix, "mcp"],
            "Generated MCP command differs",
        )
        hook = json.loads((destination / "claude-code.settings.json").read_text())[
            "hooks"
        ]["SessionStart"][0]["hooks"][0]
        result = self.invoke(
            [hook["command"], *hook["args"]],
            label="generated-host-hook",
            input_value={
                "session_id": "frozen-acceptance",
                "cwd": str(self.workspace),
                "hook_event_name": "SessionStart",
            },
        )
        require(
            result["hookSpecificOutput"]["hookEventName"] == "SessionStart",
            "Host event output differs",
        )
        require(
            LABEL in result["hookSpecificOutput"]["additionalContext"],
            "Host hook lost memory",
        )
        self.report["host_bundle"] = manifest
        return server

    async def mcp(
        self, server: dict[str, Any], thread: str, updated: dict[str, Any]
    ) -> None:
        saved: dict[str, Any] = {}
        async with asyncio.timeout(120):
            for index in range(2):
                observation: dict[str, Any] = {"session": index + 1, "operations": []}
                self.report["mcp"].append(observation)
                parameters = StdioServerParameters(
                    command=server["command"],
                    args=server["args"],
                    env={**self.environment, **server["env"]},
                    cwd=str(self.workspace),
                )
                with tempfile.TemporaryFile(mode="w+") as errors:
                    async with stdio_client(parameters, errlog=errors) as (read, write):
                        async with ClientSession(
                            read, write, read_timeout_seconds=timedelta(seconds=30)
                        ) as session:
                            await session.initialize()
                            discovery = await session.list_tools()
                            require(
                                [tool.name for tool in discovery.tools]
                                == ["research_harness"],
                                "MCP discovery differs",
                            )

                            async def invoke(
                                operation: str, arguments: dict[str, Any]
                            ) -> dict[str, Any]:
                                response = await session.call_tool(
                                    "research_harness",
                                    {"operation": operation, "arguments": arguments},
                                )
                                value = response.structuredContent
                                require(
                                    not response.isError and isinstance(value, dict),
                                    "MCP operation failed",
                                )
                                assert value is not None
                                texts = [
                                    item.text
                                    for item in response.content
                                    if item.type == "text"
                                ]
                                require(
                                    len(texts) == 1 and json.loads(texts[0]) == value,
                                    "MCP text and structured output differ",
                                )
                                require(
                                    value.get("ok") is True,
                                    "MCP inner operation failed",
                                )
                                observation["operations"].append(
                                    {"operation": operation, "result": value}
                                )
                                return value

                            restored = await invoke(
                                "memory.get",
                                {"thread_id": thread, "memory_id": updated["id"]},
                            )
                            require(
                                restored["record"] == updated, "MCP lost CLI memory"
                            )
                            if index == 0:
                                schema = await invoke("schema", {})
                                require(
                                    MEMORY_OPERATIONS <= set(schema["operations"]),
                                    "MCP memory schemas missing",
                                )
                                saved = (
                                    await invoke(
                                        "memory.remember",
                                        {
                                            "thread_id": thread,
                                            "text": "MCP process restart receipt",
                                            "source": SOURCE,
                                        },
                                    )
                                )["record"]
                            else:
                                restored = await invoke(
                                    "memory.get",
                                    {"thread_id": thread, "memory_id": saved["id"]},
                                )
                                require(
                                    restored["record"] == saved,
                                    "MCP restart lost its write",
                                )
                                recalled = await invoke(
                                    "memory.recall",
                                    {"thread_id": thread, "query": "MCP process"},
                                )
                                require(
                                    [item["record"] for item in recalled["records"]]
                                    == [saved],
                                    "MCP restart recall differs",
                                )
                observation["remaining_owned_pids"] = [
                    process.pid for process in owned_processes(self.workspace)
                ]
                require(
                    not observation["remaining_owned_pids"],
                    "MCP process survived connection close",
                )

    def github(self, repository: str, thread: str) -> None:
        live = self.report["live_github"]
        live.update(status="failed", requests=1)
        observation = self.invoke(
            [
                *self.prefix,
                "--allow-github",
                "github",
                json.dumps({"thread_id": thread, "repository": repository}),
            ],
            label="live-github",
            expected_code=None,
        )
        live["observation"] = observation
        result = self.artifact(observation["artifact_sha"])
        live["result"] = {
            key: value for key, value in result.items() if key != "response_body_base64"
        }
        body = result["response_body_base64"]
        if body is not None:
            content = base64.b64decode(body, validate=True)
            require(
                hashlib.sha256(content).hexdigest() == result["response_sha256"],
                "GitHub response digest",
            )
        require(
            observation["ok"] is True
            and self.report["commands"][-1]["returncode"] == 0
            and result["ok"] is True
            and result["http_status"] == 200,
            "Live GitHub observation failed",
        )
        require(
            result["source"] == f"https://api.github.com/repos/{repository}"
            and result["retrieved_at"] is not None,
            "GitHub source/time missing",
        )
        require(
            result["metadata"]["full_name"].casefold() == repository.casefold(),
            "GitHub identity differs",
        )
        live["status"] = "passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--github-repository")
    args = parser.parse_args()
    if not args.binary.is_absolute() or not args.report.is_absolute():
        parser.error("--binary and --report must be absolute paths")
    report: dict[str, Any] = {
        "schema_version": 1,
        "ok": False,
        "started_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "scope": (
            "Frozen CLI, generated host protocol, native SDK hooks and MCP; "
            "no native host or server REST acceptance"
        ),
        "model_requests": 0,
        "live_github": {
            "status": "not_attempted" if args.github_repository else "not_requested",
            "requests": 0,
        },
    }
    root: Path | None = None
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        require(
            os.name == "posix", "This acceptance runner requires POSIX process groups"
        )
        binary = args.binary.resolve(strict=True)
        require(
            binary.is_file() and os.access(binary, os.X_OK), "Binary is not executable"
        )
        report["binary"] = {
            "path": str(binary),
            "sha256": digest(binary),
            "bytes": binary.stat().st_size,
        }
        archive = CArchiveReader(str(binary))
        report["packaged_files"] = []
        for name in (SKILL, WORKER):
            source = (CHECKOUT / "openhands-tools" / name).read_bytes()
            require(archive.extract(name) == source, f"Bundled bytes differ: {name}")
            report["packaged_files"].append(
                {"path": name, "sha256": hashlib.sha256(source).hexdigest()}
            )
        if args.github_repository is not None:
            require(
                bool(
                    re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}",
                        args.github_repository,
                    )
                ),
                "Invalid public repository name",
            )
            require(
                args.github_repository.rsplit("/", 1)[1] not in {".", ".."},
                "Invalid public repository name",
            )
        temporary = tempfile.TemporaryDirectory(prefix="research-harness-frozen-")
        root = Path(temporary.name)
        check = FrozenCheck(binary, root, report)
        thread, updated = check.memory()
        check.native_hooks(thread)
        server = check.host(thread, report["packaged_files"][0]["sha256"])
        asyncio.run(check.mcp(server, thread, updated))
        if args.github_repository is not None:
            check.github(args.github_repository, thread)
        require(
            digest(binary) == report["binary"]["sha256"],
            "Binary changed during verification",
        )
        report["checks_passed"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)[:2000]}
    finally:
        cleanup: dict[str, Any] = {"ok": True, "remaining_pids": []}
        if root is not None:
            try:
                cleanup = stop_owned(root / "workspace")
                if temporary is not None:
                    temporary.cleanup()
                cleanup["temporary_root_removed"] = not root.exists()
                cleanup["ok"] = cleanup["ok"] and cleanup["temporary_root_removed"]
            except Exception as error:
                cleanup.update(ok=False, error_type=type(error).__name__)
        report["cleanup"] = cleanup
        report["ok"] = report.get("checks_passed", False) and cleanup["ok"]
        report["finished_at"] = datetime.now(UTC).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(json.dumps({"ok": report["ok"], "report": str(args.report)}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
