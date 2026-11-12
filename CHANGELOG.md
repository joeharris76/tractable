# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `update_item` accepts `clear_not_before=true` to clear a hold, for clients that
  drop empty-string arguments before they reach the server. `not_before=""`
  still clears it.

### Fixed

- `get_instructions(expect_store=...)` compares whole repository path segments
  instead of a substring, so `benchbox` no longer accepts a store for
  `benchbox-site` or `benchbox-sqlglot`. It accepts `owner/repo`, a bare
  repository name, or a full URL, each with or without `.git`, ignoring case.

## [1.1.0] - 2026-11-07

### Added

- `prepare` accepts `verification_path` and `finish` accepts
  `final_evidence_path`, absolute paths to JSON files, so large mapped-test
  payloads need not be pasted inline.

### Changed

- A worker holds one live claim outside any batch and one per active batch, so
  leaders of different batches on one server no longer block each other with
  `E_MULTIPLE_CLAIMS`. Agents working the same batch still share its slot.
- `prepare` refuses a receipt whose `verification.review.outcome` is not
  `clear`, `not-reviewed`, or `override`, or whose `verification.command` is
  not a non-empty argv list of single-line strings starting with an executable
  name. `verification` is now optional in the tool schema when
  `verification_path` is given.

### Fixed

- `update_item` accepts `context` and `context_append` on members of a batch
  that is no longer active, including done members with receipts; other edits
  are still refused.

## [1.0.0] - 2026-11-06

### Changed

- Release distribution now requires matching build, GitHub, and PyPI artifacts
  plus a fresh index-installed CLI/MCP smoke check. Installation guidance
  distinguishes upgradeable index requirements from immutable wheel pins;
  schema cutovers require an explicit upgraded-or-stopped client inventory.
- Renamed the product from `todo-db` to `tractable` and set the version to
  1.0.0 ([ADR 0013](docs/adr/0013-rename-to-tractable.md)). The distribution
  and import package are `tractable`; the commands are `tractable` and
  `tractable-mcp`; the MCP server name is `tractable`; the batch capability
  key is `tractable_batch`; the instructions resource is
  `tractable://instructions` and the prompt is `tractable/workflow`; project
  config lives in `.tractable/config.json`; environment variables are
  `TRACTABLE_*`; the cache root is `~/.cache/tractable-state`; new state
  commits are authored by `tractable <tractable@localhost>`. The old names are
  not read as fallbacks. State schema 4, the `todo-state` branch default, the
  `Todo-*` commit trailers, the `todo(op): summary` subject and every MCP tool
  name and input schema are unchanged.

### Upgrade from todo-db

- Install `tractable[mcp]`. The `todo-db` tool is a separate distribution and
  stays installed until you remove it.
- Rename `.todo-db/` to `.tractable/` in each project.
- Rename `TODO_DB_*` environment variables to `TRACTABLE_*`.
- Change the MCP registration server name and command to `tractable` and
  `tractable-mcp`.
- Skills that gate on the batch capability use `tractable_batch`.
- The cache root is new (`~/.cache/tractable-state`), so the first offline
  start needs one online read to fill it.
- State, branch and commit trailers are unchanged, so no state migration is
  needed, and `todo-db` 0.14.0/0.15.0 and `tractable` can write the same
  branch.
- Restart running MCP servers: they keep the version they started with.
- Remove the `todo-db` tool and delete `~/.cache/todo-db-state` only after no MCP
  registration names `todo-db` and no `todo-db-mcp` process is running.

## [0.15.0] - 2026-11-02

### Added

- Additive `todo_db_batch.features.integration_worktree_relocation = true`
  capability advertised in `initialize` and `show_batch`.
- `recover_batch_owner`: optional `expected_integration_worktree`,
  `expected_integration_head`, and `integration_worktree` relocation tuple,
  validated atomically with owner CAS and audited on the batch.
- `todo-db show`: `--rev` option passed to `show_item` for revision-pinned
  paged section reads.

### Changed

- `.github/workflows/publish.yml`: GitHub release assets publish on `v*` tags,
  while PyPI publication requires an explicit affirmative `workflow_dispatch` opt-in.
- `skill-sync.conf`: advance `skill-sync-skills` pin to `6b47649eaff3ee66bca25c62c954134daf405f34`.
## [0.14.0] - 2026-11-02

Batch v2 (ADR 0012): the integration-branch delivery loop. Receipts move to
`prepared_work_v2` and the store to schema 4 through one cutover; see the
upgrade note below before registering a batch.

### Added

- `show_batch` tool and `todo-db batch show BATCH_ID` read-only CLI command:
  registry, receipts, integration log, acceptance requirements, recoveries and
  final-PR history, with paged sections under the 16 KiB cap. The owner
  generation is never served.
- `advance_batch_head` tool: owner-only, compare-and-swap recording of a leader
  `fix` or a `develop-merge` as the new integration head, with the changed files
  verified against the real integration checkout.
- `recover_batch_owner` tool: a new session takes over a batch from a dead
  leader by naming the exact owner and generation, supplying its own new
  generation, and giving a reason. Audited on the batch and on every member;
  an exact retry is idempotent.
- `develop_ref` on `register_batch`: the remote-tracking ref (under
  `refs/remotes/`) that develop merges are verified against, fixed at
  registration.
- `initialize` advertises `capabilities.experimental.todo_db_batch` (version 2,
  receipt schema, store schema, supported schema versions, tools), also exposed
  as `todo_db.BATCH_CAPABILITY`.
