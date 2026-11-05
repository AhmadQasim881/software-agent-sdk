# Security harness implementation verification

## Source and scope

- Branch: `feature/security_harness`
- Base commit: `34f04eb2426a2a6b90a0cce504c292155b12dcc1`
- Workshop: `New_security_Harness_Design/2026-unprompted-harness-engineering.pdf`
- PDF SHA-256: `81843e4a0359c602a201ba7bfaa222a6ad6a63ec729059a5ce7823a9b8473fe9`
- Environment: Linux, Python 3.13.5, uv 0.13.5, SDK 1.60.1.

Implement the workshop responsibilities as an additive tool package with a
model-independent core and shared CLI, MCP, and OpenHands interfaces. Demonstrations
use benign local fixtures and read-only inventories. The PDF's exploitation case
studies are motivation, not runnable workflows in this package.

Each stage must pass its focused tests and file-level pre-commit checks before the
next stage begins. Verification with scripted model responses is distinguished
from live paid-model verification. No credentials are required for the former.

## Stages

| Stage | Scope | Result |
| --- | --- | --- |
| 0 | Inspect instructions, source, interfaces, and baseline | Passed |
| 1 | Durable threads, evidence, artifacts, run and event records | Passed |
| 2 | Bounded inventory, external records, local readiness | Passed |
| 3 | Context selection, inventory comparison, queryable snapshots | Passed |
| 4 | Shared CLI, MCP, and OpenHands tool interfaces | Passed |
| 5 | Skills, lifecycle hooks, compaction checkpoint | Passed |
| 6 | End-to-end workflow, installation, packaging, documentation | Pending |

### Stage 0

- `make build`: passed using the committed dependency lock; no lock changes.
- `uv run pytest tests/tools/test_init.py tests/tools/test_preset_default.py -q`:
  **5 passed**. Existing warning: tmux absent, terminal uses subprocess fallback.
- `uv run pre-commit run --files openhands-tools/openhands/tools/__init__.py`:
  passed.
- SDK native hooks: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop,
  SessionEnd. PreCompact requires a tested condenser adapter, not an unsupported
  HookConfig entry.
- The workshop specifies outcomes and examples; database schema, module layout,
  interfaces, and test cases here are project implementation decisions.

### Stage 1

- `uv run pytest tests/tools/research_harness -q`: **25 passed**.
- File-level pre-commit: passed for all six new Python files.
- Verified concurrent artifact publication, corrupt/missing/symlinked/special-file
  artifacts, bounded bytes, SQLite restart persistence, assessment history,
  closed-thread rejection, optimistic update conflicts, cross-thread reference
  rejection, failed/interrupted outcomes, explicit recovery, and event pagination.
- A real SQLite trigger fault verified that failed event publication rolls back
  the successful-run transition. No successful record is fabricated on failure.
- API immutability and digest checking do not claim protection against an
  unrestricted process with the same filesystem permissions.

### Stage 2

- `uv run pytest tests/tools/research_harness -q`: **90 passed**.
- File-level pre-commit: passed for all eleven changed/new Python files.
- AST inventory includes tests, records exclusions and coverage, rejects symlinks
  and special files, and preserves partial output when limits or parsing fail.
- Controlled HTTP and real subprocess tests verified identity validation, no
  redirects/retries/credentials, body/output limits, total deadline, cancellation,
  and process reaping. They are not a live GitHub success claim.
- Live public metadata attempt for `AhmadQasim881/software-agent-sdk`: failed
  with DNS `gaierror` in this execution environment; the failed run and observation
  were recorded correctly. No successful external response was observed.
- Live owned loopback fixture: HTTP success, instance identity, expected normal
  item, server close and thread termination all passed.
- Independent review found and fixed terminal-record capacity and interruption
  gaps. Seventeen regressions cover exact byte limits, all terminal statuses,
  interruption at each persistence boundary, committed outcomes, and cleanup
  failure preserving the original interruption.

### Stage 3

- Full harness suite: **134 passed**; all seven changed/new Python files passed
  file-level pre-commit.
- Context tests verify exact UTF-8 budgets, current failed outcomes, newest-first
  selection, explicit omissions, restart behavior, and a real concurrent SQLite
  writer proving the read snapshot is coherent. Full selection manifests are
  saved without recursively generating new research evidence.
- Snapshot tests verify equal-count replacements, duplicate multiplicities,
  line-only changes, exact filters, malformed/partial datasets, output bounds,
  and incompatible parser versions. Oracle results describe recorded facts and
  retain their provenance; they do not produce security verdicts.
- Artifact views reassemble exact bytes across Unicode boundaries. JSONL exports
  are bounded, resumable views of SQLite events, with no independent dual-written
  ledger or cursor advancement past omitted records.

### Stage 4

- Full harness suite plus existing tool initialization/preset tests: **210 passed**
  in 77.20s. File-level pre-commit passed for all changed interface files.
- CLI tests use real processes outside the checkout, with model credentials
  removed. They verify restart persistence, JSON-only stdout, success/failure exit
  codes, invalid cursors, and corrupted SQLite handling.
- MCP tests use real stdio processes and real cancellation messages. Structured
  failure results retain evidence references and `isError=true`. The MCP library
  acknowledges cancellation before handler cleanup; the owned worker is still
  drained before the handler exits.
- SDK tests use real `Conversation.run()` and `arun()` with only model transport
  scripted. They verify action parsing, observation events, evidence/session IDs,
  sync/async cancellation, actual fixture cleanup, and SDK secret masking. This
  is functional integration verification, not live-model selection/evaluation.
- Independent review found a ledger pagination feedback loop and SQLite integer
  overflow. Ledger/artifact reads now avoid self-audit events; cursors are bounded
  to signed 64-bit values. New regressions verify finite one-record pagination.
- No dependency or lock changes. Existing warnings: missing tmux fallback and a
  Pydantic warning about an upstream TypedDict ReadOnly qualifier.

### Stage 5

- Focused gate: **43 passed** across native hooks (19), session/condenser (20),
  and isolated CLI/MCP launch paths (4). All twelve changed/new files passed
  pre-commit. Native subprocess/session cases use scripted LLM transport.
- All six SDK lifecycle events are exercised through real conversations. Policy
  denial prevents execution; SessionStart adds actual context; SessionEnd fires
  on close. Failed/damaged input returns native deny output. Ledger reads remain
  finite. Advisory SDK lifecycle semantics are explicitly documented.
- Generated hook commands use `python -I`; a harmless workspace module-shadowing
  regression proves inspected source cannot replace the installed hook module.
- Condenser tests verify synchronous/asynchronous checkpoint-before-attempt,
  no-op thresholds, failure preservation, hard reset, view immutability, and
  serialization. `hook.pre_compact` is an adapter attempt, not a native SDK hook
  or a claim that condensation succeeded.
- The scoped session factory checks current ambient plugin configuration and
  rejects additional plugins before construction; it does not change user
  settings or claim protection from concurrent local changes.
- A fresh agent used the bundled skill and CLI to compare saved inventories,
  detect an equal-count content replacement, cite comparison evidence, and save
  thread version 2. Root verified all three observation runs completed, the
  assessment cited the comparison, and the source hash was unchanged. That run
  exposed an ambiguous CLI schema instruction, which was clarified.
- Skill-format validation passed. Scripted positive/unrelated turns verify
  progressive disclosure mechanics, not semantic selection by a live provider.
