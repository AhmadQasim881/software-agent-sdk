# Successful hosted harness acceptance

## Observed result

The [Research harness acceptance run 42255004206](https://github.com/AhmadQasim881/software-agent-sdk/actions/runs/42255004206)
completed successfully on 2026-11-06 for published commit
[`0cdbaffe843f995ab66a9f4e11f229c73851d3bf`](https://github.com/AhmadQasim881/software-agent-sdk/commit/0cdbaffe843f995ab66a9f4e11f229c73851d3bf).
Every job step passed. The complete checked-out tree was
`e384061a68af589db17a174319bd5e2723baecfe`; the harness runtime subtree was
`c2bcb08d967f1f3e4d0bffa49bceec46f1d273c4`.

This run closes the fresh dependency-installation and actual live public GitHub
response gates left open by the local environment. It does not establish native
model/client behavior, Docker execution or full frozen subprocess parity.

The uploaded artifact was downloaded, its SHA-256 matched GitHub's artifact
metadata, and its JUnit XML and installed-package JSON were inspected. The
unaltered [installed-package report](research-harness-hosted-install.json) is
retained in the repository for review after the hosted artifact expires.

## Results

| Check | Observed result |
| --- | --- |
| Complete harness, baseline, packaging and runner regressions | **411 passed**, zero failures, errors or skips, in **677.49 seconds**. |
| No-model example | Passed, including persisted memory, context and fixture cleanup. |
| Standard tools wheel build | Passed using the normal isolated build. |
| Fresh locked runtime | Passed with Python **3.13.18**; report identifies `runtime_kind: fresh-locked`. |
| Package file integrity | **27 files** matched checkout, wheel and installed bytes. |
| Imported module provenance | **78 module origins** were inside the selected installed runtime. |
| Dependency versions | **182 installed distributions** matched versions present in `uv.lock`. |
| Real CLI operations | **19 calls** passed; schema discovery exposed **24 shared operations**. |
| Real MCP processes | **Two processes** passed discovery, memory write/read and recall across restart. |
| Memory behavior | All six operations, stale-write rejection, provenance, bounded context, history and tombstones passed. |
| Generated host bundles | Codex, Claude Code and Claude Desktop bundles matched the packaged skill and selected interpreter. |
| Live public metadata | One real harness request returned **HTTP 200** and a completed observation. |
| Owned temporary state | Removed successfully; `temporary_root_removed: true`. |

The SDK dependency remained editable from the checked-out SDK source, as declared
by the workflow and report. The tools package was installed from the newly built
wheel, with editable tools explicitly rejected. Matching distribution versions
to the lock is not a claim of dependency artifact-hash verification.

The sole pytest warning concerned Pydantic's handling of a `ReadOnly` qualifier on
`ChatCompletionReasoningItem.summary`; it did not cause a failure. No model request
was made by this workflow.

## Live observation

The installed harness made one bounded, unauthenticated request to
`https://api.github.com/repos/AhmadQasim881/software-agent-sdk`. This was the actual
harness HTTP worker running in the fresh runtime, not the GitHub connector used
to inspect repository state.

The observation began at `2026-11-06T16:57:05.042359+00:00` and recorded retrieval
at `2026-11-06T16:57:05.539703+00:00`. It returned HTTP 200, **17,106 response bytes**,
and validated `full_name: AhmadQasim881/software-agent-sdk`, `private: false`,
`fork: true`, `archived: false` and `default_branch: main`.

The report retained the response digest
`649d4444de68ef1690e660832d792041f7c749d6a9579affc8cdb180ffa2e782`
and the persisted observation artifact digest
`285bf709948c21ebcd365463c9e44e8edbeffffd20f818a309b21719572c843f`.
The observation reports public metadata at retrieval time; it is not a security
assessment. Its temporary store was removed after the check.

## Evidence integrity

The [uploaded artifact](https://github.com/AhmadQasim881/software-agent-sdk/actions/runs/42255004206/artifacts/13522368761)
has ID **13522368761**, size **14,706 bytes**, and scheduled expiry
**2026-11-13 16:57:07 UTC**. The downloaded archive matched the digest reported
by GitHub. Its files were parsed locally from
`/tmp/research-harness-ci-42255004206-pnsdy9co`.

| Artifact | SHA-256 |
| --- | --- |
| Downloaded acceptance ZIP | `89d67d7fd494b752cfc4d39f77b8ec3a9cbd59c569b33eda97f1a2aa18be03d4` |
| Installed-package JSON, retained unchanged | `aeea9989aca031903a88c7852cd6ab332a05782731fc86a5585337d27a7d96d0` |
| JUnit XML | `afc4edb178e3ff6f446e804aed0871f703baf0d97158af475fb86afb054d1106` |
| Example output log | `5cbad72b965fced9a07a5cba9b59a2ce9c34d5a1ce10ec882d5671f44d4e552a` |
| CI-built tools wheel | `dcae288eeccb491cbb1e716e1fe89802b7f3c548d8703a7aa2750de3044b96c6` |

The CI wheel digest differs from the earlier local wheel digest. Both builds
independently matched the same 27 source files; ZIP-level identity across separate
build environments is not required or claimed.

For the remaining native model/client and deployment boundaries, see the
[completion follow-up](research-harness-followup-verification.md). This hosted
run adds successful external acceptance evidence without changing those
unobserved claims.