- Error codes `E_BATCH_OWNER` and `E_BATCH_HEAD`.

### Changed

- **Breaking, store cutover:** the first `register_batch` with
  `confirm_schema3_cutover=true` moves a schema 1, 2 or 3 store directly to
  schema 4 in one commit. It is refused while a schema-3 batch is active and
  while another worker holds a live claim. Older clients refuse a schema-4 store
  with `E_SCHEMA`. New state branches still bootstrap at schema 3. Item `batch`
  metadata can no longer raise the schema on its own; below schema 4 it is
  refused with `E_SCHEMA`.
- **Breaking, receipts:** `prepare` writes `prepared_work_v2` receipts (adding
  `owner_epoch`, `integration_seq` and `overlaps`, and dropping
  `owner_generation`). `prepared_work_v1` receipts are never reinterpreted under
  v2 rules and stay valid only on legacy batches.
- Members of an active batch are reserved to the batch owner: `take`,
  `takeover`, `prepare` and `finish` by anyone else are refused with
  `E_BATCH_OWNER`. `takeover` no longer transfers batch ownership and its
  acknowledgement drops `transferred_batches`; use `recover_batch_owner`.
  `register_batch` refuses members claimed by another worker.
- `prepare` accepts a `source_base` equal to the batch start head or any
  recorded integration head, measures changed files from it without rename
  detection, and records overlap with earlier members and with fixes dispatched
  before them. Prepared heads must be new and must not carry unrecorded leader
  commits. Receipts keep the integration head they were integrated at. Byte
  equality of earlier members' files is no longer required.
- Final evidence for a v2 batch must carry `current_acceptance` for every member
  whose files were overlapped or touched by a fix, and `final_pr.repository`.
  Develop-merge paths are excluded from the scope union unless a member or fix
  also changed them.
- **Breaking tool input:** `bind_batch_pr` takes `pr_repository` and an optional
  `expected_previous_head`; the binding can move to a new head with the guard,
  refuses a different PR, and an exact retry is idempotent.
- `update_item` keeps a prepared member's receipt and its dependents for
  `context`/`context_append`-only edits; every other edit still invalidates
  transitively. Invalidating edits are refused once any member of the batch is
  done, and a member without a receipt can be prepared again (`reprepare`).
- Idempotent retries of `advance_batch_head`, `bind_batch_pr` and
  `recover_batch_owner` succeed without writing a commit.

### Fixed

- Batch v2 acceptance accounting now remembers every integrated member's changed
  files, so a member re-prepared after an invalidating edit still requires its own
  acceptance rerun when a later member overlapped it (no silent revert).
- `show_batch` always returns a default response within the 16 KiB cap (sections
  that do not fit are paged), serves no `owner_generation` from old receipts, and
  rejects a moved `rev` on the summary read with `E_CURSOR_STALE`.
- Finishing a member of an active pre-v2 batch is refused with `E_SCHEMA` even
  without final evidence, and every new batch refusal carries recovery steps.
- A completed prepared member refuses every edit, including `context`-only ones.

## [0.13.0] - 2026-11-01

### Added

- `todo-db --version` and `todo-db-mcp --version` print the installed
  version and exit 0 without resolving a config or state remote, touching
  the network, or (for `todo-db`) requiring the `mcp` extra.

### Fixed

- `todo-db-mcp` no longer refuses to start when the state remote is
  unreachable and a cached revision exists for the state branch. It probes
  the remote for at most 10 seconds, logs a warning naming the cached
  revision, and serves reads from it; mutations still fail with `E_OFFLINE`
  until the remote is back. With no cache it still fails at startup, and a
  reachable remote that lacks the state branch still reports the bootstrap
  hint. `show_item`, including section reads, now reports `stale` and
  `cached_rev` instead of `rev` when served from the cache, as `list_items`
  already did.

## [0.12.0] - 2026-10-31

### Changed

- **Breaking tool input:** the `drop` tool and `TrackerService.drop` now
  require a `reason`. It must be non-empty after stripping, at most 2000
  characters, and may contain newlines and tabs; other control characters and
  U+0085/U+2028/U+2029 are refused with `E_STATE`, and a refused drop leaves
  the item unchanged. The reason is stored as `drop_reason` in the item detail
  in the same state commit that sets the status to `dropped`, so a drop no
  longer needs a separate `update_item` note. `show_item` (and
  `todo-db show`) return `drop_reason` for dropped items, including in the
  slim response used when the byte cap is exceeded; `list_items` rows are
  unchanged. The index schema version stays 3: items dropped earlier stay
  valid without a reason, older clients load and keep the key, and
  `validate_snapshot` rejects a `drop_reason` that is not a non-empty string.

## [0.11.0] - 2026-10-28

### Added

- The MCP server reports the tracker it is bound to. The `initialize`
  instructions, `get_instructions`, the `todo://instructions` resource and
  the `todo/workflow` prompt start with a "Bound store" section naming the
  state remote without credentials, the branch, how the target was resolved
  and the worker identity. `get_instructions` takes an optional
  `expect_store`; a store whose remote does not contain it, ignoring case, is
  refused with a `gate`-kind `E_STATE` error, so a server started in the wrong
  directory is caught before the first write. The branch name is not
  searched, since every store's default branch is `todo-state`.
