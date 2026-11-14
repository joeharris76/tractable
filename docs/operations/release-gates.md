# Release gates

Artifact correctness is not evidence of usability.

v0.4.2 was tagged, released, and pushed into a downstream consumer with every
gate green: six merged PRs, Python 3.10 and 3.13 CI, the Pi adapter CI, 219
tests, Ruff, scope checks, isolated wheel and sdist consumer smoke tests, and
SHA-256 verification of the downloaded release assets. Every one of those was
true and none of them executed the changed authentication path. The first time a
person used the release it failed. This page exists so that cannot recur
silently.

## Trigger

Both gates below are mandatory for a release whose diff touches any of:

- the publication protocol in `src/tractable/git_backend.py`, including tip
  resolution, conflict classification, retries, and reconciliation;
- the state schema, validation, or claim rules in `src/tractable/store.py`;
- the agent surface in `src/tractable/service.py` or `src/tractable/mcp/`;
- the floor CLI in `src/tractable/cli.py` or migration in
  `src/tractable/migrate.py`.

Releases that touch none of these are not subject to the downstream consumer
gate. When in doubt, run both; they cost minutes.

## Gate 1: scratch state-branch lifecycle

With the release candidate installed in its own venv (the `mcp` extra
included), run the harness with that venv's python:

```sh
<venv>/bin/python scripts/release_gate1.py <venv>/bin "$VERSION"
```

It writes only to a bare remote under a fresh temporary directory, removed on
exit, with `TRACTABLE_*` unset in every child, and prints one
`PASS|FAIL  <check>  <detail>` line per check and a final
`Gate 1: N/N checks passed`; it exits non-zero on any failure. The checks cover
both `--version` outputs, bootstrap and validate, the server's name, version and
`tractable_batch` capability, the bound store with `expect_store` mismatch and
prefix-sibling refusals, create, take, renew, `E_MULTIPLE_CLAIMS` on a second
ordinary take, a second worker refused a live claim, concurrent writers, finish,
drop, `list_items`, `register_batch` with the per-batch claim slot,
`clear_not_before`, an offline stale read and `E_OFFLINE` write, final validate
counts, and state-commit authorship. Record its command line and final line as
the gate's evidence. A gate that passes without touching the publication path
the release changed is the v0.4.2 failure mode; `--help` output is not evidence.

## Gate 2: real downstream consumer

Upgrade one real downstream consumer to the release candidate and run one real
floor-CLI command against its state branch, in a session that was not specially
prepared for the test.

"One real command" means a surviving `tractable` floor verb that does real work
— `tractable validate`, `tractable list`, or `tractable recover --limit 5` — not
`--help` and not `--version`. Agent verbs live only on the MCP server
(`tractable-mcp`), which is not a release gate — the gate exercises the floor
CLI a bootstrap or CI step actually runs.

## Ordering

Both gates run **before** the tag.

A release is not tagged, published, or announced to a consumer until both have
passed. A failed gate blocks the tag; it is not recorded as a follow-up item and
worked around. v0.4.2 dispatched its downstream upgrade after publication, which
is why the defect reached the consumer before it reached anyone who could stop
the release.

## After the tag

A tag push builds the distributions and publishes the GitHub release. It does
not publish to PyPI: that takes an explicit opt-in run on the same tag, after
the GitHub release exists:

```sh
gh workflow run publish.yml --repo joeharris76/tractable --ref "v$VERSION" -f publish_pypi=true
```

That run rebuilds the tag and requires the rebuilt wheel, sdist and
`SHA256SUMS` to be byte-identical to the GitHub release assets before it
uploads anything, then uploads the release assets themselves, so PyPI
receives exactly the released bytes. `distribution-verify` then fails unless
the run is on a `v*` tag and both the build and the upload succeeded, checks
that the files PyPI serves match, and installs `tractable[mcp]==$VERSION` from
PyPI in a fresh environment, without cache, local configuration or inherited
tracker variables. PyPI's simple index can trail the upload by a minute or
more, so the verify step waits up to 8 minutes for the index listing: it polls
`https://pypi.org/simple/tractable/` (bypassing its cache) until the version
appears, then runs the install once. It never retries the install, uploads again, or starts
another publish run; a version still unlisted after 8 minutes fails the job. Both entry points must report the release version and a
real MCP initialize/tools-list exchange must succeed against a scratch remote.

A GitHub release alone is not completed distribution. Missing, yanked or
differing files, an install failure, or a skipped or failed upload blocks
distribution completion and consumer rollout. A tag-push run that is green
has only published the GitHub release.

To rerun the index verification locally against downloaded release assets:

```sh
python3 scripts/verify_index_release.py "$VERSION" /path/to/release/assets
```

This post-publication check supplements, never replaces, the pre-tag gates.
Record the opt-in run's URL and the artifact hashes before announcing
completed distribution. These steps follow it, except that a vendored consumer
(step 2) has its own, earlier condition:

