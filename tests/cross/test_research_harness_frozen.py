"""Packaged entrypoint behavior without a provider call or a binary build."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from openhands.sdk.hooks.manager import HookManager
from openhands.tools.research_harness.hooks import make_hook_config
from openhands.tools.research_harness.hosts import render_host_bundle
from openhands.tools.research_harness.store import HarnessStore


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "openhands-agent-server/openhands/agent_server/frozen_entrypoint.py"


@pytest.fixture
def frozen_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if os.name != "posix":
        pytest.skip("The frozen-launcher fixture requires a POSIX shebang executable")
    launcher = tmp_path / "agent-server"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "sys.frozen = True\n"
        "sys.executable = __file__\n"
        "from openhands.agent_server.frozen_entrypoint import main\n"
        "main()\n"
    )
    launcher.chmod(0o700)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(launcher))
    return launcher


def test_frozen_native_hook_restores_memory_and_enforces_arguments(
    frozen_launcher: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "openhands.py").write_text("raise RuntimeError('shadow module')\n")
    store = HarnessStore(tmp_path / "state", workspace)
    thread = store.create_thread("Which display label was chosen?")
    store.create_memory(
        thread.id, "Display label: Cedar Lantern", source="Owned fixture"
    )
    manager = HookManager(
        config=make_hook_config(
            workspace=workspace, state_dir=store.state_dir, thread_id=thread.id
        ),
        working_dir=str(workspace),
        session_id="frozen-fixture",
    )
    start = manager.run_session_start()
    assert start[0].success is True
    assert "Cedar Lantern" in start[0].stdout
    allowed, results = manager.run_pre_tool_use(
        tool_name="research_harness",
        tool_input={"operation": "memory.get", "arguments": {"invalid": True}},
    )
    assert allowed is False
    assert results[0].exit_code == 2


def test_frozen_host_bundle_reopens_memory_via_its_generated_command(
    frozen_launcher: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = HarnessStore(tmp_path / "state", workspace)
    thread = store.create_thread("Which display label was chosen?")
    note = store.create_memory(
        thread.id, "Display label: Cedar Lantern", source="Owned fixture"
    )
    bundle = render_host_bundle(
        host="claude-desktop", workspace=workspace, state_dir=store.state_dir
    )
    config = json.loads(bundle["claude-desktop.config.json"])
    server = config["mcpServers"]["research_harness"]
    completed = subprocess.run(
        [
            server["command"],
            *server["args"][:-1],
            "memory.get",
            json.dumps({"thread_id": thread.id, "memory_id": note.id}),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        cwd=workspace,
        env={**os.environ, **server["env"]},
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["record"]["text"] == note.text
    assert result["record"]["source"] == note.source


def test_frozen_entrypoint_preserves_clean_json_output(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.pop("OPENHANDS_SUPPRESS_BANNER", None)
    environment.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    completed = subprocess.run(
        [sys.executable, "-I", str(ENTRYPOINT), "--research-harness", "schema"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "memory.recall" in json.loads(completed.stdout)["schemas"]


def test_frozen_entrypoint_does_not_accept_arbitrary_python_code(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "must-not-exist"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ENTRYPOINT),
            "-c",
            f"open({str(marker)!r}, 'w').write('unexpected')",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 2
    assert not marker.exists()


def test_frozen_http_worker_rejects_invalid_repository_without_sdk_imports(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ENTRYPOINT),
            "--research-harness-http-worker",
            "../invalid",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "owner/name" in completed.stderr
    assert not completed.stdout
