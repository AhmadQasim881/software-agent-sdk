# Native Codex memory acceptance: blocked before the model turn

## Result

The installed Codex CLI accepted an isolated configuration containing only the
research harness MCP server, but the client could not initialize its in-process
app server in this environment. The relevant-request case exited with code 1
after 0.18 seconds and reported:

```text
Error: failed to initialize in-process app-server client: Read-only file system (os error 30)
```

No model completion, token usage, MCP tool call, or skill selection was observed.
This result does **not** establish native host memory recall or skill-selection
acceptance. It is also separate from the OpenHands `evaluate-skill` provider
evaluator, which was not invoked by this check. No greeting case followed the
failed initialization.

The sanitized machine-readable observations are in
[`research-harness-codex-evidence.json`](research-harness-codex-evidence.json).

## Subject and fixture

- Client: `codex-cli 0.167.0-alpha.4` at `/opt/codex/bin/codex`.
- Harness runtime tree: `c2bcb08d967f1f3e4d0bffa49bceec46f1d273c4`.
- Packaged skill SHA-256:
  `bd4cf9931fdeae6a80c5c89c03909bf843cabf68401e1fee9312cdfe44baa020`.
- Existing client authentication was used normally. The probe did not read,
  print, copy, or relocate credential files, or modify a host profile.
- The fixture used an owned temporary Git workspace and a separate, disjoint
  state directory. It contained one operator-authored documentation-review
  decision: the chosen display label was `Cedar Lantern 47`.
- `write_host_bundle(host="codex", allow_github=False, ...)` produced the MCP
  configuration and packaged skill. The skill bytes were copied to the owned
  project's `.agents/skills/research-review/SKILL.md`.
- The user prompt supplied the saved thread ID and asked which display label
  had been chosen and where the decision came from. It did not contain the
  answer or name the skill.

The owned workspace instructions limited the case to reading the saved thread,
context, and memories. They prohibited terminal commands, unrelated file
inspection, network tools, and note modifications. No real repository review,
vulnerability discovery, exploit, or target interaction formed part of the
fixture.

## Preflight and invocation

Before the client invocation, `codex mcp list --json` with a whole-table
`mcp_servers` override returned exactly one enabled server:

```json
[{"name": "research_harness", "enabled": true}]
```

The configured command was the existing repository environment's Python with
`-I -m openhands.research`, fixed workspace/state arguments, and `mcp`. Public
GitHub access was disabled. The server had 20-second startup and tool deadlines.

`codex features list` verified that every requested availability gate was false,
including apps/connectors, plugins and remote plugins, hooks, native memories,
browser/computer tools, image generation, delegation, command execution,
code-mode host tools, and automatic skill MCP-dependency installation. The full
verified list is in the evidence JSON.

The client invocation used:

```text
codex --no-daemon exec
  --ignore-user-config --ephemeral --strict-config
  --sandbox read-only --json --color never
  -C <owned-workspace>
  -c mcp_servers=<single-generated-server-table>
  -c web_search="disabled"
  -c model_reasoning_effort="low"
  -c skills.bundled.enabled=false
  -c history.persistence="none"
  -c analytics.enabled=false
  -c feedback.enabled=false
  -c sqlite_home=<owned-client-state>
  -c log_dir=<owned-log-directory>
  <verified feature-disable flags>
  <one benign saved-review request>
```

The invocation had a 60-second wall-clock deadline and an owned process group.
It used no sandbox or hook-trust bypass and did not alter `HOME` or `CODEX_HOME`.
The client selected its own default model; no model was successfully observed
in a response.

The initial preflight also examined `unified_exec`. Its user disable override
remains true by design in this client: the official configuration reference
documents that user opt-outs are ignored. The separate `shell_tool` gate is
documented to control command execution tools and was verified false. The
probe did not treat a false `unified_exec` value as an isolation requirement
after checking these documented semantics.

References: installed `codex exec --help`, `codex features list`, and the
[official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
for `features.shell_tool`, `features.unified_exec`, MCP configuration, and
`model_reasoning_effort`.

## Observed acceptance boundary

| Check | Observed result |
| --- | --- |
| Generated config resolves to only the intended MCP server | Passed |
| App/plugin/command-tool gates disabled before invocation | Passed |
| Packaged skill copied with matching content hash | Passed |
| Native client initializes and starts a model turn | Blocked by read-only filesystem error |
| Native MCP memory call and response | Not observed |
| Correct seeded label in model answer | Not observed |
| Automatic skill selection | Not observed |
| Model token usage or completed inference | Not observed |
| Authored memory unchanged after the attempted invocation | Passed |
| No live owned subprocesses after cleanup | Passed |
| Owned temporary workspace, state, and raw output removed | Passed |

The error did not identify the filesystem path that could not be written.
This check does not assign that failure to the MCP server, harness memory, or
the read-only execution policy without evidence. It stopped at the native
client initialization failure and did not retry through a different credential
location, relaxed sandbox, or permissions workaround.

The remaining native-host gate is to run this same bounded saved-review case
where the normal authenticated client can initialize, then retain the actual
MCP operation/result, final answer, usage, skill-selection evidence if emitted,
and cleanup result. Existing generated-config and Python MCP tests remain
valuable but do not replace that host observation.
