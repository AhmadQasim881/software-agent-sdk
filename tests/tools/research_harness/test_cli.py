import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import JsonValue

from openhands.tools.research_harness import cli, evaluation
from openhands.tools.research_harness.store import HarnessStore


def _run(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    for name in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "LLM_MODEL",
        "OPENHANDS_SUPPRESS_BANNER",
        "LITELLM_LOCAL_MODEL_COST_MAP",
    ):
        environment.pop(name, None)
    return subprocess.run(
        [sys.executable, "-I", "-m", "openhands.research", *arguments],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_real_cli_restarts_share_state_and_stdout_is_json(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("import json\n", encoding="utf-8")
    state = tmp_path / "state"
    prefix = ("--workspace", str(workspace), "--state", str(state))
    created = _run(tmp_path, *prefix, "thread.create", '{"question":"Which imports?"}')
    assert created.returncode == 0, created.stderr
    thread_id = json.loads(created.stdout)["record"]["id"]
    observed = _run(
        tmp_path, *prefix, "inventory", json.dumps({"thread_id": thread_id})
    )
    assert observed.returncode == 0, observed.stderr
    result = json.loads(observed.stdout)
    resumed = _run(
        tmp_path, *prefix, "run.get", json.dumps({"run_id": result["run_id"]})
    )
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["record"]["status"] == "completed"
    store = HarnessStore(state, workspace)
    assert len(store.list_evidence(thread_id)) == 1


def test_cli_schema_and_error_exit_codes_without_model_credentials(
    tmp_path: Path,
) -> None:
    schema = _run(tmp_path, "schema", "inventory")
    assert schema.returncode == 0, schema.stderr
    assert (
        "thread_id" in json.loads(schema.stdout)["schemas"]["inventory"]["properties"]
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "broken.py").write_text("def broken(\n", encoding="utf-8")
    state = tmp_path / "state"
    store = HarnessStore(state, workspace)
    thread = store.create_thread("Check incomplete inventory")
    prefix = ("--workspace", str(workspace), "--state", str(state))
    failure = _run(tmp_path, *prefix, "inventory", json.dumps({"thread_id": thread.id}))
    assert failure.returncode == 1
    assert json.loads(failure.stdout)["status"] == "failed"
    invalid = _run(tmp_path, *prefix, "inventory", '{"thread_id":123}')
    assert invalid.returncode == 2 and json.loads(invalid.stdout)["ok"] is False
    assert len(store.list_runs(thread.id)) == 1


@pytest.mark.parametrize("host", ["codex", "claude-code", "claude-desktop"])
def test_cli_host_config_writes_review_bundle_without_initializing_state(
    host: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    destination = tmp_path / "review"
    arguments = [
        "--workspace",
        str(workspace),
        "--state",
        str(state),
        "--allow-github",
        "host-config",
        "--host",
        host,
        "--output",
        str(destination),
        "--python",
        sys.executable,
    ]
    assert cli.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["configuration_installed"] is False
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["host"] == host and manifest["allow_github"] is True
    assert manifest["python_executable"] == sys.executable
    assert not state.exists()
    before = (destination / "manifest.json").read_bytes()
    assert cli.main(arguments) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert (destination / "manifest.json").read_bytes() == before


@pytest.mark.parametrize(
    "arguments",
    [
        ["host-config", "--host", "codex", "--output", "review"],
        ["host-hook", "--host", "claude-code", "--thread", "thread-id"],
    ],
)
def test_host_operator_commands_require_explicit_state(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2
    assert "--state is required" in capsys.readouterr().err


def test_generated_claude_hook_commands_run_through_real_cli(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    state = tmp_path / "state"
    store = HarnessStore(state, workspace)
    thread = store.create_thread("Resume the recorded local review")
    destination = tmp_path / "review"
    generated = _run(
        tmp_path,
        "--workspace",
        str(workspace),
        "--state",
        str(state),
        "host-config",
        "--host",
        "claude-code",
        "--output",
        str(destination),
        "--thread",
        thread.id,
    )
    assert generated.returncode == 0, generated.stderr
    hooks = json.loads((destination / "claude-code.settings.json").read_text())["hooks"]
    for event_name in ("SessionStart", "PreCompact", "SessionEnd"):
        handler = hooks[event_name][0]["hooks"][0]
        completed = subprocess.run(
            [handler["command"], *handler["args"]],
            input=json.dumps(
                {
                    "session_id": "actual-cli-session",
                    "hook_event_name": event_name,
                    "cwd": str(workspace),
                }
            ),
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        if event_name == "SessionStart":
            output = json.loads(completed.stdout)["hookSpecificOutput"]
            assert output["hookEventName"] == "SessionStart"
            assert json.loads(output["additionalContext"])["thread"]["id"] == thread.id
        elif event_name == "PreCompact":
            assert thread.id in completed.stdout and len(completed.stdout) < 10_000
        else:
            assert completed.stdout == ""
    observations = [
        event
        for event in store.list_events(thread_id=thread.id)
        if event.kind == "host.observed"
    ]
    assert len(observations) == 3
    assert all(event.session_id == "actual-cli-session" for event in observations)


def test_evaluation_cli_reports_missing_credentials_without_state_or_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_name = "RESEARCH_CLI_TEST_KEY"
    monkeypatch.delenv(key_name, raising=False)
    result = _run(
        tmp_path,
        "evaluate-skill",
        "--model",
        "openai/test-model",
        "--api-key-env",
        key_name,
        "--max-budget-usd",
        "0.01",
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is False and report["status"] == "blocked"
    assert report["cost_usd"] == 0 and report["model_calls"] == 0
    assert report["max_iterations"] == 8
    assert len(result.stdout.encode("utf-8")) <= 65_537
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("status", "ok", "exit_code"),
    [
        ("passed", True, 0),
        ("failed", False, 1),
        ("blocked", False, 1),
        ("blocked", True, 1),
    ],
)
def test_evaluation_cli_preserves_result_and_exit_status(
    status: str,
    ok: bool,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def evaluate(
        *, model: str, api_key_env: str, max_budget_usd: float, max_iterations: int
    ) -> dict[str, JsonValue]:
        return {
            "ok": ok,
            "status": status,
            "model": model,
            "api_key_env": api_key_env,
            "max_budget_usd": max_budget_usd,
            "max_iterations": max_iterations,
        }

    monkeypatch.setattr(evaluation, "evaluate_from_environment", evaluate)
    code = cli.main(
        [
            "evaluate-skill",
            "--model",
            "openai/test-model",
            "--max-budget-usd",
            "0.02",
            "--max-iterations",
            "3",
        ]
    )
    assert code == exit_code
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "ok": ok,
        "status": status,
        "model": "openai/test-model",
        "api_key_env": "LLM_API_KEY",
        "max_budget_usd": 0.02,
        "max_iterations": 3,
    }


def test_evaluation_cli_rejects_oversized_utf8_result_without_truncating_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def oversized(**arguments: object) -> dict[str, JsonValue]:
        return {"ok": True, "status": "passed", "detail": "ا" * 40_000}

    monkeypatch.setattr(evaluation, "evaluate_from_environment", oversized)
    code = cli.main(
        [
            "evaluate-skill",
            "--model",
            "openai/test-model",
            "--max-budget-usd",
            "0.01",
        ]
    )
    assert code == 2
    output = capsys.readouterr().out
    assert len(output.encode("utf-8")) < 1024
    assert json.loads(output) == {
        "ok": False,
        "error": "Evaluation result exceeds 65536 UTF-8 bytes",
    }
