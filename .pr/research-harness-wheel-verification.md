# Research harness installed-wheel verification

## Result and provenance

The tools wheel passed an isolated installation and real CLI, native hook, and
MCP smoke workflow on Linux with Python 3.13.5. Verification ran outside the
checkout in a fresh virtual environment. No model requests or external metadata
requests were made.

- Source commit in the local validation history:
  `0df0c9315b8dd50fac67799a60d3f444d9653ba2`
- Verified source tree: `e9c2b6034e7af9a99031f4bc6a24144bed812e07`.
- Package: `openhands-tools` 1.60.1; SDK: 1.60.1 from the same checkout.
- Wheel: `openhands_tools-1.60.1-py3-none-any.whl`, 263,123 bytes.
- Wheel SHA-256:
  `00ea018dc7336d87b68e7180762ad3b8737d4afbf4df49e0427d4a567a8d1833`
- Wheel metadata: `Wheel-Version: 1.0`, `Generator: setuptools (84.2.1)`,
  `Root-Is-Purelib: true`, `Tag: py3-none-any`.
- uv: 0.13.5.
- Committed `uv.lock` SHA-256:
  `16ddd3b2915bbe9ba6c22d03fa1682e74e2ff33682a3a2f394125099fb884ec4`
- Temporary verification directory:
  `/tmp/research-harness-wheel-final-farzycol`.

The wheel digest identifies the artifact tested here; this does not claim that
separate builds produce identical wheel bytes. Build artifacts and the virtual
environment are temporary and are not committed.

Publication through the connected GitHub app creates new commit metadata. Each
published stage's complete tree is checked against its local validated tree,
so published commit IDs can differ while the tested source bytes remain exact.

## Installation and dependency evidence

The following commands were executed from the checkout. `WHEEL_CHECK_ROOT` below
stands for the temporary directory above. The dependency cache had already been
populated by the development setup; these commands completed offline.

```bash
uv build --package openhands-tools --wheel \
  --out-dir "$WHEEL_CHECK_ROOT/wheels" \
  --python /usr/bin/python3.13 --offline --no-build-logs

UV_PROJECT_ENVIRONMENT="$WHEEL_CHECK_ROOT/venv" \
  uv sync --frozen --no-dev --package openhands-tools \
  --no-install-package openhands-tools \
  --python /usr/bin/python3.13 --offline

uv pip install --python "$WHEEL_CHECK_ROOT/venv/bin/python" \
  --no-deps --offline \
  "$WHEEL_CHECK_ROOT/wheels/openhands_tools-1.60.1-py3-none-any.whl"
```

All three commands exited successfully. Runtime dependencies were installed from
the committed lock before the wheel was installed with `--no-deps`; no resolver
updated the lock. Inspection of every installed distribution found **182
distributions, all with versions present in `uv.lock`**. Selected versions were
Pydantic 2.13.5, FastMCP 3.2.0, MCP 1.28.1, and AnyIO 4.14.2. Build tooling was
isolated from this runtime environment; setuptools and wheel were not installed
in the runtime environment.

`openhands-sdk` was an editable installation from this checkout. The tools
distribution was installed from the built wheel: its `direct_url.json` referred
to the wheel and contained `archive_info`, not editable `dir_info`.

All 21 loaded research/bootstrap modules resolved under the fresh environment's
`site-packages`, including the CLI, session factory, hooks, and workflow loader.
All 24 files in the wheel's research-harness and bootstrap directories matched
the corresponding source bytes. The packaged skill existed in `site-packages`
and had a size of 4,177 bytes. The process asserted `sys.flags.isolated == 1` and
that its working directory was outside the checkout; no `sys.path` modification
was used.

## Runtime checks

The smoke runner used the installed interpreter with `-I`. Each CLI operation
below launched a new process with the same persistent state directory. The
workspace contained only this benign file, which was parsed without importing
or executing it:

```python
import json
from pathlib import Path
```

