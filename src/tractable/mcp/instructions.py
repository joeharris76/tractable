"""Workflow instructions surface for the MCP server.

Short protocol guidance loaded once. It names the loop and the error
codes; it does not inject guides, task bodies, or the index.
"""

from __future__ import annotations

from .target import ResolvedTarget

INSTRUCTIONS = """\
# tractable agent workflow

Drive the tracker through MCP tools. Mutations return a compact
acknowledgement; lists return brief rows (id/title/priority/status).

## The loop

1. `list_items` -- brief rows; filters: status, priority, text,
   ready_only; paging: limit (default 5) + cursor.
2. `show_item` -- one task with needs, readiness, and sections.
   Large fields arrive as section reads: field/offset/budget.
3. `register_batch` -- register the immutable repository/integration contract
   before enrolling prepared members. The first registration on a store below
   schema 4 needs `confirm_schema3_cutover=true` once every process with
   state-branch access is stopped or upgraded; older clients refuse the store
   with `E_SCHEMA`. `develop_ref` (refs/remotes/...) is fixed here.
4. `create_item` -- a task; optional batch metadata is `batch_id`,
   `member_id`, `implementation_dependencies`.
5. `take` -- claim a task; returns the claim generation plus enough
   context to begin work. One live claim per worker.
6. `prepare` -- persist a member receipt and release its claim, not done.
   Needs the registered batch, explicit same-batch edge, actual member base,
   accepted/current heads, frozen scope, clean exact checkout, and passed
   bounded-suite evidence.
7. `bind_batch_pr` -- bind the one final PR; move its head with
   `expected_previous_head`. `advance_batch_head` records a leader fix or
   develop merge; `recover_batch_owner` replaces a dead owner; `show_batch`
   reads the registry, receipts and log. Only the batch owner claims members.
8. `abort_batch` -- owner-only, resumable, after all member claims are
   released. Invalidates prepared/final evidence, keeps any bound final PR
   receipt, and returns non-terminal members to ordinary claim/finish outside
   the archived batch. Refused once a member is done, so partial closeout
   resumes without reopening it.
9. `release` -- hand the claim back (needs the generation from take).
10. `finish` -- close the task (needs the generation from take). Prepared
   tasks additionally require current combined-tree evidence covering every
   prepared member and the registered final PR.
   `drop` abandons a task and requires a `reason` (stored as `drop_reason` in
   the same commit; no separate note needed); a live claim needs its generation.
11. `renew` -- extend a long-running claim (same generation, no
   progress milestones required).

`update_item` edits title/priority/description/needs/sections/status
(open/blocked moves only; closing goes through finish/drop). Batch metadata
is explicit, immutable after assignment, and never inferred from an ordinary
dependency. A prepared member cannot be dropped while its batch is active;
complete or abort the batch instead. Text fields are replaced wholesale: add a
note with `context_append`, clear a field with an empty value. `defer`
summaries are one line, 200 characters at most; terminal tasks refuse them.

`defer` records deferred work on a task without changing its status;
`list_deferrals` reads the queue (open by default, `all` for every
resolution, `from_item` to scope one task). `promote_deferral` resolves
an open deferral into a linked planning item (created, or an existing
`to_item`); `dismiss_deferral` drops one with a recorded reason.
`finish` refuses while open deferrals exist (`E_OPEN_DEFERRALS` names
the blocking ids); `show_item` reports them as `open_deferrals`.

`not_before` (future RFC 3339) holds an open task until then; `""` clears (or `clear_not_before`).

## Session history

Mutations record actor, session, operation, and operation ID per task.
After `take`, read `show_item`'s `sessions`; before
`release`/`finish`, leave resumption notes in `context`. Aborts,
invalidations, and dead-session takeovers record on members;
`takeover` needs the holder seen plus why it is dead. Deferrals record
`defer`/`promote`/`dismiss` on the source task; a promotion that
creates its successor annotates the new item too.

## Responses

    {"ok": true,  "data": {...}}
    {"ok": false, "code": "E_...", "error": "...", "recovery": [...], "kind": "gate|error"}

`kind: "gate"` is an expected result to act on; `kind: "error"` is a
failure -- stop and report it. Read `recovery` before improvising.
Responses are capped at 16 KiB; list cursors are scoped to a state
revision.

## Codes

- `E_NOTHING_READY` -- nothing claimable. Report it; do not invent work.
- `E_OPEN_DEFERRALS` -- open deferrals block finish. Promote or dismiss
  the named ids, then retry.
- `E_MULTIPLE_CLAIMS` -- you already hold a claim. Finish or release it.
  A worker holds one claim outside any batch and one per active batch, so
  leaders of different batches on one server do not block each other.
  Agents on one server that work the same batch (or no batch) share that
  slot; run one server per agent with a distinct --actor to claim concurrently.
- `E_CLAIM_STALE` -- wrong generation or another holder. Show and retry.
- `E_BATCH_OWNER` -- only the batch owner claims its members. Ask the named
  owner, or recover the batch if it is dead.
- `E_BATCH_HEAD` -- head lineage refused. Record leader commits first.
- `E_CONFLICT` -- someone changed the task first. Re-read, re-evaluate.
- `E_CURSOR_STALE` -- state moved under your pages. Restart listing.
- `E_OVERSIZED` -- one field exceeds the cap. Use its section read.
- `E_OFFLINE` -- remote unreachable. Reads may be cached (marked
  stale); mutations fail rather than succeeding locally.
- `E_UNKNOWN` -- outcome undetermined; reconcile with the operation ID.
- `E_NO_PRINCIPAL` -- call `get_instructions`, then retry.

One server instance is one logical worker. Default identities are isolated per
server, even for clients with the same name. Use a stable, unique --actor for
restart continuity and do not run that actor concurrently. Claims are
cooperative, not access control.
"""

_STORE_ANCHOR = "\n## The loop\n"


def render_instructions(target: ResolvedTarget | None, worker: str | None = None) -> str:
    """The workflow text with a 'Bound store' section naming the tracker in use.

    The state remote is shown without credentials. *worker* is the principal
    when it is already known; before the first tool call it is still pending.
    """
    if target is None:
        return INSTRUCTIONS
    section = (
        "## Bound store\n\n"
        f"Remote: {target.state_ref.display_remote}\n"
        f"Branch: {target.state_ref.branch}\n"
        f"Resolved from: {target.source_text()}\n"
        f"Worker: {worker or 'pending (set on the first tool call)'}\n\n"
        "Every write goes to this store. Before the first write, confirm it is the tracker for\n"
        "the project you are working on: pass that project's repository name as `expect_store`\n"
        "to `get_instructions`. A mismatch is refused; stop, then restart the server with the\n"
        "project's `--config` or from the project directory.\n"
    )
    head, found, tail = INSTRUCTIONS.partition(_STORE_ANCHOR)
    if not found:
        return f"{INSTRUCTIONS}\n{section}"
    return f"{head}\n{section}{found}{tail}"
