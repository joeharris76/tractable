# MCP client registration for `tractable`

`tractable` ships one MCP server, `tractable-mcp`, installed with the `mcp` extra.
One server instance is one worker identity against one state branch. It speaks
**stdio**; there is no HTTP transport.

```
tractable-mcp [--repo-root <path>] [--config <path>]
            [--state-remote <url-or-path>] [--state-branch <name>]
            [--cache-dir <path>] [--actor <worker>]
            [--session <id>] [--log-level {debug,info,warning,error}]
```

Common flags:

- `--repo-root` — project root for `.tractable/config.json` discovery. Omit it
  when the client launches the server from the project root.
- `--state-remote` / `--state-branch` — the authoritative state branch.
  Flags beat `TRACTABLE_STATE_REMOTE` / `TRACTABLE_STATE_BRANCH`, which beat the
  discovered config.
- `--actor` — stable worker identity. It overrides `TRACTABLE_ACTOR`; either form
  supports ordinary restart/re-adoption. Give concurrent logical workers
  different actors, and never run one actor in two servers at the same time.
- With no actor, the server derives an instance-scoped identity from
  `clientInfo.name`, user/host, and its per-process session. Separate default
  servers are isolated even when their client names match.
- `--session` — override the generated session identifier. Reusing the same
  value with the same client name and user/host deliberately recreates a
  fallback identity for restart recovery. Do not put one shared value in a
  global registration: concurrent servers would then be the same worker.
- `--cache-dir` — local state cache, one Git object store per state branch (default `~/.cache/tractable-state`).

Logging goes to **stderr only**; stdout carries JSON-RPC framing.

### Claim slots

A worker holds one live claim outside any batch and one live claim per active
batch. Leaders of different batches that share one server therefore take and
prepare their members concurrently, and each batch keeps its own
`owner_generation`. Agents on one server that work the same batch, or that all
claim ordinary items, still share one slot and get `E_MULTIPLE_CLAIMS`; give
them separate servers with distinct actors.

`prepare` takes `verification_path` and `finish` takes `final_evidence_path`:
an absolute path to a JSON file (at most 1 MiB) holding the object, as an
alternative to pasting it inline. Pass one or the other. `prepare` requires
`verification.review.outcome` to be `clear`, `not-reviewed`, or `override`, and
`verification.command` to be a non-empty argv list of single-line strings whose
first element is the executable name.

### Upgrade and restart recovery

Claims made by 0.7.3 or earlier use the legacy fallback
`mcp:<clientInfo.name>:<user>@<host>`. An upgraded default server will not
silently adopt one of those claims. Stop the old server first, then either:

1. restart with `--actor '<exact legacy claim worker value>'` and call `take` on
   the same item, which rotates the generation; or
2. let the lease expire and take the item with the new server.

For new claims, prefer a stable unique `--actor` when automatic recovery is
required. A fallback worker can instead record the startup `session id` from
stderr and restart once with `--session '<recorded id>'`. These identities and
claim generations are cooperative credentials, not authentication secrets.

## Claude Code

File: `.mcp.json` at the **project root** (Claude Code launches servers from
there). Do not set `--repo-root`; the default working directory is already the
project root. Note that `${workspaceFolder}` does **not** expand in Claude
Code — only `${VAR}` and `${VAR:-default}` for environment variables.

```json
{
  "mcpServers": {
    "tractable": {
      "command": "tractable-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

Claude Code surfaces MCP **prompts** as slash commands (`/tractable/workflow`). The
`tractable://instructions` resource and the `get_instructions` tool are the portable
fallbacks for clients that do not.

## Codex

File: `~/.codex/config.toml`, which is user-global. A single
`[mcp_servers.tractable]` entry cannot carry a per-project `--repo-root`, so
Codex resolves the project from its own working directory through the same
upward `.tractable/config.json` discovery the CLI uses.

```toml
[mcp_servers.tractable]
command = "tractable-mcp"
args = []
```

## Cursor

File: `.cursor/mcp.json`, or Cursor's global MCP settings. Both use an
`mcpServers` block; `${workspaceFolder}` expansion varies by version, so omit
`--repo-root` if it does not expand.

```json
{
  "mcpServers": {
    "tractable": {
      "command": "tractable-mcp",
      "args": ["--repo-root", "${workspaceFolder}"]
    }
  }
}
```

## Windsurf

File: `~/.codeium/windsurf/mcp_config.json`, or the Windsurf UI.

```json
{
  "mcpServers": {
    "tractable": {
      "command": "tractable-mcp",
      "args": ["--repo-root", "${workspaceFolder}"]
    }
  }
}
```

## Zed

File: `~/.config/zed/settings.json`, under `context_servers` (Zed's name for
MCP servers).

```json
{
  "context_servers": {
    "tractable": {
      "command": "tractable-mcp",
      "args": ["--repo-root", "${workspaceFolder}"]
    }
  }
}
```

