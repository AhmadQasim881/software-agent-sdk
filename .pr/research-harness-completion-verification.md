# Research harness memory and host integration verification

## Scope and provenance

Validation ran on Linux with Python 3.13.5 on 2026-11-06, using the existing
development environment. The implementation is based on destination commit
`9b3c44d3083e2dc7d867c0ae4fd4a44b9ee78812` and adapts generic AgentMemory code
from source commit `2d187316c038257effbe232d5cff7cc32c4e1fa7`.

The tested `openhands-tools/openhands/tools/research_harness/` Git subtree is
`c2bcb08d967f1f3e4d0bffa49bceec46f1d273c4`. The
[packaging report](research-harness-memory-packaging.md) records individual source
hashes. An independent final comparison found all 27 runtime/bootstrap files
and the package metadata unchanged after installed-wheel verification.

The complete change includes six shared authored-memory operations, a
transactional state migration, bounded context retrieval, portable host setup,
explicit operator instructions, and an opt-in model acceptance evaluator.
The [integration guide](../New_security_Harness_Design/AGENTMEMORY_INTEGRATION.md)
documents the source adaptation, usage, contracts and limitations.

## Final regression suite

The following suite includes the complete research harness and five baseline
tool-registration/preset checks:

```bash
OPENHANDS_SUPPRESS_BANNER=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/python -I -m pytest \
    tests/tools/research_harness \
    tests/tools/test_init.py tests/tools/test_preset_default.py -q \
    --junitxml=/workspace/scratch/7c995fe92895/tmp/review/completion-pytest.xml
```

**Result: 393 passed, 0 failed, 2 warnings in 745.31 seconds (12:25).**
This is 388 harness tests plus five baseline tests, compared with 244 harness
tests plus five baseline tests at the original revision. The final suite therefore
adds 144 passing cases. No tests were skipped in this run.

The warnings were the existing Pydantic `ReadOnly` TypedDict qualifier warning
and the default terminal's fallback because `tmux` is unavailable. Neither was
a test failure. All evaluator tests used scripted provider transport; no paid
provider calls were made.

Meaningful behavior covered by the new tests includes:

| Area | Verified behavior |
| --- | --- |
| Migration | A real schema-v1 database preserves its existing records and artifacts; failed upgrades roll back; foreign-workspace ownership is rejected before migration. |
| Memory writes | Record, history, and lifecycle events commit together; concurrent stale versions conflict; failed event writes roll back. |
| Provenance and scope | Required source, valid same-thread evidence references, thread-local access, no authored note converted into evidence. |
| Retrieval | Expiry and tombstones excluded; literal query/tag behavior; filtering before bounded ranking; default context terms; pin supplementation; candidate/result/byte omissions. |
| Context | One snapshot for thread/evidence/outcomes/memory despite an interleaved writer; exact UTF-8 budgets; version/source previews and manifest accounting; no-note compatibility. |
| Shared interfaces | All six memory operations through the service and real MCP; a real SDK conversation's tool sees the same state as the service; provenance and tombstones survive process restart. |
| Session options | Explicit trusted suffix and limits reach both conversation and persisted state; invalid options fail before conversation creation; no model call during construction. |
| Hosts | Separate configuration for all three hosts; new destination required; partial-write cleanup; real generated Claude hook argv; real MCP discovery and restart. |
| Evaluator mechanics | Independent sessions, relevant/unrelated classifications, cleanup, iteration/budget bounds, fresh LLM instances, missing-key blocking, and redacted error types. Provider transport is scripted. |

The full suite also retains the existing lifecycle, worker cancellation, bounded
HTTP, fixture cleanup, inventory, comparison, artifact integrity, and telemetry
tests. Passing these tests is not evidence of real-model semantic selection or
successful live external HTTP.

## Runnable no-model example

The updated example completed with exit 0:

```bash
OPENHANDS_SUPPRESS_BANNER=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/python -I examples/01_standalone_sdk/60_research_harness/main.py
```

Observed output included:

```json
{
  "ok": true,
  "mode": "local, no model",
  "network_scope": "owned loopback fixture only",
  "imports_before": 2,
  "imports_after": 2,
  "exact_query_matches": 1,
  "fixture_normal_operation": true,
  "fixture_cleanup_completed": true,
  "context_bytes": 1904,
  "resumed_thread_version": 2,
  "retained_evidence_records": 5,
  "recalled_memory_records": 1,
  "memory_provenance_retained": true,
  "context_memory_selected": 1
}
```

