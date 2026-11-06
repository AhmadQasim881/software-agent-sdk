# Memory and runtime completion

## Scope and provenance

This follow-up fixes bounded context retrieval, packaged subprocess launchers,
and owned-process cleanup. It retains the existing AgentMemory integration and
adds repeatable frozen-runtime acceptance to the branch workflow.

The starting published branch head was
`7bd5c1cab8b09b2e4fce3824eeeab9efed57d859`, with tree
`4281e51f4d823dbcb3e35b424e2f70eca15288b2`. Its complete tracked tree matched the
local checkout before these changes. The requested AgentMemory source branch
still pointed to `2d187316c038257effbe232d5cff7cc32c4e1fa7` when checked on
2026-11-06. The [integration guide](../New_security_Harness_Design/AGENTMEMORY_INTEGRATION.md)
identifies the adapted hashing and reciprocal-rank-fusion algorithms and their
source files.

The existing implementation already supplied six memory operations, SQLite
history and optimistic versions, same-thread provenance, expiry and tombstones,
bounded lexical retrieval, and shared CLI, MCP and SDK interfaces. This follow-up
does not import the source project's saved database or introduce a daemon,
embedding provider, model credential, or new runtime dependency.

## Changes and the defects they address

| Area | Previous behavior | Resulting behavior |
| --- | --- | --- |
| Default context query | Truncating the concatenated question and next action could discard the next action; taking the first 32 terms could also exclude it. | Share the 512-character allowance between both fields and consider the first 16 eligible terms from each before filling the remaining slots. Explicit queries retain their previous behavior. |
| Frozen hook and host commands | Commands treated the frozen server executable as an ordinary Python interpreter, passing unsupported `-I -m` arguments. | A shared launcher uses the fixed `--research-harness` executable mode when frozen and preserves ordinary or explicitly selected Python launchers. |
| Frozen metadata worker | The standalone worker source was not available as a bundled data file, and its launcher also assumed Python interpreter flags. | The normal PyInstaller specification bundles that fixed source and the bootstrap dispatches a fixed worker mode before importing the SDK or server. |
| Synchronous hook timeout | A shell's child could retain captured pipes and remain alive after the shell timed out or exited. | On POSIX, the hook owns a new session and kills its known process group on interruption or timeout, then waits for the direct child. |
| Metadata-worker cleanup | Stopping only the direct worker could leave a PyInstaller child or another owned pipe holder alive. | On POSIX, stop the owned group even when its original leader has already exited, then reap the direct worker. |
| Retention guidance | Recall exclusion could be confused with deletion of retained content. | Document retained history and artifacts, filesystem access, consistent backup/removal, and the absence of per-note erasure. |

The bootstrap supports two fixed research modes and the existing normal Agent
Server entry point. It does not add a general Python `-c` or `-m` dispatch route.
The process-group correction is POSIX-specific; Windows keeps its previous
direct-process fallback. These changes do not establish an OS isolation boundary.

## Local regression and real CLI results

The complete selected regression suite passed on Linux with Python 3.13.5:
**574 passed, zero failures or errors, in 244.04 seconds** using four pytest
workers. Five warnings concerned the existing Pydantic `ReadOnly` qualifier and
the existing subprocess-terminal fallback when tmux is absent. No model call was
made.

```bash
OPENHANDS_SUPPRESS_BANNER=1 LITELLM_LOCAL_MODEL_COST_MAP=True UV_OFFLINE=1 \
  uv run --frozen --no-sync python -I -m pytest -n 4 -q \
    tests/tools/research_harness tests/sdk/hooks \
    tests/tools/test_init.py tests/tools/test_preset_default.py \
    tests/cross/test_agent_server_build_metadata.py \
    tests/cross/test_research_harness_install.py \
    tests/cross/test_research_harness_frozen.py
```

The source runtime was unchanged after that run. A later portability-only change
skips the two shebang-executable fixture cases on non-POSIX systems; the other
three entry-point tests remain available there.

Focused before/after checks established the defects rather than relying only on
new success cases:

- All three new context-query cases failed against the old behavior; all 49
  focused memory/context tests passed after the change.
- Both real synchronous hook timeout cases failed before the process-group
  change; all 152 SDK hook tests passed afterward. They cover a live shell and
  an already-exited shell whose child still owns the output pipe.
- Four of five frozen-bootstrap tests failed before the launcher and packaging
  changes; all 63 initial frozen, packaging, network and host checks passed after.
- The worker descendant regression initially reported one failure and one pass;
  all 18 network tests passed with group cleanup, including an exited leader.

An additional acceptance-driver regression passed separately after final review
found that guardian cleanup could mask a leak. The identical real-process test
failed against the original driver snapshot. The corrected driver records
survivors before cleanup, rejects any survivor after normal command completion,
and still removes the owned fixture. This test is included in the maintained
workflow alongside the 574-case suite.

Six separate real CLI processes then created a thread with a long question,
saved a note relevant to its next action, retrieved that note through implicit
context, updated it, reopened context and read history. Context returned the
current version and history retained versions 1 and 2. The fixture and its state
were removed. This exercise made zero model calls.

The normal runnable example also passed, including exact import-content changes,
fixture readiness and cleanup, persisted version-2 thread state, memory
provenance and context selection. It reported `EXAMPLE_COST: 0.0` and removed its
temporary workspace, state and artifacts.

The [local evidence summary](research-harness-runtime-local.json) retains the
JUnit counts/digest, actual six-process memory results, example output, final
binary identity and compiled-worker cancellation/deadline observations.