- `update_item` accepts `context_append`. The text is added to the end of the
  current context at the moment the write lands, separated by a blank line,
  so a progress note no longer needs the existing text resent and a
  concurrent note is not lost. It cannot be combined with `context` or be
  empty.

### Changed

- An `E_MULTIPLE_CLAIMS` refusal now carries `recovery` steps: finish or
  release the claim it names, and, because every agent on one
  `todo-db-mcp` server shares one worker identity, run each concurrent
  agent on its own server with a distinct `--actor`. `TodoError` and
  `MutationOutcome` carry an optional `recovery` list so other gates can name
  next steps the same way.
- The `update_item` and `defer` tool descriptions state what the tools do
  with their fields: text and list fields are replaced wholesale, an empty
  string or list clears one, `not_before=""` clears a hold, a `defer`
  summary is one line of at most 200 characters, and done or dropped tasks
  refuse deferrals.

### Fixed

- A local git command that times out no longer escapes as a raw
  `subprocess.TimeoutExpired`. Reads and writes return an `E_STATE` error
  that names the command and says nothing was changed; a write that times out
  after its push started returns `E_UNKNOWN` with the operation ID to
  reconcile, and the write stands if the push landed. A timeout does not
  rebuild the local object store. Fetch, push and `ls-remote` timeouts keep
  their existing `E_OFFLINE` and `E_UNKNOWN` handling.

### Security

- A state remote URL that embeds a user or token is shown without them in the
  server's startup log, in unreachable-remote and missing-branch errors, and in
  `todo-db bootstrap` output.

## [0.10.1] - 2026-10-18

### Fixed

- Git subprocesses get a null stdin. Under the MCP server, stdin is the
  JSON-RPC stream, and a git command that read from it could consume
  requests.
- Cache maintenance no longer counts pack indexes as slack. A large
  store's indexes alone could exceed the cap and trigger a repack on every
  operation.
- `recover --restore-rev` and historical reads accept only full commit
  IDs, so a revision starting with `-` can never reach git as an option.
- `cache prune` leaves a top-level trash folder younger than an hour, which
  may belong to a prune still deleting it. `--dry-run` reports namespaces
  in use, without creating lock files.

## [0.10.0] - 2026-10-18

### Changed

- The local state cache is now one bare Git object store per state
  branch (`<cache>/<namespace>/store.git`) instead of a full snapshot
  folder per revision. Reads fetch only new objects and build snapshots
  straight from Git objects; mutations, history, reconcile and restore
  use the same store and no longer fetch the branch's whole history into
  a temporary repository on every call. Against BenchBox production
  state each new revision used to add about 30 MiB to the cache, and the
  cache reached 41 GB on one machine. The whole branch history now fits
  in about 15 MiB. Steady-state growth is about the packed size of each
  commit: 0.72 times the bytes touched for one-item edits on BenchBox
  state, plus a fixed record of 0.5 to 1 KiB per commit, which dominates
  for tiny edits. A read after a new tip takes 0.37 s instead of 1.7 s. See
  [ADR 0011](docs/adr/0011-git-object-store-cache.md).
- Git commands no longer inherit variables that redirect them to another
  repository, index or identity (`GIT_DIR`, `GIT_INDEX_FILE`,
  `GIT_AUTHOR_*`, `GIT_CONFIG_PARAMETERS` and similar). Hooks, signing and
  automatic gc stay off even when global or inherited command-level config
  turns them on.
- Requires git 2.28 or newer (already needed by `bootstrap`); an older git
  now fails with a clear error.

### Added

- `todo-db cache prune [--older-than DAYS] [--dry-run]` removes legacy
  snapshot folders, folders from before namespacing, and namespaces
  unused for 30 days, then repacks each store. Namespaces in use are
  skipped. Run it once after upgrading, from the project directory (or
  with `--state-remote`) so it can also clear a local remote's legacy
  folder. `validate` prints a hint while a legacy cache remains, and
  reports a failed cache maintenance run.

## [0.9.1] - 2026-10-04

### Fixed

- Writes no longer clone the whole state remote. `create_item`, `take`,
  `finish` and every other mutation, plus `recover`/`history`,
  `reconcile` and restore, fetch only the state branch. Against a remote
  that also carries code history the clone alone could exceed the 60s git
  timeout, so a create failed at random, whatever its `needs`.
- A timeout while confirming a push that already landed, or while
  reconciling it, now returns the `unknown` outcome with its operation ID
  instead of raising, so a retry cannot apply the operation twice.
- `recover`, history and restore report a missing state branch as
  `E_STATE` ("run bootstrap"), and relative local remote paths resolve
  correctly for these operations.
- `list_deferrals` rows carry `reason` always, plus
  `resolved_item`/`resolved_reason` when set, so triage needs no
  second read per task. Summaries share the 200-char item title bound
  so every recorded deferral promotes with its default title.

## [0.9.0] - 2026-09-27

### Added

- First-class deferral verbs. `defer(id, summary, reason)` records
  deferred work on a task without changing its status; `list_deferrals`
  reads the queue (open by default, `all` for every resolution,
  `from_item` to scope one task, stable cursors like `list_items`);
  `promote_deferral(id, deferral_id, ...)` resolves an open deferral
  into a linked planning item (created, or an existing `to_item`);
  `dismiss_deferral(id, deferral_id, reason)` drops one with a recorded
  reason. Deferrals are detail-owned and additive, so no schema bump
  and no migration: older readers load snapshots that carry them.
  Every resolution records a session-history entry on the source task
  (see ADR 0010).
