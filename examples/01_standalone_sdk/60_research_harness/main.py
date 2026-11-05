"""Exercise the local research harness without a model or paid API calls.

Run from the repository after installing its workspace packages:
    uv run python -I examples/01_standalone_sdk/60_research_harness/main.py

Source edits and persisted state live in one owned temporary directory, which
is removed after the checks. The fixture uses an owned loopback HTTP server;
this example does not request GitHub metadata or use external network services.
"""

import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import JsonValue


# Set these before the SDK's transitive imports can initialize LiteLLM.
os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from openhands.tools.research_harness.service import HarnessService  # noqa: E402


def call(
    service: HarnessService, operation: str, arguments: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    result = service.execute(operation, arguments, session_id="no-model-demo")
    if result.get("ok") is not True:
        raise RuntimeError(f"{operation} did not succeed: {result}")
    return result


def read_json(service: HarnessService, digest: JsonValue) -> dict[str, Any]:
    """Retrieve a complete verified artifact through the bounded service API."""
    assert isinstance(digest, str)
    content = bytearray()
    offset = 0
    while True:
        page = call(
            service,
            "artifact.read",
            {"digest": digest, "offset": offset, "limit": 4096},
        )
        text = page["content"]
        assert isinstance(text, str)
        content.extend(
            text.encode("utf-8")
            if page["encoding"] == "utf-8"
            else base64.b64decode(text, validate=True)
        )
        if page["eof"] is True:
            break
        following = page["next_offset"]
        assert isinstance(following, int) and following > offset
        offset = following
    result = json.loads(content)
    assert isinstance(result, dict)
    return result


with TemporaryDirectory(prefix="openhands-research-demo-") as temporary:
    demo_root = Path(temporary)
    workspace = demo_root / "workspace"
    workspace.mkdir()
    state = demo_root / "state"
    source = workspace / "sample.py"
    source.write_text("import json\nfrom pathlib import Path\n", encoding="utf-8")

    service = HarnessService(workspace=workspace, state_dir=state)
    created = call(
        service,
        "thread.create",
        {
            "question": "Which static imports changed between these observations?",
            "conditions": {"scope": "owned temporary Python sample"},
            "next_action": "Record the initial imports",
        },
    )["record"]
    assert isinstance(created, dict)
    thread_id = created["id"]
    assert isinstance(thread_id, str)

    before = call(service, "inventory", {"thread_id": thread_id})
    source.write_text(
        "# Same import count, different declaration.\n"
        "import math\nfrom pathlib import Path\n",
        encoding="utf-8",
    )
    after = call(service, "inventory", {"thread_id": thread_id})
    before_data = read_json(service, before["artifact_sha"])
    after_data = read_json(service, after["artifact_sha"])
    assert before_data["complete"] and after_data["complete"]
    assert len(before_data["entries"]) == len(after_data["entries"]) == 2

    comparison = call(
        service,
        "compare",
        {
            "thread_id": thread_id,
            "before_sha": before["artifact_sha"],
            "after_sha": after["artifact_sha"],
        },
    )
    changes = read_json(service, comparison["artifact_sha"])
    assert changes["dataset_complete"] and changes["normalization_compatible"]
    assert changes["observed_equal"] is False
    assert changes["added_count"] == changes["removed_count"] == 1
    assert changes["unchanged_count"] == 1
    assert changes["added"][0]["normalized"] == "import math"
    assert changes["removed"][0]["normalized"] == "import json"

    query = call(
        service,
        "query",
        {
            "thread_id": thread_id,
            "artifact_sha": after["artifact_sha"],
            "module": "math",
            "name": "math",
            "path": "sample.py",
        },
    )
    matches = read_json(service, query["artifact_sha"])
    assert matches["dataset_complete"] and matches["match_count"] == 1
    assert matches["entries"][0]["normalized"] == "import math"

    fixture = call(service, "fixture", {"thread_id": thread_id})
    validation = read_json(service, fixture["artifact_sha"])
    assert all(validation["readiness"].values())
    assert validation["cleanup"]["completed"] is True
    assert validation["cleanup"]["thread_stopped"] is True

    context = call(service, "context", {"thread_id": thread_id, "max_bytes": 2048})
    text = context["context"]
    assert isinstance(text, str)
    assert created["question"] == json.loads(text)["thread"]["question"]
    assert context["context_bytes"] == len(text.encode("utf-8")) <= 2048
    manifest = read_json(service, context["manifest_sha"])
    assert manifest["context"] == text

    updated = call(
        service,
        "thread.update",
        {
            "thread_id": thread_id,
            "expected_version": 1,
            "assessment": (
                "Observed json replaced by math; one import was unchanged. "
                f"Comparison evidence: {comparison['evidence_id']}. "
                "The fixture passed its normal operation. "
                f"Fixture evidence: {fixture['evidence_id']}. "
                "These saved observations do not establish a security verdict."
            ),
            "next_action": (
                "Review the saved observation scope and limitations; capture a fresh "
                "inventory before making a claim about the current workspace"
            ),
        },
    )["record"]
    resumed = HarnessService(workspace=workspace, state_dir=state)
    restored = call(resumed, "thread.get", {"thread_id": thread_id})["record"]
    assert restored == updated
    assert isinstance(restored, dict) and restored["version"] == 2
    history = call(resumed, "thread.history", {"thread_id": thread_id})["records"]
    evidence = call(resumed, "evidence.list", {"thread_id": thread_id})["records"]
    assert isinstance(history, list) and len(history) == 2
    assert isinstance(evidence, list) and len(evidence) == 5
    summary = {
        "ok": True,
        "mode": "local, no model",
        "network_scope": "owned loopback fixture only",
        "imports_before": len(before_data["entries"]),
        "imports_after": len(after_data["entries"]),
        "added": changes["added"],
        "removed": changes["removed"],
        "exact_query_matches": matches["match_count"],
        "fixture_normal_operation": validation["readiness"][
            "normal_operation_verified"
        ],
        "fixture_cleanup_completed": validation["cleanup"]["completed"],
        "context_bytes": context["context_bytes"],
        "resumed_thread_version": restored["version"],
        "retained_evidence_records": len(evidence),
    }

assert not demo_root.exists()
summary["temporary_demo_cleanup"] = (
    "Workspace, state, and artifacts removed after checks"
)
print(json.dumps(summary, indent=2))
print("EXAMPLE_COST: 0.0")
