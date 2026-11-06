"""Check one source-minimal image through its installed runtime and real API.

The workflow builds the image from an exact root sdist. This program owns one
container, generated local keys and temporary fixtures. It uses the maintained
server verification CLI for authenticated HTTP evidence. No model is called.
"""

import argparse
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import sysconfig
import tarfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast


CONTAINER_PYTHON = "/agent-server/.venv/bin/python"
FIXTURE = Path("/workspace/research-harness-acceptance")
OWNER_LABEL = "org.openhands.research-acceptance.owner"
MEMORY_OPERATIONS = {
    "memory.remember",
    "memory.get",
    "memory.update",
    "memory.forget",
    "memory.recall",
    "memory.history",
}
CONTAINER_FORMAT = (
    '{"id":{{json .Id}},"image":{{json .Image}},'
    '"user":{{json .Config.User}},"labels":{{json .Config.Labels}},'
    '"state":{{json .State}},"ports":{{json .NetworkSettings.Ports}}}'
)
IMAGE_FORMAT = (
    '{"id":{{json .Id}},"repo_digests":{{json .RepoDigests}},'
    '"os":{{json .Os}},"architecture":{{json .Architecture}},"size":{{json .Size}}}'
)


class VerificationError(RuntimeError):
    """A required packaging, runtime or cleanup observation did not pass."""


class Redactor(Protocol):
    def add(self, value: str | None, label: str) -> None: ...
    def text(self, value: str) -> str: ...
    def obj(self, value: Any) -> Any: ...


class VerificationCLI(Protocol):
    REDACT: Redactor

    def main(self, argv: list[str] | None = None) -> int: ...


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_manifest(checkout: Path, sdist: Path) -> dict[str, Any]:
    """Require the tested package files to match the exact Docker source archive."""
    selected = {}
    tools = checkout / "openhands-tools"
    for package in ("openhands/tools/research_harness", "openhands/research"):
        for path in (tools / package).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md"}:
                selected[path.relative_to(checkout).as_posix()] = "openhands-tools"
    for relative in (
        "openhands/agent_server/__init__.py",
        "openhands/agent_server/__main__.py",
        "openhands/agent_server/mcp_router.py",
    ):
        selected[f"openhands-agent-server/{relative}"] = "openhands-agent-server"
    require(bool(selected), "No source files selected")
    dockerfile = "openhands-agent-server/openhands/agent_server/docker/Dockerfile"
    checks = [*selected, "uv.lock", "pyproject.toml", dockerfile]
    files = []
    with tarfile.open(sdist, "r:gz") as archive:
        roots = {Path(member.name).parts[0] for member in archive.getmembers()}
        require(len(roots) == 1, "Source archive must have one root")
        prefix = roots.pop()
        for relative in sorted(checks):
            try:
                member = archive.getmember(f"{prefix}/{relative}")
            except KeyError as error:
                raise VerificationError(
                    f"Source archive is missing: {relative}"
                ) from error
            require(member.isfile(), f"Archive entry is not a file: {relative}")
            stream = archive.extractfile(member)
            require(stream is not None, f"Cannot read archive member: {relative}")
            assert stream is not None
            actual = stream.read()
            expected = (checkout / relative).read_bytes()
            require(actual == expected, f"Source archive differs: {relative}")
            if relative in selected:
                files.append(
                    {
                        "distribution": selected[relative],
                        "path": relative.split("/", 1)[1],
                        "sha256": hashlib.sha256(expected).hexdigest(),
                    }
                )
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
    ).strip()
    expected_sha = os.environ.get("GITHUB_SHA", sha)
    require(sha == expected_sha, "Checkout SHA differs from the workflow SHA")
    return {
        "checkout_sha": sha,
        "source_tree": subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=checkout, text=True
        ).strip(),
        "sdist_sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
        "dockerfile_sha256": hashlib.sha256(
            (checkout / dockerfile).read_bytes()
        ).hexdigest(),
        "uv_lock_sha256": hashlib.sha256(
            (checkout / "uv.lock").read_bytes()
        ).hexdigest(),
        "files": files,
    }