The actual comparison reported one added `import math` and one removed
`import json` despite equal counts. The example asserted that its temporary
workspace, database and artifacts were removed and printed `EXAMPLE_COST: 0.0`.

## Packaging, configuration, and local application checks

- The standard isolated offline wheel build passed using setuptools 84.2.1.
  No dependency, lock, build requirement or package version was changed.
- The final wheel's SHA-256 is
  `8da437085837596bbf6d68286157f4437b61c5beefb8bbe5d98bc6b04266635d`.
  Installation into a disposable copy of the working runtime passed 17 actual
  CLI calls and two real MCP process sessions outside the checkout. All 234
  installed distribution versions appeared in the committed lock. This is a
  copied-runtime check; see the distinct fresh-install blocker below.
- The strict SDK loader and the skill-creator static validator both accepted the
  updated packaged `research-review` skill. Its memory instructions preserve the
  distinction between authored notes and observed evidence.
- Codex CLI `0.167.0-alpha.4` accepted the generated MCP command, arguments and
  environment through `mcp get --json` with a one-invocation configuration
  override. This parser check did not modify a host profile or run a model.
- Actual generated Claude Code SessionStart, PreCompact and SessionEnd commands
  ran as subprocesses and produced the documented protocol output. A Claude
  executable is absent, so native Claude dispatch remains unverified.
- The real local OpenHands Agent Server passed readiness, identity,
  authenticated empty-catalog operations, all 12 doctor checks, and owned
  process/port/state cleanup. That check ran at the original base revision;
  [its report](research-harness-local-validation.md) records exact scope and HTTP
  evidence. It does not claim remote research-tool deployment.

## Remaining acceptance gates

| Gate | Current evidence and next requirement |
| --- | --- |
| Live public GitHub response | An actual harness request on this task failed at DNS resolution (`gaierror`, no HTTP status, `retrieved_at=null`, 65 ms; attempt `2026-11-06T15:21:00.210546+00:00`). Run the same opt-in metadata operation where its fixed public endpoint is reachable. A successful GitHub connector read is not evidence for the harness HTTP worker. |
| Live semantic skill selection | No provider credential was supplied for this execution. The real evaluation CLI's missing-key path returned blocked, zero recorded calls and zero estimated cost. Run `evaluate-skill` with an explicitly configured model/key and inspect both natural-request cases. Scripted tests prove evaluator mechanics only. |
| Native host dispatch | Codex configuration parsing, generated hook subprocesses, and MCP execution passed. Verify the installed server and lifecycle behavior in each intended host; no native Codex/Desktop lifecycle adapter is claimed. |
| OS/network isolation | Docker and Podman are absent. The harmless namespace probe failed: `bwrap: loopback: Failed to create NETLINK_ROUTE socket: Operation not permitted`. Normal application readiness does not establish isolation. |
| Remote/container/frozen deployment | No remote Agent Server, Docker or PyInstaller runtime acceptance was performed. The wheel and local SDK/MCP results do not establish these deployments. |
| Fresh offline runtime installation | `uv sync --frozen --no-dev --package openhands-tools --no-install-package openhands-tools --offline` failed because `beartype==0.22.9` is not cached. The successful wheel check used a disposable runtime copy and retained the SDK's editable installation. |

Two earlier `make build` attempts also encountered incomplete offline dependency
or isolated-build caches. The subsequent standard isolated wheel build succeeded
with the required cached build backend; the fresh runtime dependency gate above
remains open. Requirements were not weakened to make either check pass.

## Review and publication integrity

Every changed file passed its applicable per-file pre-commit checks, including
Ruff, pycodestyle, Pyright and repository import/registration checks. Independent
read-only review found no actionable defect in memory migration, transaction
boundaries, retrieval, shared service dispatch or context delivery. `git diff
--check` and the final packaging/source hash comparison passed.

Publication compares the complete staged Git tree with the GitHub-created tree
before advancing the requested branch with an expected-head check. This preserves
the tested source bytes even if GitHub assigns different commit metadata from the
local validation commit. The code implements generic memory and bounded local
observations; it does not add autonomous vulnerability discovery or exploitation.
