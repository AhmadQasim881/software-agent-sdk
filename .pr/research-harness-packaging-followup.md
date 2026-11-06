# Research harness source and frozen packaging follow-up

## Result and scope

The packaged `research-review` skill now survives both production packaging
layers: the workspace source distribution used as the Docker build context and
the normal PyInstaller Agent Server binary. Both layers were missing the file.
The focused regressions reproduced those omissions before the fix and passed
after it. An actual source archive and an actual frozen binary now contain the
exact source bytes at the required destination.

Validation ran on 2026-11-06 in the existing Linux x86-64 development runtime,
using Python 3.13.5 and PyInstaller 6.22.3. The checkout was based on
`5f3cef92207b0fa1c4ef44b167b0aad93bae1881`, with the packaging changes described
below. The binary build used the normal specification without dependency,
lockfile, package-version, or build-requirement changes. Optional Vertex AI
dependencies were absent and the specification used its existing skip path.

The actual frozen server also passed help, startup, authenticated REST and
WebSocket readiness, tool discovery, metadata, and owned-process cleanup checks.
These checks establish the asset and server-startup contracts. They do not
establish every research operation or every supported host inside a frozen
server.

## Defect and change

`workflow.py::workflow_path()` loads
`skills/research-review/SKILL.md` beside the harness package. Although the tools
wheel already declared the skill as package data, two independent packaging
layers omitted it:

| Layer | Fix | Regression contract |
| --- | --- | --- |
| Root `MANIFEST.in` | Include `SKILL.md` under the harness skills directory. | Apply the actual manifest rules with setuptools' file selector and verify that the selected source path preserves the file bytes. |
| Agent Server `agent-server.spec` | Collect `skills/*/SKILL.md` from `openhands.tools.research_harness`. | Evaluate the specification's real `collect_data_files` arguments and verify the exact file bytes and bundle directory. |

Both tests live in `tests/cross/test_agent_server_build_metadata.py`. The existing
data-collection helper now returns source bytes keyed by filename and destination;
the previous plugin-schema regression continues to pass.

Before the production changes, both new tests failed because their selected data
was empty. After the changes, the complete metadata test file passed:

```text
Before: 2 failed, 14 deselected in 0.29s
After:  16 passed in 21.62s
```

Each changed file passed its applicable per-file pre-commit checks. `git diff
--check` also passed.

## Actual artifact verification

Artifacts were built in the owned temporary directory
`/tmp/research-harness-packaging-followup-4t7ui_07`.

The normal isolated source build succeeded:

```bash
UV_NO_SYNC=1 UV_OFFLINE=1 uv build --sdist \
  --out-dir /tmp/research-harness-packaging-followup-4t7ui_07/sdist
```

Inspection with Python's `tarfile` found exactly one matching skill member in
`openhands_build_context-0.0.0.tar.gz`:

```text
openhands_build_context-0.0.0/openhands-tools/openhands/tools/research_harness/skills/research-review/SKILL.md
```

The normal frozen build also succeeded, in approximately 225 seconds:

```bash
UV_NO_SYNC=1 UV_OFFLINE=1 OPENHANDS_SUPPRESS_BANNER=1 \
LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/pyinstaller --noconfirm \
  --workpath /tmp/research-harness-packaging-followup-4t7ui_07/work \
  --distpath /tmp/research-harness-packaging-followup-4t7ui_07/dist \
  openhands-agent-server/openhands/agent_server/agent-server.spec
```

`PyInstaller.archive.readers.CArchiveReader` extracted the skill directly from the
produced executable at:

```text
openhands/tools/research_harness/skills/research-review/SKILL.md
```

The source file, source-archive member, and frozen-archive member were compared
byte for byte. Each is **6,010 bytes**, with SHA-256:

```text
bd4cf9931fdeae6a80c5c89c03909bf843cabf68401e1fee9312cdfe44baa020
```

The executable is 138,016,720 bytes. Its SHA-256 is:

```text
c14dab683984f8ee9e6d43b469f2f9b6eb825dffe53e614d407b2bb65598befd
```

The executable's `--help` returned exit code 0 and exposed the expected host and
port options.

## Actual frozen server lifecycle

The existing `verify-agent-server` skill and `control-agent-server` CLI drove the
binary through the documented `attach`, `helper start`, `doctor`, `api`, and
`stop --purge-private` commands. The binary used its existing
`--import-modules openhands.tools.research_harness.definition` option to register
the research tool. The server bound only to an owned loopback port, with a fresh
session key, secret key, private home, configuration, and persistence paths.
VS Code and startup tool probes were disabled for this readiness check.

The entire lifecycle ran in one owning Python process invoking the existing CLI,
with child-subreaper handling. This environment gives tool invocations separate
process namespaces; ending the launcher invocation would otherwise terminate the
server before subsequent checks. Credentials were generated in private state and
never supplied as command-line values. No model call was made.

| Observable check | Result |
| --- | --- |
| `/alive`, `/health`, `/ready` | HTTP 200 for all three. |
| Missing session key | Protected count endpoint rejected the request with HTTP 401. |
| Correct session key | Protected count endpoint returned HTTP 200. |
| WebSocket authentication | Wrong key closed with code 4001; first-frame authentication with the correct key succeeded. |
| OpenAPI | Parsed successfully, with 179 operations. |
| Server metadata | Server and SDK versions both reported `1.60.1`. |
| `/api/tools/` | HTTP 200; list included `research_harness`. |
| `/api/tools/catalog` | HTTP 200; research entry was selectable and outside the default tool set. |
| Doctor | All 10 checks passed, including zero recorded log tracebacks. |
| Cleanup | Registered helper stopped; independent port-closure assertion passed; private state purged; zero owned child processes remained. |

The catalog reported the research tool as usable, but that flag is not a live
execution result for each of its operations. The attached-mode doctor also does
not enforce checkout-SHA equality or port ownership; this run tracked its helper
process group explicitly and checked port closure independently.

The temporary evidence includes `before-tests.log`, `after-tests.log`,
`sdist-build.log`, `sdist-inspection.json`, `frozen-build.log`,
`frozen-inspection.json`, `frozen-help.json`, `frozen-startup-transcript.json`,
`frozen-startup-summary.json`, and `frozen-server-redacted.log`. The retained
redacted REST exchanges are under the run's `evidence/packaging/` directory.

## Remaining boundaries

This follow-up closes the missing source-context asset, missing frozen asset,
actual normal binary build, and frozen readiness gaps. It does not replace the
separate memory integration or installed-wheel acceptance reports.

Frozen execution of host lifecycle hooks and the optional public HTTP worker was
not exercised. Those code paths use Python-interpreter subprocess entry points
(`hooks.py`, `hosts.py`, and `network.py`); their complete frozen-runtime behavior
must be verified separately before claiming full frozen harness parity. Docker
image execution, native host dispatch, real-model skill selection, and a fresh
locked dependency installation remain separate acceptance gates.
