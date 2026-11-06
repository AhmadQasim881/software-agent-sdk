# Research harness memory packaging verification

## Result and scope

**The standard offline wheel build and the installed-wheel smoke workflow passed.**
Runtime validation used a disposable copy of the existing working virtual
environment, with `openhands-tools` replaced by the new wheel. A separate attempt
to create a fresh runtime directly from the committed lock was blocked because a
locked dependency was absent from the offline cache. Both results are recorded
below.

The installed-wheel workflow verified real CLI memory persistence across
processes, the packaged skill through the SDK loader, all three generated host
bundles, and memory recovery across two real MCP stdio server processes. It ran
outside the checkout with isolated Python mode. It made no model requests or
external metadata requests.

- Recorded at `2026-11-06T15:46:58+00:00`.
- Source checkout: `/workspace/scratch/7b78f21356c4/software-agent-sdk`.
- Working tree based on commit `9b3c44d3083e2dc7d867c0ae4fd4a44b9ee78812`; the source hashes below
  identify the uncommitted implementation bytes that were built and tested.
- Verification directory: `/tmp/research-harness-memory-packaging-yAgEtt`.
- Package: `openhands-tools` 1.60.1.
- Wheel: `openhands_tools-1.60.1-py3-none-any.whl`, 281,691 bytes.
- Wheel SHA-256: `8da437085837596bbf6d68286157f4437b61c5beefb8bbe5d98bc6b04266635d`.
- Wheel metadata: `Wheel-Version: 1.0`, `Generator: setuptools (84.2.1)`,
  `Root-Is-Purelib: true`, `Tag: py3-none-any`.
- Interpreter: Python 3.13.5 on Linux; uv 0.13.5.
- Committed `uv.lock` SHA-256:
  `16ddd3b2915bbe9ba6c22d03fa1682e74e2ff33682a3a2f394125099fb884ec4`.

This verifies the generated configuration and actual MCP protocol execution. It
does not claim that Codex, Claude Code, or Claude Desktop loaded the bundle in a
running host application, or that a host dispatched a lifecycle hook. Docker,
PyInstaller, macOS, Windows, and live model skill selection were not exercised.

## Standard build and unchanged requirements

The previous report's temporary environment,
`/tmp/research-harness-wheel-final-farzycol`, no longer exists. The checkout's
existing virtual environment contains setuptools 80.9.0, which does not satisfy
the package's `setuptools>=83.0.0` build requirement. Inspection found setuptools
84.2.1 and wheel 0.48.0 in the uv cache, and the normal isolated build succeeded
with that cache. The wheel identifies setuptools 84.2.1 as its generator.

The final build included the public `MemoryRecord` export in
`research_harness/__init__.py` and the updated packaged memory workflow skill.
This command ran from the checkout and exited **0**:

```bash
uv build --package openhands-tools --wheel \
  --out-dir /tmp/research-harness-memory-packaging-yAgEtt/wheels \
  --python /usr/bin/python3.13 --offline --no-build-logs
```

No dependency, lock, package version, or build requirement was changed.
`--no-build-isolation` was not used.

## Fresh locked runtime installation blocker

A new environment was requested with the existing documented recipe:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/research-harness-memory-packaging-yAgEtt/venv \
  uv sync --frozen --no-dev --package openhands-tools \
  --no-install-package openhands-tools \
  --python /usr/bin/python3.13 --offline
```

This exited **1** after creating the virtual environment. The exact failure was:

```text
error: Failed to download `beartype==0.22.9`
  cause: Network connectivity is disabled, but the requested data wasn't found in the cache: https://files.pythonhosted.org/packages/71/cc/18245721fa7747065ab478316c7fea7c74777d07f37ae60db2e84f8172e8/beartype-0.22.9-py3-none-any.whl
```

The reported chain was `openhands-tools` 1.60.1 → `openhands-sdk` 1.60.1 →
`fastmcp` 3.2.0 → `py-key-value-aio` 0.4.4 → `beartype` 0.22.9. No retry enabled
network access or substituted dependency versions. This fresh-lock installation
remains unverified until the committed runtime dependencies are available in the
authorized cache.

## Disposable copied-runtime installation

The existing 1.2 GB working environment was copied to a temporary directory. Its
interpreter remained relocatable through `/usr/bin/python3.13` and its own
`pyvenv.cfg`. The original environment's tools installation was not replaced;
its `direct_url.json` still identified the original editable checkout afterward.

The copied environment received only the built tools wheel:

```bash
cp -a --reflink=auto .venv \
  /tmp/research-harness-memory-packaging-yAgEtt/runtime-copy

uv pip install \
  --python /tmp/research-harness-memory-packaging-yAgEtt/runtime-copy/bin/python \
  --no-deps --offline --reinstall-package openhands-tools \
  /tmp/research-harness-memory-packaging-yAgEtt/wheels/openhands_tools-1.60.1-py3-none-any.whl