1. **Global tool.** Keep the current receipt and a checksum-verified wheel
   compatible with the active schema for rollback. Replace a URL-pinned or
   checkout installation once with an index requirement:

   ```sh
   uv tool install --force --default-index https://pypi.org/simple 'tractable[mcp]>=1.0.0'
   tractable --version
   tractable-mcp --version
   ```

   Later releases use `uv tool upgrade tractable`. Check that the saved requirement
   in `$(uv tool dir)/tractable/uv-receipt.toml` retains the `mcp` extra and no
   direct `tractable` URL. A direct wheel URL stays pinned even when newer GitHub
   releases exist. A checkout install copies the tree at install time.

   For rollback, use the immutable-wheel command in the README with the saved
   compatible version and digest. Never roll a client back below the active
   schema's minimum version or downgrade authoritative state. If index
   verification fails, leave the existing working installation untouched.

   Rolling back from 1.0 to a 0.x release means switching the project back to the
   still-installed `todo-db` tool (its MCP registration and `.todo-db/` config), not
   reinstalling a pre-1.0 wheel through the tractable URL template: pre-1.0 assets are
   named `todo_db-<version>-py3-none-any.whl` and read only `.todo-db/`.
2. **Vendored consumers.** A repository that vendors the wheel, such as
   BenchBox's `_project/scripts/vendor/`, needs its own upgrade PR. It takes the
   wheel from the GitHub release, so it may adopt the release as soon as the
   release exists and its `SHA256SUMS` verifies; it need not wait for
   `distribution-verify`. Index installs, including the global tool in step 1,
   wait for `distribution-verify` to pass.
3. **Skill text.** When the release changes what agents should do, merge the
   `todo` skill change in skill-sync-skills, then advance every pin that
   serves it: each project's `skill-sync.conf` (see
   [skill-deployment.md](skill-deployment.md)) and the global loader folders
   (`~/.claude/skills`, `~/.codex/skills`, `~/.agents/skills`). For the global
   folders, bump the template rev (`deployment/global/skill-sync.conf`)
   through review in skill-sync-skills, set the same rev in
   `~/.skill-sync-deployment/skill-sync.conf`, then run `tools/skill-sync
   apply`, `verify` and `check` with `-C ~ -f
   ~/.skill-sync-deployment/skill-sync.conf`. There is no snapshot or symlink
   repoint; the skill-sync-skills README has the details.
4. **Running sessions.** An MCP server keeps the version it started with.
   Restart open sessions, then check the server: from 0.11.0, its
   `get_instructions` answer includes a "Bound store" section.

## PyPI publisher setup and recovery

The PyPI account owner registers a pending trusted publisher at
<https://pypi.org/manage/account/publishing/> with project `tractable`, GitHub
owner `joeharris76`, repository `tractable`, workflow filename `publish.yml`,
and environment `pypi`. If the project already exists, verify ownership and
configure its active publisher instead. Match these fields to the workflow;
never claim an unrelated project, add a persistent API token, or remove
environment protections to bypass an authentication failure.

After registration, start the opt-in run on the release tag (above). If its
`pypi-publish` job fails for a transient reason, rerun the failed jobs of that
same run with `gh run rerun <run-id> --failed`; a rerun keeps the opt-in. A
dispatch from a branch such as `main` cannot publish: `pypi-publish` requires
a `v*` tag ref, and the build checks that the tag matches the package
version.

If the rebuilt files differ from the GitHub release assets, `pypi-publish`
stops before uploading. Find the source of the non-reproducible build and
release a new version; never move a tag or replace published files. A
version already on PyPI is checked for parity, never overwritten.

A dispatch runs the workflow file stored at its tag, so this procedure applies
only to tags created after it was added. Earlier tags, v0.15.0 and older,
either lack the opt-in input or publish without these checks. They are not
published to PyPI: never dispatch `publish_pypi=true` on them. The first
release published to the index is a new tag.

## Before an irreversible schema cutover

The operator must inventory every client with access to the affected state
branch, including global tools, vendored consumers, CI, and running MCP
sessions. Record each owner, install source, client version, state remote and
branch, and whether its process was restarted or stopped. Do not record
credentials or private remote URLs in public evidence.

Publish and verify the compatible client before cutover. Upgrade each affected
client, restart its server, and verify its running version, bound store, and
schema capability; alternatively stop it and prevent restart with the old
binary. Installed metadata alone does not prove a running server was upgraded.
An unknown, unreachable, or incompatible client blocks cutover until its owner
resolves it. Keep a checksum-verified rollback client that can read the new
schema; rollback never means downgrading state.

Preserve the required explicit cutover confirmation. On `E_SCHEMA`, agents
still stop and ask a human to upgrade the package; this procedure authorizes
neither automatic migration nor bypassing the recovery policy.


## Evidence

Record in the release PR body:

- the command line used for gate 1 and its final line;
- the consumer, the floor-CLI command run for gate 2, the state branch it
  addressed, and whether it succeeded;
- the date and the operator.

Record no consumer secret and no private remote URL beyond what the release
already names.

## What does not satisfy these gates

State this plainly when reviewing a release PR. None of the following is
evidence that the release is usable:

- green CI on every matrix entry;
- a passing unit and integration suite, at any count;
- isolated wheel, sdist, or package smoke tests;
- checksum verification of downloaded release assets;
- a clean `git status` and no open PRs.

All five held for v0.4.2.

## Automation boundary

CI runs `scripts/release_gate1.py` on every push and pull request, in the
`release-gate1` job: it builds the wheel from the tree with `uv build`, installs
it with the `mcp` extra into a fresh venv, and runs the harness against a
disposable bare remote. That proves the tree's wheel passes the lifecycle; it
does not replace the operator's own run on the release candidate, which a
release still records. Gate 2 stays a local operator step. CI also runs a
scratch state-branch smoke (bootstrap/validate/list against a disposable bare
remote) plus `uv sync --locked`, which fails the build when the committed
lockfile drifts from the manifest. CI never touches a real state branch:
routine automation must not write to authoritative task state.