def installed_provenance(manifest: dict[str, Any]) -> dict[str, Any]:
    """Run only inside the image; workspace packages must be non-editable."""
    from openhands.tools.research_harness.service import HarnessService
    from openhands.tools.research_harness.workflow import (
        load_workflow_skill,
        workflow_path,
    )

    require(sys.flags.isolated == 1, "Image probe requires isolated Python")
    require(os.geteuid() != 0, "Image probe must use the normal non-root user")
    require(sys.executable == CONTAINER_PYTHON, "Wrong image interpreter")
    site = Path(sysconfig.get_path("purelib")).resolve()
    require(site.is_relative_to("/agent-server/.venv"), "Unexpected site-packages")
    distributions = {}
    for name in (
        "openhands-sdk",
        "openhands-tools",
        "openhands-workspace",
        "openhands-agent-server",
    ):
        distribution = importlib.metadata.distribution(name)
        direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
        require(
            not direct_url.get("dir_info", {}).get("editable", False),
            f"Editable image package: {name}",
        )
        distributions[name] = {
            "version": distribution.version,
            "direct_url": direct_url,
        }
    verified = []
    for entry in manifest["files"]:
        distribution = importlib.metadata.distribution(entry["distribution"])
        path = Path(distribution.locate_file(entry["path"])).resolve()
        require(path.is_relative_to(site), f"File is outside site-packages: {path}")
        require(
            hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"],
            f"Image bytes differ: {path}",
        )
        verified.append({**entry, "installed": str(path)})
    origins = {}
    for name in (
        "openhands.research",
        "openhands.tools.research_harness",
        "openhands.tools.research_harness.agentmemory",
        "openhands.agent_server",
    ):
        module = importlib.import_module(name)
        require(
            module.__spec__ is not None and module.__spec__.origin is not None,
            f"No module origin: {name}",
        )
        assert module.__spec__ is not None and module.__spec__.origin is not None
        origin = Path(module.__spec__.origin).resolve()
        require(origin.is_relative_to(site), f"Import is outside site-packages: {name}")
        origins[name] = str(origin)
    require(
        workflow_path().resolve().is_relative_to(site), "Skill is outside site-packages"
    )
    require(
        load_workflow_skill().name == "research-review", "Strict skill loading failed"
    )
    require(MEMORY_OPERATIONS <= set(HarnessService.schema()), "Memory schemas missing")
    return {
        "uid": os.geteuid(),
        "python": sys.executable,
        "python_version": sys.version,
        "site_packages": str(site),
        "distributions": distributions,
        "verified_files": verified,
        "module_origins": origins,
        "skill_sha256": hashlib.sha256(workflow_path().read_bytes()).hexdigest(),
    }


