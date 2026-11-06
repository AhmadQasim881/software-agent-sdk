"""Verify local Agent Server memory reads and SDK registration without inference.

Run from the repository root:
    .venv/bin/python -I .pr/research-harness-remote-memory-check.py

The maintained server verification CLI owns launch, HTTP evidence, restart and
cleanup. Memory writes use the local operator service; /api/mcp/test is used
only for reads. No conversation receives a message or starts an agent run.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Protocol, cast


os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"


class VerificationCLI(Protocol):
    def main(self, argv: list[str] | None = None) -> int: ...


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def sdk_check(phase: str, fixture_path: Path, output: Path) -> None:
    """Use the public client API, including its authenticated events connection."""
    from openhands.sdk import LLM, Agent, RemoteConversation, RemoteWorkspace, Tool
    from openhands.sdk.context import AgentContext
    from openhands.tools.research_harness.definition import ResearchHarnessTool

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    workspace = RemoteWorkspace(
        host=os.environ["AGENT_SERVER_URL"],
        api_key=os.environ["SESSION_API_KEY"],
        working_dir=fixture["workspace"],
    )
    conversation = None
    try:
        if phase == "create":
            agent = Agent(
                llm=LLM(model="openai/gpt-4o-mini", usage_id="memory-no-inference"),
                tools=[
                    Tool(
                        name=ResearchHarnessTool.name,
                        params={"state_dir": fixture["state"]},
                    )
                ],
                agent_context=AgentContext(
                    load_user_skills=False,
                    load_public_skills=False,
                    load_project_skills=False,
                    load_memory=False,
                ),
            )
            conversation = RemoteConversation(
                agent=agent,
                workspace=workspace,
                visualizer=None,
                delete_on_close=False,
                max_iteration_per_run=1,
                tags={"acceptance": "memory-registration-no-inference"},
            )
            fixture["conversation_id"] = str(conversation.id)
            write_json(fixture_path, fixture)
        else:
            conversation = RemoteConversation.attach(
                workspace, fixture["conversation_id"], visualizer=None
            )
        metrics = conversation.conversation_stats.get_combined_metrics()
        event_kinds = dict(
            Counter(type(event).__name__ for event in conversation.state.events)
        )
        tool_config = [
            tool.model_dump(mode="json") for tool in conversation.state.agent.tools
        ]
        assert tool_config == [
            {"name": "research_harness", "params": {"state_dir": fixture["state"]}}
        ], tool_config
        assert conversation.state.execution_status.value == "idle"
        assert metrics.accumulated_cost == 0.0
        assert not metrics.costs and not metrics.response_latencies
        assert not metrics.token_usages
        assert metrics.accumulated_token_usage is not None
        assert metrics.accumulated_token_usage.prompt_tokens == 0
        assert metrics.accumulated_token_usage.completion_tokens == 0
        assert (
            not {"ActionEvent", "ObservationEvent", "MessageEvent"} & event_kinds.keys()
        )
        result = {
            "phase": phase,
            "conversation_id": str(conversation.id),
            "execution_status": conversation.state.execution_status.value,
            "tools": tool_config,
            "event_kinds": event_kinds,
            "metrics": metrics.model_dump(mode="json"),
            "messages_sent": 0,
            "agent_runs_requested": 0,
        }
        write_json(output, result)
        print(json.dumps(result))
    finally:
        if conversation is not None:
            conversation.close()
        workspace.reset_client()


def run_acceptance() -> None:
    import psutil

    from openhands.tools.research_harness.service import HarnessService

    repo = Path(__file__).resolve().parent.parent
    cli_path = (
        repo / ".agents/skills/verify-agent-server/scripts/control_agent_server.py"
    )
    spec = importlib.util.spec_from_file_location("memory_acceptance_cli", cli_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cli = cast(VerificationCLI, module)
    root = Path(tempfile.mkdtemp(prefix="research-harness-remote-memory-"))
    os.environ["AGENT_SERVER_VERIFY_HOME"] = str(root)
    observations: dict[str, Any] = {"verification_home": str(root)}
    run: Path | None = None

    def invoke(label: str, arguments: list[str]) -> dict[str, Any]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(arguments)
        payload = json.loads(buffer.getvalue())
        observations[label] = payload
        print(
            json.dumps({"step": label, "exit_code": code, "ok": payload.get("ok")}),
            flush=True,
        )
        if code != 0:
            raise RuntimeError(
                f"{label} failed; saved observations contain the response"
            )
        return payload

    def api(label: str, method: str, path: str, body: object | None = None) -> Any:
        arguments = [
            "api",
            method,
            path,
            "--run",
            str(run),
            "--expect",
            "200",
            "--save",
            f"research-memory/{label}",
            "--timeout",
            "45",
        ]
        if body is not None:
            arguments.extend(["--json", json.dumps(body)])
        return invoke(label, arguments)["response"]["body"]

    def local_call(
        service: HarnessService, operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = service.execute(operation, arguments, session_id="acceptance-operator")
        assert result["ok"] is True, result
        return result

    try:
        launched = invoke(
            "launch",
            [
                "launch",
                "--new",
                "--name",
                "research-memory",
                "--checkout",
                str(repo),
                "--env",
                "OPENHANDS_SUPPRESS_BANNER=1",
                "--env",
                "LITELLM_LOCAL_MODEL_COST_MAP=True",
                "--timeout",
                "60",
            ],
        )
        run = Path(launched["run"])
        invoke("doctor-before", ["doctor", "--run", str(run)])
        info = api("server-info-before", "GET", "/server_info")
        assert info["build_git_sha"] == launched["checkout_sha"]
        assert api("count-before", "GET", "/api/conversations/count") == 0
        fixture_root = run / "fixtures" / "research-memory"
        workspace = fixture_root / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "sample.py").write_text("import json\n", encoding="utf-8")
        state = fixture_root / "state"
        service = HarnessService(workspace=workspace, state_dir=state)
        thread = local_call(
            service,
            "thread.create",
            {
                "question": "Which static import is present in the owned sample?",
            },
        )["record"]
        inventory = local_call(service, "inventory", {"thread_id": thread["id"]})
        assert inventory["status"] == "completed", inventory
        original = local_call(
            service,
            "memory.remember",
            {
                "thread_id": thread["id"],
                "kind": "decision",
                "text": "Read the saved inventory before changing the sample.",
                "source": "Owned acceptance operator",
                "tags": ["inventory"],
                "evidence_ids": [inventory["evidence_id"]],
            },
        )["record"]
        other_thread = local_call(
            service,
            "thread.create",
            {
                "question": "Separate owned thread for recall isolation",
            },
        )["record"]
        other = local_call(
            service,
            "memory.remember",
            {
                "thread_id": other_thread["id"],
                "text": "Inventory note in the other thread",
                "source": "Owned operator",
            },
        )["record"]
        fixture = {"workspace": str(workspace), "state": str(state)}
        fixture_path = fixture_root / "fixture.json"
        write_json(fixture_path, fixture)
        write_json(
            run / "evidence/research-memory/seed.json",
            {
                "thread": thread,
                "inventory": inventory,
                "original": original,
                "other_thread_record": other,
            },
        )

        def memory_read(
            label: str, operation: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            result = api(
                label,
                "POST",
                "/api/mcp/test",
                {
                    "name": "research-memory-acceptance",
                    "server": {
                        "transport": "stdio",
                        "command": str(repo / ".venv/bin/python"),
                        "args": [
                            "-I",
                            "-m",
                            "openhands.research",
                            "--workspace",
                            str(workspace),
                            "--state",
                            str(state),
                            "mcp",
                        ],
                        "cwd": str(workspace),
                        "env": {
                            "OPENHANDS_SUPPRESS_BANNER": "1",
                            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                        },
                    },
                    "timeout": 30,
                    "tool_call": {
                        "name": "research_harness",
                        "arguments": {
                            "operation": operation,
                            "arguments": arguments,
                        },
                    },
                },
            )
            assert result["ok"] is True, result
            assert "research_harness" in result["tools"]
            assert result["tool_result"]["is_error"] is False, result
            parsed = json.loads(result["tool_result"]["text"])
            assert parsed["ok"] is True, parsed
            run_info = json.loads((run / "run.json").read_text(encoding="utf-8"))
            remaining_children = []
            for process in psutil.Process(run_info["pid"]).children(recursive=True):
                try:
                    command = process.cmdline()
                except psutil.NoSuchProcess:
                    continue
                if "openhands.research" in command and str(state) in command:
                    remaining_children.append(process.pid)
            observations[f"{label}-remaining-mcp-children"] = remaining_children
            assert remaining_children == [], remaining_children
            return parsed

        reference = {"thread_id": thread["id"], "memory_id": original["id"]}
        assert memory_read("get-before", "memory.get", reference)["record"] == original
        recalled = memory_read(
            "recall-before",
            "memory.recall",
            {
                "thread_id": thread["id"],
                "query": "inventory",
                "limit": 5,
            },
        )
        assert [item["record"] for item in recalled["records"]] == [original]

        def sdk(phase: str) -> None:
            invoke(
                f"sdk-{phase}",
                [
                    "exec",
                    "--run",
                    str(run),
                    "--save",
                    f"research-memory/sdk-{phase}",
                    "--timeout",
                    "60",
                    "--",
                    str(repo / ".venv/bin/python"),
                    "-I",
                    str(Path(__file__).resolve()),
                    "--sdk-phase",
                    phase,
                    "--fixture",
                    str(fixture_path),
                    "--output",
                    str(run / f"evidence/research-memory/sdk-{phase}-result.json"),
                ],
            )

        sdk("create")
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        conversation_path = f"/api/conversations/{fixture['conversation_id']}"
        conversation_before = api("conversation-before", "GET", conversation_path)
        expected_module = "openhands.tools.research_harness.definition"
        assert (
            conversation_before["tool_module_qualnames"]["research_harness"]
            == expected_module
        )
        assert api("count-created", "GET", "/api/conversations/count") == 1

        updated = local_call(
            service,
            "memory.update",
            {
                **reference,
                "expected_version": 1,
                "text": "Keep the saved inventory and its source evidence together.",
            },
        )["record"]
        assert updated["version"] == 2
        assert updated["evidence_ids"] == original["evidence_ids"]
        write_json(run / "evidence/research-memory/operator-update.json", updated)
        invoke("restart", ["restart", "--run", str(run), "--timeout", "60"])
        invoke("doctor-after", ["doctor", "--run", str(run)])
        assert (
            api("server-info-after", "GET", "/server_info")["build_git_sha"]
            == info["build_git_sha"]
        )
        assert memory_read("get-after", "memory.get", reference)["record"] == updated
        recalled = memory_read(
            "recall-after",
            "memory.recall",
            {
                "thread_id": thread["id"],
                "query": "inventory",
                "limit": 5,
            },
        )
        assert [item["record"] for item in recalled["records"]] == [updated]
        history = memory_read("history-after", "memory.history", reference)
        assert history["records"] == [original, updated]
        assert history["has_more"] is False
        sdk("attach")
        conversation_after = api("conversation-after", "GET", conversation_path)
        assert (
            conversation_after["tool_module_qualnames"]
            == conversation_before["tool_module_qualnames"]
        )
        assert conversation_after["id"] == conversation_before["id"]
        assert conversation_after["execution_status"] == "idle"
        assert api("count-attached", "GET", "/api/conversations/count") == 1
        api("delete-conversation", "DELETE", conversation_path)
        assert api("count-deleted", "GET", "/api/conversations/count") == 0

        invoke(
            "evidence-memory",
            [
                "evidence",
                "add",
                "--run",
                str(run),
                "--feature",
                "F19.test-stdio",
                "--result",
                "pass",
                "--entry",
                "POST /api/mcp/test",
                "--expected",
                "Read scoped memory through the real server before and after restart",
                "--actual",
                "Five successful MCP reads; exact records, provenance, recall scope "
                "and two-version history",
                "--artifact",
                "evidence/research-memory/get-before.json",
                "--artifact",
                "evidence/research-memory/recall-before.json",
                "--artifact",
                "evidence/research-memory/get-after.json",
                "--artifact",
                "evidence/research-memory/recall-after.json",
                "--artifact",
                "evidence/research-memory/history-after.json",
                "--note",
                "Read-only task scope; authored memory writes used the local "
                "operator service.",
            ],
        )
        invoke(
            "evidence-sdk",
            [
                "evidence",
                "add",
                "--run",
                str(run),
                "--feature",
                "research-memory.sdk-registration",
                "--allow-unknown",
                "--result",
                "pass",
                "--entry",
                "RemoteConversation / RemoteConversation.attach",
                "--expected",
                "Create and reopen an idle native memory-tool conversation "
                "across server restart",
                "--actual",
                "Same id, tool params and persisted module mapping; zero model "
                "usage; closed client preserved conversation",
                "--artifact",
                "evidence/research-memory/sdk-create.json",
                "--artifact",
                "evidence/research-memory/sdk-attach.json",
                "--artifact",
                "evidence/research-memory/conversation-before.json",
                "--artifact",
                "evidence/research-memory/conversation-after.json",
                "--note",
                "Task-specific scope, not a full F33 agent-loop or WebSocket "
                "streaming test.",
            ],
        )
        invoke(
            "evidence-report",
            [
                "evidence",
                "report",
                "--run",
                str(run),
                "--out",
                str(run / "evidence/acceptance-report.md"),
            ],
        )
        observations["acceptance_passed"] = True
    finally:
        if run is None:
            candidates = list(root.glob("*/run.json"))
            if len(candidates) == 1:
                run = candidates[0].parent
        if run is not None:
            try:
                stopped = invoke("stop", ["stop", "--run", str(run), "--purge-private"])
                assert stopped["port_closed"] is True, stopped
                private_paths = [
                    "private",
                    "home",
                    "server",
                    "tmux",
                    "worktrees",
                    "fixtures",
                ]
                assert all(not (run / item).exists() for item in private_paths)
                observations["cleanup_verified"] = True
            finally:
                write_json(run / "evidence/acceptance-observations.json", observations)
                print(
                    json.dumps(
                        {
                            "run": str(run),
                            "acceptance_passed": observations.get(
                                "acceptance_passed", False
                            ),
                            "cleanup_verified": observations.get(
                                "cleanup_verified", False
                            ),
                        }
                    ),
                    flush=True,
                )
        else:
            write_json(root / "failed-launch-observations.json", observations)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-phase", choices=["create", "attach"])
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.sdk_phase:
        assert args.fixture is not None and args.output is not None
        sdk_check(args.sdk_phase, args.fixture, args.output)
    else:
        run_acceptance()
