# Research harness completion follow-up

## Scope and result

This follow-up closes two packaging defects, adds an automated acceptance gate
for `feature/security_harness`, and records additional real application checks.
It follows published commit `cd339589367397671fd0322c4b75f124c127522d` and was
validated on 2026-11-06. The local base commit has different commit metadata but
the same complete Git tree, `3cb9e06f8c325f5fa327739b636774279719db9e`.

The harness runtime subtree remains
`c2bcb08d967f1f3e4d0bffa49bceec46f1d273c4`; this follow-up does not change memory,
shared dispatch, retrieval, hooks, or network-worker production code. The prior
[393-test run](research-harness-completion-verification.md) therefore identifies
the same core runtime. The focused follow-up suite passed **18 tests in 36.68
seconds**: all 16 Agent Server packaging metadata tests and two acceptance-runner
failure checks. These are separate runs, not a claimed single 411-test run.

## Changes and observed evidence

| Area | Change or observed result | Evidence |
| --- | --- | --- |
| Workspace source archive | `MANIFEST.in` now includes the packaged workflow skill. A normal isolated source build preserved the exact source bytes. | [Packaging follow-up](research-harness-packaging-followup.md) |
| Frozen Agent Server | The PyInstaller specification now collects the workflow skill. A normal binary build preserved the exact bytes and passed startup, REST/WebSocket authentication, tool discovery and cleanup checks. | [Packaging follow-up](research-harness-packaging-followup.md) |
| Server memory | Five real authenticated server-side MCP reads passed across a server restart. Provenance, thread scope, record version 2 and history versions 1/2 were retained. | [Server memory report](research-harness-remote-memory-verification.md) and [retained exchanges](research-harness-remote-memory-evidence.json) |
| Native SDK configuration | A real `RemoteConversation` registered the research tool and a new client reopened the same configuration after restart. Both conversations stayed idle. | [Server memory report](research-harness-remote-memory-verification.md) |
| Installed tools wheel | The reusable acceptance runner passed against the existing disposable copied runtime, outside the source checkout. | Installed-package check below |
| Native Codex | One-server configuration and feature preflight passed. The actual client exited before a model turn with a read-only filesystem error. | [Codex report](research-harness-codex-verification.md) and [sanitized observations](research-harness-codex-evidence.json) |
| Branch CI | A new push/manual workflow runs tests, builds the wheel, installs fresh locked dependencies and exercises actual installed CLI/MCP memory plus one public metadata request. | [Workflow](../.github/workflows/research-harness-acceptance.yml) |

The source-archive and frozen-archive skill members both matched the 6,010-byte
source file, SHA-256
`bd4cf9931fdeae6a80c5c89c03909bf843cabf68401e1fee9312cdfe44baa020`.
The two new packaging regressions failed before the fix and passed after it.

## Installed-package check

The new [.github/scripts/verify_research_harness_install.py](../.github/scripts/verify_research_harness_install.py)
re-executes under the selected runtime's Python in isolated mode before importing
SDK/MCP code. It rejects an editable tools installation, verifies packaged file
bytes and imported module locations, runs from an owned workspace outside the
checkout, and removes its temporary state. It accepts only this fork for the
optional live metadata observation.

The local copied-runtime run passed with:

- 27 runtime/bootstrap files equal in source, wheel and installed package.
- 78 imported module origins inside the runtime.
- 234 installed distribution versions present in `uv.lock`.
- 19 real CLI calls, 24 exposed operations and three generated host bundles.
- All six memory operations, stale-version rejection, bounded context, retained
  provenance, history and tombstones.
- Two real MCP processes, including memory readback after restart.
- Successful temporary workspace/state cleanup.

The wheel SHA-256 was
`8da437085837596bbf6d68286157f4437b61c5beefb8bbe5d98bc6b04266635d`.
The local machine-readable result was
`/tmp/research-harness-memory-packaging-yAgEtt/reusable-runner-local.json`.
The SDK remained editable from the same checkout. Matching installed versions
to `uv.lock` does not verify dependency artifact hashes or establish a fresh
installation.

The runner's actual live-observation helper was exercised separately once. The
public request failed at DNS resolution (`gaierror`), with no HTTP status, no
retrieval timestamp and zero response bytes. Its failed evidence was preserved
and temporary state removed. A successful GitHub connector request is not a
substitute for a successful harness HTTP-worker request.

## Automated gate

The workflow uses GitHub-hosted Ubuntu, read-only repository permissions,
checkout without retained credentials and no model-provider secrets. Ordinary
relevant pushes run one unauthenticated public metadata observation of this
fork; manual dispatch can disable it. The installation step uses the committed
lock in a fresh `UV_PROJECT_ENVIRONMENT`, excludes tools during dependency
installation, then installs the built tools wheel with `--no-deps`. SDK
editability is stated explicitly.

The workflow uploads JUnit, example output and installed-package JSON even when
a later acceptance step fails. The runner keeps successful local results in its
report if the live observation fails. YAML parsing, every shell block's `bash -n`
check, per-file pre-commit and `git diff --check` passed locally. Independent
review found no blocking defect; its example-output filename correction was
applied so the JSON-plus-cost output is retained as `.log`.

This report was prepared before publishing the workflow. A hosted success is
not claimed here; inspect the branch's actual Actions run and uploaded evidence
for the fresh-install and successful live-response gates.

## Remaining acceptance boundaries

The implemented behavior remains generic persistent memory, evidence/context
delivery, import inventory, public metadata and owned readiness checks. No
autonomous vulnerability-discovery, exploit-generation or exploit-reproduction
workflow was added.

| Gate | Current boundary |
| --- | --- |
| Live model skill selection in OpenHands | No provider API credential is present for `evaluate-skill`; scripted evaluation tests do not establish model selection quality. |
| Native Codex memory use | The authenticated client failed during app-server initialization with `Read-only file system (os error 30)`. No model completion, MCP call or skill selection was observed. The denied path was not established. |
| Claude Code/Desktop | Generated bundles and Python hook subprocesses are verified; native client dispatch remains unverified because the client is absent. |
| Native remote agent-loop execution | SDK tool registration/reopening and server-side MCP reads passed. `RemoteConversation.execute_tool()` is explicitly unsupported; no model-driven server loop was run. |
| Frozen hook/HTTP-worker parity | Binary asset retention and readiness passed; interpreter-style subprocess paths inside the frozen binary were not exercised. |
| Docker and OS/network isolation | Docker/Podman are absent, and the harmless local namespace probe was denied. Source-context packaging does not establish container execution or isolation. |
| Fresh dependency install and live GitHub HTTP | Both are automated by the new hosted gate. Local copied-runtime and failed-DNS results remain distinct until an actual hosted run passes. |

No model completion was observed in this follow-up. Local readiness, persistence
and packaging checks are not a claim of complete deployment or model acceptance.
