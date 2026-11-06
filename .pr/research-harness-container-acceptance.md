# Research harness container acceptance gate

Status: implementation and local checks passed; the actual Docker build and runtime
checks are pending GitHub-hosted execution. Docker is not installed in the local
workspace. This report does not claim that a container has passed acceptance.

## Scope

The separate `research-harness-container-acceptance.yml` workflow runs on relevant
pushes to `feature/security_harness` and supports manual dispatch. It uses the
existing public Dockerfile, one `linux/amd64` image, the normal `source-minimal`
target, and the normal source-context helper with an exact prebuilt root sdist.
ACP provider and optional capability installation are disabled using the existing
build arguments. The Docker image retains the Dockerfile's normal locked
dependency installation, including its default development group.

The host installs the existing locked development group and runs the six focused
driver tests before building the image. This workflow has its own concurrency
group and does not modify the separate installed-wheel acceptance workflow.

## Required runtime evidence

| Surface | Assertion made by the gate | Current status |
| --- | --- | --- |
| Build provenance | Source sdist SHA-256, source tree/build commit, Dockerfile and lock hashes, Buildx image identity and public base-image digest evidence | Awaiting hosted build |
| Image installation | Exact installed bytes for all selected harness/MCP modules and Markdown assets plus selected server modules; isolated image Python; non-editable workspace packages and imports under image site-packages; strict packaged skill load | Awaiting hosted runtime |
| Server lifecycle | Normal non-root image entrypoint; authenticated loopback readiness, doctor and matching server build SHA before and after restart | Awaiting hosted runtime |
| Server-side MCP | Actual `/api/mcp/test` requests execute memory get and scoped recall before restart, then updated get, scoped recall and exact two-version history after restart | Awaiting hosted runtime |
| Persistence | Same container ID with a changed start timestamp; the updated note, evidence reference and history survive restart in that container's writable layer | Awaiting hosted runtime |
| Ownership and cleanup | Exact owner label/image identity; no lingering MCP child after each call; owned container and image tag removed; loopback port closed; private key and verification state removed | Awaiting hosted runtime |

The fixture contains only a small Python import inventory and benign notes. Writes
use the installed, existing `HarnessService` from a fixed operator probe. Remote
reads use the existing server MCP endpoint. No model conversation is created, and
the server conversation count must remain zero. The gate adds no production API
and makes no claim about native `RemoteConversation.execute_tool()` support or
container replacement with a separate persistent volume.

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

- Six focused tests passed: absent/changed archive skill, initial refused
  connection retry, unowned container refusal, removal of a verified image after
  archive failure, and refusal to claim/delete a changed image.
- All per-file pre-commit hooks passed for the new workflow, driver and tests,
  including YAML formatting, Ruff, PEP8 and Pyright.
- The workflow parsed successfully; all seven shell blocks passed `bash -n`.
- The driver `--help` path passed without Docker.
- The source manifest checked 30 installed-file expectations against the existing
  actual root sdist, including the skill. Its SHA-256 was
  `df6a431c889aaa918ce914be20f51aaf960a5e795681b22788010b5c2e0ce119`.
  The checkout was `72e17e7a9810549cd12dac5baedaaf53b1c9070b` with source tree
  `e384061a68af589db17a174319bd5e2723baecfe`. Hosted execution builds a fresh sdist
  and records that run's exact commit and hashes.
- `git diff --check` passed. No production files were changed for this gate.

Local checks establish driver correctness and archive expectations; they do not
substitute for the hosted Docker build and real server requests.

## Hosted results to review

The artifact `research-harness-container-<run-id>-<attempt>` contains the focused
JUnit results, source/context/build logs, Docker/Buildx versions, image build
metadata, `container-acceptance.json`, `cleanup.json`, redacted server logs, and
saved real HTTP exchanges. The runtime command exits successfully only when all
required checks and cleanup pass. Build and runtime steps have explicit bounds;
the job is limited to 45 minutes.
