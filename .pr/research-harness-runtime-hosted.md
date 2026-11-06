# Hosted verification of memory and runtime completion

## Tested revision

The hosted workflows test implementation commit
[`e88179608fd37b852a32fe193725df6f57e2f56b`](https://github.com/AhmadQasim881/software-agent-sdk/commit/e88179608fd37b852a32fe193725df6f57e2f56b)
and source tree `9c3559af353fd052486ed7eb8e5c507ba5271f17` on
`feature/security_harness`. The evidence in this report concerns that revision;
retaining the results afterward does not change its production code.

The [local completion report](research-harness-runtime-completion.md) explains
the memory-query correction, frozen bootstrap and worker packaging, POSIX
process-group cleanup, and the acceptance-driver survivor assertion. It also
retains the independently exercised normal frozen Agent Server's authenticated
startup, catalog and cleanup results.

## Installed wheel and frozen executable

[Research harness acceptance run 42273990645](https://github.com/AhmadQasim881/software-agent-sdk/actions/runs/42273990645)
completed successfully on 2026-11-06 at 18:59:21 UTC. Every step passed, including
the actual frozen checks, evidence upload and final job cleanup. The uploaded
artifact's digest matched GitHub metadata; its JUnit XML, installed-package report
and frozen-runtime report were downloaded and inspected.

| Check | Actual result |
| --- | --- |
| Combined regression suite | **575 passed**, zero failures, errors or skips, in **239.13 seconds** using four workers. |
| Runnable example | Passed its memory, context, fixture and cleanup assertions with no model call. |
| Standard tools wheel | Built normally and installed in a fresh locked runtime. |
| Installed file integrity | All **28** selected research files matched checkout, wheel and installed bytes. |
| Import provenance | **78** tools/harness module origins were inside the selected installed runtime. |
| Locked versions | **182** installed distributions matched versions in `uv.lock`. This checks versions, not dependency artifact hashes. |
| Installed interfaces | **19** CLI calls and **two** actual MCP processes passed; discovery exposed **24** shared operations. |
| Memory behavior | All six operations, stale-write rejection, source/evidence references, bounded context, retained history and tombstones passed. |
| Generated configuration | Codex, Claude Code and Claude Desktop review bundles matched the selected interpreter and packaged skill. |
| Normal frozen build | The existing PyInstaller specification built an actual executable with the new bootstrap and fixed worker source. |
| Frozen interfaces | **18** CLI/probe commands, native SDK `HookManager`, a generated host-hook command and **two** real MCP processes passed. |
| Frozen memory and context | Updated records, immutable history, provenance, thread exclusion, context bytes and manifest digest matched; the second MCP process recovered a note written by the first. |
| Frozen cleanup assertion | Every command recorded an empty survivor list before guardian cleanup. MCP connections closed without survivors; no final process termination was needed. |
| Live public metadata | One actual installed-wheel request and one actual frozen-worker request each returned **HTTP 200**. |
| Owned state | Both installed-runtime fixtures were removed successfully. No model request was made. |

The tools package was a non-editable wheel installation. Its SDK dependency
remained editable from the tested checkout, as the workflow explicitly records.
The separate container gate below verifies all four workspace packages as
non-editable installations. Native host profile installation and client dispatch
are not inferred from configuration generation or protocol subprocesses.

The hosted frozen check used Python 3.13.18 on Linux. Its executable was
138,121,472 bytes, with SHA-256
`11a1179399fd92488fe07dfe9683d37418907568367642e7e87d171bd15edc6c`.
Its skill and standalone HTTP-worker source matched checkout bytes. This is a
separate build from the local Linux executable; identical binary bytes across
those different build environments are not claimed.

### Actual live observations

Both requests used the harness's fixed, unauthenticated public endpoint
`https://api.github.com/repos/AhmadQasim881/software-agent-sdk`. Each returned
17,106 bytes and validated the expected public fork identity. These were actual
harness worker requests, not connector reads substituted for runtime execution.

| Runtime | Retrieval time, UTC | HTTP | Response SHA-256 |
| --- | --- | --- | --- |
| Fresh installed tools wheel | 2026-11-06 18:54:10.746733 | 200 | `f9027e59f58f7382e0ed5aa75536ae0242c583cc609e2affa82bea1fd9d17b94` |
| Frozen Agent Server worker | 2026-11-06 18:59:15.391313 | 200 | `c6cfda30b6644b0e13f3384f371ae56060fbdd1fc7f2758edfc170953e037168` |

The records preserve retrieval time, metadata and response/observation digests.
They describe public repository metadata at retrieval time, not a security
assessment. The failed local DNS observation remains recorded in the local
completion report; it was not relabeled as a successful request.

### Retained acceptance evidence

The [uploaded acceptance artifact](https://github.com/AhmadQasim881/software-agent-sdk/actions/runs/42273990645/artifacts/13533510246)
has ID `13533510246`, size 36,344 bytes and SHA-256
`dca5fac5e5561ca9d42c703a6e27e6e50cadc76838b7314b63db2dc0f4e964ce`.
GitHub schedules expiry for 2026-11-13 at 18:59:16 UTC. Both JSON reports below
are retained unchanged.

| Evidence | SHA-256 |
| --- | --- |
| [Installed-wheel report](research-harness-runtime-hosted-install.json) | `f4d3caaabaf0e7b4cdf64ddd82613880f145181e5213f0ab77a8e6f24484cf70` |
| [Frozen-runtime report](research-harness-runtime-hosted-frozen.json) | `795f001b80d2e4375ba3ae3f2438440e5ee1bd6a76d6cdb6efc9fd1c6b862bc8` |
| JUnit XML in the verified artifact | `9a25f4bcbc784421d73d3c040f0c0794b5746e51407219391b5827cc9a9a6984` |
| CI-built tools wheel | `caff8429adedeac7da11621bc5acc92e35038b6b3c0fcd3c205d7e1ee9a1f1bb` |

## Source-minimal container

[Research harness container acceptance run 42273990466](https://github.com/AhmadQasim881/software-agent-sdk/actions/runs/42273990466)
completed successfully on 2026-11-06 at 18:51:14 UTC. Every job step passed,
including the owned-resource cleanup and evidence upload. The downloaded artifact
matched its GitHub-reported SHA-256; its runtime report and eight-test JUnit
results were inspected.

| Check | Actual result |
| --- | --- |
| Driver failure and cleanup regressions | All eight tests passed. |
| Build | The existing `linux/amd64` `source-minimal` target built and loaded from the exact checked-out source archive. |
| Installed file integrity | 31 selected source, archive and installed-file hashes matched, including the new research runtime module. |
| Package installation | SDK, tools, workspace and Agent Server all reported `1.60.1` and `editable: false` under image `site-packages`. |
| Runtime | User `openhands`, UID `10001`; `/agent-server/.venv/bin/python`, Python `3.13.17`. |
| Readiness and authentication | All nine doctor checks passed before restart and all nine passed afterward. Both server-info results reported the exact implementation commit. |
| Real server MCP operations | Five authenticated requests returned HTTP 200 and valid inner results: get/recall before restart, then get/recall/history afterward. |
| Persistence and provenance | The original version-1 note, updated version-2 note, source and evidence references matched; history returned exactly versions 1 and 2. |
| Thread scoping | A matching note from another thread was excluded from recall. |
| Restart identity | The same container had a new process ID and start time. Its loopback binding changed from port `32768` to `32769`, and verification used the new binding. |
| Process cleanup | None of the five MCP requests left an owned MCP child. |
| Model activity | Conversation counts stayed zero and the report recorded zero model calls. |
| Final cleanup | The owned container, image tag, both verification profiles and private state were removed, with the active loopback port closed. |

Runtime verification ran from `2026-11-06T18:50:06.168151+00:00` through
`2026-11-06T18:51:09.927847+00:00`. The source archive SHA-256 was
`5c213831c243bf72af9c505c539b28476a3cc8c121dc49eb626224e63225935c`.
The image ID was
`sha256:c5ca6833858757e43f10ebd74897a8e5e89fdf6efa08aaf1f60f9f5a366be160`.

The unaltered [container runtime report](research-harness-runtime-hosted-container.json)
is retained with SHA-256
`4fe3af8c8985bb0cc82d8f8816d937e8286748dd043061d2ca654c5a59e15e9d`.
The [uploaded container artifact](https://github.com/AhmadQasim881/software-agent-sdk/actions/runs/42273990466/artifacts/13531193688)
has ID `13531193688`, size 84,229 bytes and SHA-256
`1b28b5019d933def420c2098840ce845e121ebccebdd1f4588b44af8f38ba504`.
GitHub schedules that artifact to expire on 2026-11-13 at 18:51:10 UTC; the
committed runtime JSON preserves its actual observations independently of that
artifact retention period.

## Interpretation and remaining gates

These checks use benign owned fixtures and the shared memory and observation
interfaces. The [implementation map](../New_security_Harness_Design/README_IMPLEMENTATION.md#2-architecture-and-pdf-mapping)
explains the workshop responsibilities implemented by the package. The memory
adaptation uses the requested source project's deterministic lexical hashing and
rank fusion; no semantic retrieval-quality benchmark or universal optimality is
claimed.

Linux frozen behavior and the standard source-minimal container are the tested
deployment scopes. Native Claude or Codex client dispatch, a real provider's skill
selection, the model-driven remote conversation loop, other Docker targets,
Windows/macOS frozen execution, and replacement-container persistence on a
separate volume require corresponding acceptance runs. POSIX process ownership
is not an OS or network isolation guarantee. The tests make no model requests.