```

Both commands exited **0**. uv used a full copy when hardlinking across the cache
and temporary filesystem was unavailable. No packages were resolved or installed
as transitive dependencies of this replacement.

The tools distribution's `direct_url.json` pointed to the wheel and contained
`archive_info`, with no editable `dir_info`. All **78 loaded tools/bootstrap
modules** resolved under the copied environment's `site-packages`, including the
new memory module, public `MemoryRecord` export, CLI, host adapter, and MCP server.
All **27 research-harness/bootstrap files** matched both the built wheel and the
source snapshot byte for byte.

`openhands-sdk` remained the copied environment's editable installation from the
same checkout. This is a copied-runtime packaging check, not a clean-room SDK
wheel installation or a successful fresh `uv sync`.

Inspection found **234 installed distributions**, including development tools,
and **all 234 versions were present in the committed `uv.lock`**. This version
comparison does not claim a byte-for-byte reinstall or artifact-hash verification
of every copied dependency. Selected versions were:

| Distribution | Installed version | Version in committed lock |
| --- | --- | --- |
| `anyio` | `4.14.2` | Yes |
| `beartype` | `0.22.9` | Yes |
| `fastmcp` | `3.2.0` | Yes |
| `mcp` | `1.28.1` | Yes |
| `openhands-sdk` | `1.60.1` | Yes |
| `openhands-tools` | `1.60.1` | Yes |
| `pydantic` | `2.13.5` | Yes |
| `setuptools` | `80.9.0` | Yes |
| `wheel` | `0.46.3` | Yes |

## Runtime checks

The smoke runner was executed with the installed interpreter and `-I`, from
`/tmp/research-harness-memory-packaging-yAgEtt/workspace`, outside the checkout. It asserted
`sys.flags.isolated == 1` and the copied runtime prefix. No `sys.path` modification
was used. The owned fixture contained only:

```python
import json
from pathlib import Path
```

The inventory operation parsed this file without executing it. Each CLI call
launched a new process against the same persistent state directory. All **17 CLI
calls** exited 0 and returned one valid JSON result.

| Entry point | Observed result |
| --- | --- |
| Module CLI `schema` | Exposed 24 operation schemas, including all six memory operations. |
| Installed `openhands-research` executable | Generated shebang selected the copied interpreter; `schema memory.remember` matched the module CLI schema. |
| `skill-path` and `load_workflow_skill()` | Resolved the installed asset and loaded `research-review` through the strict SDK loader. |
| `thread.create` and `inventory` | Created durable thread state and a completed benign inventory with an evidence record. |
| `memory.remember` | Saved a decision with source, tags and the existing same-thread evidence ID. |
| New-process `memory.get` and `memory.recall` | Recovered the same note and its provenance. |
| `memory.update` | Version 1 advanced to 2 while preserving the evidence reference. |
| Default `context` | Selected the saved inventory note and stayed within the exact UTF-8 byte budget. |
| `host-config` for Codex, Claude Code and Claude Desktop | Created separate review bundles; each pointed to the copied interpreter and contained the identical installed skill. No host configuration was installed. |
| First real MCP stdio process from the generated Codex bundle | Initialized, discovered `research_harness`, exposed memory schemas, read the CLI-updated note and created a separate memory note. |
| Second real MCP stdio process | Recovered the MCP-created note and recalled it by literal query. All text and structured response payloads agreed and `isError` was false. |
| `memory.forget`, `memory.history` and `memory.get` | Version 3 retained a forget tombstone; recall excluded it; history recovered versions 1, 2 and 3. |

The packaged skill is 6,010 bytes with SHA-256
`bd4cf9931fdeae6a80c5c89c03909bf843cabf68401e1fee9312cdfe44baa020`. Generated host bundles contained those same bytes.
The CLI thread was `thread_34f9615173004d469daa07e23ad5a42d` and its retained memory record was
`memory_3fa094271aec4fada1d3aa73c6f3a19e`.

## Source integrity and retained evidence

`git diff --exit-code -- uv.lock` returned 0, and the lock digest remained
unchanged. All recorded source hashes were checked again after the runtime smoke
and still matched the build snapshot. A later source or asset edit requires a
new build and comparison before this report can support that edit.

The verification directory retains `build.log`, `sync.log`,
`copied-runtime-install.log`, `copied-runtime-smoke.log`,
`copied-runtime-smoke.json`, `source-manifest-before.json`,
`source-manifest-after.json`, `wheel-inspection.json`, and the smoke runner.
Temporary wheels, environments and runtime state are not committed.

| Source path relative to checkout | SHA-256 |
| --- | --- |
| `openhands-tools/openhands/research/__init__.py` | `f72a19de2bbb74c3737f33bb3e49740f34aa69fbfcf8b562e182d8ac72037e19` |
| `openhands-tools/openhands/research/__main__.py` | `0ee5405bd0abacf0e303cfbe86639bfc0898bbdd52f878b3c1dc8b0885657074` |
| `openhands-tools/openhands/tools/research_harness/__init__.py` | `3ebae55902c7aceea070dd2c5535bc9b1994ae58b9f98234258428635affdfa7` |
| `openhands-tools/openhands/tools/research_harness/_http_worker.py` | `4b942163e40c6ce37b5a74476299c94576c1315072f55bfb9f63c3006f8c717b` |
| `openhands-tools/openhands/tools/research_harness/agentmemory.py` | `39bd936a9904a0954e9115122c8ac89f099a75fc3ed80280cf5387c1dca83af5` |
| `openhands-tools/openhands/tools/research_harness/artifacts.py` | `5eb242b4ff37643349613a1d632b4d731869d3a7a53a076d4e3b5567a87abf8c` |
| `openhands-tools/openhands/tools/research_harness/cli.py` | `67aa0c08e58c711553f630c227d19ff2ac786f0f3be62b10b79e4189e8d9a1a8` |
| `openhands-tools/openhands/tools/research_harness/commands.py` | `ec38abefc991eebacda44d160799c431bf9e262af20e3d17ce4c384023856b16` |
| `openhands-tools/openhands/tools/research_harness/condenser.py` | `bb2b201d0d489780975fdcb93ce9ae32a180ef85b7419c9871ce4abac52e60c7` |
| `openhands-tools/openhands/tools/research_harness/context.py` | `fbcd4dca6fe947c7d4544b489a3a9e557ca6adae6e4659e4a7519836b2935d6b` |
| `openhands-tools/openhands/tools/research_harness/core.py` | `10255f79e971382bd01ed8e86c86743a7679abcf7d1dc1cbbc61709f69ef0055` |
| `openhands-tools/openhands/tools/research_harness/definition.py` | `02eb4d156adcbfb29c50ff9c0b22a12c56c849369bd07713ce66edc4cd4dcb98` |
| `openhands-tools/openhands/tools/research_harness/evaluation.py` | `288913468a4f371bf3e78c8b64e42cffe4bf7b2583b5e71c5f906c86449f500b` |
| `openhands-tools/openhands/tools/research_harness/fixture.py` | `df483510a18020e4db815ab5a687bff003e1b82c75c6cb375316659d0c685647` |
| `openhands-tools/openhands/tools/research_harness/hooks.py` | `2d3bce3796b992af058babd0ca74ff08cca18c4d5645a3abd7a2dc3b22330901` |
| `openhands-tools/openhands/tools/research_harness/hosts.py` | `b502bf22e9d9fad36733c8b8899f4ffd6b9b84b7feb15f887230c43c48981e19` |
| `openhands-tools/openhands/tools/research_harness/inventory.py` | `a13ef9f5b079648326930c218037de104f98a5fc17dbbf585f1ca52cd019e0d2` |
| `openhands-tools/openhands/tools/research_harness/mcp_server.py` | `70746530fa7a34fc335a31cf6f8f8ddb78accec021a21fdab418506c3dca4d22` |
| `openhands-tools/openhands/tools/research_harness/models.py` | `9f506063bb12fcd03eaef85300d1a44c39bbbeb0519be6a480d65aea55aa4cc1` |
| `openhands-tools/openhands/tools/research_harness/network.py` | `d314a2ebd8bafa5cd990a6ce6405cc8f2593b325832c379ed7c65c0c76371c58` |
| `openhands-tools/openhands/tools/research_harness/service.py` | `7a8ab6b74a6b9bd571cb203c12b9f4917413fdbf00c1430ce324f8d1a5e8066b` |
| `openhands-tools/openhands/tools/research_harness/session.py` | `b0a7b99748970f9e69475e28064386cdf87fa77b50f5acbe62351f19d8d721ff` |
| `openhands-tools/openhands/tools/research_harness/skills/research-review/SKILL.md` | `bd4cf9931fdeae6a80c5c89c03909bf843cabf68401e1fee9312cdfe44baa020` |
| `openhands-tools/openhands/tools/research_harness/snapshots.py` | `f324c4b91248f3b79b5bcfe9579cccbe59d24945509b0a0e1f86b3a0b788916b` |
| `openhands-tools/openhands/tools/research_harness/store.py` | `67cb8e886564ad44b7aa7032acae7d961797e85aca45152b334eae45abc54c72` |
| `openhands-tools/openhands/tools/research_harness/views.py` | `059d392aacdfac1f9ec74efa8aa6a9a46066c91cec3c1a1f6d29be4d116882e1` |
| `openhands-tools/openhands/tools/research_harness/workflow.py` | `0ff041bff1cc362e8c70529d3336ba1b1e41d48d508a04fc70a8c6046137959b` |
| `openhands-tools/pyproject.toml` | `046d8009e820d400195389cc30ee5c33bf4d65ed8cb0f9fb3a46a9621f19f6de` |
| `pyproject.toml` | `e8a035d7e9aad2d0bfbdfb595a5df4868c9cb0ab40c6db6e1d7918a0a33ba888` |
| `uv.lock` | `16ddd3b2915bbe9ba6c22d03fa1682e74e2ff33682a3a2f394125099fb884ec4` |