- `E_OPEN_DEFERRALS` finish gate. `finish` refuses while open deferrals
  exist for the task, naming the blocking ids; `show_item` reports them
  as `open_deferrals` and `get_instructions` documents the loop.

## [0.8.1] - 2026-09-25

### Added

- The audited claim takeover announced in 0.8.0 now actually ships in this
  release: the implementation missed the 0.8.0 merge. `takeover(id,
  expected_holder, reason)` lets a session take over a live foreign claim
  whose holder cannot resume (see ADR 0008, amending ADR 0003 §2.2).

### Fixed

- Reads fetch only the state branch tip instead of cloning the whole
  remote, and repeat reads at the same tip serve the cached snapshot.
  Against production-scale state sharing a remote with code history this
  cuts `list_items` from ~110s (past the 30s MCP client timeout) to
  seconds cold and under a second warm.

## [0.8.0] - 2026-09-23

### Added

- Audited claim takeover. `takeover(id, expected_holder, reason)` lets a
  session take over a live foreign claim whose holder cannot resume, bound
  to the inspected holder with compare-and-swap retries, batch ownership
  transfer, and dual-surface audit (see ADR 0008, amending ADR 0003 §2.2).
- Per-task session history. Every create, claim, renewal, release,
  preparation, finish, drop, and update records its actor, session,
  operation, and operation ID in an append-only log on the touched
  task, readable through `show_item`'s `sessions` summary and paged
  `field="sessions"` section reads. Batch aborts and evidence
  invalidations record `abort`/`invalidate` entries on the rewritten
  members. State commits now also carry a
  `Todo-Session` trailer next to `Todo-Actor`, surfaced by history
  and recovery output.
- Time-held readiness. `create_item` and `update_item` accept `not_before`,
  a future RFC 3339 time, which keeps an open task out of the ready queue
  until then. Rows and `show_item` show `waiting_until` while the hold
  applies, `take` and `finish` refuse a held task with `E_NOTHING_READY`, and
  `not_before=""` clears it. Older clients load and keep the field but do
  not enforce it; upgrade them before relying on holds.

### Fixed

- Independent MCP servers that report the same client product name now receive
  separate fallback worker identities. A same-name process can no longer block,
  re-adopt, renew, finish, or release another process's claim by accident.
- Explicit actors retain stable restart behavior. Default workers can be
  deliberately restored with `--session`; legacy fallback claims require an
  explicit actor recovery or lease expiry after upgrade.

## [0.7.3] - 2026-09-12

### Fixed

- The MCP initialization handshake now derives its reported version from the
  installed package metadata instead of retaining the original 0.7.0 value;
  publication now checks the built wheel for the same invariant.
- Installation guidance and bug-report prompts now name the current release,
  and the README identifies the catalog-owned `todo` skill correctly.

### Removed

- **Retired repository-owned `skills/todo-db`.** Tracker guidance is consolidated
  into the canonical `todo` skill (v3.0.0) owned by the `skill-sync-skills`
  catalog and pinned in `skill-sync.conf`.

## [0.7.2] - 2026-09-11

### Fixed

- Legacy item IDs up to 128 characters now migrate without being renamed or
  dropped.

## [0.7.1] - 2026-09-10

### Fixed

- **Migration no longer aborts on a forward-referenced dependency.**
  `migrate --from-export` created items in export order and rejected any
  item whose `needs` named an item listed later, failing the whole
  migration with `task '<a>' needs unknown task '<b>'`. Items are now
  created first and dependencies wired afterward; `validate` still rejects
  dangling dependencies and cycles.

### Changed

- The `todo-db` agent skill documents the export-to-state-branch
  migration: bootstrap first, dry run and read the mapping report, real
  run with the required `--backup-dir`, then validate. Records that
  migration refuses a non-empty branch or a live claim.

## [0.7.0] - 2026-09-10

### Removed

- **The SQLite/Turso runtime is replaced by JSON/Git state (breaking).**
  Version 0.7.0 removes the database layer, hosted backends and credential
  machinery, the audit chain, findings, workflow gates (work units, scope
  rules, verification ladders, lint, attestation), the seven SQL
  migrations, the 26-tool MCP surface, and the hosted/parity CI harnesses.
  The normal runtime is `index.json` plus `items/<id>.json` on a dedicated
  state branch, published one commit per operation with fast-forward
  pushes, operation IDs, and bounded semantic retries. The MCP surface is
  nine tools (`list/show/create/update/take/release/finish/renew/drop`) plus
  `get_instructions`; the CLI keeps bootstrap, validation, migration,
  recovery, and read-only list/show. Migration from a 0.6.x lossless
  export is non-destructive (dry run, legacy archive, live-claim refusal,
  documented rollback); production migration is out of scope. See ADR 0007
  and `docs/measurements.md` for the design and the measured budgets.

### Added

- **The repo-owned `todo-db` agent skill documents the JSON/Git workflow**,
  sourced from `skills/todo-db/` and mirrored into `.claude/skills/`,
  `.codex/skills/`, and `.gemini/skills/` so a clone is self-contained.

### Changed

- The sdist ships `docs/` and `skills/`.

## [0.6.1] - 2026-09-03

### Added

- **`doctor` MCP tool.** A read-only tracker preflight check (database target,
  schema version, and resolved identity) is now registered in every MCP profile,
  including the default agent profile. It mirrors the `todo-db doctor` floor CLI
  for agents that only have the MCP surface.

