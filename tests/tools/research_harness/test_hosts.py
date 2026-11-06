"""Portable review bundles, real MCP discovery, and Claude lifecycle protocol."""

import io
import json
import sys
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from openhands.tools.research_harness.hosts import (
    HostName,
    render_host_bundle,
    run_host_hook,
    write_host_bundle,
)
from openhands.tools.research_harness.models import HarnessError
from openhands.tools.research_harness.store import HarnessStore
from openhands.tools.research_harness.workflow import workflow_path


@pytest.fixture
def host_paths(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "project space ' α"
    workspace.mkdir()
    return workspace, tmp_path / "state space ' α"


@pytest.mark.parametrize("host", ["codex", "claude-code", "claude-desktop"])
def test_host_bundle_serializes_explicit_paths_without_initializing_state(
    host: HostName, host_paths: tuple[Path, Path]
) -> None:
    workspace, state = host_paths
    bundle = render_host_bundle(
        host=host, workspace=workspace, state_dir=state, allow_github=True
    )
    if host == "codex":
        server = tomllib.loads(bundle["codex.config.toml"])["mcp_servers"][
            "research_harness"
        ]
    else:
        config_name = (
            "claude-code.mcp.json"
            if host == "claude-code"
            else "claude-desktop.config.json"
        )
        server = json.loads(bundle[config_name])["mcpServers"]["research_harness"]
    assert server["command"] == sys.executable
    assert server["args"] == [
        "-I",
        "-m",
        "openhands.research",
        "--workspace",
        str(workspace),
        "--state",
        str(state),
        "--allow-github",
        "mcp",
    ]
    assert server["env"]["OPENHANDS_SUPPRESS_BANNER"] == "1"
    assert not state.exists()
    assert bundle["skills/research-review/SKILL.md"] == workflow_path().read_text()
    manifest = json.loads(bundle["manifest.json"])
    assert manifest["lifecycle_hooks"] == []
    assert manifest["host_installation_verified"] is False
    assert manifest["agentmemory_node_daemon_included"] is False


def test_claude_code_hooks_are_explicit_exec_argv(
    host_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    workspace, state = host_paths
    python = tmp_path / "python interpreter"
    python.symlink_to(sys.executable)
    bundle = render_host_bundle(
        host="claude-code",
        workspace=workspace,
        state_dir=state,
        python_executable=python,
        thread_id="thread with ' quotes",
    )
    hooks = json.loads(bundle["claude-code.settings.json"])["hooks"]
    assert set(hooks) == {"SessionStart", "PreCompact", "SessionEnd"}
    for entries in hooks.values():
        command = entries[0]["hooks"][0]
        assert command["type"] == "command"
        assert command["command"] == str(python)
        assert command["args"][-5:] == [
            "host-hook",
            "--host",
            "claude-code",
            "--thread",
            "thread with ' quotes",
        ]
        assert "--allow-github" not in command["args"]


@pytest.mark.parametrize("host", ["codex", "claude-desktop"])
def test_thread_id_does_not_enable_unsupported_host_hooks(
    host: HostName, host_paths: tuple[Path, Path]
) -> None:
    workspace, state = host_paths
    bundle = render_host_bundle(
        host=host, workspace=workspace, state_dir=state, thread_id="known-thread"
    )
    assert json.loads(bundle["manifest.json"])["lifecycle_hooks"] == []
    assert "claude-code.settings.json" not in bundle


@pytest.mark.parametrize(
    "invalid", ["host", "workspace", "state", "python", "missing-python", "thread"]
)
def test_invalid_host_configuration_is_rejected_without_writes(
    invalid: str, host_paths: tuple[Path, Path]
) -> None:
    workspace, state = host_paths
    with pytest.raises(HarnessError):
        render_host_bundle(
            host=cast(HostName, "unknown" if invalid == "host" else "codex"),
            workspace=Path(sys.executable) if invalid == "workspace" else workspace,
            state_dir=workspace / "state" if invalid == "state" else state,
            python_executable=(
                Path("python")
                if invalid == "python"
                else state / "missing-python"
                if invalid == "missing-python"
                else None
            ),
            thread_id="invalid\nthread" if invalid == "thread" else None,
        )
    assert not state.exists()


def test_write_bundle_creates_only_the_review_destination(
    host_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    workspace, state = host_paths
    destination = tmp_path / "review"
    result = write_host_bundle(
        host="claude-desktop",
        workspace=workspace,
        state_dir=state,
        destination=destination,
    )
    assert result["ok"] is True
    assert result["configuration_installed"] is False
    assert (destination / "manifest.json").is_file()
    assert (destination / "skills/research-review/SKILL.md").read_text() == (
        workflow_path().read_text()
    )
    assert list(workspace.iterdir()) == []
    assert not state.exists()


@pytest.mark.parametrize("existing", ["directory", "file", "symlink"])
def test_write_bundle_never_replaces_existing_destination(
    existing: str, host_paths: tuple[Path, Path], tmp_path: Path
) -> None:
    workspace, state = host_paths
    destination = tmp_path / "review"
    if existing == "directory":
        destination.mkdir()
        sentinel = destination / "settings.json"
        sentinel.write_text("preserve me")
    elif existing == "file":
        destination.write_text("preserve me")
        sentinel = destination
    else:
        sentinel = tmp_path / "untouched"
        sentinel.write_text("preserve me")
        destination.symlink_to(sentinel)
    with pytest.raises(HarnessError, match="destination must not exist"):
        write_host_bundle(
            host="codex", workspace=workspace, state_dir=state, destination=destination
        )
    assert sentinel.read_text() == "preserve me"
    if existing == "symlink":
        assert destination.is_symlink()


def test_failed_bundle_write_cleans_up_only_files_it_created(
    host_paths: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, state = host_paths
    destination = tmp_path / "review"
    original_open = Path.open

    def fail_readme(path: Path, *args, **kwargs):
        if path == destination / "README.md":
            (destination / "unrelated.txt").write_text("preserve concurrent work")
            raise OSError("Simulated write failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_readme)
    with pytest.raises(HarnessError, match="Could not create"):
        write_host_bundle(
            host="codex", workspace=workspace, state_dir=state, destination=destination
        )
    assert list(destination.iterdir()) == [destination / "unrelated.txt"]
    assert (destination / "unrelated.txt").read_text() == "preserve concurrent work"


async def test_generated_mcp_configuration_discovers_and_restores_saved_thread(
    host_paths: tuple[Path, Path],
) -> None:
    workspace, state = host_paths
    bundle = render_host_bundle(host="codex", workspace=workspace, state_dir=state)
    server = tomllib.loads(bundle["codex.config.toml"])["mcp_servers"][
        "research_harness"
    ]
    parameters = StdioServerParameters(**server)
    thread_id = ""
    for restarting in (False, True):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                await session.initialize()
                discovery = await session.list_tools()
                assert [tool.name for tool in discovery.tools] == ["research_harness"]
                operation = "thread.get" if restarting else "thread.create"
                arguments = (
                    {"thread_id": thread_id}
                    if restarting
                    else {"question": "Restore the host review after restart"}
                )
                result = await session.call_tool(
                    "research_harness", {"operation": operation, "arguments": arguments}
                )
                assert not result.isError and result.structuredContent is not None
                record = result.structuredContent["record"]
                assert record["question"] == "Restore the host review after restart"
                if restarting:
                    assert record["id"] == thread_id
                else:
                    thread_id = record["id"]


def test_claude_lifecycle_restores_saved_context_without_reading_transcripts(
    host_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, state = host_paths
    store = HarnessStore(state, workspace)
    thread = store.create_thread("Track the documented readiness evidence")
    nested = workspace / "nested"
    nested.mkdir()
    transcript = workspace / "transcript.jsonl"
    transcript.write_text("UNSAVED TRANSCRIPT MUST NOT BE IMPORTED")
    for name in ("SessionStart", "PreCompact", "SessionEnd"):
        event = {
            "session_id": "claude-session",
            "cwd": str(nested),
            "hook_event_name": name,
            "transcript_path": str(transcript),
            "tool_input": {"private": "ARBITRARY BODY MUST NOT BE IMPORTED"},
        }
        stream = io.TextIOWrapper(
            io.BytesIO(json.dumps(event).encode()), encoding="utf-8"
        )
        monkeypatch.setattr(sys, "stdin", stream)
        assert (
            run_host_hook(
                host="claude-code",
                workspace=workspace,
                state_dir=state,
                thread_id=thread.id,
            )
            == 0
        )
        output = capsys.readouterr()
        assert output.err == ""
        if name == "SessionStart":
            hook_output = json.loads(output.out)["hookSpecificOutput"]
            assert hook_output["hookEventName"] == "SessionStart"
            saved_context = json.loads(hook_output["additionalContext"])
            assert saved_context["thread"]["id"] == thread.id
            assert saved_context["thread"]["question"] == thread.question
        elif name == "PreCompact":
            assert thread.id in output.out and thread.question in output.out
            assert len(output.out) < 10_000
        else:
            assert output.out == ""
    restored = HarnessStore(state, workspace)
    observations = [
        event
        for event in restored.list_events(thread_id=thread.id)
        if event.kind == "host.observed"
    ]
    assert [event.payload["hook_event_name"] for event in observations] == [
        "SessionStart",
        "PreCompact",
        "SessionEnd",
    ]
    assert all(event.session_id == "claude-session" for event in observations)
    assert all("manifest_sha" in event.payload for event in observations[:2])
    assert "manifest_sha" not in observations[2].payload
    persisted = json.dumps([event.model_dump(mode="json") for event in observations])
    assert "UNSAVED TRANSCRIPT" not in persisted
    assert "ARBITRARY BODY" not in persisted
    assert "transcript_path" not in persisted


@pytest.mark.parametrize(
    "invalid", ["host", "native-event", "cwd", "session", "oversize", "malformed"]
)
def test_host_hook_rejects_bad_protocol_input_without_success_output(
    invalid: str,
    host_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, state = host_paths
    event = {
        "session_id": "invalid\nsession" if invalid == "session" else "session",
        "cwd": str(workspace.parent if invalid == "cwd" else workspace),
        "hook_event_name": "SessionStart",
    }
    if invalid == "native-event":
        event["event_type"] = event.pop("hook_event_name")
    raw = json.dumps(event).encode()
    if invalid == "oversize":
        raw = b" " * 262_145
    elif invalid == "malformed":
        raw = b"{"
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    )
    status = run_host_hook(
        host="codex" if invalid == "host" else "claude-code",
        workspace=workspace,
        state_dir=state,
        thread_id="configured-thread",
    )
    output = capsys.readouterr()
    assert status == 1
    assert output.out == ""
    assert "Research host hook failed:" in output.err
    assert not state.exists()
