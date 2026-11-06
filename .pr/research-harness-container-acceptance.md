# Research harness container acceptance gate

Status: **passed** in [run 42263099121](https://github.com/AhmadQasim881/software-agent-sdk/actions/runs/42263099121)
on 2026-11-06. The normal source image, installed package checks, authenticated
server, actual MCP memory reads, same-container restart persistence and cleanup
all passed. The run also passed all eight focused driver tests. No model call was
made. The unaltered [runtime evidence](research-harness-container-runtime.json)
is retained in the repository.

## Successful hosted result

The corrected run tested commit
`48364f5ea90ac6b9d67da08bcbc1374c6da46a59`, complete tree
`cb2906019aa8b25eedfe22f7e520957128ff06b5`. The harness runtime subtree remained
`c2bcb08d967f1f3e4d0bffa49bceec46f1d273c4`, matching the separate 411-test
[installed-wheel acceptance](research-harness-hosted-acceptance.md).

| Check | Actual successful result |
| --- | --- |
| Driver regressions | Eight passed, zero errors, failures or skips; JUnit duration 0.657 seconds. |
| Standard source image | Existing `linux/amd64` `source-minimal` target built and loaded successfully from the exact root source archive. |
| Installed files and skill | All 30 selected files matched checkout, archive and image bytes; the strict skill loader accepted `research-review`. |
| Installed packages | SDK, tools, workspace and Agent Server all reported version `1.60.1` and `editable: false`. |
| Normal image runtime | User `openhands`, UID `10001`, isolated `/agent-server/.venv/bin/python`, Python `3.13.17`. |
| Server readiness and authentication | All nine doctor checks passed before restart and all nine passed after restart; reported build SHA matched the tested commit. |
| Actual MCP calls | Five successful server-side reads: get/recall before restart, then get/recall/history after restart. HTTP, outer connection and inner tool outcomes all passed. |
| Memory and provenance | Exact updated version-2 note, source, evidence reference and history versions 1/2 survived; the matching note from another thread was excluded. |
| Restart identity | Same container ID and image, with a changed start timestamp. |
| Model activity | Zero conversations before/after and no model call. |
| Cleanup | No remaining MCP child after each call; container, image tag, two verification profiles and private state removed; final owned loopback port closed. |

The run directly observed Docker changing the same container's published binding
from **`127.0.0.1:32768`** to **`127.0.0.1:32769`**. Its start time changed from
`17:37:46.168830956Z` to `17:38:15.224930038Z`. The driver created the new
authenticated profile before readiness, and subsequent doctor and memory calls
used that profile. This confirms the behavior implicated by the first run's
failure; that earlier run itself did not retain its changed binding.

Runtime verification ran from `17:37:46.045045 UTC` to `17:38:33.810195 UTC`,
including cleanup. The source archive SHA-256 was
`f2fdabbdf652dd4bc1ddb41679262f1c8e846f8f3c82a419257ea619bf8f4733`.
The image ID matched Buildx's config digest:
`sha256:9edde2f4027467411cfd77519b242d0c66c9aaf6f8abbca6d64de46052c92629`.

The [uploaded artifact](https://github.com/AhmadQasim881/software-agent-sdk/actions/runs/42263099121/artifacts/13525782275)
has ID **13525782275**, size **81,874 bytes**, and scheduled expiry
**2026-11-13 17:38:34 UTC**. Its downloaded ZIP matched the GitHub-reported SHA-256
`d3cff0d67f3f35bc78ad185245c556611a31b2033a72ccfd62eb62525a81c751`.
The retained runtime JSON SHA-256 is
`c1067df48792429bd50c8a138b51225defe1bec2791720a52c94336f2f5b7581`;
the JUnit XML SHA-256 is
`4895a749d9f9cf2b12674e199ce408b78d9fe4db9c3de102f1f97f6c092f1b21`.
The complete downloaded evidence was inspected at
`/tmp/research-harness-container-ci-42263099121-zd825fcj`.

## Scope

The separate `research-harness-container-acceptance.yml` workflow runs on relevant
pushes to `feature/security_harness` and supports manual dispatch. It uses the
existing public Dockerfile, one `linux/amd64` image, the normal `source-minimal`
target, and the normal source-context helper with an exact prebuilt root sdist.
ACP provider and optional capability installation are disabled using the existing
build arguments. The Docker image retains the Dockerfile's normal locked
dependency installation, including its default development group.

The host installs the existing locked development group and runs the eight focused
driver tests before building the image. This workflow has its own concurrency
group and does not modify the separate installed-wheel acceptance workflow.

## First hosted result

| Surface | Assertion made by the gate | First hosted result |
| --- | --- | --- |
| Build provenance | Source sdist SHA-256, source tree/build commit, Dockerfile and lock hashes, Buildx image identity and public base-image digest evidence | Passed |
| Image installation | Exact installed bytes for all selected harness/MCP modules and Markdown assets plus selected server modules; isolated image Python; non-editable workspace packages and imports under image site-packages; strict packaged skill load | Passed: 30 files, four non-editable workspace packages |
| Server lifecycle | Normal non-root image entrypoint; authenticated loopback readiness, doctor and matching server build SHA before and after restart | Initial checks passed; external readiness after restart failed |
| Server-side MCP | Actual `/api/mcp/test` requests execute memory get and scoped recall before restart, then updated get, scoped recall and exact two-version history after restart | Initial get and recall passed; post-restart calls not reached |
| Persistence | Same container ID with a changed start timestamp; the updated note, evidence reference and history survive restart in that container's writable layer | Not verified |
| Ownership and cleanup | Exact owner label/image identity; no lingering MCP child after each call; owned container and image tag removed; loopback port closed; private key and verification state removed | Passed for the completed operations and recorded port |

The fixture contains only a small Python import inventory and benign notes. Writes
use the installed, existing `HarnessService` from a fixed operator probe. Remote
reads use the existing server MCP endpoint. No model conversation is created, and
the server conversation count must remain zero. The gate adds no production API
and makes no claim about native `RemoteConversation.execute_tool()` support or
container replacement with a separate persistent volume.

## First hosted run and restart repair

[Run 42260634762](https://github.com/AhmadQasim881/software-agent-sdk/actions/runs/42260634762)
tested commit `42d6d0d0dadf1edc030da458ff8476ef1963cf4a`, source tree
`7174e41d83bf985b22b9eec902f03e924f727ca7`, and source archive SHA-256
`801a711657aaae7afe66c4b4dc8d9c0fc24a70fba80ec0a1fe8ac7cdcc5aed0c`.
The image ID matched the recorded Buildx config digest:
`sha256:546ce5409f5bdb016f72afa382a597127bd4fb542fc841e1712edee5f3a08587`.

The run passed its original six driver tests. Installed checks matched 30 files,
loaded the packaged skill, and verified all four workspace distributions at
version `1.60.1` with `editable: false`. The image ran as UID `10001` with its
isolated Python `3.13.17`. All nine initial doctor checks passed, including
authenticated access and rejection of missing/invalid credentials. Both real
server-side MCP reads passed HTTP, outer result and inner tool result checks;
neither left a matching MCP child. The operator then updated the note to version
2. No model calls were made.

After Docker restart, 175 readiness attempts to the original host endpoint
`127.0.0.1:32768` were refused. The server log shows the restarted process completed
initialization at `2026-11-06 17:22:57 UTC` and listened internally on
`0.0.0.0:8000`, without subsequent successful HTTP requests. The original driver
attempted readiness before inspecting the restarted container, so the artifact
contains no post-restart Docker binding or before/after inspection records.
A changed dynamic host port is consistent with this evidence, but the change and
its actual port were not observed and are not proven. Post-restart memory reads
were not reached. Cleanup reported no errors and removed the owned container,
image tag and private state.

The repair inspects the same owned container immediately after restart and saves
both inspection records before readiness. It retains the container ID and changed
start-time checks, validates the current loopback binding, and creates a new
authenticated verification profile with the same private key when the host port
has changed. All later doctor/API calls use the current profile. Cleanup purges
every profile and retains each profile's evidence in a separate directory, so
matching evidence filenames cannot overwrite one another. Readiness limits and
authentication checks remain unchanged.

## Resource and secret handling

One randomly named container is created from the verified image and labeled with
its ownership token. The container uses the normal image user, a loopback-only
ephemeral published port, and ordinary memory/CPU/PID limits. There are no host
mounts, Docker socket mounts, additional capabilities or privileged containers.

Session and secret keys are generated into private files and passed with an env
file. Auth values are absent from command arguments. Docker inspection is filtered
to nonsecret fields, and server logs and HTTP evidence use the maintained
verification CLI's redaction. Only `reports/` is uploaded, for seven days.

The driver records ownership immediately after checking the image ID against
Buildx metadata, before validating packaged assets. Consequently a missing skill
still removes the known image. The always-run cleanup step also attempts this
claim if the runtime step never recorded ownership. Changed or unproven image
identities and unowned containers are refused. Cleanup retains private redaction
keys only if a failed container removal needs the always-run retry.

## Local validation on 2026-11-06

- The initial six focused tests passed: absent/changed archive skill, initial refused
  connection retry, unowned container refusal, removal of a verified image after
  archive failure, and refusal to claim/delete a changed image.
- After the repair, all eight focused tests passed in 0.34 seconds. The two new
  cases exercise changed and unchanged ports using real loopback HTTP servers and
  the actual CLI attach/API profiles. The changed-port case closes the old
  listener, requires authenticated reads at the new endpoint, and checks both
  profiles' private-key cleanup and separately retained evidence. Only the Docker
  boundary is faked; this does not claim an actual local container restart.
- All per-file pre-commit hooks passed for the new workflow, driver and tests,
  including YAML formatting, Ruff, PEP8 and Pyright. All applicable hooks passed
  again for the changed driver and tests.
- The workflow parsed successfully; all seven shell blocks passed `bash -n`.
- The driver `--help` path passed without Docker.
- The source manifest checked 30 installed-file expectations against the existing
  actual root sdist, including the skill. Its SHA-256 was
  `df6a431c889aaa918ce914be20f51aaf960a5e795681b22788010b5c2e0ce119`.
  The checkout was `72e17e7a9810549cd12dac5baedaaf53b1c9070b` with source tree
  `e384061a68af589db17a174319bd5e2723baecfe`. Hosted execution builds a fresh sdist
  and records that run's exact commit and hashes.
- `git diff --check` passed. No production files were changed for this gate.

Local checks established endpoint-refresh behavior and archive expectations.
The subsequent successful hosted run above supplies the actual Docker restart
and memory persistence evidence.

## Hosted results to review

The artifact `research-harness-container-<run-id>-<attempt>` contains the focused
JUnit results, source/context/build logs, Docker/Buildx versions, image build
metadata, `container-acceptance.json`, `cleanup.json`, redacted server logs, and
saved real HTTP exchanges. The runtime command exits successfully only when all
required checks and cleanup pass. Build and runtime steps have explicit bounds;
the job is limited to 45 minutes.
