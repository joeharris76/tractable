# tractable

JSON/Git TODO tracking for coding agents.

Renamed from `todo-db` at 1.0.0; see [ADR 0013](docs/adr/0013-rename-to-tractable.md).

`tractable` keeps a project's work items as canonical JSON (`index.json`
plus `items/<id>.json`) published on a dedicated Git state branch, and
exposes them to coding agents through a small MCP server. Claims are
cooperative and one-winner, so several agents can work the same tracker
without racing. Git history supplies ordinary change history.

- **Agents** drive the tracker through the MCP server, `tractable-mcp`.
- **People and CI** use the `tractable` CLI for bootstrap, validation,
  migration, and recovery. It deliberately has no planning verbs (see
  [ADR 0007](docs/adr/0007-json-git-tracker.md)).

Version 0.7.0 replaces the SQLite/Turso runtime. The old
database-backed product is gone, not carried behind a second adapter:
see [Migration from 0.6.x](#migration-from-06x) and the
[Changelog](CHANGELOG.md).

## Requirements

- Python 3.10 or newer.
- [uv](https://docs.astral.sh/uv/) (or `pipx`/`pip`) to install.
- **git** 2.28 or newer — task state is published through Git; the `git`
  CLI must be installed and the state remote reachable.

## Install

For global tools, use the package index **after the selected release has passed
the [distribution gate](docs/operations/release-gates.md#after-the-tag)**:

```sh
uv tool install --default-index https://pypi.org/simple 'tractable[mcp]>=1.0.0'
uv tool upgrade tractable
tractable --version
tractable-mcp --version
```

To replace an existing GitHub-wheel or local-checkout installation, use the
same install command with `--force`. Keep a checksum-verified wheel compatible
with the active state schema for rollback before replacing a working install.
Restart MCP servers after upgrading; running processes keep their old version.
An index requirement lets `uv tool upgrade` discover later releases. A direct
wheel URL is an immutable pin: upgrading it does not discover GitHub releases.

Rolling back from 1.0 to a 0.x release means switching the project back to the
still-installed `todo-db` tool (its MCP registration and `.todo-db/` config), not
reinstalling a pre-1.0 wheel through the tractable URL template: pre-1.0 assets are
named `todo_db-<version>-py3-none-any.whl` and read only `.todo-db/`.

Until PyPI publication is verified, or when an immutable pin is wanted, use a
wheel from [GitHub Releases](https://github.com/joeharris76/tractable/releases).
Choose a release compatible with the state schema and copy its wheel digest
from `SHA256SUMS`:

```sh
VERSION=1.0.0
SHA256='<wheel digest from the selected release SHA256SUMS>'
uv tool install --force \
  "tractable[mcp] @ https://github.com/joeharris76/tractable/releases/download/v$VERSION/tractable-$VERSION-py3-none-any.whl#sha256=$SHA256"
```

From source:

```sh
git clone https://github.com/joeharris76/tractable.git
cd tractable && uv sync --extra mcp
```

Optional extras:

| Extra | Adds | Needed for |
| --- | --- | --- |
| `mcp` | `mcp` SDK | the agent interface (`tractable-mcp`) |

## Quickstart

Create the state branch and point the project at it. A human runs, from
anywhere with Git access to the remote:

```sh
tractable bootstrap --state-remote <git-url-or-path> --write-config
tractable validate
```

`--write-config` writes `.tractable/config.json` (state remote + branch)
in the repo root. **Commit this**; it is the point of the scaffold.
Bootstrap refuses to overwrite an existing state branch.

That registration is what Claude Code and Cursor read via `.mcp.json`.
Codex, Zed, Windsurf, and Continue keep MCP config elsewhere; snippets
for each are in
[`docs/operations/mcp-clients.md`](docs/operations/mcp-clients.md).

Your agent now has the tracker. It creates work with `create_item`, then runs
the loop below. The canonical `todo` skill from the `skill-sync-skills` catalog
is mirrored into `.claude/`, `.codex/`, and `.gemini/` to teach the workflow.

## Concepts

State schema 3 adds the registered prepared-batch contract and remains able to
read schema 1 and schema 2 snapshots. A schema-1/2 snapshot is upgraded lazily
to schema 3 only when `register_batch` writes it; ordinary reads remain
read-only. That upgrade is one-way for older binaries: before setting
`confirm_schema3_cutover=true`, stop or upgrade every process that can access
the state branch. There is no runtime capability negotiation, and schema-1
clients cannot safely coexist after cutover. Old prepared-receipt records fail
closed until rewritten through the registered schema-3 lifecycle. Ordinary
dependencies remain done-only.

| Term | Meaning |
| --- | --- |
| **Item** | One unit of tracked work: an id, title, priority, status, claim, and optional `needs` IDs in the index; description and context in its detail file. |
| **Status** | `open`, `active`, `blocked`, `done`, or `dropped`. Only `active` holds a live claim; `done`/`dropped` carry none. |
| **Claim** | A cooperative hold: worker identity, expiry, and a unique generation. Ownership plus generation checks protect renew, release, and finish from stale writers. |
| **Generation** | The token proving you hold the claim; returned by `take`, required by `renew`/`finish`/`release`. |
| **Drop reason** | `drop` requires a `reason` (non-empty, at most 2000 characters, newlines and tabs allowed). It is stored as `drop_reason` in the item's detail file in the same commit that sets `dropped`, and `show_item` returns it. Items dropped before 0.12.0 have none. |
| **Deferral** | Deferred work recorded on a task (`defer`); resolved by promoting it into a linked item (`promote_deferral`) or dropping it with a reason (`dismiss_deferral`). `finish` refuses while open deferrals exist. |
| **Prepared receipt** | Detail-owned evidence binding a member to a batch, owner generation, clean exact source worktree/revision, and a passed bounded suite. It can unlock only an explicit same-batch implementation edge; it is not completion. |
| **State branch** | The authoritative task store (`todo-state` by default). One commit per operation; fast-forward pushes only. |

## The agent loop

```
list_items  ──▶  take  ──▶  show_item  ──▶  renew ×N  ──▶  finish
                              └──▶  prepare (release claim; preserve receipt)
                                            │
                                            └──▶  release   (hand the claim back)
```

`take` returns enough context to begin work, so no immediate second read
is prescribed. Every tool returns one of:

```json
{"ok": true,  "data": {...}}
{"ok": false, "code": "E_...", "error": "...", "recovery": [...], "kind": "gate|error"}
```

`kind: "gate"` is an expected result to act on (a conflict, a stale
claim, nothing ready). `kind: "error"` is an environment or protocol failure.
Responses are capped at 16 KiB at their final serialization; lists page
with cursors scoped to a state revision, and oversized fields spill to
section reads.

One server instance is one logical worker. Without an explicit actor, each
server gets an isolated fallback identity even when several clients report the
same product name. Use a stable, unique `--actor` for a worker that must resume
across ordinary restarts; do not run that actor concurrently in two servers.

## Floor CLI

| Area | Commands |
| --- | --- |
| Bootstrap | `bootstrap [--write-config]` |
| Validation | `validate` |
| Migration | `migrate --from-export <file> [--dry-run]` |
| Recovery | `recover --op-id <id>` / `--restore-rev <sha>` / `--limit N` |
| Cache | `cache prune [--older-than DAYS] [--dry-run]` |
| Reads | `list`, `show`, `batch show` |

Planning and lifecycle mutation are MCP tools, not CLI commands.

Exit codes are a contract:

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | generic error (unreachable remote, rejected mutation, bad input) |

## Configuration

Every invocation resolves the state branch the same way; the first source
that resolves wins:

1. explicit flags (`--state-remote`, `--state-branch`, `--cache-dir`)
2. environment variables
3. the discovered `.tractable/config.json` (walk up from the working
   directory, like git)

| Variable | Purpose |
| --- | --- |
| `TRACTABLE_STATE_REMOTE` | State remote (Git URL or path). |
| `TRACTABLE_STATE_BRANCH` | State branch (default `todo-state`). |
| `TRACTABLE_CACHE_DIR` | Local state cache: one Git object store per state branch (default `~/.cache/tractable-state`). |
| `TRACTABLE_CONFIG` | Path to a config file; overrides discovery. |
| `TRACTABLE_ACTOR` | Worker identity for CLI mutations and the MCP default. |

There is no default remote: resolving without one from some source is a
hard error. The state branch shares its repository's access and
visibility — never publish credentials to it and never assume a separate
branch is private.

The MCP server resolves its store once, at launch, from the working directory
it was started in unless flags or environment variables decide it, so a
server started in the wrong directory writes to the wrong tracker. Its
instructions (the `initialize` handshake, `get_instructions`, the
`tractable://instructions` resource and the `tractable/workflow` prompt) therefore
begin with a "Bound store" section: the state remote without credentials,
the branch, how the target was resolved, and the worker identity. Pass the
project's `owner/repo` (or bare repository name) as `expect_store` to
`get_instructions` on the first call; a store whose repository is not exactly
that (compared by whole path segment, ignoring case and `.git`) is refused with a `gate`-kind
`E_STATE` error and nothing is written.

## Data safety

- The remote state branch is authoritative. Mutations fetch the accepted
  tip, apply one logical operation, validate, commit with the tip as
  parent, and fast-forward push — reporting success only after remote
  acceptance is confirmed.
- Competing pushes re-apply the operation against the new tip. Conflicting
  edits fail instead of silently overwriting.
- Every mutation carries a unique operation ID (a `Todo-Op-Id` commit
  trailer). A lost reply reconciles by operation ID without duplicating
  the change; an undetermined outcome is reported as unknown/retryable.
- Offline reads may serve a cached revision marked stale. Offline
  mutations fail rather than succeeding locally.
- `tractable-mcp` starts offline from a cached revision when the state remote
  is unreachable (probing it for at most 10 seconds) and logs a warning;
  `show_item`, `list_items` and `list_deferrals` then report `stale` with
  `cached_rev`. With no cached revision for the state branch it fails at
  startup with the unreachable error.
- The local cache is a Git object store per state branch, fetched
  incrementally and repacked in the background, so it grows by about the
  packed size of each commit rather than a full snapshot per revision
  ([ADR 0011](docs/adr/0011-git-object-store-cache.md)). After upgrading
  from 0.9.x, run `tractable cache prune` once to remove the old snapshot
  folders.
- Bootstrap refuses to overwrite an existing branch. History stays
  append-only; restores land as new commits.

## Migration from 0.6.x

Deliberate breaking change: the database runtime, hosted backends, audit
chain, workflow gates (work units, scope, verification ladders, lint,
findings), and the 26-tool surface are removed. Export first with the old
release, then migrate:

```sh
# With todo-db 0.6.x still installed:
todo-db export --output legacy-export.json   # old command
# With 0.7.0, onto a freshly bootstrapped branch:
tractable migrate --from-export legacy-export.json --dry-run
tractable migrate --from-export legacy-export.json --backup-dir ./migration-backup
```

Migration preserves IDs, priorities, statuses, dependencies,
descriptions, and evidence; unmapped fields land in per-item `legacy`
detail plus the preserved input envelope — nothing is silently dropped.
Live claims are never carried: cut over only with no active claims, or
the command refuses with the holders listed. Rollback before adoption is
deleting the new branch and re-bootstrapping; after adoption it is an
append-only `recover --restore-rev`. Production migration is out of
scope for the tooling change itself: exercise disposable fixtures and
leave live databases untouched.

## Measurements

Token and footprint targets are recorded in
[`docs/measurements.md`](docs/measurements.md), measured with
`o200k_base` on actual serialized surfaces: about 300 tokens for five
list rows, 1,500 for one task context, 100 for a mutation
acknowledgement, with a 16 KiB hard byte ceiling on every response.

## Documentation

- [Decision records](docs/adr/README.md) — why the system is shaped this way.
- [MCP client registration](docs/operations/mcp-clients.md)
- [Release gates](docs/operations/release-gates.md)
- [Skill deployment](docs/operations/skill-deployment.md)
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) ·
  [Changelog](CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).