def container_worker(phase: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Fixed operator checks executed in our own disposable container."""
    if phase == "provenance":
        return installed_provenance(manifest)
    if phase == "children":
        import psutil

        remaining = []
        for process in psutil.Process(1).children(recursive=True):
            try:
                command = process.cmdline()
            except psutil.NoSuchProcess:
                continue
            if "openhands.research" in command and str(FIXTURE / "state") in command:
                remaining.append(process.pid)
        require(not remaining, "An owned MCP child survived its request")
        return {"remaining_mcp_children": remaining}

    from openhands.tools.research_harness.service import HarnessService

    workspace, state = FIXTURE / "workspace", FIXTURE / "state"
    if phase == "seed":
        workspace.mkdir(parents=True)
        (workspace / "sample.py").write_text("import json\n", encoding="utf-8")
    service = HarnessService(workspace=workspace, state_dir=state)

    def call(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = service.execute(
            operation, arguments, session_id="container-acceptance-operator"
        )
        require(result.get("ok") is True, f"Operator {operation} failed: {result}")
        return result

    if phase == "seed":
        thread = call("thread.create", {"question": "Which static import is present?"})[
            "record"
        ]
        inventory = call("inventory", {"thread_id": thread["id"]})
        require(inventory["status"] == "completed", "Benign inventory did not complete")
        original = call(
            "memory.remember",
            {
                "thread_id": thread["id"],
                "kind": "decision",
                "tags": ["inventory"],
                "text": "Retain the inventory baseline with its evidence.",
                "source": "Owned container acceptance",
                "evidence_ids": [inventory["evidence_id"]],
            },
        )["record"]
        other_thread = call("thread.create", {"question": "Separate owned thread"})[
            "record"
        ]
        other_note = call(
            "memory.remember",
            {
                "thread_id": other_thread["id"],
                "text": "Inventory from the other thread",
                "source": "Owned container acceptance",
            },
        )["record"]
        fixture = {
            "original": original,
            "inventory": inventory,
            "other_note": other_note,
        }
    else:
        fixture = json.loads((FIXTURE / "fixture.json").read_text())
        original = fixture["original"]
        fixture["updated"] = call(
            "memory.update",
            {
                "thread_id": original["thread_id"],
                "memory_id": original["id"],
                "expected_version": 1,
                "text": "Keep the inventory baseline and its source together.",
            },
        )["record"]
        require(fixture["updated"]["version"] == 2, "Wrong updated memory version")
        require(
            fixture["updated"]["evidence_ids"] == original["evidence_ids"],
            "Evidence reference changed",
        )
    write_json(FIXTURE / "fixture.json", fixture)
    return fixture


def load_cli(checkout: Path) -> VerificationCLI:
    path = (
        checkout / ".agents/skills/verify-agent-server/scripts/control_agent_server.py"
    )
    spec = importlib.util.spec_from_file_location("container_acceptance_cli", path)
    require(
        spec is not None and spec.loader is not None, "Verification CLI unavailable"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(VerificationCLI, module)


def require_owned_container(info: dict[str, Any], owner: dict[str, Any]) -> None:
    require(
        (info["labels"] or {}).get(OWNER_LABEL) == owner["token"],
        "Refusing an unowned container",
    )
    require(info["image"] == owner["image_id"], "Container image identity changed")


class ContainerCheck:
    def __init__(self, checkout: Path, root: Path) -> None:
        self.checkout, self.root = checkout, root
        self.reports = root / "reports"
        self.private = root / "private"
        self.cli = load_cli(checkout)
        self.owner: dict[str, Any] = {}
        self.report: dict[str, Any] = {
            "status": "failed",
            "started_at": datetime.now(UTC).isoformat(),
            "scope": "linux/amd64 source-minimal, installed bytes and server MCP reads",
            "model_calls": 0,
            "observations": {},
        }
        if (root / "owner.json").exists():
            self.owner = json.loads((root / "owner.json").read_text())
        for name in ("session_key", "secret_key"):
            path = self.private / name
            if path.exists():
                self.cli.REDACT.add(path.read_text().strip(), name)

    def save_owner(self) -> None:
        write_json(self.root / "owner.json", self.owner)

    def docker(
        self,
        arguments: list[str],
        *,
        timeout: float = 60,
        source: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["docker", *arguments],
            input=source,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if check:
            require(
                result.returncode == 0,
                f"Docker {arguments[0]} failed: "
                f"{self.cli.REDACT.text(result.stderr[-3000:])}",
            )
        return result

    def inspect_container(self) -> dict[str, Any] | None:
        result = self.docker(
            [
                "inspect",
                "--type",
                "container",
                "--format",
                CONTAINER_FORMAT,
                self.owner["name"],
            ],
            check=False,
        )
        if result.returncode:
            require(
                "no such" in result.stderr.lower(),
                "Docker inspection failed; absence is unproven",
            )
            return None
        info = json.loads(result.stdout)
        require_owned_container(info, self.owner)
        return info

    def invoke(
        self, label: str, arguments: list[str], *, check: bool = True
    ) -> dict[str, Any]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.cli.main(arguments)
        result = json.loads(buffer.getvalue())
        self.report["observations"][label] = result
        if check:
            require(
                code == 0,
                f"Verification CLI step failed: {label}; see saved observations",
            )
        print(json.dumps({"step": label, "ok": code == 0}), flush=True)
        return result

    def api(
        self,
        label: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        readiness: bool = False,
    ) -> Any:
        arguments = [
            "api",
            method,
            path,
            "--run",
            self.owner["run"],
            "--expect",
            "200",
            "--save",
            f"container/{label}",
            "--timeout",
            "2" if readiness else "40",
        ]
        if body is not None:
            arguments += ["--json", json.dumps(body)]
        if not readiness:
            return self.invoke(label, arguments)["response"]["body"]
        # The CLI's --until-ok repeats HTTP responses, not connection errors.
        # Container startup/restart can initially refuse or reset connections.
        deadline = time.monotonic() + 90
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            result = self.invoke(f"{label}-{attempt}", arguments, check=False)
            if result.get("ok") is True:
                return result["response"]["body"]
            time.sleep(0.5)
        raise VerificationError(f"{label} did not become ready within 90 seconds")

    def attach_endpoint(self, info: dict[str, Any], label: str) -> None:
        """Use the current owned loopback binding, retaining the private key."""
        require_owned_container(info, self.owner)
        require(info["state"]["Running"], "Owned container is not running")
        bindings = info["ports"].get("8000/tcp")
        require(
            isinstance(bindings, list)
            and len(bindings) == 1
            and bindings[0]["HostIp"] == "127.0.0.1",
            "Server must publish only on loopback",
        )
        port = int(bindings[0]["HostPort"])
        require(0 < port < 65536, "Invalid published server port")
        previous_port = self.owner.get("port")
        self.owner["port"] = port
        self.save_owner()
        if self.owner.get("run") and previous_port == port:
            return

        session_key = (self.private / "session_key").read_text().strip()
        require(bool(session_key), "Private session key is missing")
        self.cli.REDACT.add(session_key, "session_key")
        environment = {
            "AGENT_SERVER_VERIFY_HOME": str(self.root / "verify"),
            "HARNESS_CONTAINER_SESSION_KEY": session_key,
        }
        previous_environment = {key: os.environ.get(key) for key in environment}
        try:
            os.environ.update(environment)
            attached = self.invoke(
                label,
                [
                    "attach",
                    "--url",
                    f"http://127.0.0.1:{port}",
                    "--key-env",
                    "HARNESS_CONTAINER_SESSION_KEY",
                    "--name",
                    "research-container",
                ],
            )
        finally:
            for key, value in previous_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.owner["run"] = attached["run"]
        self.save_owner()

    def restart_and_wait(self, before: dict[str, Any]) -> None:
        """Inspect and refresh a dynamic host binding before any restart probe."""
        self.report["container_before_restart"] = before
        self.docker(["restart", "--time", "15", self.owner["name"]], timeout=45)
        after = self.inspect_container()
        self.report["container_after_restart"] = after
        require(
            after is not None
            and after["id"] == before["id"]
            and after["state"]["StartedAt"] != before["state"]["StartedAt"],
            "Container restart was not observed",
        )
        assert after is not None
        self.attach_endpoint(after, "attach-after")
        self.api("ready-after", "GET", "/ready", readiness=True)

    def worker(
        self, phase: str, manifest: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        arguments = [
            "exec",
            "-i",
            "--workdir",
            "/workspace/project",
            self.owner["name"],
            CONTAINER_PYTHON,
            "-I",
            "-",
            "--_worker",
            phase,
        ]
        if manifest is not None:
            arguments += ["--manifest-json", json.dumps(manifest)]
        result = self.docker(arguments, source=Path(__file__).read_text(), timeout=60)
        value = json.loads(result.stdout)
        self.report["observations"][f"operator-{phase}"] = value
        if result.stderr:
            self.report["observations"][f"operator-{phase}-stderr"] = (
                self.cli.REDACT.text(result.stderr)
            )
        return value

    def memory(
        self, label: str, operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        response = self.api(
            label,
            "POST",
            "/api/mcp/test",
            {
                "name": "container-memory-acceptance",
                "timeout": 30,
                "server": {
                    "transport": "stdio",
                    "command": CONTAINER_PYTHON,
                    "args": [
                        "-I",
                        "-m",
                        "openhands.research",
                        "--workspace",
                        str(FIXTURE / "workspace"),
                        "--state",
                        str(FIXTURE / "state"),
                        "mcp",
                    ],
                    "cwd": str(FIXTURE / "workspace"),
                    "env": {
                        "OPENHANDS_SUPPRESS_BANNER": "1",
                        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                    },
                },
                "tool_call": {
                    "name": "research_harness",
                    "arguments": {"operation": operation, "arguments": arguments},
                },
            },
        )
        require(
            response["ok"] is True and "research_harness" in response["tools"],
            "MCP discovery failed",
        )
        require(
            response["tool_result"]["is_error"] is False, "MCP tool returned an error"
        )
        value = json.loads(response["tool_result"]["text"])
        require(value["ok"] is True, "Memory operation failed")
        self.report["observations"][f"{label}-cleanup"] = self.worker("children")
        return value

    def claim_image(
        self, image: str, *, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        require(not self.owner, "Acceptance directory has already been used")
        require(
            image.startswith("research-harness-acceptance:"),
            "Expected an owned acceptance image tag",
        )
        inspected = self.docker(
            ["image", "inspect", "--format", IMAGE_FORMAT, image], check=False
        )
        if missing_ok and inspected.returncode:
            require("no such" in inspected.stderr.lower(), "Image absence is unproven")
            return None
        require(inspected.returncode == 0, "Built acceptance image is unavailable")
        metadata = json.loads((self.reports / "build-metadata.json").read_text())
        image_info = json.loads(inspected.stdout)
        require(
            image_info["id"] == metadata["containerimage.config.digest"],
            "Image differs from build metadata",
        )
        self.report["image"] = image_info
        token = secrets.token_hex(16)
        self.owner = {
            "token": token,
            "name": f"research-memory-{token}",
            "image_tag": image,
            "image_id": image_info["id"],
        }
        self.save_owner()
        return metadata

    def run(self, image: str, sdist: Path) -> None:
        metadata = self.claim_image(image)
        assert metadata is not None
        # Claim the verified image before source/asset checks, so their failure
        # still removes that exact image in the normal and always-run cleanup.
        manifest = source_manifest(self.checkout, sdist)
        self.report["source"] = manifest
        require(
            self.report["image"]["os"] == "linux"
            and self.report["image"]["architecture"] == "amd64",
            "Unexpected image platform",
        )
        build_log = (self.reports / "docker-build.log").read_text()
        digests = [
            line
            for line in build_log.splitlines()
            if "sha256:" in line and ("docker.io/" in line or "ghcr.io/" in line)
        ]
        provenance = metadata.get("buildx.build.provenance", {})
        require(
            bool(digests) or bool(provenance.get("materials")),
            "No public base-image digest evidence",
        )
        self.report["public_image_digest_lines"] = digests
        self.private.mkdir(mode=0o700)
        environment = {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "OH_ENABLE_VSCODE": "false",
            "OH_PRELOAD_TOOLS": "false",
            "OH_ENABLE_BROWSER": "false",
            "OH_SESSION_API_KEYS_0": secrets.token_urlsafe(32),
            "OH_SECRET_KEY": secrets.token_urlsafe(32),
        }
        for filename, key in (
            ("session_key", "OH_SESSION_API_KEYS_0"),
            ("secret_key", "OH_SECRET_KEY"),
        ):
            path = self.private / filename
            path.write_text(environment[key])
            path.chmod(0o600)
            self.cli.REDACT.add(environment[key], filename)
        env_file = self.private / "container.env"
        env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in environment.items())
        )
        env_file.chmod(0o600)
        self.docker(
            [
                "create",
                "--name",
                self.owner["name"],
                "--label",
                f"{OWNER_LABEL}={self.owner['token']}",
                "--publish",
                "127.0.0.1::8000",
                "--memory",
                "4g",
                "--cpus",
                "2",
                "--pids-limit",
                "512",
                "--env-file",
                str(env_file),
                image,
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                "--import-modules",
                "openhands.tools.research_harness.definition",
            ]
        )
        self.docker(["start", self.owner["name"]])
        before = self.inspect_container()
        self.report["container_before_restart"] = before
        require(
            before is not None and before["state"]["Running"],
            "Owned container did not start",
        )
        assert before is not None
        self.attach_endpoint(before, "attach-before")
        self.api("ready-before", "GET", "/ready", readiness=True)
        self.invoke("doctor-before", ["doctor", "--run", self.owner["run"]])
        info = self.api("server-info-before", "GET", "/server_info")
        require(
            info["build_git_sha"] == manifest["checkout_sha"],
            "Server build SHA differs",
        )
        require(
            self.api("count-before", "GET", "/api/conversations/count") == 0,
            "Unexpected conversation",
        )
        self.report["installed"] = self.worker("provenance", manifest)
        seeded = self.worker("seed")
        original = seeded["original"]
        reference = {"thread_id": original["thread_id"], "memory_id": original["id"]}
        query = {"thread_id": original["thread_id"], "query": "inventory", "limit": 5}
        require(
            self.memory("get-before", "memory.get", reference)["record"] == original,
            "Memory get differs",
        )
        require(
            [
                entry["record"]
                for entry in self.memory("recall-before", "memory.recall", query)[
                    "records"
                ]
            ]
            == [original],
            "Recall scope differs",
        )
        updated = self.worker("update")["updated"]
        self.restart_and_wait(before)
        self.invoke("doctor-after", ["doctor", "--run", self.owner["run"]])
        require(
            self.api("server-info-after", "GET", "/server_info")["build_git_sha"]
            == manifest["checkout_sha"],
            "Restarted build SHA differs",
        )
        require(
            self.memory("get-after", "memory.get", reference)["record"] == updated,
            "Restarted memory differs",
        )
        require(
            [
                entry["record"]
                for entry in self.memory("recall-after", "memory.recall", query)[
                    "records"
                ]
            ]
            == [updated],
            "Restarted recall scope differs",
        )
        history = self.memory("history-after", "memory.history", reference)
        require(
            history["records"] == [original, updated] and history["has_more"] is False,
            "Version history differs",
        )
        require(
            self.api("count-after", "GET", "/api/conversations/count") == 0,
            "Unexpected model conversation",
        )
        self.report["checks_passed"] = True

    def cleanup(self) -> dict[str, Any]:
        result: dict[str, Any] = {"errors": []}
        try:
            if self.owner:
                require(
                    re.fullmatch(r"research-memory-[0-9a-f]{32}", self.owner["name"])
                    is not None,
                    "Invalid owned container name",
                )
                info = self.inspect_container()
                if info is not None:
                    logs = self.docker(
                        ["logs", "--tail", "500", self.owner["name"]], check=False
                    )
                    (self.reports / "server.log").write_text(
                        self.cli.REDACT.text(logs.stdout + logs.stderr)
                    )
                    try:
                        self.docker(
                            ["stop", "--time", "15", self.owner["name"]],
                            timeout=25,
                            check=False,
                        )
                    except subprocess.TimeoutExpired:
                        result["stop_timed_out"] = True
                    self.docker(["rm", "--force", self.owner["name"]], timeout=25)
                require(self.inspect_container() is None, "Owned container remains")
                result["container_removed"] = True
                if "port" in self.owner:
                    with socket.socket() as probe:
                        probe.settimeout(2)
                        result["port_closed"] = (
                            probe.connect_ex(("127.0.0.1", self.owner["port"])) != 0
                        )
                    require(result["port_closed"], "Owned loopback port remains open")
                image = self.docker(
                    [
                        "image",
                        "inspect",
                        "--format",
                        "{{.Id}}",
                        self.owner["image_tag"],
                    ],
                    check=False,
                )
                if image.returncode == 0:
                    require(
                        image.stdout.strip() == self.owner["image_id"],
                        "Refusing to remove a changed image tag",
                    )
                    self.docker(["image", "rm", self.owner["image_tag"]], timeout=30)
                else:
                    require(
                        "no such" in image.stderr.lower(), "Image removal is unverified"
                    )
                result["image_tag_removed"] = True
        except Exception as error:
            result["errors"].append(self.cli.REDACT.text(str(error)))
        try:
            profiles = []
            for run_json in sorted((self.root / "verify").glob("*/run.json")):
                require(
                    json.loads(run_json.read_text())["mode"] == "attach",
                    "Unexpected non-attach verification run",
                )
                self.invoke(
                    f"stop-verification-{run_json.parent.name}",
                    ["stop", "--run", str(run_json.parent), "--purge-private"],
                )
                evidence = run_json.parent / "evidence"
                if evidence.exists():
                    shutil.copytree(
                        evidence,
                        self.reports / "server-evidence" / run_json.parent.name,
                        dirs_exist_ok=True,
                    )
                profiles.append(run_json.parent.name)
            shutil.rmtree(self.root / "verify", ignore_errors=True)
            result["verification_profiles_removed"] = profiles
        except Exception as error:
            result["errors"].append(self.cli.REDACT.text(str(error)))
        # Keep the private redaction keys for an always-run cleanup retry if
        # container removal failed. Only reports/, never private/, is uploaded.
        if not self.owner or result.get("container_removed"):
            shutil.rmtree(self.private, ignore_errors=True)
        result["private_state_removed"] = (
            not self.private.exists() and not (self.root / "verify").exists()
        )
        result["ok"] = not result["errors"] and result["private_state_removed"]
        write_json(self.reports / "cleanup.json", result)
        return result


def main() -> int:
    os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--sdist", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument(
        "--_worker",
        choices=("provenance", "seed", "update", "children"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--manifest-json", default="{}", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._worker:
        print(
            json.dumps(container_worker(args._worker, json.loads(args.manifest_json)))
        )
        return 0
    require(
        args.root is not None and args.checkout is not None,
        "--root and --checkout are required",
    )
    root, checkout = args.root.resolve(), args.checkout.resolve()
    require(
        root.is_dir() and root.name.startswith("research-harness-container."),
        "Expected the workflow's owned temporary directory",
    )
    require(
        root.stat().st_uid == os.getuid() and root.stat().st_mode & 0o077 == 0,
        "Acceptance root must be private and owned",
    )
    check = ContainerCheck(checkout, root)
    if args.cleanup:
        if not check.owner and args.image:
            try:
                check.claim_image(args.image, missing_ok=True)
            except Exception as error:
                write_json(
                    check.reports / "cleanup.json",
                    {
                        "ok": False,
                        "errors": [check.cli.REDACT.text(str(error))],
                        "image_removal": "refused: identity could not be verified",
                    },
                )
                return 1
        return 0 if check.cleanup()["ok"] else 1
    try:
        require(
            args.image is not None and args.sdist is not None,
            "--image and --sdist are required",
        )
        check.run(args.image, args.sdist.resolve())
    except Exception:
        check.report["error"] = check.cli.REDACT.text(traceback.format_exc())
    finally:
        check.report["cleanup"] = check.cleanup()
        passed = (
            check.report.get("checks_passed", False) and check.report["cleanup"]["ok"]
        )
        check.report["status"] = "passed" if passed else "failed"
        check.report["finished_at"] = datetime.now(UTC).isoformat()
        write_json(
            check.reports / "container-acceptance.json",
            check.cli.REDACT.obj(check.report),
        )
        print(
            json.dumps(
                {
                    "status": check.report["status"],
                    "report": str(check.reports / "container-acceptance.json"),
                }
            )
        )
    return 0 if check.report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
