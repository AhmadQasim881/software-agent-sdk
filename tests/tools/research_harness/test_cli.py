import json
import os
import subprocess
import sys
from pathlib import Path

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
        [sys.executable, "-m", "openhands.research", *arguments],
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