## Continue

File: `~/.continue/config.json`. Continue's MCP support is experimental; check
your version's schema.

```json
{
  "mcpServers": [
    {
      "name": "tractable",
      "command": "tractable-mcp",
      "args": ["--repo-root", "${workspaceFolder}"]
    }
  ]
}
```

## Any other stdio MCP client

Nothing above is client-specific beyond the file location and the surrounding
key names. Any client that launches a stdio MCP server works: point it at the
`tractable-mcp` command with no arguments, from the project root.

A client that does not speak MCP cannot drive the tracker. The floor CLI covers
bootstrap, validation, migration, and human recovery, but it deliberately has no
planning or lifecycle verbs — those live only on the MCP surface (ADR 0007).

## Which server runs when two registrations share the name

A user-level registration (the global tool, for example `~/.local/bin/tractable-mcp`)
and a project `.mcp.json` registration (for example a project venv pinned by
`uv run --locked`) can both be named `tractable`. Which one a session runs
depends on the client. Each row says whether the answer was observed or comes
from the client's documentation.

| Client | Runs | Basis |
| --- | --- | --- |
| omp | the project `.mcp.json` server | observed |
| Claude Code | the project `.mcp.json` server, once approved; otherwise not that one | documented |
| Codex, Gemini CLI, opencode | their own user-level registration, the global tool | documented |

- **omp (observed).** omp's MCP doc, section "Discovery and precedence", loads
  providers in descending priority: OMP native config, OMP extension packages,
  Claude Code, Agent Plugins, Claude marketplace plugins and Codex, Gemini CLI,
  OpenCode, and so on; across providers the first definition wins and duplicate
  names are not merged. Claude Code, which supplies the project's Claude Code
  MCP config, ranks above Codex, Gemini CLI and OpenCode. On 2026-11-12, `ps`
  showed 11 omp sessions started in the BenchBox clone running the project
  venv's `tractable-mcp` (`_project/scripts/.venv/bin/tractable-mcp`), not the
  global tool, although Codex and Gemini user-level `tractable` entries exist.
  The same doc lists root `.mcp.json` only as the lowest-priority portable
  fallback and names `.claude/.mcp.json` as the Claude Code project file, so the
  doc alone would not settle this case; the `ps` observation does. Re-check with
  `ps` after an omp upgrade.
- **Claude Code (documented).** Claude Code's MCP doc
  (<https://code.claude.com/docs/en/mcp>, "Scope hierarchy and precedence")
  connects a same-named server once, from the highest-precedence definition and
  without merging fields across scopes: local scope, then project scope
  (`.mcp.json`), then user scope (`~/.claude.json`). A project `.mcp.json` server
  must also be approved (`enabledMcpjsonServers`, or the approval prompt; see
  "Project server approvals and workspace trust"). An unapproved server shows
  `Pending approval` and is not connected.
- **Codex, Gemini CLI and opencode (documented).** None of them reads
  `.mcp.json`. Codex reads `[mcp_servers.*]` in `~/.codex/config.toml`, Gemini
  CLI `mcpServers` in `~/.gemini/settings.json`, and opencode the `mcp` map in
  `opencode.json` (`~/.config/opencode/` or the project root). In a project with
  only a `.mcp.json` registration they therefore run their user-level
  `tractable`, the global tool, which may be a different version from the
  project's.

### Confirming what a session runs

- `serverInfo.version` from the MCP `initialize` response is the running
  server's version.
- `capabilities.experimental.tractable_batch.tool_version` carries the same
  version in the experimental capability.
- The process command line (`ps -axo command | grep tractable-mcp`) shows which
  binary was launched: a project venv path or the global tool.

`expect_store` on `get_instructions` confirms the **store** the server is bound
to, not its version: two different `tractable-mcp` versions bound to the same
remote both pass it. Check the version separately, as above.

## Verifying a registration

Confirm the state branch resolves, using the floor CLI:

```sh
tractable validate
```

Then drive the workflow through the client:

1. `list_items(ready_only=true)` — find claimable work.
2. `take` — claim a task; keep the returned `generation`.
3. `show_item` — read needs, readiness, and sections.
4. `renew` — extend a long-running claim.
5. `finish` — close the task.

## The `todo` skill

The canonical `todo` skill in `skill-sync-skills` teaches the workflow above,
mirrored into `.claude/skills/`, `.codex/skills/`, and `.gemini/skills/`. See
[`skill-deployment.md`](skill-deployment.md) for how those mirrors are
generated and verified.

## State remotes

The state branch shares its repository's access and visibility. Never publish
credentials to it and never assume a separate branch is private. A long-lived
stdio server holds no connection: every operation fetches the accepted tip, so
an unreachable remote surfaces as `E_OFFLINE` and mutations fail rather than
succeeding locally.