### Changed

- **Deferral tools promoted to the agent profile.** `defer`, `promote_deferral`,
  and `dismiss_deferral` moved from `--profile full` to the default agent
  profile, so agents can manage deferrals without the full profile. The
  agent-profile tool count rises from 19 to 23 and the full-profile count from
  37 to 38 (`scripts/mcp_snapshots/tools.json`, `tools_full.json`).
- **`E_VERIFY_GATE` recovery is actionable.** When `finish` hits the verify
  gate, the error now returns the exact `todo-db --actor <principal> verify-run
  <item> --claim-token <token>` invocation in its `recovery` list.

## [0.6.0] - 2026-09-02

### Removed

- **BREAKING — `agent` CLI, Pi adapter, and wrapper removed.** The `agent` CLI subcommand group (`todo agent …`, `todo agent instructions`), the Pi adapter package (`@todo-db/pi-adapter` / `integrations/pi/`), and the `_project/scripts/todo` wrapper script (`DEFAULT_WRAPPER_RELATIVE`, `refresh-wrapper`, `init-project --wrapper`, `TODO_DB_PI_PRINCIPAL`, and wrapper key handling) are removed. MCP `todo-db-mcp` (`todo-db[mcp]`) is the sole agent interface (ADR 0006, `docs/design/mcp-interface-migration.md`).
- **BREAKING — Per-verb planning and mutation CLI surface removed.** The subcommands `create`, `update`, `list`, `show`, `ready`, `stats`, `deps`, `start`, `done`, `defer`, `promote`, `dismiss`, `block`, `unblock`, `release`, `claim`, `check-scope`, `verify`, and `lint`, as well as the `finding` CLI subcommands except `sync` (`create`, `list`, `show`, `candidates`, `dismiss`, `triage`, `link`, `promote`), are removed. Planning and workflow coordination move exclusively to the MCP server.

### Added

- **Minimal floor CLI.** A minimal `todo-db` CLI remains for bootstrap, CI, audit/export, `finding sync`, and human verification/rebaseline: `init`, `init-project`, `migrate`, `doctor`, `audit verify`, `export`, `restore`, `restore-legacy`, `import-yaml`, `finding sync`, `config`, `sweep-stale`, `complete`, `verify-run` (attest-only human verification ladder execution with `--claim-token` and `--actor`), and `rebaseline` (audited baseline update with `--claim-token` and `--actor`).

### Changed

- **`next_action` drops `command`.** `next_action` objects returned by `AgentWorkflow` and MCP tools drop the legacy `command` string property and are strictly machine-readable (`tool` + `arguments`).
- **Release gate 2.** Rewritten in `docs/operations/release-gates.md` against surviving floor verbs (`todo-db audit verify`, `todo-db export`, `todo-db doctor`).

## [0.5.0] - 2026-09-02 (same-day, never tagged; superseded by 0.6.0 below)

### Added

- **MCP server (experimental, `todo-db[mcp]`).** The Model Context Protocol server `todo-db-mcp` is now the sole agent interface (ADR 0006, `docs/design/mcp-interface-migration.md`). It is a thin transport over `AgentWorkflow` with a connection-per-call, capability-scoped credential model (amends ADR 0005 G1), all DB and git work on one dedicated worker thread, and a mandatory explicit actor (`--actor` / `TODO_DB_ACTOR` / `mcp:<clientInfo.name>:<user>@<host>`; never `default_actor()`). Six hot-path tools plus read-only queries are always loaded; `--profile full` adds planning, findings, and admin. `next_action` is now machine-readable (`tool` + `arguments`, dual-emitted with `command` for 0.5.0). `E_SCHEMA`, `E_IDENTITY`, `E_AUDIT`, `E_OUTPUT_TRUNCATED`, `E_HOSTED` join the error taxonomy; `E_NOTHING_READY` is now wired on empty take. `finish` retains the claim on `E_LINT_GATE` (ADR 0006 G7, fixes ADR 0003 §2.5 vs code). `todo://instructions` resource, `get_instructions` tool, and `todo/workflow` prompt replace `todo agent instructions`. Per-client registration in `docs/operations/mcp-clients.md` (Claude Code, Codex, Cursor, Windsurf, Zed, Continue, Pi; `muse`/`grok` unverified — 0.6.0 gate). Tool schemas frozen in `scripts/mcp_snapshots/tools.json` (19 agent) and `tools_full.json` (37 full); subprocess stdio smoke test and stdout-purity assertion added.

### Deprecated

- The `agent` CLI (`todo agent …`, `todo agent instructions`, `AGENT_*`), the Pi adapter (`integrations/pi/`, `@todo-db/pi-adapter`), and `_project/scripts/todo` are deprecated and will be removed in **0.6.0**. The `agent` CLI and Pi adapter still work in 0.5.0. Migrate to `todo-db-mcp` or the minimal floor CLI (`init`/`migrate`/`doctor`/`audit`/`export`/`finding sync`/`verify-run`/`rebaseline`). The wrapper key in `.todo-db/config.json` is now ignored.

### Changed

- `pyproject.toml` `mcp` optional dependency pinned to `mcp>=1.10.0,<2` (SDK 1.29.1, Python floor `>=3.10` unchanged; no `requires-python` or CI matrix move).

## [0.4.3] - 2026-08-19

### Added

