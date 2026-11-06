"""Drive the existing verification CLI for one owned frozen-server lifecycle."""

import argparse
import contextlib
import ctypes
import hashlib
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
BINARY = args.binary.resolve(strict=True)
with BINARY.open("rb") as binary_stream:
    binary_digest = hashlib.file_digest(binary_stream, "sha256").hexdigest()
ARTIFACTS = args.output.resolve()
ARTIFACTS.mkdir(parents=True, exist_ok=False)
CLI_PATH = ROOT / ".agents/skills/verify-agent-server/scripts/control_agent_server.py"
module_spec = importlib.util.spec_from_file_location(
    "packaging_verification_cli", CLI_PATH
)
assert module_spec is not None and module_spec.loader is not None
cli = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = cli
module_spec.loader.exec_module(cli)
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(36, 1, 0, 0, 0) == 0, "Cannot become the owning child subreaper"
os.environ["AGENT_SERVER_VERIFY_HOME"] = str(ARTIFACTS / "live")
os.environ["RESEARCH_PACKAGING_SMOKE_KEY"] = secrets.token_urlsafe(32)
transcript = []


def call(arguments, *, check=True, environment=None):
    completed = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-I", str(CLI_PATH), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    code = completed.returncode
    result = json.loads(completed.stdout)
    transcript.append({"argv": arguments, "exit_code": code, "result": result})
    (ARTIFACTS / "frozen-startup-transcript.json").write_text(
        json.dumps(transcript, indent=2) + "\n"
    )
    if check:
        assert code == 0, result
    return result


with socket.socket() as reservation:
    reservation.bind(("127.0.0.1", 0))
    port = reservation.getsockname()[1]
attached = call(
    [
        "attach",
        "--url",
        f"http://127.0.0.1:{port}",
        "--key-env",
        "RESEARCH_PACKAGING_SMOKE_KEY",
        "--name",
        "frozen-packaging",
    ]
)
run_path = Path(attached["run"])
run = cli.load_run(run_path)
run.data["checkout_sha"] = cli.git("rev-parse", "HEAD", cwd=ROOT)
run.data["checkout_ref"] = cli.git("rev-parse", "--abbrev-ref", "HEAD", cwd=ROOT)
run.save()
(run.private / "secret_key").write_text(secrets.token_urlsafe(32))
(run.private / "secret_key").chmod(0o600)
run.register_secrets()
for directory in (run.dir / "home", run.dir / "server", run.dir / "worktrees"):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
configuration = {
    "conversations_path": str(run.dir / "server/workspace/conversations"),
    "workspace_path": str(run.dir / "server/workspace/project"),
    "bash_events_dir": str(run.dir / "server/workspace/bash_events"),
    "conversation_worktree_root": str(run.dir / "worktrees"),
    "enable_vscode": False,
    "preload_tools": False,
}
(run.private / "config.json").write_text(json.dumps(configuration, indent=2) + "\n")
server_environment = cli.server_env(
    run,
    {
        "OPENHANDS_SUPPRESS_BANNER": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "UV_NO_SYNC": "1",
        "UV_OFFLINE": "1",
    },
    [],
)
startup_ok = False
try:
    call(
        [
            "helper",
            "start",
            "--run",
            str(run_path),
            "--name",
            "frozen-server",
            "--log",
            str(run.log_path),
            "--",
            str(BINARY),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--import-modules",
            "openhands.tools.research_harness.definition",
        ],
        environment=server_environment,
    )
    deadline = time.monotonic() + 120
    while not cli.port_open("127.0.0.1", port) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert cli.port_open("127.0.0.1", port), "Frozen server did not open its owned port"
    doctor = call(["doctor", "--run", str(run_path)])
    tools = call(
        [
            "api",
            "GET",
            "/api/tools/",
            "--run",
            str(run_path),
            "--expect",
            "200",
            "--check",
            ".",
            "contains",
            "research_harness",
            "--save",
            "packaging/frozen-tools",
        ]
    )
    catalog = call(
        [
            "api",
            "GET",
            "/api/tools/catalog",
            "--run",
            str(run_path),
            "--expect",
            "200",
            "--save",
            "packaging/frozen-catalog",
        ]
    )
    entries = catalog["response"]["body"]["tools"]
    assert any(entry["name"] == "research_harness" for entry in entries)
    info = call(
        [
            "api",
            "GET",
            "/server_info",
            "--run",
            str(run_path),
            "--expect",
            "200",
            "--save",
            "packaging/frozen-server-info",
        ]
    )
    assert info["response"]["body"]["version"] not in ("unknown", "")
    startup_ok = True
finally:
    if run.log_path.exists():
        (ARTIFACTS / "frozen-server-redacted.log").write_text(
            cli.REDACT.text(run.log_path.read_text(errors="replace"))
        )
    stopped = call(["stop", "--run", str(run_path), "--purge-private"], check=False)
    deadline = time.monotonic() + 5
    while cli.port_open("127.0.0.1", port) and time.monotonic() < deadline:
        time.sleep(0.1)
    port_closed = not cli.port_open("127.0.0.1", port)
    for _ in range(100):
        with contextlib.suppress(ChildProcessError):
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        remaining = cli.psutil.Process().children(recursive=True)
        if not remaining:
            break
        time.sleep(0.05)
    private_purged = all(
        not (run.dir / sub).exists()
        for sub in ("private", "home", "server", "tmux", "worktrees", "fixtures")
    )
    summary = {
        "binary_sha256": binary_digest,
        "startup_ok": startup_ok,
        "port_closed": port_closed,
        "private_state_purged": private_purged,
        "owned_children_remaining": len(remaining),
        "run": str(run.dir),
        "stop_ok": stopped["ok"],
    }
    (ARTIFACTS / "frozen-startup-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    print(json.dumps(summary, indent=2), flush=True)
    assert stopped["ok"] and port_closed and private_purged and not remaining, summary