## Actual final frozen server

The normal PyInstaller build completed in 221.7 seconds. The final Linux
executable was 138,015,872 bytes with SHA-256
`9532bf6998ad3091d87d53420fe3cf52e6e2e75b77e96cff77bcf270ccdfc16a`.
It includes both process-group cleanup fixes. Five focused bootstrap tests also
passed after the non-POSIX fixture guard was added.

The existing Agent Server verification CLI drove this exact binary through its
normal entry point. All **10 doctor checks** passed, including readiness, HTTP
authentication, rejection of a missing key, WebSocket authentication, OpenAPI and
absence of tracebacks. Authenticated tool-list and tool-catalog requests returned
HTTP 200 and included `research_harness`. All four workspace package versions
reported `1.60.1`.

Stopping the owned server closed its loopback port, purged private state and left
zero owned descendants. The retained [server evidence summary](research-harness-frozen-server.json)
contains the actual doctor results and cleanup observations. It was assembled
from the verification CLI's captured results and contains no session key.

```bash
.venv/bin/python -I .pr/research-harness-frozen-server-check.py \
  --binary /absolute/path/openhands-agent-server \
  --output /absolute/new/evidence-directory
```

This development wrapper uses the maintained verification CLI's authenticated
profile, helper ownership, doctor, API and stop/purge operations. It requires
Linux for its child-subreaper cleanup. The binary was built before the local
implementation commit, so its reported checkout identity is the existing local
head `bd5c238716c7b188647f7a3fa299ec3e0d9e9863`; that field is not an assertion
that the old commit contains the new code. The binary digest identifies the
tested artifact.

### Frozen CLI, hooks and MCP

The corrected maintained driver passed against that same final binary from
`2026-11-06T18:39:52.600591+00:00` to
`2026-11-06T18:42:18.500930+00:00`. Its unaltered
[runtime report](research-harness-frozen-runtime.json) records:

- Seventeen actual CLI/probe commands, including schema discovery, unknown or
  invalid input rejection, memory create/update/reopen/history, stale-write
  rejection, source and evidence references, cross-thread exclusion, and bounded
  context with a matching persisted manifest digest.
- Native SDK `HookManager` execution inside the frozen process: the generated
  command used the fixed harness mode, SessionStart returned saved memory,
  permitted input passed, and an outside tool or invalid operation arguments
  were denied.
- A generated Claude Code review bundle used the frozen executable; its actual
  hook subprocess returned the expected protocol and saved memory. The bundle
  did not modify any native host profile.
- Two real MCP processes performed six operations in total. A note written
  through the first process was read and recalled through the second, and both
  processes could read the exact CLI-created version-2 note.
- The skill and standalone HTTP-worker source extracted from the binary matched
  checkout bytes. Every command recorded zero survivors before guardian cleanup.
  Both MCP connections closed without survivors; final cleanup needed to
  terminate no process and removed the disposable fixture.

The retained report's SHA-256 is
`0192a5e11771f054f2a0588eaafbedeaf10182c74bc0edd56501691d494a3e22`.

The full local run did not request public GitHub access. A focused real frozen
request separately returned the environment's DNS `gaierror`, CLI exit 1 and a
failed observation. The driver retained the complete failed artifact and rejected
the live acceptance check. That failure is retained in the local evidence summary.
It is not a successful HTTP result.

A separate actual-binary probe paused an owned fixed worker during startup:
cancellation returned in 0.00091 seconds and a test-specific 0.5-second deadline
returned in 0.50048 seconds. Neither left a worker or waitable owned child. This
verifies bounded startup interruption; the forced descendant-holds-pipe cases
are covered by the separate source subprocess regressions, and successful public
HTTP remains a distinct live check.

## Maintained acceptance gate

The branch's existing installed-wheel workflow now also runs the SDK hook suite,
checks the new bootstrap tests, builds the normal Agent Server executable, and
runs [the frozen acceptance driver](../.github/scripts/verify_research_harness_frozen.py).
The driver receives an explicit executable and report path. The workflow requests
one bounded public GitHub observation per installed runtime; these observations
use the actual harness worker and require a successful response when enabled.

```bash
.venv/bin/python -I .github/scripts/verify_research_harness_frozen.py \
  --binary /absolute/path/openhands-agent-server \
  --report /absolute/path/frozen-runtime.json
```

Add `--github-repository AhmadQasim881/software-agent-sdk` in an environment where
the fixed public endpoint is reachable. The local workspace's DNS failure must
not be reported as a successful public observation.

## Coverage boundaries

The [implementation map](../New_security_Harness_Design/README_IMPLEMENTATION.md#2-architecture-and-pdf-mapping)
covers validation, integrations, interfaces, memory, context, telemetry, skills,
deterministic tools, hooks and an import-snapshot oracle. Its observations are
static import inventories, public repository metadata and an owned readiness
fixture. It does not add autonomous vulnerability discovery or exploitation.

The earlier [installed-wheel run](research-harness-hosted-acceptance.md) and
[source-minimal container run](research-harness-container-acceptance.md) passed at
their recorded commits. They are historical evidence; a new hosted result must
identify the current implementation commit separately.

Live semantic skill selection, a model-driven native remote conversation, actual
Claude client dispatch and successful native Codex inference still require their
own configured runtimes. No provider credential was present for this local work.
Generated host configuration and real MCP subprocesses establish the shared
protocol path, not a paid-model or native-client acceptance result. Other Docker
targets, Windows/macOS frozen execution, persistent-volume container replacement,
and OS/network isolation guarantees are separate acceptance scopes.