- ADR 0005 records the hosted credential-provider contract: an additive,
  retrieval-only boundary that restores one-time provisioning and zero-
  interaction reuse without amending any ADR 0004 decision.
- `TODO_DB_CREDENTIAL_COMMAND` resolves a hosted credential from an external
  secret store when none is injected, bounded by a timeout and an output-size
  limit, never run through a shell, and never disclosing provider output. A
  non-zero provider exit is an error rather than an absent credential, so a
  failing read-only provider cannot escalate to read-write. With the variable
  unset, resolution, messages, codes, and exit statuses are unchanged.
- The Pi adapter forwards `TODO_DB_CREDENTIAL_COMMAND` through its sanitized
  environment.
- `scripts/hosted_auth_acceptance.sh` proves hosted authentication resolves with
  the injected credentials removed, which is the only check here that can fail
  the way an operator fails. Unconfigured it exits 77; `--require` makes that a
  failure. CI validates its syntax and its skip contract but does not run it
  against a hosted database, because CI's injected credentials would mask the
  condition it tests.

- `docs/operations/release-gates.md` makes a hosted acceptance run and a real
  downstream consumer check mandatory before tagging any release that touches
  credential resolution or the wrapper contract, and states plainly that green
  CI, passing tests, package smoke tests, and verified checksums do not satisfy
  them.

- `TODO_DB_CREDENTIAL_COMMAND` joins the credentials that are always rejected
  from `TODO_DB_VERIFY_ENV_PASSTHROUGH`. It holds no secret itself, but a stored
  verification command that inherited it could run the provider and print the
  token, which would defeat the verification-subprocess credential boundary by
  passing the pointer instead of the secret.

- `docs/adr/README.md` indexes the decision records, which now all live in
  `docs/adr/`. ADR 0001 and 0002 moved from `_project/decisions/`, and that
  directory's superseded copy of ADR 0003 was removed in favour of the current
  text.
- `docs/operations/test-doubles.md` records what each test double cannot catch
  and where the real behaviour is exercised instead. The libSQL and Hrana doubles
  now reject unexpected keyword arguments rather than accepting anything.

### Changed

- A provider-resolved credential reports its capability as
  `requested:read-only` or `requested:read-write`. The provider may ignore the
  request and serve both capabilities from one entry, so the label no longer
  implies a property nothing verified. Environment-variable provenance and the
  scheduled audit's read-only assertion are unchanged.
- The credential provider receives the operator's `argv` unchanged; the
  requested capability reaches it only through `TODO_DB_CREDENTIAL_CAPABILITY`
  in its environment. Appending it as a positional argument broke every
  documented one-line provider.
- Provider output is captured as bytes, bounded before decoding, and decoded
  with replacement, so malformed output degrades to a coded error instead of an
  unhandled `UnicodeDecodeError`.

- Missing- and rejected-credential messages, the exit-4 help text, and the
  generated wrapper's exit-4 notice now name the provisioning and rotation
  procedures in `docs/operations/hosted-credentials.md` and the
  `TODO_DB_CREDENTIAL_COMMAND` variable, instead of telling the reader to
  inject a credential. Refresh a generated wrapper with
  `todo-db refresh-wrapper` to pick up the new notice; the v2 contract is
  unchanged, so an unrefreshed wrapper keeps working.

### Fixed

- The macOS keychain provisioning runbook now uses `-w "$token"` argument syntax
  because macOS `security add-generic-password` does not read token data from stdin.

## [0.4.2] - 2026-08-19

### Fixed

- Canonicalize and confine generated-wrapper paths before calculating repository
  depth, and redact HTTPS/authority variants when libSQL errors translate a
  configured endpoint.
- Unify hosted credential selection and doctor provenance, add coded missing and rejected auth errors, narrow ambiguous failure classification, and negotiate legacy-safe versus v2 authentication exit codes.
- Require the scheduled hosted audit to prove it selected the server-enforced read-only credential before verifying the audit chain.

## [0.4.1] - 2026-08-18

### Added

- Schema migration 007 binds passing verification ladders to a deterministic
  Git workspace fingerprint. Existing databases migrate automatically when
  opened by `todo-db 0.4.1`; take the normal database backup before upgrading.
- Human-only, audited `todo agent rebaseline` remediation and an installable
  Pi adapter README covering trust, paging, verification, and hosted limits.
- Define the hosted credential lifecycle in ADR 0004 and an operations
  runbook: externally injected database-scoped tokens, explicit bounded RO/RW
  profiles, named rotation ownership, and coordinated database-wide compromise
  response.

### Fixed

- Replace generated-wrapper token mint/retry behavior with a single-execution
  v2 auth-contract marker, add a targeted `refresh-wrapper` migration, and
  remove Turso account probes from `doctor`.
- Require Pi project trust before resolving or executing project wrappers,
  compile the adapter against Pi's real extension API, and use its actual
  session identifier.
- Execute human agent verification ladders exactly once, reject commands that
  mutate the Git workspace, require a current attestation for no-shell model
  completion, and prevent tracker/Turso credentials from entering verification
  subprocesses even through the explicit passthrough setting.
- Recheck claim generation during completion and require it for streamlined
  release/rebaseline mutations; audit adoption and reject multiple live claims.
- Replace false hosted certification with a real-primary two-connection Turso
  race whose skipped result is non-passing. The race passed on 2026-08-18;
  commit-outcome fault injection remains uncertified and hosted agent mutation
  support remains experimental.
