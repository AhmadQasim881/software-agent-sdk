# AgentMemory integration and remaining acceptance gates

## Implementation

The research harness now has authored, thread-scoped memory shared by its CLI,
MCP server, and OpenHands SDK tool. Six new operations bring the shared operation
count to **24**. Memory uses the same SQLite store and transactional event ledger
as the existing thread and evidence records.

The integration adapts generic algorithms from the requested source repository
at **`2d187316c038257effbe232d5cff7cc32c4e1fa7`**:

| Source | Reused behavior |
| --- | --- |
| [agentmem/embeddings.py](https://github.com/AhmadQasim881/All_Specs_Vulnerability_Harness_Design/blob/2d187316c038257effbe232d5cff7cc32c4e1fa7/agentmemory/agentmem-research/agentmem/embeddings.py) | Deterministic signed word/character-trigram hashing, normalized vectors, and cosine similarity. |
| [agentmem/retrieve.py](https://github.com/AhmadQasim881/All_Specs_Vulnerability_Harness_Design/blob/2d187316c038257effbe232d5cff7cc32c4e1fa7/agentmemory/agentmem-research/agentmem/retrieve.py) | Reciprocal rank fusion with constant 60, adapted to a bounded prefiltered candidate set. |
| [agentmem-research/pyproject.toml](https://github.com/AhmadQasim881/All_Specs_Vulnerability_Harness_Design/blob/2d187316c038257effbe232d5cff7cc32c4e1fa7/agentmemory/agentmem-research/pyproject.toml) | The source package declares MIT. Attribution is retained in `agentmemory.py`; no missing copyright notice was invented. |

The destination implementation is
[`agentmemory.py`](../openhands-tools/openhands/tools/research_harness/agentmemory.py)
and the memory methods in
[`store.py`](../openhands-tools/openhands/tools/research_harness/store.py).
`MemoryRecord` is exported from `openhands.tools.research_harness`.

This is a native adaptation, not an import of the source package's research
facade. Its separate prebuilt Node daemon, specialized tool registry, and
execution workflows are not installed. There is no new daemon, embedding
provider, vector database, or runtime dependency. The retrieval scores measure
lexical similarity, not semantic understanding or truth.

## Record and migration contract

Each note belongs to one existing thread and stores its kind (`note`, `decision`,
`constraint`, or `lesson`), text, required source, optional tags and supporting
evidence references, importance, pin, expiry, version, and timestamps. Evidence
references must already exist in that same thread. Linking evidence does not
verify the note's contents.

Opening state schema v1 transactionally creates the memory tables and advances
the database to v2. Existing thread, run, evidence, event, and artifact contents
are preserved. Workspace ownership is checked before migration. A failed
migration rolls back. Tests also verify concurrent version conflicts and rollback
when writing the corresponding lifecycle event fails.

Older harness code rejects schema v2. If rollback is needed, preserve a consistent
database-and-artifact backup before upgrading. This change does not provide a
reverse migration or import an existing source project's memory database.

| Operation | Contract |
| --- | --- |
| `memory.remember` | Require `thread_id`, `text`, and `source`; create version 1 and an atomic lifecycle event. |
| `memory.get` | Retrieve a note by `thread_id` and `memory_id`, including retained inactive records. |
| `memory.update` | Require the current `expected_version`; update supplied fields and append history. `clear_expiry=true` explicitly removes expiry. |
| `memory.forget` | Require current version and a nonblank reason; retain a tombstone and history, and exclude it from recall. Forgotten notes cannot be edited. |
| `memory.recall` | Filter active unexpired notes by literal query, optional kind, and exact tags; rank a bounded window and report omissions. |
| `memory.history` | Return a bounded page of immutable record versions, with the shared pagination contract. |

Text is limited to 8,192 characters, source to 2,048, tags to 16 of at most 64
characters, and evidence references to 32. Importance is an integer from 0 to 10.
The shared 16,384-byte argument limit also applies, including Unicode text and
metadata. Expiry must be an explicit timezone-aware ISO timestamp.

## Retention, access, and removal

The persistent store has no automatic retention period or purge job. Records,
the event ledger, artifacts, and saved SDK history remain until the operator
removes them. `expires_at` and `memory.forget` exclude notes from recall and new
context selection; `memory.get` and `memory.history` still expose retained text.
Earlier versions, recorded arguments, and existing context artifacts remain.

Notes, submitted arguments, artifacts, and SDK transcripts can contain sensitive
data. Choose the intended audience and retention duration for state, exports,
and backups. Filesystem ownership, permissions, and storage controls determine
access. Existing directory permissions are not tightened when opening state.
Processes with the same filesystem access can read or modify these files despite
thread scoping and artifact digest checks.

Before backing up or removing state, stop its CLI, MCP, SDK, and server writers
and wait for owned workers and hooks to exit. For a consistent resumable backup,
copy the complete state directory together: `state.sqlite3`, any SQLite sidecar
files, `artifacts/`, and the scoped helper's `conversations/` when present. Keep
the original workspace at its recorded absolute path and use a compatible
harness version. The store checks that workspace binding; a database alone does
not preserve its referenced artifacts or SDK conversation history.

Removal is an explicit operator action. Decide whether resume or rollback still
requires a consistent backup, then remove only the confirmed owned state and
separately selected copies. There is no per-note erasure operation or secure
erasure guarantee. Exports, backups, and external host histories retain their own
copies and require separate retention decisions.

## Retrieval and context behavior

An explicit recall query is a case-folded literal substring of note text or tags.
Characters such as `%` are literal; they are not SQL wildcards. All requested
tags must match. Empty query browses active, unexpired notes.

Filtering happens before loading candidates. By default at most 200 candidates
are loaded, configurable from 1 to 500. The window is ordered by pin, update time,
and stable ID. Within it, reciprocal rank fusion combines hashed lexical
similarity, importance, and recency; pinned notes sort first. Recall returns
1–100 requested results and at most 49,152 serialized UTF-8 bytes. SQL matching
and count queries can inspect the full scoped collection; the candidate bound
limits loaded ranking work, not the database's total scan time.

The response reports eligible, matching, scanned, and excluded counts; expiry
and tombstone exclusions; candidate/result/byte omissions; and the ranking
policy. A bounded ranking is not a globally exhaustive similarity search.

`context` reads the current thread, evidence, run outcomes, and memory in one
SQLite snapshot. Without an explicit query it derives up to 32 distinct literal
terms from the question and next action. The 512-character query allowance is
shared between those fields, reserving half for each and transferring unused
space. The first 16 eligible terms from each field are considered before their
remaining terms, so a long question cannot crowd the next action out of recall.
A small documented common-word exclusion and a minimum three-character term
length still apply. An explicit query retains
substring semantics; an explicit empty query browses. Active pinned constraints
supplement the context candidates even when their wording differs.

Memory is delivered in a separately labeled `memory` section as authored data.
Its previews consume at most one third of the existing total context byte budget.
The rest of the envelope includes thread fields and selected evidence. The
default retrieval limit is eight notes, with a maximum of 20. A pin does not
guarantee inclusion under a small budget. The content and manifest record
omissions, versions, source previews, evidence references, and selection reasons.
No-note contexts preserve the previous content/result shape. Manifests containing
memory use schema 2 and policy `research-context/2`.

### Use the shared API

After the setup in [README_IMPLEMENTATION.md](README_IMPLEMENTATION.md), substitute
your returned thread ID in these examples. Use schema discovery for all fields:

```bash
research schema memory.remember
research memory.remember '{"thread_id":"thread_REPLACE","kind":"decision","text":"Compare import declarations even when counts are equal.","source":"Operator review","tags":["imports"],"importance":7}'
research memory.recall '{"thread_id":"thread_REPLACE","query":"import","limit":5}'
research context '{"thread_id":"thread_REPLACE","query":"import","memory_limit":5,"max_bytes":8192}'
research memory.get '{"thread_id":"thread_REPLACE","memory_id":"memory_REPLACE"}'
research memory.update '{"thread_id":"thread_REPLACE","memory_id":"memory_REPLACE","expected_version":1,"pinned":true}'
research memory.history '{"thread_id":"thread_REPLACE","memory_id":"memory_REPLACE","limit":10}'
```

Use the actual returned memory ID and version; example identifiers are placeholders.
The SDK service and MCP tool accept the same operation names and JSON arguments.
The no-model example demonstrates saving a decision with evidence, reopening
state, recalling it, and delivering it through context.

## Operator instructions and host setup

`create_research_conversation` now accepts explicit `operator_instructions`,
`max_iteration_per_run`, and `max_budget_per_run`. The instruction suffix is
trusted caller configuration with a 16,384-byte UTF-8 limit. It can state normal
operation, runtime requirements, scope, and stop conditions. Ambient workspace
instruction loading remains disabled for this scoped helper. Stored notes and
source text cannot change that configuration.

Generate a new bundle using installed Python and the existing workspace/state:

```bash
research host-config --host codex --output /absolute/new-codex-bundle
research host-config --host claude-code --thread thread_REPLACE --output /absolute/new-claude-code-bundle
research host-config --host claude-desktop --output /absolute/new-desktop-bundle
```

Each destination must be new. The command refuses existing paths, copies the
packaged workflow skill, and writes host settings, setup instructions, and a
manifest. Review and merge the generated settings into the intended host. It
uses absolute paths and preserves virtual-environment interpreter symlinks.

| Host | Generated setup | Lifecycle scope |
| --- | --- | --- |
| Codex | `codex.config.toml` | Shared MCP operations; call context when resuming. No generated native Codex lifecycle adapter. |
| Claude Code | `claude-code.mcp.json` and, with a thread, `claude-code.settings.json` | SessionStart context, PreCompact saved-context checkpoint/instructions, SessionEnd metadata. |
| Claude Desktop | `claude-desktop.config.json` | Shared MCP operations; skill is a readable reference. No generated Desktop lifecycle adapter. |

The Claude Code adapter follows the current
[official hooks reference](https://code.claude.com/docs/en/hooks): direct
`command`/`args` execution, SessionStart additional context, plain-text PreCompact
instructions, and no SessionEnd output. It uses only already-saved records and
does not capture unsaved transcripts. Native host installation and dispatch must
be checked in the actual client; generated configuration and protocol subprocess
tests alone cannot establish them.

## Opt-in model acceptance evaluation

The following command needs no persistent `--state` argument. Supply a model
supported by your configured provider and place its key in the explicitly named
environment variable before running it:

```bash
"$SDK_REPO/.venv/bin/python" -I -m openhands.research evaluate-skill \
  --model YOUR_PROVIDER_MODEL --api-key-env LLM_API_KEY \
  --max-budget-usd 0.10 --max-iterations 8
```

The evaluator creates two independent disposable local SDK sessions. One natural
request asks to resume a saved import review without naming the skill; the other
requests a greeting. It checks actual skill invocations, completed session status,
reported cost, and cleanup. Each case receives half the requested budget. The
environment entry point uses fresh LLM instances, no retries, a 30-second request
timeout, and 1,024 output tokens. Keys use the SDK's secret type; returned errors
contain exception types rather than provider request bodies.

Missing credentials return `status="blocked"`, zero calls, and CLI exit 1.
SDK cost estimates are checked between provider calls, so a response can exceed
its allocation; unpriced providers may report zero estimated cost. The call count
counts completed responses recorded in SDK metrics and may omit failed attempts.
This is a two-case acceptance check, not a general semantic quality benchmark.

## Verification and honest coverage

The repository's implementation and evidence are independently reviewable:

- [Memory and runtime completion](../.pr/research-harness-runtime-completion.md)
  records 574 local regression tests plus the acceptance-driver regression, the
  corrected default context query, POSIX process-group cleanup, and actual final
  Linux binary CLI, native SDK hooks, generated host commands, MCP restart and
  authenticated server checks.
- [Successful hosted acceptance](../.pr/research-harness-hosted-acceptance.md)
  records 411 passing tests, a fresh locked runtime, actual installed CLI/MCP
  memory behavior and a real HTTP 200 public GitHub observation.
- [Successful container acceptance](../.pr/research-harness-container-acceptance.md)
  records eight passing driver tests, a normal source-minimal image, four
  non-editable workspace packages, and real MCP memory/history recovery after
  restarting the same container. Both sets of nine doctor checks and cleanup
  passed; the runtime evidence is retained in the repository.
- [Completion follow-up](../.pr/research-harness-followup-verification.md) records
  the latest packaging fixes, reusable installed-package acceptance runner,
  branch CI gate and remaining deployment/model acceptance boundaries.
- [Local real-application validation](../.pr/research-harness-local-validation.md)
  records actual Agent Server readiness, identity, authenticated empty-catalog
  behavior, 12 doctor checks, and owned-process cleanup at the original revision.
- [Memory packaging verification](../.pr/research-harness-memory-packaging.md)
  identifies the new wheel and the exact installation scope and limitations.
- [Completion verification](../.pr/research-harness-completion-verification.md)
  records the final test suite, example, shared interfaces, and remaining gates.
- [Packaging follow-up](../.pr/research-harness-packaging-followup.md) fixes and
  verifies the skill asset in the actual workspace source archive and frozen
  server binary, including frozen startup and authenticated tool discovery.
- [Server memory verification](../.pr/research-harness-remote-memory-verification.md)
  records real server-side MCP reads, memory recovery across server restart, and
  native SDK conversation registration/reopening without model inference.
- [Native Codex attempt](../.pr/research-harness-codex-verification.md) records the
  accepted isolated MCP configuration and a client initialization failure before
  any model completion or tool call.

The hosted workflow closed the fresh-install and successful live GitHub gates.
Local requests remain blocked by this workspace's DNS environment; the retained
hosted report identifies the separate runtime and actual successful response.
No provider API credential is present for the OpenHands live semantic check.
The installed native Codex client failed before its model turn with a read-only
filesystem error; its generated configuration alone does not establish recall.
Claude native dispatch, other Docker targets, Windows/macOS frozen execution and
OS/network isolation claims remain unverified. Linux frozen CLI, native SDK hooks,
generated host protocols, MCP restart and worker cancellation/deadline passed in
the runtime completion. The source-minimal Docker path passed on the hosted
runner; the local namespace probe was denied.
Direct `RemoteConversation.execute_tool()` is
unsupported; the native model-driven server loop remains separate from the
verified MCP read path. These gates must pass in an appropriate runtime before
claiming full workshop acceptance.
The implemented observations remain import inventories, public metadata, and
owned application readiness checks; this change adds no autonomous vulnerability
discovery or exploitation workflow.