| Entry point | Observed result |
| --- | --- |
| `python -I -m openhands.research schema` | Exit 0; one valid JSON result with 18 operation schemas. |
| Installed `openhands-research schema` executable | Exit 0; the same 18 schemas, exercising the generated executable and its shebang. |
| `skill-path` and `load_workflow_skill()` | Path resolved to the wheel asset; strict SDK skill loading returned `research-review` with workflow marker `research-review/1`. |
| `thread.create` | Created a durable thread through the CLI. |
| `inventory` | Completed; stored artifact contained exactly `import json` and `from pathlib import Path`. |
| `fixture` | Completed; actual loopback HTTP success, expected instance identity, and normal item all verified. |
| Fixture cleanup | `completed`, `server_closed`, and `thread_stopped` all true; no cleanup errors. |
| `artifact.read` | Returned exact UTF-8 JSON bytes; their SHA-256 matched each requested artifact digest. |
| New-process `thread.get`, `run.list`, `evidence.get` | Recovered the original thread, both completed CLI runs, and the fixture's evidence/artifact association. |
| Generated native `SessionStart` hook | Exit 0 with `decision=allow`; returned actual context containing the thread and fixture evidence, and persisted its hook event/session ID. |
| Real MCP stdio process | Initialized, listed the single `research_harness` tool, obtained schemas, retrieved the existing thread, and recorded a completed inventory. |
| Second MCP stdio process | Initialized again and recovered that inventory's evidence and completed run, including its MCP session ID. |

The installed console entry point was
`openhands.research:main`. The MCP handshake reported server
`openhands-research-harness`, server version `1`, and protocol version
`2025-11-25`. Successful MCP responses had `isError=false`, and their text and
structured JSON content agreed.

The generated hook command executed in the fixture workspace was:

```bash
/tmp/research-harness-wheel-final-farzycol/venv/bin/python -I \
  -m openhands.research \
  --workspace /tmp/research-harness-wheel-final-farzycol/workspace \
  --state /tmp/research-harness-wheel-final-farzycol/state \
  hook --thread thread_ff5f82e414d644e0923cbb87ae24273e
```

The hook received an actual native `SessionStart` JSON event on stdin with
session ID `wheel-verification-hook`. It was produced by `make_hook_config()`
from the installed package, rather than a separately assembled substitute.

## Recorded evidence

| Record | Identifier or digest |
| --- | --- |
| Thread | `thread_ff5f82e414d644e0923cbb87ae24273e` |
| CLI inventory run | `run_a4493b39e98e487fa34f2d934eb03350` |
| CLI inventory artifact | `833295ba14e42d6368dd8fbbe969afff46d0180fe73d9d55bcbeeacbabd55d21` |
| Fixture run | `run_85d00d894cd24a76b542b50d76e61352` |
| Fixture artifact | `da4629fe8f225cc2d28824941853d9baf031bae173db9264d96b44932bd66f82` |
| MCP inventory run | `run_52b9fd3e6592428180773ed5f311e3c4` |
| MCP evidence recovered after restart | `evidence_1623cc3afcb441b2b13657e7d0bc1d71` |

The temporary runner, installation logs, and full result JSON were retained in
the verification directory for this session. `git diff --exit-code -- uv.lock`
passed, and the lock digest remained unchanged after installation and execution.

## Scope of this evidence

This verifies the Linux Python wheel, its packaged workflow asset, isolated CLI
launch, an actual generated SessionStart hook, local fixture cleanup, and MCP
stdio persistence across two real server processes. The broader lifecycle,
failure, cancellation, and condenser checks are recorded in
[security-harness-verification.md](security-harness-verification.md).

This smoke workflow does not claim testing inside Docker, a PyInstaller Agent
Server bundle, macOS, Windows, Codex, or Claude Desktop. It does not evaluate a
live model's skill selection, and it does not turn the owned loopback fixture
into an operating-system sandbox or validate a different application. No paid
model inference or live GitHub response is part of this packaging result.