- Add recoverable bounded context pages, blocker/deferral projections, compact
  actor-scoped claim receipts, exact adapter output caps, environment
  allowlists, and lossless Git filename decoding.
- Constrain the Python source distribution to reviewed project sources and
  packaging metadata so local agent/tooling directories cannot leak into a
  published artifact, and move the Pi build hook to `prepack` so consumers do
  not need TypeScript during installation.

## [0.4.0] - 2026-08-18

### Added

- Claim-coordinated agent workflow service and CLI subcommands under `todo agent` (`next`, `take`, `context`, `progress`, `finish`, `claims`, `adopt`, `release`).
- Schema Migration 006 adding `claimed_at`, `claimed_session`, `claim_token`, `claimed_branch`, `claimed_worktree`, and `git_baseline` columns with concurrent claim index.
- Root-safe, NUL-delimited `GitScopeEngine` verifying working-tree and branch-baseline changes without path truncation or shell escape vulnerabilities.
- Bounded agent projection service with mandatory guardrails, token rotation, and lease management.
- Pi extension adapter package (`@todo-db/pi-adapter`) providing extension tools, serialized mutation queue, and status panel for Pi.
- Batch query optimizations and typed schema error codes (`E_*`).

## [0.3.3] - 2026-08-18

### Added

- `todo-db update` now amends the remaining create-time item fields that are
  not identity or lifecycle state: `--approach` and `--category` (empty
  values clear the stored field), `--add-needs` / `--drop-needs` for
  inter-item dependencies, `--add-work-need` / `--drop-work-need` for
  dependencies on existing work units, and add/drop flags for `preserves`,
  `anti_patterns`, and `prior_art`. Additions of those rows are audited in
  the same chained `update` event as today's metadata/work/verify/scope
  diffs. Drops that loosen a gate or remove a recorded guardrail require
  `--reason`. Duplicate, missing, contradictory, empty, and cyclic values
  abort the transaction without writing a partial event. Item id, state,
  created timestamps, project identity, and done work-unit summaries stay
  immutable.

## [0.3.2] - 2026-08-13

### Added

- `todo-db complete` now runs every configured verification rung before the
  item can become done. Any failing command, including pytest's exit 5 for an
  empty selection, leaves the item active. Commands are graded only by exit
  status; `expected` remains human acceptance text rather than an implicit
  output-substring assertion. Hosted databases retain the
  `TODO_DB_ALLOW_HOSTED_VERIFY_RUN=1` trust boundary. Maintainers can use
  `--override-verification REASON` when a rung cannot run; the actor, reason,
  and overridden sequence numbers are recorded in the hash-chained completion
  event. Items without a verification ladder keep their existing completion
  behavior.
- `todo-db lint` now reports verification commands that invoke pytest through
  a selected uv project whose base dependencies, enabled dependency groups,
  and selected extras do not provide pytest. Explicit `--with pytest` and
  `--with-requirements` injections remain valid. The check is static and
  conservative: it does not execute commands and skips shell or TOML forms it
  cannot parse safely.

## [0.3.1] - 2026-08-10

### Fixed

- `todo-db release` is holder-only: a non-holder cannot release another
  actor's active or stale claim, while the holder can release and an
  unclaimed item remains an idempotent no-op. Non-holder attempts retain the
  CLI's generic error / exit-2 contract.

## [0.3.0] - 2026-07-25

### Added

- `todo-db update <id>`: a safe, fully audited amendment verb for
  create-time-only fields. `--title`/`--description`/`--priority`/
  `--worktree` edit item metadata with `create`-identical validation;
  `--add-work` extends the breakdown (new units start `pending`);
  `--edit-work WID:SUMMARY` rewrites a summary only while the unit is
  pending (a done unit's evidence attaches to its summary, so it is
  immutable); `--add-verify`/`--drop-verify SEQ` amend verification steps.
  `--reason` is required for any edit to a done/dropped item and always for
  `--drop-verify`. Item id, state, created timestamps, and project identity
  stay immutable, and `update` never transitions state. Each call commits
  atomically with one hash-chained `update` event carrying exact from/to
  diffs, and verification amendments log the full command text (they are
  security-relevant history because `verify --run` executes stored
  commands). Calls with no change flags or equal-value edits exit 2 instead
  of logging empty diffs.

- Auth-failure classification for the hosted backend: connect/sync failures
  that are auth-shaped (HTTP 401/403, `unauthorized`/`forbidden`, token/JWT
  complaints in the underlying libsql error, matched conservatively against
  the already-redacted message) raise the new `HostedAuthError` whose message
  names the concrete remediation (refresh `TODO_DB_AUTH_TOKEN` /
  `TODO_DB_RO_AUTH_TOKEN` via `turso db tokens create`, or `turso auth
  login`). The CLI maps `HostedAuthError` to a new exit code 4; exit 2 stays
  the generic fix-the-cause error, and the exit-code contract is documented
  in `--help` and the README. URL/token redaction guarantees are unchanged.
- `todo-db doctor`: a read-only preflight that checks config discovery,
  identity resolution (with source tier; FAIL only when unresolvable and the
  database is unbound), the database target (local file/parent and schema
  version with a `behind -- run init to migrate` warning; hosted URL scheme
  plus a read-only `SELECT` probe against the primary with auth failures
  classified), turso CLI availability and `turso auth whoami` for hosted
  targets (WARN means automatic token re-mint is unavailable), and
  finding-drafts dir writability. Exit 0 healthy (warnings allowed), 4 on
  any auth-classified failure, 2 on other failures; `--json` emits
  structured check records with an exit hint; `--rw` opts into a hosted
  replica open+sync probe (off by default so doctor stays side-effect-free).
- Wrapper auto-remediation: the `init-project --wrapper` script now retries
  once on exit 4 against a `libsql://` target after minting a fresh token
  with the turso CLI (name resolved from `turso db list`, token exported and
  never echoed). When remediation is impossible it prints a delimited
  `TODO-DB AUTH ALERT` block to stderr — tracker writes are blocked, the two
  remediation commands, do not continue batch work — and exits 4. The
  wrapper stays shellcheck-clean and keeps the existing tool-resolution and
  `TODO_DB_CONFIG` behavior.

## [0.2.0] - 2026-07-25

### Added

- New-project bootstrap: a discovered repo-local `.todo-db/config.json`
  (found by walking up from the current directory like git discovery, or via
  `TODO_DB_CONFIG`) supplies the project identity and database target, with
  precedence explicit flags > `TODO_DB_*` environment variables > discovered
  config. A new `init-project` command runs `init` and scaffolds the repo:
  it writes the committed config file, a `.todo-db/.gitignore` that ignores
  the databases but keeps `config.json` tracked, and (with `--wrapper
  [PATH]`) an executable wrapper script that prefers an installed `todo-db`
  on PATH, falls back to a sibling `../todo-db` checkout, and relies on the
  config file instead of hardcoded identity flags. `--db` records a local
  path (default `.todo-db/standalone.sqlite`) or `libsql://` URL in the
  config; existing scaffolding is never overwritten without `--force`.
- Hosted `verify --run` gate: against a hosted (libsql/https) database,
  `verify --run` refuses to execute the DB-stored command (exit 2) unless
  `TODO_DB_ALLOW_HOSTED_VERIFY_RUN=1` is set, because commands in a shared
  database are written by other actors and executing them locally is a
  lateral code-execution channel. Local databases are unchanged.
- `scripts/turso_acceptance.sh`: a live hosted acceptance script that
  provisions a throwaway Turso database with the `turso` CLI, exercises
  init/create/claim/done/complete, the finding draft→sync→show flow, audit
  verify, and export against the real backend, asserts schema v4, and always
  destroys the database on exit (`--keep` to retain it). Tokens stay in the
  environment and are never echoed. Passed against a real Turso database on
  2026-07-25 — the first live end-to-end validation of the hosted path.
- Findings domain ported from the BenchBox tracker (schema v4, packaged
  migration `004_findings.sql`): credential-free draft capture under
  `~/.todo-db/finding-drafts/<project-id>/` (`TODO_DB_FINDING_DRAFTS_DIR`
  override), a `finding` CLI group
  (`create`/`list`/`show`/`candidates`/`dismiss`/`triage`/`link`/`promote`/
  `sync`) with `sync` as the sole credentialed landing step, disposition
  transitions with reason-required terminal states, atomic finding→item
  promotion, findings tables in the lossless export/restore envelope, audit
  hash-chain coverage of every finding mutation, and a findings banner on
  `ready` plus finding counts in `stats`. Draft parsing requires the new
  `findings` extra (`pyyaml`).
- MIT `LICENSE` file (the metadata already claimed MIT; the text now ships).
- `CHANGELOG.md` and a GitHub Actions CI workflow running lint and the full
  test suite with all extras.

### Fixed

- The migration runner now splits SQL on real statement boundaries (quotes,
  comments, and `BEGIN`/`CASE`...`END` bodies) instead of every bare `;`, so
  trigger-containing migrations and string literals with semicolons apply
  correctly on both backends.

### Changed

- **Breaking:** the implicit default project identity
  (`todo-db-standalone`/`todo-db`) is removed. `init` with no identity from
  flags, environment, or a discovered config is now a hard error, so a
  database can no longer silently bind to the placeholder identity that made
  the mismatch guard useless. Commands other than `init` may still run
  without supplying an identity: they proceed under the identity already
  bound in the database, and the binding check enforces only when the caller
  asserts one. Anyone relying on the old implicit default must now pass
  `--project-id`/`--repository`, set
  `TODO_DB_PROJECT_ID`/`TODO_DB_REPOSITORY`, or adopt `.todo-db/config.json`
  (BenchBox's compat shim always passes explicit identity flags and is
  unaffected).
- `TOOL_VERSION` is now derived from package metadata instead of a duplicated
  constant, so `pyproject.toml` is the single version source.
- The `dev` dependency group now includes `pyyaml` and `libsql`, so a fresh
  `uv sync && uv run pytest` passes without a manual `--all-extras` sync.

## [0.1.0] - 2026-07-21

### Added

- Standalone baseline extracted from the BenchBox in-repo tracker: SQLite and
  Turso/libSQL backends, packaged checksum-verified migrations (schema v3),
  SHA-256 audit chain with Ed25519-signed export manifests, project-identity
  binding, full item lifecycle CLI (`init`, `create`, `claim`, `start`,
  `done`, `defer`, `promote`, `dismiss`, `complete`, `lint`, `check-scope`,
  `verify`, `sweep-stale`, `export`, `restore`, `restore-legacy`,
  `audit verify`, and query commands), legacy YAML import bridge behind the
  `legacy` extra, and operational acceptance gates (two-process claim
  contention, packaging smoke tests, hosted outage fail-closed, credential
  redaction).
